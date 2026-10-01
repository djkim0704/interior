"""최종 배치를 Gemini 텍스트 모델의 입체 인테리어 SVG로 변환한다."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from google import genai
from google.genai import types

from .gemini_retry import GeminiBusyError, call_with_retry
from .gemini_telemetry import instrument
from PIL import Image, ImageDraw, ImageOps


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL = "gemini-3.6-flash"
PROMPT_VERSION = "room-perspective-svg-v2"
MAX_PRODUCT_IMAGES = 8
MAX_DOWNLOAD_BYTES = 12 * 1024 * 1024
FAILURE_COOLDOWN_SECONDS = 10 * 60
SVG_NS = "http://www.w3.org/2000/svg"
_GENERATION_LOCK = threading.Lock()

# Gemini가 종종 안전한 SVG 표현 속성을 ``style="fill:..."`` 형태로
# 묶어서 반환한다. 스타일 전체를 허용하면 CSS 주입 표면이 커지고, 전부
# 거부하면 정상 결과까지 폐기된다. 필요한 표현 속성만 개별 SVG 속성으로
# 풀어 쓴 뒤 style 자체는 제거한다.
SAFE_STYLE_PROPERTIES = frozenset({
    "clip-path",
    "color",
    "color-interpolation-filters",
    "fill",
    "fill-opacity",
    "fill-rule",
    "filter",
    "flood-color",
    "flood-opacity",
    "isolation",
    "mask",
    "mix-blend-mode",
    "opacity",
    "stop-color",
    "stop-opacity",
    "stroke",
    "stroke-dasharray",
    "stroke-dashoffset",
    "stroke-linecap",
    "stroke-linejoin",
    "stroke-miterlimit",
    "stroke-opacity",
    "stroke-width",
    "visibility",
})

ET.register_namespace("", SVG_NS)


def _client() -> genai.Client:
    """프로젝트 API 키로 프록시를 사용하지 않는 Gemini 클라이언트를 만든다."""
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY가 없습니다.")
    return instrument(genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            client_args={"trust_env": False},
            async_client_args={"trust_env": False},
            # 이 렌더는 아래에서 모델별 재시도와 폴백을 직접 관리한다.
            # SDK 기본 5회 재시도까지 겹치면 한 모델의 503에 수 분을 쓴다.
            retry_options=types.HttpRetryOptions(attempts=1),
        ),
    ))


def _room_svg_models(primary: str) -> list[str]:
    """우선 모델과 503/429 시 사용할 대체 모델 목록을 만든다."""
    configured = os.getenv(
        "GEMINI_ROOM_SVG_FALLBACK_MODELS",
        "gemini-3.1-flash-lite,gemini-2.5-flash-lite",
    )
    candidates = [primary]
    candidates.extend(item.strip() for item in configured.split(","))
    return list(dict.fromkeys(item for item in candidates if item))


def _file_digest(path: Path) -> str:
    """원본 사진 내용이 바뀌면 기존 SVG 캐시를 재사용하지 않도록 해시를 만든다."""
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _hex_color(value: Any, fallback: str) -> str:
    """씬에서 받은 색상이 올바른 HEX 값인지 확인하고 아니면 기본색을 쓴다."""
    text = str(value or "").strip()
    if len(text) == 7 and text.startswith("#"):
        try:
            int(text[1:], 16)
            return text
        except ValueError:
            pass
    return fallback


def _rotated_box(
    center_x: float,
    center_y: float,
    width: float,
    depth: float,
    angle_degrees: float,
) -> list[tuple[float, float]]:
    """가구 중심·크기·회전각을 탑뷰 가이드용 네 꼭짓점으로 변환한다."""
    import math

    radians = math.radians(angle_degrees)
    cosine = math.cos(radians)
    sine = math.sin(radians)
    points = []
    for local_x, local_y in (
        (-width / 2, -depth / 2),
        (width / 2, -depth / 2),
        (width / 2, depth / 2),
        (-width / 2, depth / 2),
    ):
        points.append(
            (
                center_x + local_x * cosine - local_y * sine,
                center_y + local_x * sine + local_y * cosine,
            )
        )
    return points


def render_layout_guide(
    scene: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """미터 단위 씬을 위치 고정용 탑뷰 PNG로 만든다.

    이 이미지는 사용자에게 보여줄 결과물이 아니라 Gemini가 가구의 위치·크기·
    회전 방향을 유지하도록 제공하는 제어 이미지다.
    """
    output_path = Path(output_path)
    room = scene.get("room") or {}
    width_m = max(float(room.get("width_m") or 4.0), 0.1)
    depth_m = max(float(room.get("depth_m") or 4.0), 0.1)
    canvas_width, canvas_height, margin = 1400, 1000, 75
    scale = min(
        (canvas_width - margin * 2) / width_m,
        (canvas_height - margin * 2) / depth_m,
    )
    room_width = width_m * scale
    room_depth = depth_m * scale
    left = (canvas_width - room_width) / 2
    top = (canvas_height - room_depth) / 2

    image = Image.new("RGB", (canvas_width, canvas_height), "#ece9e2")
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (left, top, left + room_width, top + room_depth),
        fill=_hex_color(room.get("floor_color"), "#b08a5e"),
        outline="#282521",
        width=12,
    )
    objects = list(scene.get("objects") or [])
    objects.sort(key=lambda item: 0 if item.get("type") == "rug" else 1)
    for obj in objects:
        points = _rotated_box(
            left + float(obj.get("cx") or 0.0) * scale,
            top + float(obj.get("cy") or 0.0) * scale,
            max(float(obj.get("w_m") or 0.1) * scale, 4.0),
            max(float(obj.get("d_m") or 0.1) * scale, 4.0),
            float(obj.get("rotation_deg") or 0.0),
        )
        draw.polygon(
            points,
            fill=_hex_color(obj.get("color"), "#9a9186"),
            outline="#f4c95d" if obj.get("is_product") else "#302c27",
            width=7 if obj.get("is_product") else 4,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path, format="PNG")
    return output_path


def _jpeg_bytes(source: Image.Image, max_side: int) -> bytes:
    """이미지 방향을 보정하고 Gemini 입력에 적합한 크기의 JPEG로 압축한다."""
    image = ImageOps.exif_transpose(source).convert("RGB")
    scale = min(1.0, max_side / max(image.size))
    if scale < 1.0:
        image = image.resize(
            (round(image.width * scale), round(image.height * scale)),
            Image.Resampling.LANCZOS,
        )
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90, optimize=True)
    return buffer.getvalue()


def _path_part(path: Path, max_side: int = 1600) -> types.Part:
    """로컬 사진을 Gemini 멀티모달 요청의 인라인 이미지로 변환한다."""
    with Image.open(path) as source:
        data = _jpeg_bytes(source, max_side)
    return types.Part.from_bytes(data=data, mime_type="image/jpeg")


def _url_part(url: str, max_side: int = 1200) -> types.Part:
    """원격 상품 사진을 용량 제한 안에서 내려받아 인라인 이미지로 변환한다."""
    with requests.Session() as session:
        session.trust_env = False
        with session.get(url, timeout=15, stream=True) as response:
            response.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("상품 이미지가 허용 크기를 초과했습니다.")
                chunks.append(chunk)
    with Image.open(io.BytesIO(b"".join(chunks))) as source:
        data = _jpeg_bytes(source, max_side)
    return types.Part.from_bytes(data=data, mime_type="image/jpeg")


def _extract_svg(text: str) -> str:
    """Gemini 응답의 설명과 코드 펜스를 제거하고 SVG 문서만 추출한다."""
    start = text.find("<svg")
    end = text.rfind("</svg>")
    if start < 0 or end < start:
        raise ValueError("Gemini 응답에서 SVG 문서를 찾지 못했습니다.")
    return text[start : end + len("</svg>")]


def _safe_style_value(value: str) -> bool:
    """외부 리소스나 실행 가능한 CSS가 없는 표현 값인지 확인한다."""
    lowered = value.lower().replace(" ", "")
    if any(
        marker in lowered
        for marker in (
            "javascript:",
            "data:",
            "http:",
            "https:",
            "@import",
            "expression(",
        )
    ):
        return False
    return all(
        reference.strip("'\"").startswith("#")
        for reference in re.findall(r"url\(([^)]+)\)", lowered)
    )


def _expand_safe_inline_style(element: ET.Element) -> None:
    """허용한 inline style 선언만 SVG 표현 속성으로 변환한다."""
    raw_style = element.attrib.pop("style", "")
    for declaration in raw_style.split(";"):
        if ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if (
            name not in SAFE_STYLE_PROPERTIES
            or not value
            or not _safe_style_value(value)
        ):
            continue
        element.attrib.setdefault(name, value)


def _sanitize_svg(svg_text: str) -> str:
    """스크립트·외부 리소스를 차단하고 허용한 벡터 요소만 남긴다."""
    if len(svg_text.encode("utf-8")) > 2 * 1024 * 1024:
        raise ValueError("생성된 SVG가 허용 크기를 초과했습니다.")

    root = ET.fromstring(svg_text)
    if root.tag.rsplit("}", 1)[-1] != "svg":
        raise ValueError("SVG 루트 요소가 없습니다.")
    allowed_tags = {
        "svg", "g", "defs", "title", "desc", "path", "rect", "circle",
        "ellipse", "line", "polyline", "polygon", "linearGradient",
        "radialGradient", "stop", "pattern", "clipPath", "mask", "filter",
        "feGaussianBlur", "feOffset", "feColorMatrix", "feBlend",
        "feMerge", "feMergeNode", "feFlood", "feComposite",
        # 위 프롬프트가 그림자·질감을 "filters" 로 그리라고 지시하는데 정작
        # 대표 필터가 빠져 있어서, 모델이 시킨 대로 그리면 검증에서 막혔다.
        # feDropShadow 는 feGaussianBlur+feOffset+feMerge 의 축약형이라
        # 이미 허용된 조합과 표현력이 같다. 나머지도 순수 그래픽 연산이라
        # 스크립트·외부 리소스와 무관하다(외부 참조가 가능한 feImage 는 제외).
        "feDropShadow", "feTurbulence", "feDisplacementMap",
        "feMorphology", "feTile",
    }
    elements = list(root.iter())
    if len(elements) > 1200:
        raise ValueError("생성된 SVG 요소가 너무 많습니다.")

    for element in elements:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in allowed_tags:
            raise ValueError(f"허용하지 않는 SVG 요소입니다: {tag}")
        _expand_safe_inline_style(element)
        for attribute, value in element.attrib.items():
            name = attribute.rsplit("}", 1)[-1].lower()
            lowered = str(value).lower().replace(" ", "")
            if name.startswith("on") or name == "href":
                raise ValueError(f"허용하지 않는 SVG 속성입니다: {name}")
            if any(marker in lowered for marker in ("javascript:", "data:", "http:", "https:")):
                raise ValueError("SVG 외부 리소스 참조는 허용하지 않습니다.")
            for reference in re.findall(r"url\(([^)]+)\)", lowered):
                if not reference.strip("'\"").startswith("#"):
                    raise ValueError("SVG 필터는 문서 내부 정의만 참조할 수 있습니다.")
        if not str(element.tag).startswith("{"):
            element.tag = f"{{{SVG_NS}}}{tag}"

    root.set("viewBox", "0 0 1600 900")
    root.set("width", "1600")
    root.set("height", "900")
    root.set("preserveAspectRatio", "xMidYMid meet")
    return ET.tostring(root, encoding="unicode")


def _cache_key(
    scene: dict[str, Any],
    original_image: Path,
    selected_products: list[dict[str, Any]],
    style_prompt: str,
    model: str,
) -> str:
    """같은 설계 입력에는 같은 SVG를 반환하도록 안정적인 캐시 키를 만든다."""
    payload = {
        "version": PROMPT_VERSION,
        "model": model,
        "scene": scene,
        "original_image": _file_digest(original_image),
        "style_prompt": style_prompt,
        "products": [
            {
                "type": item.get("type"),
                "title": item.get("title"),
                "image": item.get("image"),
            }
            for item in selected_products
        ],
    }
    return hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:24]


def _prompt(
    scene: dict[str, Any],
    style_prompt: str,
    product_references: list[str],
) -> str:
    """입체감·재질·조명을 강조하면서 배치를 고정하는 SVG 지시문을 만든다."""
    room = scene.get("room") or {}
    object_summary = "\n".join(
        "- "
        f"{item.get('label') or item.get('type')}: "
        f"center=({float(item.get('cx') or 0):.2f}m, "
        f"{float(item.get('cy') or 0):.2f}m), "
        f"size={float(item.get('w_m') or 0):.2f}m x "
        f"{float(item.get('d_m') or 0):.2f}m x "
        f"{float(item.get('height_m') or 0):.2f}m, "
        f"rotation={float(item.get('rotation_deg') or 0):.0f}deg"
        for item in scene.get("objects") or []
    )
    products = "\n".join(product_references) or "- No separate product photos."
    return f"""
