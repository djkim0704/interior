from __future__ import annotations

import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation import metrics, svg_geometry  # noqa: E402
from model2 import scene_graph  # noqa: E402
from model2.floorplan_3d import build_scene  # noqa: E402
from model2.scene_render_2d import layout_metrics, render_svg  # noqa: E402
from model2.web_floorplan import (  # noqa: E402
    _floor_box,
    apply_floorplan_edits_to_layout,
    prepare_floorplan_edit_markup,
)


def _analysis() -> dict:
    # normalize_layout 출력과 같은 형태(0~1 정규화, 회전 0, 평면도 크기)
    return {
        "room": {"aspect_ratio_width_to_depth": 0.8, "floor_color": "#c8a27a"},
        "objects": [
            # 왼쪽 벽에 머리를 붙인 침대: 평면도에서 가로로 길다
            {"id": "bed_1", "category": "bed", "label_ko": "침대", "x": 0.3, "y": 0.4,
             "width": 0.5, "depth": 0.3, "rotation_deg": 0, "wall_anchors": ["left"], "confidence": 0.9},
            {"id": "desk_1", "category": "desk", "label_ko": "책상", "x": 0.7, "y": 0.1,
             "width": 0.3, "depth": 0.12, "rotation_deg": 0, "wall_anchors": ["top"], "confidence": 0.8},
            {"id": "sofa_1", "category": "sofa", "label_ko": "소파", "x": 0.5, "y": 0.85,
             "width": 0.45, "depth": 0.18, "rotation_deg": 0, "wall_anchors": ["bottom"], "confidence": 0.4},
            {"id": "door_1", "category": "door", "label_ko": "문", "x": 0.97, "y": 0.6,
             "width": 0.04, "depth": 0.2, "rotation_deg": 0, "wall_anchors": ["right"], "confidence": 0.7},
            {"id": "lamp_1", "category": "floor_lamp", "label_ko": "조명", "x": 0.9, "y": 0.9,
             "width": 0.06, "depth": 0.06, "rotation_deg": 0, "wall_anchors": [], "confidence": 0.6},
        ],
    }


