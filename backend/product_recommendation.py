"""Mood-aware, provider-independent furniture recommendation pipeline."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import random
import re
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol

import requests

try:
    from .product_profiles import (
        CATEGORY_ALIASES,
        CATEGORY_CONFIG,
        GLOBAL_BLOCKED_WORDS,
        SEARCH_SIGNATURES,
        STYLE_PROFILES,
        TERM_CATEGORY_ALLOW,
    )
except ImportError:
    from product_profiles import (  # type: ignore[no-redef]
        CATEGORY_ALIASES,
        CATEGORY_CONFIG,
        GLOBAL_BLOCKED_WORDS,
        SEARCH_SIGNATURES,
        STYLE_PROFILES,
        TERM_CATEGORY_ALLOW,
    )


LOGGER = logging.getLogger("furniture-recommendation")
LOGGER.setLevel(logging.INFO)

DISPLAY_PER_QUERY = 30
#SerpApi 1회 응답에서 최대 30개 상품만 후보로 사용
FINAL_RECOMMENDATION_COUNT = 8
# 가구 종류별로 기본 8개를 추천
MAX_SAME_BRAND = 1
# 최종 추천 목록에 동일 브랜드 상품이 최대 1개만 들어가도록 제한
MAX_SAME_MALL = 2
# 동일 판매처의 상품은 최종 목록에 최대 2개까지만

class ProductSearchProvider(Protocol):
    """Search backend used only for collecting product candidates."""

    def search(
        self,
        query: str,
        display: int = DISPLAY_PER_QUERY,
    ) -> list[dict[str, Any]]:
        """Return normalized product dictionaries."""


class ImageSimilarityService(Protocol):
    """Optional image similarity backend."""

    def similarity(
        self,
        reference_image: str | Path,
        product_image_url: str,
    ) -> float | None:
        """Return cosine-like similarity or None when unavailable."""


class ClipImageSimilarityService:
    """Reuse the project's CLIP encoder with URL-keyed disk embeddings."""

    def __init__(self, cache_dir: str | Path) -> None:
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._reference_cache: dict[str, Any] = {}
        self._disabled = False

    def _encode_path(self, path: Path) -> Any:
        from mood_search_v1 import search as mood_search

        model, processor, device = mood_search._get_clip()
        return mood_search.encode_images(
            [path],
            model,
            processor,
            device,
        )[0]

    def similarity(
        self,
        reference_image: str | Path,
        product_image_url: str,
    ) -> float | None:
        """Return CLIP cosine similarity, caching each product embedding."""
        import numpy as np

        if self._disabled:
            return None
        reference_path = Path(reference_image)
        if not reference_path.is_file() or not product_image_url:
            return None
        reference_key = str(reference_path.resolve())
        try:
            reference_embedding = self._reference_cache.get(reference_key)
            if reference_embedding is None:
                reference_embedding = self._encode_path(reference_path)
                self._reference_cache[reference_key] = reference_embedding

            image_key = hashlib.sha256(product_image_url.encode("utf-8")).hexdigest()[:24]
            embedding_path = self.cache_dir / f"{image_key}.npy"
            image_path = self.cache_dir / f"{image_key}.img"
            if embedding_path.exists():
                product_embedding = np.load(embedding_path)
            else:
                if not image_path.exists():
                    with requests.Session() as http:
                        http.trust_env = False
                        response = http.get(product_image_url, timeout=12)
                        response.raise_for_status()
                    image_path.write_bytes(response.content)
                product_embedding = self._encode_path(image_path)
                np.save(embedding_path, product_embedding)
            similarity = float(product_embedding @ reference_embedding)
            # CLIP cosine can be negative; recommendation weights expect 0..1.
            return max(0.0, min(1.0, (similarity + 1.0) / 2.0))
        except Exception as exc:
            self._disabled = True
            LOGGER.warning("clip_disabled_after_failure error=%s", exc)
            return None


class _SerpApiError(ValueError):
    """SerpApi가 돌려준 오류.

    retryable=False면 같은 요청을 다시 보내도 결과가 같다. "결과 없음"이나
    "검색 한도 소진" 같은 확정 응답이 여기 해당하며, 이걸 재시도하면
    질의 하나에 30초 넘게 매달려 상품 페이지 전체가 멈춘다.
    """

    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


