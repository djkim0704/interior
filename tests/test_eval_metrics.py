from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.genai import types  # noqa: E402

from evaluation import metrics, svg_geometry  # noqa: E402
from model2 import gemini_telemetry  # noqa: E402

ROOM = {"width_m": 4.0, "depth_m": 3.0}


def _obj(object_id, object_type, cx, cy, w, d, rotation=0.0, **extra):
    return {
        "id": object_id,
        "type": object_type,
        "cx": cx,
        "cy": cy,
        "w_m": w,
        "d_m": d,
        "rotation_deg": rotation,
        **extra,
    }


class GeometryTests(unittest.TestCase):
    def test_rotation_swaps_footprint_extents(self) -> None:
        poly = metrics.footprint(_obj("a", "bed", 1.0, 1.0, 2.0, 1.0, rotation=90))
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        self.assertAlmostEqual(max(xs) - min(xs), 1.0, places=6)
        self.assertAlmostEqual(max(ys) - min(ys), 2.0, places=6)

    def test_overlap_area_of_offset_squares(self) -> None:
        a = metrics.footprint(_obj("a", "x", 0.5, 0.5, 1.0, 1.0))
        b = metrics.footprint(_obj("b", "x", 1.0, 1.0, 1.0, 1.0))
        self.assertAlmostEqual(metrics.overlap_area(a, b), 0.25, places=6)


class CollisionTests(unittest.TestCase):
    def test_detects_overlap_and_ignores_rug_and_tucked_chair(self) -> None:
        objects = [
            _obj("bed", "bed", 1.0, 1.0, 2.0, 1.5),
            _obj("desk", "desk", 1.5, 1.2, 1.0, 0.6),
            _obj("rug", "rug", 1.0, 1.0, 3.0, 2.0),
            _obj("chair", "chair", 3.2, 2.0, 0.5, 0.5),
            _obj("desk2", "desk", 3.2, 2.2, 1.0, 0.6),
        ]
        result = metrics.collision_metrics(objects)
        self.assertEqual(result["colliding_pairs"], 1)
        self.assertEqual({result["collisions"][0]["a"], result["collisions"][0]["b"]}, {"bed", "desk"})
        # bed, desk 두 개가 4개 중 충돌
        self.assertEqual(result["object_collision_rate"], 0.5)

    def test_wall_penetration(self) -> None:
        objects = [
            _obj("inside", "bed", 1.0, 1.0, 1.0, 1.0),
            _obj("outside", "shelf", 3.9, 1.0, 0.6, 0.3),
            _obj("door", "door", 0.0, 1.0, 0.9, 0.1, wall_mounted=True),
        ]
        result = metrics.wall_metrics(objects, ROOM)
        self.assertEqual(result["wall_penetrations"], 1)
        self.assertEqual(result["violations"][0]["id"], "outside")


class WalkwayTests(unittest.TestCase):
    def test_empty_room_is_fully_reachable_from_door(self) -> None:
        door = _obj("door", "door", 2.0, 0.0, 0.9, 0.1, wall="top", wall_mounted=True)
        result = metrics.walkway_metrics([door], ROOM)
        self.assertEqual(result["doors_blocked"], 0)
        self.assertGreater(result["reachable_floor_ratio"], 0.6)

    def test_wall_to_wall_cabinet_cuts_room_in_half(self) -> None:
        door = _obj("door", "door", 1.0, 0.0, 0.9, 0.1, wall="top", wall_mounted=True)
        divider = _obj("wall_cabinet", "cabinet", 2.0, 1.5, 0.4, 3.0)
        target = _obj("bed", "bed", 3.4, 1.5, 0.8, 1.6)
        result = metrics.walkway_metrics([door, divider, target], ROOM)
        self.assertIn("bed", result["inaccessible"])
        self.assertLess(result["reachable_floor_ratio"], 0.5)

    def test_furniture_in_front_of_door_blocks_it(self) -> None:
        door = _obj("door", "door", 2.0, 0.0, 0.9, 0.1, wall="top", wall_mounted=True)
        blocker = _obj("shelf", "shelf", 2.0, 0.4, 1.2, 0.6)
        result = metrics.walkway_metrics([door, blocker], ROOM)
        self.assertEqual(result["doors_blocked"], 1)


class SyncTests(unittest.TestCase):
    def test_reports_center_error_and_missing_ids(self) -> None:
        boxes = {"bed_1": (0.0, 0.0, 2.0, 1.0)}
        objects = [_obj("bed_1", "bed", 1.1, 0.5, 2.0, 1.0), _obj("lamp_1", "unknown", 3, 2, 0.3, 0.3)]
        result = metrics.sync_metrics(boxes, objects, ["bed_1", "lamp_1"])
        self.assertAlmostEqual(result["mean_center_error_m"], 0.1, places=4)
        self.assertEqual(result["missing_in_2d"], ["lamp_1"])
        self.assertEqual(result["unknown_type_in_3d"], ["lamp_1"])


