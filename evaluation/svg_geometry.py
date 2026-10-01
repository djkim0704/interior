"""평면도 SVG에서 가구 그룹(<g id="bed_1">)이 실제로 그려진 영역을 계산한다.

2D와 3D가 같은 자리에 가구를 놓는지 재려면, layout JSON이 아니라 사용자가
실제로 보는 SVG의 기하를 기준으로 삼아야 한다. Gemini가 그린 SVG는 rect·path에
translate/scale이 여러 겹 중첩돼 있어서 transform을 끝까지 누적해 계산한다.

정확도 한계: path의 곡선과 arc는 제어점·끝점의 외접 박스로 근사한다.
실제 곡선보다 약간 크게 잡히므로 2D↔3D 오차는 보수적으로(크게) 나온다.
"""
from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Iterable

SVG_NS = "{http://www.w3.org/2000/svg}"

Matrix = tuple[float, float, float, float, float, float]  # a b c d e f
Box = tuple[float, float, float, float]  # x0 y0 x1 y1

IDENTITY: Matrix = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")


def _multiply(m: Matrix, n: Matrix) -> Matrix:
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (
        a * a2 + c * b2,
        b * a2 + d * b2,
        a * c2 + c * d2,
        b * c2 + d * d2,
        a * e2 + c * f2 + e,
        b * e2 + d * f2 + f,
    )


def _apply(m: Matrix, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = m
    return a * x + c * y + e, b * x + d * y + f


def parse_transform(text: str | None) -> Matrix:
    result = IDENTITY
    if not text:
        return result
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", text):
        values = [float(v) for v in _NUMBER.findall(args)]
        name = name.lower()
        if name == "translate":
            tx = values[0] if values else 0.0
            ty = values[1] if len(values) > 1 else 0.0
            step: Matrix = (1, 0, 0, 1, tx, ty)
        elif name == "scale":
            sx = values[0] if values else 1.0
            sy = values[1] if len(values) > 1 else sx
            step = (sx, 0, 0, sy, 0, 0)
        elif name == "rotate" and values:
            angle = math.radians(values[0])
            cos, sin = math.cos(angle), math.sin(angle)
            step = (cos, sin, -sin, cos, 0, 0)
            if len(values) >= 3:
                cx, cy = values[1], values[2]
                step = _multiply(_multiply((1, 0, 0, 1, cx, cy), step), (1, 0, 0, 1, -cx, -cy))
        elif name == "matrix" and len(values) == 6:
            step = tuple(values)  # type: ignore[assignment]
        elif name == "skewx" and values:
            step = (1, 0, math.tan(math.radians(values[0])), 1, 0, 0)
        elif name == "skewy" and values:
            step = (1, math.tan(math.radians(values[0])), 0, 1, 0, 0)
        else:
            continue
        result = _multiply(result, step)
    return result


def _float(element: ET.Element, name: str, default: float = 0.0) -> float:
    raw = element.get(name)
    if raw is None:
        return default
    match = _NUMBER.search(raw)
    return float(match.group()) if match else default


def _path_points(d: str) -> list[tuple[float, float]]:
    """path 데이터에서 끝점과 제어점을 절대 좌표로 뽑는다."""
    tokens = re.findall(r"[MmLlHhVvCcSsQqTtAaZz]|" + _NUMBER.pattern, d)
    points: list[tuple[float, float]] = []
    x = y = 0.0
    start_x = start_y = 0.0
    command = ""
    index = 0
    arity = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}

    while index < len(tokens):
        token = tokens[index]
        if token.isalpha():
            command = token
            index += 1
            if command in "Zz":
                x, y = start_x, start_y
                points.append((x, y))
                continue
        if not command:
            index += 1
            continue
        upper = command.upper()
        count = arity[upper]
        args = tokens[index : index + count]
        if len(args) < count or any(t.isalpha() for t in args):
            break
        values = [float(v) for v in args]
        index += count
        relative = command.islower()
        ox, oy = (x, y) if relative else (0.0, 0.0)

        if upper == "H":
            x = values[0] + (x if relative else 0.0)
            points.append((x, y))
        elif upper == "V":
            y = values[0] + (y if relative else 0.0)
            points.append((x, y))
        elif upper == "A":
            rx, ry = abs(values[0]), abs(values[1])
            end_x, end_y = values[5] + ox, values[6] + oy
            # 호의 외접 박스를 정확히 구하지 않고 양 끝점 ± 반지름으로 근사한다
            mid_x, mid_y = (x + end_x) / 2, (y + end_y) / 2
            points.extend([(x, y), (end_x, end_y)])
            points.extend(
                [
                    (mid_x - rx, mid_y - ry),
                    (mid_x + rx, mid_y + ry),
                ]
            )
            x, y = end_x, end_y
        else:
            pairs = [(values[i] + ox, values[i + 1] + oy) for i in range(0, count, 2)]
            points.extend(pairs)
            x, y = pairs[-1]
            if upper == "M":
                start_x, start_y = x, y
                # M 뒤에 이어지는 좌표쌍은 L로 해석한다
                command = "l" if relative else "L"
    return points


