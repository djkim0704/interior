from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from PIL import Image  # noqa: E402

from model2 import gemini_floorplan_artwork as artwork_mod  # noqa: E402
from model2 import scene_graph, web_floorplan  # noqa: E402
from model2.floorplan_3d import build_scene  # noqa: E402
from test_floorplan_artwork import GEMINI_REPLY, _FakeClient as _Client, _graph  # noqa: E402

SCENE = {
    "room": {"aspect_ratio_width_to_depth": 0.8},
    "objects": [{"id": "bed_1", "category": "bed", "x": 0.3, "y": 0.3, "width": 0.4, "depth": 0.45, "wall_anchors": ["top"], "confidence": 0.9}],
}


class ArtworkFailureTests(unittest.TestCase):
    def test_failure_marker_expires(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "room.jpg"
            Image.new("RGB", (64, 48), "#c8b8a0").save(photo)
            cache = Path(tmp) / "c"
            graph = _graph()
            client = _Client("실패합니다")
            self.assertEqual(artwork_mod.generate_artwork(client, photo, graph, model="m", cache_dir=cache), {})
            self.assertEqual(artwork_mod.generate_artwork(client, photo, graph, model="m", cache_dir=cache), {})
            self.assertEqual(client.models.calls, 1)  # 방금 실패했으면 다시 부르지 않는다
            failed = next(cache.glob("*.failed"))
            old = time.time() - artwork_mod.FAILURE_COOLDOWN_SECONDS - 5
            os.utime(failed, (old, old))
            client.models.text = GEMINI_REPLY
            self.assertTrue(artwork_mod.generate_artwork(client, photo, graph, model="m", cache_dir=cache))
            self.assertEqual(client.models.calls, 2)  # 기한이 지나면 다시 그린다


class AnalysisCacheTests(unittest.TestCase):
    def test_reanalyzes_when_model_or_settings_change(self) -> None:
        calls = []

        def fake_analyze(client, image_path, model):
            calls.append(model)
            return json.loads(json.dumps(SCENE))

        with tempfile.TemporaryDirectory() as tmp:
            photo = Path(tmp) / "upload_x.jpg"
            Image.new("RGB", (64, 48), "#c8b8a0").save(photo)
            env = {"FLOORPLAN_2D_ARTWORK": "local", "GEMINI_API_KEY": "test", "GEMINI_LAYOUT_THINKING": "low"}
            with mock.patch.dict(os.environ, env), \
                    mock.patch.object(web_floorplan, "analyze_room", fake_analyze), \
                    mock.patch.object(web_floorplan, "_client", lambda: object()):
                web_floorplan.generate_floorplan_for_web(photo, tmp, analysis_model="m1", room_width=4.0, room_depth=5.0)
                web_floorplan.generate_floorplan_for_web(photo, tmp, analysis_model="m1", room_width=4.0, room_depth=5.0)
                self.assertEqual(calls, ["m1"])  # 같은 설정이면 재사용
                web_floorplan.generate_floorplan_for_web(photo, tmp, analysis_model="m2", room_width=4.0, room_depth=5.0)
                self.assertEqual(calls, ["m1", "m2"])  # 모델이 바뀌면 다시 분석
                with mock.patch.dict(os.environ, {"GEMINI_LAYOUT_THINKING": "high"}):
                    web_floorplan.generate_floorplan_for_web(photo, tmp, analysis_model="m2", room_width=4.0, room_depth=5.0)
                self.assertEqual(calls, ["m1", "m2", "m2"])  # thinking 설정이 바뀌어도 다시 분석


class ArtStyleBackfillTests(unittest.TestCase):
    def test_old_layout_without_art_style_gets_floor_from_artwork(self) -> None:
        generated = ROOT / "frontend" / "static" / "generated"
        name = "test_backfill_artwork_tmp.json"
        art = artwork_mod.parse_artwork(GEMINI_REPLY, ["bed_1", "desk_1"])
        (generated / name).write_text(json.dumps(art), encoding="utf-8")
        self.addCleanup((generated / name).unlink)
        graph = scene_graph.from_analysis(SCENE, width_m=4.0, depth_m=5.0)
        graph["artwork_file"] = name  # 예전 편집본처럼 art_style 없이 그림 파일만 가리킨다
        room = build_scene(graph)["room"]
        self.assertEqual(room["floor_pattern_id"], "gx-floor-pattern")
        self.assertEqual(room["wall_color"], "#9a8676")


if __name__ == "__main__":
    unittest.main()
