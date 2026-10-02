"""가구 하나하나의 입체 형태를 Gemini에게 "부품 목록"으로 받아온다.

three.js 쪽 예전 BUILDERS(지금은 삭제)는 사람이 손으로 짜 넣은 상자 조합이라 종류가 늘수록
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
    dict 를 돌려준다. 호출부는 그대로 2D SVG 돌출로 그리면 되기 때문에,
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


# ──────────────────────────────────────────────────────────────
# 가구별 형태 (three.js는 배치만, 모양은 멀티모달 모델이 만든다)
# ──────────────────────────────────────────────────────────────
# 위의 타입 단위 설계도는 '소파' 하나에 모양 하나라 방마다·상품마다 차이가 없다.
# 여기서는 가구 하나하나를 그 가구의 사진(상품 사진, 또는 방 사진에서 잘라 낸 부분)과
# 함께 보내 고유한 부품 목록을 받는다. 캐시는 가구 정체성(종류·이름·색·속성·크기 비율·
# 사진) 단위라 위치·회전이 바뀌어도 다시 부르지 않고, 새로 들어온 가구만 묻는다.
#
# 품질 설정(.env)
#   OBJECT_PARTS_MAX          가구당 최대 부품 수 (기본 40)
#   OBJECT_PARTS_BATCH        한 번에 묻는 가구 수 (기본 1 = 가구마다 따로, 품질 우선)
#   GEMINI_OBJECT_PARTS_THINKING  off|low|medium|high (기본 medium). 구조를 먼저 설계하게 한다
#   OBJECT_PARTS_IMAGE_MAX    가구 사진 긴 변 픽셀 (기본 1280)
OBJECT_PROMPT_VERSION = "object-parts-v2"
OBJECT_PARTS_DIR = "gemini_object_parts_v1"
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
PART_MATERIALS = {"wood", "fabric", "leather", "metal", "glass", "plastic", "rattan", "marble"}
OBJECT_SHAPES = {"box", "rounded_box", "cylinder", "sphere"}


def _env_int(name: str, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(os.getenv(name, "").strip() or default)))
    except ValueError:
        return default


def max_parts_per_object() -> int:
    return _env_int("OBJECT_PARTS_MAX", 40, 8, 80)


def thinking_for(model: str, level: str | None = None) -> types.ThinkingConfig:
    """thinking 수준을 모델 세대에 맞는 설정으로 바꾼다.

    Gemini 3 계열은 thinking_level만 받고, 2.5 이하는 thinking_budget(토큰 수)을 받는다.
    """
    level = (level or "medium").strip().lower()
    if level == "off":
        from .gemini_svg_experiment import minimal_thinking

        return minimal_thinking(model)
    if str(model).lower().startswith("gemini-3"):
        mapping = {"low": types.ThinkingLevel.LOW, "medium": types.ThinkingLevel.MEDIUM, "high": types.ThinkingLevel.HIGH}
        return types.ThinkingConfig(thinking_level=mapping.get(level, types.ThinkingLevel.MEDIUM))
    return types.ThinkingConfig(thinking_budget={"low": 1024, "medium": 4096, "high": 8192}.get(level, 4096))


OBJECT_PROMPT = """
You are an expert 3D furniture modeler. For each object below, build a faithful 3D
model of THAT specific item from primitives for a three.js renderer. Each numbered
image (when given) shows the real item. Before writing parts, study the image:
identify the main components (frame, seat, cushions, back, arms, legs, top, drawers,
doors, handles, headboard, mattress, shade, pot, leaves), their relative sizes, how
they connect, and their colors and materials. Then model each component.

Coordinate contract (follow exactly):
- Origin is the center of the footprint, ON THE FLOOR.
- "y" is the height of the part's BOTTOM face above the floor (before rotation).
- The back of the object faces -z. The front faces +z.
- All sizes and positions are FRACTIONS of the object's own bounding box
  (W, D, H given per object). x/z run from -0.5 to 0.5, y from 0 to 1.
  Only tall headboards or backrests may rise to y+h = 1.3.

