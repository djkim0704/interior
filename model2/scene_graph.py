"""표준 공간 데이터(Scene Graph v1). 2D 평면도와 3D 화면은 모두 이것만 읽는다.

기존에는 같은 방을 다섯 가지 JSON으로 표현했다(분석 JSON, legacy layout,
픽셀 배치, 3D 씬 등). 변환을 거칠 때마다 회전·관계·id가 조금씩 사라졌고,
2D는 Gemini가 다시 그리고 3D는 별도 배치 엔진이 다시 계산해서 둘이 어긋났다.
이제 좌표·크기·회전은 여기 한 곳에만 있고, 나머지는 여기서 계산한 '뷰'다.

좌표 규약 (floorplan_3d.py, floorplan_3d.js와 같다)
  - 원점은 방 좌상단, cx는 가로(→), cy는 깊이(↓) 방향. 단위는 미터.
  - w_m, d_m은 가구 자신의 기준이다. w_m은 등받이(뒷면)와 나란한 폭,
    d_m은 앞뒤 깊이. 평면도에서 차지하는 가로·세로가 아니다.
  - rotation_deg는 위에서 본 시계방향 각. 0이면 뒷면이 위쪽(top) 벽을 향한다.

legacy 필드
  기존 라우트(유지·제거, 상품 추가, 편집 저장)는 아직 정규화 좌표
  x, y, w, h, wall을 읽는다. 그래서 객체마다 이 값들을 미터 값에서 계산해
  같이 저장한다. 미터 값이 원본이고, legacy 값은 sync_legacy()가 매번 다시 쓴다.
  미터 값이 없는 객체(예: 기존 라우트가 끼워 넣은 상품)만 legacy 값에서 복원한다.
"""
from __future__ import annotations

import copy
import math
import statistics
import time
from typing import Any

SCHEMA = "scene_graph_v1"

DEFAULT_LONG_SIDE_M = 4.0
DEFAULT_CEILING_M = 2.4
MIN_ASPECT, MAX_ASPECT = 0.35, 2.5
MIN_ROOM_SIDE_M, MAX_ROOM_SIDE_M = 1.5, 12.0
WALL_GAP_M = 0.04  # 벽걸이 객체가 벽에서 떨어진 거리. floorplan_3d.js와 같아야 한다

WALLS = ("top", "right", "bottom", "left")
WALL_ROTATION = {"top": 0.0, "right": 90.0, "bottom": 180.0, "left": 270.0}

# 분석기가 쓰는 자유 카테고리 → 2D·3D가 그릴 줄 아는 타입.
# 기존 TYPE_MAP에 sofa·wardrobe·tv 등이 빠져 3D에서 상자로 그려지던 문제를 메운다.
CATEGORY_TO_TYPE = {
    "bed": "bed",
    "bunk_bed": "bed",
    "sofa_bed": "sofa",
    "sofa": "sofa",
    "couch": "sofa",
    "armchair": "sofa",
    "loveseat": "sofa",
    "desk": "desk",
    "table": "table",
    "dining_table": "table",
    "coffee_table": "low_table",
    "low_table": "low_table",
    "side_table": "nightstand",
    "end_table": "nightstand",
    "nightstand": "nightstand",
    "bedside_table": "nightstand",
    "chair": "chair",
    "dining_chair": "chair",
    "desk_chair": "desk_chair",
    "office_chair": "desk_chair",
    "gaming_chair": "desk_chair",
    "floor_chair": "floor_chair",
    "stool": "stool",
    "ottoman": "stool",
    "pouf": "stool",
    "bench": "bench",
    "shelf": "shelf",
    "bookshelf": "shelf",
    "bookcase": "shelf",
    "cabinet": "cabinet",
    "sideboard": "cabinet",
    "tv_stand": "cabinet",
    "tv_cabinet": "cabinet",
    "drawer": "dresser",
    "chest_of_drawers": "dresser",
    "dresser": "dresser",
    "wardrobe": "wardrobe",
    "closet": "wardrobe",
    "vanity": "vanity",
    "dressing_table": "vanity",
    "rug": "rug",
    "carpet": "rug",
    "mat": "rug",
    "mirror": "mirror",
    "lamp": "lamp",
    "floor_lamp": "lamp",
    "table_lamp": "lamp",
    "plant": "plant",
    "potted_plant": "plant",
    "tv": "tv",
    "television": "tv",
    "monitor": "decor",
    "fridge": "fridge",
    "refrigerator": "fridge",
    "washer": "washer",
    "washing_machine": "washer",
    "aircon": "aircon",
    "air_conditioner": "aircon",
    "curtain": "curtain",
    "door": "door",
    "window": "window",
}

