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

from model2 import gemini_furniture_parts as gfp  # noqa: E402


def _scene(sofa_cx: float = 1.0, with_product: bool = False) -> dict:
    objects = [
        {"id": "sofa_1", "type": "sofa", "label": "소파", "cx": sofa_cx, "cy": 1.0, "w_m": 2.0, "d_m": 0.9,
         "height_m": 0.85, "rotation_deg": 0, "color": "#7d6b5d", "attrs": {"seat_count": 3},
         "photo_box": [0.1, 0.5, 0.6, 0.9]},
        {"id": "door_1", "type": "door", "label": "문", "cx": 0.04, "cy": 2.0, "w_m": 0.9, "d_m": 0.08, "height_m": 2.05},
    ]
    if with_product:
        objects.append(
            {"id": "product_1", "type": "chair", "label": "의자", "product_title": "원목 의자", "cx": 2.0, "cy": 2.0,
             "w_m": 0.5, "d_m": 0.5, "height_m": 0.9, "color": "#a0805f", "attrs": {}, "is_product": True,
             "image_file": "abc.jpg"}
        )
    return {"room": {"width_m": 4, "depth_m": 4}, "objects": objects}


class _Models:
    def __init__(self, reply=None, fail=False) -> None:
        self.reply = reply
        self.fail = fail
        self.calls: list = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(contents)
        if self.fail:
            raise RuntimeError("boom")
        ids = [line.split("id=")[1].split(" ")[0] for line in contents[0].splitlines() if "id=" in line]
        reply = self.reply if self.reply is not None else {
            i: {"parts": [
                {"shape": "box", "w": 1, "h": 0.4, "d": 1, "x": 0, "y": 0, "z": 0, "color": "#112233", "material": "fabric"},
                {"shape": "cylinder", "r": 0.05, "h": 0.1, "x": 0.4, "y": 0, "z": 0.4, "material": "plasma"},
            ]}
            for i in ids
        }
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text=json.dumps(reply))]))]
        )


class _Client:
    def __init__(self, **kwargs) -> None:
        self.models = _Models(**kwargs)


class ObjectPartsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.photo = self.cache / "room.jpg"
        Image.new("RGB", (400, 300), "#c8b8a0").save(self.photo)
        (self.cache / "product_images_v1").mkdir()
        Image.new("RGB", (60, 60), "#a0805f").save(self.cache / "product_images_v1" / "abc.jpg")

    def test_generates_per_object_with_images_and_skips_wall_items(self) -> None:
        client = _Client()
        parts = gfp.generate_object_parts(_scene(with_product=True), self.cache, room_photo=self.photo, model="m", client=client)
        self.assertEqual(sorted(parts), ["product_1", "sofa_1"])  # 문은 묻지 않는다
        contents = client.models.calls[0]
        self.assertEqual(len(contents), 3)  # 프롬프트 + 소파(방 사진 일부) + 의자(상품 사진)
        self.assertIn("원목 의자", contents[0])
        part = parts["sofa_1"]["parts"]
        self.assertEqual((part[0]["color"], part[0]["material"]), ("#112233", "fabric"))
        self.assertNotIn("material", part[1])  # 허용하지 않은 재질은 버린다

    def test_cache_ignores_position_and_asks_only_new_objects(self) -> None:
        client = _Client()
        gfp.generate_object_parts(_scene(), self.cache, room_photo=self.photo, model="m", client=client)
        again = gfp.generate_object_parts(_scene(sofa_cx=3.0), self.cache, room_photo=self.photo, model="m", client=client)
        self.assertEqual(len(client.models.calls), 1)
        self.assertIn("sofa_1", again)
        gfp.generate_object_parts(_scene(with_product=True), self.cache, room_photo=self.photo, model="m", client=client)
        self.assertEqual(len(client.models.calls), 2)
        self.assertIn("id=product_1", client.models.calls[1][0])
        self.assertNotIn("id=sofa_1", client.models.calls[1][0])

    def test_empty_parts_are_dropped(self) -> None:
        client = _Client(reply={"sofa_1": {"parts": []}})
        self.assertEqual(gfp.generate_object_parts(_scene(), self.cache, model="m", client=client), {})

    def test_failure_does_not_retry_immediately(self) -> None:
        client = _Client(fail=True)
        self.assertEqual(gfp.generate_object_parts(_scene(), self.cache, model="m", client=client), {})
        self.assertEqual(gfp.generate_object_parts(_scene(), self.cache, model="m", client=client), {})
        self.assertEqual(len(client.models.calls), 1)


if __name__ == "__main__":
    unittest.main()
