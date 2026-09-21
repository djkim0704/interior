# Gemini layout JSON → rule-based SVG floor plan renderer (v3)
from __future__ import annotations

import json
import re
from html import escape
from pathlib import Path
from typing import Any

# API structured-output enum과 렌더러가 공유하는 객체 타입 vocabulary
LAYOUT_OBJECT_TYPES = [
    "bed", "desk", "table", "low_table", "shelf", "cabinet",
    "chair", "floor_chair", "stool", "rug", "mirror", "lamp",
    "plant", "door", "window", "unknown",
    # 구매 가능한 종류(backend PURCHASE_LABELS)와 맞추기 위해 추가.
    # 예전에는 소파를 구매해도 어휘에 없어서 unknown 박스로 그려졌다.
    "sofa", "wardrobe", "dresser", "bench",
    # 방 사진에 자주 나오는 생활가전·가구
    "tv", "fridge", "aircon", "washer",
    "vanity", "nightstand", "desk_chair", "curtain",
]

# 벽·바닥에 고정돼 드래그·격자 스냅에서 제외하는 요소
FIXED_TYPES = {
    "window", "door", "rug", "curtain", "aircon", "tv",
}

# 프롬프트는 코드에 하드코딩하지 않고 프로젝트 루트 prompts/*.txt에서 읽어온다.
# rule_based_svg.py는 mood_pipeline/ 안에 있으므로 부모의 부모가 프로젝트 루트.
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts"


def load_prompt(name: str) -> str:
    """prompts/<name>.txt 파일을 읽어 프롬프트 문자열로 반환 (앞뒤 공백 제거)."""
    return (PROMPTS_DIR / f"{name}.txt").read_text(encoding="utf-8").strip()


RULE_BASED_LAYOUT_PROMPT = load_prompt("rule_based_layout")

RENDERER_VERSION = "4.0"

# 방/캔버스 크기 — aspect_ratio에 따라 render_svg 시작 시 _set_canvas()가 재계산.
BASE_LONG_SIDE = 820  # 긴 변 기준 길이(px)
ROOM_W, ROOM_H = 860, 560
MARGIN_X, MARGIN_Y = 90, 90
CANVAS_W = ROOM_W + MARGIN_X * 2
CANVAS_H = ROOM_H + MARGIN_Y * 2 + 120
GRID = 50
GRID_SNAP = 20  # 가구 정렬 격자(px)

# 모노톤 팔레트 — 색으로 구분하지 않고 선/톤 차이로만 읽히게 한다.
STYLE = {
    "wall": "#141414",
    "line": "#3f3f3f",
    "thin": "#9c9c9c",
    "grid": "#ededed",
    "floor": "#ffffff",
    "wood": "#dcdcdc",
    "bed": "#fcfcfc",
    "glass": "#ebebeb",
    "text": "#111111",
    "muted": "#767676",
    "rug": "#b8b8b8",
    "accent": "#111111",
    "accent_soft": "#e8e8e8",
    "existing": "#f5f5f5",
    "recommend": "#e9e9e9",
    # 아이소메트릭에서 면을 구분하는 3톤 (위/좌/우)
    "iso_top": "#f2f2f2",
    "iso_left": "#d8d8d8",
    "iso_right": "#c2c2c2",
}

FONT_FAMILY = "Pretendard, Noto Sans KR, Arial, sans-serif"


KOREAN_LABELS = {
    "bed": "침대",
    "desk": "책상",
    "table": "테이블",
    "low_table": "낮은 테이블",
    "shelf": "선반",
    "cabinet": "수납장",
    "chair": "의자",
    "floor_chair": "좌식 의자",
    "stool": "스툴",
    "rug": "러그",
    "mirror": "거울",
    "lamp": "조명",
    "plant": "식물",
    "door": "문",
    "window": "창문",
    "unknown": "가구",
    "sofa": "소파",
    "wardrobe": "옷장",
    "dresser": "서랍장",
    "bench": "벤치",
    "tv": "TV",
    "fridge": "냉장고",
    "aircon": "에어컨",
    "washer": "세탁기",
    "vanity": "화장대",
    "nightstand": "협탁",
    "desk_chair": "책상 의자",
    "curtain": "커튼",
}


LABEL_ALIASES = {
    "single bed": "싱글 침대",
    "bed": "침대",
    "desk": "책상",
    "table": "테이블",
    "low table": "낮은 테이블",
    "nightstand": "협탁",
    "side table": "협탁",
    "tv stand": "TV장",
    "table lamp": "탁상 조명",
    "floor lamp": "스탠드 조명",
    "shelf": "선반",
    "cabinet": "수납장",
    "chair": "의자",
    "floor chair": "좌식 의자",
    "stool": "스툴",
    "rug": "러그",
    "mirror": "거울",
    "lamp": "조명",
    "plant": "식물",
    "door": "문",
    "window": "창문",
    "sofa": "소파",
    "couch": "소파",
    "two seater sofa": "2인 소파",
    "wardrobe": "옷장",
    "closet": "옷장",
    "dresser": "서랍장",
    "chest of drawers": "서랍장",
    "bench": "벤치",
    "tv": "TV",
    "television": "TV",
    "fridge": "냉장고",
    "refrigerator": "냉장고",
    "aircon": "에어컨",
    "air conditioner": "에어컨",
    "washer": "세탁기",
    "washing machine": "세탁기",
    "vanity": "화장대",
    "dressing table": "화장대",
    "desk chair": "책상 의자",
    "office chair": "책상 의자",
    "curtain": "커튼",
    "curtains": "커튼",
    "blind": "블라인드",
}

STD_SIZE: dict[str, tuple[int, int]] = {
    "bed": (230, 250),
    "desk": (280, 115),
    "table": (210, 110),
    "low_table": (230, 95),
    "shelf": (165, 120),
    "cabinet": (150, 110),
    "chair": (70, 70),
    "floor_chair": (78, 78),
    "stool": (82, 82),
    "rug": (360, 210),
    "mirror": (48, 170),
    "lamp": (118, 105),
    "plant": (55, 55),
    "window": (240, 20),
    "door": (25, 170),
    "unknown": (110, 75),
    # 구매 가능 종류
    "sofa": (250, 120),
    "wardrobe": (170, 118),
    "dresser": (140, 105),
    "bench": (170, 82),
    # 생활가전·가구 (tv·aircon·curtain 은 벽에 붙는 얇은 요소)
    "tv": (190, 26),
    "fridge": (110, 105),
    "aircon": (130, 26),
    "washer": (100, 100),
    "vanity": (160, 92),
    "nightstand": (80, 80),
    "desk_chair": (86, 86),
    "curtain": (240, 20),
}

ZONE_POS: dict[str, tuple[float, float]] = {
    "bed": (0.20, 0.25),
    "shelf": (0.43, 0.16),
    "cabinet": (0.43, 0.16),
    "lamp": (0.62, 0.17),
    "stool": (0.76, 0.17),
    "rug": (0.50, 0.50),
    "mirror": (0.10, 0.70),
    "low_table": (0.78, 0.58),
    "table": (0.78, 0.58),
    "desk": (0.76, 0.78),
    "floor_chair": (0.57, 0.75),
    "chair": (0.67, 0.75),
    "window": (0.45, 0.98),
    "door": (0.98, 0.43),
    "plant": (0.88, 0.55),
    "unknown": (0.50, 0.50),
    "sofa": (0.50, 0.72),
    "wardrobe": (0.14, 0.18),
    "dresser": (0.30, 0.16),
    "bench": (0.50, 0.88),
    "tv": (0.50, 0.05),
    "fridge": (0.90, 0.12),
    "aircon": (0.76, 0.04),
    "washer": (0.90, 0.88),
    "vanity": (0.16, 0.60),
    "nightstand": (0.06, 0.34),
    "desk_chair": (0.70, 0.66),
    "curtain": (0.44, 0.03),
}

