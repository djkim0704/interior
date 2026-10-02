from __future__ import annotations

import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.genai import types  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from model2 import gemini_furniture_views as gfv  # noqa: E402


def _render_png(fill: str = "#8a5a3c") -> bytes:
    # 흰 배경 가운데 가구(갈색 사각형), 그 안에 흰 쿠션(흰 사각형)이 있는 그림
    image = Image.new("RGB", (200, 160), "#ffffff")
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 30, 160, 130), fill=fill)
    draw.rectangle((70, 50, 130, 90), fill="#ffffff")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _scene(count: int = 1) -> dict:
    objects = [
        {"id": f"sofa_{i}", "type": "sofa", "label": "소파", "cx": 1.0 + i, "cy": 1.0, "w_m": 2.0, "d_m": 0.9,
         "height_m": 0.85, "color": "#8a5a3c", "attrs": {}, "photo_box": [0.1, 0.5, 0.6, 0.9]}
        for i in range(count)
    ]
    objects.append({"id": "door_1", "type": "door", "w_m": 0.9, "d_m": 0.08, "height_m": 2.0, "wall_mounted": True})
    return {"room": {"width_m": 6, "depth_m": 4}, "objects": objects}


class _Models:
    def __init__(self, fail_on: int | None = None) -> None:
        self.calls: list = []
        self.fail_on = fail_on

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(contents)
        if self.fail_on is not None and len(self.calls) == self.fail_on:
            raise RuntimeError("image boom")
        part = types.Part(inline_data=types.Blob(data=_render_png(), mime_type="image/png"))
        return types.GenerateContentResponse(candidates=[types.Candidate(content=types.Content(parts=[part]))])


class _Client:
    def __init__(self, **kwargs) -> None:
        self.models = _Models(**kwargs)


class CutOutTests(unittest.TestCase):
    def test_outer_white_becomes_transparent_inner_white_stays(self) -> None:
        png = gfv.cut_out_white(_render_png())
        with Image.open(io.BytesIO(png)) as image:
            self.assertEqual(image.size, (121, 101))  # 가구 부분만 잘렸다
            self.assertEqual(image.getpixel((0, 0))[3], 255)  # 가구 모서리는 불투명
            self.assertEqual(image.getpixel((60, 40))[:3], (255, 255, 255))  # 흰 쿠션은 남는다
            self.assertEqual(image.getpixel((60, 40))[3], 255)


class GenerateViewsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name)
        self.photo = self.cache / "room.jpg"
        Image.new("RGB", (400, 300), "#c8b8a0").save(self.photo)

    def test_four_views_with_front_as_reference_for_turns(self) -> None:
        client = _Client()
        out = gfv.generate_object_views(_scene(), self.cache, room_photo=self.photo, client=client, model="img")
        views = out["views"]["sofa_1" if "sofa_1" in out["views"] else "sofa_0"]
        self.assertEqual(sorted(views["views"]), ["back", "front", "left", "right"])
        self.assertTrue(views["views"]["front"].startswith("/static/generated/gemini_furniture_views_v1/"))
        self.assertEqual(len(client.models.calls), 4)  # 문은 그리지 않는다
        self.assertIn("FRONT", client.models.calls[0][0])
        # 옆·뒤 그림은 앞 그림(png)을 함께 보내 같은 가구로 그리게 한다
        self.assertIn("RIGHT side", client.models.calls[1][0])
        self.assertEqual(client.models.calls[1][1].inline_data.mime_type, "image/png")
        self.assertEqual(out["remaining"], 0)

    def test_cache_ignores_position(self) -> None:
        client = _Client()
        gfv.generate_object_views(_scene(), self.cache, room_photo=self.photo, client=client, model="img")
        moved = _scene()
        moved["objects"][0]["cx"] = 3.3
        out = gfv.generate_object_views(moved, self.cache, room_photo=self.photo, client=client, model="img")
        self.assertEqual(len(client.models.calls), 4)
        self.assertEqual(len(out["views"]), 1)

    def test_draws_at_most_max_new_objects_and_reports_remaining(self) -> None:
        client = _Client()
        out = gfv.generate_object_views(_scene(3), self.cache, room_photo=self.photo, client=client, model="img", max_new=2)
        self.assertEqual((len(out["views"]), out["remaining"]), (2, 1))
        out = gfv.generate_object_views(_scene(3), self.cache, room_photo=self.photo, client=client, model="img", max_new=2)
        self.assertEqual((len(out["views"]), out["remaining"]), (3, 0))

    def test_one_failure_does_not_stop_others(self) -> None:
        client = _Client(fail_on=2)  # 첫 가구의 두 번째 그림에서 실패
        out = gfv.generate_object_views(_scene(2), self.cache, room_photo=self.photo, client=client, model="img")
        self.assertEqual(list(out["views"]), ["sofa_1"])

    def test_single_view_mode(self) -> None:
        import os
        from unittest import mock

        client = _Client()
        with mock.patch.dict(os.environ, {"FURNITURE_VIEW_COUNT": "1"}):
            out = gfv.generate_object_views(_scene(), self.cache, room_photo=self.photo, client=client, model="img")
        self.assertEqual(list(out["views"]["sofa_0"]["views"]), ["front"])
        self.assertEqual(len(client.models.calls), 1)


    def test_floorplan_svg_is_the_primary_reference(self) -> None:
        artwork = {
            "defs": '<linearGradient id="gx-a"/>',
            "objects": {"sofa_0": {"markup": '<rect width="200" height="90" fill="#123456"/><rect width="50" height="20" fill="url(#gx-a)"/>', "w": 200, "h": 90}},
        }
        client = _Client()
        gfv.generate_object_views(_scene(), self.cache, room_photo=self.photo, client=client, model="img", artwork=artwork)
        front_prompt = client.models.calls[0][0]
        self.assertIn("```svg", front_prompt)
        self.assertIn("#123456", front_prompt)
        self.assertIn("gx-a", front_prompt)
        self.assertIn("RIGHT side", client.models.calls[1][0])
        self.assertIn("#123456", client.models.calls[1][0])  # 옆·뒤 그림에도 같은 기준

    def test_whole_room_photo_when_no_photo_box_and_cache_is_per_room(self) -> None:
        scene = _scene()
        scene["objects"][0].pop("photo_box")
        client = _Client()
        gfv.generate_object_views(scene, self.cache, room_photo=self.photo, client=client, model="img")
        self.assertIn("whole real room", client.models.calls[0][0])
        self.assertEqual(len(client.models.calls[0]), 2)  # 프롬프트 + 방 사진
        # 다른 방 사진이면 같은 이름·색의 가구라도 새로 그린다(옛 캐시의 재사용 문제)
        other = self.cache / "other_room.jpg"
        Image.new("RGB", (400, 300), "#405060").save(other)
        gfv.generate_object_views(scene, self.cache, room_photo=other, client=client, model="img")
        self.assertEqual(len(client.models.calls), 8)


    def test_long_shared_defs_never_cut_the_item_drawing(self) -> None:
        filler = "".join(f'<linearGradient id="gx-unused{i}"><stop offset="0" stop-color="#000"/></linearGradient>' for i in range(400))
        artwork = {
            "defs": filler + '<linearGradient id="gx-used"><stop offset="0" stop-color="#abcdef"/></linearGradient>',
            "objects": {"sofa_0": {"markup": '<rect width="200" height="90" fill="url(#gx-used)"/>', "w": 200, "h": 90}},
        }
        svg = gfv.object_svg(artwork, "sofa_0")
        self.assertTrue(svg.endswith('fill="url(#gx-used)" /></svg>') or svg.endswith('fill="url(#gx-used)"/></svg>'))
        self.assertIn('id="gx-used"', svg)
        self.assertNotIn("gx-unused", svg)
        self.assertLessEqual(len(svg), gfv.SVG_REFERENCE_MAX_CHARS)

    def test_product_without_image_file_does_not_use_room_photo(self) -> None:
        scene = _scene()
        scene["objects"][0].pop("photo_box")
        scene["objects"][0].update(is_product=True, image_file="missing.jpg")
        client = _Client()
        gfv.generate_object_views(scene, self.cache, room_photo=self.photo, client=client, model="img")
        self.assertEqual(len(client.models.calls[0]), 1)  # 프롬프트만, 방 사진 없음
        self.assertNotIn("whole real room", client.models.calls[0][0])


    def test_nested_def_references_are_followed(self) -> None:
        artwork = {
            "defs": '<linearGradient id="gx-wood"><stop offset="0" stop-color="#8a5a3c"/></linearGradient>'
                    '<pattern id="gx-quilt" width="10" height="10"><rect width="10" height="10" fill="url(#gx-wood)"/></pattern>'
                    '<linearGradient id="gx-other"/>',
            "objects": {"bed_0": {"markup": '<rect width="200" height="90" fill="url(#gx-quilt)"/>', "w": 200, "h": 90}},
        }
        svg = gfv.object_svg(artwork, "bed_0")
        self.assertIn('id="gx-quilt"', svg)
        self.assertIn('id="gx-wood"', svg)  # 무늬가 쓰는 그라데이션까지 따라간다
        self.assertNotIn("gx-other", svg)


if __name__ == "__main__":
    unittest.main()
