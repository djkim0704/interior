"""Gemini 일시적 장애(503/429/5xx)를 지수 백오프로 재시도한다.

Gemini는 서버가 붐빌 때 503 UNAVAILABLE을 돌려준다. 내 쿼터나 API 키 문제가
아니라 구글 쪽 용량 문제라서 몇 초 뒤 같은 요청을 다시 보내면 대개 성공한다.
재시도를 모두 소진했을 때만 GeminiBusyError로 바꿔 올려서, 호출하는 쪽이
"일시적 혼잡"과 진짜 실패를 구분할 수 있게 한다.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any, Callable, TypeVar

from google.genai import errors as genai_errors


T = TypeVar("T")

# 재시도가 의미 있는 상태 코드. 4xx 중에서는 429(쿼터/속도 제한)만 포함한다.
# 400/401/403은 요청이나 키 자체가 잘못된 것이라 다시 보내도 결과가 같다.
RETRYABLE_CODES = frozenset({429, 500, 502, 503, 504})

DEFAULT_ATTEMPTS = 3
# 페이지 요청 하나가 붙들려 있을 수 있는 총 대기 시간(초). 서버가 알려준
# 대기 시간이 길면 재시도가 1분을 넘길 수 있어, 그 전에 포기하고 화면에
# 사유를 보여준다.
DEFAULT_MAX_TOTAL_WAIT = 45.0
# 무료 등급 Flash 계열은 RPM이 5다. 즉 요청 하나당 12초는 띄워야 한다.
# 1~2초 간격으로 재시도하면 503을 피하려다 오히려 분당 한도를 깨서
# 429를 자초한다. 그래서 첫 대기를 12초 위로 잡는다.
DEFAULT_BASE_DELAY = 15.0
MAX_DELAY = 60.0


class GeminiBusyError(RuntimeError):
    """일시적 오류로 재시도를 다 소진했을 때 올라온다.

    503(서버 혼잡)과 429(요청 한도 초과)는 대응이 달라서 문구를 나눈다.
    """

    def __init__(
        self,
        description: str,
        *,
        code: int | None,
        attempts: int,
        original: BaseException,
        daily: bool = False,
    ) -> None:
        if daily:
            reason = (
                "오늘 쓸 수 있는 Gemini 요청을 모두 소진했습니다"
                " (무료 등급 일일 한도). 내일 다시 시도하거나"
                " 다른 모델로 바꿔 주세요."
            )
        elif code == 429:
            reason = (
                "Gemini 요청 한도(분당)를 넘었습니다."
                " 1분쯤 뒤에 다시 시도해 주세요."
            )
        else:
            reason = (
                "Gemini 서버가 혼잡합니다. 잠시 후 다시 시도해 주세요."
            )
        super().__init__(
            f"{description}: {reason} (HTTP {code}, {attempts}회 시도)"
        )
        self.code = code
        self.attempts = attempts
        self.original = original
        self.daily = daily


def _env_int(name: str, fallback: int) -> int:
    try:
        value = int(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return fallback
    return value if value >= 1 else fallback


def _env_float(name: str, fallback: float) -> float:
    try:
        value = float(os.getenv(name, "").strip())
    except (TypeError, ValueError):
        return fallback
    return value if value > 0 else fallback


def _delay_for(attempt: int, base_delay: float) -> float:
    """지수 백오프 + 지터. 동시 요청이 같은 순간에 몰려 재시도하지 않게 한다."""
    delay = min(base_delay * (2 ** (attempt - 1)), MAX_DELAY)
    return delay + random.uniform(0.0, delay * 0.25)


def _violations(exc: BaseException) -> list[dict]:
    """APIError.details에서 QuotaFailure 위반 목록을 꺼낸다."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return []
    items = details.get("error", {}).get("details", [])
    out: list[dict] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        if "QuotaFailure" in str(item.get("@type", "")):
            found = item.get("violations")
            out.extend(v for v in (found or []) if isinstance(v, dict))
    return out