def _env_float(name: str, fallback: float) -> float:
    """.env 값을 실수로 읽는다. 비었거나 이상하면 fallback."""
    try:
        value = float(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _env_int(name: str, fallback: int) -> int:
    """.env 값을 정수로 읽는다. 비었거나 이상하면 fallback."""
    try:
        value = int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return fallback
    return value if value >= 0 else fallback


def _redact_key(message: str, api_key: str) -> str:
    """예외 메시지에 URL이 섞여 api_key가 로그로 새는 것을 막는다."""
    return message.replace(api_key, "***") if api_key else message


class SerpApiShoppingProvider:
    """SerpApi Google Shopping 후보를 기존 내부 상품 형식으로 변환한다."""

    endpoint = "https://serpapi.com/search.json"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
        retries: int | None = None,
    ) -> None:
        # Google Shopping 검색은 캐시되지 않은 질의에서 10초를 넘기는 일이
        # 잦다. 상품 1종마다 질의를 여러 번 돌리므로 10초/재시도 0회에서는
        # 일부 질의가 매번 타임아웃으로 버려졌다.
        if timeout is None:
            timeout = _env_float("SERPAPI_TIMEOUT", 30.0)
        if retries is None:
            retries = _env_int("SERPAPI_RETRIES", 2)
        self.api_key = (
            api_key
            or os.getenv("SERPAPI_API", "")
            or os.getenv("SERPAPI_API_KEY", "")
        )
        self.google_domain = os.getenv(
            "SERPAPI_GOOGLE_DOMAIN",
            "google.co.kr",
        ).strip()
        self.country = os.getenv("SERPAPI_GL", "kr").strip()
        self.language = os.getenv("SERPAPI_HL", "ko").strip()
        self.location = os.getenv("SERPAPI_LOCATION", "").strip()
        self.timeout = timeout
        self.retries = max(0, min(int(retries), 2))
        # prefetch()가 채워 두는 질의별 결과. 인스턴스는 요청마다 새로
        # 만들어지므로 요청 하나를 넘어 살아남지 않는다.
        self._prefetched: dict[tuple[str, int], list[dict[str, Any]]] = {}

    def prefetch(
        self,
        queries: list[str],
        *,
        display: int = DISPLAY_PER_QUERY,
    ) -> None:
        """여러 질의를 동시에 미리 받아 둔다.

        SerpApi 한 번은 중앙값 12초짜리 네트워크 대기라, 가구 종류마다
        순차로 부르면 종류 13개에 2분 30초가 넘는다. 결과를 미리 채워
        두면 뒤따르는 search()가 즉시 반환하므로, 추천 로직의 호출 순서와
        누적 상태(shown_product_ids 등)는 그대로 둔 채 대기 시간만 줄인다.

        실패한 질의는 저장하지 않는다. 그 질의는 나중에 search()가 평소처럼
        직접 호출하므로 결과가 달라지지 않는다.
        """
        targets = [
            query
            for query in dict.fromkeys(queries)
            if query and (query, display) not in self._prefetched
        ]
        if not targets:
            return
        workers = max(1, min(_env_int("SERPAPI_MAX_WORKERS", 8), len(targets)))

        def fetch(query: str) -> tuple[str, list[dict[str, Any]] | None]:
            try:
                return query, self._search_uncached(query, display)
            except Exception as exc:
                LOGGER.warning(
                    "prefetch_failed query=%r error=%s", query, exc
                )
                return query, None

        with ThreadPoolExecutor(max_workers=workers) as pool:
            # 결과 저장은 메인 스레드에서만 일어나므로 별도 잠금이 필요 없다.
            for query, found in pool.map(fetch, targets):
                if found is not None:
                    self._prefetched[(query, display)] = found

    def search(
        self,
        query: str,
        display: int = DISPLAY_PER_QUERY,
    ) -> list[dict[str, Any]]:
        """미리 받아둔 결과가 있으면 그걸 쓰고, 없으면 직접 조회한다."""
        found = self._prefetched.pop((query, display), None)
        if found is not None:
            return found
        return self._search_uncached(query, display)

    def _search_uncached(
        self,
        query: str,
        display: int = DISPLAY_PER_QUERY,
    ) -> list[dict[str, Any]]:
        """SerpApi를 검색하고 추천 로직이 사용하는 공통 필드로 정규화한다."""
        if not self.api_key:
            raise ValueError(
                "SERPAPI_API 또는 SERPAPI_API_KEY가 없습니다."
            )
        params = {
            "engine": "google_shopping",
            "q": query,
            "api_key": self.api_key,
            "google_domain": self.google_domain,
            "gl": self.country,
            "hl": self.language,
            "device": "desktop",
        }
        if self.location:
            params["location"] = self.location
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with requests.Session() as http:
                    http.trust_env = False
                    response = http.get(
                        self.endpoint,
                        params=params,
                        timeout=self.timeout,
                    )
                if response.status_code != 200:
                    try:
                        error_payload = response.json()
                        error_message = (
                            str(error_payload.get("error") or "")
                            if isinstance(error_payload, dict)
                            else ""
                        )
                    except ValueError:
                        error_message = ""
                    error_message = error_message or response.text[:300]
                    LOGGER.warning(
                        "provider_error query=%r status=%s body=%s",
                        query,
                        response.status_code,
                        error_message,
                    )
                    raise _SerpApiError(
                        "SerpApi 요청 실패 "
                        f"({response.status_code}): {error_message}",
                        # 429(속도 제한)와 5xx는 잠시 뒤 풀릴 수 있다.
                        # 그 외 4xx는 키나 파라미터 문제라 재시도해도 같다.
                        retryable=(
                            response.status_code == 429
                            or 500 <= response.status_code < 600
                        ),
                    )
                payload = response.json()
                if not isinstance(payload, dict):
                    raise _SerpApiError(
                        "SerpApi 응답 형식이 올바르지 않습니다.",
                        retryable=True,
                    )
                if payload.get("error"):
                    # "Google hasn't returned any results", "run out of
                    # searches", "Invalid API key" 등. 모두 확정 응답이다.
                    raise _SerpApiError(
                        str(payload["error"]),
                        retryable=False,
                    )
                products = [
                    {
                        "productId": str(item.get("product_id") or ""),
                        "title": clean_title(str(item.get("title") or "")),
                        "link": (
                            item.get("link")
                            or item.get("product_link")
                        ),
                        "image": (
                            item.get("thumbnail")
                            or item.get("serpapi_thumbnail")
                        ),
                        "price": _safe_int(item.get("extracted_price")),
                        "shop": item.get("source"),
                        "brand": item.get("brand") or "",
                        "maker": item.get("maker") or "",
                        "category1": " ".join(
                            [
                                str(item.get("snippet") or ""),
                                *[
                                    str(value)
                                    for value in (
                                        item.get("extensions") or []
                                    )
                                ],
                            ]
                        ).strip(),
                        "category2": "",
                        "category3": "",
                        "category4": "",
                    }
                    for item in payload.get("shopping_results", [])
                ]
                return products[: max(1, min(int(display), 100))]
            except (requests.RequestException, ValueError) as exc:
                last_error = (
                    RuntimeError(
                        "SerpApi 네트워크 요청에 실패했습니다 "
                        f"({type(exc).__name__}: "
                        f"{_redact_key(str(exc), self.api_key)})."
                    )
                    if isinstance(exc, requests.RequestException)
                    else exc
                )
                # 네트워크 오류는 항상 일시적으로 본다. SerpApi가 돌려준
                # 오류는 _SerpApiError.retryable이 판단한다.
                retryable = (
                    isinstance(exc, requests.RequestException)
                    or getattr(exc, "retryable", True)
                )
                if not retryable:
                    break
                if attempt < self.retries:
                    time.sleep(0.25 * (attempt + 1))
        assert last_error is not None
        raise last_error


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def normalize_category(category: str) -> str:
    """Map English, Korean and legacy category names to one Korean name."""
    key = re.sub(r"\s+", " ", str(category or "").strip().lower())
    return CATEGORY_ALIASES.get(key, CATEGORY_ALIASES.get(str(category), str(category)))


