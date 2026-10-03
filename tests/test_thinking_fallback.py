"""모델이 거절하는 추론 단계(thinking_level)는 한 단계 올려 다시 보낸다. 외부 호출 없음."""
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

from google.genai import types  # noqa: E402

from model2 import gemini_telemetry  # noqa: E402
from model2.gemini_svg_experiment import minimal_thinking  # noqa: E402


class _Models:
    def __init__(self, rejected: set[str]) -> None:
        self.rejected = rejected
        self.levels: list[str] = []

    def generate_content(self, *, model, contents, config=None):
        level = gemini_telemetry._thinking_level_name(config)
        self.levels.append(level)
        if level in self.rejected:
            raise RuntimeError(f"400 INVALID_ARGUMENT. Thinking level {level} is not supported for this model.")
        part = types.Part(text="ok")
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[part]))])


class _Client:
    def __init__(self, rejected: set[str]) -> None:
        self.models = _Models(rejected)


class ThinkingFallbackTests(unittest.TestCase):
    def setUp(self) -> None:
        gemini_telemetry._rejected_levels.clear()
        self.addCleanup(gemini_telemetry._rejected_levels.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {"GEMINI_REPLAY_MODE": "off", "GEMINI_CALL_LOG": "off"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_minimal_is_retried_as_low_and_remembered(self) -> None:
        raw = _Client({"MINIMAL"})
        client = gemini_telemetry.instrument(raw)
        config = types.GenerateContentConfig(thinking_config=minimal_thinking("gemini-3.8-flash"))
        self.assertEqual(client.models.generate_content(model="gemini-3.8-flash", contents=["x"], config=config).text, "ok")
        self.assertEqual(raw.models.levels, ["MINIMAL", "LOW"])
        # 다음 호출은 처음부터 LOW로 보낸다(헛호출 없음)
        client.models.generate_content(model="gemini-3.8-flash", contents=["y"], config=config)
        self.assertEqual(raw.models.levels, ["MINIMAL", "LOW", "LOW"])

    def test_other_errors_are_not_retried(self) -> None:
        class Broken:
            class models:
                calls = 0

                @classmethod
                def generate_content(cls, **kwargs):
                    cls.calls += 1
                    raise RuntimeError("429 RESOURCE_EXHAUSTED")

        client = gemini_telemetry.instrument(Broken())
        config = types.GenerateContentConfig(thinking_config=minimal_thinking("gemini-3.8-flash"))
        with self.assertRaises(RuntimeError):
            client.models.generate_content(model="gemini-3.8-flash", contents=["x"], config=config)
        self.assertEqual(Broken.models.calls, 1)


if __name__ == "__main__":
    unittest.main()
