from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2 import product_dimensions as pd  # noqa: E402


class ParseTests(unittest.TestCase):
    def assertDims(self, text, w, d, h=None):
        found = pd.parse_dimensions(text)
        self.assertIsNotNone(found, text)
        self.assertAlmostEqual(found["w_m"], w, places=3, msg=text)
        self.assertAlmostEqual(found["d_m"], d, places=3, msg=text)
        if h is None:
            self.assertIsNone(found["h_m"], text)
        else:
            self.assertAlmostEqual(found["h_m"], h, places=3, msg=text)

    def test_common_korean_listing_formats(self) -> None:
        self.assertDims("원목 책상 1200x600x750 화이트", 1.2, 0.6, 0.75)
        self.assertDims("모던 수납장 80 x 40 x 120cm", 0.8, 0.4, 1.2)
        self.assertDims("패브릭 소파 2100*900*850mm", 2.1, 0.9, 0.85)
        self.assertDims("러그 160×230", 1.6, 2.3)
        self.assertDims("W1200 D600 H750 사무용 책상", 1.2, 0.6, 0.75)
        self.assertDims("가로 120cm 세로 60cm 높이 75cm 테이블", 1.2, 0.6, 0.75)
        self.assertDims("폭:80 깊이:45 높이:180 (cm) 책장", 0.8, 0.45, 1.8)

    def test_ignores_model_numbers_and_implausible_values(self) -> None:
        self.assertIsNone(pd.parse_dimensions("2024 신상 LED 무드등"))
        self.assertIsNone(pd.parse_dimensions("모델명 SK-1200 블랙"))
        self.assertIsNone(pd.parse_dimensions("5000x6000x7000 대형"))  # 5m 넘는 가구는 없다
        self.assertIsNone(pd.parse_dimensions(""))


class SizeClassTests(unittest.TestCase):
    def test_bed_size_classes(self) -> None:
        self.assertEqual(pd.size_class_dimensions("bed", "호텔식 퀸 침대 프레임")["w_m"], 1.5)
        self.assertEqual(pd.size_class_dimensions("bed", "슈퍼싱글 저상형 침대")["w_m"], 1.1)
        self.assertEqual(pd.size_class_dimensions("bed", "라지킹 패밀리 침대")["w_m"], 1.8)

    def test_sofa_seats(self) -> None:
        found = pd.size_class_dimensions("sofa", "북유럽 3인용 패브릭 소파")
        self.assertEqual((found["w_m"], found["seats"]), (2.0, 3))


class ResolveTests(unittest.TestCase):
    def test_title_wins_and_axes_are_oriented(self) -> None:
        # 침대는 폭 < 길이가 되도록 축을 맞춘다
        found = pd.resolve({"type": "bed", "title": "원목 침대 2000x1500"}, None, fetch=False)
        self.assertEqual((found["w_m"], found["d_m"], found["dimension_source"], found["measured"]), (1.5, 2.0, "title", True))
        # 책상은 폭 > 깊이
        found = pd.resolve({"type": "desk", "title": "책상 60x120"}, None, fetch=False)
        self.assertEqual((found["w_m"], found["d_m"]), (1.2, 0.6))

    def test_falls_back_through_sources(self) -> None:
        product = {"type": "bed", "title": "모던 퀸 침대", "visual_profile": {}}
        self.assertEqual(pd.resolve(product, None, fetch=False)["dimension_source"], "size_class")
        product["visual_profile"] = {"dimensions": {"w_m": 1.6, "d_m": 2.05, "h_m": 0.9}}
        found = pd.resolve(product, None, fetch=False)
        self.assertEqual((found["dimension_source"], found["w_m"], found["h_m"]), ("image", 1.6, 0.9))
        found = pd.resolve({"type": "lamp", "title": "무드등"}, None, fetch=False)
        self.assertEqual((found["dimension_source"], found["measured"]), ("type_default", False))

    def test_page_is_used_when_title_has_no_size(self) -> None:
        page = '<html><head><meta name="description" content="편안한 2인 소파. 사이즈 1600 x 850 x 800 mm"></head></html>'
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(pd, "fetch_page", return_value=page) as fetch:
            found = pd.resolve({"type": "sofa", "title": "소파", "link": "https://shop.example/p/1"}, tmp)
        fetch.assert_called_once()
        self.assertEqual((found["dimension_source"], found["w_m"], found["d_m"]), ("page", 1.6, 0.85))


class PageExtractTests(unittest.TestCase):
    def test_json_ld_product_size(self) -> None:
        page = (
            '<script type="application/ld+json">{"@type":"Product","name":"책상",'
            '"width":{"value":120,"unitText":"cm"},"depth":{"value":60,"unitText":"cm"},'
            '"height":{"value":72,"unitText":"cm"}}</script>'
        )
        found = pd.extract_from_page(page)
        self.assertEqual((found["w_m"], found["d_m"], found["h_m"], found["pattern"]), (1.2, 0.6, 0.72, "json_ld"))

    def test_body_text_near_size_keyword_only(self) -> None:
        page = "<div>상품번호 20241201 가격 129000</div><p>제품 규격: 가로 90 / 깊이 45 / 높이 110 cm</p>"
        found = pd.extract_from_page(page)
        self.assertEqual((found["w_m"], found["d_m"], found["h_m"]), (0.9, 0.45, 1.1))

    def test_fetch_is_disabled_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"PRODUCT_PAGE_FETCH": "0"}):
            self.assertIsNone(pd.fetch_page("https://shop.example/p/1", Path(tmp)))


if __name__ == "__main__":
    unittest.main()