def clean_title(title: str) -> str:
    """Remove markup/entities and normalize whitespace in a product title."""
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(str(title or "")))
    return re.sub(r"\s+", " ", without_tags).strip()


def get_category_text(item: dict[str, Any]) -> str:
    """공급자가 제공한 카테고리·설명 필드를 검색용 문자열로 합친다."""
    return " ".join(
        str(item.get(f"category{index}") or "").strip()
        for index in range(1, 5)
        if str(item.get(f"category{index}") or "").strip()
    )


MOOD_ALIASES: dict[str, tuple[str, ...]] = {
    "시원하고 밝은": ("시원", "청량", "밝은", "cool airy", "라이트블루", "아쿠아"),
    "따뜻하고 아늑한": ("따뜻", "아늑", "포근", "편안", "warm cozy", "코지"),
    "내추럴 우드": ("내추럴", "원목", "우드", "natural wood"),
    "파스텔": ("파스텔", "pastel", "러블리"),
    "빈티지 레트로": ("빈티지", "레트로", "앤틱", "vintage retro"),
    "럭셔리 모던": ("럭셔리", "호텔", "고급", "luxury modern"),
    "미니멀 화이트": ("미니멀", "화이트", "minimal white"),
    "모던 그레이": ("모던 그레이", "그레이", "modern grey", "modern gray"),
    "모노톤": ("모노톤", "모노크롬", "흑백", "monochrome"),
    "플랜테리어 그린": ("플랜테리어", "보태니컬", "그린", "plant green"),
    "베이지": ("베이지", "beige", "뉴트럴"),
    "블랙": ("블랙", "black", "다크모던"),
}

EXPLICIT_MOOD_TAGS: dict[str, str] = {
    "cool airy": "시원하고 밝은",
    "cool": "시원하고 밝은",
    "warm cozy": "따뜻하고 아늑한",
    "natural wood": "내추럴 우드",
    "cute pastel": "파스텔",
    "pastel": "파스텔",
    "vintage retro": "빈티지 레트로",
    "luxury modern": "럭셔리 모던",
    "minimal white": "미니멀 화이트",
    "modern grey": "모던 그레이",
    "modern gray": "모던 그레이",
    "monochrome": "모노톤",
    "plant green": "플랜테리어 그린",
    "beige": "베이지",
    "black": "블랙",
}

