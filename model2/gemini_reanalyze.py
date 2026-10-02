"""신뢰도가 낮은 가구만 다시 분석한다 (항목 17).

방 전체를 다시 분석하면 호출 하나가 모든 가구의 JSON을 다시 만들어 토큰이 크고,
이미 맞게 나온 가구까지 값이 흔들린다. 여기서는 확신이 낮은 가구만 골라
사진에서 그 부분을 잘라 보낸다. 사진 속 위치(photo_box)가 없는 옛 분석 결과면
사진 전체와 가구 설명을 함께 보낸다.

결과는 바로 덮어쓰지 않고 source="ai_refined"로 표시한다. 사람이 확인하기 전까지는
여전히 AI 추정이라는 뜻이다. 사람이 고친 가구(source="user")는 건드리지 않는다.
"""
from __future__ import annotations

import io
import json
import re
import time
from pathlib import Path
from typing import Any

from google.genai import types
from PIL import Image

from . import scene_graph
from .gemini_svg_experiment import _ensure_not_truncated, default_thinking

LOW_CONFIDENCE = 0.6
MAX_OBJECTS = 6
CROP_MARGIN = 0.08  # 잘라 낼 때 주변을 조금 더 포함해 맥락을 남긴다
CROP_MAX_SIDE = 768

PROMPT = """
You previously analyzed this room photo and were unsure about some objects.
Each numbered image is a crop of the photo around one uncertain object (or the
whole photo when no crop was available). Look carefully and answer for each
object id whether it really exists, what it is, and its approximate top-down
size as fractions of the room (same units as before).

Return JSON only:
{"objects": [{"id": "...", "exists": true, "category": "bed", "label_ko": "침대",
  "width": 0.4, "depth": 0.5, "confidence": 0.8, "note_ko": "짧은 근거"}]}

Use one of these categories when possible: bed, sofa, armchair, desk, table,
coffee_table, chair, desk_chair, stool, bench, shelf, cabinet, dresser,
wardrobe, nightstand, rug, mirror, lamp, plant, tv, fridge, washer, curtain,
decor. Keep confidence honest; do not raise it unless the crop shows the object
clearly.

OBJECTS:
""".strip()


def targets(graph: dict[str, Any], ids: list[str] | None = None) -> list[dict[str, Any]]:
    rows = []
    for obj in graph.get("objects") or []:
        if obj.get("source") not in {"ai", "ai_refined"}:
            continue
        if ids is not None:
            if str(obj.get("id")) in ids:
                rows.append(obj)
        elif float(obj.get("confidence") if obj.get("confidence") is not None else 1.0) < LOW_CONFIDENCE:
            rows.append(obj)
    rows.sort(key=lambda o: float(o.get("confidence") or 0.0))
    return rows[:MAX_OBJECTS]


def _crop(image: Image.Image, box: list[float] | None, max_side: int = CROP_MAX_SIDE) -> bytes:
    if box:
        x0, y0, x1, y1 = box
        x0, y0 = max(0.0, x0 - CROP_MARGIN), max(0.0, y0 - CROP_MARGIN)
        x1, y1 = min(1.0, x1 + CROP_MARGIN), min(1.0, y1 + CROP_MARGIN)
        w, h = image.size
        image = image.crop((int(x0 * w), int(y0 * h), max(int(x0 * w) + 8, int(x1 * w)), max(int(y0 * h) + 8, int(y1 * h))))
    scale = min(1.0, max_side / max(image.size))
    if scale < 1.0:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def build_request(photo: Path, objects: list[dict[str, Any]], room: dict[str, Any]) -> list[Any]:
    W, D = float(room["width_m"]), float(room["depth_m"])
    lines = []
    parts: list[Any] = []
    with Image.open(photo) as source:
        image = source.convert("RGB")
        for number, obj in enumerate(objects, start=1):
            plan_w, plan_d = scene_graph.plan_extent(obj)
            lines.append(
                f"{number}. id={obj['id']} | now: {obj.get('category')} '{obj.get('label')}' | "
                f"width {plan_w / W:.2f}, depth {plan_d / D:.2f} | confidence {float(obj.get('confidence') or 0):.2f}"
                + ("" if obj.get("photo_box") else " | (no crop: find it in the whole photo)")
            )
            parts.append(types.Part.from_bytes(data=_crop(image, obj.get("photo_box")), mime_type="image/jpeg"))
    return [PROMPT + "\n" + "\n".join(lines), *parts]


