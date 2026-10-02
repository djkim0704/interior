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