LIBRARY_MOOD_MAP = {
    "cool_airy": "시원하고 밝은",
    "warm_cozy": "따뜻하고 아늑한",
    "natural_wood": "내추럴 우드",
    "cute_pastel": "파스텔",
    "vintage_retro": "빈티지 레트로",
    "luxury_modern": "럭셔리 모던",
    "minimal_white": "미니멀 화이트",
    "modern_grey": "모던 그레이",
    "monochrome_minimal": "모노톤",
    "plant_green": "플랜테리어 그린",
}


def analyze_mood_context(
    prompt: str,
    tags: list[str] | None = None,
    selected_image: str = "",
) -> dict[str, Any]:
    """Build compatible top-3 mood scores and observed attributes."""
    text = " ".join([prompt, *(tags or [])]).lower()
    locked_moods: list[str] = []
    for tag in tags or []:
        mood = EXPLICIT_MOOD_TAGS.get(
            re.sub(r"\s+", " ", str(tag).strip().lower())
        )
        if mood and mood not in locked_moods:
            locked_moods.append(mood)
    # The English labels inserted by the UI are also kept in the textarea.
    # Recover the lock even if an older client omitted the tags array.
    if not locked_moods:
        normalized_prompt = re.sub(
            r"\s+",
            " ",
            str(prompt or "").strip().lower(),
        )
        for phrase, mood in EXPLICIT_MOOD_TAGS.items():
            if re.search(
                rf"(?<![a-z]){re.escape(phrase)}(?![a-z])",
                normalized_prompt,
            ) and mood not in locked_moods:
                locked_moods.append(mood)

    raw_scores = {mood: 0.01 for mood in STYLE_PROFILES}
    for mood, aliases in MOOD_ALIASES.items():
        raw_scores[mood] += sum(1.0 for alias in aliases if alias in text)
    for mood in locked_moods:
        raw_scores[mood] += 20.0

    normalized_path = selected_image.replace("\\", "/").lower()
    image_mood = ""
    for library_key, mood in LIBRARY_MOOD_MAP.items():
        if library_key in normalized_path:
            raw_scores[mood] += (
                2.5
                if not locked_moods or mood in locked_moods
                else 0.15
            )
            image_mood = mood
            break

    ranked = sorted(raw_scores, key=lambda mood: (-raw_scores[mood], mood))[:3]
    total = sum(raw_scores[mood] for mood in ranked)
    mood_scores = {
        mood: round(raw_scores[mood] / total, 6)
        for mood in ranked
    }
    # Correct rounding drift so consumers can rely on an exact sum of one.
    primary = ranked[0]
    mood_scores[primary] += 1.0 - sum(mood_scores.values())

    all_terms = {
        group: list(dict.fromkeys(
            term
            for profile in STYLE_PROFILES.values()
            for term in profile[group]
        ))
        for group in ("colors", "materials", "forms")
    }
    observed = {
        group: [term for term in all_terms[group] if term.lower() in text]
        for group in ("colors", "materials", "forms")
    }
    # The selected library image provides a reliable mood-level prior. We only
    # add its first terms when no explicit text observation exists, and mark
    # the origin for debugging instead of claiming object-level vision.
    profile_mood = (
        image_mood
        if image_mood and (
            not locked_moods
            or image_mood in locked_moods
        )
        else locked_moods[0]
        if locked_moods
        else ""
    )
    if profile_mood:
        image_profile = STYLE_PROFILES[profile_mood]
        for group in ("colors", "materials", "forms"):
            if not observed[group]:
                observed[group] = image_profile[group][:2]
    avoid = merge_style_terms(mood_scores, "avoid")[:6]
    return {
        "primary_mood": primary,
        "mood_scores": mood_scores,
        "colors": observed["colors"],
        "materials": observed["materials"],
        "forms": observed["forms"],
        "details": [],
        "avoid": avoid,
        "furniture_categories": [],
        "_selected_image_mood": image_mood,
        "_locked_moods": locked_moods,
    }


