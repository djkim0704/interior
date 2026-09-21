# 평면도 layout JSON → 3D 배치 확인용 씬 데이터 변환
#
# 웹 평면도(model2_gemini_svg)의 layout 스키마는 정규화 좌표만 담고 있다.
#   {"room": {"aspect_ratio": 0.72, ...},
#    "objects": [{"type": "bed", "x": .23, "y": .28, "w": .42, "h": .52, "wall": "top"}]}
#
# 3D로 그리려면 (1) 미터 단위 절대 치수 (2) 가구 높이 (3) 회전각이 필요한데
# layout에는 셋 다 없다. 이 모듈이 그 셋을 채워서 브라우저(three.js)가 바로
# 쓸 수 있는 씬 데이터로 바꾼다. 렌더링 자체는 하지 않는다.
#
# 좌표 규약
#   - cx, cy: 방 안에서의 중심 위치 (미터, 좌상단 원점, cy는 깊이 방향)
#   - w_m, d_m: 가로·깊이 (미터).  height_m: 높이,  base_m: 바닥으로부터 띄운 높이
#   - rotation_deg: 위에서 본 회전각. 0 = 등을 위쪽(top) 벽에 붙인 상태.
#                   top=0, right=90, bottom=180, left=270
from __future__ import annotations

from typing import Any

# 방 치수를 모를 때 긴 변에 가정하는 길이(m)와 천장 높이(m)
DEFAULT_LONG_SIDE_M = 4.0
DEFAULT_CEILING_M = 2.4

# aspect_ratio(가로÷세로) 허용 범위 — normalize_layout 과 동일하게 맞춘다
MIN_ASPECT, MAX_ASPECT = 0.35, 2.5

# 가구 정규화 크기 하한/상한
MIN_SIZE, MAX_SIZE = 0.02, 1.0

# wall 값 → 위에서 본 회전각
WALL_ROTATION = {
    "top": 0.0,
    "right": 90.0,
    "bottom": 180.0,
    "left": 270.0,
}

