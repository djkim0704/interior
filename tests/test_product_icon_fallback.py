""".env에 적은 아이콘 모델이 없어져도(404) 종류별 기본 모델로 다시 시도한다. 외부 호출 없음."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2 import product_icon_svg  # noqa: E402

ICON = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200"><g id="icon"><rect width="10" height="10" fill="#000"/></g></svg>'


class _Response:
    def __init__(self, text: str) -> None:
        self.text = text


class _Models:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(model)
        if model == "gemini-2.5-flash":
            raise RuntimeError("404 NOT_FOUND. This model is no longer available to new users.")
        return _Response(ICON)


class _Client:
    def __init__(self) -> None:
        self.models = _Models()


class IconFallbackTests(unittest.TestCase):
    def test_retired_override_falls_back_to_default_model(self) -> None:
        client = _Client()
        svg = product_icon_svg.generate_product_icon_svg(
            client, b"img", "image/jpeg", title="의자", category="chair", model="gemini-2.5-flash"
        )
        self.assertEqual(client.models.calls[0], "gemini-2.5-flash")
        self.assertEqual(client.models.calls[1], product_icon_svg.resolve_icon_model("chair", None))
        self.assertIn("rect", svg)


if __name__ == "__main__":
    unittest.main()
