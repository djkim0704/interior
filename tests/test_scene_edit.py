from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2 import scene_edit, scene_graph  # noqa: E402
from model2.floorplan_3d import build_scene  # noqa: E402


def _graph() -> dict:
    analysis = {
        "room": {"aspect_ratio_width_to_depth": 0.8},
        "objects": [
            {"id": "bed_1", "category": "bed", "label_ko": "침대", "x": 0.25, "y": 0.3,
             "width": 0.4, "depth": 0.45, "wall_anchors": ["top"], "confidence": 0.9},
            {"id": "desk_1", "category": "desk", "label_ko": "책상", "x": 0.8, "y": 0.85,
             "width": 0.3, "depth": 0.12, "wall_anchors": ["bottom"], "confidence": 0.8},
            {"id": "box_1", "category": "storage box", "label_ko": "상자", "x": 0.75, "y": 0.4,
             "width": 0.1, "depth": 0.1, "wall_anchors": [], "confidence": 0.35},
        ],
    }
    return scene_graph.from_analysis(analysis, width_m=4.0, depth_m=5.0)


def _obj(graph: dict, object_id: str) -> dict:
    return next(o for o in graph["objects"] if o["id"] == object_id)


class OpsTests(unittest.TestCase):
    def test_move_marks_user_and_records_ai_value(self) -> None:
        graph = _graph()
        before = _obj(graph, "desk_1")
        edited, changed = scene_edit.apply_ops(graph, [{"op": "move", "id": "desk_1", "cx": 2.0, "cy": 2.5}])
        desk = _obj(edited, "desk_1")
        self.assertEqual(changed, ["desk_1"])
        self.assertEqual((desk["cx"], desk["cy"]), (2.0, 2.5))
        self.assertEqual((desk["source"], desk["confidence"]), ("user", 1.0))
        self.assertEqual(edited["corrections"][0]["ai"]["cx"], before["cx"])
        # 원본은 그대로다
        self.assertEqual(_obj(graph, "desk_1")["cx"], before["cx"])
        # 3D도 같은 값을 받는다
        self.assertEqual(next(o for o in build_scene(edited)["objects"] if o["id"] == "desk_1")["cx"], 2.0)

    def test_moved_furniture_stays_and_others_make_room(self) -> None:
        graph = _graph()
        bed = _obj(graph, "bed_1")
        edited, _ = scene_edit.apply_ops(graph, [{"op": "move", "id": "desk_1", "cx": bed["cx"], "cy": bed["cy"]}])
        self.assertEqual((_obj(edited, "desk_1")["cx"], _obj(edited, "desk_1")["cy"]), (bed["cx"], bed["cy"]))
        self.assertNotEqual((_obj(edited, "bed_1")["cx"], _obj(edited, "bed_1")["cy"]), (bed["cx"], bed["cy"]))

    def test_rotate_resize_retype_confirm_remove(self) -> None:
        graph = _graph()
        edited, _ = scene_edit.apply_ops(
            graph,
            [
                {"op": "rotate", "id": "desk_1", "rotation_deg": 450},
                {"op": "resize", "id": "desk_1", "w_m": 1.4, "d_m": 0.7},
                {"op": "retype", "id": "box_1", "type": "plant"},
                {"op": "confirm", "id": "bed_1"},
            ],
        )
        desk = _obj(edited, "desk_1")
        self.assertEqual(desk["rotation_deg"], 90.0)
        self.assertEqual((desk["w_m"], desk["d_m"]), (1.4, 0.7))
        box = _obj(edited, "box_1")
        self.assertEqual((box["type"], box["label"]), ("plant", "식물"))
        self.assertEqual(_obj(edited, "bed_1")["confidence"], 1.0)
        edited, _ = scene_edit.apply_ops(edited, [{"op": "remove", "id": "box_1"}])
        self.assertNotIn("box_1", [o["id"] for o in edited["objects"]])
        self.assertEqual([o["source_index"] for o in edited["objects"]], list(range(len(edited["objects"]))))

    def test_add_door_on_wall(self) -> None:
        edited, changed = scene_edit.apply_ops(_graph(), [{"op": "add", "type": "door", "wall": "left", "offset": 0.7}])
        door = _obj(edited, changed[0])
        self.assertEqual(door["type"], "door")
        self.assertAlmostEqual(door["cx"], scene_graph.WALL_GAP_M, places=4)
        self.assertAlmostEqual(door["cy"], 0.7 * 5.0, places=2)
        self.assertEqual(door["rotation_deg"], 270.0)
        # 문을 넣으면 동선 상태가 새로 계산된다
        self.assertIn("walkway", edited)

    def test_add_door_on_bottom_and_right_walls_stays_on_that_wall(self) -> None:
        for wall, rotation in (("bottom", 180.0), ("right", 90.0)):
            edited, changed = scene_edit.apply_ops(_graph(), [{"op": "add", "type": "door", "wall": wall, "offset": 0.2}])
            door = _obj(edited, changed[0])
            self.assertEqual((door["wall"], door["rotation_deg"]), (wall, rotation))
            if wall == "bottom":
                self.assertAlmostEqual(door["cy"], 5.0 - scene_graph.WALL_GAP_M, places=4)
            else:
                self.assertAlmostEqual(door["cx"], 4.0 - scene_graph.WALL_GAP_M, places=4)

    def test_bad_requests_raise_readable_errors(self) -> None:
        with self.assertRaises(scene_edit.EditError):
            scene_edit.apply_ops(_graph(), [{"op": "move", "id": "nope", "cx": 1, "cy": 1}])
        with self.assertRaises(scene_edit.EditError):
            scene_edit.apply_ops(_graph(), [{"op": "add", "type": "door"}])
        with self.assertRaises(scene_edit.EditError):
            scene_edit.apply_ops(_graph(), [{"op": "move", "id": "desk_1", "cx": "abc", "cy": 1}])
        with self.assertRaises(scene_edit.EditError):
            scene_edit.apply_ops(_graph(), [])

    def test_uncertain_objects_lists_low_confidence_ai_items(self) -> None:
        rows = scene_edit.uncertain_objects(_graph())
        self.assertEqual([r["id"] for r in rows], ["box_1"])
        edited, _ = scene_edit.apply_ops(_graph(), [{"op": "confirm", "id": "box_1"}])
        self.assertEqual(scene_edit.uncertain_objects(edited), [])


if __name__ == "__main__":
    unittest.main()
