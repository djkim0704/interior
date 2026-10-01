"""가구마다 Gemini 이미지 모델이 입체 그림을 그리고, three.js는 그 그림을 배치만 한다.

부품 좌표(JSON)로 모양을 쌓는 방식은 모델이 공간 구조를 잘 이해해야 해서 결과가
들쭉날쭉하다. 여기서는 이미지 모델이 가구 하나를 비스듬히 위에서 본 입체 그림으로
그리고, three.js가 그 그림을 Scene Graph 위치·크기에 맞춰 바닥에 세운다.

그림은 한 방향에서 본 모습이라 카메라를 돌리면 어색해진다. 그래서 앞·오른쪽·뒤·왼쪽
네 방향을 그리고, three.js가 카메라 방향에 가장 가까운 그림으로 바꿔 끼운다.
네 장이 같은 가구로 보이도록 앞 그림을 먼저 그리고, 나머지는 앞 그림을 함께 보여
주며 "같은 가구를 돌린 모습"으로 그리게 한다.

  - 배경은 흰색으로 그리게 하고, 저장할 때 바깥쪽 흰색을 투명으로 지운다.
  - 캐시는 가구 정체성(종류·이름·색·속성·크기 비율·참고 사진) 단위다. 옮기거나 돌려도
    다시 그리지 않고, 새로 들어온 가구만 그린다.
  - 실패한 가구는 그림 없이 입체 모형(부품 목록 또는 파라메트릭)으로 그려진다.

설정(.env)
  GEMINI_FURNITURE_VIEWS          1이면 사용(기본 1)
  GEMINI_FURNITURE_IMAGE_MODEL    비우면 GEMINI_IMAGE_MODEL
  FURNITURE_VIEW_COUNT            4(기본) 또는 1
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from google.genai import types
from PIL import Image

from .gemini_furniture_parts import SKIP_TYPES, _client, _object_image
from .gemini_retry import call_with_retry

VIEWS_VERSION = "furniture-views-v1"
VIEWS_DIR = "gemini_furniture_views_v1"
DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"
FAILURE_COOLDOWN_SECONDS = 10 * 60
VIEW_ORDER = ("front", "right", "back", "left")
# 흰 배경으로 볼 밝기 기준. 가구의 흰 부분까지 지우지 않도록 바깥에서 이어진 영역만 지운다
WHITE_THRESHOLD = 238
MAX_SIDE_PX = 768
_LOCK = threading.Lock()

FRONT_PROMPT = """
Draw ONE piece of furniture as a polished 3D product render, matching the reference
photo exactly (same design, proportions, colors, materials, legs and details).

Object: {label} ({kind}), real size W {w:.2f} m x D {d:.2f} m x H {h:.2f} m.
Features: {attrs}

