"""Scene Graph 편집 연산. 2D·3D·검토 패널이 모두 이 함수 하나로 고친다 (항목 15·16·19).

예전에는 2D 편집기가 SVG 전체를 보내고 서버가 SVG에서 좌표를 거꾸로 읽어 냈다.
3D나 검토 화면에서 고치려면 같은 일을 또 구현해야 했다. 여기서는 '무엇을 바꿨는지'만
연산 목록으로 받는다. 화면이 어디든 결과는 같은 Scene Graph에 같은 방식으로 들어가고,
2D와 3D는 그 그래프에서 다시 그려진다.

연산(op)
  move     {"id", "cx", "cy"}                  미터, 방 좌상단 기준
  rotate   {"id", "rotation_deg"}              위에서 본 시계방향 절대각
  resize   {"id", "w_m", "d_m"}                가구 기준 폭·깊이
  remove   {"id"}
  confirm  {"id"}                              AI 결과가 맞다고 사용자가 확인
  retype   {"id", "type", "label"?}            종류를 바로잡음
  add      {"type", "wall"?, "offset"?, "cx"?, "cy"?, "w_m"?, "d_m"?, "label"?}
           문·창은 wall과 offset(벽을 따라 0~1)으로 넣는다

사용자가 고친 값은 source="user", confidence=1.0이 되고, AI가 냈던 값은
graph["corrections"]에 전후로 남는다. 최종 공간 분석 결과를 사람이 보정했다는
근거이자, 분석 정확도를 다시 잴 때 정답으로 쓸 수 있는 기록이다.
"""
from __future__ import annotations

import copy
import time
from typing import Any

from . import scene_graph

MIN_SIZE_M = 0.05
MAX_SIZE_M = 6.0
KOREAN_LABELS = {
    "bed": "침대",
    "sofa": "소파",
    "desk": "책상",
    "table": "테이블",
    "low_table": "낮은 테이블",
    "chair": "의자",
    "desk_chair": "책상 의자",
    "floor_chair": "좌식 의자",
    "stool": "스툴",
    "bench": "벤치",
    "shelf": "선반",
    "cabinet": "수납장",
    "dresser": "서랍장",
    "wardrobe": "옷장",
    "vanity": "화장대",
    "nightstand": "협탁",
    "rug": "러그",
    "mirror": "거울",
    "lamp": "조명",
    "plant": "식물",
    "tv": "TV",
    "fridge": "냉장고",
    "washer": "세탁기",
    "aircon": "에어컨",
    "curtain": "커튼",
    "door": "문",
    "window": "창문",
    "decor": "소품",
}
EDITABLE_TYPES = set(KOREAN_LABELS)


class EditError(ValueError):
    """잘못된 편집 요청. 메시지는 사용자에게 그대로 보여 준다."""


def _snapshot(obj: dict[str, Any]) -> dict[str, Any]:
    return {
        key: obj.get(key)
        for key in ("type", "category", "label", "cx", "cy", "w_m", "d_m", "rotation_deg", "wall", "confidence", "source")
    }


