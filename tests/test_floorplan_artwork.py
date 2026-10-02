from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from google.genai import types  # noqa: E402

from evaluation import metrics, svg_geometry  # noqa: E402
from model2 import gemini_floorplan_artwork as artwork_mod  # noqa: E402
from model2 import scene_graph  # noqa: E402
from model2.floorplan_3d import build_scene  # noqa: E402
from model2.scene_render_2d import layout_metrics, render_svg  # noqa: E402

GEMINI_REPLY = """```svg
<svg xmlns="http://www.w3.org/2000/svg">
  <defs>
    <linearGradient id="quilt"><stop offset="0" stop-color="#fff"/><stop offset="1" stop-color="#eee"/></linearGradient>
    <pattern id="floor-pattern" width="40" height="40" patternUnits="userSpaceOnUse"><rect width="40" height="40" fill="#e6d3b8"/></pattern>
  </defs>
  <g id="room-style" data-floor-color="#e6d3b8" data-wall-color="#9a8676"/>
  <g id="bed_1" data-w="180" data-h="240">
    <rect x="-6" y="0" width="192" height="24" fill="#b88a62"/>
    <rect x="0" y="20" width="180" height="225" rx="10" fill="url(#quilt)"/>
    <text x="90" y="120">침대</text>
  </g>
  <g id="desk_1" data-w="240" data-h="120"><rect x="10" y="5" width="220" height="110" fill="#c9a77c"/></g>
  <g id="ghost_9"><rect width="5" height="5"/></g>
</svg>
```"""


def _graph() -> dict:
    analysis = {
        "room": {"aspect_ratio_width_to_depth": 0.8},
        "objects": [
            {"id": "bed_1", "category": "bed", "label_ko": "침대", "x": 0.3, "y": 0.35,
             "width": 0.4, "depth": 0.45, "wall_anchors": ["top"], "confidence": 0.9},
            {"id": "desk_1", "category": "desk", "label_ko": "책상", "x": 0.8, "y": 0.8,
             "width": 0.3, "depth": 0.12, "wall_anchors": ["right"], "confidence": 0.8},
            {"id": "door_1", "category": "door", "label_ko": "문", "x": 0.5, "y": 0.98,
             "width": 0.2, "depth": 0.03, "wall_anchors": ["bottom"], "confidence": 0.7},
        ],
    }
    return scene_graph.from_analysis(analysis, width_m=4.0, depth_m=5.0)


class ParseTests(unittest.TestCase):
    def test_parses_objects_defs_and_room_style(self) -> None:
        art = artwork_mod.parse_artwork(GEMINI_REPLY, ["bed_1", "desk_1"])
        self.assertEqual(set(art["objects"]), {"bed_1", "desk_1"})  # 목록에 없는 ghost_9는 버린다
        self.assertEqual(art["floor_pattern_id"], "gx-floor-pattern")
        self.assertEqual(art["room"]["wall_color"], "#9a8676")
        bed = art["objects"]["bed_1"]["markup"]
        # 내부 참조는 접두사가 붙은 id로 바뀌고, 텍스트는 빠진다
        self.assertIn("url(#gx-quilt)", bed)
        self.assertNotIn("<text", bed)
        self.assertNotIn("ns0:", bed)
        self.assertIn('id="gx-quilt"', art["defs"])

    def test_rejects_scripts_and_external_refs(self) -> None:
        bad = '<svg xmlns="http://www.w3.org/2000/svg"><g id="bed_1"><script>alert(1)</script></g></svg>'
        with self.assertRaises(ValueError):
            artwork_mod.parse_artwork(bad, ["bed_1"])

    def test_cache_key_ignores_position_but_not_identity(self) -> None:
        graph = _graph()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as photo:
            photo.write(b"fake-photo")
        self.addCleanup(Path(photo.name).unlink)
        key = artwork_mod.cache_key(Path(photo.name), graph, "m")
        moved = scene_graph.ensure(graph)
        moved["objects"][0]["cx"] += 0.5
        moved["objects"][0]["rotation_deg"] = 90.0
        self.assertEqual(artwork_mod.cache_key(Path(photo.name), moved, "m"), key)
        moved["objects"][0]["color"] = "#ff0000"
        self.assertNotEqual(artwork_mod.cache_key(Path(photo.name), moved, "m"), key)