View: three-quarter view from the FRONT, camera about 30 degrees above the floor,
looking slightly down, the front of the object facing the viewer. Orthographic-like,
minimal perspective. The whole object fully visible, centered, filling most of the image.
Background: pure flat white (#FFFFFF), no floor, no shadow, no room, no props, no text.
""".strip()

TURN_PROMPT = """
The first image is a 3D render of a piece of furniture seen from the FRONT.
Draw the SAME object (identical design, colors, materials, proportions and lighting)
seen from its {side}: the camera has moved {turn} around the object, still about
30 degrees above the floor, looking slightly down.

Object: {label} ({kind}), real size W {w:.2f} m x D {d:.2f} m x H {h:.2f} m.
The whole object fully visible, centered, filling most of the image.
Background: pure flat white (#FFFFFF), no floor, no shadow, no props, no text.
""".strip()

TURNS = {
    "right": ("RIGHT side", "90 degrees to the object's right"),
    "back": ("BACK", "180 degrees to behind the object"),
    "left": ("LEFT side", "90 degrees to the object's left"),
}


def enabled() -> bool:
    return os.getenv("GEMINI_FURNITURE_VIEWS", "1").strip().lower() not in {"0", "false", "no", "off"}


def view_names() -> tuple[str, ...]:
    return VIEW_ORDER if os.getenv("FURNITURE_VIEW_COUNT", "4").strip() != "1" else ("front",)


def image_model() -> str:
    return (
        os.getenv("GEMINI_FURNITURE_IMAGE_MODEL", "").strip()
        or os.getenv("GEMINI_IMAGE_MODEL", "").strip()
        or DEFAULT_IMAGE_MODEL
    )


def cut_out_white(png_or_jpeg: bytes) -> bytes:
    """바깥에서 이어진 흰 배경을 투명으로 바꾸고 그림 부분만 잘라 PNG로 돌려준다.

    가장자리에서 시작하는 흰 영역만 지운다. 가구 안의 흰 쿠션·흰 상판은 남는다.
    """
    with Image.open(io.BytesIO(png_or_jpeg)) as source:
        image = source.convert("RGBA")
    scale = min(1.0, MAX_SIDE_PX / max(image.size))
    if scale < 1.0:
        image = image.resize((round(image.width * scale), round(image.height * scale)), Image.Resampling.LANCZOS)
    width, height = image.size
    pixels = image.load()

    def white(x: int, y: int) -> bool:
        r, g, b, _ = pixels[x, y]
        return r >= WHITE_THRESHOLD and g >= WHITE_THRESHOLD and b >= WHITE_THRESHOLD

    seen = bytearray(width * height)
    queue: deque[tuple[int, int]] = deque()
    for x in range(width):
        for y in (0, height - 1):
            if white(x, y):
                queue.append((x, y))
    for y in range(height):
        for x in (0, width - 1):
            if white(x, y):
                queue.append((x, y))
    while queue:
        x, y = queue.popleft()
        index = y * width + x
        if seen[index]:
            continue
        seen[index] = 1
        r, g, b, _ = pixels[x, y]
        pixels[x, y] = (r, g, b, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < width and 0 <= ny < height and not seen[ny * width + nx] and white(nx, ny):
                queue.append((nx, ny))
    box = image.getbbox()
    if box:
        image = image.crop(box)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _image_from(response: Any) -> bytes:
    notes = []
    parts = []
    if response.candidates and response.candidates[0].content:
        parts = response.candidates[0].content.parts or []
    for part in parts:
        if getattr(part, "text", None):
            notes.append(str(part.text))
        inline = getattr(part, "inline_data", None)
        if inline is not None and inline.data:
            return inline.data
    raise RuntimeError("이미지 모델이 그림을 반환하지 않았습니다. " + " ".join(notes)[:200])


def _item(obj: dict[str, Any], room_photo: Path | None, image_dir: Path) -> dict[str, Any]:
    reference = _object_image(obj, room_photo, image_dir)
    return {
        "id": str(obj["id"]),
        "kind": str(obj.get("type") or ""),
        "label": str(obj.get("product_title") or obj.get("label") or obj.get("type"))[:60],
        "w": max(float(obj.get("w_m") or 0.3), 0.05),
        "d": max(float(obj.get("d_m") or 0.3), 0.05),
        "h": max(float(obj.get("height_m") or 0.5), 0.05),
        "color": str(obj.get("color") or ""),
        "attrs": obj.get("attrs") or {},
        "reference": reference,
        "reference_digest": hashlib.sha256(reference).hexdigest()[:16] if reference else None,
    }


def _key(item: dict[str, Any], model: str) -> str:
    payload = {
        "version": VIEWS_VERSION,
        "model": model,
        "kind": item["kind"],
        "label": item["label"],
        "color": item["color"],
        "attrs": item["attrs"],
        "ratio": [round(item["w"] / item["h"] * 10) / 10, round(item["d"] / item["h"] * 10) / 10],
        "reference": item["reference_digest"],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]


def _draw(client: Any, model: str, contents: list[Any]) -> bytes:
    response = call_with_retry(
        client.models.generate_content,
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio="1:1"),
        ),
        description="가구 입체 그림 생성",
    )
    return cut_out_white(_image_from(response))


def generate_object_views(
    scene: dict[str, Any],
    cache_dir: str | Path,
    *,
    room_photo: str | Path | None = None,
    url_prefix: str = "/static/generated",
    client: Any = None,
    model: str | None = None,
    max_new: int | None = None,
) -> dict[str, Any]:
    """{"views": {object_id: {"views": {"front": url, ...}, "sizes": {...}}}, "remaining": n}.

    캐시에 없는 가구만 그린다. 가구 하나에 이미지 호출이 여러 번 들어가 오래 걸리므로
    max_new로 한 번에 그릴 가구 수를 제한하고, 남은 수를 돌려준다. 화면은 남은 가구가
    없을 때까지 다시 부르며 그려진 가구부터 보여 준다.
    """
    if not enabled():
        return {"views": {}, "remaining": 0}
    model = model or image_model()
    root = Path(cache_dir).resolve()
    out_dir = root / VIEWS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    image_dir = root / "product_images_v1"
    photo = Path(room_photo) if room_photo else None
    names = view_names()
    result: dict[str, Any] = {}

    def entry_for(key: str) -> dict[str, Any] | None:
        views, sizes = {}, {}
        for name in names:
            path = out_dir / f"{key}_{name}.png"
            if not path.is_file():
                return None
            views[name] = f"{url_prefix}/{VIEWS_DIR}/{path.name}"
            with Image.open(path) as png:
                sizes[name] = [png.width, png.height]
        return {"views": views, "sizes": sizes, "model": model}

    pending = []
    for obj in scene.get("objects") or []:
        kind = str(obj.get("type") or "")
        if not kind or kind in SKIP_TYPES:
            continue
        item = _item(obj, photo, image_dir)
        key = _key(item, model)
        cached = entry_for(key)
        if cached:
            result[item["id"]] = cached
            continue
        failed = out_dir / f"{key}.failed"
        if failed.is_file() and time.time() - failed.stat().st_mtime < FAILURE_COOLDOWN_SECONDS:
            continue
        item["key"] = key
        pending.append(item)

    remaining = 0
    if max_new is not None and len(pending) > max_new:
        remaining = len(pending) - max_new
        pending = pending[:max_new]
    if not pending:
        return {"views": result, "remaining": 0}

    with _LOCK:
        try:
            client = client or _client()
        except Exception as exc:
            print(f"[gemini-furniture-views] 클라이언트 생성 실패: {exc}")
            return {"views": result, "remaining": 0}
        for item in pending:
            key = item["key"]
            try:
                facts = dict(
                    label=item["label"],
                    kind=item["kind"],
                    w=item["w"],
                    d=item["d"],
                    h=item["h"],
                    attrs=json.dumps(item["attrs"], ensure_ascii=False) or "-",
                )
                front_path = out_dir / f"{key}_front.png"
                if not front_path.is_file():
                    contents: list[Any] = [FRONT_PROMPT.format(**facts)]
                    if item["reference"]:
                        contents.append(types.Part.from_bytes(data=item["reference"], mime_type="image/jpeg"))
                    front_path.write_bytes(_draw(client, model, contents))
                front_bytes = front_path.read_bytes()
                for name in names[1:]:
                    path = out_dir / f"{key}_{name}.png"
                    if path.is_file():
                        continue
                    side, turn = TURNS[name]
                    contents = [
                        TURN_PROMPT.format(side=side, turn=turn, **facts),
                        types.Part.from_bytes(data=front_bytes, mime_type="image/png"),
                    ]
                    if item["reference"]:
                        contents.append(types.Part.from_bytes(data=item["reference"], mime_type="image/jpeg"))
                    path.write_bytes(_draw(client, model, contents))
            except Exception as exc:
                print(f"[gemini-furniture-views] {item['id']} 그림 생성 실패: {exc}")
                (out_dir / f"{key}.failed").write_text(f"{int(time.time())}\n{type(exc).__name__}: {exc}", encoding="utf-8")
                continue
            cached = entry_for(key)
            if cached:
                result[item["id"]] = cached
    return {"views": result, "remaining": remaining}
