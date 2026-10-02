"""2D 가구 그림(SVG)을 3D로 세우기 위한 자료를 만든다.

3D는 이미지 모델이 그린 평면 그림을 세우던 방식에서, 2D 평면도의 위에서 본 SVG를
three.js SVGLoader로 밀어 올리는(돌출) 방식으로 바꿨다. 같은 SVG에서 나오므로 2D와
3D의 모양·색이 같고, 3D를 위한 API 호출이 따로 없다.

위에서 본 그림에는 높이가 없다. 그래서 2D 그림을 그릴 때 Gemini가 도형마다 높이
정보(data-z0·data-z1 등, gemini_floorplan_artwork.ARTWORK_PROMPT)를 함께 적는다.
그 정보가 없는 예전 그림이나 코드가 그린 기본 모양은 화면이 종류별 규칙으로 높이를
정한다(floorplan_3d.js buildSolid).

여기서 하는 일
  - 그 가구의 SVG 조각을 고른다(Gemini 그림, 없으면 2D 렌더러의 기본 모양).
  - 그라데이션·무늬 칠을 대표색으로 바꾼다. SVGLoader는 url(#...) 칠을 못 읽는다.
  - 2D와 같은 기준 범위(box)를 함께 준다. 화면은 이 범위를 가구 바닥면에 맞춘다.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Any

from . import scene_render_2d

SVG_NS = "http://www.w3.org/2000/svg"
# 기본 모양을 그릴 때의 축척. 화면이 box로 다시 맞추므로 값 자체는 상관없다
CODE_PX_PER_M = 100.0
# 조명 효과(그림자·빛)로 보이는 칠. 물체가 아니라 3D에서 세우면 덩어리가 된다
LIGHT_EFFECT = re.compile(r"shadow|glow|highlight|shine|light", re.I)
SKIP_TYPES = {"door", "window"}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paint_colors(defs: str) -> dict[str, str]:
    """그라데이션·무늬 id → 대표색. 그라데이션은 가운데 정지점, 무늬는 첫 칠."""
    if not defs:
        return {}
    try:
        root = ET.fromstring(f'<defs xmlns="{SVG_NS}">{defs}</defs>')
    except ET.ParseError:
        return {}
    colors: dict[str, str] = {}
    links: dict[str, str] = {}
    for el in root.iter():
        paint_id = el.get("id")
        if not paint_id:
            continue
        tag = _local(el.tag)
        if tag in {"linearGradient", "radialGradient"}:
            stops = [
                stop.get("stop-color") or _style_value(stop.get("style"), "stop-color")
                for stop in el
                if _local(stop.tag) == "stop"
            ]
            stops = [s for s in stops if s]
            if stops:
                colors[paint_id] = stops[len(stops) // 2]
            href = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href")
            if not stops and href and href.startswith("#"):
                links[paint_id] = href[1:]
        elif tag == "pattern":
            for child in el.iter():
                fill = child.get("fill")
                if fill and fill not in {"none", "transparent"} and not fill.startswith("url("):
                    colors[paint_id] = fill
                    break
    # 정지점을 다른 그라데이션에서 빌려 쓰는 경우
    for paint_id, target in links.items():
        if target in colors:
            colors[paint_id] = colors[target]
    return colors


def _style_value(style: str | None, name: str) -> str | None:
    match = re.search(rf"(?:^|;)\s*{re.escape(name)}\s*:\s*([^;]+)", style or "")
    return match.group(1).strip() if match else None


def flatten_paints(markup: str, defs: str) -> str:
    """fill="url(#gx-…)"를 대표색으로 바꾼다. 조명 효과 칠이면 3D에서 빼도록 표시한다."""
    colors = _paint_colors(defs)

    def paint(match: re.Match[str]) -> str:
        attr, paint_id = match.group(1), match.group(2)
        color = colors.get(paint_id, "#b7aa98")
        skip = ' data-3d="skip"' if attr == "fill" and LIGHT_EFFECT.search(paint_id) else ""
        return f'{attr}="{color}"{skip}'

    # 첫 참조만 본다. Gemini가 가끔 url(#a, url(#b)) 같은 잘못된 값을 쓴다
    markup = re.sub(r'\b(fill|stroke)="\s*url\(#([^)",\s]+)[^"]*"', paint, markup)
    # style="fill:url(#…)" 형태
    return re.sub(
        r"(fill|stroke)\s*:\s*url\(#([^)]+)\)",
        lambda m: f"{m.group(1)}:{colors.get(m.group(2), '#b7aa98')}",
        markup,
    )


def object_solid(obj: dict[str, Any], artwork: dict[str, Any] | None) -> dict[str, Any] | None:
    """{"svg": 단독 SVG 문서, "box": [x0, y0, x1, y1], "source": "gemini"|"code"}.

    box는 2D 렌더러가 그림을 바닥면에 맞출 때 쓰는 범위와 같다(3D 전용 부품 제외).
    그래야 3D의 외곽이 2D와 같다.
    """
    kind = str(obj.get("type") or "unknown")
    if kind in SKIP_TYPES:
        return None
    drawn = ((artwork or {}).get("objects") or {}).get(str(obj.get("id")))
    if drawn and drawn.get("markup"):
        markup = flatten_paints(str(drawn["markup"]), str((artwork or {}).get("defs") or ""))
        source = "gemini"
    else:
        # Gemini 그림이 없는 가구(추가한 상품 등)는 2D에 실제로 그려지는 기본 모양을 쓴다
        w = float(obj.get("w_m") or 0.5) * CODE_PX_PER_M
        d = float(obj.get("d_m") or 0.5) * CODE_PX_PER_M
        shape = scene_render_2d.SHAPES.get(kind, scene_render_2d._generic)
        markup = shape(w, d, scene_render_2d._color(obj))
        source = "code"
    box = scene_render_2d._artwork_box(scene_render_2d.strip_only3d(markup))
    if box is None:
        return None
    return {
        "svg": f'<svg xmlns="{SVG_NS}">{markup}</svg>',
        "box": [round(v, 3) for v in box],
        "source": source,
    }
