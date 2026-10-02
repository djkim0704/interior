from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.product_recommendation import recommend_furniture  # noqa: E402
from model2 import scene_graph, spatial_fit  # noqa: E402

sys.path.insert(0, str(ROOT / "tests"))
from test_product_recommendation import MOODS, OBSERVED, MockProvider, product  # noqa: E402


def _room(objects: list[dict], width: float = 3.6, depth: float = 4.0) -> dict:
    graph = {
        "schema": scene_graph.SCHEMA,
        "room": {"width_m": width, "depth_m": depth, "estimated": False, "scale_source": "user"},
        "objects": [],
    }
    for index, obj in enumerate(objects):
        graph["objects"].append(
            {"category": obj["type"], "label": obj["type"], "rotation_deg": 0.0, "wall": "none",
             "confidence": 0.9, "source": "ai", "source_index": index, **obj}
        )
    return scene_graph.sync_legacy(graph)


class SpatialFitTests(unittest.TestCase):
    def test_product_that_fits_scores_high_with_reason(self) -> None:
        room = _room([{"id": "bed_1", "type": "bed", "cx": 0.8, "cy": 1.1, "w_m": 1.5, "d_m": 2.0, "wall": "top"}])
        fit = spatial_fit.make_scorer(room, "desk")({"title": "원목 책상 1000x500x740"})
        self.assertTrue(fit["fits"])
        self.assertEqual(fit["space"], 1.0)
        self.assertEqual(fit["dimensions"]["dimension_source"], "title")
        self.assertIn("통로 60cm를 유지한 채 놓을 수 있어요", fit["reasons"])

    def test_product_bigger_than_room_does_not_fit(self) -> None:
        fit = spatial_fit.make_scorer(_room([], width=2.5, depth=3.0), "sofa")({"title": "대형 소파 3500x1000x850"})
        self.assertFalse(fit["fits"])
        self.assertEqual(fit["space"], 0.0)
        self.assertIn("방에 들어가지 않는 크기예요", fit["reasons"])

    def test_no_free_spot_in_crowded_room(self) -> None:
        objects = [
            {"id": "bed_1", "type": "bed", "cx": 1.0, "cy": 1.0, "w_m": 2.0, "d_m": 2.0},
            {"id": "wardrobe_1", "type": "wardrobe", "cx": 1.0, "cy": 2.6, "w_m": 2.0, "d_m": 1.2},
        ]
        fit = spatial_fit.make_scorer(_room(objects, width=2.0, depth=3.2), "sofa")({"title": "소파 1800x900x800"})
        self.assertFalse(fit["fits"])

    def test_replacement_prefers_similar_size_at_same_spot(self) -> None:
        room = _room([{"id": "sofa_1", "type": "sofa", "cx": 1.8, "cy": 3.5, "w_m": 2.0, "d_m": 0.9, "wall": "bottom", "rotation_deg": 180.0}])
        scorer = spatial_fit.make_scorer(room, "sofa", replace_id="sofa_1")
        same = scorer({"title": "3인 소파 2000x900x850"})
        tiny = scorer({"title": "1인 소파 800x800x800"})
        self.assertIn("교체할 가구 자리에 들어가요", same["reasons"])
        self.assertGreater(same["size"], tiny["size"])
        self.assertEqual(same["placement"], {"cx": 1.8, "cy": 3.5})

    def test_unknown_size_is_not_measured(self) -> None:
        # 표준 크기로 지어내 재지 않는다
        fit = spatial_fit.make_scorer(_room([]), "desk")({"title": "감성 책상"})
        self.assertEqual((fit["dimensions"], fit["space"], fit["size"], fit["fits"]), (None, None, None, None))
        self.assertIn("치수 정보가 없어 공간 적합도를 재지 못했어요", fit["reasons"])

    def test_combine_pushes_non_fitting_down(self) -> None:
        good = spatial_fit.combine(0.6, {"space": 1.0, "size": 1.0, "fits": True})
        great_mood_but_no_room = spatial_fit.combine(1.0, {"space": 0.0, "size": 0.0, "fits": False})
        self.assertGreater(good, great_mood_but_no_room)


class RecommendWithSpatialTests(unittest.TestCase):
    def test_spatial_scorer_reorders_and_attaches_fit(self) -> None:
        items = [
            product("big", "아이보리 패브릭 라운드 소파 3500x1000x850"),
            product("ok", "베이지 패브릭 소파 1800x850x800", shop="다른몰"),
        ]
        room = _room([], width=2.5, depth=3.0)
        selected, _, _ = recommend_furniture(
            "sofa", MOODS, OBSERVED, None, "s", 0, set(),
            provider=MockProvider(items),
            spatial_scorer=spatial_fit.make_scorer(room, "sofa"),
        )
        self.assertEqual(selected[0]["productId"], "ok")
        self.assertTrue(selected[0]["fit"]["fits"])
        big = next(item for item in selected if item["productId"] == "big")
        self.assertFalse(big["fit"]["fits"])
        self.assertIn("mood", big["fit"])

    def test_without_scorer_behaviour_is_unchanged(self) -> None:
        items = [product("a", "베이지 패브릭 소파")]
        selected, _, _ = recommend_furniture("sofa", MOODS, OBSERVED, None, "s", 0, set(), provider=MockProvider(items))
        self.assertNotIn("fit", selected[0])


if __name__ == "__main__":
    unittest.main()
