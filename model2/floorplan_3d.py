# Scene Graph → 3D 배치 확인용 씬 데이터 변환
#
# 위치·크기·회전·높이는 Scene Graph 값을 그대로 옮긴다. 높이는 공간 분석이 사진에서
# 추정한 값(상품은 치수)이고, 종류별 고정 높이표는 두지 않는다. 가구 모양은 2D 평면도
# SVG를 밀어 올려 세운다(art_solid). 렌더링 자체는 브라우저(three.js)가 한다.
#
# 좌표 규약
#   - cx, cy: 방 안에서의 중심 위치 (미터, 좌상단 원점, cy는 깊이 방향)
#   - w_m, d_m: 가로·깊이 (미터).  height_m: 높이,  base_m: 바닥으로부터 띄운 높이
#   - rotation_deg: 위에서 본 회전각. 0 = 등을 위쪽(top) 벽에 붙인 상태.
#                   top=0, right=90, bottom=180, left=270
from __future__ import annotations

from typing import Any

from . import scene_graph

# 분석 색이 없을 때 쓰는 중립색(가구 모양은 2D SVG의 색을 쓰므로 거의 쓰이지 않는다)
NEUTRAL_COLOR = "#9a9186"
# 방 마감 기본색
DEFAULT_FLOOR_COLOR = "#b08a5e"
DEFAULT_WALL_COLOR = "#efe9dd"