# 타입별 3D 프리셋
#   height_m     : 높이(m)
#   base_m       : 바닥에서 띄운 높이(m). 벽걸이/창문처럼 떠 있는 것만 0 이 아니다
#   color        : 주 색상. three.js 쪽에서 이 색을 밝기 조절해 면별 톤을 만든다
#   wall_mounted : True 면 벽면에 붙여 배치한다(바닥에 놓지 않는다)
TYPE_PRESETS: dict[str, dict[str, Any]] = {
    "bed":         {"height_m": 0.50, "base_m": 0.00, "color": "#b0655f", "wall_mounted": False},
    "desk":        {"height_m": 0.74, "base_m": 0.00, "color": "#9c7248", "wall_mounted": False},
    "table":       {"height_m": 0.74, "base_m": 0.00, "color": "#a67c4e", "wall_mounted": False},
    "low_table":   {"height_m": 0.40, "base_m": 0.00, "color": "#ae8354", "wall_mounted": False},
    "shelf":       {"height_m": 1.80, "base_m": 0.00, "color": "#8f6a44", "wall_mounted": False},
    "cabinet":     {"height_m": 1.05, "base_m": 0.00, "color": "#8a6a44", "wall_mounted": False},
    "chair":       {"height_m": 0.88, "base_m": 0.00, "color": "#7f7269", "wall_mounted": False},
    "floor_chair": {"height_m": 0.42, "base_m": 0.00, "color": "#87796d", "wall_mounted": False},
    "stool":       {"height_m": 0.45, "base_m": 0.00, "color": "#8d7f72", "wall_mounted": False},
    "rug":         {"height_m": 0.02, "base_m": 0.00, "color": "#c2b49c", "wall_mounted": False},
    "mirror":      {"height_m": 1.20, "base_m": 0.80, "color": "#cfd6da", "wall_mounted": True},
    "lamp":        {"height_m": 1.55, "base_m": 0.00, "color": "#d8cdb4", "wall_mounted": False},
    "plant":       {"height_m": 0.95, "base_m": 0.00, "color": "#5f7d4f", "wall_mounted": False},
    "door":        {"height_m": 2.05, "base_m": 0.00, "color": "#e2d9c8", "wall_mounted": True},
    "window":      {"height_m": 1.30, "base_m": 0.85, "color": "#bcd6e0", "wall_mounted": True},
    "unknown":     {"height_m": 0.55, "base_m": 0.00, "color": "#9a9186", "wall_mounted": False},
    # 구매 가능 종류 (backend PURCHASE_LABELS 와 짝을 맞춘다)
    "sofa":        {"height_m": 0.85, "base_m": 0.00, "color": "#7d6b5d", "wall_mounted": False},
    "wardrobe":    {"height_m": 2.00, "base_m": 0.00, "color": "#8a6a44", "wall_mounted": False},
    "dresser":     {"height_m": 0.80, "base_m": 0.00, "color": "#96754d", "wall_mounted": False},
    "bench":       {"height_m": 0.45, "base_m": 0.00, "color": "#8d7f6a", "wall_mounted": False},
    # 생활가전·가구
    "tv":          {"height_m": 0.65, "base_m": 0.85, "color": "#2c2c30", "wall_mounted": True},
    "fridge":      {"height_m": 1.80, "base_m": 0.00, "color": "#d5d8da", "wall_mounted": False},
    "aircon":      {"height_m": 0.32, "base_m": 1.90, "color": "#eef1f2", "wall_mounted": True},
    "washer":      {"height_m": 0.85, "base_m": 0.00, "color": "#e1e4e6", "wall_mounted": False},
    "vanity":      {"height_m": 0.75, "base_m": 0.00, "color": "#a98a63", "wall_mounted": False},
    "nightstand":  {"height_m": 0.55, "base_m": 0.00, "color": "#96754d", "wall_mounted": False},
    "desk_chair":  {"height_m": 0.95, "base_m": 0.00, "color": "#4a4a50", "wall_mounted": False},
    "curtain":     {"height_m": 1.95, "base_m": 0.30, "color": "#cdbfae", "wall_mounted": True},
}

FALLBACK_PRESET = TYPE_PRESETS["unknown"]

# 방 마감 기본색
DEFAULT_FLOOR_COLOR = "#b08a5e"
DEFAULT_WALL_COLOR = "#efe9dd"


def _number(value: Any, default: float, low: float, high: float) -> float:
    # layout 값이 None·문자열·범위 밖이어도 렌더가 깨지지 않게 방어한다
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return max(low, min(high, result))


def _positive(value: Any) -> float | None:
    # 사용자가 입력한 방 치수처럼 "있으면 쓰고 없으면 추정" 인 값 판별
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result <= 0:
        return None
    return result