def enrich_mood_analysis_with_gemini(
    base_analysis: dict[str, Any],
    prompt: str,
    selected_image_path: str | Path | None,
    cache_dir: str | Path,
) -> dict[str, Any]:
    """Analyze the one selected mood image once, cache it, and merge with text.

    This is intentionally not used for candidate product images.
    """
    image_path = Path(selected_image_path) if selected_image_path else None
    if image_path is None or not image_path.is_file():
        return base_analysis
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        return base_analysis

    cache_root = Path(cache_dir)
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = hashlib.sha256(
        image_path.read_bytes()
        + prompt.encode("utf-8")
    ).hexdigest()[:24]
    cache_path = cache_root / f"{cache_key}.json"
    try:
        if cache_path.exists():
            vision = json.loads(cache_path.read_text(encoding="utf-8"))
        else:
            from google import genai
            from google.genai import types

            model = os.getenv(
                "GEMINI_FEATURE_MODEL",
                "gemini-2.5-flash-lite",
            ).strip()
            allowed_moods = list(STYLE_PROFILES)
            analysis_prompt = f"""
Analyze this selected interior reference image together with the user's text.
User text: {prompt}
Use only these mood names: {json.dumps(allowed_moods, ensure_ascii=False)}
Return JSON only:
{{
  "primary_mood": "one allowed mood",
  "mood_scores": {{"allowed mood": 0.0}},
  "colors": ["actually visible Korean color terms"],
  "materials": ["clearly visible Korean material terms"],
  "forms": ["clearly visible Korean shape/form terms"],
  "details": ["short clearly visible details"],
  "avoid": ["visually conflicting terms"]
}}
Return exactly the top three moods. Their scores must sum to 1.0. Do not
guess materials or forms that are not visible.
""".strip()
            mime_type = {
                ".png": "image/png",
                ".webp": "image/webp",
                ".jpeg": "image/jpeg",
                ".jpg": "image/jpeg",
            }.get(image_path.suffix.lower(), "image/jpeg")
            client = genai.Client(api_key=api_key)
            response = client.models.generate_content(
                model=model,
                contents=[
                    analysis_prompt,
                    types.Part.from_bytes(
                        data=image_path.read_bytes(),
                        mime_type=mime_type,
                    ),
                ],
                config=types.GenerateContentConfig(
                    temperature=0,
                    response_mime_type="application/json",
                ),
            )
            vision = json.loads(str(response.text or "{}"))
            cache_path.write_text(
                json.dumps(vision, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return _merge_and_validate_mood_analysis(base_analysis, vision)
    except Exception as exc:
        LOGGER.warning("gemini_mood_analysis_failed error=%s", exc)
        return base_analysis


def _merge_and_validate_mood_analysis(
    base: dict[str, Any],
    vision: dict[str, Any],
) -> dict[str, Any]:
    """Validate Gemini JSON and merge image evidence ahead of text priors."""
    if not isinstance(vision, dict):
        return base
    locked_moods = [
        str(mood)
        for mood in base.get("_locked_moods", [])
        if mood in STYLE_PROFILES
    ]
    combined_scores = {
        mood: 0.65 * float(base.get("mood_scores", {}).get(mood, 0.0))
        for mood in STYLE_PROFILES
    }
    vision_scores = vision.get("mood_scores")
    if isinstance(vision_scores, dict):
        positive = {
            mood: max(0.0, float(score))
            for mood, score in vision_scores.items()
            if mood in STYLE_PROFILES and isinstance(score, (int, float))
        }
        vision_total = sum(positive.values())
        if vision_total > 0:
            for mood, score in positive.items():
                if locked_moods and mood not in locked_moods:
                    continue
                combined_scores[mood] += 0.35 * score / vision_total
    if locked_moods:
        # Explicit UI choices define the search family. Image vision may tune
        # their relative weights but cannot replace them with another family.
        for mood in STYLE_PROFILES:
            if mood not in locked_moods:
                combined_scores[mood] = 0.0
        for position, mood in enumerate(locked_moods):
            combined_scores[mood] += 1.0 - min(position, 4) * 0.08
    top_moods = sorted(
        combined_scores,
        key=lambda mood: (-combined_scores[mood], mood),
    )[:3]
    total = sum(combined_scores[mood] for mood in top_moods) or 1.0
    mood_scores = {
        mood: combined_scores[mood] / total
        for mood in top_moods
    }
    mood_scores[top_moods[0]] += 1.0 - sum(mood_scores.values())

    result = dict(base)
    result["primary_mood"] = top_moods[0]
    result["mood_scores"] = mood_scores
    for key in ("colors", "materials", "forms", "details", "avoid"):
        values = vision.get(key)
        vision_terms = (
            [str(value).strip() for value in values if str(value).strip()]
            if isinstance(values, list)
            else []
        )
        base_terms = [
            str(value).strip()
            for value in base.get(key, [])
            if str(value).strip()
        ]
        result[key] = list(dict.fromkeys(vision_terms + base_terms))[:10]
    result["_gemini_image_analysis"] = True
    return result


def merge_style_terms(
    mood_scores: dict[str, float],
    group: str,
) -> list[str]:
    """Merge profile terms by summed mood and list-position weights.

    Summation rewards a term shared by multiple selected moods, which is more
    useful than max-weight because overlap is evidence of a stable preference.
    """
    scores: dict[str, float] = {}
    for mood, mood_weight in mood_scores.items():
        terms = STYLE_PROFILES.get(mood, {}).get(group, [])
        for index, term in enumerate(terms):
            position_weight = 1.0 - 0.35 * index / max(len(terms) - 1, 1)
            scores[term] = scores.get(term, 0.0) + float(mood_weight) * position_weight
    return sorted(scores, key=lambda term: (-scores[term], term))


def _allowed(term: str, category: str) -> bool:
    allowed_categories = TERM_CATEGORY_ALLOW.get(term)
    return allowed_categories is None or category in allowed_categories


def generate_search_queries(
    category: str,
    mood_scores: dict[str, float],
    observed: dict[str, list[str]],
    session_id: str,
    request_round: int = 0,
) -> list[str]:
    """Create one query from the selected mood's search signature.

    Secondary mood profiles are intentionally excluded here.  They still take
    part in product reranking and image similarity, but no longer leak generic
    or conflicting terms into the provider query.
    """
    category = normalize_category(category)
    config = CATEGORY_CONFIG[category]
    noun = config["query_terms"][0]
    primary_mood = max(
        mood_scores,
        key=lambda mood: float(mood_scores[mood]),
        default=next(iter(STYLE_PROFILES)),
    )
    profile = STYLE_PROFILES.get(primary_mood, {})
    signature = SEARCH_SIGNATURES.get(primary_mood, {})

    def mood_terms(group: str) -> list[str]:
        signature_terms = list(signature.get(group, []))
        allowed_observed = [
            term
            for term in observed.get(group, [])
            if term in signature_terms or term in profile.get(group, [])
        ]
        # Signatures lead so a generic observed word cannot make moods converge.
        return list(dict.fromkeys(signature_terms + allowed_observed))

    colors = mood_terms("colors")
    materials = [
        term for term in mood_terms("materials")
        if _allowed(term, category)
    ]
    forms = [
        term for term in mood_terms("forms")
        if _allowed(term, category)
    ]

    seed_text = f"{session_id}|{category}|{request_round}"
    rng = random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16))
    for pool in (colors, materials, forms):
        if len(pool) > 2:
            head, tail = pool[:2], pool[2:]
            rng.shuffle(tail)
            pool[:] = head + tail

    staged: list[str] = []
    if colors and materials:
        staged.extend(
            (
                f"{color} {noun}"
                if color.replace(" ", "") == material.replace(" ", "")
                else f"{color} {material} {noun}"
            )
            for color, material in zip(colors[:2], materials[:2])
        )
    staged.extend(f"{term} {noun}" for term in colors[:2])
    staged.extend(f"{term} {noun}" for term in materials[:2])
    if forms and materials:
        staged.append(f"{forms[0]} {materials[0]} {noun}")
    staged.extend(f"{term} {noun}" for term in forms[:1])

    if not staged:
        staged.append(f"{primary_mood} {noun}")

    unique = list(dict.fromkeys(staged))
    selected_index = max(0, int(request_round)) % len(unique)
    return [unique[selected_index]]


