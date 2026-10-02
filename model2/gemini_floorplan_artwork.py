"""Gemini가 가구별 겉모양(그림)만 그리고, 위치·크기·회전은 Scene Graph가 정한다.

예전에는 Gemini가 평면도 전체를 그려서 그림은 풍성했지만 가구 위치를 Gemini가
다시 정해 3D와 어긋났다. 여기서는 역할을 나눈다.

  - Gemini: 가구마다 '자기 크기의 상자' 안에 위에서 본 그림을 그린다. 사진의
    색·재질·무늬를 따른다. 바닥 무늬도 정의한다.
  - 코드: 그 그림을 Scene Graph의 cx·cy·w_m·d_m·rotation_deg에 맞춰 끼운다.
    그림의 외곽 박스를 가구 바닥면에 정확히 맞추므로, Gemini가 상자를 조금
    벗어나 그려도 2D 외곽은 3D와 같다.

캐시는 사진·모델·가구 정체성(id, 종류, 이름, 색, 재질, 무늬)으로 잡는다. 위치·
크기·회전은 키에 넣지 않는다. 가구를 옮기거나 돌려도 그림을 다시 받지 않는다.

Gemini가 실패하면 빈 결과를 돌려준다. 렌더러는 그 가구를 기본 모양으로 그린다.
"""
from __future__ import annotations

import hashlib
import json
import time
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from google.genai import types

from .gemini_svg_experiment import _ensure_not_truncated, _extract_svg, _image_part, minimal_thinking

# 2: 3D로 세우기 위한 도형별 높이 정보(data-z0 등)를 함께 받는다
ARTWORK_VERSION = "2"
FAILURE_COOLDOWN_SECONDS = 10 * 60
SVG_NS = "http://www.w3.org/2000/svg"
# 그림 조각 크기(px). 가구 실측 비율을 유지하되 긴 변을 이 정도로 맞춰 그리게 한다.
SYMBOL_LONG_SIDE_PX = 240.0
SKIP_TYPES = {"door", "window"}  # 벽 구조물은 코드가 그린다

# 3D 돌출에 쓰는 도형별 높이 규칙. 방 가구 그림과 상품 아이콘이 같이 쓴다(product_icon_svg)
SOLID_HINTS = """
The same artwork is also extruded into a 3D model, so describe the height of
every part. Put these attributes on each drawn element (or on a <g> that
groups elements sharing the same values):
- data-z0 / data-z1: bottom and top of that part as a FRACTION of the object's
  total height (0 = floor, 1 = its highest point; total height is given per
  object in metres). Examples: sofa seat base 0-0.5, back rest 0-1, arms
  0-0.75, seat cushions 0.5-0.62; bed frame 0-0.45, mattress 0.45-0.8,
  pillows 0.8-0.92, headboard 0-1; desk/table top 0.94-1; chair seat
  0.48-0.53, chair back 0.53-1; wardrobe or shelf body 0-1.
- Surface details lying on a part (stitching, wood grain, folds, patterns,
  book spines seen from above) use z0 = z1 = the top of that part.
- data-soft: 0 to 1, how rounded the edges are (0 crisp wood or metal,
  0.5 upholstered seat, 1 pillow or cushion).
- data-taper: 0 to 0.5, how much narrower the top is than the bottom (lamp
  shades, tapered legs). Omit when 0.
- data-only3d="1": structural parts hidden under the top surface that must
  exist in 3D but are not visible from above: table and chair legs, bed legs,
  the pedestal of a stool or lamp. Draw them at their real top-down position;
  they are removed from the 2D plan.
- data-3d="skip": pure lighting effects (soft shadows, glows, highlights).
- Every physical part must be a FILLED shape (rect, circle, ellipse, polygon
  or closed path with a fill). Draw legs, poles and posts as small filled
  rects or circles at their footprint position, never as <line> or
  stroke-only paths: lines have no area and cannot be built in 3D.
""".strip()

ARTWORK_PROMPT = """
You are an expert interior illustrator and SVG artist. Draw top-down artwork
for each piece of furniture listed below, matching the supplied interior photo.

The target style is a warm hand-drawn architectural interior illustration:
- Directly overhead orthographic view of each object, not an oblique perspective.
- Objects feel volumetric through their own construction: curved cushions,
  folded blankets with soft wave lines, pillows, visible frames, headboards,
  drawer fronts, chair backs, cabinet tops, lamps, books and small decor.
- Restrained soft local shadows, gentle gradients and highlights. Thin, light
  outlines. A harmonious warm palette (cream, beige, light wood) that still
  preserves each object's real color, material and distinctive pattern from
  the photograph.
- Do NOT fake volume with a large offset copy of the silhouette.

Each object has its own local drawing box, given as W x H pixels:
- Draw the object inside x from 0 to W and y from 0 to H, filling the box.
- y=0 is the BACK of the object (headboard, sofa back, the side against the
  wall). y=H is the front, where a person approaches it.
- Do not draw labels or text. Do not draw the room floor inside object boxes.

{solid_hints}

Return ONLY one SVG document, no Markdown, structured exactly like this:
<svg xmlns="http://www.w3.org/2000/svg">
  <defs>
    <!-- shared gradients/patterns; MUST include a floor pattern with
         id="floor-pattern" (light wood planks or tile matching the photo) -->
  </defs>
  <g id="room-style" data-floor-color="#d9c3a5" data-wall-color="#8a7766"/>
  <g id="OBJECT_ID" data-w="W" data-h="H"> ...object artwork... </g>
  ... one <g> per listed object, id exactly as given ...
</svg>

Technical requirements:
- Self-contained SVG only. No external images, URLs, fonts, scripts,
  foreignObject, animation or embedded raster data.
- Prefer compact paths, reusable gradients and patterns. Keep the whole
  document under 12,000 output tokens.

OBJECTS (id, kind, Korean name, W x H, total height, color / material / pattern hints):
""".strip().replace("{solid_hints}", SOLID_HINTS)


