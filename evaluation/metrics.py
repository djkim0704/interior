"""배치 품질 지표. 모든 좌표는 미터, 방 좌상단 원점, x=가로, y=깊이 방향.

지표 정의는 docs/metrics/README.md에 같은 내용으로 정리했다. 특허 명세서의
실험 결과로 쓰이므로, 정의를 바꾸면 기준값(baseline)도 다시 측정해야 한다.
"""
from __future__ import annotations

import math
from collections import deque
from typing import Any, Iterable

Point = tuple[float, float]
Polygon = list[Point]

# 바닥에 놓이지 않거나 겹쳐도 정상인 것. layout_solver의 예외 목록과 같은 취지다.
NON_BLOCKING_TYPES = {
    "rug",
    "door",
    "window",
    "mirror",
    "curtain",
    "aircon",
    "tv",
    "lamp",
    "decor",
}
# 의자는 책상·식탁 아래로 일부 들어가는 게 정상 배치다
TUCKABLE = {
    frozenset({"chair", "desk"}),
    frozenset({"chair", "table"}),
    frozenset({"desk_chair", "desk"}),
    frozenset({"stool", "table"}),
    frozenset({"stool", "desk"}),
    frozenset({"floor_chair", "low_table"}),
}

# 한쪽이라도 이 면적(m²)을 넘게 겹쳐야 충돌로 본다. 렌더 반올림 수준의 접촉은 제외.
COLLISION_MIN_AREA_M2 = 0.01
COLLISION_MIN_FRACTION = 0.05
WALL_MIN_OUTSIDE_M2 = 0.005
# 사람이 지나가는 데 필요한 통로 폭의 절반(m). 0.30이면 폭 60cm 통로.
DEFAULT_CLEARANCE_M = 0.30
GRID_M = 0.05

# floorplan_3d.js의 BUILDERS에 전용 형태가 있는 타입. 이 밖의 타입은 상자로 그려진다.
BUILDER_TYPES = {
    "bed", "desk", "table", "low_table", "shelf", "cabinet", "chair", "floor_chair",
    "stool", "rug", "lamp", "plant", "mirror", "door", "window", "sofa", "wardrobe",
    "dresser", "bench", "tv", "fridge", "aircon", "washer", "vanity", "nightstand",
    "desk_chair", "curtain", "decor",
}


# ---------------------------------------------------------------- 기하 기본

def footprint(obj: dict[str, Any]) -> Polygon:
    """3D 씬 객체(cx, cy, w_m, d_m, rotation_deg)의 바닥 사각형."""
    cx, cy = float(obj["cx"]), float(obj["cy"])
    w, d = float(obj["w_m"]), float(obj["d_m"])
    angle = math.radians(float(obj.get("rotation_deg") or 0.0))
    cos, sin = math.cos(angle), math.sin(angle)
    corners = [(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)]
    return [(cx + x * cos - y * sin, cy + x * sin + y * cos) for x, y in corners]


def polygon_area(poly: Polygon) -> float:
    if len(poly) < 3:
        return 0.0
    total = 0.0
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        total += x1 * y2 - x2 * y1
    return abs(total) / 2


def _clip(subject: Polygon, clip: Polygon) -> Polygon:
    # Sutherland–Hodgman. 가구 바닥면은 모두 볼록 사각형이라 이걸로 충분하다.
    def orientation(poly: Polygon) -> float:
        return sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]))

    if orientation(clip) < 0:
        clip = list(reversed(clip))
    output = list(subject)
    for (ax, ay), (bx, by) in zip(clip, clip[1:] + clip[:1]):
        if not output:
            break

        def inside(p: Point) -> bool:
            return (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax) >= -1e-12

        def intersect(p: Point, q: Point) -> Point:
            dx1, dy1 = q[0] - p[0], q[1] - p[1]
            dx2, dy2 = bx - ax, by - ay
            denom = dx1 * dy2 - dy1 * dx2
            if abs(denom) < 1e-15:
                return q
            t = ((ax - p[0]) * dy2 - (ay - p[1]) * dx2) / denom
            return p[0] + t * dx1, p[1] + t * dy1

        current, output = output, []
        for index, point in enumerate(current):
            previous = current[index - 1]
            if inside(point):
                if not inside(previous):
                    output.append(intersect(previous, point))
                output.append(point)
            elif inside(previous):
                output.append(intersect(previous, point))
    return output


