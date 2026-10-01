"""Scene Graph → 2D 평면도 SVG. 좌표를 다시 계산하지 않고 그대로 그린다.

기존에는 Gemini가 분석 JSON을 보고 SVG를 새로 그렸다. 그림은 보기 좋았지만
가구 위치를 Gemini가 다시 정해서 3D(별도 배치 엔진)와 어긋났다. 여기서는
Scene Graph의 cx·cy·w_m·d_m·rotation_deg를 픽셀로 환산만 한다. 3D도 같은 값을
쓰므로 2D와 3D의 위치·크기·회전이 정의상 같다.

기존 편집 기능과의 계약
  - 가구마다 <g id="{scene id}">, 라벨은 <g id="label-{scene id}"> (AGENTS.md 7.2)
  - 가구 본체는 <g transform="translate(cx cy) rotate(r)"> 안에 원점 중심으로 그린다.
    편집기(prepare_floorplan_edit_markup)가 중심을 기준으로 감싸 움직인다.
  - 바닥은 transform 없는 <rect>이고 가로·세로 500px 이상이다. web_floorplan의
    _floor_box()가 "x>0, y>0, 500px 이상인 가장 작은 rect"로 바닥을 찾는다.
    가구 rect는 원점 중심이라 x가 음수가 되어 바닥으로 오인되지 않는다.
"""
from __future__ import annotations

import html
import math
import re
from typing import Any

from . import scene_graph

MARGIN = 90.0
LOW_CONFIDENCE = 0.6
FONT = "Pretendard, 'Noto Sans KR', 'Malgun Gothic', sans-serif"

TYPE_COLORS = {
    "bed": "#e9d6cf",
    "sofa": "#b9a089",
    "desk": "#c9a77c",
    "table": "#c39a6b",
    "low_table": "#b98f5f",
    "chair": "#8d7b6c",
    "desk_chair": "#55555c",
    "floor_chair": "#9b8a79",
    "stool": "#a08b75",
    "bench": "#a08b75",
    "shelf": "#a07a52",
    "cabinet": "#a3815b",
    "dresser": "#a3815b",
    "wardrobe": "#9b7a55",
    "vanity": "#b5966f",
    "nightstand": "#a88660",
    "rug": "#d9cdb8",
    "mirror": "#cdd8de",
    "lamp": "#efd98f",
    "plant": "#7e9c6b",
    "tv": "#2f2f34",
    "fridge": "#d9dcde",
    "washer": "#e3e6e8",
    "aircon": "#eef1f2",
    "curtain": "#d8cbb9",
    "door": "#e2d3bb",
    "window": "#bcd6e0",
    "decor": "#b7aa98",
    "unknown": "#b7aa98",
}

# 겹쳐 그릴 때 아래에 깔릴수록 작은 값
LAYER = {"rug": 0, "door": 1, "window": 1, "curtain": 1}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")


def _color(obj: dict[str, Any]) -> str:
    value = str(obj.get("color") or "")
    if _HEX.match(value):
        return value
    return TYPE_COLORS.get(str(obj.get("type")), TYPE_COLORS["unknown"])


def _shade(hex_color: str, factor: float) -> str:
    value = hex_color.lstrip("#")
    r, g, b = (int(value[i : i + 2], 16) for i in (0, 2, 4))
    if factor < 1:
        r, g, b = (int(c * factor) for c in (r, g, b))
    else:
        r, g, b = (int(c + (255 - c) * (factor - 1)) for c in (r, g, b))
    return f"#{max(0, min(255, r)):02x}{max(0, min(255, g)):02x}{max(0, min(255, b)):02x}"


def _f(value: float) -> str:
    return f"{value:.1f}"


def _rect(x: float, y: float, w: float, h: float, fill: str, *, rx: float = 0, stroke: str = "#5b4a3c", sw: float = 1.4, extra: str = "") -> str:
    return (
        f'<rect x="{_f(x)}" y="{_f(y)}" width="{_f(max(w, 0.5))}" height="{_f(max(h, 0.5))}" '
        f'rx="{_f(rx)}" fill="{fill}" stroke="{stroke}" stroke-width="{sw}"{extra}/>'
    )


