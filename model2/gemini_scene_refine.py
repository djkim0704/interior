"""SVG까지 그린 뒤, Gemini가 방 전체를 글로 다시 보고 크기·높이·배치를 바로잡는다.

공간 분석은 사진 한 장에서 위치와 크기를 한꺼번에 읽어서, 가구 하나하나는 어색할 때가
있다(식탁에서 멀리 떨어진 의자, 벽에서 뜬 침대, 이름에 비해 너무 큰 협탁 등). 여기서는
방 실측과 가구 목록(이름·크기·높이·위치·방향)만 글로 주고 실내 배치 관점에서 판단하게
한다. 사진은 보내지 않는다. 사진 해석은 분석이 이미 했고, 이 단계는 상식으로 고치는
단계라 글만으로 충분하고 토큰도 적게 든다.

안전장치
  - 문·창문은 고치지 않는다(사진에서 본 방 구조라 기준으로 둔다).
  - 사용자가 고친 가구와 추가한 상품은 고치지 않는다.
  - 한 번에 MAX_MOVE_M 넘게 옮기지 못한다. 사진과 너무 달라지지 않게 한다.
  - 끝나면 배치 보정기(placement_solver)가 충돌·벽·동선을 다시 맞춘다.

같은 입력이면 결과를 캐시해 다시 부르지 않는다. 실패하면 아무것도 바꾸지 않는다.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
from pathlib import Path
from typing import Any

from google.genai import types

from .gemini_furniture_parts import thinking_for
from .gemini_svg_experiment import _ensure_not_truncated

REFINE_VERSION = "1"
FAILURE_COOLDOWN_SECONDS = 10 * 60
# 사진에서 읽은 자리를 크게 벗어나지 않게 한 번에 옮길 수 있는 거리(m)
MAX_MOVE_M = 1.0
FIXED_TYPES = {"door", "window"}

PROMPT = """
You are an expert interior planner. Below is a room reconstructed from one
photo: the real room size and every object with its Korean name, kind, size,
height, position and facing. The positions come from reading the photo and are
roughly right, but individual objects can be implausible. Fix only what a
careful interior designer would fix, using common sense about each named piece
of furniture.

Coordinates (metres): origin at the top-left corner of the room, x to the
right (0..W), y downward/into the room (0..D). cx, cy are the object's centre.
w_m is the width along the object's back, d_m its depth front-to-back, h_m its
height. rotation_deg is clockwise seen from above; 0 means the object's back
faces the top wall (y=0), 90 the right wall, 180 the bottom wall, 270 the left.

Fix, when clearly needed:
- Sizes and heights that do not fit the named object (a nightstand 2 m wide,
  a rug 1 m high, a dining table 0.3 m high).
- Placement that makes no sense: chairs far from the table they belong to or
  facing away from it, beds, sofas, wardrobes and shelves floating away from
  the wall they should back onto, objects poking outside the room.
- Facing: seating faces its table or the room, storage faces the room.
Keep every object inside the room, avoid overlaps, keep about 0.6 m walkways
and keep the area in front of doors clear. Do not move an object more than
{max_move} m from its current centre. Do not change objects marked fixed.
Leave correct objects out of the answer.

Return JSON only:
{{"objects": [{{"id": "chair_1", "cx": 1.2, "cy": 2.0, "rotation_deg": 90,
  "w_m": 0.45, "d_m": 0.5, "h_m": 0.85, "reason_ko": "식탁 쪽으로 옮기고 식탁을 보게 돌림"}}]}}
Include only the fields you change, plus id and reason_ko.

ROOM: W = {width} m, D = {depth} m, ceiling = {ceiling} m

