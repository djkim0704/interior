from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.genai import types  # noqa: E402
from PIL import Image  # noqa: E402

from model2 import gemini_reanalyze, scene_graph  # noqa: E402
from model2.topdown_experiment.run import normalize_layout  # noqa: E402


def _graph() -> dict:
    analysis = {
        "room": {"aspect_ratio_width_to_depth": 0.8},
        "objects": [
            {"id": "bed_1", "category": "bed", "label_ko": "침대", "x": 0.25, "y": 0.3,
             "width": 0.4, "depth": 0.45, "wall_anchors": ["top"], "confidence": 0.9,
             "photo_box": [0.1, 0.4, 0.6, 0.9]},
            {"id": "box_1", "category": "box", "label_ko": "상자", "x": 0.75, "y": 0.5,
             "width": 0.1, "depth": 0.1, "confidence": 0.3, "photo_box": [0.7, 0.5, 0.8, 0.7]},
            {"id": "thing_1", "category": "chair", "label_ko": "의자?", "x": 0.5, "y": 0.8,
             "width": 0.12, "depth": 0.12, "confidence": 0.4},
        ],
    }
    return scene_graph.from_analysis(analysis, width_m=4.0, depth_m=5.0)


class _Models:
    def __init__(self, reply: dict) -> None:
        self.reply = reply
        self.requests: list = []

    def generate_content(self, *, model, contents, config=None):
        self.requests.append(contents)
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text=json.dumps(self.reply))]))]
        )


class _Client:
    def __init__(self, reply: dict) -> None:
        self.models = _Models(reply)


class ReanalyzeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photo = Path(self.tmp.name) / "room.jpg"
        Image.new("RGB", (400, 300), "#c8b8a0").save(self.photo)

    def test_only_low_confidence_objects_are_sent_with_crops(self) -> None:
        client = _Client({"objects": []})
        gemini_reanalyze.reanalyze(client, self.photo, _graph(), model="m")
        contents = client.models.requests[0]
        prompt = contents[0]
        self.assertIn("id=box_1", prompt)
        self.assertIn("id=thing_1", prompt)
        self.assertNotIn("id=bed_1", prompt)
        # 프롬프트 1개 + 가구마다 이미지 1장. photo_box가 없는 가구는 사진 전체를 보낸다
        self.assertEqual(len(contents), 3)
        self.assertIn("no crop", prompt.split("id=thing_1")[1].splitlines()[0])

    def test_refinements_update_type_size_and_mark_ai_refined(self) -> None:
        reply = {
            "objects": [
                {"id": "box_1", "exists": True, "category": "plant", "label_ko": "화분",
                 "width": 0.08, "depth": 0.08, "confidence": 0.85, "note_ko": "잎이 보인다"},
                {"id": "thing_1", "exists": False, "confidence": 0.7, "note_ko": "그림자였다"},
                {"id": "bed_1", "exists": True, "category": "sofa", "confidence": 0.99},
            ]
        }
        graph, changed = gemini_reanalyze.reanalyze(_Client(reply), self.photo, _graph(), model="m")
        by_id = {o["id"]: o for o in graph["objects"]}
        self.assertEqual(sorted(changed), ["box_1", "thing_1"])
        self.assertEqual((by_id["box_1"]["type"], by_id["box_1"]["label"], by_id["box_1"]["source"]), ("plant", "화분", "ai_refined"))
        self.assertAlmostEqual(by_id["box_1"]["w_m"], 0.08 * 4.0, places=3)
        # 없다고 해도 지우지 않고 확신만 낮춘다(사람이 확인)
        self.assertLessEqual(by_id["thing_1"]["confidence"], 0.2)
        self.assertEqual(by_id["thing_1"]["refined_note"], "그림자였다")
        # 요청하지 않은 침대는 바뀌지 않는다
        self.assertEqual(by_id["bed_1"]["type"], "bed")
        self.assertEqual(graph["history"][-1]["action"], "reanalyze")
        self.assertEqual(len(graph["refinements"]), 2)

    def test_user_edited_objects_are_never_reanalyzed(self) -> None:
        graph = _graph()
        for obj in graph["objects"]:
            if obj["id"] == "box_1":
                obj["source"] = "user"
        self.assertEqual([o["id"] for o in gemini_reanalyze.targets(graph)], ["thing_1"])

    def test_normalize_layout_keeps_valid_photo_box_only(self) -> None:
        layout = normalize_layout(
            {
                "room": {},
                "objects": [
                    {"id": "a", "category": "bed", "photo_box": [0.6, 0.9, 0.1, 0.4]},
                    {"id": "b", "category": "desk", "photo_box": [0.2, 0.2, 0.2, 0.5]},
                    {"id": "c", "category": "lamp", "photo_box": "nope"},
                ],
            }
        )
        boxes = {o["id"]: o["photo_box"] for o in layout["objects"]}
        self.assertEqual(boxes["a"], [0.1, 0.4, 0.6, 0.9])
        self.assertIsNone(boxes["b"])
        self.assertIsNone(boxes["c"])


if __name__ == "__main__":
    unittest.main()
