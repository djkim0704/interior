"""가구 하나하나의 입체 형태를 Gemini에게 "부품 목록"으로 받아온다.

three.js 쪽 BUILDERS 는 사람이 손으로 짜 넣은 상자 조합이라 종류가 늘수록
품질 편차가 크다. 그렇다고 완성된 그림을 Gemini 에게 그리게 하면(입체 SVG)
무료 등급 모델이 감당하지 못해 납작한 도형으로 주저앉는다. 그래서 그림 대신
"상자와 원기둥을 이렇게 쌓아라"는 좌표 목록만 받아 오고, 재질·조명·원근은
three.js 가 GPU 로 처리한다. 텍스트 JSON 이라 가벼운 모델로도 생성된다.

좌표 규약은 floorplan_3d.js 의 box()/cylinder() 와 동일하다.
  - 원점은 가구가 놓인 바닥면의 중심
  - y 는 밑면 기준 높이(중심이 아니다)
  - 뒷면이 -z 를 향한다. 벽 방향 회전은 three.js 호출부가 준다
  - 모든 값은 미터, 가구 바깥 치수(w_m/d_m/height_m)를 넘지 않는다
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

from .gemini_retry import call_with_retry
from .gemini_telemetry import instrument


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3.6-flash"
PROMPT_VERSION = "furniture-parts-v1"
FAILURE_COOLDOWN_SECONDS = 10 * 60
MAX_PARTS_PER_ITEM = 14
_GENERATION_LOCK = threading.Lock()

# 형태를 받아올 가치가 있는 종류만 추린다. door/window/rug 처럼 이미 판 한 장이면
# 충분한 것까지 모델에 물으면 토큰만 쓰고 결과는 같다.
SKIP_TYPES = {
    "door",
    "window",
    "rug",
    "mirror",
    "curtain",
    "aircon",
}


def _client() -> genai.Client:
    """프로젝트 API 키로 프록시를 사용하지 않는 Gemini 클라이언트를 만든다."""
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 가 없습니다.")
    return instrument(genai.Client(api_key=api_key))


def _target_types(scene: dict[str, Any]) -> list[str]:
    """씬에 실제로 등장하는 가구 종류만 중복 없이 뽑는다."""
    seen: list[str] = []
    for item in scene.get("objects") or []:
        kind = str(item.get("type") or "").strip()
        if not kind or kind in SKIP_TYPES or kind in seen:
            continue
        seen.append(kind)
    return seen


def _prompt(types_: list[str], style_prompt: str) -> str:
    """부품 좌표 JSON 만 받도록 지시문을 만든다."""
    listing = "\n".join(f"- {name}" for name in types_)
    return f"""
You are describing furniture as simple 3D part lists for a three.js renderer.
Return ONLY a JSON object. No markdown, no prose, no code fences.

For each furniture type below, break the object into stacked primitives that
read clearly as that piece of furniture from across a room.

Coordinate contract (follow exactly):
- Origin is the center of the footprint, ON THE FLOOR.
- "y" is the height of the part's BOTTOM face above the floor, not its center.
- The back of the object faces -z. The front faces +z.
- All numbers are FRACTIONS of the object's own bounding box, from 0 to 1,
  except x/y/z which run from -0.5 to 1.2 (y may exceed 1 for headboards).
- Keep every part inside the bounding box unless the piece genuinely rises
  above it (a headboard, a tall backrest).

Each part is one of:
  {{"shape":"box","w":..,"h":..,"d":..,"x":..,"y":..,"z":..,"tone":..}}
  {{"shape":"cylinder","r":..,"h":..,"x":..,"y":..,"z":..,"tone":..}}

"tone" is a brightness multiplier on the object's own color, 0.5 to 1.2.
Use it to separate cushions from frames, tops from legs. Do not send hex colors.

Rules:
- At most {MAX_PARTS_PER_ITEM} parts per type. Fewer, well-placed parts beat many.
- Include the details that make the silhouette recognizable: legs, backrests,
  armrests, headboards, drawer fronts, shelves, cushions.
- Do not include labels, text, or any field not listed above.

Design preference: {style_prompt[:300] or 'neutral, contemporary'}

Furniture types:
{listing}