def overlap_area(first: Polygon, second: Polygon) -> float:
    return polygon_area(_clip(first, second))


def _room_polygon(room: dict[str, Any]) -> Polygon:
    w, d = float(room["width_m"]), float(room["depth_m"])
    return [(0.0, 0.0), (w, 0.0), (w, d), (0.0, d)]


def _blocking(obj: dict[str, Any]) -> bool:
    if obj.get("wall_mounted"):
        return False
    return str(obj.get("type") or "") not in NON_BLOCKING_TYPES


# ---------------------------------------------------------------- 충돌·벽

def collision_metrics(objects: list[dict[str, Any]]) -> dict[str, Any]:
    items = [o for o in objects if _blocking(o)]
    polys = [footprint(o) for o in items]
    pairs = 0
    colliding: list[dict[str, Any]] = []
    involved: set[int] = set()
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            types = frozenset({str(items[i].get("type")), str(items[j].get("type"))})
            if types in TUCKABLE:
                continue
            pairs += 1
            area = overlap_area(polys[i], polys[j])
            smaller = min(polygon_area(polys[i]), polygon_area(polys[j])) or 1e-9
            if area > COLLISION_MIN_AREA_M2 and area / smaller > COLLISION_MIN_FRACTION:
                involved.update({i, j})
                colliding.append(
                    {
                        "a": items[i].get("id"),
                        "b": items[j].get("id"),
                        "overlap_m2": round(area, 4),
                        "overlap_ratio": round(area / smaller, 3),
                    }
                )
    return {
        "blocking_objects": len(items),
        "pairs_checked": pairs,
        "colliding_pairs": len(colliding),
        "pair_collision_rate": _ratio(len(colliding), pairs),
        # 특허 표에 쓰는 "가구 충돌률"은 객체 기준이다: 하나라도 겹친 가구의 비율
        "object_collision_rate": _ratio(len(involved), len(items)),
        "overlap_total_m2": round(sum(c["overlap_m2"] for c in colliding), 4),
        "collisions": colliding,
    }


def wall_metrics(objects: list[dict[str, Any]], room: dict[str, Any]) -> dict[str, Any]:
    room_poly = _room_polygon(room)
    items = [o for o in objects if not o.get("wall_mounted") and str(o.get("type")) not in {"door", "window"}]
    violations = []
    for obj in items:
        poly = footprint(obj)
        total = polygon_area(poly)
        outside = total - overlap_area(poly, room_poly)
        if outside > WALL_MIN_OUTSIDE_M2:
            violations.append({"id": obj.get("id"), "outside_m2": round(outside, 4)})
    return {
        "floor_objects": len(items),
        "wall_penetrations": len(violations),
        "wall_penetration_rate": _ratio(len(violations), len(items)),
        "violations": violations,
    }


# ---------------------------------------------------------------- 동선

def _grid(room: dict[str, Any], step: float) -> tuple[int, int]:
    return max(1, int(round(float(room["width_m"]) / step))), max(1, int(round(float(room["depth_m"]) / step)))


def _point_in_polygon(x: float, y: float, poly: Polygon) -> bool:
    inside = False
    for (x1, y1), (x2, y2) in zip(poly, poly[1:] + poly[:1]):
        if (y1 > y) != (y2 > y):
            if x < (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-12) + x1:
                inside = not inside
    return inside


def _distance_to_segment(px: float, py: float, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - a[0]) * dx + (py - a[1]) * dy) / length))
    return math.hypot(px - (a[0] + t * dx), py - (a[1] + t * dy))