def _positive(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result <= 0:
        return None
    return result


def _attrs_for(obj: dict[str, Any]) -> dict[str, Any]:
    # 형태 속성은 상품 사진을 본 Gemini 값만 쓴다(종류별 기본 형태는 두지 않는다)
    attrs = dict(obj.get("attrs") or {})
    material = str(obj.get("material") or "").lower()
    for key in ("wood", "fabric", "leather", "metal", "glass", "rattan", "marble"):
        if key in material and "material" not in attrs:
            attrs["material"] = key
    return attrs


def convert_graph_object(obj: dict[str, Any]) -> dict[str, Any]:
    """Scene Graph 객체 → 3D 씬 객체. 위치·크기·회전·높이·id를 그대로 옮긴다."""
    obj_type = str(obj.get("type") or "unknown").lower()
    color = str(obj.get("color") or "")
    marker = obj.get("product_marker")
    return {
        "id": str(obj["id"]),
        "type": obj_type,
        "label": str(obj.get("label") or obj_type),
        "cx": round(float(obj["cx"]), 4),
        "cy": round(float(obj["cy"]), 4),
        "w_m": round(float(obj["w_m"]), 4),
        "d_m": round(float(obj["d_m"]), 4),
        "height_m": round(float(obj.get("h_m") or 0.0), 4),
        "base_m": round(float(obj.get("base_m") or 0.0), 4),
        "rotation_deg": round(float(obj.get("rotation_deg") or 0.0) % 360, 3),
        "wall": str(obj.get("wall") or "none"),
        "wall_mounted": obj_type in scene_graph.WALL_MOUNTED_TYPES,
        "color": color if len(color) == 7 and color.startswith("#") else NEUTRAL_COLOR,
        "confidence": obj.get("confidence"),
        "source": obj.get("source"),
        "attrs": _attrs_for(obj),
        "dimension_source": obj.get("dimension_source"),
        "photo_box": obj.get("photo_box"),
        "image_file": obj.get("image_file"),
        "is_product": str(obj.get("source") or "") == "selected_product",
        "marker": marker if isinstance(marker, int) else None,
        "product_title": (
            str(obj["product_title"]) if obj.get("product_title") else None
        ),
    }


def _load_artwork(graph: dict[str, Any]) -> dict[str, Any] | None:
    name = str(graph.get("artwork_file") or "")
    if not name:
        return None
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "frontend" / "static" / "generated" / Path(name).name
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _fill_art_style(graph: dict[str, Any]) -> None:
    """2D 그림의 바닥·벽 스타일이 없는 배치(기능 추가 전에 저장된 편집본 등)를 채운다.

    편집본·수정본은 만들 당시의 방 정보를 그대로 들고 다녀서, 나중에 생긴 바닥 무늬
    정보가 없다. 그러면 2D는 새 바닥으로, 3D는 옛 단색으로 그려진다.
    """
    room = graph.get("room") or {}
    name = str(graph.get("artwork_file") or "")
    if room.get("art_style") or not name:
        return
    import json
    from pathlib import Path

    from .gemini_floorplan_artwork import room_style

    path = Path(__file__).resolve().parents[1] / "frontend" / "static" / "generated" / Path(name).name
    try:
        room["art_style"] = room_style(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return


def build_scene_from_graph(
    layout: dict[str, Any],
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Scene Graph → three.js 씬. 2D 렌더러와 같은 값을 그대로 쓴다.

    재배치를 하지 않으므로 2D와 3D의 위치·크기·회전이 정의상 같다.
    plan에 사용자 실측이 있고 그래프와 다르면 그래프 쪽을 그 치수로 맞춘다.
    """
    graph = scene_graph.ensure(layout)
    _fill_art_style(graph)
    plan_in = plan or {}
    width = _positive(plan_in.get("width_m"))
    depth = _positive(plan_in.get("depth_m"))
    room_in = graph["room"]
    if width and depth and (
        abs(width - float(room_in["width_m"])) > 1e-3
        or abs(depth - float(room_in["depth_m"])) > 1e-3
    ):
        graph = scene_graph.rescale_room(graph, width, depth)
        room_in = graph["room"]
    room = {
        "width_m": round(float(room_in["width_m"]), 3),
        "depth_m": round(float(room_in["depth_m"]), 3),
        "ceiling_m": round(
            _positive(plan_in.get("ceiling_m"))
            or _positive(room_in.get("ceiling_m"))
            or scene_graph.ceiling_from_objects(graph["objects"]),
            3,
        ),
        "estimated": bool(room_in.get("estimated")),
        "scale_source": room_in.get("scale_source"),
        # 2D 그림(Gemini)의 바닥·벽 색이 있으면 그걸 쓴다. 분석 색과 다르면 2D와 3D가
        # 다른 방처럼 보이기 때문이다
        "floor_color": str((room_in.get("art_style") or {}).get("floor_color") or room_in.get("floor_color") or DEFAULT_FLOOR_COLOR),
        "wall_color": str((room_in.get("art_style") or {}).get("wall_color") or room_in.get("wall_color") or DEFAULT_WALL_COLOR),
        "floor_pattern_svg": (room_in.get("art_style") or {}).get("floor_pattern_svg"),
        "floor_pattern_id": (room_in.get("art_style") or {}).get("floor_pattern_id"),
    }
    from .art_solid import object_solid

    # 가구 모양은 2D 평면도의 SVG를 밀어 올려 세운다. 2D와 같은 그림이라 모양·색이
    # 같고, 3D를 위해 API를 따로 부르지 않는다
    artwork = _load_artwork(graph)
    objects = []
    for obj in graph["objects"]:
        converted = convert_graph_object(obj)
        solid = object_solid(obj, artwork)
        if solid:
            converted["art3d"] = solid
        objects.append(converted)
    return {
        "room": room,
        "objects": objects,
        "placement": "scene_graph",
    }


def build_scene(
    layout: dict[str, Any] | None,
    plan: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """layout JSON + 방 치수 → three.js 가 바로 쓰는 씬 데이터.

    Scene Graph(scene_graph_v1)만 다룬다. 예전 형식(rule_based_v3)은 높이 정보가 없어
    3D로 세우지 않는다.
    """
    if isinstance(layout, dict) and layout.get("schema") == "scene_graph_v1":
        return build_scene_from_graph(layout, plan)
    return {"room": None, "objects": [], "placement": "unsupported"}