def is_daily_quota_error(exc: BaseException) -> bool:
    """일일 한도(RPD) 초과인지. 이건 재시도해도 자정까지 안 풀린다."""
    for violation in _violations(exc):
        marker = f"{violation.get('quotaId', '')}{violation.get('quotaMetric', '')}"
        if "PerDay" in marker or "per_day" in marker.lower():
            return True
    return False


def server_retry_delay(exc: BaseException) -> float | None:
    """서버가 RetryInfo로 알려준 대기 시간(초). 없으면 None."""
    details = getattr(exc, "details", None)
    if not isinstance(details, dict):
        return None
    for item in details.get("error", {}).get("details", []) or []:
        if not isinstance(item, dict) or "RetryInfo" not in str(item.get("@type", "")):
            continue
        raw = str(item.get("retryDelay") or "").strip()
        try:
            return float(raw[:-1]) if raw.endswith("s") else float(raw)
        except ValueError:
            return None
    return None


def call_with_retry(
    operation: Callable[..., T],
    *args: Any,
    description: str = "Gemini 요청",
    retry_attempts: int | None = None,
    retry_base_delay: float | None = None,
    retry_max_total_wait: float | None = None,
    **kwargs: Any,
) -> T:
    """operation을 호출하고, 일시적 오류면 지수 백오프로 다시 시도한다.

    재시도 횟수와 첫 대기 시간은 .env의 GEMINI_RETRY_ATTEMPTS,
    GEMINI_RETRY_BASE_DELAY로 조절할 수 있다.
    """
    attempts = (
        max(1, int(retry_attempts))
        if retry_attempts is not None
        else _env_int("GEMINI_RETRY_ATTEMPTS", DEFAULT_ATTEMPTS)
    )
    base_delay = (
        max(0.1, float(retry_base_delay))
        if retry_base_delay is not None
        else _env_float("GEMINI_RETRY_BASE_DELAY", DEFAULT_BASE_DELAY)
    )
    max_total_wait = (
        max(0.1, float(retry_max_total_wait))
        if retry_max_total_wait is not None
        else _env_float(
            "GEMINI_RETRY_MAX_TOTAL_WAIT",
            DEFAULT_MAX_TOTAL_WAIT,
        )
    )
    waited = 0.0

    for attempt in range(1, attempts + 1):
        try:
            return operation(*args, **kwargs)
        except genai_errors.APIError as exc:
            code = getattr(exc, "code", None)
            if code not in RETRYABLE_CODES:
                raise
            # 일일 한도는 자정까지 안 풀린다. 기다릴 이유가 없다.
            daily = is_daily_quota_error(exc)
            if daily or attempt >= attempts:
                raise GeminiBusyError(
                    description,
                    code=code,
                    attempts=attempt if daily else attempts,
                    original=exc,
                    daily=daily,
                ) from exc

            # 서버가 알려준 대기 시간이 있으면 그게 가장 정확하다.
            advised = server_retry_delay(exc)
            delay = (
                min(advised + 1.0, MAX_DELAY)
                if advised is not None
                else _delay_for(attempt, base_delay)
            )
            # 남은 예산을 넘기면 더 기다리지 않고 사유를 돌려준다.
            if waited + delay > max_total_wait:
                raise GeminiBusyError(
                    description,
                    code=code,
                    attempts=attempt,
                    original=exc,
                ) from exc
            waited += delay

            print(
                f"[gemini-retry] {description} 실패(HTTP {code}). "
                f"{delay:.1f}초 후 재시도 {attempt + 1}/{attempts}"
            )
            time.sleep(delay)

    # attempts >= 1 이 보장되므로 루프는 반드시 return 하거나 raise 한다.
    raise AssertionError("도달할 수 없는 분기")


def api_status_code(exc: BaseException) -> int | None:
    """Gemini 예외에서 HTTP 상태 코드를 꺼낸다. 아니면 None."""
    if isinstance(exc, GeminiBusyError):
        return exc.code
    if isinstance(exc, genai_errors.APIError):
        return getattr(exc, "code", None)
    return None