class AccuracyTests(unittest.TestCase):
    def test_matches_by_category_and_distance(self) -> None:
        predicted = [
            dict(_obj("p1", "bed", 1.0, 1.0, 2.0, 1.5), category="bed"),
            dict(_obj("p2", "chair", 3.0, 2.0, 0.5, 0.5), category="desk_chair"),
            dict(_obj("p3", "plant", 0.2, 0.2, 0.3, 0.3), category="plant"),
        ]
        truth = {
            "room": {"width_m": 4.2, "depth_m": 3.0},
            "objects": [
                {"category": "bed", "cx": 1.2, "cy": 1.0, "w_m": 2.0, "d_m": 1.4, "rotation_deg": 0},
                {"category": "chair", "cx": 3.1, "cy": 2.0, "w_m": 0.5, "d_m": 0.5, "rotation_deg": 90},
            ],
        }
        result = metrics.accuracy_metrics(predicted, ROOM, truth)
        self.assertEqual(result["matched"], 2)
        self.assertAlmostEqual(result["recall"], 1.0)
        self.assertAlmostEqual(result["precision"], 0.6667, places=3)
        self.assertAlmostEqual(result["mean_center_error_m"], 0.15, places=4)
        self.assertEqual(result["room"]["width_error_m"], 0.2)


class SvgGeometryTests(unittest.TestCase):
    SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1000 1000">
      <defs><pattern id="p"><rect width="5000" height="5000"/></pattern></defs>
      <rect x="0" y="0" width="1000" height="1000" fill="#fff"/>
      <rect x="100" y="100" width="800" height="800" fill="url(#p)"/>
      <g id="bed_1"><g transform="translate(200, 300)">
        <rect x="0" y="0" width="100" height="50"/>
        <path d="M10 10 l50 0 l0 100 z"/>
      </g></g>
      <g transform="scale(2)"><g id="lamp_1"><circle cx="100" cy="100" r="10"/></g></g>
    </svg>"""

    def setUp(self) -> None:
        handle = tempfile.NamedTemporaryFile("w", suffix=".svg", delete=False, encoding="utf-8")
        handle.write(self.SVG)
        handle.close()
        self.path = Path(handle.name)
        self.addCleanup(self.path.unlink)

    def test_group_boxes_follow_nested_transforms(self) -> None:
        root = svg_geometry.load(self.path)
        boxes = svg_geometry.group_boxes(root, ["bed_1", "lamp_1", "missing"])
        self.assertEqual(boxes["bed_1"], (200.0, 300.0, 300.0, 410.0))
        self.assertEqual(boxes["lamp_1"], (180.0, 180.0, 220.0, 220.0))
        self.assertNotIn("missing", boxes)

    def test_floor_box_picks_smallest_large_rect_outside_defs(self) -> None:
        root = svg_geometry.load(self.path)
        self.assertEqual(svg_geometry.floor_box(root, ["bed_1"]), (100.0, 100.0, 900.0, 900.0))


class _FakeModels:
    def __init__(self) -> None:
        self.calls = 0

    def generate_content(self, *, model, contents, config=None):
        self.calls += 1
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text='{"ok": 1}')]))],
            usage_metadata=types.GenerateContentResponseUsageMetadata(
                prompt_token_count=10,
                candidates_token_count=5,
                total_token_count=15,
            ),
        )


class _FakeClient:
    def __init__(self) -> None:
        self.models = _FakeModels()


class TelemetryTests(unittest.TestCase):
    def test_record_then_replay_without_calling_api(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                "GEMINI_REPLAY_DIR": str(Path(tmp) / "replay"),
                "GEMINI_CALL_LOG": str(Path(tmp) / "calls.jsonl"),
            }
            fake = _FakeClient()
            client = gemini_telemetry.instrument(fake)
            with mock.patch.dict(os.environ, {**env, "GEMINI_REPLAY_MODE": "record"}):
                with gemini_telemetry.run_context("t:room"):
                    first = client.models.generate_content(model="m", contents=["hi", b"img"])
            with mock.patch.dict(os.environ, {**env, "GEMINI_REPLAY_MODE": "replay"}):
                second = client.models.generate_content(model="m", contents=["hi", b"img"])
                with self.assertRaises(gemini_telemetry.ReplayMissError):
                    client.models.generate_content(model="m", contents=["hi", b"other"])
            self.assertEqual(fake.models.calls, 1)
            self.assertEqual(first.text, second.text)
            records = [json.loads(line) for line in Path(env["GEMINI_CALL_LOG"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([r["mode"] for r in records], ["record", "replay", "replay"])
            self.assertEqual(records[0]["run"], "t:room")
            self.assertEqual(records[0]["total_tokens"], 15)
            self.assertTrue(records[1]["replayed"])
            self.assertEqual(records[2]["error"], "replay_miss")

    def test_off_mode_passes_through_and_logs(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "calls.jsonl"
            with mock.patch.dict(os.environ, {"GEMINI_REPLAY_MODE": "off", "GEMINI_CALL_LOG": str(log)}):
                fake = _FakeClient()
                gemini_telemetry.instrument(fake).models.generate_content(model="m", contents="x")
            self.assertEqual(fake.models.calls, 1)
            self.assertEqual(len(log.read_text(encoding="utf-8").splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