Response shape:
{{"<type>": {{"parts": [ ... ]}}, ...}}
""".strip()


def _coerce_part(raw: Any) -> dict[str, Any] | None:
    """모델 응답 한 조각을 검증한다. 범위를 벗어나면 버리지 않고 자른다."""
    if not isinstance(raw, dict):
        return None
    shape = str(raw.get("shape") or "").strip().lower()
    if shape not in {"box", "cylinder"}:
        return None

    def number(key: str, low: float, high: float, default: float = 0.0) -> float:
        try:
            value = float(raw.get(key))
        except (TypeError, ValueError):
            return default
        if value != value:  # NaN 은 three.js 에서 조용히 도형을 없앤다
            return default
        return max(low, min(high, value))

    part: dict[str, Any] = {
        "shape": shape,
        "x": number("x", -0.5, 1.2),
        "y": number("y", -0.5, 1.2),
        "z": number("z", -0.5, 1.2),
        "tone": number("tone", 0.4, 1.3, 1.0),
    }
    if shape == "box":
        part["w"] = number("w", 0.01, 1.2, 0.1)
        part["h"] = number("h", 0.01, 1.2, 0.1)
        part["d"] = number("d", 0.01, 1.2, 0.1)
    else:
        part["r"] = number("r", 0.005, 0.6, 0.05)
        part["h"] = number("h", 0.01, 1.2, 0.1)
    return part


def _coerce(payload: Any, types_: list[str]) -> dict[str, Any]:
    """요청한 종류만 남기고, 부품이 하나도 없는 항목은 버린다.

    빈 항목을 남기면 three.js 가 "설계도가 있다"고 믿고 기존 빌더를 건너뛰어
    가구가 통째로 사라진다. 그래서 여기서 걸러 두는 편이 안전하다.
    """
    if not isinstance(payload, dict):
        return {}
    result: dict[str, Any] = {}
    for kind in types_:
        entry = payload.get(kind)
        if not isinstance(entry, dict):
            continue
        raw_parts = entry.get("parts")
        if not isinstance(raw_parts, list):
            continue
        parts = [
            coerced
            for coerced in (_coerce_part(item) for item in raw_parts[:MAX_PARTS_PER_ITEM])
            if coerced
        ]
        if parts:
            result[kind] = {"parts": parts}
    return result


def _extract_json(text: str) -> Any:
    """코드펜스나 앞뒤 설명이 섞여 와도 첫 JSON 객체만 떼어낸다."""
    stripped = text.strip()
    fence = re.search(r"```(?:json)?\s*(.+?)```", stripped, re.DOTALL)
    if fence:
        stripped = fence.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError("JSON 객체를 찾지 못했습니다.")
    return json.loads(stripped[start : end + 1])


def _cache_key(types_: list[str], style_prompt: str, model: str) -> str:
    """같은 종류 조합이면 같은 설계도를 쓰도록 안정적인 키를 만든다.

    좌표가 가구 치수의 '비율'이라 방 크기나 배치가 달라져도 재사용할 수 있다.
    덕분에 배치를 바꿔가며 여러 번 들어와도 호출이 늘지 않는다.
    """
    payload = {
        "version": PROMPT_VERSION,
        "model": model,
        "types": sorted(types_),
        "style_prompt": style_prompt,
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]


def generate_furniture_parts(
    scene: dict[str, Any],
    cache_dir: str | Path,
    *,
    style_prompt: str = "",
    model: str | None = None,
) -> dict[str, Any]:
    """가구 종류별 부품 설계도를 생성하거나 캐시를 반환한다.

    캐시가 없을 때만 API 를 한 번 호출한다. 실패해도 예외를 올리지 않고 빈
    dict 를 돌려준다. 호출부는 그대로 기존 BUILDERS 로 그리면 되기 때문에,
    설계도는 "있으면 좋은 것"이지 렌더의 전제 조건이 아니다.
    """
    types_ = _target_types(scene)
    if not types_:
        return {}

    model = (
        model
        # 완성된 그림이 아니라 좌표 JSON 만 받으므로 가벼운 모델로 충분하다.
        # 무료 등급 한도를 입체 SVG 와 나눠 쓰는 자리라 기본을 낮게 둔다.
        or os.getenv("GEMINI_FURNITURE_PARTS_MODEL", "").strip()
        or os.getenv("GEMINI_ANALYSIS_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    cache_root = Path(cache_dir).resolve() / "gemini_furniture_parts_v1"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = _cache_key(types_, style_prompt, model)
    output_path = cache_root / f"{cache_key}.json"
    failure_path = cache_root / f"{cache_key}.failed"

    if output_path.is_file():
        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        except Exception:
            # 캐시가 깨졌으면 지우고 새로 받는다. 남겨두면 영영 못 고친다.
            output_path.unlink(missing_ok=True)
    if (
        failure_path.is_file()
        and time.time() - failure_path.stat().st_mtime < FAILURE_COOLDOWN_SECONDS
    ):
        return {}

    with _GENERATION_LOCK:
        if output_path.is_file():
            try:
                return json.loads(output_path.read_text(encoding="utf-8"))
            except Exception:
                output_path.unlink(missing_ok=True)

        try:
            # Client 를 변수로 붙들어 둔다. _client().models... 로 쓰면 Client 가
            # GC 되면서 내부 커넥션이 닫혀 "client has been closed" 로 실패한다.
            client = _client()
            response = call_with_retry(
                client.models.generate_content,
                model=model,
                contents=[_prompt(types_, style_prompt)],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.3,
                    max_output_tokens=8000,
                ),
                description="가구 부품 설계도 생성",
            )
            if not response.text:
                raise RuntimeError("Gemini가 부품 설계도를 반환하지 않았습니다.")
            parts = _coerce(_extract_json(str(response.text)), types_)
            if not parts:
                raise RuntimeError("쓸 수 있는 부품 설계도가 없습니다.")
            temporary_path = output_path.with_suffix(".tmp")
            temporary_path.write_text(
                json.dumps(parts, ensure_ascii=False),
                encoding="utf-8",
            )
            temporary_path.replace(output_path)
            failure_path.unlink(missing_ok=True)
            return parts
        except Exception as exc:
            # 사유까지 적어 둬야 쿨다운 중에도 파일만 보고 원인을 알 수 있다.
            failure_path.write_text(
                f"{int(time.time())}\n{type(exc).__name__}: {exc}",
                encoding="utf-8",
            )
            print(f"[gemini-furniture-parts] 생성 실패: {exc}")
            return {}