TYPE_PRIORITY: dict[str, int] = {
    "rug": 0,
    "bed": 1,
    "desk": 2,
    "table": 2,
    "low_table": 2,
    "shelf": 3,
    "cabinet": 3,
    "mirror": 4,
    "lamp": 4,
    "stool": 4,
    "chair": 4,
    "floor_chair": 4,
    "window": 5,
    "door": 5,
    "plant": 5,
    # 0=바닥에 깔리는 것, 숫자가 클수록 나중에(위에) 그린다
    "sofa": 1,
    "wardrobe": 3,
    "dresser": 3,
    "fridge": 3,
    "washer": 3,
    "vanity": 2,
    "bench": 4,
    "nightstand": 4,
    "desk_chair": 4,
    "tv": 5,
    "aircon": 5,
    "curtain": 5,
}

LayoutDict = dict[str, Any]
PlacedObject = dict[str, Any]
Rect = tuple[float, float, float, float]


def extract_json_from_text(raw: str) -> dict[str, Any]:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise RuntimeError("응답에서 JSON 객체를 찾을 수 없습니다.")
    return json.loads(text[start : end + 1])


def norm_type(t: str | None) -> str:
    t = (t or "unknown").lower().strip().replace(" ", "_").replace("-", "_")
    aliases = {
        "laptop": "desk",
        "laptop_desk": "desk",
        "coffee_table": "low_table",
        "shelf_unit": "shelf",
        "shelving": "shelf",
        "bookshelf": "shelf",
        "carpet": "rug",
        "table_lamp": "lamp",
        # Gemini·YOLO 가 내놓는 표현을 어휘로 흡수한다
        "couch": "sofa",
        "settee": "sofa",
        "closet": "wardrobe",
        "armoire": "wardrobe",
        "chest_of_drawers": "dresser",
        "drawers": "dresser",
        "television": "tv",
        "tv_set": "tv",
        "monitor": "tv",
        "refrigerator": "fridge",
        "air_conditioner": "aircon",
        "ac": "aircon",
        "washing_machine": "washer",
        "dressing_table": "vanity",
        "makeup_table": "vanity",
        "bedside_table": "nightstand",
        "side_table": "nightstand",
        "office_chair": "desk_chair",
        "curtains": "curtain",
        "blind": "curtain",
        "blinds": "curtain",
    }
    t = aliases.get(t, t)
    return t if t in STD_SIZE else "unknown"

def display_label(
    object_type: str,
    raw_label: str | None,
    source: str | None = None,
) -> str:
    """
    영문 또는 기술용 가구 라벨을
    사용자용 한글 라벨로 변환한다.
    """

    object_type = norm_type(
        object_type
    )

    label_text = str(
        raw_label or ""
    ).strip()

    # 추천 가구의 번호는 별도 배지로 그리기 때문에
    # "1. 새 테이블"에서 앞 번호를 제거한다.
    if source == "selected_product":
        label_text = re.sub(
            r"^\s*\d+\s*[.)-]?\s*",
            "",
            label_text,
        )

        if label_text:
            return label_text

    normalized = (
        label_text
        .lower()
        .replace("_", " ")
        .replace("-", " ")
    )

    normalized = re.sub(
        r"\s+",
        " ",
        normalized,
    ).strip()

    if normalized in LABEL_ALIASES:
        return LABEL_ALIASES[
            normalized
        ]

    type_text = object_type.replace(
        "_",
        " ",
    )

    if (
        not normalized
        or normalized == type_text
    ):
        return KOREAN_LABELS.get(
            object_type,
            "가구",
        )

    # 이미 한글 이름이면 그대로 사용
    if re.search(
        r"[가-힣]",
        label_text,
    ):
        return label_text

    return KOREAN_LABELS.get(
        object_type,
        label_text or "가구",
    )


def clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _is_percent_coords(obj: dict[str, Any]) -> bool:
    nums: list[float] = []
    for key in ("x", "y", "w", "h"):
        val = obj.get(key)
        if val is not None:
            try:
                nums.append(float(val))
            except (TypeError, ValueError):
                pass
    return bool(nums) and max(abs(n) for n in nums) > 1.5


def _infer_wall(cx: float, cy: float) -> str:
    if cx < 0.22:
        return "left"
    if cx > 0.78:
        return "right"
    if cy < 0.22:
        return "top"
    if cy > 0.78:
        return "bottom"
    return "none"


def _type_from_object(obj: dict[str, Any]) -> str:
    label = (obj.get("label") or "").lower()
    t = norm_type(str(obj.get("type") or ""))
    if "low table" in label:
        return "low_table"
    if "mirror" in label:
        return "mirror"
    if "floor chair" in label:
        return "floor_chair"
    if "laptop" in label or "desk" in label:
        return "desk"
    if t == "table" and "low" in label:
        return "low_table"
    if t in ("unknown", "other"):
        if "stool" in label:
            return "stool"
        if "mirror" in label:
            return "mirror"
        if "rug" in label:
            return "rug"
    return t


def _skip_redundant_object(obj: dict[str, Any]) -> bool:
    label = (obj.get("label") or "").lower()
    skip = ("laptop", "lamp on table", "plant on stool", "table lamp")
    return any(s in label for s in skip)


def coerce_layout_v3(layout: LayoutDict) -> LayoutDict:
    """layout_detail v2(0~100) 또는 혼합 스키마 → v3 렌더러용 0~1 center 좌표."""
    if layout.get("schema") == "rule_based_v3":
        return layout

    room_in = layout.get("room") or {}
    aspect = (
        room_in.get("aspect_ratio")
        or room_in.get("width_to_depth_ratio")
        or 1.45
    )

    out_objects: list[dict[str, Any]] = []
    chair_count = 0

    for obj in layout.get("objects", []):
        if _skip_redundant_object(obj):
            continue

        t = _type_from_object(obj)
        if t == "floor_chair":
            chair_count += 1
            if chair_count > 4:
                continue

        if _is_percent_coords(obj):
            x = float(obj.get("x", 0))
            y = float(obj.get("y", 0))
            w = float(obj.get("w", 10))
            h = float(obj.get("h", 10))
            # v2(0~100)는 x,y가 좌상단 → center 변환, w,h는 0~1 비율로 보존
            cx = clamp((x + w / 2) / 100.0, 0.04, 0.96)
            cy = clamp((y + h / 2) / 100.0, 0.04, 0.96)
            ow = clamp(w / 100.0, 0.0, 0.6)
            oh = clamp(h / 100.0, 0.0, 0.6)
        else:
            cx = clamp(float(obj.get("x", 0.5)), 0.04, 0.96)
            cy = clamp(float(obj.get("y", 0.5)), 0.04, 0.96)
            ow = clamp(float(obj.get("w", 0.0) or 0.0), 0.0, 0.6)
            oh = clamp(float(obj.get("h", 0.0) or 0.0), 0.0, 0.6)

        wall = obj.get("wall") or _infer_wall(cx, cy)
        if wall == "none":
            wall = _infer_wall(cx, cy)

        conf = float(obj.get("confidence", 0.72) or 0.72)
        out_objects.append(
            {
                "type": t,
                "label": (
                    obj.get("label")
                    or t.replace(
                        "_",
                        " ",
                    ).title()
                ),
                "x": cx,
                "y": cy,
                "w": ow,
                "h": oh,
                "wall": wall,
                "confidence": conf,

                # 추천 상품과 연결하는 정보
                "source": obj.get(
                    "source"
                ),
                "product_marker": obj.get(
                    "product_marker"
                ),
                "product_link": obj.get(
                    "product_link"
                ),
                "product_title": obj.get(
                    "product_title"
                ),
            }
        )

    has_window = any(o["type"] == "window" for o in out_objects)
    if not has_window:
        for wall in layout.get("walls") or []:
            side = str(wall.get("side", "")).lower()
            wins = wall.get("windows") or []
            if not wins:
                continue
            pos_map = {"west": "left", "east": "right", "north": "top", "south": "bottom"}
            wside = pos_map.get(side, side)
            if wside not in ("left", "right", "top", "bottom"):
                continue
            cx, cy = {"left": (0.05, 0.5), "right": (0.95, 0.5), "top": (0.5, 0.05), "bottom": (0.5, 0.95)}[wside]
            out_objects.append(
                {
                    "type": "window",
                    "label": "Window",
                    "x": cx,
                    "y": cy,
                    "w": 0.24,
                    "h": 0.03,
                    "wall": wside,
                    "confidence": 0.7,
                }
            )

    return {
        "schema": "rule_based_v3",
        "room": {
            "shape": room_in.get("shape", "rectangle"),
            "aspect_ratio": float(aspect),
            "description": room_in.get("description", ""),
        },
        "objects": out_objects,
    }