Part shapes:
  {"shape":"box","w":..,"h":..,"d":..,"x":..,"y":..,"z":..}
  {"shape":"rounded_box","w":..,"h":..,"d":..,"radius":0.3,"x":..,"y":..,"z":..}
      radius = fraction of the part's smallest side (0..0.5). Use for cushions,
      mattresses, upholstered arms and backs, soft edges.
  {"shape":"cylinder","r":..,"r2":..,"h":..,"x":..,"y":..,"z":..}
      r = top radius, r2 = bottom radius (optional; tapered legs, lamp shades, pots),
      both fractions of the shorter footprint side.
  {"shape":"sphere","w":..,"h":..,"d":..,"x":..,"y":..,"z":..}
      ellipsoid inside that box (bulbs, round cushions, leaf clusters).
Every part may also have:
  "rx","ry","rz": rotation in degrees about the part's own center
      (tilt backrests 5-15 degrees with rx, splay legs, angle armrests),
  "color": "#rrggbb" sampled from the image,
  "material": one of wood, fabric, leather, metal, glass, plastic, rattan, marble.

Rules:
- At most MAX_PARTS parts per object. Spend them on recognizable details:
  separate seat and back cushions, visible legs, drawer fronts and handles,
  frame rails, buttons or seams only if clearly visible.
- Every part must touch the object; no floating pieces. Legs reach the floor (y=0).
- Keep the overall silhouette inside the bounding box.
- Do not include labels, text, or any field not listed above.

Design preference: STYLE

Objects:
LISTING

