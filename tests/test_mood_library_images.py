"""무드 라이브러리 사진 사본이 없으면 images/final 원본으로 대신한다. 외부 호출 없음."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from mood_search_v1 import config  # noqa: E402


class LibraryImageTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        base = Path(tmp.name)
        self.library = base / "mood_library"
        self.images = base / "images" / "final"
        (self.library / "warm_c05").mkdir(parents=True)
        self.images.mkdir(parents=True)
        (self.library / "warm_c05" / "meta.json").write_text(json.dumps({"cover_source": "aa.jpg"}), encoding="utf-8")
        (self.images / "aa.jpg").write_bytes(b"a")
        (self.images / "bb.jpg").write_bytes(b"b")
        for name, value in (("MOOD_LIBRARY_DIR", self.library), ("IMAGE_ROOT", self.images)):
            patcher = mock.patch.object(config, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_falls_back_to_original_photos(self) -> None:
        self.assertEqual(config.resolve_library_image("warm_c05/gallery/bb.jpg"), self.images / "bb.jpg")
        self.assertEqual(config.resolve_library_image("warm_c05/cover.jpg"), self.images / "aa.jpg")

    def test_copy_is_used_when_present(self) -> None:
        copy = self.library / "warm_c05" / "gallery" / "bb.jpg"
        copy.parent.mkdir()
        copy.write_bytes(b"copy")
        self.assertEqual(config.resolve_library_image("warm_c05/gallery/bb.jpg"), copy.resolve())

    def test_missing_or_outside_paths(self) -> None:
        self.assertIsNone(config.resolve_library_image("warm_c05/gallery/zz.jpg"))
        self.assertIsNone(config.resolve_library_image("../secret.txt"))
        self.assertIsNone(config.resolve_library_image(""))


if __name__ == "__main__":
    unittest.main()
