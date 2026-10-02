"""추천 상품이 이 방에 실제로 맞는지 평가한다 (항목 8·21).

무드만 보면 방에 들어가지도 않는 소파나, 놓으면 통로가 막히는 수납장이 1순위가
된다. 여기서는 상품을 Scene Graph에 실제 치수로 넣어 보고 배치 보정기로 자리를
찾게 한 뒤 세 가지를 잰다.

  space_fit  겹치지 않는 자리가 있고 동선이 유지되는가 (0~1)
  size_fit   교체 대상과 크기가 비슷한가 (0~1). 새로 추가하는 상품은 비교 대상이 없어 None
  reasons    사용자에게 보여 줄 근거 문장

치수는 제목·설명에서만 읽는다(product_dimensions.resolve, fetch=False). 추천 목록마다
상품 페이지를 받거나 사진을 분석하면 너무 느리고 비싸다. 상품을 고른 뒤에 정밀하게 잰다.
"""
from __future__ import annotations

import copy
import math
from typing import Any, Callable

from . import placement_solver, product_dimensions, scene_graph

WEIGHTS = {"mood": 0.55, "space": 0.30, "size": 0.15}
FAR_MOVE_M = 1.0


def _size_score_against(target_area: float, area: float) -> float:
    # 같은 크기 1.0, 두 배(또는 절반) 0.0. 곱셈 오차라 로그로 잰다
    ratio = max(area, 1e-6) / max(target_area, 1e-6)
    return round(max(0.0, 1.0 - abs(math.log(ratio)) / math.log(2.0)), 3)


def make_scorer(
    layout: dict[str, Any],
    category: str,
    *,
    replace_id: str | None = None,
) -> Callable[[dict[str, Any]], dict[str, Any]]:
    """상품 하나를 받아 적합도를 돌려주는 함수를 만든다. 방 상태는 한 번만 준비한다."""
    base = scene_graph.ensure(layout, solve_new=False)
    room = base["room"]
    W, D = float(room["width_m"]), float(room["depth_m"])
    kind = scene_graph.object_type(category)
    target = next((o for o in base["objects"] if str(o.get("id")) == str(replace_id)), None) if replace_id else None
    others = [o for o in base["objects"] if o is not target]
    before = placement_solver.walkway_state(others, room)
    before_blocked = set(before["inaccessible"]) | set(before["door_blocked"])

    def score(product: dict[str, Any]) -> dict[str, Any]:
        dims = product_dimensions.resolve({**product, "type": kind}, None, fetch=False)
        if dims is None:
            # 표준 크기로 지어내 재지 않는다. 무드 점수로만 순위를 정한다
            return {
                "space": None,
                "size": None,
                "fits": None,
                "dimensions": None,
                "reasons": ["치수 정보가 없어 공간 적합도를 재지 못했어요"],
            }
        w, d = float(dims["w_m"]), float(dims["d_m"])
        reasons: list[str] = []

        # 방보다 크면 놓아 볼 필요도 없다
        if min(w, d) > min(W, D) or max(w, d) > max(W, D):
            return {
                "space": 0.0,
                "size": 0.0,
                "fits": False,
                "dimensions": dims,
                "reasons": reasons + ["방에 들어가지 않는 크기예요"],
            }

        graph = copy.deepcopy(base)
        if target is not None:
            graph["objects"] = [o for o in graph["objects"] if str(o.get("id")) != str(target["id"])]
            start = (float(target["cx"]), float(target["cy"]), float(target["rotation_deg"]), target.get("wall", "none"))
        else:
            start = (W / 2, D / 2, 0.0, "none")
        candidate = {
            "id": "__candidate__",
            "type": kind,
            "category": kind,
            "label": kind,
            "cx": start[0],
            "cy": start[1],
            "w_m": w,
            "d_m": d,
            "rotation_deg": start[2],
            "wall": start[3],
            "confidence": 1.0,
            "source": "ai",
        }
        graph["objects"].append(candidate)
        placement_solver.solve(graph, movable_ids=["__candidate__"], walkway=False)
        placed = next(o for o in graph["objects"] if o["id"] == "__candidate__")
        poly = placement_solver._corners(placed)
        collides = any(
            placement_solver.blocking(other)
            and not placement_solver._pair_ignored(placed, other)
            and placement_solver.overlap(poly, placement_solver._corners(other)) > placement_solver.OVERLAP_TOLERANCE_M2
            for other in graph["objects"]
            if other is not placed
        )
        moved = math.dist((start[0], start[1]), (float(placed["cx"]), float(placed["cy"])))
        after = placement_solver.walkway_state(graph["objects"], room)
        newly_blocked = (set(after["inaccessible"]) | set(after["door_blocked"])) - before_blocked - {"__candidate__"}

        if collides:
            space = 0.0
            reasons.append("겹치지 않게 놓을 자리가 없어요")
        else:
            space = 1.0
            if newly_blocked:
                space -= 0.4
                reasons.append("놓으면 다른 가구로 가는 통로가 좁아져요")
            else:
                reasons.append("통로 60cm를 유지한 채 놓을 수 있어요")
            if target is not None and moved > FAR_MOVE_M:
                space -= 0.3
                reasons.append(f"원래 자리에서 {moved:.1f}m 옮겨야 들어가요")
            elif target is not None:
                reasons.append("교체할 가구 자리에 들어가요")
        area = w * d
        if target is not None:
            size = _size_score_against(float(target["w_m"]) * float(target["d_m"]), area)
            if size < 0.5:
                reasons.append("교체할 가구보다 크기 차이가 커요")
        else:
            # 종류별 '적당한 면적' 표는 두지 않는다. 방에 들어가는지는 공간 점수가 본다
            size = None
        return {
            "space": round(max(0.0, space), 3),
            "size": round(size, 3) if size is not None else None,
            "fits": not collides,
            "dimensions": dims,
            "placement": {"cx": round(float(placed["cx"]), 3), "cy": round(float(placed["cy"]), 3)},
            "reasons": reasons,
        }

    return score


def weighted_total(mood: float | None, fit: dict[str, Any]) -> float:
    """잰 항목만 가중 평균한다. 크기 비교 대상이 없는(새로 추가) 상품은 무드·공간으로만.

    치수를 몰라 공간을 못 잰 상품은 공간 0점으로 넣어 뒤로 민다.
    """
    parts = [("space", fit.get("space") or 0.0)]
    if mood is not None:
        parts.append(("mood", mood))
    if fit.get("size") is not None:
        parts.append(("size", fit["size"]))
    weight = sum(WEIGHTS[name] for name, _ in parts)
    return sum(WEIGHTS[name] * value for name, value in parts) / weight


def combine(mood: float, fit: dict[str, Any]) -> float:
    """무드·공간·크기 적합도를 하나로. 들어가지 않는 상품은 무드가 좋아도 뒤로 민다."""
    total = weighted_total(mood, fit)
    if fit.get("fits") is False:
        total *= 0.4
    return round(total, 4)