def _distance_to_polygon(px: float, py: float, poly: Polygon) -> float:
    if _point_in_polygon(px, py, poly):
        return 0.0
    return min(_distance_to_segment(px, py, a, b) for a, b in zip(poly, poly[1:] + poly[:1]))


def _door_entry(door: dict[str, Any], room: dict[str, Any], clearance: float) -> Point:
    """문 중심에서 방 안쪽으로 clearance만큼 들어온 지점. 사람이 서는 첫 위치다."""
    w, d = float(room["width_m"]), float(room["depth_m"])
    x = min(max(float(door["cx"]), 0.0), w)
    y = min(max(float(door["cy"]), 0.0), d)
    wall = str(door.get("wall") or "")
    if wall not in {"top", "bottom", "left", "right"}:
        wall = min({"top": y, "bottom": d - y, "left": x, "right": w - x}.items(), key=lambda kv: kv[1])[0]
    inset = clearance + GRID_M
    if wall == "top":
        y = inset
    elif wall == "bottom":
        y = d - inset
    elif wall == "left":
        x = inset
    else:
        x = w - inset
    return x, y


def walkway_metrics(
    objects: list[dict[str, Any]],
    room: dict[str, Any],
    *,
    clearance: float = DEFAULT_CLEARANCE_M,
    step: float = GRID_M,
) -> dict[str, Any]:
    """문에서 출발해 폭 2×clearance 통로로 갈 수 있는 바닥과 가구를 센다.

    점유 격자에서 '사람 중심이 설 수 있는 칸'은 벽과 가구에서 clearance 이상
    떨어진 칸이다. 문 앞 칸에서 BFS로 도달 가능한 칸을 구한다.
    """
    cols, rows = _grid(room, step)
    w, d = float(room["width_m"]), float(room["depth_m"])
    blockers = [footprint(o) for o in objects if _blocking(o)]
    doors = [o for o in objects if str(o.get("type")) == "door"]

    def center(col: int, row: int) -> Point:
        return (col + 0.5) * step, (row + 0.5) * step

    free = [[False] * cols for _ in range(rows)]
    floor_cells = 0  # 가구가 차지하지 않은 바닥 칸(벽 여유 무시)
    for row in range(rows):
        for col in range(cols):
            x, y = center(col, row)
            occupied = any(_point_in_polygon(x, y, p) for p in blockers)
            if not occupied:
                floor_cells += 1
            if occupied:
                continue
            if min(x, y, w - x, d - y) < clearance:
                continue
            if any(_distance_to_polygon(x, y, p) < clearance for p in blockers):
                continue
            free[row][col] = True

    def cell_of(point: Point) -> tuple[int, int]:
        return (
            min(cols - 1, max(0, int(point[0] / step))),
            min(rows - 1, max(0, int(point[1] / step))),
        )

    def bfs(start: tuple[int, int]) -> set[tuple[int, int]]:
        seen = {start}
        queue = deque([start])
        while queue:
            col, row = queue.popleft()
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nc, nr = col + dc, row + dr
                if 0 <= nc < cols and 0 <= nr < rows and free[nr][nc] and (nc, nr) not in seen:
                    seen.add((nc, nr))
                    queue.append((nc, nr))
        return seen

    starts = []
    for door in doors:
        col, row = cell_of(_door_entry(door, room, clearance))
        starts.append({"id": door.get("id"), "cell": (col, row), "blocked": not free[row][col]})

    reachable: set[tuple[int, int]] = set()
    door_source = "door"
    if starts:
        for start in starts:
            if not start["blocked"]:
                reachable |= bfs(start["cell"])
    else:
        # 문이 검출되지 않으면 가장 큰 자유 영역을 기준으로 삼는다(지표에 표시)
        door_source = "largest_free_region"
        remaining = {(c, r) for r in range(rows) for c in range(cols) if free[r][c]}
        while remaining:
            region = bfs(next(iter(remaining)))
            remaining -= region
            if len(region) > len(reachable):
                reachable = region

    # 문끼리 서로 오갈 수 있는지(문이 둘 이상일 때만 의미가 있다)
    open_starts = [s for s in starts if not s["blocked"]]
    doors_connected = None
    if len(starts) >= 2:
        doors_connected = bool(open_starts) and len(open_starts) == len(starts) and all(
            s["cell"] in bfs(open_starts[0]["cell"]) for s in open_starts
        )

    # 가구마다 '앞에 설 수 있는가': 바닥면에서 clearance+0.1m 이내에 도달 칸이 있는지
    reach_margin = clearance + 0.10
    accessible = []
    furniture = [o for o in objects if _blocking(o)]
    for obj in furniture:
        poly = footprint(obj)
        ok = any(
            _distance_to_polygon(*center(c, r), poly) <= reach_margin
            for c, r in reachable
        )
        accessible.append({"id": obj.get("id"), "accessible": ok})

    return {
        "clearance_m": clearance,
        "grid_m": step,
        "door_count": len(doors),
        "door_source": door_source,
        "doors_blocked": sum(1 for s in starts if s["blocked"]),
        "doors_connected": doors_connected,
        # 가구가 없는 바닥 중 문에서 걸어서 닿는 비율
        "reachable_floor_ratio": _ratio(len(reachable), floor_cells),
        "furniture_access_rate": _ratio(sum(1 for a in accessible if a["accessible"]), len(accessible)),
        "inaccessible": [a["id"] for a in accessible if not a["accessible"]],
    }


