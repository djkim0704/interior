"""상품 사진에서 3D 모델을 만들 속성을 뽑는다 (항목 5).

기존 visual_profile은 색 양자화와 제목 키워드로만 채워져서 다리 모양·팔걸이·등받이
높이 같은 형태 정보가 타입 기본값이었다. 멀티모달 모델에게 사진을 보여 주고 형태
속성을 구조화 JSON으로 받는다. 사진에 치수표가 보이면 그 값도 받는다(치수 출처 'image').

결과는 기존 캐시 경로(product_visuals_gemini_v3)에 쓴다. 그 경로는 읽기만 하고 쓰는
곳이 없어 사실상 죽어 있었다.
"""
from __future__ import annotations

import json
import re
from typing import Any

from google.genai import types

from .gemini_svg_experiment import _ensure_not_truncated, minimal_thinking

LEG_STYLES = {"none", "four_legs", "metal_legs", "wood_legs", "hairpin", "pedestal", "sled", "casters", "plinth"}
BACK_HEIGHTS = {"none", "low", "mid", "high"}
TOP_SHAPES = {"rectangle", "rounded_rectangle", "round", "oval", "l_shape"}
MATERIALS = {"wood", "fabric", "leather", "metal", "glass", "plastic", "rattan", "marble", "mixed"}

PROMPT = """
Look at this furniture product photo and describe its physical form for a 3D model.
Product title: {title}
Expected kind: {kind}

Return JSON only:
{{
  "primary_color": "#rrggbb",
  "secondary_color": "#rrggbb",
  "material": "wood|fabric|leather|metal|glass|plastic|rattan|marble|mixed",
  "top_shape": "rectangle|rounded_rectangle|round|oval|l_shape",
  "leg_style": "none|four_legs|metal_legs|wood_legs|hairpin|pedestal|sled|casters|plinth",
  "leg_height_ratio": 0.15,
  "has_armrests": false,
  "back_height": "none|low|mid|high",
  "seat_count": 0,
  "has_headboard": false,
  "drawer_count": 0,
  "door_count": 0,
  "open_shelves": 0,
  "visible_dimensions_cm": {{"width": null, "depth": null, "height": null}},
  "confidence": 0.8
}}

Rules:
- leg_height_ratio is the visible leg height divided by the total height (0 if no legs).
- visible_dimensions_cm only if numbers are printed in the image (a size chart);
  otherwise keep nulls. Never guess numbers.
- Use the closest allowed value for every enum.
""".strip()


def _hex(value: Any) -> str | None:
    text = str(value or "").strip()
    return text.lower() if re.fullmatch(r"#[0-9a-fA-F]{6}", text) else None


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    return text if text in allowed else default


def _int(value: Any, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(round(float(value)))))
    except (TypeError, ValueError):
        return low


def normalize(raw: dict[str, Any]) -> dict[str, Any]:
    """모델 응답을 허용 값으로 묶는다. 이상한 값이 와도 3D가 깨지지 않게 한다."""
    try:
        leg_ratio = max(0.0, min(0.6, float(raw.get("leg_height_ratio") or 0.0)))
    except (TypeError, ValueError):
        leg_ratio = 0.0
    dims_cm = raw.get("visible_dimensions_cm") or {}
    dims = None
    try:
        width, depth = float(dims_cm.get("width")), float(dims_cm.get("depth"))
        height = dims_cm.get("height")
        if 8 <= width <= 400 and 8 <= depth <= 400:
            dims = {
                "w_m": round(width / 100, 3),
                "d_m": round(depth / 100, 3),
                "h_m": round(float(height) / 100, 3) if height not in (None, "") and 8 <= float(height) <= 300 else None,
            }
    except (TypeError, ValueError, AttributeError):
        dims = None
    try:
        confidence = max(0.0, min(1.0, float(raw.get("confidence", 0.5))))
    except (TypeError, ValueError):
        confidence = 0.5
    leg_style = _choice(raw.get("leg_style"), LEG_STYLES, "none")
    return {
        "primary_color": _hex(raw.get("primary_color")),
        "secondary_color": _hex(raw.get("secondary_color")),
        "material": _choice(raw.get("material"), MATERIALS, "mixed"),
        "top_shape": _choice(raw.get("top_shape"), TOP_SHAPES, "rectangle"),
        "leg_style": leg_style,
        "leg_height_ratio": round(leg_ratio if leg_style not in {"none", "plinth"} else 0.0, 3),
        "has_armrests": bool(raw.get("has_armrests")),
        "back_height": _choice(raw.get("back_height"), BACK_HEIGHTS, "none"),
        "seat_count": _int(raw.get("seat_count"), 0, 6),
        "has_headboard": bool(raw.get("has_headboard")),
        "drawer_count": _int(raw.get("drawer_count"), 0, 12),
        "door_count": _int(raw.get("door_count"), 0, 6),
        "open_shelves": _int(raw.get("open_shelves"), 0, 8),
        "dimensions": dims,
        "confidence": confidence,
        "analysis_source": "gemini",
    }