def validate_product(
    item: dict[str, Any],
    category: str,
) -> tuple[bool, str | None]:
    """Apply hard category, image and blocked-word filters."""
    category = normalize_category(category)
    config = CATEGORY_CONFIG[category]
    title = clean_title(str(item.get("title") or ""))
    normalized_title = title.lower().replace(" ", "")
    searchable = f"{title} {get_category_text(item)}".lower().replace(" ", "")
    if not str(item.get("image") or "").strip():
        return False, "missing_product_image"
    if any(word.lower().replace(" ", "") in searchable for word in GLOBAL_BLOCKED_WORDS):
        return False, "blocked_global_word"
    if any(word.lower().replace(" ", "") in searchable for word in config["blocked"]):
        return False, "blocked_category_word"
    if not any(hit.lower().replace(" ", "") in searchable for hit in config["category_hits"]):
        return False, "category_mismatch"
    if category == "침대":
        head_only_cues = (
            "헤드보드",
            "침대헤드",
            "헤드쿠션",
            "저상형헤드",
            "헤드판",
        )
        complete_bed_cues = (
            "침대프레임",
            "수납침대",
            "침대세트",
            "매트리스포함",
            "bedframe",
        )
        if (
            any(cue in normalized_title for cue in head_only_cues)
            and not any(cue in normalized_title for cue in complete_bed_cues)
        ):
            return False, "incomplete_bed_product"
    return True, None


OPTION_PATTERN = re.compile(
    r"\b(?:[123]\s*인용|ss|q|k|퀸|킹|슈퍼싱글)\b|"
    r"\b\d+(?:\.\d+)?\s*(?:cm|mm)\b|"
    r"\([^)]*\)|\[[^\]]*\]",
    re.IGNORECASE,
)
COLOR_OPTION_PATTERN = re.compile(
    r"\b(?:화이트|아이보리|베이지|블랙|그레이|브라운|블루|핑크|민트|"
    r"네이비|월넛|오크|색상선택)\b",
    re.IGNORECASE,
)