# 타입별 실제 높이·띄움 높이. floorplan_3d.TYPE_PRESETS와 같은 값을 쓴다.
# (3D 프리셋을 그대로 import하면 순환 참조가 생겨 여기서 기본값만 둔다)
WALL_MOUNTED_TYPES = {"door", "window", "mirror", "tv", "aircon", "curtain"}
# 등을 벽에 붙이는 게 자연스러운 가구. 이 타입만 벽 방향으로 회전시킨다.
WALL_FACING_TYPES = {
    "bed",
    "sofa",
    "desk",
    "shelf",
    "cabinet",
    "dresser",
    "wardrobe",
    "vanity",
    "nightstand",
    "bench",
    "fridge",
    "washer",
}

# 실제 크기를 아는 기준 객체. 방 실측이 없을 때 축척을 추정하는 데 쓴다(항목 12).
#   side: 평면도에서 이 길이가 긴 변(long)인지 짧은 변(short)인지.
#         분석기가 주는 가구 방향은 자주 틀려서, 방향과 무관한 긴 변·짧은 변을 쓴다.
#   length_m: 국내 기성 가구·건축 표준 치수
#   weight: 치수 편차가 작을수록 크게 둔다. 침대 길이(1.95~2.1m)가 가장 안정적이다.
REFERENCE_SIZES = {
    "bed": {"side": "long", "length_m": 2.0, "weight": 1.0},
    "door": {"side": "long", "length_m": 0.9, "weight": 0.7},
    "desk": {"side": "short", "length_m": 0.6, "weight": 0.4},
    "wardrobe": {"side": "short", "length_m": 0.6, "weight": 0.5},
    "sofa": {"side": "short", "length_m": 0.9, "weight": 0.3},
    "fridge": {"side": "long", "length_m": 0.75, "weight": 0.4},
    "washer": {"side": "long", "length_m": 0.6, "weight": 0.4},
}
# 1인용·수납형처럼 표준 치수에서 크게 벗어나는 카테고리는 기준에서 뺀다
REFERENCE_EXCLUDED_CATEGORIES = {"armchair", "loveseat", "sofa_bed", "bunk_bed", "closet"}
# 실측 없이 추정한 방의 긴 변 허용 범위(m). 사진 한 장 분석의 크기 오차가 커서
# 주거 공간에서 흔한 범위로 묶는다.
ESTIMATE_LONG_SIDE_RANGE = (2.6, 7.0)
# 기준 가구 추정을 일반적인 방 크기(긴 변 DEFAULT_LONG_SIDE_M) 쪽으로 당기는 강도.
# 기준 가구 가중치 합이 이 값보다 작으면 사전값 쪽이 더 크게 반영된다.
# 침대 하나(가중치 1.0 × 신뢰도)면 추정이 우세하고, 책상 하나(0.4 × 신뢰도)면
# 사전값에 가깝게 나온다. 정답 데이터가 생기면 이 값을 다시 맞춘다.
PRIOR_WEIGHT = 0.6
# 벽걸이 객체의 최대 두께(m). 3D가 벽에서 WALL_GAP_M 떨어진 곳에 중심을 두므로
# 이보다 두꺼우면 벽을 뚫고 나간다.
WALL_MOUNTED_MAX_DEPTH_M = 0.08

