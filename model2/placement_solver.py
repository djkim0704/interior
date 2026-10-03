"""Scene Graph 위에서 가구 배치를 보정한다 (항목 9·10).

분석기가 주는 좌표는 대략적이라 가구끼리 겹치거나 벽을 넘거나, 사람이 지나갈
틈이 없게 놓이는 경우가 많다. 2D·3D가 같은 Scene Graph를 그리므로 여기서 한 번
고치면 양쪽에 동시에 반영된다.

단계
  1) 방 안으로 넣기: 회전을 반영한 바닥면이 벽을 넘지 않게 한다.
  2) 충돌 해소: 고정 가구부터 놓고, 나머지는 원래 위치에서 가까운 순서로
     빈자리를 찾는다. 벽에 붙은 가구는 먼저 벽을 따라서만 움직인다.
     문 앞(문 폭만큼의 정사각형)은 열리는 공간이라 비워 둔다.
  3) 동선 확보: 문(없으면 가장 큰 빈 영역)에서 폭 2×CLEARANCE 통로로 닿지
     않는 가구를, 충돌 없이 닿게 되는 가장 가까운 자리로 옮긴다.

고정(locked) 가구는 움직이지 않는다. 사용자가 직접 옮긴 가구와 선택한 상품이
여기에 해당한다. 사람의 의도가 자동 보정보다 우선해야 하기 때문이다.

모든 이동은 graph["solver_adjustments"]에 사유와 함께 남긴다(특허의 '배치 보정
이력'과 사용자 확인 화면에 쓴다).
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Iterator

import numpy as np

from . import scene_graph

# 겹쳐도 되거나 바닥을 막지 않는 것. evaluation/metrics.py의 정의와 같은 취지다.
NON_BLOCKING_TYPES = {"rug", "door", "window", "mirror", "curtain", "aircon", "tv", "lamp", "decor"}
TUCKABLE = {
    frozenset({"chair", "desk"}),
    frozenset({"chair", "table"}),
    frozenset({"desk_chair", "desk"}),
    frozenset({"stool", "table"}),
    frozenset({"stool", "desk"}),
    frozenset({"floor_chair", "low_table"}),
}
# 이 면적(m²) 이하의 겹침은 접촉으로 보고 무시한다
OVERLAP_TOLERANCE_M2 = 0.004
CLEARANCE_M = 0.30
GRID_M = 0.10
REACH_MARGIN_M = 0.10
SEARCH_STEP_M = 0.05
SEARCH_MAX_M = 2.0
# 이 거리 안에 빈자리가 없으면 멀리 옮기기 전에 크기를 조금 줄여 본다.
# 사진 한 장의 가구 크기 추정은 실제보다 큰 경향이 있어서, 2m를 옮기는 것보다
# 20% 이내로 줄이는 쪽이 원래 배치(사진)에 더 가깝다.
NEAR_SHIFT_M = 0.8
SHRINK_STEPS = (0.95, 0.9, 0.85, 0.8)
WALKWAY_MAX_CANDIDATES = 320
WALKWAY_MAX_OBJECTS = 8
WALKWAY_STEP_M = 0.10
WALKWAY_MAX_M = 1.5


# ---------------------------------------------------------------- 기하

def _corners(obj: dict[str, Any], cx: float | None = None, cy: float | None = None) -> np.ndarray:
    cx = float(obj["cx"]) if cx is None else cx
    cy = float(obj["cy"]) if cy is None else cy
    w, d = float(obj["w_m"]), float(obj["d_m"])
    angle = math.radians(float(obj.get("rotation_deg") or 0.0))
    cos, sin = math.cos(angle), math.sin(angle)
    local = np.array([(-w / 2, -d / 2), (w / 2, -d / 2), (w / 2, d / 2), (-w / 2, d / 2)])
    rot = np.array([[cos, -sin], [sin, cos]])
    return local @ rot.T + np.array([cx, cy])


def _area(poly: np.ndarray) -> float:
    if len(poly) < 3:
        return 0.0
    x, y = poly[:, 0], poly[:, 1]
    return float(abs(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))) / 2)


def _clip(subject: np.ndarray, clip: np.ndarray) -> np.ndarray:
    # 볼록 사각형끼리의 교집합(Sutherland–Hodgman)
    def orientation(poly: np.ndarray) -> float:
        x, y = poly[:, 0], poly[:, 1]
        return float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1)))

    if orientation(clip) < 0:
        clip = clip[::-1]
    output = [tuple(p) for p in subject]
    n = len(clip)
    for i in range(n):
        if not output:
            break
        ax, ay = clip[i]
        bx, by = clip[(i + 1) % n]
        current, output = output, []

        def inside(p: tuple[float, float]) -> bool:
            return (bx - ax) * (p[1] - ay) - (by - ay) * (p[0] - ax) >= -1e-12

        def cross(p: tuple[float, float], q: tuple[float, float]) -> tuple[float, float]:
            dx1, dy1 = q[0] - p[0], q[1] - p[1]
            dx2, dy2 = bx - ax, by - ay
            denom = dx1 * dy2 - dy1 * dx2
            if abs(denom) < 1e-15:
                return q
            t = ((ax - p[0]) * dy2 - (ay - p[1]) * dx2) / denom
            return p[0] + t * dx1, p[1] + t * dy1

        for j, point in enumerate(current):
            previous = current[j - 1]
            if inside(point):
                if not inside(previous):
                    output.append(cross(previous, point))
                output.append(point)
            elif inside(previous):
                output.append(cross(previous, point))
    return np.array(output) if output else np.zeros((0, 2))


def overlap(first: np.ndarray, second: np.ndarray) -> float:
    # 외접 박스가 안 겹치면 클리핑을 건너뛴다(대부분의 쌍이 여기서 끝난다)
    if (
        first[:, 0].max() <= second[:, 0].min()
        or second[:, 0].max() <= first[:, 0].min()
        or first[:, 1].max() <= second[:, 1].min()
        or second[:, 1].max() <= first[:, 1].min()
    ):
        return 0.0
    return _area(_clip(first, second))


def blocking(obj: dict[str, Any]) -> bool:
    kind = str(obj.get("type") or "")
    return kind not in NON_BLOCKING_TYPES and kind not in scene_graph.WALL_MOUNTED_TYPES


def _clamp_inside(obj: dict[str, Any], cx: float, cy: float, room: dict[str, Any]) -> tuple[float, float]:
    plan_w, plan_d = scene_graph.plan_extent(obj)
    W, D = float(room["width_m"]), float(room["depth_m"])
    half_w, half_d = min(plan_w, W) / 2, min(plan_d, D) / 2
    return min(max(cx, half_w), W - half_w), min(max(cy, half_d), D - half_d)


def _door_keepouts(objects: Iterable[dict[str, Any]], room: dict[str, Any]) -> list[np.ndarray]:
    """문이 열리는 공간. 문 폭만큼의 정사각형을 방 안쪽에 둔다."""
    W, D = float(room["width_m"]), float(room["depth_m"])
    zones = []
    for obj in objects:
        if obj.get("type") != "door":
            continue
        size = max(0.6, min(1.2, float(obj["w_m"])))
        cx, cy = float(obj["cx"]), float(obj["cy"])
        wall = obj.get("wall")
        if wall == "top":
            box = (cx - size / 2, 0.0, cx + size / 2, size)
        elif wall == "bottom":
            box = (cx - size / 2, D - size, cx + size / 2, D)
        elif wall == "left":
            box = (0.0, cy - size / 2, size, cy + size / 2)
        elif wall == "right":
            box = (W - size, cy - size / 2, W, cy + size / 2)
        else:
            continue
        x0, y0, x1, y1 = box
        zones.append(np.array([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]))
    return zones


# ---------------------------------------------------------------- 후보 위치

def _candidates(
    obj: dict[str, Any],
    room: dict[str, Any],
    max_m: float = SEARCH_MAX_M,
) -> Iterator[tuple[float, float]]:
    """원래 위치에서 가까운 순서로 후보를 낸다. 벽에 붙은 가구는 벽을 따라 먼저."""
    ox, oy = float(obj["cx"]), float(obj["cy"])
    wall = obj.get("wall") if obj.get("wall") in scene_graph.WALLS else None
    yield _clamp_inside(obj, ox, oy, room)
    steps = int(max_m / SEARCH_STEP_M)
    if wall:
        along_x = wall in {"top", "bottom"}
        for i in range(1, steps + 1):
            for sign in (1, -1):
                delta = sign * i * SEARCH_STEP_M
                yield _clamp_inside(obj, ox + delta if along_x else ox, oy if along_x else oy + delta, room)
    for i in range(1, steps + 1):
        radius = i * SEARCH_STEP_M
        count = max(8, int(2 * math.pi * radius / SEARCH_STEP_M))
        for k in range(count):
            angle = 2 * math.pi * k / count
            yield _clamp_inside(obj, ox + radius * math.cos(angle), oy + radius * math.sin(angle), room)


def _walkway_candidates(obj: dict[str, Any], room: dict[str, Any]) -> Iterator[tuple[float, float]]:
    # 동선 보정은 후보마다 격자 전체를 다시 계산해서 비싸다. 10cm 간격으로 성기게 본다.
    ox, oy = float(obj["cx"]), float(obj["cy"])
    wall = obj.get("wall") if obj.get("wall") in scene_graph.WALLS else None
    steps = int(WALKWAY_MAX_M / WALKWAY_STEP_M)
    if wall:
        along_x = wall in {"top", "bottom"}
        for i in range(1, steps + 1):
            for sign in (1, -1):
                delta = sign * i * WALKWAY_STEP_M
                yield _clamp_inside(obj, ox + delta if along_x else ox, oy if along_x else oy + delta, room)
    for i in range(1, steps + 1):
        radius = i * WALKWAY_STEP_M
        count = max(8, int(2 * math.pi * radius / WALKWAY_STEP_M))
        for k in range(count):
            angle = 2 * math.pi * k / count
            yield _clamp_inside(obj, ox + radius * math.cos(angle), oy + radius * math.sin(angle), room)


# ---------------------------------------------------------------- 동선 격자

class _Grid:
    def __init__(self, room: dict[str, Any], step: float = GRID_M) -> None:
        self.W, self.D = float(room["width_m"]), float(room["depth_m"])
        self.step = step
        self.cols = max(1, int(round(self.W / step)))
        self.rows = max(1, int(round(self.D / step)))
        xs = (np.arange(self.cols) + 0.5) * self.W / self.cols
        ys = (np.arange(self.rows) + 0.5) * self.D / self.rows
        self.x, self.y = np.meshgrid(xs, ys)

    def distance(self, obj: dict[str, Any]) -> np.ndarray:
        # 회전된 사각형까지의 거리. 안쪽이면 0
        angle = math.radians(float(obj.get("rotation_deg") or 0.0))
        cos, sin = math.cos(angle), math.sin(angle)
        dx, dy = self.x - float(obj["cx"]), self.y - float(obj["cy"])
        lx = dx * cos + dy * sin
        ly = -dx * sin + dy * cos
        ex = np.maximum(np.abs(lx) - float(obj["w_m"]) / 2, 0.0)
        ey = np.maximum(np.abs(ly) - float(obj["d_m"]) / 2, 0.0)
        return np.hypot(ex, ey)

    def cell(self, x: float, y: float) -> tuple[int, int]:
        return (
            min(self.rows - 1, max(0, int(y / self.D * self.rows))),
            min(self.cols - 1, max(0, int(x / self.W * self.cols))),
        )


def walkway_state(objects: list[dict[str, Any]], room: dict[str, Any], clearance: float = CLEARANCE_M) -> dict[str, Any]:
    """문에서 걸어서 닿는 영역과, 앞에 설 수 없는 가구 목록."""
    from scipy import ndimage

    grid = _Grid(room)
    blockers = [o for o in objects if blocking(o)]
    distances = [grid.distance(o) for o in blockers]
    wall_distance = np.minimum.reduce([grid.x, grid.y, grid.W - grid.x, grid.D - grid.y])
    free = wall_distance >= clearance
    for dist in distances:
        free &= dist >= clearance
    labels, count = ndimage.label(free)

    doors = [o for o in objects if o.get("type") == "door"]
    door_blocked = []
    reachable_labels: set[int] = set()
    for door in doors:
        wall = door.get("wall")
        x, y = float(door["cx"]), float(door["cy"])
        inset = clearance + grid.step
        if wall == "top":
            y = inset
        elif wall == "bottom":
            y = grid.D - inset
        elif wall == "left":
            x = inset
        elif wall == "right":
            x = grid.W - inset
        row, col = grid.cell(x, y)
        if labels[row, col]:
            reachable_labels.add(int(labels[row, col]))
        else:
            door_blocked.append(door.get("id"))
    if not doors and count:
        sizes = ndimage.sum(free, labels, index=range(1, count + 1))
        reachable_labels = {int(np.argmax(sizes)) + 1}
    reachable = np.isin(labels, list(reachable_labels)) if reachable_labels else np.zeros_like(free)

    inaccessible = []
    for obj, dist in zip(blockers, distances):
        if not np.any(reachable & (dist <= clearance + REACH_MARGIN_M)):
            inaccessible.append(str(obj.get("id")))
    return {
        "inaccessible": inaccessible,
        "door_blocked": door_blocked,
        "reachable_ratio": float(reachable.sum()) / float(max(1, free.size)),
    }


def _walkway_cost(state: dict[str, Any]) -> int:
    return len(state["inaccessible"]) + 3 * len(state["door_blocked"])


# ---------------------------------------------------------------- 보정

def _is_locked(obj: dict[str, Any], locked_ids: set[str]) -> bool:
    return str(obj.get("id")) in locked_ids or obj.get("source") in {"user", "selected_product"} and obj.get("locked", True) is not False


def _pair_ignored(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return frozenset({str(a.get("type")), str(b.get("type"))}) in TUCKABLE


def solve(
    layout: dict[str, Any],
    *,
    locked_ids: Iterable[str] = (),
    movable_ids: Iterable[str] | None = None,
    walkway: bool = True,
) -> dict[str, Any]:
    """배치를 제자리에서 보정하고 같은 dict를 돌려준다.

    locked_ids: 움직이지 않을 객체. source가 user·selected_product인 객체도 고정이다.
    movable_ids: 주어지면 이 객체들만 움직인다(예: 새로 추가한 상품만 빈자리에 놓기).
    walkway: False면 충돌·벽만 고친다(편집 직후처럼 다른 가구를 크게 옮기면 안 될 때).
    """
    graph = layout
    room = graph["room"]
    objects = graph.get("objects") or []
    locked = {str(i) for i in locked_ids}
    movable_only = {str(i) for i in movable_ids} if movable_ids is not None else None
    adjustments = graph.setdefault("solver_adjustments", [])

    def can_move(obj: dict[str, Any]) -> bool:
        if movable_only is not None:
            return str(obj.get("id")) in movable_only
        return not _is_locked(obj, locked)

    def record(obj: dict[str, Any], before: tuple[float, float], reason: str) -> None:
        after = (float(obj["cx"]), float(obj["cy"]))
        if math.dist(before, after) >= 0.01:
            adjustments.append(
                {
                    "object_id": obj.get("id"),
                    "reason": reason,
                    "units": "m",
                    "from": [round(before[0], 3), round(before[1], 3)],
                    "to": [round(after[0], 3), round(after[1], 3)],
                }
            )

    floor = [o for o in objects if blocking(o)]
    keepouts = _door_keepouts(objects, room)

    # 1) 방 안으로
    for obj in floor:
        if not can_move(obj):
            continue
        before = (float(obj["cx"]), float(obj["cy"]))
        obj["cx"], obj["cy"] = _clamp_inside(obj, *before, room)
        record(obj, before, "room_boundary")

    # 2) 충돌 해소. 고정 → 벽에 붙은 것 → 큰 것 → 신뢰도 높은 것 순서로 확정한다.
    order = sorted(
        floor,
        key=lambda o: (
            0 if not can_move(o) else 1,
            0 if o.get("wall") in scene_graph.WALLS else 1,
            -float(o["w_m"]) * float(o["d_m"]),
            -float(o.get("confidence") or 0.5),
        ),
    )
    placed: list[tuple[dict[str, Any], np.ndarray]] = []

    def collision_cost(obj: dict[str, Any], poly: np.ndarray) -> float:
        cost = 0.0
        for other, other_poly in placed:
            if _pair_ignored(obj, other):
                continue
            area = overlap(poly, other_poly)
            if area > OVERLAP_TOLERANCE_M2:
                cost += area
        for zone in keepouts:
            area = overlap(poly, zone)
            if area > OVERLAP_TOLERANCE_M2:
                cost += area
        return cost

    for obj in order:
        if not can_move(obj):
            placed.append((obj, _corners(obj)))
            continue
        before = (float(obj["cx"]), float(obj["cy"]))
        original_size = (float(obj["w_m"]), float(obj["d_m"]))

        def nearest_free(max_m: float) -> tuple[float, float] | None:
            for cx, cy in _candidates(obj, room, max_m):
                if collision_cost(obj, _corners(obj, cx, cy)) == 0.0:
                    return cx, cy
            return None

        # ① 가까운 빈자리 → ② 조금 줄여서 가까운 빈자리 → ③ 멀어도 빈자리 → ④ 겹침 최소
        found = nearest_free(NEAR_SHIFT_M)
        scale = 1.0
        if found is None:
            for factor in SHRINK_STEPS:
                obj["w_m"], obj["d_m"] = original_size[0] * factor, original_size[1] * factor
                found = nearest_free(NEAR_SHIFT_M)
                if found is not None:
                    scale = factor
                    break
            if found is None:
                obj["w_m"], obj["d_m"] = original_size
        if found is None:
            found = nearest_free(SEARCH_MAX_M)
        if found is None:
            best, best_score = before, math.inf
            for cx, cy in _candidates(obj, room):
                score = collision_cost(obj, _corners(obj, cx, cy)) * 1000.0 + math.dist(before, (cx, cy))
                if score < best_score:
                    best, best_score = (cx, cy), score
            found = best
        obj["cx"], obj["cy"] = found
        placed.append((obj, _corners(obj)))
        if scale < 1.0:
            adjustments.append(
                {
                    "object_id": obj.get("id"),
                    "reason": "collision_resize",
                    "units": "m",
                    "scale": scale,
                    "from_size": [round(v, 3) for v in original_size],
                    "to_size": [round(float(obj["w_m"]), 3), round(float(obj["d_m"]), 3)],
                    "from": [round(before[0], 3), round(before[1], 3)],
                    "to": [round(found[0], 3), round(found[1], 3)],
                }
            )
        else:
            record(obj, before, "collision")

    # 3) 동선 확보
    if walkway:
        state = walkway_state(objects, room)
        polys = {id(o): p for o, p in placed}
        tries = 0
        while _walkway_cost(state) and tries < WALKWAY_MAX_OBJECTS:
            tries += 1
            # 접근이 막힌 가구 자신보다 그 앞을 가로막은 가구를 옮겨야 풀리는 경우가
            # 많다. 그래서 움직일 수 있는 가구 전부를 후보로 두되, 막힌 가구를 먼저,
            # 그다음 작은 가구부터 시도한다(큰 가구를 옮기면 방 구성이 크게 바뀐다).
            blocked = set(state["inaccessible"])
            targets = [o for o in floor if can_move(o)]
            targets.sort(
                key=lambda o: (
                    0 if str(o.get("id")) in blocked else 1,
                    float(o["w_m"]) * float(o["d_m"]),
                )
            )
            improved = False
            for obj in targets:
                before = (float(obj["cx"]), float(obj["cy"]))
                others = [(o, p) for o, p in placed if o is not obj]
                best = None
                current = _walkway_cost(state)
                for index, (cx, cy) in enumerate(_walkway_candidates(obj, room)):
                    if index >= WALKWAY_MAX_CANDIDATES:
                        break
                    poly = _corners(obj, cx, cy)
                    if any(
                        not _pair_ignored(obj, o) and overlap(poly, p) > OVERLAP_TOLERANCE_M2 for o, p in others
                    ) or any(overlap(poly, z) > OVERLAP_TOLERANCE_M2 for z in keepouts):
                        continue
                    obj["cx"], obj["cy"] = cx, cy
                    trial = walkway_state(objects, room)
                    obj["cx"], obj["cy"] = before
                    if _walkway_cost(trial) < current:
                        best = (cx, cy, trial)
                        break
                if best is not None:
                    obj["cx"], obj["cy"], state = best
                    polys[id(obj)] = _corners(obj)
                    placed = [(o, polys.get(id(o), p)) for o, p in placed]
                    record(obj, before, "walkway")
                    improved = True
                    break
            if not improved:
                break
        graph["walkway"] = {
            "clearance_m": CLEARANCE_M,
            "inaccessible": state["inaccessible"],
            "door_blocked": state["door_blocked"],
            "reachable_ratio": round(state["reachable_ratio"], 4),
        }
    return graph