def extract(
    client: Any,
    image_bytes: bytes,
    mime_type: str,
    *,
    title: str,
    kind: str,
    model: str,
) -> dict[str, Any]:
    response = client.models.generate_content(
        model=model,
        contents=[
            PROMPT.format(title=title[:120] or "-", kind=kind or "furniture"),
            types.Part.from_bytes(data=image_bytes, mime_type=mime_type or "image/jpeg"),
        ],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            # pro 계열은 thinking을 끌 수 없어 추론 토큰이 이 한도를 함께 쓴다.
            # 1024면 응답이 잘려 모든 상품이 타입 기본 형태로 떨어진다
            max_output_tokens=8192,
            thinking_config=minimal_thinking(model),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini가 상품 속성을 반환하지 않았습니다.")
    _ensure_not_truncated(response)
    text = response.text.strip()
    start, end = text.find("{"), text.rfind("}")
    raw = json.loads(text[start : end + 1]) if start >= 0 and end > start else {}
    return normalize(raw if isinstance(raw, dict) else {})


# 타입별 기본 형태. 사진 분석이 없을 때(감지된 기존 가구 등)도 3D가 그럴듯하게 서도록 한다.
TYPE_DEFAULTS: dict[str, dict[str, Any]] = {
    "sofa": {"has_armrests": True, "back_height": "mid", "seat_count": 3, "leg_style": "wood_legs", "leg_height_ratio": 0.12},
    "chair": {"has_armrests": False, "back_height": "high", "leg_style": "four_legs", "leg_height_ratio": 0.5},
    "desk_chair": {"has_armrests": True, "back_height": "high", "leg_style": "casters", "leg_height_ratio": 0.45},
    "bed": {"has_headboard": True, "leg_style": "plinth", "leg_height_ratio": 0.0},
    "desk": {"leg_style": "four_legs", "top_shape": "rectangle", "drawer_count": 0},
    "table": {"leg_style": "four_legs", "top_shape": "rectangle"},
    "low_table": {"leg_style": "four_legs", "top_shape": "rectangle"},
    "cabinet": {"leg_style": "plinth", "door_count": 2},
    "dresser": {"leg_style": "plinth", "drawer_count": 4},
    "wardrobe": {"leg_style": "plinth", "door_count": 2},
    "shelf": {"leg_style": "none", "open_shelves": 4},
    "nightstand": {"leg_style": "plinth", "drawer_count": 2},
}


def attributes_for(kind: str, profile: dict[str, Any] | None) -> dict[str, Any]:
    """3D 빌더에 넘길 형태 속성. 사진 분석 값이 있으면 그걸, 없으면 타입 기본값."""
    base = dict(TYPE_DEFAULTS.get(kind, {}))
    profile = profile or {}
    observed = profile.get("analysis_source") == "gemini"
    for key in (
        "material",
        "top_shape",
        "leg_style",
        "leg_height_ratio",
        "has_armrests",
        "back_height",
        "seat_count",
        "has_headboard",
        "drawer_count",
        "door_count",
        "open_shelves",
        "primary_color",
        "secondary_color",
    ):
        value = profile.get(key)
        if value in (None, "", "mixed"):
            continue
        # 로컬 프로필(색 양자화·키워드)은 모르면 none/False/0을 채운다. 관찰한 값이 아니므로
        # 타입 기본값을 덮어쓰지 않는다. 사진을 본 Gemini 결과만 그런 값도 믿는다
        if not observed and value in ("none", False, 0):
            continue
        base[key] = value
    # 기존 로컬 프로필의 표기를 3D 표기로 맞춘다
    legacy_legs = {"wood": "wood_legs", "metal": "metal_legs"}
    if base.get("leg_style") in legacy_legs:
        base["leg_style"] = legacy_legs[base["leg_style"]]
    if base.get("leg_style") not in LEG_STYLES:
        base.pop("leg_style", None)
    return base