# ---------------------------------------------------------------- 타입별 모양
# 모든 함수는 원점 중심, 폭 w(뒷면과 나란한 방향), 깊이 d. 뒷면은 y=-d/2.

def _bed(w: float, d: float, c: str) -> str:
    head = min(d * 0.08, 14)
    pillow_h = min(d * 0.14, 28)
    parts = [
        _rect(-w / 2, -d / 2, w, d, _shade(c, 0.82), rx=4),
        _rect(-w / 2, -d / 2, w, head, _shade(c, 0.6), rx=3),
        _rect(-w / 2 + 4, -d / 2 + head + 2, w - 8, d - head - 6, "#fbf8f4", rx=6, sw=1),
    ]
    pillows = 2 if w > 90 else 1
    gap = 6
    pw = (w - 16 - gap * (pillows - 1)) / pillows
    for i in range(pillows):
        parts.append(_rect(-w / 2 + 8 + i * (pw + gap), -d / 2 + head + 8, pw, pillow_h, "#ffffff", rx=6, sw=1))
    fold = -d / 2 + head + pillow_h + 18
    parts.append(_rect(-w / 2 + 4, fold, w - 8, d / 2 - fold - 4, c, rx=6, sw=1))
    parts.append(f'<line x1="{_f(-w / 2 + 6)}" y1="{_f(fold + 8)}" x2="{_f(w / 2 - 6)}" y2="{_f(fold + 8)}" stroke="{_shade(c, 0.75)}" stroke-width="1.2"/>')
    return "".join(parts)


def _sofa(w: float, d: float, c: str) -> str:
    back = d * 0.28
    arm = min(w * 0.12, d * 0.3)
    parts = [
        _rect(-w / 2, -d / 2, w, d, _shade(c, 0.8), rx=8),
        _rect(-w / 2, -d / 2, w, back, _shade(c, 0.68), rx=8),
        _rect(-w / 2, -d / 2, arm, d, _shade(c, 0.72), rx=7),
        _rect(w / 2 - arm, -d / 2, arm, d, _shade(c, 0.72), rx=7),
    ]
    seats = max(1, min(4, int(round((w - 2 * arm) / max(d * 0.75, 1)))))
    sw_ = (w - 2 * arm - 4) / seats
    for i in range(seats):
        parts.append(_rect(-w / 2 + arm + 2 + i * sw_, -d / 2 + back + 1, sw_ - 2, d - back - 4, c, rx=6, sw=1))
    return "".join(parts)


def _chair(w: float, d: float, c: str) -> str:
    back = max(d * 0.18, 4)
    return _rect(-w / 2, -d / 2 + back * 0.6, w, d - back * 0.6, c, rx=w * 0.18) + _rect(
        -w / 2, -d / 2, w, back, _shade(c, 0.7), rx=back / 2
    )


def _desk_chair(w: float, d: float, c: str) -> str:
    r = min(w, d) / 2
    return (
        f'<circle cx="0" cy="{_f(d * 0.06)}" r="{_f(r * 0.86)}" fill="{c}" stroke="#3c3c40" stroke-width="1.4"/>'
        + _rect(-w * 0.38, -d / 2, w * 0.76, d * 0.22, _shade(c, 0.7), rx=d * 0.1, stroke="#3c3c40")
    )


def _table(w: float, d: float, c: str) -> str:
    inset = min(w, d) * 0.08
    return _rect(-w / 2, -d / 2, w, d, c, rx=3) + _rect(
        -w / 2 + inset, -d / 2 + inset, w - 2 * inset, d - 2 * inset, "none", rx=2, stroke=_shade(c, 0.78), sw=1
    )


