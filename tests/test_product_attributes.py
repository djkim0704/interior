from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.genai import types  # noqa: E402

from model2 import product_attributes as pa  # noqa: E402
from model2 import scene_graph  # noqa: E402
from model2.floorplan_3d import build_scene  # noqa: E402


class NormalizeTests(unittest.TestCase):
    def test_clamps_enums_numbers_and_dimensions(self) -> None:
        out = pa.normalize(
            {
                "primary_color": "#A0B0C0",
                "material": "Velvet",
                "leg_style": "hairpin",
                "leg_height_ratio": 3,
                "has_armrests": 1,
                "back_height": "medium",
                "seat_count": 12,
                "visible_dimensions_cm": {"width": 160, "depth": 85, "height": 80},
            }
        )
        self.assertEqual(out["primary_color"], "#a0b0c0")
        self.assertEqual(out["material"], "mixed")
        self.assertEqual(out["leg_height_ratio"], 0.6)
        self.assertTrue(out["has_armrests"])
        self.assertEqual(out["back_height"], "none")
        self.assertEqual(out["seat_count"], 6)
        self.assertEqual(out["dimensions"], {"w_m": 1.6, "d_m": 0.85, "h_m": 0.8})

    def test_no_dimensions_unless_printed(self) -> None:
        self.assertIsNone(pa.normalize({"visible_dimensions_cm": {"width": None}})["dimensions"])
        self.assertIsNone(pa.normalize({"visible_dimensions_cm": {"width": 2, "depth": 900}})["dimensions"])


class AttributesForTests(unittest.TestCase):
    def test_nothing_observed_means_no_attributes(self) -> None:
        # 종류별 기본 형태는 없다. 사진을 보지 않았으면 비어 있다
        self.assertEqual(pa.attributes_for("sofa", {}), {})
        # 로컬 프로필의 'none'은 관찰값이 아니므로 버린다
        local = {"leg_style": "none", "has_armrests": False, "analysis_source": "local"}
        self.assertEqual(pa.attributes_for("sofa", local), {})
        # 로컬 표기 wood → wood_legs
        self.assertEqual(pa.attributes_for("table", {"leg_style": "wood"})["leg_style"], "wood_legs")

    def test_gemini_observations_win_even_when_negative(self) -> None:
        observed = pa.normalize({"leg_style": "none", "has_armrests": False, "seat_count": 2})
        attrs = pa.attributes_for("sofa", observed)
        self.assertEqual((attrs["leg_style"], attrs["has_armrests"], attrs["seat_count"]), ("none", False, 2))


class ExtractTests(unittest.TestCase):
    def test_extract_parses_json_reply(self) -> None:
        reply = {"material": "leather", "leg_style": "metal_legs", "seat_count": 2, "confidence": 0.9}

        class Models:
            def generate_content(self, *, model, contents, config=None):
                self.config = config
                return types.GenerateContentResponse(
                    candidates=[types.Candidate(content=types.Content(parts=[types.Part(text=json.dumps(reply))]))]
                )

        class Client:
            models = Models()

        out = pa.extract(Client(), b"jpeg", "image/jpeg", title="가죽 2인 소파", kind="sofa", model="gemini-3.5-flash-lite")
        self.assertEqual((out["material"], out["leg_style"], out["analysis_source"]), ("leather", "metal_legs", "gemini"))
        # Gemini 3 계열은 thinking_level로 최소화한다(thinking_budget=0은 400)
        self.assertIsNotNone(Client.models.config.thinking_config.thinking_level)
        # pro 모델이면 thinking 토큰이 한도를 함께 쓰므로 넉넉해야 한다
        self.assertGreaterEqual(Client.models.config.max_output_tokens, 8192)


class SceneIntegrationTests(unittest.TestCase):
    def test_product_size_height_and_attrs_reach_3d(self) -> None:
        graph = scene_graph.from_analysis(
            {"room": {"aspect_ratio_width_to_depth": 1.0}, "objects": []}, width_m=4.0, depth_m=4.0
        )
        graph["objects"].append(
            {"type": "sofa", "label": "소파", "x": 0.5, "y": 0.5, "w": 0.0, "h": 0.0, "wall": "bottom",
             "source": "selected_product", "product_marker": 1, "w_m": 1.6, "d_m": 0.85, "h_m": 0.78,
             "attrs": {"seat_count": 2, "has_armrests": False}, "dimension_source": "title"}
        )
        scene = build_scene(scene_graph.ensure(graph))
        sofa = scene["objects"][0]
        self.assertEqual((sofa["w_m"], sofa["d_m"], sofa["height_m"]), (1.6, 0.85, 0.78))
        self.assertEqual((sofa["attrs"]["seat_count"], sofa["attrs"]["has_armrests"]), (2, False))
        self.assertEqual(sofa["dimension_source"], "title")
        self.assertTrue(sofa["is_product"])


class EstimatedDimensionsTests(unittest.TestCase):
    def test_estimate_is_kept_separately_from_printed_chart(self) -> None:
        out = pa.normalize({"estimated_dimensions_cm": {"width": 120, "depth": 60, "height": 74}})
        self.assertIsNone(out["dimensions"])
        self.assertEqual(out["estimated_dimensions"], {"w_m": 1.2, "d_m": 0.6, "h_m": 0.74})
        self.assertIsNone(pa.normalize({"estimated_dimensions_cm": {"width": None}})["estimated_dimensions"])


if __name__ == "__main__":
    unittest.main()
