"""Gemini 호출을 계측하고, 저장된 응답으로 재생(replay)할 수 있게 한다.

특허의 기술적 효과(API 호출 수, 처리 시간, 토큰)를 주장하려면 수치가
재현 가능해야 한다. 그래서 호출마다 기록을 남기고, 같은 입력이면 저장된
응답을 그대로 돌려주는 모드를 둔다. 재생 모드에서는 API를 한 번도 부르지
않으므로 평가를 몇 번 돌려도 비용이 들지 않는다.

환경변수
  GEMINI_REPLAY_MODE  off(기본) | record | replay
      record : 실제로 호출하고 응답을 저장한다(이미 있으면 덮어쓴다)
      replay : 저장된 응답만 쓴다. 없으면 호출하지 않고 예외를 낸다
               — 실수로 유료 호출이 나가는 걸 막기 위해서다
  GEMINI_REPLAY_DIR   응답 저장 위치 (기본 output/gemini_replay)
  GEMINI_CALL_LOG     호출 기록 JSONL (기본 output/metrics/gemini_calls.jsonl,
                      "off"면 기록하지 않음)

주의: 기록은 generate_content 호출 단위다. SDK의 HttpRetryOptions가 내부에서
다시 보낸 HTTP 요청은 SDK 밖에서 보이지 않으므로 1회로 집계된다.
call_with_retry의 재시도는 generate_content를 다시 부르므로 각각 집계된다.
"""
from __future__ import annotations

import contextlib
import contextvars
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from google.genai import types


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REPLAY_DIR = PROJECT_ROOT / "output" / "gemini_replay"
DEFAULT_CALL_LOG = PROJECT_ROOT / "output" / "metrics" / "gemini_calls.jsonl"

# 평가 스크립트가 "이 호출은 어느 사진·어느 실행의 것"인지 묶을 때 쓴다.
# contextvar라 스레드풀로 넘어간 호출에는 전달되지 않는다. 그 경우 run은 None.
_current_run: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "gemini_run",
    default=None,
)
_log_lock = threading.Lock()


class ReplayMissError(RuntimeError):
    """replay 모드에서 저장된 응답이 없을 때."""


@contextlib.contextmanager
def run_context(name: str) -> Iterator[None]:
    token = _current_run.set(name)
    try:
        yield
    finally:
        _current_run.reset(token)


def _mode() -> str:
    value = os.getenv("GEMINI_REPLAY_MODE", "off").strip().lower()
    return value if value in {"off", "record", "replay"} else "off"


def _replay_dir() -> Path:
    raw = os.getenv("GEMINI_REPLAY_DIR", "").strip()
    return Path(raw) if raw else DEFAULT_REPLAY_DIR


def _log_path() -> Path | None:
    raw = os.getenv("GEMINI_CALL_LOG", "").strip()
    if raw.lower() == "off":
        return None
    return Path(raw) if raw else DEFAULT_CALL_LOG


def _canonical(value: Any) -> Any:
    # 이미지 바이트까지 키에 넣어야 다른 사진에 같은 응답이 재생되지 않는다
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, bytes):
        return {"__bytes__": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(k): _canonical(v) for k, v in sorted(value.items())}
    if hasattr(value, "model_dump"):
        return _canonical(value.model_dump(exclude_none=True))
    if hasattr(value, "tobytes"):  # PIL.Image
        return {"__image__": hashlib.sha256(value.tobytes()).hexdigest()}
    return repr(value)


def request_key(model: str, contents: Any, config: Any) -> str:
    payload = json.dumps(
        {
            "model": model,
            "contents": _canonical(contents),
            "config": _canonical(config),
        },
        ensure_ascii=False,
        sort_keys=True,
        default=repr,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _caller() -> str:
    # 호출부를 바꾸지 않고도 "어느 단계의 호출인지" 남기려고 스택에서 찾는다
    frame = sys._getframe(1)
    while frame is not None:
        name = frame.f_code.co_name
        if frame.f_globals.get("__name__") != __name__ and name not in {
            "call_with_retry",
            "<lambda>",
        }:
            return f"{Path(frame.f_code.co_filename).stem}.{name}"
        frame = frame.f_back
    return "unknown"


def _usage(response: Any) -> dict[str, Any]:
    usage = getattr(response, "usage_metadata", None)
    candidates = getattr(response, "candidates", None) or []
    finish = getattr(candidates[0], "finish_reason", None) if candidates else None
    return {
        "prompt_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "thoughts_tokens": getattr(usage, "thoughts_token_count", None),
        "total_tokens": getattr(usage, "total_token_count", None),
        "finish_reason": str(finish) if finish is not None else None,
    }


def _write_log(record: dict[str, Any]) -> None:
    path = _log_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=False)
        with _log_lock, path.open("a", encoding="utf-8") as file:
            file.write(line + "\n")
    except OSError:
        # 기록 실패가 평면도 생성을 막아서는 안 된다
        pass


_LEVEL_ORDER = ["MINIMAL", "LOW", "MEDIUM", "HIGH"]
# 모델별로 거절당한 thinking_level. 서버를 다시 켤 때까지 기억한다
_rejected_levels: dict[str, set[str]] = {}