def _storage(w: float, d: float, c: str, *, doors: int | None = None) -> str:
    parts = [_rect(-w / 2, -d / 2, w, d, c, rx=2)]
    count = doors or max(1, min(5, int(round(w / max(d * 1.6, 1)))))
    for i in range(1, count):
        x = -w / 2 + w * i / count
        parts.append(f'<line x1="{_f(x)}" y1="{_f(-d / 2 + 2)}" x2="{_f(x)}" y2="{_f(d / 2 - 2)}" stroke="{_shade(c, 0.7)}" stroke-width="1"/>')
    # 앞면(문이 열리는 쪽) 표시
    parts.append(f'<line x1="{_f(-w / 2 + 3)}" y1="{_f(d / 2 - 3)}" x2="{_f(w / 2 - 3)}" y2="{_f(d / 2 - 3)}" stroke="{_shade(c, 0.6)}" stroke-width="2"/>')
    return "".join(parts)


def _shelf(w: float, d: float, c: str) -> str:
    parts = [_rect(-w / 2, -d / 2, w, d, c, rx=1)]
    books = max(2, int(w / 9))
    for i in range(books):
        x = -w / 2 + 3 + i * (w - 6) / books
        tone = _shade(c, 0.55 + 0.35 * ((i * 37) % 7) / 7)
        parts.append(_rect(x, -d / 2 + 2, (w - 6) / books - 1, d * 0.62, tone, sw=0.4))
    return "".join(parts)


def _rug(w: float, d: float, c: str) -> str:
    return _rect(-w / 2, -d / 2, w, d, c, rx=6, stroke=_shade(c, 0.8), sw=1.2, extra=' opacity="0.9"') + _rect(
        -w / 2 + 8, -d / 2 + 8, w - 16, d - 16, "none", rx=4, stroke=_shade(c, 0.72), sw=1.6, extra=' stroke-dasharray="6 4"'
    )


def _lamp(w: float, d: float, c: str) -> str:
    # 바닥 영역(w×d)을 꽉 채우는 타원으로 그려 3D 바닥면과 외곽이 같게 한다
    return (
        f'<ellipse rx="{_f(w / 2)}" ry="{_f(d / 2)}" fill="{_shade(c, 1.15)}" stroke="#8a7a4a" stroke-width="1.2"/>'
        f'<ellipse rx="{_f(w * 0.18)}" ry="{_f(d * 0.18)}" fill="{_shade(c, 0.85)}" stroke="none"/>'
    )


def _plant(w: float, d: float, c: str) -> str:
    r = min(w, d) / 2
    leaves = "".join(
        f'<ellipse cx="{_f(math.cos(a) * r * 0.45)}" cy="{_f(math.sin(a) * r * 0.45)}" rx="{_f(r * 0.5)}" ry="{_f(r * 0.28)}" '
        f'transform="rotate({math.degrees(a):.0f} {_f(math.cos(a) * r * 0.45)} {_f(math.sin(a) * r * 0.45)})" fill="{_shade(c, 0.9 + 0.1 * (i % 2))}" stroke="{_shade(c, 0.6)}" stroke-width="0.8"/>'
        for i, a in enumerate(math.radians(k * 60) for k in range(6))
    )
    pot = f'<ellipse rx="{_f(w / 2)}" ry="{_f(d / 2)}" fill="{_shade(c, 1.35)}" stroke="{_shade(c, 0.7)}" stroke-width="0.8" opacity="0.55"/>'
    return pot + f'<circle r="{_f(r * 0.55)}" fill="#a8835c" stroke="#7a5c3e" stroke-width="1"/>' + leaves


def _flat(w: float, d: float, c: str) -> str:
    return _rect(-w / 2, -d / 2, w, d, c, rx=1.5)


