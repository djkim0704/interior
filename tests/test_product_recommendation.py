from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.product_recommendation import (  # noqa: E402
    SerpApiShoppingProvider,
    _merge_and_validate_mood_analysis,
    analyze_mood_context,
    calculate_text_style_score,
    clean_title,
    deduplicate_products,
    generate_search_queries,
    merge_style_terms,
    normalize_scores,
    recommend_furniture,
    select_diverse_products,
    validate_product,
)


MOODS = {
    "따뜻하고 아늑한": 0.55,
    "내추럴 우드": 0.30,
    "베이지": 0.15,
}
OBSERVED = {
    "colors": ["아이보리", "베이지"],
    "materials": ["패브릭", "오크"],
    "forms": ["라운드", "낮은"],
}


def product(
    product_id: str,
    title: str,
    *,
    brand: str = "",
    shop: str = "몰",
    image: str = "https://example.com/image.jpg",
    category: str = "가구/인테리어>거실가구>소파",
) -> dict[str, Any]:
    return {
        "productId": product_id,
        "title": title,
        "link": f"https://example.com/{product_id}",
        "image": image,
        "price": 100000,
        "shop": shop,
        "brand": brand,
        "maker": "",
        "category1": category,
        "category2": "",
        "category3": "",
        "category4": "",
    }


class MockProvider:
    def __init__(self, items: list[dict[str, Any]]) -> None:
        self.items = items
        self.queries: list[str] = []

    def search(
        self,
        query: str,
        display: int = 30,
    ) -> list[dict[str, Any]]:
        self.queries.append(query)
        return [dict(item) for item in self.items[:display]]


class FailingImageSimilarity:
    def similarity(
        self,
        reference_image: str | Path,
        product_image_url: str,
    ) -> float | None:
        raise RuntimeError("expected test failure")