def _parse(text: str) -> list[dict[str, Any]]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.I)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("재분석 응답에서 JSON을 찾지 못했습니다.")
    value = json.loads(cleaned[start : end + 1])
    rows = value.get("objects") if isinstance(value, dict) else None
    if not isinstance(rows, list):
        raise ValueError("재분석 응답 형식이 올바르지 않습니다.")
    return [r for r in rows if isinstance(r, dict) and r.get("id")]


def apply_refinements(graph: dict[str, Any], rows: list[dict[str, Any]], allowed_ids: set[str]) -> list[str]:
    """재분석 결과를 그래프에 반영하고 바뀐 id 목록을 돌려준다. 그래프를 제자리에서 고친다."""
    room = graph["room"]
    W, D = float(room["width_m"]), float(room["depth_m"])
    changed = []
    for row in rows:
        object_id = str(row.get("id"))
        if object_id not in allowed_ids:
            continue
        obj = next((o for o in graph["objects"] if str(o.get("id")) == object_id), None)
        if obj is None or obj.get("source") == "user":
            continue
        before = {k: obj.get(k) for k in ("category", "type", "label", "w_m", "d_m", "confidence")}
        try:
            confidence = max(0.0, min(1.0, float(row.get("confidence", obj.get("confidence") or 0.5))))
        except (TypeError, ValueError):
            confidence = float(obj.get("confidence") or 0.5)
        if row.get("exists") is False:
            # 없다고 판단해도 지우지 않는다. 확신을 낮춰 사용자 확인 목록 맨 위에 올린다
            obj["confidence"] = min(confidence, 0.2)
            obj["refined_note"] = str(row.get("note_ko") or "AI가 다시 봤을 때 보이지 않는다고 판단했어요.")[:120]
        else:
            category = str(row.get("category") or obj.get("category") or "").lower()
            kind = scene_graph.object_type(category)
            if kind not in {"unknown"}:
                obj["category"] = category
                obj["type"] = kind
            if row.get("label_ko"):
                obj["label"] = str(row["label_ko"])[:30]
            try:
                plan_w = max(0.02, min(1.0, float(row["width"]))) * W
                plan_d = max(0.02, min(1.0, float(row["depth"]))) * D
                obj["w_m"], obj["d_m"] = scene_graph.local_from_plan(plan_w, plan_d, float(obj["rotation_deg"]))
            except (KeyError, TypeError, ValueError):
                pass
            obj["confidence"] = confidence
            if row.get("note_ko"):
                obj["refined_note"] = str(row["note_ko"])[:120]
        obj["source"] = "ai_refined"
        graph.setdefault("refinements", []).append(
            {"ts": round(time.time(), 3), "id": object_id, "before": before, "after": {k: obj.get(k) for k in before}}
        )
        changed.append(object_id)
    return changed


def reanalyze(
    client: Any,
    photo: Path,
    graph: dict[str, Any],
    *,
    model: str,
    ids: list[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """확신이 낮은 가구만 다시 묻고 반영한 새 그래프를 돌려준다(원본은 그대로)."""
    import copy

    updated = copy.deepcopy(graph)
    chosen = targets(updated, ids)
    if not chosen:
        return updated, []
    response = client.models.generate_content(
        model=model,
        contents=build_request(Path(photo), chosen, updated["room"]),
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            # pro 계열이면 thinking 토큰도 이 한도를 쓴다
            max_output_tokens=24576,
            thinking_config=default_thinking(model),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini가 재분석 결과를 반환하지 않았습니다.")
    _ensure_not_truncated(response)
    changed = apply_refinements(updated, _parse(response.text), {str(o["id"]) for o in chosen})
    if changed:
        from .placement_solver import solve

        # 크기가 바뀌었을 수 있으니 바뀐 가구만 다시 자리를 맞춘다
        solve(updated, movable_ids=changed, walkway=False)
        for obj in updated["objects"]:
            obj["edit_origin"] = scene_graph._origin(obj)
        scene_graph.append_history(updated, "ai", "reanalyze", objects=changed, requested=len(chosen))
    return scene_graph.sync_legacy(updated), changed