def _appliance(w: float, d: float, c: str, mark: str) -> str:
    body = _rect(-w / 2, -d / 2, w, d, c, rx=3, stroke="#7b8288")
    if mark == "washer":
        return body + f'<circle r="{_f(min(w, d) * 0.3)}" fill="none" stroke="#7b8288" stroke-width="1.4"/>'
    return body + f'<line x1="{_f(-w / 2 + 3)}" y1="{_f(d * 0.1)}" x2="{_f(w / 2 - 3)}" y2="{_f(d * 0.1)}" stroke="#7b8288" stroke-width="1.2"/>'


def _generic(w: float, d: float, c: str) -> str:
    return _rect(-w / 2, -d / 2, w, d, c, rx=min(w, d) * 0.15)


SHAPES = {
    "bed": _bed,
    "sofa": _sofa,
    "chair": _chair,
    "floor_chair": _chair,
    "stool": lambda w, d, c: f'<ellipse rx="{_f(w / 2)}" ry="{_f(d / 2)}" fill="{c}" stroke="#5b4a3c" stroke-width="1.4"/>',
    "desk_chair": _desk_chair,
    "desk": _table,
    "table": _table,
    "low_table": _table,
    "bench": _table,
    "shelf": _shelf,
    "cabinet": _storage,
    "dresser": lambda w, d, c: _storage(w, d, c, doors=2),
    "wardrobe": _storage,
    "vanity": _storage,
    "nightstand": lambda w, d, c: _storage(w, d, c, doors=1),
    "rug": _rug,
    "lamp": _lamp,
    "plant": _plant,
    "mirror": _flat,
    "tv": _flat,
    "aircon": _flat,
    "curtain": _flat,
    "fridge": lambda w, d, c: _appliance(w, d, c, "fridge"),
    "washer": lambda w, d, c: _appliance(w, d, c, "washer"),
}


def _door(w: float, d: float, c: str) -> str:
    # 벽에 붙은 문: 문짝 + 열리는 궤적. 방 안쪽(+y)으로 열린다고 본다.
    leaf = w
    return (
        f'<rect x="{_f(-w / 2)}" y="{_f(-d / 2 - 4)}" width="{_f(w)}" height="{_f(d + 8)}" fill="#fbf8f2" stroke="none"/>'
        f'<line x1="{_f(-w / 2)}" y1="0" x2="{_f(-w / 2)}" y2="{_f(leaf)}" stroke="#5b4a3c" stroke-width="2.2" data-annotation="true"/>'
        f'<path d="M{_f(-w / 2)} {_f(leaf)} A{_f(leaf)} {_f(leaf)} 0 0 0 {_f(w / 2)} 0" fill="none" stroke="#8c7a66" stroke-width="1" stroke-dasharray="4 3" data-annotation="true"/>'
    )


def _window(w: float, d: float, c: str) -> str:
    return (
        f'<rect x="{_f(-w / 2)}" y="{_f(-d / 2 - 5)}" width="{_f(w)}" height="{_f(d + 10)}" fill="#f4fafc" stroke="#6f8f9c" stroke-width="1.2"/>'
        f'<line x1="{_f(-w / 2)}" y1="0" x2="{_f(w / 2)}" y2="0" stroke="#6f8f9c" stroke-width="1.2"/>'
    )


# ---------------------------------------------------------------- 렌더

def layout_metrics(graph: dict[str, Any]) -> dict[str, float]:
    """픽셀 축척과 바닥 위치. 편집·테스트에서 같은 환산을 쓰도록 공개한다."""
    W = float(graph["room"]["width_m"])
    D = float(graph["room"]["depth_m"])
    # 짧은 변이 520px 이상이어야 _floor_box가 바닥을 찾는다
    px = max(520.0 / min(W, D), 880.0 / max(W, D))
    return {
        "px_per_m": px,
        "floor_x": MARGIN,
        "floor_y": MARGIN,
        "floor_w": W * px,
        "floor_h": D * px,
        "canvas_w": W * px + 2 * MARGIN,
        "canvas_h": D * px + 2 * MARGIN,
    }