def resolve_room(
    layout: dict[str, Any] | None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """방의 절대 치수를 결정한다. 사용자 입력 > layout 기록 > aspect_ratio 추정 순."""
    room_in = (layout or {}).get("room") or {}
    plan_in = plan or {}

    # 1순위: 업로드 단계에서 사용자가 입력한 실측치
    width = _positive(plan_in.get("width_m"))
    depth = _positive(plan_in.get("depth_m"))

    # 2순위: layout 에 기록된 치수
    if width is None or depth is None:
        width = width or _positive(room_in.get("width_m"))
        depth = depth or _positive(room_in.get("depth_m"))

    estimated = False

    # 3순위: 비율만 알고 있으니 긴 변을 가정해서 만든다
    if width is None or depth is None:
        aspect = _number(
            room_in.get("aspect_ratio"),
            0.75,
            MIN_ASPECT,
            MAX_ASPECT,
        )
        if aspect >= 1.0:
            width = DEFAULT_LONG_SIDE_M
            depth = DEFAULT_LONG_SIDE_M / aspect
        else:
            depth = DEFAULT_LONG_SIDE_M
            width = DEFAULT_LONG_SIDE_M * aspect
        estimated = True

    ceiling = _positive(plan_in.get("ceiling_m")) or DEFAULT_CEILING_M

    return {
        "width_m": round(width, 3),
        "depth_m": round(depth, 3),
        "ceiling_m": round(ceiling, 3),
        # estimated=True 면 화면에 "치수 추정값" 배지를 띄워 사용자를 오해시키지 않는다
        "estimated": estimated,
        "floor_color": str(room_in.get("floor_color") or DEFAULT_FLOOR_COLOR),
        "wall_color": str(room_in.get("wall_color") or DEFAULT_WALL_COLOR),
    }


def _infer_wall(cx: float, cy: float) -> str:
    # wall 이 비어 있으면 중심 위치로 가장 가까운 벽을 고른다
    distances = {
        "top": cy,
        "bottom": 1.0 - cy,
        "left": cx,
        "right": 1.0 - cx,
    }
    return min(distances, key=distances.get)


def convert_object(
    obj: dict[str, Any],
    room: dict[str, Any],
    index: int = 0,
) -> dict[str, Any] | None:
    """layout 가구 1개 → 3D 씬 오브젝트. 타입을 알 수 없으면 unknown 프리셋을 쓴다."""
    if not isinstance(obj, dict):
        return None

    obj_type = str(obj.get("type") or "unknown").lower()
    preset = TYPE_PRESETS.get(obj_type, FALLBACK_PRESET)

    cx = _number(obj.get("x"), 0.5, 0.0, 1.0)
    cy = _number(obj.get("y"), 0.5, 0.0, 1.0)
    w = _number(obj.get("w"), 0.15, MIN_SIZE, MAX_SIZE)
    d = _number(obj.get("h"), 0.15, MIN_SIZE, MAX_SIZE)

    wall_raw = str(obj.get("wall") or "").lower()
    if wall_raw in WALL_ROTATION:
        wall = wall_raw
    elif wall_raw == "none":
        # 벽에서 떨어진 가구 — 회전 없이 방 가운데 방향 그대로 둔다
        wall = "none"
    else:
        wall = _infer_wall(cx, cy)

    rotation = WALL_ROTATION.get(wall, 0.0)

    width_m = room["width_m"]
    depth_m = room["depth_m"]

    return {
        "id": str(obj.get("scene_id") or f"{obj_type}_{index}"),
        "type": obj_type,
        "label": str(obj.get("label") or obj_type),
        # 미터 단위 중심 좌표 (좌상단 원점)
        "cx": round(cx * width_m, 4),
        "cy": round(cy * depth_m, 4),
        "w_m": round(w * width_m, 4),
        "d_m": round(d * depth_m, 4),
        "height_m": preset["height_m"],
        "base_m": preset["base_m"],
        "rotation_deg": rotation,
        "wall": wall,
        "wall_mounted": preset["wall_mounted"],
        "color": preset["color"],
    }


def convert_placed(
    obj: dict[str, Any],
    room: dict[str, Any],
    canvas: dict[str, int],
    index: int = 0,
) -> dict[str, Any] | None:
    """SVG가 실제로 그린 배치(픽셀) → 3D 씬 오브젝트.

    rule_based_svg 는 캔버스 픽셀 좌표로 배치를 확정한다(여백 포함).
    여백을 빼고 방 크기로 나눠 0~1 비율로 되돌린 뒤 미터로 환산한다.
    """
    if not isinstance(obj, dict):
        return None

    room_w = canvas["room_w"] or 1
    room_h = canvas["room_h"] or 1

    obj_type = str(obj.get("type") or "unknown").lower()
    preset = TYPE_PRESETS.get(obj_type, FALLBACK_PRESET)

    # 픽셀 → 방 기준 0~1 비율
    fx = _number(
        (float(obj.get("cx", 0)) - canvas["margin_x"]) / room_w, 0.5, 0.0, 1.0
    )
    fy = _number(
        (float(obj.get("cy", 0)) - canvas["margin_y"]) / room_h, 0.5, 0.0, 1.0
    )
    fw = _number(float(obj.get("w", 0)) / room_w, 0.15, MIN_SIZE, MAX_SIZE)
    fd = _number(float(obj.get("h", 0)) / room_h, 0.15, MIN_SIZE, MAX_SIZE)

    wall_raw = str(obj.get("wall") or "").lower()
    if wall_raw in WALL_ROTATION:
        wall = wall_raw
    elif wall_raw == "none":
        wall = "none"
    else:
        wall = _infer_wall(fx, fy)

    width_m = room["width_m"]
    depth_m = room["depth_m"]

    # 구매로 추가된 가구는 3D에서도 구분해서 보여준다
    source = str(obj.get("source") or "")
    is_product = source == "selected_product"
    marker = obj.get("product_marker")

    return {
        "id": str(obj.get("scene_id") or f"{obj_type}_{obj.get('idx', index)}"),
        "type": obj_type,
        "label": str(obj.get("label") or obj_type),
        "cx": round(fx * width_m, 4),
        "cy": round(fy * depth_m, 4),
        "w_m": round(fw * width_m, 4),
        "d_m": round(fd * depth_m, 4),
        "height_m": preset["height_m"],
        "base_m": preset["base_m"],
        "rotation_deg": WALL_ROTATION.get(wall, 0.0),
        "wall": wall,
        "wall_mounted": preset["wall_mounted"],
        "color": preset["color"],
        "is_product": is_product,
        "marker": marker if isinstance(marker, int) else None,
        "product_title": (
            str(obj["product_title"]) if obj.get("product_title") else None
        ),
    }


def _placed_objects(
    layout: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]] | None:
    """SVG와 동일한 배치를 얻는다. 실패하면 None(호출부가 raw layout 으로 폴백)."""
    import copy

    from mood_pipeline.rule_based_svg import resolve_placement

    # resolve_placement 는 layout["objects"] 를 덮어쓰므로 사본을 넘긴다
    _, objects, canvas = resolve_placement(copy.deepcopy(layout))
    return objects, canvas


