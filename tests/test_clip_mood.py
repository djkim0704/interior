from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from backend import product_recommendation as pr  # noqa: E402
from test_product_recommendation import MOODS, OBSERVED, MockProvider, product  # noqa: E402


class _Service(pr.ClipImageSimilarityService):
    """CLIP 모델과 네트워크 없이 동작을 확인하는 가짜 서비스."""

    def __init__(self, cache_dir, failing_urls=()):
        super().__init__(cache_dir)
        self.failing = set(failing_urls)

    def _encode_text(self, text):
        return np.array([1.0, 0.0])

    def _encode_path(self, path):
        return np.array([1.0, 0.0])

    def _product_embedding(self, url):
        if url in self.failing:
            raise RuntimeError("403 thumbnail")
        # URL 끝 숫자로 유사도를 다르게 만든다
        value = 0.15 + 0.04 * int(url[-1])
        return np.array([value, 0.0])


class ClipServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_one_failed_thumbnail_does_not_disable_clip(self) -> None:
        service = _Service(self.tmp.name, failing_urls={"https://img/1"})
        self.assertIsNone(service.text_similarity("warm sofa", "https://img/1"))
        self.assertIsNotNone(service.text_similarity("warm sofa", "https://img/4"))
        self.assertFalse(service._disabled)

    def test_disables_after_consecutive_failures(self) -> None:
        urls = [f"https://bad/{i}" for i in range(6)]
        service = _Service(self.tmp.name, failing_urls=urls)
        for url in urls:
            service.text_similarity("warm sofa", url)
        self.assertTrue(service._disabled)

    def test_text_similarity_is_spread_to_0_1(self) -> None:
        service = _Service(self.tmp.name)
        low = service.text_similarity("x", "https://img/0")   # 코사인 0.15 → 0
        high = service.text_similarity("x", "https://img/5")  # 코사인 0.35 → 1
        self.assertAlmostEqual(low, 0.0, places=6)
        self.assertAlmostEqual(high, 1.0, places=6)


class MoodTextTests(unittest.TestCase):
    def test_english_mood_sentence(self) -> None:
        text = pr.mood_clip_text("sofa", {"따뜻하고 아늑한": 0.7, "내추럴 우드": 0.3})
        self.assertEqual(text, "a warm cozy and natural wood style sofa, interior furniture product photo")


class RecommendWithMoodTextTests(unittest.TestCase):
    def test_clip_runs_without_selected_mood_image(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            service = _Service(tmp)
            items = [
                product("a", "베이지 패브릭 소파", image="https://img/1"),
                product("b", "베이지 패브릭 소파 라운드", image="https://img/5", shop="다른몰"),
            ]
            with mock.patch.object(service, "text_similarity", wraps=service.text_similarity) as spy:
                selected, _, _ = pr.recommend_furniture(
                    "sofa", MOODS, OBSERVED, None, "s", 0, set(),
                    provider=MockProvider(items),
                    image_similarity_service=service,
                    mood_text="a warm cozy style sofa",
                )
            self.assertTrue(spy.called)
            self.assertIsNotNone(selected[0]["_image_similarity"])
            # 무드 문장과 더 닮은 상품(b)이 앞선다
            self.assertEqual(selected[0]["productId"], "b")


if __name__ == "__main__":
    unittest.main()