def render_svg(layout: dict[str, Any], *, title: str | None = None) -> str:
    graph = scene_graph.ensure(layout) if not _is_synced(layout) else layout
    m = layout_metrics(graph)
    px = m["px_per_m"]
    fx, fy, fw, fh = m["floor_x"], m["floor_y"], m["floor_w"], m["floor_h"]
    room = graph["room"]
    floor_color = room.get("floor_color") if _HEX.match(str(room.get("floor_color") or "")) else "#d8b993"
    wall_color = "#3d342c"

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_f(m["canvas_w"])} {_f(m["canvas_h"])}" '
        f'width="{_f(m["canvas_w"])}" height="{_f(m["canvas_h"])}" data-renderer="scene_graph" '
        f'data-room-width-m="{room["width_m"]}" data-room-depth-m="{room["depth_m"]}" data-px-per-m="{px:.4f}">',
        f"<title>{html.escape(title or '평면도')}</title>",
        "<defs>"
        '<pattern id="sg-planks" width="40" height="160" patternUnits="userSpaceOnUse">'
        f'<rect x="0" y="0" width="40" height="160" fill="{floor_color}"/>'
        f'<path d="M0 0V160M40 0V160M0 80H40" stroke="{_shade(floor_color, 0.88)}" stroke-width="1"/>'
        "</pattern>"
        '<filter id="sg-shadow" x="-20%" y="-20%" width="140%" height="140%">'
        '<feDropShadow dx="1.5" dy="2.5" stdDeviation="2.2" flood-color="#000" flood-opacity="0.18"/>'
        "</filter>"
        "</defs>",
        f'<rect x="0" y="0" width="{_f(m["canvas_w"])}" height="{_f(m["canvas_h"])}" fill="#faf7f2" stroke="none"/>',
        # 벽(바깥 테두리) — 바닥보다 커서 _floor_box의 '가장 작은 rect'에 걸리지 않는다
        f'<rect x="{_f(fx - 14)}" y="{_f(fy - 14)}" width="{_f(fw + 28)}" height="{_f(fh + 28)}" fill="{wall_color}" stroke="none"/>',
        # 바닥은 편집기가 좌표 환산 기준으로 읽으므로 반올림 오차를 줄이려 소수 3자리로 쓴다
        f'<rect x="{fx:.3f}" y="{fy:.3f}" width="{fw:.3f}" height="{fh:.3f}" fill="url(#sg-planks)" stroke="{_shade(floor_color, 0.7)}" stroke-width="1" data-floor="true"/>',
    ]

    objects = sorted(
        graph.get("objects") or [],
        key=lambda o: (LAYER.get(str(o.get("type")), 2), -float(o["w_m"]) * float(o["d_m"])),
    )
    labels = []
    for obj in objects:
        kind = str(obj.get("type") or "unknown")
        w, d = float(obj["w_m"]) * px, float(obj["d_m"]) * px
        cx, cy = fx + float(obj["cx"]) * px, fy + float(obj["cy"]) * px
        color = _color(obj)
        if kind == "door":
            body = _door(w, max(d, 8), color)
        elif kind == "window":
            body = _window(w, max(d, 8), color)
        else:
            body = SHAPES.get(kind, _generic)(w, d, color)
        confidence = float(obj.get("confidence") if obj.get("confidence") is not None else 1.0)
        low = confidence < LOW_CONFIDENCE and obj.get("source") == "ai"
        filt = "" if kind in {"rug", "door", "window"} else ' filter="url(#sg-shadow)"'
        # AI가 확신하지 못한 가구는 점선 외곽선으로 표시한다(항목 18)
        outline = (
            f'<rect x="{_f(-w / 2 - 4)}" y="{_f(-d / 2 - 4)}" width="{_f(w + 8)}" height="{_f(d + 8)}" rx="6" '
            'fill="none" stroke="#d9822b" stroke-width="1.6" stroke-dasharray="5 4" data-low-confidence="true" data-annotation="true"/>'
            if low
            else ""
        )
        out.append(
            f'<g id="{html.escape(str(obj["id"]))}" data-type="{kind}" data-category="{html.escape(str(obj.get("category") or kind))}" '
            f'data-confidence="{confidence:.2f}" data-source="{html.escape(str(obj.get("source") or ""))}">'
            f'<g transform="translate({_f(cx)} {_f(cy)}) rotate({float(obj["rotation_deg"]):.1f})"{filt}>{body}{outline}</g>'
            "</g>"
        )
        if kind not in {"door", "window", "rug"} and min(w, d) >= 14:
            labels.append((obj, cx, cy, low))

    for obj, cx, cy, low in labels:
        text = html.escape(str(obj.get("label") or obj.get("type")))
        if low:
            text += " ?"
        width = 14 + 11 * len(str(obj.get("label") or "")) + (12 if low else 0)
        out.append(
            f'<g id="label-{html.escape(str(obj["id"]))}" transform="translate({_f(cx)} {_f(cy)})" '
            f'data-base-x="{_f(cx)}" data-base-y="{_f(cy)}" pointer-events="none">'
            f'<rect x="{_f(-width / 2)}" y="-10" width="{_f(width)}" height="20" rx="10" fill="#ffffff" fill-opacity="0.88" stroke="#5b4a3c" stroke-width="0.6"/>'
            f'<text x="0" y="4.5" text-anchor="middle" font-family="{FONT}" font-size="12" font-weight="600" fill="#33281f">{text}</text>'
            "</g>"
        )

    out.append(_dimensions(room, m))
    out.append("</svg>")
    return "".join(out)