def normalize_product_title(title: str) -> str:
    """Normalize option-heavy titles for duplicate detection."""
    value = OPTION_PATTERN.sub(" ", clean_title(title))
    value = COLOR_OPTION_PATTERN.sub(" ", value)
    return re.sub(r"[^0-9a-z가-힣]+", "", value.lower())


def deduplicate_products(
    items: list[dict[str, Any]],
    threshold: float = 0.86,
) -> list[dict[str, Any]]:
    """Remove product-id, normalized-title and fuzzy-title duplicates."""
    result: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    normalized_titles: list[str] = []
    for item in items:
        product_id = str(item.get("productId") or "").strip()
        normalized = normalize_product_title(str(item.get("title") or ""))
        if product_id and product_id in seen_ids:
            continue
        if normalized and (
            normalized in normalized_titles
            or any(
                SequenceMatcher(None, normalized, previous).ratio() >= threshold
                for previous in normalized_titles
            )
        ):
            continue
        if product_id:
            seen_ids.add(product_id)
        if normalized:
            normalized_titles.append(normalized)
        result.append(item)
    return result


def _item_text(item: dict[str, Any]) -> str:
    return " ".join(
        [
            clean_title(str(item.get("title") or "")),
            str(item.get("brand") or ""),
            str(item.get("maker") or ""),
            get_category_text(item),
        ]
    ).lower().replace(" ", "")


def calculate_text_style_score(
    item: dict[str, Any],
    observed: dict[str, list[str]],
    mood_scores: dict[str, float],
) -> float:
    """Calculate an unnormalized text style score with bounded term matches."""
    text = _item_text(item)
    score = 0.0
    observed_colors = set(observed.get("colors", []))
    observed_materials = set(observed.get("materials", []))
    observed_forms = set(observed.get("forms", []))
    for term in observed_colors:
        score += 3.0 if term.lower().replace(" ", "") in text else 0.0
    for term in observed_materials:
        score += 3.0 if term.lower().replace(" ", "") in text else 0.0
    for term in observed_forms:
        score += 2.2 if term.lower().replace(" ", "") in text else 0.0
    weighted_groups = (
        ("colors", 2.5),
        ("materials", 2.5),
        ("forms", 1.8),
        ("styles", 1.2),
        ("rerank", 0.7),
        ("avoid", -3.5),
    )
    for group, weight in weighted_groups:
        for term in set(merge_style_terms(mood_scores, group)):
            if term.lower().replace(" ", "") in text:
                score += weight
    return score


def normalize_scores(values: list[float]) -> list[float]:
    """Min-max normalize safely for empty, singleton and tied inputs."""
    if not values:
        return []
    low, high = min(values), max(values)
    if math.isclose(low, high):
        return [1.0 for _ in values]
    return [(value - low) / (high - low) for value in values]


def _diversity_similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    left_title = normalize_product_title(str(left.get("title") or ""))
    right_title = normalize_product_title(str(right.get("title") or ""))
    title_similarity = SequenceMatcher(None, left_title, right_title).ratio()
    brand_same = bool(left.get("brand") and left.get("brand") == right.get("brand"))
    mall_same = bool(left.get("shop") and left.get("shop") == right.get("shop"))
    return min(1.0, title_similarity + 0.08 * brand_same + 0.05 * mall_same)


def select_diverse_products(
    ranked: list[dict[str, Any]],
    limit: int = FINAL_RECOMMENDATION_COUNT,
) -> list[dict[str, Any]]:
    """MMR-select products and progressively relax brand/mall constraints."""
    if not ranked:
        return []
    selected: list[dict[str, Any]] = []
    remaining = list(ranked)
    stages = [
        (MAX_SAME_BRAND, MAX_SAME_MALL, 0.86),
        (MAX_SAME_BRAND, 3, 0.88),
        (2, 3, 0.91),
        # Exact/fuzzy duplicates were already removed earlier. The last stage
        # therefore lifts the title constraint completely to fill the UI.
        (limit, limit, 1.01),
    ]
    for brand_limit, mall_limit, title_limit in stages:
        while remaining and len(selected) < limit:
            brand_counts = Counter(str(item.get("brand") or "") for item in selected)
            mall_counts = Counter(str(item.get("shop") or "") for item in selected)
            eligible = [
                item for item in remaining
                if (
                    not item.get("brand")
                    or brand_counts[str(item.get("brand"))] < brand_limit
                )
                and (
                    not item.get("shop")
                    or mall_counts[str(item.get("shop"))] < mall_limit
                )
                and all(_diversity_similarity(item, chosen) < title_limit for chosen in selected)
            ]
            if not eligible:
                break
            choice = max(
                eligible,
                key=lambda item: (
                    0.78 * float(item.get("_final_score", 0.0))
                    - 0.22 * max(
                        (_diversity_similarity(item, chosen) for chosen in selected),
                        default=0.0,
                    )
                ),
            )
            selected.append(choice)
            remaining.remove(choice)
        if len(selected) >= limit:
            break
    return selected


