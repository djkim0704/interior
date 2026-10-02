"""2D 가구 그림을 3D로 세우는 자료(art_solid). 외부 호출 없음."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2 import art_solid, scene_render_2d  # noqa: E402
from model2.gemini_floorplan_artwork import parse_artwork  # noqa: E402

SOFA = {"id": "sofa_1", "type": "sofa", "label": "소파", "cx": 1, "cy": 1, "w_m": 2.0, "d_m": 0.9, "rotation_deg": 0}

ARTWORK = {
    "defs": '<linearGradient id="gx-cush"><stop stop-color="#111111"/><stop stop-color="#d9c2a0"/>'
            '<stop stop-color="#222222"/></linearGradient>'
            '<radialGradient id="gx-shadow-grad"><stop stop-color="#000"/></radialGradient>',
    "objects": {
        "sofa_1": {
            "markup": '<ellipse cx="100" cy="50" rx="120" ry="70" fill="url(#gx-shadow-grad)"/>'
                      '<rect x="0" y="0" width="200" height="100" fill="url(#gx-cush)" data-z0="0" data-z1="0.5"/>'
                      '<rect x="300" y="300" width="10" height="10" fill="#333" data-only3d="1"/>',
        }
    },
}


class ObjectSolidTests(unittest.TestCase):
    def test_gradients_become_solid_colors_and_shadows_are_skipped(self) -> None:
        solid = art_solid.object_solid(SOFA, ARTWORK)
        self.assertEqual(solid["source"], "gemini")
        self.assertNotIn("url(#", solid["svg"])
        self.assertIn('fill="#d9c2a0"', solid["svg"])  # 가운데 정지점
        self.assertIn('data-3d="skip"', solid["svg"])  # 그림자 칠
        self.assertIn('data-z1="0.5"', solid["svg"])  # 높이 정보는 그대로

    def test_box_matches_2d_and_ignores_3d_only_parts(self) -> None:
        # 3D 전용 부품(다리 등)은 2D 맞춤 범위에 넣지 않는다. 넣으면 2D와 3D 외곽이 달라진다
        solid = art_solid.object_solid(SOFA, ARTWORK)
        self.assertIn("data-only3d", solid["svg"])
        markup2d = scene_render_2d.strip_only3d(ARTWORK["objects"]["sofa_1"]["markup"])
        self.assertNotIn("data-only3d", markup2d)
        self.assertEqual(solid["box"], [round(v, 3) for v in scene_render_2d._artwork_box(markup2d)])

    def test_malformed_paint_reference(self) -> None:
        art = {"defs": ARTWORK["defs"], "objects": {"sofa_1": {"markup": '<rect width="10" height="10" fill="url(#gx-cush, url(#x))"/>'}}}
        self.assertNotIn("url(", art_solid.object_solid(SOFA, art)["svg"])

    def test_code_shape_when_no_artwork(self) -> None:
        solid = art_solid.object_solid({**SOFA, "id": "product_1", "type": "desk"}, ARTWORK)
        self.assertEqual(solid["source"], "code")
        self.assertTrue(solid["box"][2] > solid["box"][0])

    def test_doors_and_windows_are_not_solids(self) -> None:
        self.assertIsNone(art_solid.object_solid({**SOFA, "type": "door"}, None))

    def test_2d_plan_hides_3d_only_parts(self) -> None:
        graph = {
            "schema": "scene_graph_v1",
            "room": {"width_m": 4, "depth_m": 3},
            "objects": [SOFA],
        }
        svg = scene_render_2d.render_svg(graph, artwork={**ARTWORK, "objects": ARTWORK["objects"]})
        self.assertNotIn("data-only3d", svg)


class ArtworkParseTests(unittest.TestCase):
    def test_height_attributes_survive_parsing(self) -> None:
        text = (
            '<svg xmlns="http://www.w3.org/2000/svg"><defs></defs>'
            '<g id="sofa_1" data-w="200" data-h="90"><rect width="200" height="90" fill="#123456" '
            'data-z0="0" data-z1="0.5" data-soft="0.4"/><rect x="5" y="5" width="8" height="8" data-only3d="1"/></g></svg>'
        )
        art = parse_artwork(text, ["sofa_1"])
        markup = art["objects"]["sofa_1"]["markup"]
        for attr in ('data-z1="0.5"', 'data-soft="0.4"', 'data-only3d="1"'):
            self.assertIn(attr, markup)


if __name__ == "__main__":
    unittest.main()


class RefineTypeTests(unittest.TestCase):
    def test_name_narrows_broad_types(self) -> None:
        from model2.scene_graph import refine_type

        # 좌식 테이블을 식탁 높이로 세우면 상판이 떠 보인다
        self.assertEqual(refine_type("table", "좌식 테이블"), "low_table")
        self.assertEqual(refine_type("table", "식탁"), "table")
        self.assertEqual(refine_type("unknown", "모듈 선반장"), "shelf")
        self.assertEqual(refine_type("table", "서랍장"), "dresser")
        self.assertEqual(refine_type("unknown", "수납장 (캐비닛)"), "cabinet")
        # 이미 구체적인 종류는 이름으로 바꾸지 않는다
        self.assertEqual(refine_type("sofa", "낮은 소파"), "sofa")