def _is_synced(layout: dict[str, Any]) -> bool:
    return scene_graph.is_scene_graph(layout) and all(
        all(k in o for k in ("cx", "cy", "w_m", "d_m", "rotation_deg", "id")) for o in layout.get("objects") or []
    )


def _dimensions(room: dict[str, Any], m: dict[str, float]) -> str:
    fx, fy, fw, fh = m["floor_x"], m["floor_y"], m["floor_w"], m["floor_h"]
    estimated = bool(room.get("estimated"))
    suffix = " (추정)" if estimated else ""
    width_text = f"{float(room['width_m']):.2f} m{suffix}"
    depth_text = f"{float(room['depth_m']):.2f} m{suffix}"
    color = "#8a7866"
    y = fy + fh + 44
    x = fx - 44
    return (
        f'<g id="room-dimensions" pointer-events="none" font-family="{FONT}" font-size="14" fill="{color}">'
        f'<line x1="{_f(fx)}" y1="{_f(y)}" x2="{_f(fx + fw)}" y2="{_f(y)}" stroke="{color}" stroke-width="1"/>'
        f'<line x1="{_f(fx)}" y1="{_f(y - 6)}" x2="{_f(fx)}" y2="{_f(y + 6)}" stroke="{color}"/>'
        f'<line x1="{_f(fx + fw)}" y1="{_f(y - 6)}" x2="{_f(fx + fw)}" y2="{_f(y + 6)}" stroke="{color}"/>'
        f'<text x="{_f(fx + fw / 2)}" y="{_f(y + 20)}" text-anchor="middle">{html.escape(width_text)}</text>'
        f'<line x1="{_f(x)}" y1="{_f(fy)}" x2="{_f(x)}" y2="{_f(fy + fh)}" stroke="{color}" stroke-width="1"/>'
        f'<line x1="{_f(x - 6)}" y1="{_f(fy)}" x2="{_f(x + 6)}" y2="{_f(fy)}" stroke="{color}"/>'
        f'<line x1="{_f(x - 6)}" y1="{_f(fy + fh)}" x2="{_f(x + 6)}" y2="{_f(fy + fh)}" stroke="{color}"/>'
        f'<text x="{_f(x - 12)}" y="{_f(fy + fh / 2)}" text-anchor="middle" transform="rotate(-90 {_f(x - 12)} {_f(fy + fh / 2)})">{html.escape(depth_text)}</text>'
        "</g>"
    )