# 상품처럼 크기가 비어 들어온 객체에 쓰는 표준 크기(폭, 깊이, m)
DEFAULT_SIZES = {
    "bed": (1.5, 2.0),
    "sofa": (1.9, 0.9),
    "desk": (1.2, 0.6),
    "table": (1.2, 0.8),
    "low_table": (0.9, 0.5),
    "chair": (0.5, 0.5),
    "desk_chair": (0.6, 0.6),
    "floor_chair": (0.55, 0.6),
    "stool": (0.4, 0.4),
    "bench": (1.2, 0.4),
    "shelf": (0.8, 0.3),
    "cabinet": (0.8, 0.45),
    "dresser": (0.8, 0.45),
    "wardrobe": (1.2, 0.6),
    "vanity": (0.8, 0.45),
    "nightstand": (0.45, 0.4),
    "rug": (1.6, 1.2),
    "mirror": (0.5, 0.05),
    "lamp": (0.35, 0.35),
    "plant": (0.4, 0.4),
    "tv": (1.2, 0.08),
    "fridge": (0.75, 0.7),
    "washer": (0.6, 0.6),
    "aircon": (0.9, 0.25),
    "curtain": (1.6, 0.12),
    "door": (0.9, 0.1),
    "window": (1.2, 0.1),
    "decor": (0.3, 0.3),
    "unknown": (0.5, 0.5),
}


# ---------------------------------------------------------------- 기본 도구

def _number(value: Any, default: float, low: float | None = None, high: float | None = None) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    if low is not None:
        result = max(low, result)
    if high is not None:
        result = min(high, result)
    return result