OBJECTS (JSON):
{objects}
""".strip()


def enabled() -> bool:
    return os.getenv("GEMINI_SCENE_REFINE", "1").strip().lower() not in {"0", "false", "no", "off"}


def _editable(obj: dict[str, Any]) -> bool:
    return (
        str(obj.get("type")) not in FIXED_TYPES
        and obj.get("source") not in {"user", "selected_product"}
    )


def _payload(graph: dict[str, Any]) -> dict[str, Any]:
    room = graph["room"]
    rows = []
    for obj in graph.get("objects") or []:
        rows.append(
            {
                "id": str(obj["id"]),
                "kind": obj.get("type"),
                "name": obj.get("label"),
                "cx": round(float(obj["cx"]), 2),
                "cy": round(float(obj["cy"]), 2),
                "w_m": round(float(obj["w_m"]), 2),
                "d_m": round(float(obj["d_m"]), 2),
                "h_m": round(float(obj["h_m"]), 2) if obj.get("h_m") else None,
                "rotation_deg": round(float(obj.get("rotation_deg") or 0.0) % 360, 1),
                "wall": obj.get("wall") or "none",
                "fixed": not _editable(obj),
            }
        )
    return {
        "width": round(float(room["width_m"]), 2),
        "depth": round(float(room["depth_m"]), 2),
        "ceiling": round(float(room.get("ceiling_m") or 0.0), 2),
        "objects": rows,
    }


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def apply(graph: dict[str, Any], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """모델 답을 범위 안으로 묶어 반영한다. 바뀐 내용 목록을 돌려준다."""
    room = graph["room"]
    W, D = float(room["width_m"]), float(room["depth_m"])
    ceiling = float(room.get("ceiling_m") or 0.0) or None
    by_id = {str(o.get("id")): o for o in graph.get("objects") or []}
    changes = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        obj = by_id.get(str(row.get("id")))
        if obj is None or not _editable(obj):
            continue
        before = {k: obj.get(k) for k in ("cx", "cy", "w_m", "d_m", "h_m", "rotation_deg")}
        for key in ("w_m", "d_m"):
            value = _number(row.get(key))
            if value is not None:
                # 회전에 따라 어느 벽 방향이 될지 몰라 방의 긴 변까지 허용한다
                obj[key] = round(min(max(value, 0.05), max(W, D)), 3)
        height = _number(row.get("h_m"))
        if height is not None and 0.003 <= height <= (ceiling or 4.0):
            obj["h_m"] = round(height, 3)
            obj["h_source"] = "llm"
        rotation = _number(row.get("rotation_deg"))
        if rotation is not None:
            obj["rotation_deg"] = round(rotation % 360, 1)
        cx, cy = _number(row.get("cx")), _number(row.get("cy"))
        if cx is not None or cy is not None:
            nx = cx if cx is not None else float(obj["cx"])
            ny = cy if cy is not None else float(obj["cy"])
            dx, dy = nx - float(obj["cx"]), ny - float(obj["cy"])
            distance = math.hypot(dx, dy)
            if distance > MAX_MOVE_M:
                # 너무 멀리 옮기려 하면 같은 방향으로 한도까지만 옮긴다
                scale = MAX_MOVE_M / distance
                nx, ny = float(obj["cx"]) + dx * scale, float(obj["cy"]) + dy * scale
            obj["cx"] = round(min(max(nx, 0.0), W), 3)
            obj["cy"] = round(min(max(ny, 0.0), D), 3)
        after = {k: obj.get(k) for k in before}
        if after != before:
            obj["llm_refined"] = True
            changes.append(
                {"id": obj["id"], "before": before, "after": after, "reason_ko": str(row.get("reason_ko") or "")[:120]}
            )
    return changes


def refine(
    client: Any,
    graph: dict[str, Any],
    *,
    model: str,
    cache_dir: Path,
) -> list[dict[str, Any]]:
    """그래프를 제자리에서 고친다. 바뀐 내용 목록(실패하면 빈 목록)."""
    payload = _payload(graph)
    if not any(not row["fixed"] for row in payload["objects"]):
        return []
    key = hashlib.sha256(
        json.dumps({"v": REFINE_VERSION, "model": model, "p": payload}, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:24]
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{key}.json"
    failed_path = cache_dir / f"{key}.failed"
    rows = None
    if cache_path.exists():
        try:
            rows = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            rows = None
    if rows is None:
        if failed_path.exists() and time.time() - failed_path.stat().st_mtime < FAILURE_COOLDOWN_SECONDS:
            return []
        prompt = PROMPT.format(
            max_move=MAX_MOVE_M,
            width=payload["width"],
            depth=payload["depth"],
            ceiling=payload["ceiling"] or "unknown",
            objects=json.dumps(payload["objects"], ensure_ascii=False, indent=1),
        )
        try:
            response = client.models.generate_content(
                model=model,
                contents=[prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    temperature=0.2,
                    max_output_tokens=16000,
                    # 가구 사이 관계(의자와 식탁, 벽과 등)를 따져야 해서 추론을 켠다(기본 medium)
                    thinking_config=thinking_for(model, os.getenv("GEMINI_SCENE_REFINE_THINKING", "").strip() or "medium"),
                ),
            )
            if not response.text:
                raise RuntimeError("Gemini가 배치 보정 결과를 반환하지 않았습니다.")
            _ensure_not_truncated(response)
            text = response.text.strip()
            start, end = text.find("{"), text.rfind("}")
            data = json.loads(text[start : end + 1]) if start >= 0 and end > start else {}
            rows = [row for row in (data.get("objects") or []) if isinstance(row, dict)]
        except Exception as exc:
            print(f"[gemini-scene-refine] 배치 보정 실패, 분석 결과를 그대로 씁니다: {exc}")
            try:
                failed_path.write_text(str(exc)[:500], encoding="utf-8")
            except OSError:
                pass
            return []
        cache_path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    changes = apply(graph, rows)
    if changes:
        graph.setdefault("llm_refinements", []).extend(changes)
        graph.setdefault("history", []).append(
            {"ts": round(time.time(), 3), "source": "llm", "action": "refine", "changed": [c["id"] for c in changes]}
        )
    return changes