def _thinking_level_name(config: Any) -> str | None:
    level = getattr(getattr(config, "thinking_config", None), "thinking_level", None)
    if level is None:
        return None
    return str(getattr(level, "name", level)).split(".")[-1].upper()


def _supported_thinking(model: str, config: Any) -> Any:
    """거절당한 단계면 받는 단계가 나올 때까지 한 단계씩 올린 config."""
    level = _thinking_level_name(config)
    rejected = _rejected_levels.get(model) or set()
    if not level or level not in rejected or level not in _LEVEL_ORDER:
        return config
    for name in _LEVEL_ORDER[_LEVEL_ORDER.index(level) + 1:]:
        if name not in rejected:
            thinking = config.thinking_config.model_copy(update={"thinking_level": types.ThinkingLevel[name]})
            return config.model_copy(update={"thinking_config": thinking})
    return config


class _InstrumentedModels:
    def __init__(self, client: Any) -> None:
        # 원본 Client를 붙들어 둔다. 놓치면 GC가 커넥션을 닫는다(AGENTS.md 7.4)
        self._client = client
        self._models = client.models

    def __getattr__(self, name: str) -> Any:
        return getattr(self._models, name)

    def _call_with_supported_thinking(self, model: str, contents: Any, config: Any, **kwargs: Any) -> Any:
        """추론 단계를 지원하지 않는다는 400이면 한 단계 올려 다시 보낸다.

        같은 Gemini 3 계열이라도 모델마다 받는 thinking_level이 다르다(MINIMAL을 거절하는
        모델이 있다). 호출부마다 모델별 표를 두지 않고, 거절당한 단계를 기억해 다음부터는
        처음부터 받는 단계로 보낸다.
        """
        config = _supported_thinking(model, config)
        for _ in range(len(_LEVEL_ORDER)):
            try:
                return self._models.generate_content(model=model, contents=contents, config=config, **kwargs)
            except Exception as exc:
                text = str(exc).lower()
                level = _thinking_level_name(config)
                if not level or "thinking level" not in text or "not supported" not in text:
                    raise
                _rejected_levels.setdefault(model, set()).add(level)
                upgraded = _supported_thinking(model, config)
                if _thinking_level_name(upgraded) == level:
                    raise
                print(f"[gemini] {model}은 추론 단계 {level}을 받지 않아 {_thinking_level_name(upgraded)}로 다시 보냅니다.")
                config = upgraded
        raise RuntimeError("지원하는 추론 단계를 찾지 못했습니다.")

    def generate_content(self, *, model: str, contents: Any, config: Any = None, **kwargs: Any) -> Any:
        mode = _mode()
        key = request_key(model, contents, config)
        cache_path = _replay_dir() / f"{key}.json"
        record: dict[str, Any] = {
            "ts": round(time.time(), 3),
            "run": _current_run.get(),
            "caller": _caller(),
            "model": model,
            "key": key[:16],
            "mode": mode,
        }
        started = time.perf_counter()

        if mode == "replay":
            if not cache_path.exists():
                record.update(ok=False, replayed=False, error="replay_miss", latency_s=0.0)
                _write_log(record)
                raise ReplayMissError(
                    f"replay 모드인데 저장된 Gemini 응답이 없습니다 ({record['caller']}, {model}). "
                    "GEMINI_REPLAY_MODE=record로 한 번 실행해 응답을 저장하세요."
                )
            stored = json.loads(cache_path.read_text(encoding="utf-8"))
            response = types.GenerateContentResponse.model_validate(stored["response"])
            # 재생 시에도 원래 걸린 시간을 남겨야 처리시간 지표가 의미를 가진다
            record.update(
                ok=True,
                replayed=True,
                latency_s=stored.get("latency_s"),
                **_usage(response),
            )
            _write_log(record)
            return response

        try:
            response = self._call_with_supported_thinking(model, contents, config, **kwargs)
        except Exception as exc:
            record.update(
                ok=False,
                replayed=False,
                latency_s=round(time.perf_counter() - started, 3),
                error=f"{type(exc).__name__}: {str(exc)[:200]}",
            )
            _write_log(record)
            raise

        latency = round(time.perf_counter() - started, 3)
        record.update(ok=True, replayed=False, latency_s=latency, **_usage(response))
        _write_log(record)

        if mode == "record":
            try:
                cache_path.parent.mkdir(parents=True, exist_ok=True)
                cache_path.write_text(
                    json.dumps(
                        {
                            "model": model,
                            "caller": record["caller"],
                            "latency_s": latency,
                            "response": response.model_dump(mode="json", exclude_none=True),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
            except OSError:
                pass
        return response


class InstrumentedClient:
    """genai.Client를 감싸 models.generate_content만 가로챈다."""

    def __init__(self, client: Any) -> None:
        self._client = client
        self.models = _InstrumentedModels(client)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client, name)


def instrument(client: Any) -> Any:
    if isinstance(client, InstrumentedClient):
        return client
    return InstrumentedClient(client)