def _positive(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result <= 0:
        return None
    return result


def object_type(category: Any) -> str:
    key = str(category or "").strip().lower().replace(" ", "_").replace("-", "_")
    if key in CATEGORY_TO_TYPE:
        return CATEGORY_TO_TYPE[key]
    # "single_bed", "wooden_desk"처럼 수식어가 붙은 경우 끝 단어로 다시 찾는다
    for part in reversed(key.split("_")):
        if part in CATEGORY_TO_TYPE:
            return CATEGORY_TO_TYPE[part]
    return "decor" if key in {"speaker", "decor", "decoration", "vase", "clock", "frame", "basket"} else "unknown"


def _is_quarter_turn(rotation: float) -> bool:
    return 45 <= (rotation % 180) <= 135


def plan_extent(obj: dict[str, Any]) -> tuple[float, float]:
    """평면도에서 차지하는 외접 박스의 가로·세로(m)."""
    angle = math.radians(float(obj.get("rotation_deg") or 0.0))
    w, d = float(obj["w_m"]), float(obj["d_m"])
    return (
        abs(w * math.cos(angle)) + abs(d * math.sin(angle)),
        abs(w * math.sin(angle)) + abs(d * math.cos(angle)),
    )


def local_from_plan(plan_w: float, plan_d: float, rotation: float) -> tuple[float, float]:
    """평면도 가로·세로 → 가구 기준 폭·깊이. 90°/270°면 두 값이 바뀐다."""
    if _is_quarter_turn(rotation):
        return plan_d, plan_w
    return plan_w, plan_d


def _nearest_wall(cx: float, cy: float, width: float, depth: float) -> str:
    distances = {"top": cy, "bottom": depth - cy, "left": cx, "right": width - cx}
    return min(distances, key=distances.get)


# ---------------------------------------------------------------- 방 축척

def calibrate_room(
    aspect: float,
    objects_normalized: list[dict[str, Any]],
    *,
    width_m: float | None = None,
    depth_m: float | None = None,
) -> dict[str, Any]:
    """방의 실제 가로·세로를 정한다 (항목 11·12).

    우선순위
      1) 사용자가 가로·세로를 모두 입력 → 그대로 쓴다 ("user")
      2) 한 변만 입력 → 분석한 가로÷세로 비율로 나머지 변을 계산 ("user_one_side")
      3) 입력 없음 → 크기를 아는 기준 가구(침대 길이 2.0m, 문 폭 0.9m 등)의
         정규화 길이로 축척을 추정 ("reference_objects")
      4) 기준 가구도 없음 → 긴 변 4.0m 가정 ("default")
    """
    aspect = _number(aspect, 0.75, MIN_ASPECT, MAX_ASPECT)
    width_m, depth_m = _positive(width_m), _positive(depth_m)
    references: list[dict[str, Any]] = []

    if width_m and depth_m:
        source = "user"
    elif width_m or depth_m:
        source = "user_one_side"
        if width_m:
            depth_m = width_m / aspect
        else:
            width_m = depth_m * aspect
    else:
        # 방 가로를 W라 두면 세로는 W/aspect. 기준 가구의 정규화 길이 × 방 길이 =
        # 표준 길이 이므로, 가구마다 W 후보를 하나씩 얻고 가중 중앙값을 쓴다.
        estimates: list[tuple[float, float]] = []
        for obj in objects_normalized:
            spec = REFERENCE_SIZES.get(str(obj.get("type")))
            if not spec or str(obj.get("category") or "") in REFERENCE_EXCLUDED_CATEGORIES:
                continue
            nw, nd = float(obj["w"]), float(obj["h"])  # 평면도 가로·세로(정규화)
            if nw <= 0 or nd <= 0:
                continue
            # 방 가로를 W라 하면 이 가구의 평면도 크기는 (nw·W, nd·W/aspect).
            # 긴 변이 표준 길이 L이면 W = L / max(nw, nd/aspect),
            # 짧은 변이 L이면 W = L / min(nw, nd/aspect).
            x_unit, y_unit = nw, nd / aspect
            span = max(x_unit, y_unit) if spec["side"] == "long" else min(x_unit, y_unit)
            estimate = spec["length_m"] / span
            weight = spec["weight"] * _number(obj.get("confidence"), 0.5, 0.05, 1.0)
            estimates.append((estimate, weight))
            references.append(
                {
                    "id": obj.get("scene_id") or obj.get("id"),
                    "type": obj.get("type"),
                    "standard_m": spec["length_m"],
                    "width_estimate_m": round(estimate, 3),
                    "weight": round(weight, 3),
                }
            )
        plausible = [
            (value, weight)
            for value, weight in estimates
            if MIN_ROOM_SIDE_M <= value <= MAX_ROOM_SIDE_M
            and MIN_ROOM_SIDE_M <= value / aspect <= MAX_ROOM_SIDE_M
        ]
        if plausible:
            source = "reference_objects"
            estimate = _weighted_median(plausible)
            # 사진 한 장의 가구 크기는 오차가 커서, 기준이 약할 때는 사전값과
            # 로그 공간에서 가중 평균한다(곱셈 오차라 로그 평균이 맞다)
            prior = DEFAULT_LONG_SIDE_M * (1.0 if aspect >= 1.0 else aspect)
            total = sum(weight for _, weight in plausible)
            width_m = math.exp(
                (total * math.log(estimate) + PRIOR_WEIGHT * math.log(prior))
                / (total + PRIOR_WEIGHT)
            )
            depth_m = width_m / aspect
            low, high = ESTIMATE_LONG_SIDE_RANGE
            long_side = max(width_m, depth_m)
            if not low <= long_side <= high:
                factor = min(high, max(low, long_side)) / long_side
                width_m, depth_m = width_m * factor, depth_m * factor
                source = "reference_objects_clamped"
        else:
            source = "default"
            if aspect >= 1.0:
                width_m, depth_m = DEFAULT_LONG_SIDE_M, DEFAULT_LONG_SIDE_M / aspect
            else:
                width_m, depth_m = DEFAULT_LONG_SIDE_M * aspect, DEFAULT_LONG_SIDE_M

    return {
        "width_m": round(float(width_m), 3),
        "depth_m": round(float(depth_m), 3),
        "aspect_ratio": round(float(width_m) / float(depth_m), 4),
        "scale_source": source,
        "scale_references": references,
        # 사용자가 두 변을 다 준 경우만 '실측'이다. 화면에 추정 배지를 띄우는 기준
        "estimated": source not in {"user"},
    }


def _weighted_median(values: list[tuple[float, float]]) -> float:
    ordered = sorted(values)
    total = sum(weight for _, weight in ordered)
    running = 0.0
    for value, weight in ordered:
        running += weight
        if running >= total / 2:
            return value
    return statistics.median(v for v, _ in ordered)


# ---------------------------------------------------------------- 생성

def from_analysis(
    scene: dict[str, Any],
    *,
    width_m: float | None = None,
    depth_m: float | None = None,
    ceiling_m: float | None = None,
    solve: bool = True,
) -> dict[str, Any]:
    """normalize_layout 결과(0~1 정규화 좌표) → Scene Graph.

    solve=True면 충돌·벽·동선 보정(placement_solver)까지 거친다. 보정 전 상태가
    필요한 실험(보정 효과 측정)에서만 False로 둔다.
    """
    room_in = scene.get("room") or {}
    aspect = _number(room_in.get("aspect_ratio_width_to_depth"), 0.75, MIN_ASPECT, MAX_ASPECT)
    if width_m and depth_m:
        aspect = _number(float(width_m) / float(depth_m), aspect, MIN_ASPECT, MAX_ASPECT)

    staged: list[dict[str, Any]] = []
    for index, raw in enumerate(scene.get("objects") or []):
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category") or "unknown").lower()
        kind = object_type(category)
        anchors = [a for a in raw.get("wall_anchors") or [] if a in WALLS]
        gemini_rotation = _number(raw.get("rotation_deg"), 0.0) % 360
        nx = _number(raw.get("x"), 0.5, 0.0, 1.0)
        ny = _number(raw.get("y"), 0.5, 0.0, 1.0)
        nw = _number(raw.get("width"), 0.12, 0.01, 1.0)
        nd = _number(raw.get("depth"), 0.12, 0.01, 1.0)

        # 분석기는 width·depth를 평면도상 가로·세로로, 회전은 거의 0으로 준다.
        # 회전이 0이면 가구가 바라보는 방향을 붙은 벽으로 정하고, 평면도에서
        # 차지하는 영역(nw×nd)은 그대로 유지한다. 그래야 분석 결과와 2D·3D가
        # 같은 자리를 차지한다.
        if abs(gemini_rotation) > 1e-6:
            rotation = gemini_rotation
            plan_given = False
        elif kind in WALL_MOUNTED_TYPES or kind in WALL_FACING_TYPES:
            wall = anchors[0] if anchors else None
            rotation = WALL_ROTATION.get(wall, 0.0) if wall else 0.0
            plan_given = True
        else:
            rotation = 0.0
            plan_given = True

        staged.append(
            {
                "index": index,
                "raw": raw,
                "category": category,
                "type": kind,
                "anchors": anchors,
                "rotation": rotation,
                "plan_given": plan_given,
                "nx": nx,
                "ny": ny,
                "nw": nw,
                "nd": nd,
            }
        )

    # 축척 추정은 평면도상 정규화 크기로 한다(정규화 단위가 방 크기에 비례하므로)
    reference_view = []
    for item in staged:
        if item["plan_given"]:
            plan_w, plan_h = item["nw"], item["nd"]
        else:
            angle = math.radians(item["rotation"])
            plan_w = abs(item["nw"] * math.cos(angle)) + abs(item["nd"] * math.sin(angle))
            plan_h = abs(item["nw"] * math.sin(angle)) + abs(item["nd"] * math.cos(angle))
        reference_view.append(
            {
                "id": item["raw"].get("id"),
                "type": item["type"],
                "category": item["category"],
                "rotation_deg": item["rotation"],
                "w": plan_w,
                "h": plan_h,
                "confidence": item["raw"].get("confidence"),
            }
        )
    room = calibrate_room(aspect, reference_view, width_m=width_m, depth_m=depth_m)
    W, D = room["width_m"], room["depth_m"]

    objects = []
    for item, view in zip(staged, reference_view):
        raw = item["raw"]
        plan_w, plan_d = view["w"] * W, view["h"] * D
        w_m, d_m = local_from_plan(plan_w, plan_d, item["rotation"])
        obj = {
            "id": str(raw.get("id") or f"{item['category']}_{item['index'] + 1}"),
            "category": item["category"],
            "type": item["type"],
            "label": str(raw.get("label_ko") or item["category"]),
            "cx": item["nx"] * W,
            "cy": item["ny"] * D,
            "w_m": w_m,
            "d_m": d_m,
            "rotation_deg": item["rotation"],
            "wall": item["anchors"][0] if item["anchors"] else "none",
            "wall_anchors": item["anchors"],
            "relations": list(raw.get("relations") or []),
            "confidence": _number(raw.get("confidence"), 0.5, 0.0, 1.0),
            "source": "ai",
            "source_index": item["index"],
            "color": raw.get("color"),
            "material": raw.get("material"),
            "pattern": raw.get("pattern"),
        }
        objects.append(obj)

    graph = {
        "schema": SCHEMA,
        "renderer_source": "scene_graph",
        "room": {
            **room,
            "ceiling_m": round(_positive(ceiling_m) or DEFAULT_CEILING_M, 3),
            "shape": room_in.get("shape") or "rectangle",
            "floor_color": room_in.get("floor_color"),
            "wall_color": room_in.get("wall_color"),
            "summary_ko": room_in.get("summary_ko"),
        },
        "objects": objects,
        "uncertainties": list(scene.get("uncertainties") or []),
        "solver_adjustments": list(scene.get("solver_adjustments") or []),
        "history": [{"ts": round(time.time(), 3), "source": "ai", "action": "analyze"}],
    }
    _place_wall_mounted(graph)
    if solve:
        from .placement_solver import solve as solve_placement

        solve_placement(graph)
    for obj in graph["objects"]:
        obj["edit_origin"] = _origin(obj)
    return sync_legacy(graph)


def _origin(obj: dict[str, Any]) -> dict[str, float]:
    # 기존 SVG 편집기는 '처음 그린 상태' 기준의 누적 배율·각도를 보낸다.
    # 그 기준값을 남겨 둬야 편집을 여러 번 저장해도 배율이 겹쳐 곱해지지 않는다.
    return {
        key: round(float(obj[key]), 4)
        for key in ("cx", "cy", "w_m", "d_m", "rotation_deg")
    }


def _place_wall_mounted(graph: dict[str, Any]) -> None:
    """문·창 같은 벽걸이 객체를 벽면에 붙인다.

    3D는 벽걸이 객체의 좌표를 무시하고 벽에 붙여 그린다. 2D도 같은 자리에
    그려야 하므로 Scene Graph 단계에서 미리 벽에 붙여 둔다.
    """
    W, D = graph["room"]["width_m"], graph["room"]["depth_m"]
    for obj in graph["objects"]:
        if obj["type"] not in WALL_MOUNTED_TYPES:
            continue
        wall = obj.get("wall")
        # 사용자가 직접 옮긴 벽걸이(거울을 다른 벽으로 옮기는 등)는 놓은 자리에서
        # 가장 가까운 벽에 붙인다. 원래 벽으로 되돌리면 사용자의 의도를 무시하게 된다.
        if wall not in WALLS or obj.get("source") == "user":
            wall = _nearest_wall(obj["cx"], obj["cy"], W, D)
            obj["wall"] = wall
        # 분석기가 벽에 붙였다면서 긴 변을 벽과 수직으로 주는 경우가 있다
        # (TV가 벽을 뚫고 나감). 벽걸이는 긴 변이 벽을 따라가고 얇아야 하므로
        # 방향과 무관하게 긴 변 = 폭, 짧은 변(최대 8cm) = 두께로 다시 정한다.
        long_side = max(float(obj["w_m"]), float(obj["d_m"]))
        short_side = min(float(obj["w_m"]), float(obj["d_m"]))
        obj["w_m"] = long_side
        obj["d_m"] = min(short_side, WALL_MOUNTED_MAX_DEPTH_M)
        obj["rotation_deg"] = WALL_ROTATION[wall]
        if wall == "top":
            obj["cy"] = WALL_GAP_M
        elif wall == "bottom":
            obj["cy"] = D - WALL_GAP_M
        elif wall == "left":
            obj["cx"] = WALL_GAP_M
        else:
            obj["cx"] = W - WALL_GAP_M


# ---------------------------------------------------------------- legacy 뷰

def sync_legacy(graph: dict[str, Any]) -> dict[str, Any]:
    """미터 값 → 기존 라우트가 읽는 정규화 필드(x, y, w, h, wall, scene_id)."""
    room = graph["room"]
    W, D = float(room["width_m"]), float(room["depth_m"])
    room["aspect_ratio"] = round(W / D, 4)
    for index, obj in enumerate(graph.get("objects") or []):
        plan_w, plan_d = plan_extent(obj)
        for key in ("cx", "cy", "w_m", "d_m"):
            obj[key] = round(float(obj[key]), 4)
        obj["rotation_deg"] = round(float(obj["rotation_deg"]) % 360, 3)
        obj["x"] = round(min(1.0, max(0.0, obj["cx"] / W)), 4)
        obj["y"] = round(min(1.0, max(0.0, obj["cy"] / D)), 4)
        obj["w"] = round(min(1.0, max(0.01, plan_w / W)), 4)
        obj["h"] = round(min(1.0, max(0.01, plan_d / D)), 4)
        obj.setdefault("scene_id", obj["id"])
        obj["scene_id"] = obj["id"]
        obj.setdefault("source_index", index)
        obj.setdefault("wall", "none")
    return graph


def is_scene_graph(layout: Any) -> bool:
    return isinstance(layout, dict) and layout.get("schema") == SCHEMA


def ensure(layout: dict[str, Any], *, solve_new: bool = True) -> dict[str, Any]:
    """기존 라우트를 거친 Scene Graph를 다시 일관된 상태로 맞춘다.

    상품 추가처럼 legacy 필드만 채워 넣은 객체는 미터 값을 legacy에서 복원한다.
    크기가 0이면 타입별 표준 크기를 쓴다. solve_new=True면 새로 들어온 객체만
    빈자리로 옮긴다(기존 가구는 그대로 둔다). 고정 슬롯(PURCHASE_POSITIONS)에
    놓인 상품이 다른 가구와 겹치던 문제를 여기서 푼다.
    """
    graph = copy.deepcopy(layout)
    new_ids: list[str] = []
    room = graph["room"]
    W, D = float(room["width_m"]), float(room["depth_m"])
    used_ids = {str(o.get("id")) for o in graph.get("objects") or [] if o.get("id")}
    for index, obj in enumerate(graph.get("objects") or []):
        if all(key in obj for key in ("cx", "cy", "w_m", "d_m", "rotation_deg")):
            continue
        kind = object_type(obj.get("type") or obj.get("category"))
        obj["type"] = kind
        obj.setdefault("category", kind)
        wall = str(obj.get("wall") or "none")
        rotation = WALL_ROTATION.get(wall, 0.0) if kind in WALL_FACING_TYPES | WALL_MOUNTED_TYPES else 0.0
        plan_w = _number(obj.get("w"), 0.0, 0.0, 1.0) * W
        plan_d = _number(obj.get("h"), 0.0, 0.0, 1.0) * D
        if plan_w <= 0.01 or plan_d <= 0.01:
            w_m, d_m = DEFAULT_SIZES.get(kind, DEFAULT_SIZES["unknown"])
        else:
            w_m, d_m = local_from_plan(plan_w, plan_d, rotation)
        obj.update(
            cx=_number(obj.get("x"), 0.5, 0.0, 1.0) * W,
            cy=_number(obj.get("y"), 0.5, 0.0, 1.0) * D,
            w_m=w_m,
            d_m=d_m,
            rotation_deg=rotation,
        )
        if not obj.get("id"):
            if obj.get("source") == "selected_product":
                base = f"product_{obj.get('product_marker') or index}"
            else:
                base = f"{kind}_{index}"
            candidate = base
            suffix = 2
            while candidate in used_ids:
                candidate = f"{base}_{suffix}"
                suffix += 1
            obj["id"] = candidate
            used_ids.add(candidate)
        obj.setdefault("label", kind)
        obj.setdefault("confidence", 1.0 if obj.get("source") == "selected_product" else 0.5)
        new_ids.append(str(obj["id"]))
    _place_wall_mounted(graph)
    if new_ids and solve_new:
        from .placement_solver import solve as solve_placement

        solve_placement(graph, movable_ids=new_ids)
    for obj in graph.get("objects") or []:
        if str(obj.get("id")) in new_ids or "edit_origin" not in obj:
            obj["edit_origin"] = _origin(obj)
    return sync_legacy(graph)


def rescale_room(layout: dict[str, Any], width_m: float, depth_m: float, *, source: str = "user") -> dict[str, Any]:
    """방 실측이 나중에 들어왔을 때 비율을 유지한 채 미터 값을 다시 계산한다."""
    graph = ensure(layout)
    room = graph["room"]
    sx = float(width_m) / float(room["width_m"])
    sy = float(depth_m) / float(room["depth_m"])
    for obj in graph["objects"]:
        plan_w, plan_d = plan_extent(obj)
        obj["cx"] *= sx
        obj["cy"] *= sy
        obj["w_m"], obj["d_m"] = local_from_plan(plan_w * sx, plan_d * sy, float(obj["rotation_deg"]))
        obj["edit_origin"] = _origin(obj)
    room.update(
        width_m=round(float(width_m), 3),
        depth_m=round(float(depth_m), 3),
        scale_source=source,
        estimated=source != "user",
    )
    _place_wall_mounted(graph)
    graph.setdefault("history", []).append(
        {"ts": round(time.time(), 3), "source": "user", "action": "rescale_room"}
    )
    return sync_legacy(graph)


def apply_svg_edit(
    layout: dict[str, Any],
    scene_id: str,
    *,
    center_px: tuple[float, float],
    floor_box: tuple[float, float, float, float],
    scale: float,
    angle: float,
    origin: dict[str, float] | None = None,
) -> bool:
    """기존 SVG 편집기의 결과(누적 이동·배율·회전)를 미터 값에 반영한다.

    배율과 각도는 처음 그린 상태(edit_origin) 기준 누적값이라 원점 값에
    한 번만 적용한다. 반영했으면 True.
    """
    target = next((o for o in layout.get("objects") or [] if str(o.get("id")) == scene_id), None)
    if target is None:
        return False
    room = layout["room"]
    floor_x, floor_y, floor_w, floor_h = floor_box
    origin = origin or target.get("edit_origin") or _origin(target)
    cx = min(1.0, max(0.0, (center_px[0] - floor_x) / floor_w)) * float(room["width_m"])
    cy = min(1.0, max(0.0, (center_px[1] - floor_y) / floor_h)) * float(room["depth_m"])
    w_m = float(origin["w_m"]) * scale
    d_m = float(origin["d_m"]) * scale
    rotation = (float(origin["rotation_deg"]) + angle) % 360
    # 편집기는 손대지 않은 가구도 함께 보낸다. 실제로 바뀐 것만 '사용자 수정'으로
    # 표시해야 보정기가 나머지 가구를 비켜 줄 수 있다. 1cm·1°는 픽셀 반올림 수준이다.
    changed = (
        math.dist((cx, cy), (float(target["cx"]), float(target["cy"]))) > 0.01
        or abs(w_m - float(target["w_m"])) > 0.01
        or abs(d_m - float(target["d_m"])) > 0.01
        or min(abs(rotation - float(target["rotation_deg"])) % 360, 360 - abs(rotation - float(target["rotation_deg"])) % 360) > 1.0
    )
    if not changed:
        return False
    target.update(cx=cx, cy=cy, w_m=w_m, d_m=d_m, rotation_deg=rotation)
    if target.get("source") == "ai":
        target["source"] = "user"
    target["user_rotation"] = angle
    target["user_scale"] = scale
    return True


def append_history(layout: dict[str, Any], source: str, action: str, **extra: Any) -> None:
    layout.setdefault("history", []).append(
        {"ts": round(time.time(), 3), "source": source, "action": action, **extra}
    )