# ---------------------------------------------------------------- 2D ↔ 3D

def sync_metrics(
    boxes_2d_m: dict[str, tuple[float, float, float, float]],
    objects_3d: list[dict[str, Any]],
    expected_ids: Iterable[str],
) -> dict[str, Any]:
    """2D(SVG에 실제로 그려진 영역)와 3D 배치의 차이.

    boxes_2d_m은 미터로 환산한 SVG 외접 박스. 3D는 회전을 반영한 바닥면의
    외접 박스를 쓴다. SVG 쪽도 외접 박스라 같은 기준으로 비교된다.
    """
    by_id = {str(o.get("id")): o for o in objects_3d}
    expected = [str(i) for i in expected_ids]
    rows = []
    for object_id in expected:
        box_2d = boxes_2d_m.get(object_id)
        obj = by_id.get(object_id)
        if box_2d is None or obj is None:
            continue
        poly = footprint(obj)
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        box_3d = (min(xs), min(ys), max(xs), max(ys))
        c2 = ((box_2d[0] + box_2d[2]) / 2, (box_2d[1] + box_2d[3]) / 2)
        c3 = ((box_3d[0] + box_3d[2]) / 2, (box_3d[1] + box_3d[3]) / 2)
        rows.append(
            {
                "id": object_id,
                "type": obj.get("type"),
                "center_error_m": round(math.dist(c2, c3), 4),
                "width_error_m": round(abs((box_2d[2] - box_2d[0]) - (box_3d[2] - box_3d[0])), 4),
                "depth_error_m": round(abs((box_2d[3] - box_2d[1]) - (box_3d[3] - box_3d[1])), 4),
                "iou": round(_box_iou(box_2d, box_3d), 4),
            }
        )
    missing_2d = [i for i in expected if i not in boxes_2d_m]
    missing_3d = [i for i in expected if i not in by_id]
    unknown_3d = [str(o.get("id")) for o in objects_3d if str(o.get("type")) not in BUILDER_TYPES]
    errors = [r["center_error_m"] for r in rows]
    return {
        "matched": len(rows),
        "expected": len(expected),
        "missing_in_2d": missing_2d,
        "missing_in_3d": missing_3d,
        # 3D에서 전용 형태 없이 상자로 그려진 객체(TYPE_MAP 누락 등)
        "unknown_type_in_3d": unknown_3d,
        "mean_center_error_m": _mean(errors),
        "max_center_error_m": round(max(errors), 4) if errors else None,
        "mean_iou": _mean([r["iou"] for r in rows]),
        "objects_over_10cm": sum(1 for e in errors if e > 0.10),
        "objects": rows,
    }