Response shape (JSON only):
{"<id>": {"parts": [ ... ]}, ...}
""".strip()


def _object_prompt(items: list[dict[str, Any]], style_prompt: str) -> str:
    listing = "\n".join(
        f'- image {item["image_index"] or "-"} | id={item["id"]} | {item["type"]} "{item["label"]}" | '
        f'size W{item["w"]:.2f} x D{item["d"]:.2f} x H{item["h"]:.2f} m | color {item["color"] or "-"} | '
        f'features {json.dumps(item["attrs"], ensure_ascii=False)}'
        for item in items
    )
    return (
        OBJECT_PROMPT.replace("MAX_PARTS", str(max_parts_per_object()))
        .replace("STYLE", style_prompt[:300] or "match the photos")
        .replace("LISTING", listing)
    )


def _coerce_object_part(raw: Any) -> dict[str, Any] | None:
    """가구별 부품 하나를 검증한다. 범위를 벗어난 값은 버리지 않고 자른다."""
    if not isinstance(raw, dict):
        return None
    shape = str(raw.get("shape") or "").strip().lower()
    if shape not in OBJECT_SHAPES:
        return None

    def number(key: str, low: float, high: float, default: float = 0.0) -> float:
        try:
            value = float(raw.get(key))
        except (TypeError, ValueError):
            return default
        if value != value:  # NaN은 three.js에서 도형을 조용히 없앤다
            return default
        return max(low, min(high, value))

    part: dict[str, Any] = {
        "shape": shape,
        "x": number("x", -0.6, 0.6),
        "y": number("y", 0.0, 1.3),
        "z": number("z", -0.6, 0.6),
    }
    if shape == "cylinder":
        part["r"] = number("r", 0.004, 0.6, 0.05)
        if raw.get("r2") is not None:
            part["r2"] = number("r2", 0.0, 0.6, part["r"])
        part["h"] = number("h", 0.005, 1.3, 0.1)
    else:
        part["w"] = number("w", 0.005, 1.2, 0.1)
        part["h"] = number("h", 0.005, 1.3, 0.1)
        part["d"] = number("d", 0.005, 1.2, 0.1)
        if shape == "rounded_box":
            part["radius"] = number("radius", 0.0, 0.5, 0.2)
    for axis, limit in (("rx", 90.0), ("ry", 180.0), ("rz", 90.0)):
        if raw.get(axis) is not None:
            value = number(axis, -limit, limit, 0.0)
            if abs(value) >= 0.5:
                part[axis] = round(value, 1)
    color = str(raw.get("color") or "")
    if _HEX.match(color):
        part["color"] = color.lower()
    material = str(raw.get("material") or "").lower()
    if material in PART_MATERIALS:
        part["material"] = material
    if raw.get("tone") is not None:
        part["tone"] = number("tone", 0.4, 1.3, 1.0)
    return part


def _object_key(item: dict[str, Any], model: str, style_prompt: str) -> str:
    payload = {
        "version": OBJECT_PROMPT_VERSION,
        "model": model,
        "style": style_prompt,
        "max_parts": max_parts_per_object(),
        "thinking": os.getenv("GEMINI_OBJECT_PARTS_THINKING", "medium").strip().lower(),
        "type": item["type"],
        "label": item["label"],
        "color": item["color"],
        "attrs": item["attrs"],
        # 크기는 비율만 본다(5% 단위). 같은 가구를 조금 늘려도 모양을 다시 받지 않는다
        "ratio": [round(item["w"] / item["h"] * 20) / 20, round(item["d"] / item["h"] * 20) / 20],
        "image": item["image_digest"],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _object_image(obj: dict[str, Any], room_photo: Path | None, image_dir: Path | None) -> bytes | None:
    """상품은 상품 사진, 기존 가구는 방 사진에서 그 가구 부분을 잘라 쓴다."""
    max_side = _env_int("OBJECT_PARTS_IMAGE_MAX", 1280, 384, 2048)
    image_file = obj.get("image_file")
    if image_file and image_dir is not None:
        path = image_dir / Path(str(image_file)).name
        if path.is_file():
            from PIL import Image

            from .gemini_reanalyze import _crop

            with Image.open(path) as source:
                return _crop(source.convert("RGB"), None, max_side=max_side)
    if room_photo is not None and Path(room_photo).is_file() and obj.get("photo_box"):
        from PIL import Image

        from .gemini_reanalyze import _crop

        with Image.open(room_photo) as source:
            return _crop(source.convert("RGB"), obj["photo_box"], max_side=max_side)
    return None


def generate_object_parts(
    scene: dict[str, Any],
    cache_dir: str | Path,
    *,
    room_photo: str | Path | None = None,
    style_prompt: str = "",
    model: str | None = None,
    client: Any = None,
) -> dict[str, Any]:
    """가구별 부품 목록 {object_id: {"parts": [...]}}. 캐시에 없는 가구만 묻는다.

    기본은 가구마다 따로 묻는다(OBJECT_PARTS_BATCH=1). 한 요청에 여러 가구를 넣으면
    호출은 줄지만 모델의 주의가 나뉘어 세부가 떨어진다. 실패한 묶음만 비고, 빠진
    가구는 three.js가 파라메트릭 모양으로 그린다.
    """
    model = (
        model
        or os.getenv("GEMINI_FURNITURE_PARTS_MODEL", "").strip()
        or os.getenv("GEMINI_ANALYSIS_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    cache_root = Path(cache_dir).resolve() / OBJECT_PARTS_DIR
    cache_root.mkdir(parents=True, exist_ok=True)
    image_dir = Path(cache_dir).resolve() / "product_images_v1"
    photo = Path(room_photo) if room_photo else None
    max_parts = max_parts_per_object()
    batch_size = _env_int("OBJECT_PARTS_BATCH", 1, 1, 12)
    thinking = os.getenv("GEMINI_OBJECT_PARTS_THINKING", "medium")

    result: dict[str, Any] = {}
    pending: list[dict[str, Any]] = []
    for obj in scene.get("objects") or []:
        kind = str(obj.get("type") or "")
        if not kind or kind in SKIP_TYPES:
            continue
        image = _object_image(obj, photo, image_dir)
        item = {
            "id": str(obj["id"]),
            "type": kind,
            "label": str(obj.get("product_title") or obj.get("label") or kind)[:60],
            "w": max(float(obj.get("w_m") or 0.3), 0.05),
            "d": max(float(obj.get("d_m") or 0.3), 0.05),
            "h": max(float(obj.get("height_m") or 0.5), 0.05),
            "color": str(obj.get("color") or ""),
            "attrs": obj.get("attrs") or {},
            "image": image,
            "image_digest": hashlib.sha256(image).hexdigest()[:16] if image else None,
        }
        key = _object_key(item, model, style_prompt)
        path = cache_root / f"{key}.json"
        if path.is_file():
            try:
                result[item["id"]] = json.loads(path.read_text(encoding="utf-8"))
                continue
            except Exception:
                path.unlink(missing_ok=True)
        failed = cache_root / f"{key}.failed"
        if failed.is_file() and time.time() - failed.stat().st_mtime < FAILURE_COOLDOWN_SECONDS:
            continue
        item["key"] = key
        pending.append(item)

    if not pending:
        return result

    from .gemini_svg_experiment import _ensure_not_truncated

    with _GENERATION_LOCK:
        # Client 를 변수로 붙들어 둔다(AGENTS.md 7.4)
        try:
            client = client or _client()
        except Exception as exc:
            print(f"[gemini-object-parts] 클라이언트 생성 실패: {exc}")
            return result
        for start in range(0, len(pending), batch_size):
            batch = pending[start : start + batch_size]
            images = [item for item in batch if item["image"]]
            for item in batch:
                item["image_index"] = None
            for index, item in enumerate(images, start=1):
                item["image_index"] = index
            contents: list[Any] = [_object_prompt(batch, style_prompt)]
            contents += [types.Part.from_bytes(data=item["image"], mime_type="image/jpeg") for item in images]
            try:
                response = call_with_retry(
                    client.models.generate_content,
                    model=model,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json",
                        temperature=0.3,
                        # thinking 토큰도 이 한도를 함께 쓴다. 부품 40개 × 가구 수에
                        # 설계 추론까지 들어가므로 넉넉히 둔다
                        max_output_tokens=12000 + 6000 * len(batch),
                        thinking_config=thinking_for(model, thinking),
                    ),
                    description="가구별 3D 형태 생성",
                )
                if not response.text:
                    raise RuntimeError("Gemini가 가구 형태를 반환하지 않았습니다.")
                _ensure_not_truncated(response)
                payload = _extract_json(str(response.text))
                if not isinstance(payload, dict):
                    raise RuntimeError("가구 형태 응답 형식이 올바르지 않습니다.")
            except Exception as exc:
                print(f"[gemini-object-parts] 생성 실패({', '.join(i['id'] for i in batch)}): {exc}")
                for item in batch:
                    (cache_root / f"{item['key']}.failed").write_text(
                        f"{int(time.time())}\n{type(exc).__name__}: {exc}", encoding="utf-8"
                    )
                continue
            for item in batch:
                entry = payload.get(item["id"])
                # 가구 하나만 물었는데 id 없이 parts만 돌려주는 경우도 받아 준다
                if entry is None and len(batch) == 1 and "parts" in payload:
                    entry = payload
                raw_parts = entry.get("parts") if isinstance(entry, dict) else None
                parts = [
                    coerced
                    for coerced in (_coerce_object_part(raw) for raw in (raw_parts or [])[:max_parts])
                    if coerced
                ]
                if not parts:
                    # 빈 설계도를 넘기면 three.js가 가구를 통째로 지운다(AGENTS.md 7.9). 남기지 않는다
                    continue
                recipe = {"parts": parts, "source": "gemini", "model": model, "version": OBJECT_PROMPT_VERSION}
                (cache_root / f"{item['key']}.json").write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
                result[item["id"]] = recipe
    return result