Create one polished 1600x900 SVG architectural interior rendering. Return only
the complete <svg xmlns="http://www.w3.org/2000/svg"> document. This is a fixed
perspective illustration, not a top-down floor plan.

Reference order:
1. The first image is the original room photograph. Preserve doors, windows,
   wall openings, proportions, and the plausible camera side.
2. The second image is the authoritative top-down placement guide. Preserve the
   position, footprint, orientation, and count of every object.
3. Remaining images are selected products. Match their recognizable shape,
   color, upholstery, and material.

Draw an eye-level wide-angle corner view with convincing depth. Simulate PBR-like
wood, fabric, metal, glass, soft daylight, practical warm lighting, contact
shadows, ambient occlusion, highlights, and reflections using SVG gradients,
patterns, opacity, masks, and filters. Aim for a premium architectural editorial
illustration with substantially more material detail than simple flat boxes.

Do not add, remove, duplicate, resize, or move major furniture. Do not include
labels, measurements, UI, people, watermarks, raster images, base64 data, scripts,
stylesheets, style attributes, foreignObject, or external URLs. Use SVG
presentation attributes such as fill, stroke, opacity, and filter instead. Use
only self-contained SVG vector elements and internal defs referenced as url(#id).

Room: {float(room.get('width_m') or 0):.2f}m x
{float(room.get('depth_m') or 0):.2f}m, ceiling
{float(room.get('ceiling_m') or 0):.2f}m.
Objects:
{object_summary or '- none'}
Design preference: {style_prompt[:500] or 'Preserve the original room mood.'}
Product reference mapping:
{products}
""".strip()


def generate_room_svg(
    scene: dict[str, Any],
    original_image: str | Path,
    selected_products: list[dict[str, Any]],
    cache_dir: str | Path,
    *,
    style_prompt: str = "",
    model: str | None = None,
) -> Path:
    """Gemini 텍스트 모델로 입체 렌더 SVG를 생성하거나 캐시 파일을 반환한다.

    캐시가 없을 때만 API를 한 번 호출한다. 실패한 동일 조합은 짧은 시간 동안
    재호출하지 않으며, 호출부는 기존 Three.js 화면으로 대체할 수 있다.
    """
    # Flask 경유뿐 아니라 모듈을 단독 실행해도 전용 모델 설정을 먼저 읽는다.
    load_dotenv(PROJECT_ROOT / ".env")
    original_image = Path(original_image).resolve()
    if not original_image.is_file():
        raise FileNotFoundError("원본 방 사진을 찾을 수 없습니다.")
    model = (
        model
        # GEMINI_ANALYSIS_MODEL 은 평면도 생성까지 함께 쓰는 값이라, 이 렌더만
        # 가벼운 모델로 내리려고 그걸 건드리면 평면도 SVG 품질까지 같이 떨어진다.
        # (평면도는 layout id 와 <g id> 를 맞춰야 해서 지시 준수력이 중요하다.)
        # 그래서 이 렌더 전용 값을 먼저 본다. 입체 렌더는 장식용이라 id 계약이 없고
        # 실패해도 three.js 화면으로 대체되므로 한도가 넉넉한 모델이 낫다.
        or os.getenv("GEMINI_ROOM_SVG_MODEL", "").strip()
        or os.getenv("GEMINI_ANALYSIS_MODEL", "").strip()
        or DEFAULT_MODEL
    )
    cache_root = Path(cache_dir).resolve() / "gemini_room_svg_v1"
    cache_root.mkdir(parents=True, exist_ok=True)
    cache_key = _cache_key(
        scene,
        original_image,
        selected_products,
        style_prompt,
        model,
    )
    output_path = cache_root / f"{cache_key}.svg"
    guide_path = cache_root / f"{cache_key}_guide.png"
    failure_path = cache_root / f"{cache_key}.failed"
    if output_path.is_file():
        return output_path
    if (
        failure_path.is_file()
        and time.time() - failure_path.stat().st_mtime < FAILURE_COOLDOWN_SECONDS
    ):
        raise RuntimeError("최근 SVG 렌더 생성이 실패해 잠시 재호출을 보류합니다.")

    with _GENERATION_LOCK:
        if output_path.is_file():
            return output_path
        if (
            failure_path.is_file()
            and time.time() - failure_path.stat().st_mtime
            < FAILURE_COOLDOWN_SECONDS
        ):
            raise RuntimeError("최근 SVG 렌더 생성이 실패해 잠시 재호출을 보류합니다.")

        render_layout_guide(scene, guide_path)
        contents: list[Any] = [
            _path_part(original_image),
            _path_part(guide_path),
        ]
        product_references: list[str] = []
        for product in selected_products[:MAX_PRODUCT_IMAGES]:
            image_url = str(product.get("image") or "").strip()
            if not image_url:
                continue
            try:
                contents.append(_url_part(image_url))
            except Exception:
                continue
            product_references.append(
                f"- Reference image {len(contents)}: "
                f"{product.get('label') or product.get('type') or 'furniture'} — "
                f"{str(product.get('title') or '')[:180]}"
            )
        contents.insert(0, _prompt(scene, style_prompt, product_references))

        try:
            # Client를 변수로 붙들어 둔다. _client().models... 처럼 임시객체로
            # 쓰면 .models를 꺼낸 순간 Client의 참조가 사라져 GC가 수거하고,
            # 그때 내부 HTTP 커넥션이 닫혀 "client has been closed"로 실패한다.
            client = _client()
            svg_text = None
            failures: list[str] = []
            for candidate_model in _room_svg_models(model):
                try:
                    response = call_with_retry(
                        client.models.generate_content,
                        model=candidate_model,
                        contents=contents,
                        config=types.GenerateContentConfig(
                            response_mime_type="text/plain",
                            temperature=0.2,
                            max_output_tokens=16000,
                        ),
                        description=(
                            f"입체 렌더 SVG 생성({candidate_model})"
                        ),
                        # 같은 모델만 오래 두드리지 않고 한 번 재시도한 뒤
                        # 용량이 다른 안정 모델로 전환한다.
                        retry_attempts=2,
                        retry_base_delay=5.0,
                        retry_max_total_wait=15.0,
                    )
                    if not response.text:
                        raise RuntimeError(
                            "Gemini가 입체 렌더 SVG를 반환하지 않았습니다."
                        )
                    svg_text = _sanitize_svg(
                        _extract_svg(str(response.text))
                    )
                    break
                except (
                    GeminiBusyError,
                    RuntimeError,
                    ValueError,
                    ET.ParseError,
                ) as model_exc:
                    failures.append(
                        f"{candidate_model}: "
                        f"{type(model_exc).__name__}: {model_exc}"
                    )
                    print(
                        "[gemini-room-svg] "
                        f"{candidate_model} 실패, 대체 모델 확인: {model_exc}"
                    )

            if svg_text is None:
                raise RuntimeError(
                    "모든 입체 SVG 모델이 실패했습니다. " + " | ".join(failures)
                )
            temporary_path = output_path.with_suffix(".tmp")
            temporary_path.write_text(svg_text, encoding="utf-8")
            temporary_path.replace(output_path)
            failure_path.unlink(missing_ok=True)
            return output_path
        except Exception as exc:
            # 타임스탬프만 남기면, 쿨다운 동안 호출부가 받는 메시지가
            # "재호출을 보류합니다" 뿐이라 정작 원인이 로그에서 밀려 사라진다.
            # 사유를 함께 적어 두면 파일만 봐도 원인을 알 수 있다.
            # 쿨다운 판정은 st_mtime 기준이라 내용이 늘어도 영향이 없다.
            failure_path.write_text(
                f"{int(time.time())}\n{type(exc).__name__}: {exc}",
                encoding="utf-8",
            )
            raise