def recommend_furniture(
    category: str,
    mood_scores: dict[str, float],
    observed: dict[str, list[str]],
    selected_image: str | Path | None,
    session_id: str,
    request_round: int,
    shown_product_ids: set[str],
    *,
    provider: ProductSearchProvider,
    image_similarity_service: ImageSimilarityService | None = None,
    final_limit: int = FINAL_RECOMMENDATION_COUNT,
) -> tuple[list[dict[str, Any]], set[str], list[str]]:
    """Run candidate collection, filtering, scoring and diverse selection."""
    category = normalize_category(category)
    queries = generate_search_queries(
        category,
        mood_scores,
        observed,
        session_id,
        request_round,
    )
    LOGGER.info("category=%s queries=%s", category, queries)
    candidates: list[dict[str, Any]] = []
    occurrence: Counter[str] = Counter()
    for query_index, query in enumerate(queries):
        try:
            found = provider.search(query, display=DISPLAY_PER_QUERY)
        except Exception as exc:
            LOGGER.warning("query_failed query=%r error=%s", query, exc)
            continue
        LOGGER.info("query=%r response_count=%d", query, len(found))
        for rank, source_item in enumerate(found):
            item = dict(source_item)
            item["matched_query"] = query
            item["_provider_rank"] = rank
            item["_query_index"] = query_index
            identity = str(item.get("productId") or item.get("link") or item.get("title") or "")
            occurrence[identity] += 1
            candidates.append(item)

    LOGGER.info("candidate_count=%d", len(candidates))
    reasons: Counter[str] = Counter()
    valid: list[dict[str, Any]] = []
    for item in candidates:
        is_valid, reason = validate_product(item, category)
        if not is_valid:
            reasons[str(reason)] += 1
            LOGGER.debug(
                "filtered product_id=%r title=%r reason=%s",
                item.get("productId"),
                item.get("title"),
                reason,
            )
            continue
        valid.append(item)
    LOGGER.info("after_filter=%d removed=%s", len(valid), dict(reasons))

    unique = deduplicate_products(valid)
    LOGGER.info("after_dedup=%d", len(unique))
    unseen = [
        item for item in unique
        if str(item.get("productId") or "") not in shown_product_ids
    ]
    if len(unseen) >= 10:
        scoring_pool = unseen
    else:
        scoring_pool = unseen + [
            item for item in unique
            if item not in unseen
        ]
    LOGGER.info("after_history_priority=%d unseen=%d", len(scoring_pool), len(unseen))

    raw_text = [
        calculate_text_style_score(item, observed, mood_scores)
        for item in scoring_pool
    ]
    text_scores = normalize_scores(raw_text)
    image_candidate_indexes = set(
        sorted(
            range(len(scoring_pool)),
            key=lambda index: (
                text_scores[index],
                -int(scoring_pool[index].get("_provider_rank") or 0),
            ),
            reverse=True,
        )[:40]
    )
    image_used = False
    for index, item in enumerate(scoring_pool):
        item["_text_style_score"] = text_scores[index]
        item["_category_score"] = 1.0
        rank = int(item.get("_provider_rank") or 0)
        item["_provider_rank_score"] = max(
            0.0,
            1.0 - rank / max(DISPLAY_PER_QUERY, 1),
        )
        image_score: float | None = None
        if (
            index in image_candidate_indexes
            and image_similarity_service
            and selected_image
            and item.get("image")
        ):
            try:
                image_score = image_similarity_service.similarity(
                    selected_image,
                    str(item["image"]),
                )
            except Exception as exc:
                LOGGER.warning("image_similarity_failed url=%r error=%s", item.get("image"), exc)
        item["_image_similarity"] = image_score
        if image_score is None:
            final = (
                0.60 * item["_text_style_score"]
                + 0.25 * item["_category_score"]
                + 0.15 * item["_provider_rank_score"]
            )
        else:
            image_used = True
            final = (
                0.40 * max(0.0, min(1.0, image_score))
                + 0.30 * item["_text_style_score"]
                + 0.20 * item["_category_score"]
                + 0.10 * item["_provider_rank_score"]
            )
        identity = str(item.get("productId") or item.get("link") or item.get("title") or "")
        item["_final_score"] = min(1.0, final + min(occurrence[identity] - 1, 3) * 0.01)

    scoring_pool.sort(key=lambda item: float(item.get("_final_score", 0.0)), reverse=True)
    selected = select_diverse_products(scoring_pool, final_limit)
    updated_ids = set(shown_product_ids)
    updated_ids.update(
        str(item.get("productId") or "")
        for item in selected
        if str(item.get("productId") or "")
    )
    LOGGER.info(
        "image_similarity=%s final=%s",
        image_used,
        [
            (item.get("title"), round(float(item.get("_final_score", 0.0)), 4))
            for item in selected
        ],
    )
    return selected, updated_ids, queries
