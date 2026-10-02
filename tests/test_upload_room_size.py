"""업로드는 방 가로·세로를 필수로 받는다(축척의 기준). 외부 호출 없음."""
from __future__ import annotations

import io
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for path in (ROOT, ROOT / "backend"):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


class UploadRoomSizeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ.setdefault("PRODUCT_CLIP_ENABLED", "0")
        import app as appmod  # noqa: E402

        cls.client = appmod.app.test_client()

    def _post(self, **fields):
        data = {"photo": (io.BytesIO(b"\xff\xd8\xff"), "room.jpg"), **fields}
        return self.client.post("/upload", data=data, content_type="multipart/form-data")

    def test_missing_width_or_depth_is_rejected(self) -> None:
        for fields in ({}, {"room_width": "3.2"}, {"room_depth": "4.0"}):
            response = self._post(**fields)
            self.assertEqual(response.status_code, 400, fields)
            self.assertIn("가로·세로", response.get_json()["error"])

    def test_floorplan_without_room_size_goes_back_to_upload(self) -> None:
        with self.client.session_transaction() as session:
            session.clear()
            session["uploaded_file"] = "x.jpg"
        response = self.client.get("/floorplan")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/upload", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()


class SavedDesign3DTests(unittest.TestCase):
    """저장 디자인은 3D를 따로 저장하지 않고 평면도 파일 이름으로 배치를 찾아 그린다."""

    def test_layout_is_found_from_floorplan_name(self) -> None:
        import json
        import tempfile
        from types import SimpleNamespace
        from unittest import mock

        os.environ.setdefault("PRODUCT_CLIP_ENABLED", "0")
        import app as appmod  # noqa: E402
        from model2 import scene_graph

        graph = scene_graph.from_analysis(
            {"room": {"aspect_ratio_width_to_depth": 0.8}, "objects": [
                {"id": "bed_1", "category": "bed", "label_ko": "침대", "x": 0.3, "y": 0.4,
                 "width": 0.4, "depth": 0.5, "height_m": 0.45, "confidence": 0.9}]},
            width_m=3.2, depth_m=4.0,
        )
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(appmod, "GENERATED_DIR", tmp):
            Path(tmp, "modified_layout_abc123.json").write_text(json.dumps(graph), encoding="utf-8")
            design = SimpleNamespace(modified_floorplan_file="modified_floorplan_abc123.svg",
                                     original_floorplan_file="upload_x_model2_floorplan.svg")
            scene = appmod.saved_design_scene_3d(design)
            self.assertEqual([o["id"] for o in scene["objects"]], ["bed_1"])
            # 저장할 때 파일명에 넣어 둔 배치 이름으로 찾는다
            saved = SimpleNamespace(modified_floorplan_file="saved_1a2b3c4d5e6f__modified_layout_abc123.svg",
                                    original_floorplan_file="upload_x_model2_floorplan.svg")
            self.assertEqual(len(appmod.saved_design_scene_3d(saved)["objects"]), 1)
            # 예전 이름 규칙으로 저장된 결과 평면도는 배치를 알 수 없다. 원본 배치로 대신 그리지 않는다
            Path(tmp, "upload_x_model2_layout.json").write_text(json.dumps(graph), encoding="utf-8")
            old = SimpleNamespace(modified_floorplan_file="saved_modified_floorplan_ffff.svg",
                                  original_floorplan_file="upload_x_model2_floorplan.svg")
            self.assertIsNone(appmod.saved_design_scene_3d(old))
            # 결과 평면도가 없는 디자인은 원본 배치로 그린다
            original_only = SimpleNamespace(modified_floorplan_file=None,
                                            original_floorplan_file="upload_x_model2_floorplan.svg")
            self.assertIsNotNone(appmod.saved_design_scene_3d(original_only))
            # 배치 파일이 없으면 3D 없이 보여 준다
            missing = SimpleNamespace(modified_floorplan_file="modified_floorplan_zzz.svg", original_floorplan_file=None)
            self.assertIsNone(appmod.saved_design_scene_3d(missing))

    def test_readonly_page_has_no_edit_urls(self) -> None:
        page = (ROOT / "frontend" / "templates" / "result.html").read_text(encoding="utf-8")
        block = page.split('id="floorplan3dBox"', 1)[1].split("></div>", 1)[0]
        self.assertIn("{% if not readonly %}", block)
        self.assertLess(block.index("{% if not readonly %}"), block.index("data-edit-url"))
