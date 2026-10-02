"""SVG 이후 Gemini가 크기·높이·배치를 다시 판단하는 단계. 가짜 응답만 쓴다(외부 호출 없음)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2 import gemini_scene_refine as refine  # noqa: E402


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text
        self.candidates = []


class _Models:
    def __init__(self, answer: dict) -> None:
        self.answer = answer
        self.calls: list = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(contents)
        return _Response(json.dumps(self.answer, ensure_ascii=False))


class _Client:
    def __init__(self, answer: dict) -> None:
        self.models = _Models(answer)


def _graph() -> dict:
    return {
        "room": {"width_m": 4.0, "depth_m": 5.0, "ceiling_m": 2.4},
        "objects": [
            {"id": "table_1", "type": "table", "label": "원형 식탁", "cx": 2.0, "cy": 2.5, "w_m": 1.0, "d_m": 1.0, "h_m": 0.75, "rotation_deg": 0},
            {"id": "chair_1", "type": "chair", "label": "의자", "cx": 3.5, "cy": 4.5, "w_m": 0.5, "d_m": 0.5, "h_m": 0.85, "rotation_deg": 0},
            {"id": "rug_1", "type": "rug", "label": "러그", "cx": 2.0, "cy": 2.5, "w_m": 2.0, "d_m": 1.6, "h_m": 1.4, "rotation_deg": 0},
            {"id": "door_1", "type": "door", "label": "문", "cx": 0.0, "cy": 4.0, "w_m": 0.9, "d_m": 0.08, "h_m": 2.0, "rotation_deg": 270},
            {"id": "product_1", "type": "desk", "label": "책상", "cx": 1.0, "cy": 1.0, "w_m": 1.2, "d_m": 0.6, "h_m": 0.74, "rotation_deg": 0, "source": "selected_product"},
        ],
    }


class RefineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)

    def _run(self, answer: dict):
        graph = _graph()
        client = _Client(answer)
        changes = refine.refine(client, graph, model="m", cache_dir=self.cache)
        return graph, changes, client

    def test_fixes_height_and_moves_chair_toward_table(self) -> None:
        graph, changes, _ = self._run({"objects": [
            {"id": "rug_1", "h_m": 0.015, "reason_ko": "러그는 납작함"},
            {"id": "chair_1", "cx": 2.6, "cy": 3.2, "rotation_deg": 315, "reason_ko": "식탁 옆으로"},
        ]})
        objs = {o["id"]: o for o in graph["objects"]}
        self.assertEqual((objs["rug_1"]["h_m"], objs["rug_1"]["h_source"]), (0.015, "llm"))
        self.assertEqual(objs["chair_1"]["rotation_deg"], 315)
        self.assertEqual({c["id"] for c in changes}, {"rug_1", "chair_1"})
        self.assertIn("llm_refinements", graph)

    def test_move_is_limited(self) -> None:
        graph, _, _ = self._run({"objects": [{"id": "chair_1", "cx": 0.0, "cy": 0.0}]})
        chair = next(o for o in graph["objects"] if o["id"] == "chair_1")
        moved = ((chair["cx"] - 3.5) ** 2 + (chair["cy"] - 4.5) ** 2) ** 0.5
        self.assertAlmostEqual(moved, refine.MAX_MOVE_M, places=2)

    def test_fixed_objects_are_not_changed(self) -> None:
        # 문·창문과 사용자가 추가한 상품은 고치지 않는다
        graph, changes, _ = self._run({"objects": [
            {"id": "door_1", "cx": 2.0}, {"id": "product_1", "w_m": 3.0},
        ]})
        self.assertEqual(changes, [])
        objs = {o["id"]: o for o in graph["objects"]}
        self.assertEqual((objs["door_1"]["cx"], objs["product_1"]["w_m"]), (0.0, 1.2))

    def test_same_input_is_cached(self) -> None:
        answer = {"objects": [{"id": "rug_1", "h_m": 0.02}]}
        _, _, first = self._run(answer)
        _, _, second = self._run(answer)
        self.assertEqual((len(first.models.calls), len(second.models.calls)), (1, 0))

    def test_failure_changes_nothing(self) -> None:
        class Broken:
            class models:
                @staticmethod
                def generate_content(**kwargs):
                    raise RuntimeError("boom")

        graph = _graph()
        self.assertEqual(refine.refine(Broken(), graph, model="m", cache_dir=self.cache), [])
        self.assertEqual(graph["objects"][2]["h_m"], 1.4)

    def test_prompt_has_room_and_names(self) -> None:
        _, _, client = self._run({"objects": []})
        prompt = client.models.calls[0][0]
        self.assertIn("W = 4.0 m, D = 5.0 m", prompt)
        self.assertIn("원형 식탁", prompt)


if __name__ == "__main__":
    unittest.main()