def _element_points(element: ET.Element) -> list[tuple[float, float]]:
    tag = element.tag.replace(SVG_NS, "")
    if tag == "rect":
        x, y = _float(element, "x"), _float(element, "y")
        w, h = _float(element, "width"), _float(element, "height")
        if w <= 0 or h <= 0:
            return []
        return [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
    if tag == "circle":
        cx, cy, r = _float(element, "cx"), _float(element, "cy"), _float(element, "r")
        return [(cx - r, cy - r), (cx + r, cy + r), (cx - r, cy + r), (cx + r, cy - r)]
    if tag == "ellipse":
        cx, cy = _float(element, "cx"), _float(element, "cy")
        rx, ry = _float(element, "rx"), _float(element, "ry")
        return [(cx - rx, cy - ry), (cx + rx, cy + ry), (cx - rx, cy + ry), (cx + rx, cy - ry)]
    if tag == "line":
        return [
            (_float(element, "x1"), _float(element, "y1")),
            (_float(element, "x2"), _float(element, "y2")),
        ]
    if tag in {"polygon", "polyline"}:
        values = [float(v) for v in _NUMBER.findall(element.get("points", ""))]
        return list(zip(values[0::2], values[1::2]))
    if tag == "path":
        return _path_points(element.get("d", ""))
    return []


def _union(boxes: Iterable[Box]) -> Box | None:
    items = list(boxes)
    if not items:
        return None
    return (
        min(b[0] for b in items),
        min(b[1] for b in items),
        max(b[2] for b in items),
        max(b[3] for b in items),
    )


def _bbox(element: ET.Element, matrix: Matrix) -> Box | None:
    tag = element.tag.replace(SVG_NS, "")
    if tag in {"defs", "text", "title", "desc", "style", "clipPath", "mask", "pattern", "filter"}:
        return None
    matrix = _multiply(matrix, parse_transform(element.get("transform")))
    boxes: list[Box] = []
    points = [_apply(matrix, px, py) for px, py in _element_points(element)]
    if points:
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        boxes.append((min(xs), min(ys), max(xs), max(ys)))
    for child in element:
        child_box = _bbox(child, matrix)
        if child_box is not None:
            boxes.append(child_box)
    return _union(boxes)


def _walk(element: ET.Element, matrix: Matrix, out: dict[str, tuple[ET.Element, Matrix]]) -> None:
    for child in element:
        tag = child.tag.replace(SVG_NS, "")
        if tag == "defs":
            continue
        child_matrix = _multiply(matrix, parse_transform(child.get("transform")))
        element_id = child.get("id")
        if element_id and element_id not in out:
            # 그룹 자신의 transform은 _bbox에서 다시 적용하므로 부모 행렬을 저장한다
            out[element_id] = (child, matrix)
        _walk(child, child_matrix, out)


def load(path: str | Path) -> ET.Element:
    return ET.parse(path).getroot()


def group_boxes(root: ET.Element, ids: Iterable[str]) -> dict[str, Box]:
    """id별로 SVG 사용자 좌표계의 외접 박스를 돌려준다. 못 찾은 id는 빠진다."""
    found: dict[str, tuple[ET.Element, Matrix]] = {}
    _walk(root, IDENTITY, found)
    result: dict[str, Box] = {}
    for object_id in ids:
        entry = found.get(object_id)
        if entry is None:
            continue
        box = _bbox(entry[0], entry[1])
        if box is not None and box[2] > box[0] and box[3] > box[1]:
            result[object_id] = box
    return result


def floor_box(root: ET.Element, furniture_ids: Iterable[str] = ()) -> Box | None:
    """방 바닥 사각형을 찾는다.

    가구 그룹 밖에 있는 rect 중 viewBox의 40% 이상을 덮는 가장 작은 것을 고른다.
    앱의 _floor_box와 같은 가정(바닥은 큰 rect 하나)을 쓰되, transform까지 반영한다.
    """
    view = [float(v) for v in _NUMBER.findall(root.get("viewBox", ""))]
    if len(view) == 4:
        view_w, view_h = view[2], view[3]
    else:
        view_w, view_h = _float(root, "width", 1024.0), _float(root, "height", 1024.0)
    skip = set(furniture_ids) | {f"label-{i}" for i in furniture_ids}

    candidates: list[tuple[float, Box]] = []

    def visit(element: ET.Element, matrix: Matrix) -> None:
        for child in element:
            tag = child.tag.replace(SVG_NS, "")
            if tag == "defs" or child.get("id") in skip:
                continue
            child_matrix = _multiply(matrix, parse_transform(child.get("transform")))
            if tag == "rect" and str(child.get("fill") or "") != "none":
                points = [_apply(child_matrix, x, y) for x, y in _element_points(child)]
                if points:
                    xs = [p[0] for p in points]
                    ys = [p[1] for p in points]
                    box = (min(xs), min(ys), max(xs), max(ys))
                    w, h = box[2] - box[0], box[3] - box[1]
                    if w >= 0.4 * view_w and h >= 0.4 * view_h:
                        candidates.append((w * h, box))
            visit(child, child_matrix)

    visit(root, IDENTITY)
    if not candidates:
        return None
    return min(candidates)[1]