def _box_iou(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------- 정답 대비

# 분석기와 정답 라벨의 카테고리 표기가 달라도 같은 가구로 매칭되게 묶는다
CATEGORY_GROUPS = {
    "coffee_table": "low_table",
    "nightstand": "cabinet",
    "dresser": "cabinet",
    "desk_chair": "chair",
    "floor_chair": "chair",
}
MATCH_MAX_DISTANCE_M = 1.0


def _group(category: str) -> str:
    category = str(category or "").lower()
    return CATEGORY_GROUPS.get(category, category)


def accuracy_metrics(
    predicted: list[dict[str, Any]],
    predicted_room: dict[str, Any],
    truth: dict[str, Any],
    *,
    category_key: str = "category",
) -> dict[str, Any]:
    """정답 Scene Graph 대비 검출·위치·크기·회전 정확도.

    predicted: cx, cy, w_m, d_m, rotation_deg와 category_key 필드를 가진 객체(미터).
    truth: {"room": {"width_m", "depth_m"}, "objects": [{"category", "cx", "cy",
            "w_m", "d_m", "rotation_deg"}]} — docs/metrics/README.md 참고.
    """
    from scipy.optimize import linear_sum_assignment

    true_objects = [o for o in truth.get("objects") or [] if isinstance(o, dict)]
    pred = [o for o in predicted if isinstance(o, dict)]
    big = 1e6
    cost = [[big] * max(1, len(true_objects)) for _ in range(max(1, len(pred)))]
    for i, p in enumerate(pred):
        for j, t in enumerate(true_objects):
            if _group(p.get(category_key)) != _group(t.get("category")):
                continue
            distance = math.dist((float(p["cx"]), float(p["cy"])), (float(t["cx"]), float(t["cy"])))
            if distance <= MATCH_MAX_DISTANCE_M:
                cost[i][j] = distance
    matches = []
    if pred and true_objects:
        rows, cols = linear_sum_assignment(cost)
        for i, j in zip(rows, cols):
            if cost[i][j] < big:
                matches.append((pred[i], true_objects[j], cost[i][j]))

    tp = len(matches)
    precision = _ratio(tp, len(pred))
    recall = _ratio(tp, len(true_objects))
    f1 = (
        round(2 * precision * recall / (precision + recall), 4)
        if precision and recall
        else 0.0
    )

    def rotation_error(p: dict[str, Any], t: dict[str, Any]) -> float:
        diff = abs(float(p.get("rotation_deg") or 0) - float(t.get("rotation_deg") or 0)) % 360
        return min(diff, 360 - diff)

    truth_room = truth.get("room") or {}
    room_error = None
    if truth_room.get("width_m") and truth_room.get("depth_m"):
        room_error = {
            "width_error_m": round(abs(float(predicted_room["width_m"]) - float(truth_room["width_m"])), 3),
            "depth_error_m": round(abs(float(predicted_room["depth_m"]) - float(truth_room["depth_m"])), 3),
        }
    return {
        "predicted": len(pred),
        "truth": len(true_objects),
        "matched": tp,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "mean_center_error_m": _mean([m[2] for m in matches]),
        "mean_width_error_m": _mean([abs(float(m[0]["w_m"]) - float(m[1]["w_m"])) for m in matches]),
        "mean_depth_error_m": _mean([abs(float(m[0]["d_m"]) - float(m[1]["d_m"])) for m in matches]),
        "mean_rotation_error_deg": _mean([rotation_error(m[0], m[1]) for m in matches]),
        "room": room_error,
    }


# ---------------------------------------------------------------- 공통

def _ratio(numerator: float, denominator: float) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None