ABS_MIN_SIZE = 22  # 아무리 작아도 이 픽셀 미만으로는 안 그림(가독성)


def _gemini_size(o: dict[str, Any], std_w: int, std_h: int) -> tuple[float, float]:
    """Gemini w,h(0~1 비율)를 픽셀 크기로 변환해 모든 타입에 반영.
    없거나 비정상이면 표준 크기로 fallback. 아이콘이 깨지지 않게
    최소=표준의 60%(절대 하한 22px), 최대=방의 60%로만 클램프한다."""
    try:
        gw = float(o.get("w") or 0.0)
    except (TypeError, ValueError):
        gw = 0.0
    try:
        gh = float(o.get("h") or 0.0)
    except (TypeError, ValueError):
        gh = 0.0
    w = gw * ROOM_W if gw > 0.015 else std_w
    h = gh * ROOM_H if gh > 0.015 else std_h
    w = clamp(w, max(ABS_MIN_SIZE, std_w * 0.6), ROOM_W * 0.6)
    h = clamp(h, max(ABS_MIN_SIZE, std_h * 0.6), ROOM_H * 0.6)
    return w, h


_SURFACE_TYPES = {"desk", "table", "low_table"}


def _footprint_frac(o: dict[str, Any]) -> tuple[float, float, float, float]:
    x, y = float(o.get("x", 0.5)), float(o.get("y", 0.5))
    w, h = float(o.get("w", 0.1) or 0.1), float(o.get("h", 0.1) or 0.1)
    return x - w / 2, y - h / 2, x + w / 2, y + h / 2


def _contained_frac(a: dict[str, Any], b: dict[str, Any]) -> float:
    """a 면적 중 b와 겹치는 비율(0~1)."""
    ax0, ay0, ax1, ay1 = _footprint_frac(a)
    bx0, by0, bx1, by1 = _footprint_frac(b)
    ix = max(0.0, min(ax1, bx1) - max(ax0, bx0))
    iy = max(0.0, min(ay1, by1) - max(ay0, by0))
    area = max(1e-6, (ax1 - ax0) * (ay1 - ay0))
    return ix * iy / area