class RenderWithArtworkTests(unittest.TestCase):
    def test_artwork_is_fitted_to_scene_graph_footprint(self) -> None:
        graph = _graph()
        art = artwork_mod.parse_artwork(GEMINI_REPLY, ["bed_1", "desk_1"])
        svg = render_svg(graph, artwork=art)
        self.assertIn('data-artwork="gemini"', svg)
        self.assertIn("url(#gx-floor-pattern)", svg)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan.svg"
            path.write_text(svg, encoding="utf-8")
            root = svg_geometry.load(path)
        ids = [o["id"] for o in graph["objects"]]
        floor = svg_geometry.floor_box(root, ids)
        px = layout_metrics(graph)["px_per_m"]
        boxes = {
            k: ((x0 - floor[0]) / px, (y0 - floor[1]) / px, (x1 - floor[0]) / px, (y1 - floor[1]) / px)
            for k, (x0, y0, x1, y1) in svg_geometry.group_boxes(root, ids).items()
        }
        result = metrics.sync_metrics(boxes, build_scene(graph)["objects"], ids)
        # Gemini가 상자를 벗어나 그린 침대(헤드보드 x=-6)도 바닥면에 정확히 맞는다
        bed = next(r for r in result["objects"] if r["id"] == "bed_1")
        self.assertGreater(bed["iou"], 0.995)
        # 오른쪽 벽 책상은 90도 회전된 채로 맞는다
        desk = next(r for r in result["objects"] if r["id"] == "desk_1")
        self.assertGreater(desk["iou"], 0.995)

    def test_object_without_artwork_falls_back_to_local_shape(self) -> None:
        graph = _graph()
        art = artwork_mod.parse_artwork(GEMINI_REPLY, ["bed_1"])
        svg = render_svg(graph, artwork=art)
        self.assertEqual(svg.count('data-artwork="gemini"'), 1)


class _FakeModels:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def generate_content(self, **_kwargs):
        self.calls += 1
        return types.GenerateContentResponse(
            candidates=[types.Candidate(content=types.Content(parts=[types.Part(text=self.text)]))]
        )


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.models = _FakeModels(text)


class GenerateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.photo = Path(self.tmp.name) / "room.jpg"
        from PIL import Image

        Image.new("RGB", (64, 48), "#c8b8a0").save(self.photo)

    def test_caches_by_identity(self) -> None:
        client = _FakeClient(GEMINI_REPLY)
        graph = _graph()
        first = artwork_mod.generate_artwork(client, self.photo, graph, model="m", cache_dir=Path(self.tmp.name) / "c")
        moved = scene_graph.ensure(graph)
        moved["objects"][0]["cx"] += 0.3
        second = artwork_mod.generate_artwork(client, self.photo, moved, model="m", cache_dir=Path(self.tmp.name) / "c")
        self.assertEqual(client.models.calls, 1)
        self.assertEqual(first["objects"].keys(), second["objects"].keys())

    def test_failure_returns_empty_and_does_not_retry(self) -> None:
        client = _FakeClient("죄송합니다, 그릴 수 없습니다.")
        graph = _graph()
        cache = Path(self.tmp.name) / "c2"
        self.assertEqual(artwork_mod.generate_artwork(client, self.photo, graph, model="m", cache_dir=cache), {})
        self.assertEqual(artwork_mod.generate_artwork(client, self.photo, graph, model="m", cache_dir=cache), {})
        self.assertEqual(client.models.calls, 1)


class RoomStyleTests(unittest.TestCase):
    def test_floor_pattern_and_colors_reach_3d(self) -> None:
        reply = GEMINI_REPLY.replace(
            '<pattern id="floor-pattern" width="40" height="40" patternUnits="userSpaceOnUse"><rect width="40" height="40" fill="#e6d3b8"/></pattern>',
            '<linearGradient id="grain"><stop offset="0" stop-color="#c9a77c"/></linearGradient>'
            '<pattern id="floor-pattern" width="40" height="40" patternUnits="userSpaceOnUse"><rect width="40" height="40" fill="url(#grain)"/></pattern>',
        )
        art = artwork_mod.parse_artwork(reply, ["bed_1", "desk_1"])
        style = artwork_mod.room_style(art)
        self.assertEqual((style["floor_color"], style["wall_color"]), ("#e6d3b8", "#9a8676"))
        self.assertEqual(style["floor_pattern_id"], "gx-floor-pattern")
        # 무늬가 참조하는 정의(나뭇결 그라데이션)까지 함께 담는다. 쓰지 않는 정의(이불)는 뺀다
        self.assertIn('id="gx-grain"', style["floor_pattern_svg"])
        self.assertNotIn("gx-quilt", style["floor_pattern_svg"])
        graph = _graph()
        graph["room"]["art_style"] = style
        room = build_scene(graph)["room"]
        self.assertEqual((room["floor_color"], room["wall_color"]), ("#e6d3b8", "#9a8676"))
        self.assertEqual(room["floor_pattern_id"], "gx-floor-pattern")

    def test_without_artwork_3d_keeps_analysis_colors(self) -> None:
        room = build_scene(_graph())["room"]
        self.assertIsNone(room["floor_pattern_svg"])
        self.assertTrue(room["floor_color"].startswith("#"))


if __name__ == "__main__":
    unittest.main()
