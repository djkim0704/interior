from __future__ import annotations

import io
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
from PIL import Image  # noqa: E402

from model2.topdown_experiment import run  # noqa: E402


class _Models:
    def __init__(self) -> None:
        self.request = None

    def generate_content(self, *, model, contents, config=None):
        self.request = {"model": model, "contents": contents, "config": config}
        reply = {"room": {"aspect_ratio_width_to_depth": 0.8}, "objects": [
            {"id": "door_1", "category": "door", "x": 0.9, "y": 0.5, "width": 0.2, "depth": 0.03, "wall_anchors": ["right"]},
        ]}
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text=json.dumps(reply))]))]
        )


class _Client:
    def __init__(self) -> None:
        self.models = _Models()


class LayoutAnalysisRequestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photo = Path(self.tmp.name) / "room.jpg"
        Image.new("RGB", (4000, 3000), "#c8b8a0").save(self.photo)

    def _sent_image_size(self, request) -> tuple[int, int]:
        part = request["contents"][1]
        with Image.open(io.BytesIO(part.inline_data.data)) as image:
            return image.size

    def test_defaults_bigger_photo_low_thinking_and_door_rules(self) -> None:
        client = _Client()
        with mock.patch.dict(os.environ, {"GEMINI_LAYOUT_IMAGE_MAX": "", "GEMINI_LAYOUT_THINKING": ""}):
            layout = run.analyze_room(client, self.photo, "gemini-3.8-flash")
        request = client.models.request
        self.assertEqual(self._sent_image_size(request), (2400, 1800))
        self.assertEqual(request["config"].thinking_config.thinking_level, types.ThinkingLevel.LOW)
        self.assertIn("List EVERY door and window", request["contents"][0])
        self.assertIn("about 0.9 m wide", request["contents"][0])
        self.assertEqual(layout["objects"][0]["category"], "door")

    def test_settings_from_env(self) -> None:
        client = _Client()
        with mock.patch.dict(os.environ, {"GEMINI_LAYOUT_IMAGE_MAX": "1600", "GEMINI_LAYOUT_THINKING": "off"}):
            run.analyze_room(client, self.photo, "gemini-2.5-flash")
        request = client.models.request
        self.assertEqual(self._sent_image_size(request), (1600, 1200))
        self.assertEqual(request["config"].thinking_config.thinking_budget, 0)

    def test_pro_model_gets_no_thinking_override_when_off(self) -> None:
        client = _Client()
        with mock.patch.dict(os.environ, {"GEMINI_LAYOUT_THINKING": "off"}):
            run.analyze_room(client, self.photo, "gemini-3.1-pro")
        self.assertIsNone(client.models.request["config"].thinking_config)


if __name__ == "__main__":
    unittest.main()