def build_scene(
    layout: dict[str, Any] | None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """layout JSON + 방 치수 → three.js 가 바로 쓰는 씬 데이터.

    좌표는 SVG 평면도와 같아야 한다. layout 의 x·y·w·h 를 그대로 쓰면
    표준 크기 대체·벽 맞춤·겹침 해소·격자 스냅이 빠져서 2D와 어긋나므로,
    rule_based_svg 의 배치 파이프라인을 그대로 통과시킨 결과를 쓴다.
    """
    room = resolve_room(layout, plan)

    raw_objects = (layout or {}).get("objects")
    if not isinstance(raw_objects, list):
        raw_objects = []

    objects: list[dict[str, Any]] = []
    placement = "svg"

    try:
        resolved = _placed_objects(layout) if raw_objects else None
    except Exception:
        # 렌더러를 못 불러와도 3D 자체는 떠야 한다
        resolved = None

    if resolved is not None:
        placed, canvas = resolved
        for index, obj in enumerate(placed):
            converted = convert_placed(obj, room, canvas, index)
            if converted is not None:
                objects.append(converted)
    else:
        placement = "raw"
        for index, obj in enumerate(raw_objects):
            converted = convert_object(obj, room, index)
            if converted is not None:
                objects.append(converted)

    return {
        "room": room,
        "objects": objects,
        # svg = 2D 평면도와 동일한 배치, raw = layout 값 그대로(폴백)
        "placement": placement,
        # 지원하는 타입 목록 — 프론트에서 전용 지오메트리 유무를 판단할 때 쓴다
        "known_types": sorted(TYPE_PRESETS),
    }