class FromAnalysisTests(unittest.TestCase):
    def test_wall_facing_furniture_keeps_plan_footprint_and_gets_rotation(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        bed = next(o for o in graph["objects"] if o["id"] == "bed_1")
        # 왼쪽 벽 → 270도. 가구 기준 폭·깊이는 평면도 가로·세로와 뒤바뀐다
        self.assertEqual(bed["rotation_deg"], 270.0)
        self.assertAlmostEqual(bed["w_m"], 0.3 * 5.0, places=3)
        self.assertAlmostEqual(bed["d_m"], 0.5 * 4.0, places=3)
        # 그래도 평면도에서 차지하는 영역은 분석 결과 그대로다
        plan_w, plan_d = scene_graph.plan_extent(bed)
        self.assertAlmostEqual(plan_w, 2.0, places=3)
        self.assertAlmostEqual(plan_d, 1.5, places=3)
        self.assertAlmostEqual(bed["w"], 0.5, places=3)
        self.assertAlmostEqual(bed["h"], 0.3, places=3)

    def test_categories_map_to_renderable_types(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        types = {o["id"]: o["type"] for o in graph["objects"]}
        self.assertEqual(types["sofa_1"], "sofa")
        self.assertEqual(types["lamp_1"], "lamp")
        self.assertEqual(scene_graph.object_type("armchair"), "sofa")
        self.assertEqual(scene_graph.object_type("wooden_bookshelf"), "shelf")

    def test_wall_mounted_objects_snap_to_wall(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        door = next(o for o in graph["objects"] if o["id"] == "door_1")
        self.assertAlmostEqual(door["cx"], 4.0 - scene_graph.WALL_GAP_M, places=4)
        self.assertEqual(door["rotation_deg"], 90.0)


class CalibrationTests(unittest.TestCase):
    def test_both_sides_from_user(self) -> None:
        room = scene_graph.calibrate_room(0.8, [], width_m=3.0, depth_m=4.5)
        self.assertEqual((room["width_m"], room["depth_m"], room["scale_source"]), (3.0, 4.5, "user"))
        self.assertFalse(room["estimated"])

    def test_one_side_uses_analyzed_aspect(self) -> None:
        room = scene_graph.calibrate_room(0.8, [], width_m=3.2)
        self.assertEqual(room["scale_source"], "user_one_side")
        self.assertAlmostEqual(room["depth_m"], 4.0, places=3)
        room = scene_graph.calibrate_room(0.8, [], depth_m=4.0)
        self.assertAlmostEqual(room["width_m"], 3.2, places=3)

    def test_wall_mounted_long_side_runs_along_wall_and_is_thin(self) -> None:
        analysis = _analysis()
        # 분석기가 TV의 긴 변을 벽과 수직으로 준 경우
        analysis["objects"].append(
            {"id": "tv_1", "category": "tv", "x": 0.95, "y": 0.5, "width": 0.2, "depth": 0.06,
             "rotation_deg": 0, "wall_anchors": ["right"], "confidence": 0.8}
        )
        graph = scene_graph.from_analysis(analysis, width_m=4.0, depth_m=5.0)
        tv = next(o for o in graph["objects"] if o["id"] == "tv_1")
        self.assertAlmostEqual(tv["w_m"], 0.8, places=3)
        self.assertLessEqual(tv["d_m"], scene_graph.WALL_MOUNTED_MAX_DEPTH_M)
        self.assertEqual(tv["rotation_deg"], 90.0)
        # 3D 기준으로 바닥면이 방 밖으로 나가지 않는다
        self.assertEqual(metrics.wall_metrics([dict(tv, wall_mounted=False, type="cabinet")], graph["room"])["wall_penetrations"], 0)

    def test_no_measurement_is_an_error(self) -> None:
        # 가구 표준 치수로 축척을 추정하지 않는다. 업로드에서 가로·세로를 필수로 받는다
        with self.assertRaises(ValueError):
            scene_graph.calibrate_room(0.8, [{"type": "bed", "w": 0.5, "h": 0.3}])


class EnsureTests(unittest.TestCase):
    def test_legacy_only_product_gets_metric_fields_from_its_size(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        graph["objects"].append(
            {"type": "shelf", "label": "선반", "x": 0.24, "y": 0.24, "w": 0.0, "h": 0.0, "w_m": 0.8, "d_m": 0.3,
             "wall": "left", "source": "selected_product", "product_marker": 1}
        )
        fixed = scene_graph.ensure(graph)
        product = fixed["objects"][-1]
        self.assertEqual(product["id"], "product_1")
        self.assertEqual((product["w_m"], product["d_m"]), (0.8, 0.3))
        self.assertEqual(product["rotation_deg"], 270.0)
        self.assertGreater(product["w"], 0)

    def test_rescale_room_keeps_relative_positions(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        before = {o["id"]: (o["x"], o["y"], o["w"], o["h"]) for o in graph["objects"]}
        scaled = scene_graph.rescale_room(graph, 3.2, 4.0)
        for obj in scaled["objects"]:
            # 벽걸이 객체는 방 크기와 상관없이 벽에서 일정 거리(4cm)에 붙는다
            if obj["type"] in scene_graph.WALL_MOUNTED_TYPES:
                continue
            for a, b in zip(before[obj["id"]], (obj["x"], obj["y"], obj["w"], obj["h"])):
                self.assertAlmostEqual(a, b, places=3)


class SyncTests(unittest.TestCase):
    """같은 Scene Graph에서 만든 2D와 3D의 좌표 차이가 0이어야 한다 (Phase 1 완료 조건)."""

    def test_2d_svg_and_3d_scene_match_exactly(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        scene3d = build_scene(graph)
        self.assertEqual(scene3d["placement"], "scene_graph")
        ids = [o["id"] for o in graph["objects"]]
        self.assertEqual([o["id"] for o in scene3d["objects"]], ids)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.svg"
            path.write_text(render_svg(graph), encoding="utf-8")
            root = svg_geometry.load(path)
        floor = svg_geometry.floor_box(root, ids)
        m = layout_metrics(graph)
        self.assertEqual(floor, (m["floor_x"], m["floor_y"], m["floor_x"] + m["floor_w"], m["floor_y"] + m["floor_h"]))
        boxes = {
            k: ((x0 - floor[0]) / m["px_per_m"], (y0 - floor[1]) / m["px_per_m"], (x1 - floor[0]) / m["px_per_m"], (y1 - floor[1]) / m["px_per_m"])
            for k, (x0, y0, x1, y1) in svg_geometry.group_boxes(root, ids).items()
        }
        result = metrics.sync_metrics(boxes, scene3d["objects"], ids)
        self.assertEqual(result["missing_in_2d"], [])
        self.assertLess(result["max_center_error_m"], 0.002)
        bed = next(r for r in result["objects"] if r["id"] == "bed_1")
        self.assertGreater(bed["iou"], 0.99)

    def test_floor_box_used_by_editor_matches_renderer(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        m = layout_metrics(graph)
        root = ET.fromstring(render_svg(graph))
        for got, expected in zip(_floor_box(root), (m["floor_x"], m["floor_y"], m["floor_w"], m["floor_h"])):
            self.assertAlmostEqual(got, expected, places=2)

    def test_low_confidence_object_is_marked(self) -> None:
        svg = render_svg(scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0))
        self.assertIn('data-low-confidence="true"', svg)
        self.assertIn("소파 ?", svg)


class EditRoundTripTests(unittest.TestCase):
    def _edited_svg(self, graph: dict, *, dx: float, angle: float, scale: float) -> str:
        markup = prepare_floorplan_edit_markup(render_svg(graph), graph)
        root = ET.fromstring(markup)
        for element in root.iter():
            if element.get("data-scene-id") == "desk_1":
                tx = float(element.get("data-tx")) + dx
                element.set("data-tx", f"{tx:.3f}")
                element.set("data-angle", str(angle))
                element.set("data-scale", str(scale))
        return ET.tostring(root, encoding="unicode")

    def test_edit_moves_rotates_and_scales_in_meters(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        desk = next(o for o in graph["objects"] if o["id"] == "desk_1")
        px = layout_metrics(graph)["px_per_m"]
        edited = apply_floorplan_edits_to_layout(self._edited_svg(graph, dx=0.5 * px, angle=90, scale=1.5), graph)
        moved = next(o for o in edited["objects"] if o["id"] == "desk_1")
        self.assertAlmostEqual(moved["cx"], desk["cx"] + 0.5, places=2)
        self.assertEqual(moved["rotation_deg"], 90.0)
        self.assertAlmostEqual(moved["w_m"], desk["w_m"] * 1.5, places=3)
        self.assertEqual(moved["source"], "user")
        self.assertEqual(edited["history"][-1]["action"], "svg_edit")
        # 3D도 같은 회전을 받는다(기존 경로는 user_rotation을 버렸다)
        scene3d = build_scene(edited)
        self.assertEqual(next(o for o in scene3d["objects"] if o["id"] == "desk_1")["rotation_deg"], 90.0)

    def test_saving_twice_does_not_compound_scale(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        desk_w = next(o for o in graph["objects"] if o["id"] == "desk_1")["w_m"]
        svg = self._edited_svg(graph, dx=0, angle=0, scale=1.5)
        once = apply_floorplan_edits_to_layout(svg, graph)
        twice = apply_floorplan_edits_to_layout(svg, once)
        self.assertAlmostEqual(next(o for o in twice["objects"] if o["id"] == "desk_1")["w_m"], desk_w * 1.5, places=3)

    def test_label_follows_furniture_when_dragged(self) -> None:
        graph = scene_graph.from_analysis(_analysis(), width_m=4.0, depth_m=5.0)
        markup = prepare_floorplan_edit_markup(render_svg(graph), graph)
        self.assertIn('data-label-id="label-desk_1"', markup)


if __name__ == "__main__":
    unittest.main()