def symbol_box(obj: dict[str, Any]) -> tuple[int, int]:
    """가구 기준 폭·깊이 비율을 유지한 그림 상자 크기(px)."""
    w, d = max(float(obj["w_m"]), 0.05), max(float(obj["d_m"]), 0.05)
    scale = SYMBOL_LONG_SIDE_PX / max(w, d)
    return max(24, int(round(w * scale))), max(24, int(round(d * scale)))


def _targets(graph: dict[str, Any]) -> list[dict[str, Any]]:
    return [o for o in graph.get("objects") or [] if str(o.get("type")) not in SKIP_TYPES]


def cache_key(image_path: Path, graph: dict[str, Any], model: str) -> str:
    identity = [
        [
            str(o.get("id")),
            str(o.get("type")),
            str(o.get("label")),
            str(o.get("color") or ""),
            str(o.get("material") or ""),
            str(o.get("pattern") or ""),
        ]
        for o in _targets(graph)
    ]
    payload = json.dumps(
        {
            "v": ARTWORK_VERSION,
            "model": model,
            "photo": hashlib.sha256(Path(image_path).read_bytes()).hexdigest(),
            "objects": identity,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _height_m(obj: dict[str, Any]) -> float:
    """3D에서 쓰는 높이와 같은 값(공간 분석이 사진에서 추정한 h_m)을 알려 준다."""
    return float(obj.get("h_m") or 0.0)


def _prompt(graph: dict[str, Any]) -> str:
    lines = []
    for obj in _targets(graph):
        w, h = symbol_box(obj)
        hints = " / ".join(
            str(obj.get(key))
            for key in ("color", "material", "pattern")
            if obj.get(key)
        )
        height = _height_m(obj)
        lines.append(f'- {obj["id"]} | {obj.get("type")} | {obj.get("label")} | {w} x {h} | {height:.2f} m | {hints or "-"}')
    return ARTWORK_PROMPT + "\n" + "\n".join(lines)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def parse_artwork(svg_text: str, expected_ids: list[str]) -> dict[str, Any]:
    """Gemini SVG → {"defs": str, "objects": {id: {"markup", "w", "h"}}, "room": {...}}.

    내부 id는 모두 'gx-' 접두사를 붙여 다시 쓴다. 평면도의 가구 id(<g id="bed_1">)와
    겹치면 편집 기능이 엉뚱한 요소를 잡는다(AGENTS.md 7.2).
    """
    svg = _extract_svg(svg_text)  # 스크립트·외부 참조 차단
    root = ET.fromstring(svg)
    ids = {el.get("id") for el in root.iter() if el.get("id")}
    rename = {old: f"gx-{old}" for old in ids}

    def rewrite(element: ET.Element) -> None:
        for el in element.iter():
            if el.get("id") in rename:
                el.set("id", rename[el.get("id")])
            for name, value in list(el.attrib.items()):
                if "url(#" in value or value.startswith("#"):
                    el.set(
                        name,
                        re.sub(
                            r"url\(#([^)]+)\)",
                            lambda m: f"url(#{rename.get(m.group(1), m.group(1))})",
                            value,
                        ),
                    )
            # 텍스트는 그리지 말라고 했지만 혹시 넣었으면 뺀다. 라벨은 코드가 그린다
            for child in list(el):
                if _local(child.tag) == "text":
                    el.remove(child)

    defs_markup = ""
    room: dict[str, str] = {}
    objects: dict[str, dict[str, Any]] = {}
    wanted = set(expected_ids)
    for child in list(root):
        tag = _local(child.tag)
        child_id = child.get("id")
        if tag == "defs":
            rewrite(child)
            defs_markup = "".join(ET.tostring(item, encoding="unicode") for item in child)
        elif tag == "g" and child_id == "room-style":
            room = {
                "floor_color": str(child.get("data-floor-color") or ""),
                "wall_color": str(child.get("data-wall-color") or ""),
            }
        elif tag == "g" and child_id in wanted:
            try:
                w = float(child.get("data-w") or 0)
                h = float(child.get("data-h") or 0)
            except ValueError:
                w = h = 0.0
            rewrite(child)
            child.attrib.pop("id", None)
            inner = "".join(ET.tostring(item, encoding="unicode") for item in child)
            if inner.strip():
                objects[child_id] = {"markup": inner, "w": w, "h": h}
    # ET.tostring이 붙이는 네임스페이스 접두사를 걷어 낸다(평면도 SVG에 그대로 끼우기 위해)
    def clean(markup: str) -> str:
        return markup.replace(f' xmlns:ns0="{SVG_NS}"', "").replace("ns0:", "").replace(f' xmlns="{SVG_NS}"', "")

    return {
        "version": ARTWORK_VERSION,
        "defs": clean(defs_markup),
        "floor_pattern_id": "gx-floor-pattern" if "floor-pattern" in ids else None,
        "room": room,
        "objects": {k: {**v, "markup": clean(v["markup"])} for k, v in objects.items()},
    }


def room_style(artwork: dict[str, Any] | None) -> dict[str, Any]:
    """2D 그림의 바닥색·벽색·바닥 무늬. 3D 바닥과 벽을 2D와 같게 칠하는 데 쓴다.

    바닥 무늬는 <pattern id="gx-floor-pattern">과 그 무늬가 참조하는 정의만 떼어 낸
    SVG 조각이다. 브라우저가 이걸 이미지로 그려 3D 바닥에 타일처럼 깐다.
    """
    if not artwork:
        return {}
    style: dict[str, Any] = {}
    room = artwork.get("room") or {}
    for key in ("floor_color", "wall_color"):
        value = str(room.get(key) or "")
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            style[key] = value.lower()
    pattern_id = artwork.get("floor_pattern_id")
    defs = str(artwork.get("defs") or "")
    if pattern_id and defs:
        try:
            root = ET.fromstring(f'<defs xmlns="{SVG_NS}">{defs}</defs>')
        except ET.ParseError:
            root = None
        if root is not None:
            by_id = {child.get("id"): child for child in list(root) if child.get("id")}
            wanted = [pattern_id]
            keep: list[str] = []
            seen: set[str] = set()
            while wanted:
                current = wanted.pop()
                if current in seen or current not in by_id:
                    continue
                seen.add(current)
                text = ET.tostring(by_id[current], encoding="unicode")
                keep.append(text)
                wanted.extend(re.findall(r"url\(#([^)]+)\)", text))
            markup = "".join(keep).replace(f' xmlns:ns0="{SVG_NS}"', "").replace("ns0:", "").replace(f' xmlns="{SVG_NS}"', "")
            if markup and len(markup) < 20000:
                style["floor_pattern_svg"] = markup
                style["floor_pattern_id"] = pattern_id
    return style


def generate_artwork(
    client: Any,
    image_path: Path,
    graph: dict[str, Any],
    *,
    model: str,
    cache_dir: Path,
) -> dict[str, Any]:
    """가구별 그림을 받아 온다. 캐시가 있으면 쓰고, 실패하면 빈 결과."""
    targets = _targets(graph)
    if not targets:
        return {}
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = cache_key(image_path, graph, model)
    cache_path = cache_dir / f"{key}.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    failed_path = cache_dir / f"{key}.failed"
    if failed_path.exists() and time.time() - failed_path.stat().st_mtime < FAILURE_COOLDOWN_SECONDS:
        # 같은 입력으로 방금 실패했다면 잠시 다시 부르지 않는다(유료 호출 낭비 방지).
        # 기한 없이 막으면 서버 혼잡 한 번에 그 방은 영영 기본 모양으로만 그려진다
        return {}

    try:
        response = client.models.generate_content(
            model=model,
            contents=[_prompt(graph), _image_part(Path(image_path))],
            config=types.GenerateContentConfig(
                response_mime_type="text/plain",
                temperature=0.3,
                # 3D 높이 속성이 붙어 그림 하나가 조금 길어졌다
                max_output_tokens=20000,
                # 배치는 이미 정해져 있고 그림만 그리는 작업이라 thinking이 필요 없다.
                # 켜 두면 thinking 토큰이 출력 한도를 먹어 SVG가 잘린다.
                thinking_config=minimal_thinking(model),
            ),
        )
        if not response.text:
            raise RuntimeError("Gemini가 그림 SVG를 반환하지 않았습니다.")
        _ensure_not_truncated(response)
        artwork = parse_artwork(response.text, [str(o["id"]) for o in targets])
        if not artwork["objects"]:
            raise RuntimeError("응답에 가구 그림이 하나도 없습니다.")
    except Exception as exc:
        print(f"[gemini-floorplan-artwork] 생성 실패: {exc}")
        try:
            failed_path.write_text(str(exc)[:500], encoding="utf-8")
        except OSError:
            pass
        return {}

    artwork["key"] = key
    artwork["model"] = model
    cache_path.write_text(json.dumps(artwork, ensure_ascii=False), encoding="utf-8")
    return artwork