class RecommendationTests(unittest.TestCase):
    def test_serpapi_disk_cache_survives_provider_recreation(self) -> None:
        response = MagicMock()
        response.status_code = 200
        response.json.return_value = {
            "shopping_results": [
                {
                    "product_id": "cached-1",
                    "title": "캐시 소파",
                    "link": "https://example.com/cached-1",
                    "thumbnail": "https://example.com/cached-1.jpg",
                    "extracted_price": 120000,
                    "source": "테스트몰",
                }
            ]
        }

        with tempfile.TemporaryDirectory() as cache_dir:
            with patch(
                "backend.product_recommendation.requests.Session"
            ) as session_factory:
                http = session_factory.return_value.__enter__.return_value
                http.get.return_value = response

                first_provider = SerpApiShoppingProvider(
                    "test-key",
                    retries=0,
                    cache_dir=cache_dir,
                    cache_ttl_seconds=3600,
                )
                first = first_provider.search("패브릭 소파", display=5)

                second_provider = SerpApiShoppingProvider(
                    "test-key",
                    retries=0,
                    cache_dir=cache_dir,
                    cache_ttl_seconds=3600,
                )
                second = second_provider.search("패브릭 소파", display=5)

        self.assertEqual(first, second)
        self.assertEqual(first[0]["productId"], "cached-1")
        self.assertEqual(http.get.call_count, 1)

    def test_top_three_moods_sum_to_one(self) -> None:
        result = analyze_mood_context(
            "따뜻하고 아늑한 원목 베이지 방",
            ["Warm Cozy", "Natural Wood"],
            "warm_cozy_c05/gallery/a.jpg",
        )
        self.assertEqual(len(result["mood_scores"]), 3)
        self.assertAlmostEqual(sum(result["mood_scores"].values()), 1.0)
        self.assertEqual(result["primary_mood"], "따뜻하고 아늑한")

    def test_every_explicit_mood_tag_locks_its_search_family(self) -> None:
        canonical_tags = {
            "Cool Airy": "시원하고 밝은",
            "Warm Cozy": "따뜻하고 아늑한",
            "Natural Wood": "내추럴 우드",
            "Cute Pastel": "파스텔",
            "Vintage Retro": "빈티지 레트로",
            "Luxury Modern": "럭셔리 모던",
            "Minimal White": "미니멀 화이트",
            "Modern Grey": "모던 그레이",
            "Monochrome": "모노톤",
            "Plant Green": "플랜테리어 그린",
        }
        for tag, expected in canonical_tags.items():
            with self.subTest(tag=tag):
                result = analyze_mood_context(
                    tag,
                    [tag],
                    "monochrome_minimal/example.jpg",
                )
                self.assertEqual(result["primary_mood"], expected)
                self.assertEqual(result["_locked_moods"], [expected])

    def test_gemini_cannot_replace_an_explicit_cool_choice(self) -> None:
        base = analyze_mood_context(
            "Cool Airy",
            ["Cool Airy"],
            "monochrome_minimal/example.jpg",
        )
        merged = _merge_and_validate_mood_analysis(
            base,
            {
                "mood_scores": {
                    "모노톤": 1.0,
                },
                "colors": ["블랙", "화이트"],
                "materials": ["스틸"],
                "forms": ["직선형"],
            },
        )
        self.assertEqual(merged["primary_mood"], "시원하고 밝은")
        queries = generate_search_queries(
            "sofa",
            merged["mood_scores"],
            {
                "colors": merged["colors"],
                "materials": merged["materials"],
                "forms": merged["forms"],
            },
            "session",
        )
        query_text = " ".join(queries)
        self.assertIn("아쿠아블루", query_text)
        self.assertNotIn("블랙앤화이트", query_text)

    def test_merge_terms_weighted_and_deduplicated(self) -> None:
        terms = merge_style_terms(MOODS, "styles")
        self.assertEqual(len(terms), len(set(terms)))
        self.assertLess(terms.index("코지"), terms.index("자연주의"))

    def test_primary_mood_signature_leads_search_queries(self) -> None:
        queries = generate_search_queries(
            "sofa", MOODS, OBSERVED, "session", 0
        )
        self.assertTrue(queries[0].startswith("카멜브라운 테디패브릭"))

    def test_secondary_mood_terms_do_not_leak_into_queries(self) -> None:
        queries = generate_search_queries(
            "sofa",
            {"시원하고 밝은": 0.8, "모던 그레이": 0.15, "내추럴 우드": 0.05},
            {
                "colors": ["아쿠아블루", "스모크그레이"],
                "materials": ["투명아크릴", "콘크리트", "오크"],
                "forms": [],
            },
            "session",
        )
        query_text = " ".join(queries)
        self.assertIn("아쿠아블루", query_text)
        self.assertNotIn("스모크그레이", query_text)
        self.assertNotIn("콘크리트", query_text)
        self.assertNotIn("오크", query_text)

    def test_recommendation_uses_one_provider_query(self) -> None:
        provider = MockProvider([])
        recommend_furniture(
            "sofa",
            {"파스텔": 0.8, "블랙": 0.2},
            {"colors": [], "materials": [], "forms": []},
            None,
            "session",
            0,
            set(),
            provider=provider,
        )
        self.assertEqual(len(provider.queries), 1)

    def test_forbidden_material_category_pair_removed(self) -> None:
        queries = generate_search_queries(
            "table",
            {"따뜻하고 아늑한": 1.0},
            {"colors": [], "materials": ["부클"], "forms": []},
            "session",
        )
        self.assertFalse(any("부클" in query for query in queries))

    def test_query_has_no_more_than_two_attributes(self) -> None:
        queries = generate_search_queries(
            "sofa", MOODS, OBSERVED, "session", 0
        )
        self.assertEqual(len(queries), 1)
        self.assertTrue(all(len(query.split()) <= 3 for query in queries))

    def test_query_generation_is_deterministic_per_round(self) -> None:
        first = generate_search_queries("sofa", MOODS, OBSERVED, "abc", 2)
        second = generate_search_queries("sofa", MOODS, OBSERVED, "abc", 2)
        changed = generate_search_queries("sofa", MOODS, OBSERVED, "abc", 3)
        self.assertEqual(first, second)
        self.assertNotEqual(first, changed)

    def test_clean_title(self) -> None:
        self.assertEqual(
            clean_title("<b>아이보리</b>&nbsp;  패브릭 소파"),
            "아이보리 패브릭 소파",
        )

    def test_blocks_cover_parts_and_miniatures(self) -> None:
        for title in ("소파커버", "소파 다리만", "미니어처 소파"):
            valid, reason = validate_product(product("1", title), "소파")
            self.assertFalse(valid)
            self.assertIn(reason, {"blocked_global_word", "blocked_category_word"})

    def test_headboard_only_product_is_not_a_bed(self) -> None:
        item = product(
            "head",
            "패브릭 침대 벨벳 쿠션 저상형 헤드",
            category="가구/인테리어 침실가구 침대 침대프레임",
        )
        valid, reason = validate_product(item, "침대")
        self.assertFalse(valid)
        self.assertEqual(reason, "incomplete_bed_product")

    def test_complete_bed_with_headboard_is_allowed(self) -> None:
        item = product(
            "bed",
            "쿠션 헤드보드 수납 침대프레임",
            category="가구/인테리어 침실가구 침대 침대프레임",
        )
        valid, reason = validate_product(item, "침대")
        self.assertTrue(valid)
        self.assertIsNone(reason)

    def test_product_id_duplicate_removed(self) -> None:
        items = [product("1", "원목 소파"), product("1", "다른 소파")]
        self.assertEqual(len(deduplicate_products(items)), 1)

    def test_color_option_duplicate_removed(self) -> None:
        items = [
            product("1", "브랜드 라운드 소파 화이트 2인용"),
            product("2", "브랜드 라운드 소파 블랙 3인용"),
        ]
        self.assertEqual(len(deduplicate_products(items)), 1)

    def test_previous_products_are_preferentially_excluded(self) -> None:
        names = [
            "아르코", "보네르", "클라우드", "오슬로", "라비앙", "마레",
            "로웰", "세이지", "브릭", "코펜", "누보", "모먼트",
        ]
        items = [
            product(str(index), f"{names[index]} 패브릭 소파", brand=f"B{index}")
            for index in range(len(names))
        ]
        selected, _, _ = recommend_furniture(
            "sofa",
            MOODS,
            OBSERVED,
            None,
            "s",
            0,
            {"0", "1"},
            provider=MockProvider(items),
        )
        self.assertFalse({"0", "1"} & {item["productId"] for item in selected})

    def test_tied_score_normalization_is_safe(self) -> None:
        self.assertEqual(normalize_scores([4.0, 4.0]), [1.0, 1.0])
        self.assertEqual(normalize_scores([4.0]), [1.0])

    def test_image_similarity_failure_falls_back_to_text(self) -> None:
        names = ["아르코", "보네르", "클라우드", "오슬로", "라비앙", "마레", "로웰"]
        items = [
            product(str(index), f"{names[index]} 아이보리 패브릭 소파", brand=f"B{index}")
            for index in range(len(names))
        ]
        selected, _, _ = recommend_furniture(
            "소파",
            MOODS,
            OBSERVED,
            ROOT / "missing.jpg",
            "s",
            0,
            set(),
            provider=MockProvider(items),
            image_similarity_service=FailingImageSimilarity(),
        )
        self.assertEqual(len(selected), len(items))
        self.assertTrue(all(item["_image_similarity"] is None for item in selected))

    def test_text_score_rewards_observed_attribute(self) -> None:
        matching = calculate_text_style_score(
            product("1", "아이보리 패브릭 소파"),
            OBSERVED,
            MOODS,
        )
        other = calculate_text_style_score(
            product("2", "차콜 메탈 소파"),
            OBSERVED,
            MOODS,
        )
        self.assertGreater(matching, other)

    def test_brand_limit_in_diverse_selection(self) -> None:
        names = ["아르코", "보네르", "클라우드", "오슬로", "라비앙", "마레", "로웰", "세이지"]
        ranked = [
            {
                **product(
                    str(index),
                    f"{names[index]} 소파",
                    brand=("A" if index < 4 else f"B{index}"),
                    shop=f"S{index}",
                ),
                "_final_score": 1.0 - index * 0.03,
            }
            for index in range(8)
        ]
        selected = select_diverse_products(ranked, 5)
        self.assertEqual(len(selected), 5)
        self.assertLessEqual(
            sum(item["brand"] == "A" for item in selected),
            2,
        )

    def test_diversity_constraints_relax_when_candidates_are_few(self) -> None:
        names = ["아르코", "보네르", "클라우드", "오슬로", "라비앙"]
        ranked = [
            {
                **product(str(index), f"{names[index]} 소파", brand="한브랜드", shop="한몰"),
                "_final_score": 1.0 - index * 0.05,
            }
            for index in range(5)
        ]
        self.assertEqual(len(select_diverse_products(ranked, 5)), 5)


if __name__ == "__main__":
    unittest.main()