def _number(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        raise EditError(f"{name} 값이 숫자가 아닙니다.") from None
    if result != result:
        raise EditError(f"{name} 값이 올바르지 않습니다.")
    return result


def _find(graph: dict[str, Any], object_id: Any) -> dict[str, Any]:
    target = next((o for o in graph["objects"] if str(o.get("id")) == str(object_id)), None)
    if target is None:
        raise EditError("해당 가구를 찾을 수 없습니다.")
    return target


def _new_id(graph: dict[str, Any], kind: str) -> str:
    used = {str(o.get("id")) for o in graph["objects"]}
    index = 1
    while f"{kind}_user_{index}" in used:
        index += 1
    return f"{kind}_user_{index}"


def _mark_user(graph: dict[str, Any], obj: dict[str, Any], before: dict[str, Any], op: str) -> None:
    # AI 값 → 사용자 값 기록은 처음 고칠 때의 AI 값만 남긴다(여러 번 고쳐도 원래 AI 값이 보존)
    if before.get("source") == "ai":
        graph.setdefault("corrections", []).append(
            {"ts": round(time.time(), 3), "id": obj["id"], "op": op, "ai": before}
        )
    obj["source"] = "user" if obj.get("source") in {"ai", "ai_refined", None, "user"} else obj.get("source")
    obj["confidence"] = 1.0


def apply_ops(layout: dict[str, Any], ops: list[dict[str, Any]]) -> tuple[dict[str, Any], list[str]]:
    """연산을 적용한 새 그래프와 바뀐 객체 id 목록. 원본은 건드리지 않는다."""
    if not isinstance(ops, list) or not ops:
        raise EditError("편집 내용이 비어 있습니다.")
    if len(ops) > 50:
        raise EditError("한 번에 너무 많은 편집을 보냈습니다.")
    graph = scene_graph.ensure(copy.deepcopy(layout), solve_new=False)
    room = graph["room"]
    W, D = float(room["width_m"]), float(room["depth_m"])
    changed: list[str] = []
    walkway = False

    for raw in ops:
        if not isinstance(raw, dict):
            raise EditError("편집 형식이 올바르지 않습니다.")
        op = str(raw.get("op") or "")
        if op == "add":
            kind = scene_graph.object_type(raw.get("type"))
            if kind not in EDITABLE_TYPES:
                raise EditError("추가할 수 없는 종류입니다.")
            # 크기는 사용자가 넣은 값만 쓴다(종류별 표준 크기는 없다). 문·창은 벽 두께 쪽이 얇다
            if raw.get("w_m") is None:
                raise EditError("추가할 크기(폭)를 입력해 주세요.")
            w_m = min(MAX_SIZE_M, max(MIN_SIZE_M, _number(raw.get("w_m"), "w_m")))
            if raw.get("d_m") is None and kind not in scene_graph.WALL_MOUNTED_TYPES:
                raise EditError("추가할 크기(깊이)를 입력해 주세요.")
            d_m = min(MAX_SIZE_M, max(MIN_SIZE_M, _number(raw.get("d_m", scene_graph.WALL_MOUNTED_MAX_DEPTH_M), "d_m")))
            wall = str(raw.get("wall") or "none")
            if kind in scene_graph.WALL_MOUNTED_TYPES:
                if wall not in scene_graph.WALLS:
                    raise EditError("문·창은 붙일 벽을 골라 주세요.")
                offset = min(1.0, max(0.0, _number(raw.get("offset", 0.5), "offset")))
                along = W if wall in {"top", "bottom"} else D
                position = min(max(offset * along, w_m / 2), along - w_m / 2)
                # 벽 쪽 좌표는 그 벽 위에 둔다. 0으로 두면 아래·오른쪽 벽 문이
                # '가장 가까운 벽' 계산에서 반대편(위·왼쪽) 벽에 붙는다
                cx = position if wall in {"top", "bottom"} else (W if wall == "right" else 0.0)
                cy = position if wall in {"left", "right"} else (D if wall == "bottom" else 0.0)
            else:
                cx = min(W, max(0.0, _number(raw.get("cx", W / 2), "cx")))
                cy = min(D, max(0.0, _number(raw.get("cy", D / 2), "cy")))
            obj = {
                "id": _new_id(graph, kind),
                "category": kind,
                "type": kind,
                "label": str(raw.get("label") or KOREAN_LABELS.get(kind, kind))[:30],
                "cx": cx,
                "cy": cy,
                "w_m": w_m,
                "d_m": d_m,
                "rotation_deg": scene_graph.WALL_ROTATION.get(wall, 0.0),
                "wall": wall,
                "confidence": 1.0,
                "source": "user",
                "source_index": len(graph["objects"]),
            }
            graph["objects"].append(obj)
            changed.append(obj["id"])
            walkway = walkway or kind == "door"
            graph.setdefault("corrections", []).append(
                {"ts": round(time.time(), 3), "id": obj["id"], "op": "add", "ai": None}
            )
            continue

        obj = _find(graph, raw.get("id"))
        before = _snapshot(obj)
        if op == "move":
            obj["cx"] = min(W, max(0.0, _number(raw.get("cx"), "cx")))
            obj["cy"] = min(D, max(0.0, _number(raw.get("cy"), "cy")))
        elif op == "rotate":
            obj["rotation_deg"] = _number(raw.get("rotation_deg"), "rotation_deg") % 360
        elif op == "resize":
            obj["w_m"] = min(MAX_SIZE_M, max(MIN_SIZE_M, _number(raw.get("w_m"), "w_m")))
            obj["d_m"] = min(MAX_SIZE_M, max(MIN_SIZE_M, _number(raw.get("d_m"), "d_m")))
        elif op == "remove":
            if before.get("source") == "ai":
                graph.setdefault("corrections", []).append(
                    {"ts": round(time.time(), 3), "id": obj["id"], "op": "remove", "ai": before}
                )
            graph["objects"].remove(obj)
            changed.append(str(obj["id"]))
            continue
        elif op == "confirm":
            pass
        elif op == "retype":
            kind = scene_graph.object_type(raw.get("type"))
            if kind not in EDITABLE_TYPES:
                raise EditError("바꿀 수 없는 종류입니다.")
            obj["type"] = kind
            obj["category"] = kind
            obj["label"] = str(raw.get("label") or KOREAN_LABELS.get(kind, kind))[:30]
        else:
            raise EditError(f"알 수 없는 편집입니다: {op}")
        _mark_user(graph, obj, before, op)
        changed.append(str(obj["id"]))

    for index, obj in enumerate(graph["objects"]):
        obj["source_index"] = index
    # 벽걸이는 놓은 자리에서 가장 가까운 벽에 붙고, 겹친 다른 가구는 비켜 준다.
    # 사용자가 고친 가구는 고정한다. 문을 추가했을 때만 동선까지 다시 맞춘다.
    scene_graph._place_wall_mounted(graph)
    from .placement_solver import solve

    solve(graph, locked_ids=changed, walkway=walkway)
    for obj in graph["objects"]:
        # 편집 후 기준값을 지금 상태로 맞춘다. 2D 편집기는 이 값을 기준으로 누적 배율을 보낸다
        obj["edit_origin"] = scene_graph._origin(obj)
    scene_graph.append_history(graph, "user", "edit", ops=[str(o.get("op")) for o in ops], objects=changed)
    return scene_graph.sync_legacy(graph), changed


def uncertain_objects(layout: dict[str, Any], threshold: float = 0.6) -> list[dict[str, Any]]:
    """사용자에게 확인을 부탁할 가구 목록 (항목 18)."""
    rows = []
    for obj in layout.get("objects") or []:
        confidence = float(obj.get("confidence") if obj.get("confidence") is not None else 1.0)
        if obj.get("source") in {"ai", "ai_refined"} and confidence < threshold:
            rows.append(
                {
                    "id": obj["id"],
                    "type": obj.get("type"),
                    "label": obj.get("label"),
                    "confidence": round(confidence, 2),
                    "refined": obj.get("source") == "ai_refined",
                }
            )
    return sorted(rows, key=lambda r: r["confidence"])