def merge_subparts(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """책상 서랍 등 '부분품'을 모품(desk/table)에 흡수(제거).
    - 라벨에 drawer/drawers가 있고 surface에 걸치면 제거
    - cabinet/shelf/unknown이 surface에 60% 이상 포함되면 제거(책상 위/밑 부속)."""
    surfaces = [o for o in objects if norm_type(o.get("type")) in _SURFACE_TYPES]
    if not surfaces:
        return objects

    kept: list[dict[str, Any]] = []
    for o in objects:
        if o in surfaces:
            kept.append(o)
            continue
        t = norm_type(o.get("type"))
        label = (o.get("label") or "").lower()
        drop = False
        if "drawer" in label and t in ("cabinet", "unknown", "desk", "shelf"):
            drop = any(_contained_frac(o, s) > 0.35 for s in surfaces)
        if not drop and t in ("cabinet", "shelf", "unknown"):
            drop = any(_contained_frac(o, s) > 0.6 for s in surfaces)
        if not drop:
            kept.append(o)
    return kept


def postprocess_layout_objects(objects: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """부분품 병합, 저신뢰 소품 제거, 벽당 창문 1개, low_table 주변 chair → floor_chair."""
    objects = merge_subparts(objects)
    filtered: list[dict[str, Any]] = []
    lamp_count = 0

    for obj in objects:
        t = norm_type(obj.get("type"))
        conf = float(obj.get("confidence", 0.5) or 0.5)
        label = (obj.get("label") or "").lower()

        if t in ("lamp", "plant") and conf < 0.65:
            continue
        if t == "lamp":
            lamp_count += 1
            if lamp_count > 2:
                continue
        if any(s in label for s in ("laptop", "table lamp", "plant on stool")):
            continue

        filtered.append(obj)

    windows_by_wall: dict[str, dict[str, Any]] = {}
    rest: list[dict[str, Any]] = []
    wall_center = {
        "left": (0.05, 0.5),
        "right": (0.95, 0.5),
        "top": (0.5, 0.05),
        "bottom": (0.5, 0.95),
    }

    for obj in filtered:
        t = norm_type(obj.get("type"))
        if t != "window":
            rest.append(obj)
            continue
        wall = str(obj.get("wall") or "bottom").lower()
        if wall not in wall_center:
            wall = _infer_wall(float(obj.get("x", 0.5)), float(obj.get("y", 0.5)))
        conf = float(obj.get("confidence", 0.5) or 0.5)
        prev = windows_by_wall.get(wall)
        if prev is None or conf > float(prev.get("confidence", 0) or 0):
            cx, cy = wall_center.get(wall, (0.5, 0.95))
            windows_by_wall[wall] = {
                **obj,
                "type": "window",
                "label": "Window",
                "wall": wall,
                "x": cx,
                "y": cy,
                "w": 0.35,
                "h": 0.03,
                "confidence": conf,
            }

    rest.extend(windows_by_wall.values())

    table = next((o for o in rest if norm_type(o.get("type")) == "low_table"), None)
    if table is not None:
        tx = float(table.get("x", 0.5))
        ty = float(table.get("y", 0.5))
        for obj in rest:
            if norm_type(obj.get("type")) != "chair":
                continue
            cx = float(obj.get("x", 0.5))
            cy = float(obj.get("y", 0.5))
            if (cx - tx) ** 2 + (cy - ty) ** 2 < 0.12:
                obj["type"] = "floor_chair"
                obj["label"] = "Floor Chair"

    return rest


def normalize_objects(
    layout: LayoutDict,
) -> list[PlacedObject]:
    objs: list[PlacedObject] = []

    for idx, obj in enumerate(
        layout.get(
            "objects",
            [],
        )
    ):
        object_type = norm_type(
            obj.get("type")
        )

        source = obj.get(
            "source"
        )

        object_label = display_label(
            object_type,
            obj.get("label"),
            source=source,
        )

        std_w, std_h = STD_SIZE.get(
            object_type,
            STD_SIZE["unknown"],
        )

        zone_x, zone_y = ZONE_POS.get(
            object_type,
            ZONE_POS["unknown"],
        )

        confidence = float(
            obj.get(
                "confidence",
                0.5,
            )
            or 0.5
        )

        x_value = obj.get("x")
        y_value = obj.get("y")

        x_normalized = (
            float(x_value)
            if x_value is not None
            else zone_x
        )

        y_normalized = (
            float(y_value)
            if y_value is not None
            else zone_y
        )

        center_x = (
            clamp(
                x_normalized,
                0.02,
                0.98,
            )
            * ROOM_W
            + MARGIN_X
        )

        center_y = (
            clamp(
                y_normalized,
                0.02,
                0.98,
            )
            * ROOM_H
            + MARGIN_Y
        )

        width, height = _gemini_size(
            obj,
            std_w,
            std_h,
        )

        objs.append(
            {
                "type": object_type,
                "label": object_label,
                "cx": center_x,
                "cy": center_y,
                "w": width,
                "h": height,
                "wall": (
                    obj.get(
                        "wall",
                        "none",
                    )
                    or "none"
                ),
                "confidence": confidence,
                "idx": idx,

                # 추천 상품 표시 정보
                "source": source,
                "product_marker": (
                    obj.get(
                        "product_marker"
                    )
                ),
                "product_link": (
                    obj.get(
                        "product_link"
                    )
                ),
                "product_title": (
                    obj.get(
                        "product_title"
                    )
                ),
            }
        )

    return objs


CHAIR_SNAP_OFFSET = 92


def snap_chairs_to_table(objs: list[PlacedObject]) -> list[PlacedObject]:
    """low_table(또는 주변에 의자가 모인 table) 주변 floor_chair를 상하좌우로 정렬."""
    tables = [o for o in objs if o["type"] in ("low_table", "table")]
    if not tables:
        return objs

    chairs = [o for o in objs if o["type"] in ("chair", "floor_chair")]
    if len(chairs) < 2:
        return objs

    table = next((o for o in tables if o["type"] == "low_table"), None)
    if table is None:
        best: PlacedObject | None = None
        best_count = 0
        for cand in tables:
            near = sum(
                1
                for c in chairs
                if (c["cx"] - cand["cx"]) ** 2 + (c["cy"] - cand["cy"]) ** 2 < 120**2
            )
            if near > best_count:
                best, best_count = cand, near
        if best is None or best_count < 2:
            return objs
        table = best
        table["type"] = "low_table"
        table["label"] = "Low Table"

    tcx, tcy = table["cx"], table["cy"]
    offsets = [(0, 1), (0, -1), (1, 0), (-1, 0)]
    chairs.sort(key=lambda o: (o["cx"] - tcx) ** 2 + (o["cy"] - tcy) ** 2)

    for i, chair in enumerate(chairs[:4]):
        dx, dy = offsets[i]
        chair["cx"] = clamp(
            tcx + dx * CHAIR_SNAP_OFFSET,
            MARGIN_X + 36,
            MARGIN_X + ROOM_W - 36,
        )
        chair["cy"] = clamp(
            tcy + dy * CHAIR_SNAP_OFFSET,
            MARGIN_Y + 36,
            MARGIN_Y + ROOM_H - 36,
        )
        chair["type"] = "floor_chair"
        chair["label"] = "Floor Chair"
        cw, ch = STD_SIZE["floor_chair"]
        chair["w"], chair["h"] = cw, ch
        chair["wall"] = "none"

    return objs


def wall_attach(o: PlacedObject) -> PlacedObject:
    t = o["type"]
    x = o["cx"] - o["w"] / 2
    y = o["cy"] - o["h"] / 2
    wall = o.get("wall", "none")

    if t == "window":
        wall = wall if wall in ("top", "bottom", "left", "right") else "bottom"
        if wall in ("top", "bottom"):
            o["w"], o["h"] = STD_SIZE["window"]
            x = clamp(o["cx"] - o["w"] / 2, MARGIN_X + 80, MARGIN_X + ROOM_W - o["w"] - 80)
            y = MARGIN_Y - 7 if wall == "top" else MARGIN_Y + ROOM_H - 13
        else:
            o["w"], o["h"] = 20, 220
            x = MARGIN_X - 7 if wall == "left" else MARGIN_X + ROOM_W - 13
            y = clamp(o["cy"] - o["h"] / 2, MARGIN_Y + 80, MARGIN_Y + ROOM_H - o["h"] - 80)
    elif t == "door":
        wall = wall if wall in ("top", "bottom", "left", "right") else "right"
        if wall in ("left", "right"):
            o["w"], o["h"] = 25, 170
            x = MARGIN_X if wall == "left" else MARGIN_X + ROOM_W - o["w"]
            y = clamp(o["cy"] - o["h"] / 2, MARGIN_Y + 80, MARGIN_Y + ROOM_H - o["h"] - 80)
        else:
            o["w"], o["h"] = 170, 25
            x = clamp(o["cx"] - o["w"] / 2, MARGIN_X + 80, MARGIN_X + ROOM_W - o["w"] - 80)
            y = MARGIN_Y if wall == "top" else MARGIN_Y + ROOM_H - o["h"]
    elif wall in ("left", "right", "top", "bottom"):
        pad = 30
        if wall == "left":
            x = MARGIN_X + pad
        if wall == "right":
            x = MARGIN_X + ROOM_W - o["w"] - pad
        if wall == "top":
            y = MARGIN_Y + pad
        if wall == "bottom":
            y = MARGIN_Y + ROOM_H - o["h"] - pad

    o["x"] = clamp(x, MARGIN_X + 18, MARGIN_X + ROOM_W - o["w"] - 18)
    o["y"] = clamp(y, MARGIN_Y + 18, MARGIN_Y + ROOM_H - o["h"] - 18)
    return o


def auto_pack(objs: list[PlacedObject]) -> list[PlacedObject]:
    # 벽 부착만 적용하고 Gemini 위치는 유지 — 겹침 해소는 resolve_overlaps가 담당.
    # rug(우선순위 0)가 먼저 오도록 정렬 → 그리기 순서상 배경에 깔림.
    return [wall_attach(o) for o in sorted(objs, key=lambda x: TYPE_PRIORITY.get(x["type"], 9))]


# ── 틀(방 경계·벽·겹침) 안으로 밀어넣는 보정 ────────────────────────────────
INNER_L = MARGIN_X + 18
INNER_T = MARGIN_Y + 18
INNER_R = MARGIN_X + ROOM_W - 18
INNER_B = MARGIN_Y + ROOM_H - 18


# 소프트 벽 고정: 겹침 해소 때는 모든 축 이동 허용(벽이 꽉 차면 안쪽으로 밀려남).
# 대신 _wall_pull이 매 반복 벽 쪽으로 되당겨, 여유가 있으면 벽에 붙게 만든다.
def _can_move_x(o: PlacedObject) -> bool:
    return True


def _can_move_y(o: PlacedObject) -> bool:
    return True


# 벽에 되당길 구조 가구(소품 lamp/plant/chair는 제외 → 벽 앞 2열로 자유 배치)
WALL_PULL_TYPES = {"bed", "shelf", "cabinet", "desk", "table", "low_table", "mirror"}
WALL_PULL = 0.30


def _wall_pull(o: PlacedObject) -> None:
    if o["type"] not in WALL_PULL_TYPES:
        return
    w = o.get("wall")
    if w == "left":
        o["x"] += (MARGIN_X + 30 - o["x"]) * WALL_PULL
    elif w == "right":
        o["x"] += (MARGIN_X + ROOM_W - o["w"] - 30 - o["x"]) * WALL_PULL
    elif w == "top":
        o["y"] += (MARGIN_Y + 30 - o["y"]) * WALL_PULL
    elif w == "bottom":
        o["y"] += (MARGIN_Y + ROOM_H - o["h"] - 30 - o["y"]) * WALL_PULL


def _clamp_into_room(o: PlacedObject) -> None:
    o["x"] = clamp(o["x"], INNER_L, INNER_R - o["w"])
    o["y"] = clamp(o["y"], INNER_T, INNER_B - o["h"])


def _apply_push(a: PlacedObject, b: PlacedObject, pen: float, sign: float, axis: str) -> None:
    can_a = _can_move_x(a) if axis == "x" else _can_move_y(a)
    can_b = _can_move_x(b) if axis == "x" else _can_move_y(b)
    if can_a and can_b:
        a[axis] += sign * pen / 2
        b[axis] -= sign * pen / 2
    elif can_a:
        a[axis] += sign * pen
    elif can_b:
        b[axis] -= sign * pen


_GROUP_SEATING = {"chair", "floor_chair", "stool"}
_GROUP_TABLES = {"table", "low_table"}
_ACCESSORY = {"lamp", "plant"}
_ACC_SURFACES = {"desk", "table", "low_table", "shelf", "cabinet"}

# 의도된 조합이라도 이 비율을 넘게 파묻히면 "박힌 것"으로 보고 떼어놓는다.
# (의자가 식탁에 살짝 물리는 건 자연스럽지만, 절반 이상 잠기면 배치가 깨져 보인다.)
GROUP_OVERLAP_MAX = 0.35


def _overlap_frac_px(a: PlacedObject, b: PlacedObject) -> float:
    """픽셀 좌표(x,y = 좌상단) 기준, 작은 쪽 면적 중 겹치는 비율(0~1)."""
    ix = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    iy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    if ix <= 0 or iy <= 0:
        return 0.0
    smaller = min(a["w"] * a["h"], b["w"] * b["h"])
    if smaller <= 0:
        return 0.0
    return ix * iy / smaller


def _grouped_pair(a: PlacedObject, b: PlacedObject) -> bool:
    # 인접/포개짐이 의도된 조합은 서로 밀어내지 않는다:
    #  - 테이블 주변의 의자
    #  - 책상/선반 위에 올려둔 조명·화분(그리기 순서상 위에 얹혀 보임)
    # 단, 얕게 물릴 때만 면제한다. 깊게 파묻힌 건 의도가 아니라 배치 오류다.
    ta, tb = a["type"], b["type"]
    if (ta in _GROUP_TABLES and tb in _GROUP_SEATING) or (
        tb in _GROUP_TABLES and ta in _GROUP_SEATING
    ):
        return _overlap_frac_px(a, b) <= GROUP_OVERLAP_MAX
    if (ta in _ACCESSORY and tb in _ACC_SURFACES) or (
        tb in _ACCESSORY and ta in _ACC_SURFACES
    ):
        return True
    return False


def _separate_pair(a: PlacedObject, b: PlacedObject, pad: float) -> bool:
    if _grouped_pair(a, b):
        return False
    ox = min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"])
    oy = min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"])
    if ox <= -pad or oy <= -pad:  # 이미 pad 이상 떨어짐
        return False

    px, py = ox + pad, oy + pad  # 여유 패딩 포함 침투량
    x_ok = _can_move_x(a) or _can_move_x(b)
    y_ok = _can_move_y(a) or _can_move_y(b)
    if not x_ok and not y_ok:
        return False

    # 침투가 작은 축으로 분리하되, 그 축이 둘 다 고정이면 다른 축 사용
    use_x = px <= py
    if use_x and not x_ok:
        use_x = False
    elif not use_x and not y_ok:
        use_x = True

    # 같은 벽에 붙은 두 가구는 벽면을 따라 분리(좌우벽→상하, 상하벽→좌우)
    wa, wb = a.get("wall"), b.get("wall")
    if wa == wb:
        if wa in ("left", "right"):
            use_x = False
        elif wa in ("top", "bottom"):
            use_x = True

    if use_x:
        sign = -1.0 if (a["x"] + a["w"] / 2) <= (b["x"] + b["w"] / 2) else 1.0
        _apply_push(a, b, px, sign, "x")
    else:
        sign = -1.0 if (a["y"] + a["h"] / 2) <= (b["y"] + b["h"] / 2) else 1.0
        _apply_push(a, b, py, sign, "y")
    return True


def _push_out_of_rect(a: PlacedObject, r: Rect, pad: float) -> None:
    rx, ry, rw, rh = r
    px = min(a["x"] + a["w"], rx + rw) - max(a["x"], rx)
    py = min(a["y"] + a["h"], ry + rh) - max(a["y"], ry)
    if px <= 0 or py <= 0:
        return
    if px <= py and _can_move_x(a):
        sign = -1.0 if (a["x"] + a["w"] / 2) <= (rx + rw / 2) else 1.0
        a["x"] += sign * (px + pad)
    elif _can_move_y(a):
        sign = -1.0 if (a["y"] + a["h"] / 2) <= (ry + rh / 2) else 1.0
        a["y"] += sign * (py + pad)


def resolve_overlaps(
    objs: list[PlacedObject], iterations: int = 140, pad: float = 8.0
) -> list[PlacedObject]:
    """Gemini 배치는 유지하되 겹치는 가구를 최소 이동으로 떼어 놓고, 방 안에 가둔다.
    rug는 배경, window/door는 벽 고정이라 대상에서 제외(문/창은 회피 장애물로만 사용)."""
    movable = [o for o in objs if o["type"] not in ("rug", "window", "door")]
    obstacles: list[Rect] = [
        (o["x"], o["y"], o["w"], o["h"])
        for o in objs
        if o["type"] in ("window", "door")
    ]
    n = len(movable)
    for _ in range(iterations):
        moved = False
        for i in range(n):
            a = movable[i]
            for j in range(i + 1, n):
                if _separate_pair(a, movable[j], pad):
                    moved = True
            for rect in obstacles:
                _push_out_of_rect(a, rect, pad)
        for o in movable:
            _wall_pull(o)  # 여유 있으면 벽으로 되당김(소프트 고정)
            _clamp_into_room(o)
        if not moved:
            break
    return objs


def grid_align(objs: list[PlacedObject], grid: int = GRID_SNAP) -> list[PlacedObject]:
    """가구를 격자에 살짝 스냅해 정렬감(틀)을 준다. 벽 고정 축은 건드리지 않음.
    (스냅 후 미세 겹침이 생길 수 있어 render_svg에서 resolve_overlaps를 한 번 더 돌린다.)"""
    for o in objs:
        if o["type"] in FIXED_TYPES:
            continue
        wall = o.get("wall")
        # 벽에 붙은 축(수직축)은 스냅하지 않아 벽면 밀착을 유지
        if wall not in ("left", "right"):
            o["x"] = round((o["x"] - MARGIN_X) / grid) * grid + MARGIN_X
        if wall not in ("top", "bottom"):
            o["y"] = round((o["y"] - MARGIN_Y) / grid) * grid + MARGIN_Y
        _clamp_into_room(o)
    return objs


def _set_canvas(aspect_ratio: float | int | None) -> None:
    """room.aspect_ratio(width÷depth)에 맞춰 방·캔버스·내부경계 상수를 재계산."""
    global ROOM_W, ROOM_H, CANVAS_W, CANVAS_H, INNER_L, INNER_T, INNER_R, INNER_B
    try:
        a = float(aspect_ratio or 0) or 1.45
    except (TypeError, ValueError):
        a = 1.45
    a = clamp(a, 0.6, 2.0)  # width : depth
    if a >= 1.0:
        ROOM_W = BASE_LONG_SIDE
        ROOM_H = round(BASE_LONG_SIDE / a)
    else:
        ROOM_H = BASE_LONG_SIDE
        ROOM_W = round(BASE_LONG_SIDE * a)
    CANVAS_W = ROOM_W + MARGIN_X * 2
    CANVAS_H = ROOM_H + MARGIN_Y * 2 + 120
    INNER_L = MARGIN_X + 18
    INNER_T = MARGIN_Y + 18
    INNER_R = MARGIN_X + ROOM_W - 18
    INNER_B = MARGIN_Y + ROOM_H - 18


def label(
    x: float,
    y: float,
    text: str,
    size: int = 15,
    anchor: str = "middle",
    weight: int = 600,
    fill: str | None = None,
) -> str:
    text_fill = (
        fill
        or STYLE["text"]
    )

    return (
        f'<text '
        f'x="{x:.1f}" '
        f'y="{y:.1f}" '
        f'text-anchor="{anchor}" '
        f'font-family="{FONT_FAMILY}" '
        f'font-size="{size}" '
        f'font-weight="{weight}" '
        f'fill="{text_fill}">'
        f'{escape(str(text))}'
        f'</text>'
    )


def grid() -> str:
    parts: list[str] = []
    for x in range(MARGIN_X, MARGIN_X + ROOM_W + 1, GRID):
        parts.append(
            f'<line x1="{x}" y1="{MARGIN_Y}" x2="{x}" y2="{MARGIN_Y + ROOM_H}" stroke="{STYLE["grid"]}"/>'
        )
    for y in range(MARGIN_Y, MARGIN_Y + ROOM_H + 1, GRID):
        parts.append(
            f'<line x1="{MARGIN_X}" y1="{y}" x2="{MARGIN_X + ROOM_W}" y2="{y}" stroke="{STYLE["grid"]}"/>'
        )
    return "\n".join(parts)


def room() -> str:
    return (
        f'<rect x="{MARGIN_X}" y="{MARGIN_Y}" width="{ROOM_W}" height="{ROOM_H}" '
        f'fill="{STYLE["floor"]}" stroke="{STYLE["wall"]}" stroke-width="10" rx="4"/>'
        f"{grid()}"
        f'<rect x="{MARGIN_X}" y="{MARGIN_Y}" width="{ROOM_W}" height="{ROOM_H}" '
        f'fill="none" stroke="{STYLE["wall"]}" stroke-width="10" rx="4"/>'
    )


def window(o: PlacedObject) -> str:
    x, y, w, h = o["x"], o["y"], o["w"], o["h"]
    return (
        f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{STYLE["glass"]}" '
        f'stroke="{STYLE["line"]}" stroke-width="2"/>'
        f'<line x1="{x + w / 2}" y1="{y}" x2="{x + w / 2}" y2="{y + h}" stroke="{STYLE["line"]}"/>'
        f'<line x1="{x + 8}" y1="{y + h / 2}" x2="{x + w - 8}" y2="{y + h / 2}" '
        f'stroke="{STYLE["thin"]}" stroke-width="1.5"/></g>'
    )


def door(o: PlacedObject) -> str:
    x, y, w, h = o["x"], o["y"], o["w"], o["h"]
    hx = x + w
    hy = y + h
    r = min(145, h)
    return (
        f'<g><rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{STYLE["floor"]}" '
        f'stroke="{STYLE["line"]}" stroke-width="2"/>'
        f'<path d="M {hx} {hy} A {r} {r} 0 0 0 {hx - r} {hy - r}" fill="none" '
        f'stroke="{STYLE["thin"]}" stroke-dasharray="6 4"/>'
        f'<line x1="{hx}" y1="{hy}" x2="{hx - r}" y2="{hy}" stroke="{STYLE["line"]}" stroke-width="3"/></g>'
    )


def generic(o: PlacedObject) -> str:
    x, y, w, h = o["x"], o["y"], o["w"], o["h"]
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
        f'fill="none" stroke="{STYLE["line"]}" stroke-width="1.5" rx="2"/>'
    )


ISO_K = 0.5          # y축 압축비(2:1 아이소메트릭)
# 타입별 높이 계수 — 바닥 짧은 변(min(w,h), 최대 120) 대비.
# 실제 가구 높이 비율에 맞춰 침대는 낮고 선반은 높게.
ISO_HEIGHT = {
    "bed": 0.34,
    "shelf": 1.60,
    "cabinet": 1.15,
    "table": 0.62,
    "low_table": 0.30,
    "desk": 0.66,
    "mirror": 1.45,
    "lamp": 1.05,
    "stool": 0.45,
    "chair": 0.80,
    "floor_chair": 0.30,
    "unknown": 0.55,
}


def _iso_pts(x: float, y: float, w: float, h: float):
    """바닥 사각형 -> 아이소메트릭 평행사변형 4점.

    45° 회전이 아니라 전단(shear)이다. 회전시키면 폭보다 깊은 가구
    (벽 수납장 90x336 등)가 위아래로 뾰족한 다이아몬드가 되어
    '눕힌 사각형'으로 읽히지 않는다. 전단은 가로 변을 수평으로
    유지하므로 어떤 비율에서도 상판이 상판으로 보인다.

    반환 순서: 뒤왼 -> 뒤오른 -> 앞오른 -> 앞왼 (시계방향)
    """
    d = h * ISO_K          # 눕힌 깊이
    # 전단량. 뒤/앞을 절반씩 밀어 원래 자리 안에 머문다.
    # 폭보다 깊이가 훨씬 큰 가구(벽거울 28x401 등)는 전단이 폭을 넘어
    # 벽을 삐져나가므로 폭의 절반으로 제한한다.
    skew = min(d / 2, w / 2)
    top_y = y + h / 2 - d / 2
    bot_y = top_y + d
    return (
        (x + skew, top_y),          # 뒤왼
        (x + w, top_y),             # 뒤오른
        (x + w - skew, bot_y),      # 앞오른
        (x, bot_y),                 # 앞왼
    )


def _poly(pts, fill: str, sw: float = 1.2) -> str:
    d = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    return (
        f'<polygon points="{d}" fill="{fill}" '
        f'stroke="{STYLE["line"]}" stroke-width="{sw}" '
        f'stroke-linejoin="round"/>'
    )


def _iso_lift(o: PlacedObject) -> float:
    """가구 높이(px). 바닥 면적이 아니라 타입으로 정해 과장을 막는다.
    세워 올린 윗면이 방 천장(위쪽 벽)을 넘지 않도록 잘라낸다."""
    base = min(o["w"], o["h"])
    lift = ISO_HEIGHT.get(o["type"], 0.5) * min(base, 120)
    # 바닥 면의 최상단 y에서 위로 lift만큼 올라가므로, 방 안에 남을 만큼만.
    top_y = o["y"] + o["h"] / 2 - (o["h"] * ISO_K) / 2
    headroom = top_y - (MARGIN_Y + 6)
    return max(6.0, min(lift, headroom))


def iso_box(o: PlacedObject) -> str:
    """아이소메트릭 직육면체. 천면·정면·측면을 톤으로 구분."""
    x, y, w, h = o["x"], o["y"], o["w"], o["h"]
    lift = _iso_lift(o)
    bl, br, fr, fl = _iso_pts(x, y, w, h)

    def up(p):
        return (p[0], p[1] - lift)

    # 보이는 면만: 정면(앞), 측면(오른), 천면
    front = _poly([fl, fr, up(fr), up(fl)], STYLE["iso_left"])
    side = _poly([fr, br, up(br), up(fr)], STYLE["iso_right"])
    top = _poly([up(bl), up(br), up(fr), up(fl)], STYLE["iso_top"])
    return f"<g>{front}{side}{top}</g>"


def iso_flat(o: PlacedObject) -> str:
    """러그처럼 높이가 없는 것 — 눕힌 면만."""
    x, y, w, h = o["x"], o["y"], o["w"], o["h"]
    pts = _iso_pts(x, y, w, h)
    d = " ".join(f"{px:.1f},{py:.1f}" for px, py in pts)
    return (
        f'<polygon points="{d}" fill="none" '
        f'stroke="{STYLE["rug"]}" stroke-width="1.6" '
        f'stroke-dasharray="6 4"/>'
    )


def iso_round(o: PlacedObject) -> str:
    """의자·스툴 — 눕힌 타원 좌판 + 기둥 + 바닥 접지.

    단, 바닥이 정사각형에서 크게 벗어난 좌석(소파를 chair로 인식한
    328x102 등)은 타원으로 그리면 납작한 팬케이크가 되므로
    직육면체로 대신 그린다.
    """
    x, y, w, h = o["x"], o["y"], o["w"], o["h"]
    if max(w, h) > min(w, h) * 1.8:
        return iso_box(o)
    d = h * ISO_K
    cx = x + w / 2
    cy = y + h / 2
    rx = w / 2
    ry = d / 2
    lift = _iso_lift(o)
    return (
        f'<g>'
        f'<ellipse cx="{cx}" cy="{cy}" rx="{rx * 0.5}" ry="{ry * 0.5}" '
        f'fill="none" stroke="{STYLE["thin"]}" stroke-width="1" '
        f'stroke-dasharray="2 3"/>'
        f'<line x1="{cx}" y1="{cy}" x2="{cx}" y2="{cy - lift}" '
        f'stroke="{STYLE["line"]}" stroke-width="1.5"/>'
        f'<ellipse cx="{cx}" cy="{cy - lift}" rx="{rx}" ry="{ry}" '
        f'fill="{STYLE["iso_top"]}" stroke="{STYLE["line"]}" stroke-width="1.2"/>'
        f'</g>'
    )


ISO_DRAWERS: dict[str, Any] = {
    "bed": iso_box,
    "rug": iso_flat,
    "shelf": iso_box,
    "cabinet": iso_box,
    "table": iso_box,
    "low_table": iso_box,
    "desk": iso_box,
    "mirror": iso_box,
    "lamp": iso_round,
    "stool": iso_round,
    "chair": iso_round,
    "floor_chair": iso_round,
    "window": window,
    "door": door,
    # 새 타입은 전용 그림 없이 기존 프리미티브를 재사용한다
    "sofa": iso_box,
    "wardrobe": iso_box,
    "dresser": iso_box,
    "bench": iso_box,
    "tv": iso_box,
    "fridge": iso_box,
    "aircon": iso_box,
    "washer": iso_box,
    "vanity": iso_box,
    "nightstand": iso_box,
    "desk_chair": iso_round,
    "curtain": iso_flat,
}


# ──────────────────────────────────────────────────────
# 스타일 4: 아이콘 — 가구를 실제 크기 사각형으로 그리지 않고,
# 자리 중앙에 일정한 크기의 심볼 하나로 표시한다.
# 배치 관계만 빠르게 읽히는 대신 크기 정보는 버린다.
# ──────────────────────────────────────────────────────
DRAWERS: dict[str, Any] = ISO_DRAWERS


def selected_product_outline(
    obj: PlacedObject,
) -> str:
    """
    선택한 추천 가구 주변에
    점선 강조 테두리를 표시한다.
    """

    if (
        obj.get("source")
        != "selected_product"
    ):
        return ""

    x = float(obj["x"]) - 7
    y = float(obj["y"]) - 7

    width = (
        float(obj["w"])
        + 14
    )

    height = (
        float(obj["h"])
        + 14
    )

    return (
        f'<rect '
        f'x="{x}" '
        f'y="{y}" '
        f'width="{width}" '
        f'height="{height}" '
        f'rx="12" '
        f'fill="none" '
        f'stroke="{STYLE["accent"]}" '
        f'stroke-width="4" '
        f'stroke-dasharray="10 6"/>'
    )


def selected_product_marker(
    obj: PlacedObject,
) -> str:
    """
    추천 상품 카드와 연결되는
    번호 배지를 평면도에 표시한다.
    """

    if (
        obj.get("source")
        != "selected_product"
    ):
        return ""

    marker = obj.get(
        "product_marker"
    )

    if marker in (
        None,
        "",
    ):
        return ""

    x = (
        float(obj["x"])
        + float(obj["w"])
        - 5
    )

    y = (
        float(obj["y"])
        + 5
    )

    radius = 18

    return (
        f'<g class="product-marker">'

        f'<circle '
        f'cx="{x}" '
        f'cy="{y}" '
        f'r="{radius + 4}" '
        f'fill="#ffffff" '
        f'opacity="0.96"/>'

        f'<circle '
        f'cx="{x}" '
        f'cy="{y}" '
        f'r="{radius}" '
        f'fill="{STYLE["accent"]}" '
        f'stroke="#ffffff" '
        f'stroke-width="2"/>'

        f'<text '
        f'x="{x}" '
        f'y="{y + 6}" '
        f'text-anchor="middle" '
        f'font-family="{FONT_FAMILY}" '
        f'font-size="17" '
        f'font-weight="800" '
        f'fill="#ffffff">'
        f'{escape(str(marker))}'
        f'</text>'

        f'</g>'
    )


def draw_obj(
    obj: PlacedObject,
) -> str:
    drawer = DRAWERS.get(
        obj["type"],
        generic,
    )

    object_svg = drawer(
        obj
    )

    source = escape(
        str(
            obj.get("source")
            or "detected"
        )
    )

    # 드래그 이동용 식별자·플래그. 벽·바닥 고정 요소는 드래그 대상에서 제외.
    idx = obj.get("idx")
    draggable = obj["type"] not in FIXED_TYPES
    data_attrs = f' data-source="{source}"'
    if idx is not None:
        data_attrs += f' data-index="{idx}"'
    if draggable:
        data_attrs += ' data-draggable="1"'

    return (
        f'<g{data_attrs}>'
        f'{selected_product_outline(obj)}'
        f'{object_svg}'
        f'{selected_product_marker(obj)}'
        f'</g>'
    )


def dimensions() -> str:
    """
    실측 숫자가 없는 평면도에서는
    가로·세로 방향만 표시한다.
    """

    x1 = MARGIN_X
    y1 = MARGIN_Y

    x2 = MARGIN_X + ROOM_W
    y2 = MARGIN_Y + ROOM_H

    top_y = MARGIN_Y - 38
    left_x = MARGIN_X - 50

    width_text = label(
        (x1 + x2) / 2,
        top_y - 8,
        "가로 방향",
        13,
        anchor="middle",
        weight=500,
        fill=STYLE["muted"],
    )

    depth_center_y = (y1 + y2) / 2

    return (
        f'<line '
        f'x1="{x1}" '
        f'y1="{top_y}" '
        f'x2="{x2}" '
        f'y2="{top_y}" '
        f'stroke="{STYLE["thin"]}" '
        f'stroke-width="1.5"/>'

        f'<circle '
        f'cx="{x1}" '
        f'cy="{top_y}" '
        f'r="3.5" '
        f'fill="{STYLE["thin"]}"/>'

        f'<circle '
        f'cx="{x2}" '
        f'cy="{top_y}" '
        f'r="3.5" '
        f'fill="{STYLE["thin"]}"/>'

        f'{width_text}'

        f'<line '
        f'x1="{left_x}" '
        f'y1="{y1}" '
        f'x2="{left_x}" '
        f'y2="{y2}" '
        f'stroke="{STYLE["thin"]}" '
        f'stroke-width="1.5"/>'

        f'<circle '
        f'cx="{left_x}" '
        f'cy="{y1}" '
        f'r="3.5" '
        f'fill="{STYLE["thin"]}"/>'

        f'<circle '
        f'cx="{left_x}" '
        f'cy="{y2}" '
        f'r="3.5" '
        f'fill="{STYLE["thin"]}"/>'

        f'<text '
        f'x="{left_x - 10}" '
        f'y="{depth_center_y}" '
        f'text-anchor="middle" '
        f'font-family="{FONT_FAMILY}" '
        f'font-size="13" '
        f'font-weight="500" '
        f'fill="{STYLE["muted"]}" '
        f'transform="rotate('
        f'-90 '
        f'{left_x - 10} '
        f'{depth_center_y}'
        f')">'
        f'세로 방향'
        f'</text>'
    )


def fit_walls(objs: list[PlacedObject], edge: int = 40, pad: int = 12) -> list[PlacedObject]:
    """한 벽에 붙은 구조 가구의 폭 합이 벽 길이를 넘으면 그 줄 가구를 비율 축소.
    (병합으로도 안 들어가는 과다검출을 한 줄에 맞춰 겹침을 원천 차단.)"""
    sides = (
        ("top", "w", ROOM_W - 2 * edge),
        ("bottom", "w", ROOM_W - 2 * edge),
        ("left", "h", ROOM_H - 2 * edge),
        ("right", "h", ROOM_H - 2 * edge),
    )
    for side, dim, avail in sides:
        row = [o for o in objs if o.get("wall") == side and o["type"] in WALL_PULL_TYPES]
        if len(row) < 2:
            continue
        needed = sum(o[dim] for o in row) + pad * (len(row) - 1)
        if needed <= avail:
            continue
        factor = max(0.5, avail / needed)  # 최대 50%까지만 축소
        for o in row:
            o["w"] *= factor
            o["h"] *= factor
    return objs


def resolve_placement(
    layout: LayoutDict,
) -> tuple[LayoutDict, list[PlacedObject], dict[str, int]]:
    """layout → 최종 배치. SVG가 실제로 그리는 좌표를 그대로 돌려준다.

    layout 의 x·y·w·h 는 그대로 그려지지 않는다. 표준 크기 대체(_gemini_size),
    벽 맞춤, 자동 패킹, 겹침 해소, 격자 스냅을 거친 결과가 최종 좌표다.
    3D 뷰어도 이 함수를 써서 SVG와 같은 배치를 그린다(따로 계산하면 어긋난다).

    반환: (정규화된 layout, 배치된 객체들(픽셀 좌표), 캔버스 크기)
    """
    layout = coerce_layout_v3(
        layout
    )

    room_data = (
        layout.get("room")
        or {}
    )

    _set_canvas(
        room_data.get(
            "aspect_ratio"
        )
    )

    layout["objects"] = (
        postprocess_layout_objects(
            layout.get(
                "objects",
                [],
            )
        )
    )

    normalized = normalize_objects(
        layout
    )

    snapped = snap_chairs_to_table(
        normalized
    )

    sized = fit_walls(
        snapped
    )

    packed = auto_pack(
        sized
    )

    first_resolved = (
        resolve_overlaps(
            packed
        )
    )

    aligned = grid_align(
        first_resolved
    )

    objects = resolve_overlaps(
        aligned
    )

    # ROOM_W·ROOM_H 는 _set_canvas 가 방 비율에 따라 바꾸는 전역이다.
    # 호출자가 오래된 값을 읽지 않도록 지금 값을 함께 돌려준다.
    canvas = {
        "room_w": ROOM_W,
        "room_h": ROOM_H,
        "margin_x": MARGIN_X,
        "margin_y": MARGIN_Y,
    }

    return layout, objects, canvas


def render_svg(
    layout: LayoutDict,
    title: str = "AI 인테리어 평면도",
) -> str:
    layout, objects, _canvas = (
        resolve_placement(
            layout
        )
    )

    room_data = (
        layout.get("room")
        or {}
    )

    object_svg = "\n".join(
        draw_obj(obj)
        for obj in objects
    )

    selected_objects = [
        obj
        for obj in objects
        if (
            obj.get("source")
            == "selected_product"
        )
    ]

    if selected_objects:
        subtitle = (
            "번호가 표시된 가구는 "
            "아래 추천 상품 카드와 연결됩니다."
        )

    else:
        subtitle = (
            "방 구조와 기존 가구 배치를 "
            "한눈에 확인해보세요."
        )

    footer_y = (
        MARGIN_Y
        + ROOM_H
        + 56
    )

    footer_parts: list[str] = [
        (
            f'<rect '
            f'x="{MARGIN_X}" '
            f'y="{footer_y - 28}" '
            f'width="18" '
            f'height="18" '
            f'rx="5" '
            f'fill="{STYLE["existing"]}" '
            f'stroke="{STYLE["line"]}" '
            f'stroke-width="1.5"/>'
        ),

        label(
            MARGIN_X + 28,
            footer_y - 14,
            "기존 가구",
            13,
            anchor="start",
            weight=500,
            fill=STYLE["muted"],
        ),

        (
            f'<rect '
            f'x="{MARGIN_X + 125}" '
            f'y="{footer_y - 28}" '
            f'width="18" '
            f'height="18" '
            f'rx="5" '
            f'fill="{STYLE["recommend"]}" '
            f'stroke="{STYLE["accent"]}" '
            f'stroke-width="2" '
            f'stroke-dasharray="5 3"/>'
        ),

        label(
            MARGIN_X + 153,
            footer_y - 14,
            "선택한 추천 가구",
            13,
            anchor="start",
            weight=500,
            fill=STYLE["muted"],
        ),
    ]

    if selected_objects:
        marker_x = MARGIN_X

        marker_y = (
            footer_y
            + 22
        )

        for index, obj in enumerate(
            selected_objects[:6]
        ):
            marker = obj.get(
                "product_marker",
                index + 1,
            )

            item_label = display_label(
                obj.get(
                    "type",
                    "unknown",
                ),
                obj.get("label"),
                source=(
                    "selected_product"
                ),
            )

            footer_parts.append(
                (
                    f'<circle '
                    f'cx="{marker_x + 13}" '
                    f'cy="{marker_y}" '
                    f'r="13" '
                    f'fill="{STYLE["accent"]}"/>'
                )
            )

            footer_parts.append(
                (
                    f'<text '
                    f'x="{marker_x + 13}" '
                    f'y="{marker_y + 5}" '
                    f'text-anchor="middle" '
                    f'font-family="{FONT_FAMILY}" '
                    f'font-size="13" '
                    f'font-weight="800" '
                    f'fill="#ffffff">'
                    f'{escape(str(marker))}'
                    f'</text>'
                )
            )

            footer_parts.append(
                label(
                    marker_x + 34,
                    marker_y + 5,
                    item_label,
                    13,
                    anchor="start",
                    weight=600,
                )
            )

            marker_x += 145

            if (
                marker_x
                > (
                    MARGIN_X
                    + ROOM_W
                    - 120
                )
            ):
                marker_x = MARGIN_X
                marker_y += 34

    footer_svg = "".join(
        footer_parts
    )

    return (
        f'<svg '
        f'xmlns="http://www.w3.org/2000/svg" '
        f'width="{CANVAS_W}" '
        f'height="{CANVAS_H}" '
        f'viewBox="0 0 {CANVAS_W} {CANVAS_H}" '
        f'role="img" '
        f'aria-label="{escape(title)}">'

        f'<!-- renderer-version:'
        f'{RENDERER_VERSION} -->'

        f'<rect '
        f'width="100%" '
        f'height="100%" '
        f'fill="#f7f3ee"/>'

        f'<rect '
        f'x="24" '
        f'y="20" '
        f'width="{CANVAS_W - 48}" '
        f'height="{CANVAS_H - 40}" '
        f'rx="24" '
        f'fill="#ffffff"/>'

        f'<text '
        f'x="42" '
        f'y="48" '
        f'font-family="{FONT_FAMILY}" '
        f'font-size="28" '
        f'font-weight="800" '
        f'fill="{STYLE["text"]}">'
        f'{escape(title)}'
        f'</text>'

        f'<text '
        f'x="42" '
        f'y="76" '
        f'font-family="{FONT_FAMILY}" '
        f'font-size="14" '
        f'font-weight="500" '
        f'fill="{STYLE["muted"]}">'
        f'{escape(subtitle)}'
        f'</text>'

        f'{dimensions()}'
        f'{room()}'
        f'{object_svg}'
        f'{footer_svg}'

        f'</svg>'
    )


def save_svg(
    layout: LayoutDict,
    path: str | Path,
    title: str = "AI 인테리어 평면도",
) -> Path:
    output_path = Path(
        path
    )

    output_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    svg_text = render_svg(
        layout,
        title=title,
    )

    output_path.write_text(
        svg_text,
        encoding="utf-8",
    )

    return output_path