"""model2 Gemini SVG 실험을 기존 Flask 평면도 흐름에 연결하는 어댑터."""
from __future__ import annotations

import json
import hashlib
import io
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
import requests
from PIL import Image

from .gemini_retry import call_with_retry
from .gemini_svg_experiment import _extract_svg, generate_svg_text
from .product_icon_svg import generate_product_icon_svg
from .topdown_experiment.run import analyze_room


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ANALYSIS_MODEL = "gemini-3.6-flash"

TYPE_MAP = {
    "bed": "bed",
    "desk": "desk",
    "table": "table",
    "coffee_table": "low_table",
    "low_table": "low_table",
    "chair": "chair",
    "floor_chair": "floor_chair",
    "stool": "stool",
    "shelf": "shelf",
    "cabinet": "cabinet",
    "dresser": "cabinet",
    "nightstand": "cabinet",
    "rug": "rug",
    "mirror": "mirror",
    "lamp": "lamp",
    "plant": "plant",
    "door": "door",
    "window": "window",
}

SELECTABLE_CATEGORIES = {
    "bed",
    "desk",
    "table",
    "coffee_table",
    "low_table",
    "chair",
    "floor_chair",
    "stool",
    "shelf",
    "cabinet",
    "dresser",
    "nightstand",
    "rug",
    "mirror",
    "lamp",
    "plant",
    "sofa",
}

SVG_NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", SVG_NS)


def _client() -> genai.Client:
    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("프로젝트 .env에 GEMINI_API_KEY가 없습니다.")
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(
            client_args={"trust_env": False},
            async_client_args={"trust_env": False},
        ),
    )


def enrich_products_with_visual_profiles(
    products: list[dict[str, Any]],
    cache_dir: str | Path,
) -> list[dict[str, Any]]:
    """Analyze selected product photos with Gemini, with a local fallback."""
    cache_root = Path(cache_dir).expanduser()
    if not cache_root.is_absolute():
        cache_root = PROJECT_ROOT / cache_root
    cache_root = cache_root.resolve()
    gemini_cache_dir = cache_root / "product_visuals_gemini_v3"
    local_cache_dir = cache_root / "product_visuals_local_v3"
    # Bump the cache whenever the photo-to-icon contract changes.  Reusing
    # v2 here would keep serving the old generic/incorrectly colored icons.
    direct_svg_cache_dir = cache_root / "product_icon_svg_v4"
    gemini_cache_dir.mkdir(parents=True, exist_ok=True)
    local_cache_dir.mkdir(parents=True, exist_ok=True)
    direct_svg_cache_dir.mkdir(parents=True, exist_ok=True)
    direct_svg_error_dir = cache_root / "product_icon_errors_v4"
    direct_svg_error_dir.mkdir(parents=True, exist_ok=True)
    gemini_client: genai.Client | None = None
    gemini_unavailable = False

    for product in products:
        image_url = str(
            product.get("image")
            or ""
        ).strip()
        if not image_url:
            continue

        cache_key = hashlib.sha256(
            image_url.encode("utf-8")
        ).hexdigest()[:24]
        gemini_cache_path = gemini_cache_dir / f"{cache_key}.json"
        local_cache_path = local_cache_dir / f"{cache_key}.json"
        direct_svg_cache_path = direct_svg_cache_dir / f"{cache_key}.svg"
        direct_svg_error_path = direct_svg_error_dir / f"{cache_key}.txt"

        try:
            image_bytes: bytes | None = None
            mime_type = "image/jpeg"
            if direct_svg_cache_path.exists():
                product["icon_svg"] = direct_svg_cache_path.read_text(
                    encoding="utf-8"
                )

            needs_download = (
                "icon_svg" not in product
                or (
                    not gemini_cache_path.exists()
                    and not local_cache_path.exists()
                )
            )
            if needs_download:
                with requests.Session() as image_session:
                    image_session.trust_env = False
                    response = image_session.get(
                        image_url,
                        timeout=15,
                    )
                    response.raise_for_status()
                image_bytes = response.content
                mime_type = (
                    response.headers.get(
                        "Content-Type",
                        "image/jpeg",
                    )
                    .split(";", 1)[0]
                    .strip()
                )
                if not mime_type.startswith("image/"):
                    mime_type = "image/jpeg"

            # The attached prototype's direct photo -> SVG route is primary.
            # It costs one Gemini call per newly selected product and is cached.
            if (
                "icon_svg" not in product
                and image_bytes
                and not gemini_unavailable
            ):
                try:
                    if gemini_client is None:
                        gemini_client = _client()
                    icon_svg = generate_product_icon_svg(
                        gemini_client,
                        image_bytes,
                        mime_type,
                        title=str(product.get("title") or ""),
                        category=str(product.get("type") or ""),
                        # When no explicit override is configured,
                        # product_icon_svg selects the model by category:
                        # prominent furniture uses Flash; smaller items Lite.
                        model=(
                            os.getenv("GEMINI_PRODUCT_ICON_MODEL", "").strip()
                            or None
                        ),
                    )
                    direct_svg_cache_path.write_text(
                        icon_svg,
                        encoding="utf-8",
                    )
                    direct_svg_error_path.unlink(
                        missing_ok=True
                    )
                    product["icon_svg"] = icon_svg
                except Exception as exc:
                    # A malformed SVG or one problematic product must not
                    # prevent the remaining selected products from being
                    # analyzed. Only quota/auth/connectivity failures make
                    # further calls in this request pointless.
                    error_text = str(exc).lower()
                    gemini_unavailable = any(
                        marker in error_text
                        for marker in (
                            "resource_exhausted",
                            "quota",
                            "429",
                            "api key",
                            "permission_denied",
                            "connection",
                            "timed out",
                        )
                    )
                    print(
                        "[product-icon] "
                        f"Gemini 직접 SVG 생성 실패, 로컬 아이콘으로 대체: {exc}"
                    )
                    direct_svg_error_path.write_text(
                        str(exc),
                        encoding="utf-8",
                    )

            if gemini_cache_path.exists():
                profile = json.loads(
                    gemini_cache_path.read_text(
                        encoding="utf-8"
                    )
                )
            elif local_cache_path.exists():
                profile = json.loads(
                    local_cache_path.read_text(
                        encoding="utf-8"
                    )
                )
            else:
                if image_bytes is None:
                    raise RuntimeError("상품 이미지 데이터가 없습니다.")
                profile = _local_product_visual_profile(
                    image_bytes,
                    product,
                )
                local_cache_path.write_text(
                    json.dumps(
                        profile,
                        ensure_ascii=False,
                        indent=2,
                    ),
                    encoding="utf-8",
                )

            profile = _apply_product_title_cues(
                profile,
                product,
            )
            product["visual_profile"] = (
                _normalize_visual_profile(
                    profile
                )
            )

        except Exception as exc:
            print(
                "[product-visual] "
                f"상품 이미지 분석 실패: {exc}"
            )

    return products


def _local_product_visual_profile(
    image_bytes: bytes,
    product: dict[str, Any],
) -> dict[str, Any]:
    """Infer a compact icon palette and silhouette without any remote AI."""
    with Image.open(
        io.BytesIO(
            image_bytes
        )
    ) as source:
        image = source.convert(
            "RGB"
        )
        image.thumbnail(
            (192, 192)
        )

    width, height = image.size
    crop = image.crop(
        (
            int(
                width * 0.08
            ),
            int(
                height * 0.08
            ),
            max(
                int(
                    width * 0.92
                ),
                1,
            ),
            max(
                int(
                    height * 0.92
                ),
                1,
            ),
        )
    )
    quantized = crop.quantize(
        colors=12,
        method=Image.Quantize.MEDIANCUT,
    ).convert(
        "RGB"
    )
    colors = quantized.getcolors(
        maxcolors=(
            quantized.width
            * quantized.height
        )
    ) or []

    ranked_colors = []
    for count, rgb in colors:
        red, green, blue = rgb
        maximum = max(
            rgb
        )
        minimum = min(
            rgb
        )
        brightness = (
            red
            + green
            + blue
        ) / 3
        if (
            brightness > 244
            or brightness < 18
        ):
            continue
        saturation = (
            maximum
            - minimum
        ) / max(
            maximum,
            1,
        )
        weight = count * (
            0.65
            + saturation
        )
        ranked_colors.append(
            (
                weight,
                rgb,
            )
        )

    ranked_colors.sort(
        reverse=True
    )
    primary_rgb = (
        ranked_colors[0][1]
        if ranked_colors
        else (
            183,
            139,
            104,
        )
    )
    secondary_rgb = None
    for _, candidate in ranked_colors[1:]:
        distance = sum(
            (
                candidate[index]
                - primary_rgb[index]
            )
            ** 2
            for index
            in range(3)
        ) ** 0.5
        if distance >= 42:
            secondary_rgb = candidate
            break
    if secondary_rgb is None:
        secondary_rgb = tuple(
            min(
                255,
                int(
                    channel
                    + (
                        255
                        - channel
                    )
                    * 0.48
                ),
            )
            for channel
            in primary_rgb
        )

    def hex_color(
        rgb: tuple[int, int, int],
    ) -> str:
        return (
            "#"
            + "".join(
                f"{channel:02X}"
                for channel
                in rgb
            )
        )

    item_type = str(
        product.get("type")
        or "unknown"
    ).lower()
    title = str(
        product.get("title")
        or ""
    ).lower()

    shape = {
        "bed": "rounded_rectangle",
        "sofa": "rounded_rectangle",
        "chair": "rounded_rectangle",
        "desk": "rectangle",
        "table": "rounded_rectangle",
        "bench": "rounded_rectangle",
        "shelf": "rectangle",
        "cabinet": "rectangle",
        "dresser": "rectangle",
        "wardrobe": "rectangle",
        "lamp": "circle",
        "rug": "rectangle",
        "plant": "irregular",
    }.get(
        item_type,
        "rounded_rectangle",
    )
    if any(
        cue in title
        for cue
        in (
            "원형",
            "동그란",
            "라운드 테이블",
            "round",
            "circle",
        )
    ):
        shape = "circle"
    elif any(
        cue in title
        for cue
        in (
            "타원",
            "오벌",
            "oval",
        )
    ):
        shape = "oval"
    elif any(
        cue in title
        for cue
        in (
            "l자",
            "ㄱ자",
            "코너형",
            "l-shaped",
        )
    ):
        shape = "l_shape"
    elif any(
        cue in title
        for cue
        in (
            "사각",
            "스퀘어",
            "square",
        )
    ):
        shape = "square"

    material_cues = (
        (
            (
                "벨벳",
                "테디",
                "패브릭",
                "린넨",
                "fabric",
                "velvet",
            ),
            "fabric",
        ),
        (
            (
                "원목",
                "오크",
                "월넛",
                "wood",
            ),
            "wood",
        ),
        (
            (
                "가죽",
                "레더",
                "leather",
            ),
            "leather",
        ),
        (
            (
                "아크릴",
                "acrylic",
            ),
            "plastic",
        ),
        (
            (
                "메탈",
                "철제",
                "스틸",
                "크롬",
                "metal",
                "steel",
            ),
            "metal",
        ),
        (
            (
                "라탄",
                "위빙",
            ),
            "woven",
        ),
        (
            (
                "유리",
                "글라스",
                "glass",
            ),
            "glass",
        ),
    )
    material = "mixed"
    for cues, material_name in material_cues:
        if any(
            cue in title
            for cue
            in cues
        ):
            material = material_name
            break
    if item_type == "rug":
        # Rug listing titles often contain option names such as 월넛/브라운,
        # which must not turn the floor textile into a wooden object.
        material = "woven"

    # Shopping photos often contain a large wall/floor background. Explicit
    # product color words are therefore a more reliable local correction.
    named_colors = (
        (
            (
                "아쿠아블루",
                "aqua blue",
                "aquablue",
            ),
            (68, 145, 178),
            (157, 211, 229),
        ),
        (
            (
                "라이트블루",
                "light blue",
            ),
            (119, 184, 216),
            (194, 226, 239),
        ),
        (
            (
                "스모크그레이",
                "smoke grey",
                "smoke gray",
            ),
            (91, 96, 102),
            (169, 174, 179),
        ),
        (
            (
                "카멜브라운",
                "camel brown",
            ),
            (174, 112, 65),
            (220, 176, 137),
        ),
        (
            (
                "베이비핑크",
                "baby pink",
            ),
            (226, 158, 181),
            (247, 211, 223),
        ),
        (
            (
                "라벤더",
                "lavender",
            ),
            (158, 143, 204),
            (218, 210, 238),
        ),
        (
            (
                "민트",
                "mint",
            ),
            (110, 188, 163),
            (190, 226, 214),
        ),
        (
            (
                "세이지그린",
                "sage green",
            ),
            (126, 151, 121),
            (190, 205, 185),
        ),
        (
            (
                "매트블랙",
                "matte black",
            ),
            (48, 49, 51),
            (109, 111, 114),
        ),
        (
            (
                "아이보리",
                "ivory",
            ),
            (225, 215, 193),
            (247, 242, 230),
        ),
        (
            (
                "블루",
                " blue",
            ),
            (57, 111, 157),
            (143, 187, 215),
        ),
    )
    for cues, named_primary, named_secondary in named_colors:
        if any(
            cue in title
            for cue
            in cues
        ):
            primary_rgb = named_primary
            secondary_rgb = named_secondary
            break

    return {
        "shape": shape,
        "primary_color": hex_color(
            primary_rgb
        ),
        "secondary_color": hex_color(
            secondary_rgb
        ),
        "material": material,
        "corner_roundness": (
            0.38
            if shape
            == "rounded_rectangle"
            else 0.08
        ),
        "has_center_division": (
            item_type
            in {
                "sofa",
                "cabinet",
                "dresser",
                "wardrobe",
            }
        ),
        "seat_count": (
            2
            if item_type == "sofa"
            else 1
            if item_type in {"chair", "floor_chair"}
            else 0
        ),
        "has_armrests": item_type in {"sofa", "chair"},
        "has_headboard": item_type == "bed",
        "has_cushions": item_type in {"bed", "sofa", "chair"},
        "storage_type": "none",
        "is_frame_only": False,
        "leg_style": "none",
        "pattern": "solid",
        "has_border": True,
        "detail": (
            "local color and silhouette"
        ),
        "analysis_source": "local",
    }


def _apply_product_title_cues(
    profile: dict[str, Any],
    product: dict[str, Any],
) -> dict[str, Any]:
    """Apply high-confidence title facts after either visual analyzer.

    Shopping photographs frequently devote more pixels to a wall, floor or
    bedding than to the sold frame itself.  Exact option words in the product
    title are more reliable for these facts and should win over that noise.
    """
    result = dict(profile or {})
    title = re.sub(
        r"\s+",
        " ",
        str(product.get("title") or "").lower(),
    )
    item_type = str(product.get("type") or "").lower()

    color_cues = (
        (("화이트 오크", "화이트오크", "white oak"), "#E6DDCD", "#F6F2E9"),
        (("퓨어화이트", "화이트", "white"), "#EEECE6", "#FAF9F5"),
        (("아이보리", "ivory"), "#E7DDC9", "#F8F3E8"),
        (("오크", "oak"), "#C9A875", "#E7D2AD"),
        (("월넛", "walnut"), "#765238", "#B08A67"),
        (("매트블랙", "블랙", "black"), "#343434", "#777777"),
        (("스모크그레이", "그레이", "grey", "gray"), "#77797C", "#B9BBBD"),
        (("아쿠아블루", "아쿠아", "aqua"), "#4F9FC2", "#A7D9E9"),
        (("라이트블루", "light blue"), "#78B8D8", "#C7E5F1"),
        (("블루", "blue"), "#3F76A4", "#91BCD8"),
        (("라벤더", "lavender"), "#A394CF", "#DDD5EF"),
        (("민트", "mint"), "#78BCA7", "#C5E4DA"),
        (("베이비핑크", "핑크", "pink"), "#DFA5B9", "#F3D5DF"),
        (("베이지", "beige"), "#CDBA9E", "#EEE4D3"),
    )
    exact_material_color = any(
        cue in title
        for cue in (
            "화이트 오크",
            "화이트오크",
            "white oak",
        )
    )
    matched_colors = [
        (cues, primary, secondary)
        for cues, primary, secondary in color_cues
        if any(cue in title for cue in cues)
    ]
    if exact_material_color:
        result["primary_color"] = "#E6DDCD"
        result["secondary_color"] = "#F6F2E9"
    elif len(matched_colors) == 1:
        cues, primary, secondary = matched_colors[0]
        current_color = str(
            result.get("primary_color") or ""
        )
        current_saturation = 0.0
        if re.fullmatch(r"#[0-9A-Fa-f]{6}", current_color):
            rgb = [
                int(current_color[index:index + 2], 16)
                for index in (1, 3, 5)
            ]
            current_saturation = (
                max(rgb) - min(rgb)
            ) / max(max(rgb), 1)
        image_is_ambiguous = (
            str(result.get("analysis_source") or "") != "local"
            or current_saturation < 0.08
        )
        if image_is_ambiguous:
            result["primary_color"] = primary
            result["secondary_color"] = secondary

    if item_type == "bed":
        frame_only = any(
            cue in title
            for cue in ("침대 프레임", "침대프레임", "프레임", "bed frame")
        )
        upholstered = any(
            cue in title
            for cue in ("패브릭", "벨벳", "가죽", "쿠션", "업홀스터")
        )
        result["is_frame_only"] = frame_only
        result["has_cushions"] = bool(upholstered and not frame_only)
        result["has_headboard"] = not any(
            cue in title for cue in ("무헤드", "헤드리스", "no headboard")
        )
        if frame_only and not upholstered:
            result["shape"] = "rectangle"
            try:
                current_roundness = float(
                    result.get("corner_roundness")
                    or 0.08
                )
            except (TypeError, ValueError):
                current_roundness = 0.08
            result["corner_roundness"] = min(
                current_roundness,
                0.10,
            )
            if any(cue in title for cue in ("원목", "오크", "우드", "wood")):
                result["material"] = "wood"

        if any(cue in title for cue in ("리프트업", "리프트 업", "lift-up", "lift up")):
            result["storage_type"] = "lift_up"
        elif any(cue in title for cue in ("서랍", "drawer")):
            result["storage_type"] = "drawers"
        elif any(cue in title for cue in ("오픈 수납", "선반형", "open shelf")):
            result["storage_type"] = "open_shelf"
        elif "수납" in title:
            result["storage_type"] = "drawers"

    return result


def _normalize_visual_profile(
    profile: dict[str, Any],
) -> dict[str, Any]:
    shapes = {
        "rectangle",
        "rounded_rectangle",
        "square",
        "circle",
        "oval",
        "l_shape",
        "irregular",
    }

    def color(value: Any, fallback: str) -> str:
        text = str(value or "").strip()
        return (
            text.upper()
            if re.fullmatch(
                r"#[0-9a-fA-F]{6}",
                text,
            )
            else fallback
        )

    shape = str(
        profile.get("shape")
        or "rounded_rectangle"
    ).lower()
    try:
        roundness = min(
            1.0,
            max(
                0.0,
                float(
                    profile.get(
                        "corner_roundness",
                        0.15,
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        roundness = 0.15

    try:
        seat_count = min(
            6,
            max(
                0,
                int(
                    profile.get(
                        "seat_count",
                        0,
                    )
                ),
            ),
        )
    except (TypeError, ValueError):
        seat_count = 0

    leg_styles = {
        "none",
        "wood",
        "metal",
        "sled",
        "four_legs",
        "pedestal",
    }
    patterns = {
        "solid",
        "striped",
        "checkered",
        "geometric",
        "floral",
        "woven",
    }
    leg_style = str(
        profile.get("leg_style")
        or "none"
    ).lower()
    pattern = str(
        profile.get("pattern")
        or "solid"
    ).lower()

    normalized_parts: list[dict[str, Any]] = []
    raw_parts = profile.get("parts")
    if isinstance(raw_parts, list):
        allowed_primitives = {
            "rect",
            "ellipse",
            "line",
            "polygon",
        }
        allowed_fills = {
            "primary",
            "secondary",
            "accent",
            "light",
            "dark",
            "none",
        }

        def unit(value: Any, default: float = 0.0) -> float:
            try:
                return min(1.0, max(0.0, float(value)))
            except (TypeError, ValueError):
                return default

        for index, raw_part in enumerate(raw_parts[:24]):
            if not isinstance(raw_part, dict):
                continue
            primitive = str(
                raw_part.get("primitive") or ""
            ).lower()
            if primitive not in allowed_primitives:
                continue
            fill = str(
                raw_part.get("fill") or "secondary"
            ).lower()
            if fill not in allowed_fills:
                fill = "secondary"
            try:
                z_index = min(
                    20,
                    max(-20, int(raw_part.get("z_index", index))),
                )
            except (TypeError, ValueError):
                z_index = index
            part = {
                "primitive": primitive,
                "role": re.sub(
                    r"[^a-z_]",
                    "",
                    str(raw_part.get("role") or "detail").lower(),
                )[:24],
                "x": unit(raw_part.get("x")),
                "y": unit(raw_part.get("y")),
                "width": unit(raw_part.get("width"), 0.1),
                "height": unit(raw_part.get("height"), 0.1),
                "corner_roundness": unit(
                    raw_part.get("corner_roundness")
                ),
                "fill": fill,
                "stroke": bool(raw_part.get("stroke", True)),
                "stroke_width": min(
                    0.04,
                    max(
                        0.002,
                        unit(raw_part.get("stroke_width"), 0.01),
                    ),
                ),
                "z_index": z_index,
            }
            if primitive == "polygon":
                raw_points = raw_part.get("points")
                points = []
                if isinstance(raw_points, list):
                    for raw_point in raw_points[:10]:
                        if (
                            isinstance(raw_point, (list, tuple))
                            and len(raw_point) >= 2
                        ):
                            points.append(
                                [
                                    unit(raw_point[0]),
                                    unit(raw_point[1]),
                                ]
                            )
                if len(points) < 3:
                    continue
                part["points"] = points
            normalized_parts.append(part)
    normalized_parts.sort(
        key=lambda part: int(part["z_index"])
    )

    return {
        "shape": (
            shape
            if shape in shapes
            else "rounded_rectangle"
        ),
        "primary_color": color(
            profile.get("primary_color"),
            "#B78B68",
        ),
        "secondary_color": color(
            profile.get("secondary_color"),
            "#E8DCCB",
        ),
        "material": str(
            profile.get("material")
            or "mixed"
        ),
        "corner_roundness": roundness,
        "has_center_division": bool(
            profile.get(
                "has_center_division"
            )
        ),
        "seat_count": seat_count,
        "has_armrests": bool(
            profile.get("has_armrests")
        ),
        "has_headboard": bool(
            profile.get("has_headboard")
        ),
        "has_cushions": bool(
            profile.get("has_cushions")
        ),
        "storage_type": (
            str(profile.get("storage_type") or "none")
            if str(profile.get("storage_type") or "none")
            in {"none", "lift_up", "drawers", "open_shelf"}
            else "none"
        ),
        "is_frame_only": bool(
            profile.get("is_frame_only")
        ),
        "leg_style": (
            leg_style
            if leg_style in leg_styles
            else "none"
        ),
        "pattern": (
            pattern
            if pattern in patterns
            else "solid"
        ),
        "has_border": bool(
            profile.get(
                "has_border",
                True,
            )
        ),
        "detail": str(
            profile.get("detail")
            or ""
        )[:80],
        "analysis_source": str(
            profile.get("analysis_source")
            or "local"
        )[:16],
        "parts": normalized_parts,
    }


def _legacy_layout(
    scene: dict[str, Any],
    *,
    room_width: float | None,
    room_depth: float | None,
) -> dict[str, Any]:
    room = dict(scene.get("room") or {})
    if room_width and room_depth:
        aspect_ratio = float(room_width) / float(room_depth)
    else:
        aspect_ratio = float(room.get("aspect_ratio_width_to_depth") or 0.75)

    objects = []
    for source_index, obj in enumerate(scene.get("objects") or []):
        category = str(obj.get("category") or "unknown").lower()
        object_type = TYPE_MAP.get(category, "unknown")
        anchors = list(obj.get("wall_anchors") or [])
        wall = anchors[0] if anchors else "none"
        objects.append(
            {
                "type": object_type,
                "label": str(obj.get("label_ko") or category or "가구"),
                "x": float(obj.get("x", 0.5)),
                "y": float(obj.get("y", 0.5)),
                "w": float(obj.get("width", 0.12)),
                "h": float(obj.get("depth", 0.12)),
                "wall": wall,
                "confidence": float(obj.get("confidence", 0.5)),
                "source": "model2_gemini_svg",
                "source_index": source_index,
                "scene_id": str(obj.get("id") or f"object_{source_index}"),
            }
        )

    return {
        "schema": "rule_based_v3",
        "renderer_source": "model2_gemini_svg",
        "room": {
            "aspect_ratio": aspect_ratio,
            "width_m": room_width,
            "depth_m": room_depth,
        },
        "objects": objects,
    }


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 생성 결과 캐시 유효 시간(초). 30분이 지난 결과는 버리고 다시 만든다.
FLOORPLAN_CACHE_TTL_SECONDS = 1800


def _is_cache_fresh(path: Path) -> bool:
    """캐시 파일이 존재하고 TTL 안쪽이면 True.

    shutil.copy2가 mtime을 보존하므로, 같은 사진을 다른 이름으로 올려
    캐시를 복사해 와도 원본이 만들어진 시각을 기준으로 만료된다.
    """
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return False
    return age < FLOORPLAN_CACHE_TTL_SECONDS


def _reuse_identical_upload_cache(
    image_path: Path,
    output_dir: Path,
    *,
    scene_path: Path,
    svg_path: Path,
    base_svg_path: Path,
    raw_path: Path,
) -> bool:
    """파일명이 달라도 내용이 같은 업로드면 기존 Gemini 결과를 복사한다."""
    if _is_cache_fresh(scene_path) and _is_cache_fresh(svg_path):
        return True
    source_digest = _file_digest(image_path)
    for candidate in image_path.parent.iterdir():
        if (
            candidate == image_path
            or not candidate.is_file()
            or candidate.stat().st_size != image_path.stat().st_size
        ):
            continue
        if _file_digest(candidate) != source_digest:
            continue
        candidate_scene = output_dir / f"{candidate.stem}_model2_scene.json"
        candidate_svg = output_dir / f"{candidate.stem}_model2_floorplan.svg"
        if not (
            _is_cache_fresh(candidate_scene)
            and _is_cache_fresh(candidate_svg)
        ):
            continue
        shutil.copy2(candidate_scene, scene_path)
        shutil.copy2(candidate_svg, svg_path)
        candidate_base_svg = (
            output_dir
            / (
                f"{candidate.stem}"
                "_model2_floorplan_base.svg"
            )
        )
        if candidate_base_svg.exists():
            shutil.copy2(
                candidate_base_svg,
                base_svg_path,
            )
        candidate_raw = output_dir / f"{candidate.stem}_model2_svg_response.txt"
        if candidate_raw.exists():
            shutil.copy2(candidate_raw, raw_path)
        return True
    return False


def _apply_room_dimensions_to_svg(
    base_svg_path: Path,
    output_svg_path: Path,
    *,
    room_width: float | None,
    room_depth: float | None,
) -> None:
    """Rescale a cached floorplan to the explicitly entered room ratio."""
    if not (
        room_width
        and room_depth
    ):
        shutil.copy2(
            base_svg_path,
            output_svg_path,
        )
        return

    tree = ET.parse(
        base_svg_path
    )
    root = tree.getroot()
    floor_x, floor_y, floor_width, floor_height = (
        _floor_box(
            root
        )
    )
    if (
        floor_width <= 0
        or floor_height <= 0
    ):
        shutil.copy2(
            base_svg_path,
            output_svg_path,
        )
        return

    target_aspect = (
        float(
            room_width
        )
        / float(
            room_depth
        )
    )
    current_aspect = (
        floor_width
        / floor_height
    )
    scale_x = (
        target_aspect
        / current_aspect
    )
    scale_x = min(
        3.5,
        max(
            0.30,
            scale_x,
        ),
    )

    view_box_values = [
        float(value)
        for value
        in str(
            root.get(
                "viewBox",
                "0 0 1000 1000",
            )
        ).replace(
            ",",
            " ",
        ).split()
    ]
    if len(
        view_box_values
    ) != 4:
        view_box_values = [
            0.0,
            0.0,
            1000.0,
            1000.0,
        ]
    view_x, view_y, view_width, view_height = (
        view_box_values
    )
    center_x = (
        floor_x
        + floor_width / 2
    )

    scalable_children = []
    for child in list(
        root
    ):
        local_tag = child.tag.rsplit(
            "}",
            1,
        )[-1]
        if local_tag in {
            "defs",
            "style",
            "metadata",
            "title",
            "desc",
        }:
            continue
        scalable_children.append(
            child
        )
        root.remove(
            child
        )

    dimension_group = ET.SubElement(
        root,
        _svg_tag("g"),
        {
            "id": "room-dimension-scale",
            "transform": (
                f"translate({center_x:.3f} 0) "
                f"scale({scale_x:.6f} 1) "
                f"translate({-center_x:.3f} 0)"
            ),
        },
    )
    for child in scalable_children:
        dimension_group.append(
            child
        )

    new_view_x = (
        center_x
        + (
            view_x
            - center_x
        )
        * scale_x
    )
    new_view_width = (
        view_width
        * scale_x
    )
    root.set(
        "viewBox",
        (
            f"{new_view_x:.3f} "
            f"{view_y:.3f} "
            f"{new_view_width:.3f} "
            f"{view_height:.3f}"
        ),
    )
    root.set(
        "width",
        f"{new_view_width:.0f}",
    )
    root.set(
        "height",
        f"{view_height:.0f}",
    )
    root.set(
        "data-room-width-m",
        str(
            room_width
        ),
    )
    root.set(
        "data-room-depth-m",
        str(
            room_depth
        ),
    )
    root.set(
        "data-room-aspect",
        f"{target_aspect:.6f}",
    )
    tree.write(
        output_svg_path,
        encoding="utf-8",
        xml_declaration=True,
    )


def generate_floorplan_for_web(
    image_path: str | Path,
    output_dir: str | Path,
    *,
    analysis_model: str | None = None,
    skip_existing: bool = True,
    room_width: float | None = None,
    room_depth: float | None = None,
) -> dict[str, Any]:
    """사진 → model2 장면 분석 → 상세 SVG + 수정 기능용 호환 layout."""
    load_dotenv(PROJECT_ROOT / ".env")
    image_path = Path(image_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    base_model = analysis_model or os.getenv(
        "GEMINI_ANALYSIS_MODEL",
        DEFAULT_ANALYSIS_MODEL,
    )
    # 두 단계는 모델에 요구하는 바가 다르다.
    #  - 배치 분석: 구조화된 JSON. normalize_layout이 빠진 필드를 메워줘서
    #    가벼운 모델로도 버틴다. 무료 등급 RPD가 큰 lite 계열이 유리하다.
    #  - SVG 생성: layout id와 정확히 일치하는 <g id="...">를 붙여야 해서
    #    (prepare_floorplan_edit_markup / _remove_object_and_label이 이 id로
    #    가구를 찾는다) 지시 준수력이 좋은 모델이 필요하다.
    # .env에서 따로 지정할 수 있고, 없으면 기존처럼 한 모델을 공유한다.
    # 호출자가 analysis_model을 명시하면 그 값을 그대로 존중한다.
    layout_model = (
        analysis_model
        or os.getenv("GEMINI_LAYOUT_MODEL", "").strip()
        or base_model
    )
    svg_model = (
        analysis_model
        or os.getenv("GEMINI_SVG_MODEL", "").strip()
        or base_model
    )
    stem = image_path.stem
    scene_path = output_dir / f"{stem}_model2_scene.json"
    layout_path = output_dir / f"{stem}_model2_layout.json"
    svg_path = output_dir / f"{stem}_model2_floorplan.svg"
    base_svg_path = (
        output_dir
        / f"{stem}_model2_floorplan_base.svg"
    )
    raw_path = output_dir / f"{stem}_model2_svg_response.txt"

    if skip_existing:
        _reuse_identical_upload_cache(
            image_path,
            output_dir,
            scene_path=scene_path,
            svg_path=svg_path,
            base_svg_path=(
                base_svg_path
            ),
            raw_path=raw_path,
        )

    client = _client()
    scene_reused = skip_existing and _is_cache_fresh(scene_path)
    if scene_reused:
        scene = json.loads(scene_path.read_text(encoding="utf-8"))
    else:
        scene = call_with_retry(
            analyze_room,
            client,
            image_path,
            layout_model,
            description="방 배치 분석",
        )
        scene_path.write_text(
            json.dumps(scene, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if room_width and room_depth:
        scene.setdefault("room", {})[
            "aspect_ratio_width_to_depth"
        ] = float(room_width) / float(room_depth)

    legacy = _legacy_layout(
        scene,
        room_width=room_width,
        room_depth=room_depth,
    )
    layout_path.write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # scene을 새로 뽑았으면 SVG도 반드시 다시 만든다. 새 layout의 객체 id와
    # 옛 SVG의 <g id="...">가 어긋나면 수정·삭제 기능이 조용히 죽는다.
    if not (scene_reused and _is_cache_fresh(svg_path)):
        svg_text, raw_text = call_with_retry(
            generate_svg_text,
            client,
            image_path,
            scene,
            model=svg_model,
            description="평면도 SVG 생성",
        )
        svg_path.write_text(svg_text, encoding="utf-8")
        base_svg_path.write_text(
            svg_text,
            encoding="utf-8",
        )
        raw_path.write_text(raw_text, encoding="utf-8")

    if not base_svg_path.exists():
        shutil.copy2(
            svg_path,
            base_svg_path,
        )

    _apply_room_dimensions_to_svg(
        base_svg_path,
        svg_path,
        room_width=room_width,
        room_depth=room_depth,
    )

    svg_markup = svg_path.read_text(encoding="utf-8")
    scene_objects = list(scene.get("objects") or [])
    furniture_objects = []
    for index, obj in enumerate(legacy["objects"]):
        scene_category = (
            str(scene_objects[index].get("category") or "").lower()
            if index < len(scene_objects)
            else ""
        )
        if scene_category not in SELECTABLE_CATEGORIES:
            continue
        furniture_objects.append(
            {
                "source_index": index,
                "type": str(obj.get("type") or "unknown"),
                "label": str(obj.get("label") or obj.get("type") or "가구"),
            }
        )
    return {
        "svg_path": str(svg_path),
        "layout_file": str(layout_path),
        "svg_markup": svg_markup,
        "objects": furniture_objects,
        "provider": "model2_gemini_svg",
        # 어느 모델이 만든 결과인지 남긴다. 503/품질 문제를 추적할 때 필요하다.
        "layout_model": layout_model,
        "svg_model": svg_model,
    }


def _svg_tag(name: str) -> str:
    return f"{{{SVG_NS}}}{name}"


def _remove_element(root: ET.Element, target: ET.Element) -> None:
    for parent in root.iter():
        if target in list(parent):
            parent.remove(target)
            return


def _remove_object_and_label(
    root: ET.Element,
    *,
    scene_id: str,
    label: str,
) -> None:
    for element in list(root.iter()):
        element_id = element.get("id")
        if element_id in {scene_id, f"label-{scene_id}"}:
            _remove_element(root, element)

    # 이전 캐시에는 label-* ID가 없으므로 텍스트가 같은 독립 라벨 그룹을 제거한다.
    for group in list(root.iter(_svg_tag("g"))):
        texts = [
            (text.text or "").strip()
            for text in group.iter(_svg_tag("text"))
        ]
        if label and label in texts:
            _remove_element(root, group)


def _floor_box(root: ET.Element) -> tuple[float, float, float, float]:
    candidates = []
    for rect in root.iter(_svg_tag("rect")):
        try:
            x = float(rect.get("x", 0))
            y = float(rect.get("y", 0))
            width = float(rect.get("width", 0))
            height = float(rect.get("height", 0))
        except ValueError:
            continue
        fill = str(rect.get("fill") or "")
        if x > 0 and y > 0 and width >= 500 and height >= 500 and fill != "none":
            candidates.append((width * height, x, y, width, height))
    if not candidates:
        return 100.0, 70.0, 824.0, 880.0
    _, x, y, width, height = min(candidates)
    return x, y, width, height


def _add_text(
    parent: ET.Element,
    *,
    x: float,
    y: float,
    text: str,
    owner_id: str | None = None,
) -> None:
    attributes = {
        "transform": f"translate({x:.1f} {y:.1f})",
        "filter": "url(#shadow-sm)",
        "pointer-events": (
            "all"
            if owner_id
            else "none"
        ),
        "data-base-x": f"{x:.1f}",
        "data-base-y": f"{y:.1f}",
    }
    if owner_id:
        attributes["id"] = f"label-{owner_id}"
        attributes["data-label-for"] = owner_id
        attributes["data-drag-for"] = owner_id
        attributes["style"] = (
            "cursor: grab; user-select: none;"
        )

    label_group = ET.SubElement(
        parent,
        _svg_tag("g"),
        attributes,
    )
    width = max(58, len(text) * 15 + 22)
    ET.SubElement(
        label_group,
        _svg_tag("rect"),
        {
            "x": str(-width / 2),
            "y": "-15",
            "width": str(width),
            "height": "30",
            "rx": "15",
            "fill": "#ffffff",
            "fill-opacity": "0.94",
            "stroke": "#8c6b4c",
            "stroke-width": "1.5",
        },
    )
    text_element = ET.SubElement(
        label_group,
        _svg_tag("text"),
        {
            "x": "0",
            "y": "6",
            "text-anchor": "middle",
            "font-family": "Malgun Gothic, sans-serif",
            "font-size": "15",
            "font-weight": "bold",
            "fill": "#2b1a0e",
        },
    )
    text_element.text = text


def _mix_hex_color(
    color: str,
    target: str,
    amount: float,
) -> str:
    """Blend validated renderer colors without adding external dependencies."""
    if not re.fullmatch(r"#[0-9A-Fa-f]{6}", color):
        color = "#B78B68"
    source_rgb = [
        int(color[index:index + 2], 16)
        for index in (1, 3, 5)
    ]
    target_rgb = [
        int(target[index:index + 2], 16)
        for index in (1, 3, 5)
    ]
    ratio = min(1.0, max(0.0, amount))
    mixed = [
        round(source + (destination - source) * ratio)
        for source, destination in zip(source_rgb, target_rgb)
    ]
    return "#" + "".join(f"{channel:02X}" for channel in mixed)


def _add_direct_product_icon(
    group: ET.Element,
    svg_text: str,
    *,
    width: float,
    height: float,
) -> bool:
    """Fit a previously sanitized standalone icon SVG inside a furniture group."""
    try:
        source_root = ET.fromstring(svg_text)
        view_box_values = [
            float(value)
            for value in re.split(
                r"[\s,]+",
                str(source_root.attrib.get("viewBox") or "0 0 200 200").strip(),
            )
            if value
        ]
        if len(view_box_values) != 4:
            return False
        min_x, min_y, source_width, source_height = view_box_values
        if source_width <= 0 or source_height <= 0:
            return False
        scale = min(
            width / source_width,
            height / source_height,
        ) * 0.96
        translate_x = -source_width * scale / 2 - min_x * scale
        translate_y = -source_height * scale / 2 - min_y * scale
        wrapper = ET.SubElement(
            group,
            _svg_tag("g"),
            {
                "transform": (
                    f"translate({translate_x:.3f} {translate_y:.3f}) "
                    f"scale({scale:.6f})"
                ),
                "data-direct-product-icon": "true",
            },
        )
        children = list(source_root)
        if not children:
            group.remove(wrapper)
            return False
        for child in children:
            wrapper.append(child)
        return True
    except (ET.ParseError, TypeError, ValueError):
        return False


def _add_designed_product_parts(
    group: ET.Element,
    parts: list[dict[str, Any]],
    *,
    width: float,
    height: float,
    primary: str,
    secondary: str,
    stroke: str,
) -> None:
    """Render Gemini's validated normalized icon recipe as safe SVG."""
    x0 = -width / 2
    y0 = -height / 2
    palette = {
        "primary": primary,
        "secondary": secondary,
        "accent": _mix_hex_color(primary, secondary, 0.45),
        "light": _mix_hex_color(secondary, "#FFFFFF", 0.62),
        "dark": _mix_hex_color(primary, "#000000", 0.38),
        "none": "none",
    }
    scale = min(width, height)

    for part in parts:
        primitive = str(part.get("primitive") or "")
        fill = palette.get(
            str(part.get("fill") or "secondary"),
            secondary,
        )
        part_stroke = (
            stroke
            if part.get("stroke", True)
            else "none"
        )
        stroke_width = max(
            0.8,
            float(part.get("stroke_width") or 0.01) * scale,
        )
        x = x0 + float(part.get("x") or 0.0) * width
        y = y0 + float(part.get("y") or 0.0) * height
        part_width = float(part.get("width") or 0.0) * width
        part_height = float(part.get("height") or 0.0) * height
        common = {
            "fill": fill,
            "stroke": part_stroke,
            "stroke-width": f"{stroke_width:.2f}",
            "data-product-part": str(part.get("role") or "detail"),
        }

        if primitive == "rect":
            ET.SubElement(
                group,
                _svg_tag("rect"),
                {
                    "x": f"{x:.2f}",
                    "y": f"{y:.2f}",
                    "width": f"{part_width:.2f}",
                    "height": f"{part_height:.2f}",
                    "rx": (
                        f"{float(part.get('corner_roundness') or 0.0) * min(part_width, part_height) * .5:.2f}"
                    ),
                    **common,
                },
            )
        elif primitive == "ellipse":
            ET.SubElement(
                group,
                _svg_tag("ellipse"),
                {
                    "cx": f"{x + part_width / 2:.2f}",
                    "cy": f"{y + part_height / 2:.2f}",
                    "rx": f"{part_width / 2:.2f}",
                    "ry": f"{part_height / 2:.2f}",
                    **common,
                },
            )
        elif primitive == "line":
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{x:.2f}",
                    "y1": f"{y:.2f}",
                    "x2": f"{x0 + float(part.get('width') or 0.0) * width:.2f}",
                    "y2": f"{y0 + float(part.get('height') or 0.0) * height:.2f}",
                    "fill": "none",
                    "stroke": (
                        part_stroke
                        if part_stroke != "none"
                        else palette["dark"]
                    ),
                    "stroke-width": f"{stroke_width:.2f}",
                    "data-product-part": str(part.get("role") or "detail"),
                },
            )
        elif primitive == "polygon":
            points = " ".join(
                f"{x0 + float(point[0]) * width:.2f},"
                f"{y0 + float(point[1]) * height:.2f}"
                for point in part.get("points", [])
            )
            if points:
                ET.SubElement(
                    group,
                    _svg_tag("polygon"),
                    {
                        "points": points,
                        **common,
                    },
                )


def _add_profiled_product_shape(
    group: ET.Element,
    profile: dict[str, Any],
    *,
    item_type: str,
    width: float,
    height: float,
) -> None:
    default_sizes = {
        "bed": (187.0, 250.0),
        "sofa": (198.0, 117.0),
        "chair": (99.0, 99.0),
        "desk": (182.0, 104.0),
        "table": (182.0, 104.0),
        "bench": (177.0, 78.0),
        "shelf": (143.0, 143.0),
        "cabinet": (143.0, 143.0),
        "dresser": (143.0, 143.0),
        "wardrobe": (143.0, 182.0),
        "lamp": (94.0, 94.0),
        "rug": (187.0, 140.0),
        "plant": (107.0, 125.0),
    }
    default_width, default_height = (
        default_sizes.get(
            item_type,
            (130.0, 104.0),
        )
    )
    width = width or default_width
    height = height or default_height
    x = -width / 2
    y = -height / 2
    primary = profile.get(
        "primary_color",
        "#B78B68",
    )
    secondary = profile.get(
        "secondary_color",
        "#E8DCCB",
    )
    stroke = (
        "#3D2717"
        if profile.get(
            "has_border",
            True,
        )
        else primary
    )
    shape = profile.get(
        "shape",
        "rounded_rectangle",
    )
    roundness = float(
        profile.get(
            "corner_roundness",
            0.15,
        )
    )
    radius = min(
        width,
        height,
    ) * roundness * 0.45
    common = {
        "fill": str(primary),
        "stroke": str(stroke),
        "stroke-width": "2.5",
    }

    if shape in {
        "circle",
        "oval",
    }:
        ET.SubElement(
            group,
            _svg_tag("ellipse"),
            {
                "cx": "0",
                "cy": "0",
                "rx": f"{width / 2:.1f}",
                "ry": (
                    f"{width / 2:.1f}"
                    if shape == "circle"
                    else f"{height / 2:.1f}"
                ),
                **common,
            },
        )
    elif shape == "l_shape":
        cut_x = x + width * 0.58
        cut_y = y + height * 0.52
        points = (
            f"{x:.1f},{y:.1f} "
            f"{x + width:.1f},{y:.1f} "
            f"{x + width:.1f},{cut_y:.1f} "
            f"{cut_x:.1f},{cut_y:.1f} "
            f"{cut_x:.1f},{y + height:.1f} "
            f"{x:.1f},{y + height:.1f}"
        )
        ET.SubElement(
            group,
            _svg_tag("polygon"),
            {
                "points": points,
                **common,
            },
        )
    elif shape == "irregular":
        points = (
            f"{x + width * .08:.1f},{y:.1f} "
            f"{x + width * .92:.1f},{y + height * .04:.1f} "
            f"{x + width:.1f},{y + height * .62:.1f} "
            f"{x + width * .76:.1f},{y + height:.1f} "
            f"{x + width * .12:.1f},{y + height * .92:.1f} "
            f"{x:.1f},{y + height * .28:.1f}"
        )
        ET.SubElement(
            group,
            _svg_tag("polygon"),
            {
                "points": points,
                **common,
            },
        )
    else:
        effective_width = (
            min(width, height)
            if shape == "square"
            else width
        )
        effective_height = (
            effective_width
            if shape == "square"
            else height
        )
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{-effective_width / 2:.1f}",
                "y": f"{-effective_height / 2:.1f}",
                "width": f"{effective_width:.1f}",
                "height": f"{effective_height:.1f}",
                "rx": (
                    "0"
                    if shape == "rectangle"
                    else f"{radius:.1f}"
                ),
                **common,
            },
        )

    designed_parts = profile.get("parts")
    if (
        isinstance(designed_parts, list)
        and len(designed_parts) >= 3
    ):
        _add_designed_product_parts(
            group,
            designed_parts,
            width=width,
            height=height,
            primary=str(primary),
            secondary=str(secondary),
            stroke=str(stroke),
        )
        return

    # Add category-specific top-view construction details without copying the
    # shopping-photo background or perspective.
    if item_type == "bed":
        frame_only = bool(
            profile.get("is_frame_only")
        )
        storage_type = str(
            profile.get("storage_type")
            or "none"
        )
        # Upholstered/frame rim and mattress.
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .055:.1f}",
                "y": f"{y + height * .10:.1f}",
                "width": f"{width * .89:.1f}",
                "height": f"{height * .84:.1f}",
                "rx": f"{max(4.0, radius):.1f}",
                "fill": str(secondary),
                "stroke": str(stroke),
                "stroke-width": "1.5",
            },
        )
        # Headboard and cushions are based on the selected product photo.
        if profile.get("has_headboard"):
            ET.SubElement(
                group,
                _svg_tag("rect"),
                {
                    "x": f"{x + width * .025:.1f}",
                    "y": f"{y + height * .025:.1f}",
                    "width": f"{width * .95:.1f}",
                    "height": f"{height * .14:.1f}",
                    "rx": f"{max(4.0, radius):.1f}",
                    "fill": str(primary),
                    "stroke": str(stroke),
                    "stroke-width": "2",
                },
            )
        # Loose pillows belong to upholstered/complete beds, not every product
        # whose shopping category happens to be a bed frame.
        if profile.get("has_cushions") and not frame_only:
            for pillow_x in (
                x + width * .10,
                x + width * .52,
            ):
                ET.SubElement(
                    group,
                    _svg_tag("rect"),
                    {
                        "x": f"{pillow_x:.1f}",
                        "y": f"{y + height * .16:.1f}",
                        "width": f"{width * .38:.1f}",
                        "height": f"{height * .18:.1f}",
                        "rx": f"{max(5.0, radius):.1f}",
                        "fill": "#F7F4ED",
                        "stroke": str(stroke),
                        "stroke-width": "1.2",
                    },
                )
        # Side rails and footboard make frame-only products read as beds
        # instead of plain rounded rectangles.
        for rail_x in (
            x + width * .035,
            x + width * .925,
        ):
            ET.SubElement(
                group,
                _svg_tag("rect"),
                {
                    "x": f"{rail_x:.1f}",
                    "y": f"{y + height * .13:.1f}",
                    "width": f"{width * .04:.1f}",
                    "height": f"{height * .79:.1f}",
                    "rx": f"{max(2.0, radius * .35):.1f}",
                    "fill": str(primary),
                    "stroke": str(stroke),
                    "stroke-width": "1.1",
                },
            )
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .055:.1f}",
                "y": f"{y + height * .90:.1f}",
                "width": f"{width * .89:.1f}",
                "height": f"{height * .055:.1f}",
                "rx": f"{max(2.0, radius * .35):.1f}",
                "fill": str(primary),
                "stroke": str(stroke),
                "stroke-width": "1.1",
            },
        )
        # Mattress/duvet surface. Frame-only listings stay light so the actual
        # frame color remains dominant instead of becoming a generic duvet.
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .075:.1f}",
                "y": f"{y + height * .38:.1f}",
                "width": f"{width * .85:.1f}",
                "height": f"{height * .53:.1f}",
                "rx": f"{max(4.0, radius * .7):.1f}",
                "fill": (
                    str(secondary)
                    if frame_only
                    else str(primary)
                ),
                "fill-opacity": (
                    "0.62"
                    if frame_only
                    else "0.88"
                ),
                "stroke": str(stroke),
                "stroke-width": "1.5",
            },
        )
        for seam in (
            ()
            if frame_only
            else (
                0.54,
                0.70,
                0.84,
            )
        ):
            ET.SubElement(
                group,
                _svg_tag("path"),
                {
                    "d": (
                        f"M {x + width * .11:.1f} "
                        f"{y + height * seam:.1f} "
                        f"Q 0 {y + height * (seam + .035):.1f} "
                        f"{x + width * .89:.1f} "
                        f"{y + height * seam:.1f}"
                    ),
                    "fill": "none",
                    "stroke": str(secondary),
                    "stroke-width": "1.4",
                    "stroke-opacity": "0.8",
                },
            )

        # Preserve storage construction explicitly stated by the listing.
        if storage_type == "lift_up":
            ET.SubElement(
                group,
                _svg_tag("rect"),
                {
                    "x": f"{x + width * .14:.1f}",
                    "y": f"{y + height * .43:.1f}",
                    "width": f"{width * .72:.1f}",
                    "height": f"{height * .39:.1f}",
                    "rx": f"{max(2.0, radius * .25):.1f}",
                    "fill": "none",
                    "stroke": str(stroke),
                    "stroke-width": "1.3",
                    "stroke-dasharray": "5 3",
                },
            )
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{x + width * .20:.1f}",
                    "y1": f"{y + height * .81:.1f}",
                    "x2": f"{x + width * .80:.1f}",
                    "y2": f"{y + height * .81:.1f}",
                    "stroke": str(stroke),
                    "stroke-width": "2",
                },
            )
        elif storage_type in {"drawers", "open_shelf"}:
            for division in (0.42, 0.62, 0.82):
                ET.SubElement(
                    group,
                    _svg_tag("line"),
                    {
                        "x1": f"{x + width * .08:.1f}",
                        "y1": f"{y + height * division:.1f}",
                        "x2": f"{x + width * .20:.1f}",
                        "y2": f"{y + height * division:.1f}",
                        "stroke": str(stroke),
                        "stroke-width": "1.2",
                    },
                )
    elif item_type == "sofa":
        # Back cushion.
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .06:.1f}",
                "y": f"{y + height * .07:.1f}",
                "width": f"{width * .88:.1f}",
                "height": f"{height * .30:.1f}",
                "rx": f"{max(6.0, radius):.1f}",
                "fill": str(primary),
                "stroke": str(stroke),
                "stroke-width": "1.8",
            },
        )
        # Seat cushion.
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .15:.1f}",
                "y": f"{y + height * .34:.1f}",
                "width": f"{width * .70:.1f}",
                "height": f"{height * .56:.1f}",
                "rx": f"{max(6.0, radius):.1f}",
                "fill": str(secondary),
                "stroke": str(stroke),
                "stroke-width": "1.5",
            },
        )
        # Arm rests and seat divisions are based on Gemini's visual profile.
        if profile.get("has_armrests"):
            for arm_x in (
                x + width * .025,
                x + width * .835,
            ):
                ET.SubElement(
                    group,
                    _svg_tag("rect"),
                    {
                        "x": f"{arm_x:.1f}",
                        "y": f"{y + height * .23:.1f}",
                        "width": f"{width * .14:.1f}",
                        "height": f"{height * .68:.1f}",
                        "rx": f"{max(5.0, radius):.1f}",
                        "fill": str(primary),
                        "stroke": str(stroke),
                        "stroke-width": "1.5",
                    },
                )
        seat_count = max(1, int(profile.get("seat_count") or 1))
        for seat_index in range(1, seat_count):
            seam_x = x + width * (
                .15 + .70 * seat_index / seat_count
            )
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{seam_x:.1f}",
                    "y1": f"{y + height * .39:.1f}",
                    "x2": f"{seam_x:.1f}",
                    "y2": f"{y + height * .84:.1f}",
                    "stroke": str(stroke),
                    "stroke-width": "1.4",
                    "stroke-opacity": "0.65",
                },
            )
        for tuft_x in (-0.22, 0.22):
            ET.SubElement(
                group,
                _svg_tag("circle"),
                {
                    "cx": f"{width * tuft_x:.1f}",
                    "cy": f"{y + height * .61:.1f}",
                    "r": f"{max(2.0, min(width, height) * .025):.1f}",
                    "fill": str(primary),
                    "stroke": str(stroke),
                    "stroke-width": "0.8",
                },
            )
    elif item_type == "chair":
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .12:.1f}",
                "y": f"{y + height * .08:.1f}",
                "width": f"{width * .76:.1f}",
                "height": f"{height * .34:.1f}",
                "rx": f"{max(5.0, radius):.1f}",
                "fill": str(primary),
                "stroke": str(stroke),
                "stroke-width": "1.6",
            },
        )
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .19:.1f}",
                "y": f"{y + height * .39:.1f}",
                "width": f"{width * .62:.1f}",
                "height": f"{height * .48:.1f}",
                "rx": f"{max(5.0, radius):.1f}",
                "fill": str(secondary),
                "stroke": str(stroke),
                "stroke-width": "1.5",
            },
        )
        for arm_x in (
            x + width * .06,
            x + width * .82,
        ):
            ET.SubElement(
                group,
                _svg_tag("rect"),
                {
                    "x": f"{arm_x:.1f}",
                    "y": f"{y + height * .30:.1f}",
                    "width": f"{width * .12:.1f}",
                    "height": f"{height * .58:.1f}",
                    "rx": f"{max(3.0, radius):.1f}",
                    "fill": str(primary),
                    "stroke": str(stroke),
                    "stroke-width": "1.2",
                },
            )
        # Central upholstery seam keeps the seat/back visually distinct.
        ET.SubElement(
            group,
            _svg_tag("path"),
            {
                "d": (
                    f"M {x + width * .25:.1f} {y + height * .62:.1f} "
                    f"Q 0 {y + height * .68:.1f} "
                    f"{x + width * .75:.1f} {y + height * .62:.1f}"
                ),
                "fill": "none",
                "stroke": str(stroke),
                "stroke-width": "1.2",
                "stroke-opacity": "0.65",
            },
        )
    elif item_type == "desk":
        # Work surface, drawer bank, cable grommet and front edge.
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .055:.1f}",
                "y": f"{y + height * .07:.1f}",
                "width": f"{width * .89:.1f}",
                "height": f"{height * .82:.1f}",
                "rx": f"{max(3.0, radius * .55):.1f}",
                "fill": str(secondary),
                "stroke": str(stroke),
                "stroke-width": "1.5",
            },
        )
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .61:.1f}",
                "y": f"{y + height * .13:.1f}",
                "width": f"{width * .27:.1f}",
                "height": f"{height * .66:.1f}",
                "rx": "3",
                "fill": str(primary),
                "stroke": str(stroke),
                "stroke-width": "1.3",
            },
        )
        for drawer_y in (0.34, 0.56):
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{x + width * .63:.1f}",
                    "y1": f"{y + height * drawer_y:.1f}",
                    "x2": f"{x + width * .86:.1f}",
                    "y2": f"{y + height * drawer_y:.1f}",
                    "stroke": str(stroke),
                    "stroke-width": "1",
                },
            )
        ET.SubElement(
            group,
            _svg_tag("circle"),
            {
                "cx": f"{x + width * .16:.1f}",
                "cy": f"{y + height * .22:.1f}",
                "r": f"{max(3.0, min(width, height) * .045):.1f}",
                "fill": str(primary),
                "stroke": str(stroke),
                "stroke-width": "1",
            },
        )
    elif item_type == "table":
        # Inset tabletop and visible top-view leg caps.
        inset_shape = (
            "ellipse"
            if shape in {"circle", "oval"}
            else "rect"
        )
        if inset_shape == "ellipse":
            ET.SubElement(
                group,
                _svg_tag("ellipse"),
                {
                    "cx": "0",
                    "cy": "0",
                    "rx": f"{width * .42:.1f}",
                    "ry": f"{height * .40:.1f}",
                    "fill": str(secondary),
                    "stroke": str(stroke),
                    "stroke-width": "1.5",
                },
            )
        else:
            ET.SubElement(
                group,
                _svg_tag("rect"),
                {
                    "x": f"{x + width * .08:.1f}",
                    "y": f"{y + height * .10:.1f}",
                    "width": f"{width * .84:.1f}",
                    "height": f"{height * .80:.1f}",
                    "rx": f"{max(3.0, radius * .6):.1f}",
                    "fill": str(secondary),
                    "stroke": str(stroke),
                    "stroke-width": "1.5",
                },
            )
        for px, py in ((.13, .16), (.87, .16), (.13, .84), (.87, .84)):
            ET.SubElement(
                group,
                _svg_tag("circle"),
                {
                    "cx": f"{x + width * px:.1f}",
                    "cy": f"{y + height * py:.1f}",
                    "r": f"{max(2.5, min(width, height) * .035):.1f}",
                    "fill": str(primary),
                    "stroke": str(stroke),
                    "stroke-width": "0.9",
                },
            )
    elif item_type == "bench":
        # Seat cushion/slats and four structural supports.
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .06:.1f}",
                "y": f"{y + height * .16:.1f}",
                "width": f"{width * .88:.1f}",
                "height": f"{height * .68:.1f}",
                "rx": f"{max(5.0, radius):.1f}",
                "fill": str(secondary),
                "stroke": str(stroke),
                "stroke-width": "1.5",
            },
        )
        for division in (.34, .66):
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{x + width * division:.1f}",
                    "y1": f"{y + height * .20:.1f}",
                    "x2": f"{x + width * division:.1f}",
                    "y2": f"{y + height * .80:.1f}",
                    "stroke": str(primary),
                    "stroke-width": "1.4",
                },
            )
    elif item_type == "lamp":
        # Shade, bulb, socket and light spokes.
        ET.SubElement(
            group,
            _svg_tag("circle"),
            {
                "cx": "0",
                "cy": "0",
                "r": f"{min(width, height) * .34:.1f}",
                "fill": str(secondary),
                "stroke": str(stroke),
                "stroke-width": "1.7",
            },
        )
        ET.SubElement(
            group,
            _svg_tag("circle"),
            {
                "cx": "0",
                "cy": "0",
                "r": f"{min(width, height) * .13:.1f}",
                "fill": "#FFF3B0",
                "stroke": str(stroke),
                "stroke-width": "1.3",
            },
        )
        for dx, dy in ((0, -.46), (.46, 0), (0, .46), (-.46, 0)):
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{width * dx * .64:.1f}",
                    "y1": f"{height * dy * .64:.1f}",
                    "x2": f"{width * dx:.1f}",
                    "y2": f"{height * dy:.1f}",
                    "stroke": str(primary),
                    "stroke-width": "2",
                    "stroke-linecap": "round",
                },
            )
    elif item_type == "plant":
        # Pot rim, soil and a varied leaf canopy.
        ET.SubElement(
            group,
            _svg_tag("circle"),
            {
                "cx": "0",
                "cy": f"{height * .20:.1f}",
                "r": f"{min(width, height) * .22:.1f}",
                "fill": "#8D5B3B",
                "stroke": str(stroke),
                "stroke-width": "1.5",
            },
        )
        ET.SubElement(
            group,
            _svg_tag("circle"),
            {
                "cx": "0",
                "cy": f"{height * .17:.1f}",
                "r": f"{min(width, height) * .14:.1f}",
                "fill": "#4E3527",
                "stroke": "none",
            },
        )
        for dx, dy, angle in (
            (-.27, -.18, -35),
            (0, -.32, 0),
            (.27, -.18, 35),
            (-.18, .02, -65),
            (.18, .02, 65),
        ):
            ET.SubElement(
                group,
                _svg_tag("ellipse"),
                {
                    "cx": f"{width * dx:.1f}",
                    "cy": f"{height * dy:.1f}",
                    "rx": f"{width * .13:.1f}",
                    "ry": f"{height * .24:.1f}",
                    "fill": str(primary),
                    "stroke": str(stroke),
                    "stroke-width": "1",
                    "transform": (
                        f"rotate({angle} {width * dx:.1f} {height * dy:.1f})"
                    ),
                },
            )
    elif item_type == "rug":
        for inset in (
            0.04,
            0.09,
        ):
            ET.SubElement(
                group,
                _svg_tag("rect"),
                {
                    "x": f"{x + width * inset:.1f}",
                    "y": f"{y + height * inset:.1f}",
                    "width": f"{width * (1 - inset * 2):.1f}",
                    "height": f"{height * (1 - inset * 2):.1f}",
                    "rx": f"{max(2.0, radius):.1f}",
                    "fill": "none",
                    "stroke": str(secondary),
                    "stroke-width": "2",
                    "stroke-opacity": "0.75",
                },
            )
        for stripe in (
            0.30,
            0.50,
            0.70,
        ):
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{x + width * .08:.1f}",
                    "y1": f"{y + height * stripe:.1f}",
                    "x2": f"{x + width * .92:.1f}",
                    "y2": f"{y + height * stripe:.1f}",
                    "stroke": str(secondary),
                    "stroke-width": "1.4",
                    "stroke-opacity": "0.55",
                },
            )
    elif item_type in {
        "cabinet",
        "dresser",
        "wardrobe",
        "shelf",
    }:
        row_count = (
            4
            if item_type
            == "dresser"
            else 3
        )
        if item_type == "wardrobe":
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": "0",
                    "y1": f"{y + height * .06:.1f}",
                    "x2": "0",
                    "y2": f"{y + height * .94:.1f}",
                    "stroke": str(secondary),
                    "stroke-width": "2",
                },
            )
            for handle_x in (-width * .06, width * .06):
                ET.SubElement(
                    group,
                    _svg_tag("rect"),
                    {
                        "x": f"{handle_x - 2:.1f}",
                        "y": f"{-height * .08:.1f}",
                        "width": "4",
                        "height": f"{height * .16:.1f}",
                        "rx": "2",
                        "fill": str(secondary),
                    },
                )
        else:
            for row in range(1, row_count):
                row_y = y + height * row / row_count
                ET.SubElement(
                    group,
                    _svg_tag("line"),
                    {
                        "x1": f"{x + width * .06:.1f}",
                        "y1": f"{row_y:.1f}",
                        "x2": f"{x + width * .94:.1f}",
                        "y2": f"{row_y:.1f}",
                        "stroke": str(secondary),
                        "stroke-width": "2",
                    },
                )
            if item_type == "dresser":
                for row in range(row_count):
                    ET.SubElement(
                        group,
                        _svg_tag("rect"),
                        {
                            "x": f"{-width * .10:.1f}",
                            "y": (
                                f"{y + height * (row + .5) / row_count - 2:.1f}"
                            ),
                            "width": f"{width * .20:.1f}",
                            "height": "4",
                            "rx": "2",
                            "fill": str(secondary),
                        },
                    )
            elif item_type == "cabinet":
                ET.SubElement(
                    group,
                    _svg_tag("line"),
                    {
                        "x1": "0",
                        "y1": f"{y + height * .06:.1f}",
                        "x2": "0",
                        "y2": f"{y + height * .94:.1f}",
                        "stroke": str(secondary),
                        "stroke-width": "1.5",
                    },
                )
                for handle_x in (-width * .06, width * .06):
                    ET.SubElement(
                        group,
                        _svg_tag("circle"),
                        {
                            "cx": f"{handle_x:.1f}",
                            "cy": "0",
                            "r": "2.8",
                            "fill": str(secondary),
                        },
                    )
            elif item_type == "shelf":
                # Small objects/books make an open shelf visually distinct.
                for shelf_y in (.18, .50, .82):
                    for book_x, book_w in ((.13, .11), (.29, .08), (.42, .13)):
                        ET.SubElement(
                            group,
                            _svg_tag("rect"),
                            {
                                "x": f"{x + width * book_x:.1f}",
                                "y": f"{y + height * (shelf_y - .10):.1f}",
                                "width": f"{width * book_w:.1f}",
                                "height": f"{height * .09:.1f}",
                                "fill": str(primary),
                                "stroke": str(stroke),
                                "stroke-width": ".7",
                            },
                        )
    elif profile.get(
        "has_center_division"
    ):
        ET.SubElement(
            group,
            _svg_tag("line"),
            {
                "x1": "0",
                "y1": f"{y + height * .08:.1f}",
                "x2": "0",
                "y2": f"{y + height * .92:.1f}",
                "stroke": str(secondary),
                "stroke-width": "3",
            },
        )

    material = str(
        profile.get("material")
        or "mixed"
    )
    if material == "fabric":
        ET.SubElement(
            group,
            _svg_tag("rect"),
            {
                "x": f"{x + width * .035:.1f}",
                "y": f"{y + height * .035:.1f}",
                "width": f"{width * .93:.1f}",
                "height": f"{height * .93:.1f}",
                "rx": f"{max(4.0, radius):.1f}",
                "fill": "none",
                "stroke": str(secondary),
                "stroke-width": "1.4",
                "stroke-dasharray": "4 3",
                "stroke-opacity": "0.7",
            },
        )
    elif material == "wood":
        for grain in (
            0.28,
            0.50,
            0.72,
        ):
            ET.SubElement(
                group,
                _svg_tag("path"),
                {
                    "d": (
                        f"M {x + width * .08:.1f} "
                        f"{y + height * grain:.1f} "
                        f"Q 0 {y + height * (grain - .04):.1f} "
                        f"{x + width * .92:.1f} "
                        f"{y + height * grain:.1f}"
                    ),
                    "fill": "none",
                    "stroke": str(secondary),
                    "stroke-width": "1.2",
                    "stroke-opacity": "0.6",
                },
            )
    elif material == "metal":
        for bolt_x, bolt_y in (
            (
                x + width * .08,
                y + height * .08,
            ),
            (
                x + width * .92,
                y + height * .08,
            ),
            (
                x + width * .08,
                y + height * .92,
            ),
            (
                x + width * .92,
                y + height * .92,
            ),
        ):
            ET.SubElement(
                group,
                _svg_tag("circle"),
                {
                    "cx": f"{bolt_x:.1f}",
                    "cy": f"{bolt_y:.1f}",
                    "r": "2.2",
                    "fill": str(secondary),
                    "stroke": str(stroke),
                    "stroke-width": "0.7",
                },
            )
    elif material == "glass":
        ET.SubElement(
            group,
            _svg_tag("line"),
            {
                "x1": f"{x + width * .16:.1f}",
                "y1": f"{y + height * .20:.1f}",
                "x2": f"{x + width * .72:.1f}",
                "y2": f"{y + height * .08:.1f}",
                "stroke": "#FFFFFF",
                "stroke-width": "3",
                "stroke-opacity": "0.75",
                "stroke-linecap": "round",
            },
        )

    pattern = str(
        profile.get("pattern")
        or "solid"
    )
    if pattern in {"striped", "checkered", "geometric"}:
        stripe_positions = (0.25, 0.50, 0.75)
        for position in stripe_positions:
            ET.SubElement(
                group,
                _svg_tag("line"),
                {
                    "x1": f"{x + width * .08:.1f}",
                    "y1": f"{y + height * position:.1f}",
                    "x2": f"{x + width * .92:.1f}",
                    "y2": f"{y + height * position:.1f}",
                    "stroke": str(secondary),
                    "stroke-width": "1.6",
                    "stroke-opacity": "0.65",
                },
            )
        if pattern in {"checkered", "geometric"}:
            for position in stripe_positions:
                ET.SubElement(
                    group,
                    _svg_tag("line"),
                    {
                        "x1": f"{x + width * position:.1f}",
                        "y1": f"{y + height * .08:.1f}",
                        "x2": f"{x + width * position:.1f}",
                        "y2": f"{y + height * .92:.1f}",
                        "stroke": str(secondary),
                        "stroke-width": "1.4",
                        "stroke-opacity": "0.55",
                    },
                )
    elif pattern in {"floral", "woven"}:
        for px, py in (
            (0.25, 0.30),
            (0.50, 0.50),
            (0.75, 0.70),
        ):
            ET.SubElement(
                group,
                _svg_tag("circle"),
                {
                    "cx": f"{x + width * px:.1f}",
                    "cy": f"{y + height * py:.1f}",
                    "r": f"{max(2.0, min(width, height) * .025):.1f}",
                    "fill": str(secondary),
                    "fill-opacity": "0.65",
                },
            )

    leg_style = str(
        profile.get("leg_style")
        or "none"
    )
    if leg_style != "none" and item_type not in {"rug", "plant", "lamp"}:
        leg_color = (
            "#76604D"
            if leg_style == "wood"
            else "#58616A"
        )
        for leg_x, leg_y in (
            (x + width * .10, y + height * .10),
            (x + width * .90, y + height * .10),
            (x + width * .10, y + height * .90),
            (x + width * .90, y + height * .90),
        ):
            ET.SubElement(
                group,
                _svg_tag("circle"),
                {
                    "cx": f"{leg_x:.1f}",
                    "cy": f"{leg_y:.1f}",
                    "r": f"{max(2.2, min(width, height) * .035):.1f}",
                    "fill": leg_color,
                    "stroke": str(stroke),
                    "stroke-width": "0.8",
                },
            )


def _add_selected_product(
    root: ET.Element,
    product: dict[str, Any],
    obj: dict[str, Any],
    *,
    index: int,
    floor_box: tuple[float, float, float, float],
) -> dict[str, Any]:
    floor_x, floor_y, floor_width, floor_height = floor_box
    x = floor_x + float(obj.get("x", 0.5)) * floor_width
    y = floor_y + float(obj.get("y", 0.5)) * floor_height
    relative_width = float(obj.get("w") or 0.0)
    relative_height = float(obj.get("h") or 0.0)
    object_width = (
        relative_width * floor_width
        if relative_width > 0
        else 0.0
    )
    object_height = (
        relative_height * floor_height
        if relative_height > 0
        else 0.0
    )
    item_type = str(product.get("type") or obj.get("type") or "unknown")
    marker = int(product.get("marker") or index + 1)
    owner_id = f"selected-product-{marker}"
    group = ET.SubElement(
        root,
        _svg_tag("g"),
        {
            "id": owner_id,
            "transform": f"translate({x:.1f} {y:.1f})",
            "filter": "url(#shadow-med)",
            "data-draggable": "true",
            "data-tx": f"{x:.1f}",
            "data-ty": f"{y:.1f}",
            "data-origin-x": f"{x:.1f}",
            "data-origin-y": f"{y:.1f}",
            "data-label-id": f"label-{owner_id}",
            "data-rotatable": "true",
            "data-angle": "0",
            "data-resizable": "true",
            "data-scale": "1",
        },
    )

    common = {"stroke": "#3d2717", "stroke-width": "2"}
    if item_type == "bed":
        bed_width = object_width or 144.0
        bed_height = object_height or 192.0
        bed_x = -bed_width / 2
        bed_y = -bed_height / 2
        ET.SubElement(group, _svg_tag("rect"), {
            "x": f"{bed_x:.1f}", "y": f"{bed_y:.1f}",
            "width": f"{bed_width:.1f}", "height": f"{bed_height:.1f}", "rx": "12",
            "fill": "#d8c6ab", **common,
        })
        ET.SubElement(group, _svg_tag("rect"), {
            "x": f"{bed_x + 8:.1f}", "y": f"{bed_y + 10:.1f}",
            "width": f"{max(20.0, bed_width - 16):.1f}",
            "height": f"{max(24.0, bed_height * 0.24):.1f}", "rx": "10",
            "fill": "#f3eee5", "stroke": "#8f755d", "stroke-width": "2",
        })
        ET.SubElement(group, _svg_tag("rect"), {
            "x": f"{bed_x + 10:.1f}",
            "y": f"{bed_y + bed_height * 0.29:.1f}",
            "width": f"{max(20.0, bed_width - 20):.1f}",
            "height": f"{max(24.0, bed_height * 0.66):.1f}", "rx": "8",
            "fill": "#c9847c", "stroke": "#8f554f", "stroke-width": "2",
        })
    elif item_type == "sofa":
        ET.SubElement(group, _svg_tag("rect"), {
            "x": "-76", "y": "-45", "width": "152", "height": "90", "rx": "20",
            "fill": "#a87957", **common,
        })
        ET.SubElement(group, _svg_tag("line"), {
            "x1": "0", "y1": "-38", "x2": "0", "y2": "38",
            "stroke": "#6e482f", "stroke-width": "3",
        })
    elif item_type == "chair":
        ET.SubElement(group, _svg_tag("rect"), {
            "x": "-34", "y": "-32", "width": "68", "height": "64", "rx": "16",
            "fill": "#9fb6c4", **common,
        })
        ET.SubElement(group, _svg_tag("path"), {
            "d": "M-38 -20 Q0 -52 38 -20", "fill": "none",
            "stroke": "#9a693e", "stroke-width": "9", "stroke-linecap": "round",
        })
    elif item_type in {"desk", "table"}:
        ET.SubElement(group, _svg_tag("rect"), {
            "x": "-70", "y": "-40", "width": "140", "height": "80", "rx": "8",
            "fill": "#efe9dc", **common,
        })
        ET.SubElement(group, _svg_tag("line"), {
            "x1": "38", "y1": "-38", "x2": "38", "y2": "38",
            "stroke": "#b4a68f", "stroke-width": "3",
        })
    elif item_type in {
        "shelf",
        "cabinet",
        "dresser",
        "wardrobe",
    }:
        ET.SubElement(group, _svg_tag("rect"), {
            "x": "-55", "y": "-55", "width": "110", "height": "110", "rx": "7",
            "fill": "#a97950", **common,
        })
        for row in (-18, 18):
            ET.SubElement(group, _svg_tag("line"), {
                "x1": "-50", "y1": str(row), "x2": "50", "y2": str(row),
                "stroke": "#5d381e", "stroke-width": "2",
            })
    elif item_type == "bench":
        ET.SubElement(group, _svg_tag("rect"), {
            "x": "-68", "y": "-30", "width": "136", "height": "60", "rx": "16",
            "fill": "#ad805c", **common,
        })
        for leg_x in (-48, 48):
            ET.SubElement(group, _svg_tag("circle"), {
                "cx": str(leg_x), "cy": "23", "r": "6", "fill": "#553621",
            })
    elif item_type == "lamp":
        ET.SubElement(group, _svg_tag("circle"), {
            "cx": "0", "cy": "0", "r": "34", "fill": "#f3df9b", **common,
        })
        for angle in range(0, 360, 45):
            ET.SubElement(group, _svg_tag("line"), {
                "x1": "0", "y1": "0",
                "x2": str(30 * __import__("math").cos(__import__("math").radians(angle))),
                "y2": str(30 * __import__("math").sin(__import__("math").radians(angle))),
                "stroke": "#9d7837", "stroke-width": "1.5",
            })
    elif item_type == "rug":
        rug_width = object_width or 144.0
        rug_height = object_height or 108.0
        ET.SubElement(group, _svg_tag("rect"), {
            "x": f"{-rug_width / 2:.1f}",
            "y": f"{-rug_height / 2:.1f}",
            "width": f"{rug_width:.1f}",
            "height": f"{rug_height:.1f}", "rx": "5",
            # Do not depend on a pattern ID from the original Gemini SVG.
            # Older/cached SVGs may not define jute-texture, which makes the
            # replacement rug render transparent.
            "fill": "#d8c3a0", **common,
        })
        for offset in (-0.30, -0.10, 0.10, 0.30):
            line_y = offset * rug_height
            ET.SubElement(group, _svg_tag("line"), {
                "x1": f"{-rug_width / 2 + 8:.1f}",
                "y1": f"{line_y:.1f}",
                "x2": f"{rug_width / 2 - 8:.1f}",
                "y2": f"{line_y:.1f}",
                "stroke": "#b59b76",
                "stroke-width": "1.5",
                "stroke-opacity": "0.55",
            })
    elif item_type == "plant":
        ET.SubElement(group, _svg_tag("circle"), {
            "cx": "0", "cy": "8", "r": "20", "fill": "#aa7047", **common,
        })
        for dx, dy in ((-28, -24), (0, -36), (28, -22), (-18, -4), (20, 0)):
            ET.SubElement(group, _svg_tag("ellipse"), {
                "cx": str(dx), "cy": str(dy), "rx": "12", "ry": "24",
                "fill": "#668b5f", "stroke": "#385738", "stroke-width": "1.5",
                "transform": f"rotate({dx} {dx} {dy})",
            })
    else:
        ET.SubElement(group, _svg_tag("rect"), {
            "x": "-45", "y": "-35", "width": "90", "height": "70", "rx": "8",
            "fill": "#bc916c", **common,
        })

    direct_icon_added = False
    icon_svg = product.get("icon_svg")
    if isinstance(icon_svg, str) and icon_svg.strip():
        direct_width = object_width or {
            "bed": 187.0,
            "sofa": 198.0,
            "chair": 99.0,
            "rug": 187.0,
        }.get(item_type, 143.0)
        direct_height = object_height or {
            "bed": 250.0,
            "sofa": 117.0,
            "chair": 99.0,
            "rug": 140.0,
        }.get(item_type, 117.0)
        fallback_elements = list(group)
        direct_icon_added = _add_direct_product_icon(
            group,
            icon_svg,
            width=direct_width,
            height=direct_height,
        )
        if direct_icon_added:
            for fallback_element in fallback_elements:
                group.remove(fallback_element)

    visual_profile = product.get("visual_profile")
    if (
        not direct_icon_added
        and isinstance(visual_profile, dict)
    ):
        # Remove the category fallback drawing so non-rectangular analyzed
        # silhouettes (round, oval, L-shaped, etc.) do not reveal it below.
        for fallback_element in list(
            group
        ):
            group.remove(
                fallback_element
            )
        _add_profiled_product_shape(
            group,
            visual_profile,
            item_type=item_type,
            width=object_width,
            height=object_height,
        )

    control_width = object_width or {
        "bed": 187.0,
        "sofa": 198.0,
        "chair": 99.0,
        "rug": 187.0,
    }.get(item_type, 143.0)
    control_height = object_height or {
        "bed": 250.0,
        "sofa": 117.0,
        "chair": 99.0,
        "rug": 140.0,
    }.get(item_type, 117.0)
    control_y = -control_height / 2 - 15
    controls = (
        (
            control_width / 2 - 43,
            "data-scale-handle",
            "shrink",
            "−",
            "가구 아이콘 축소",
        ),
        (
            control_width / 2 - 13,
            "data-scale-handle",
            "grow",
            "+",
            "가구 아이콘 확대",
        ),
        (
            control_width / 2 + 17,
            "data-rotate-handle",
            "true",
            "↻",
            "가구를 시계 방향으로 90도 회전",
        ),
    )
    for control_x, attribute, value, symbol, aria_label in controls:
        handle = ET.SubElement(
            group,
            _svg_tag("g"),
            {
                attribute: value,
                "transform": (
                    f"translate({control_x:.1f} {control_y:.1f})"
                ),
                "style": "cursor: pointer;",
                "aria-label": aria_label,
            },
        )
        ET.SubElement(
            handle,
            _svg_tag("circle"),
            {
                "cx": "0",
                "cy": "0",
                "r": "13",
                "fill": "#FFFFFF",
                "stroke": "#7A4A2D",
                "stroke-width": "2",
            },
        )
        handle_text = ET.SubElement(
            handle,
            _svg_tag("text"),
            {
                "x": "0",
                "y": "5",
                "text-anchor": "middle",
                "font-family": "Arial, sans-serif",
                "font-size": "16",
                "font-weight": "700",
                "fill": "#7A4A2D",
                "pointer-events": "none",
            },
        )
        handle_text.text = symbol

    badge = ET.SubElement(group, _svg_tag("circle"), {
        "cx": "0", "cy": "0", "r": "13", "fill": "#1f1a17",
    })
    badge.set("aria-label", f"추천 가구 {marker}")
    marker_text = ET.SubElement(group, _svg_tag("text"), {
        "x": "0", "y": "5", "text-anchor": "middle",
        "font-family": "Arial, sans-serif", "font-size": "14",
        "font-weight": "bold", "fill": "#ffffff",
    })
    marker_text.text = str(marker)
    return {
        "owner_id": owner_id,
        "x": x,
        "y": (
            y
            + control_height / 2
            + 24
        ),
        "text": f"{marker}. 새 {obj.get('label') or item_type}",
    }


def create_modified_svg(
    original_svg_path: str | Path,
    original_layout: dict[str, Any],
    modified_layout: dict[str, Any],
    *,
    remove_indices: set[int],
    selected_products: list[dict[str, Any]],
    output_path: str | Path,
) -> str:
    """기존 상세 SVG를 유지하면서 선택된 가구만 제거·추가한다."""
    original_svg_path = Path(original_svg_path)
    output_path = Path(output_path)
    tree = ET.parse(original_svg_path)
    root = tree.getroot()
    # Browser-only edit state may exist in a user-saved original SVG. Event
    # listeners are never part of SVG markup, so these markers must not be
    # copied into a newly generated result plan.
    root.attrib.pop("data-drag-bound", None)
    root.attrib.pop("data-edit-enabled", None)
    runtime_classes = {
        "floorplan-dragging",
        "floorplan-edit-mode",
    }
    remaining_classes = [
        class_name
        for class_name in str(root.get("class") or "").split()
        if class_name not in runtime_classes
    ]
    if remaining_classes:
        root.set("class", " ".join(remaining_classes))
    else:
        root.attrib.pop("class", None)
    original_objects = list(original_layout.get("objects") or [])
    for index in remove_indices:
        if not 0 <= index < len(original_objects):
            continue
        obj = original_objects[index]
        _remove_object_and_label(
            root,
            scene_id=str(obj.get("scene_id") or f"object_{index}"),
            label=str(obj.get("label") or ""),
        )

    selected_objects = [
        obj
        for obj in modified_layout.get("objects") or []
        if obj.get("source") == "selected_product"
    ]
    floor_box = _floor_box(root)
    drawing_parent = next(
        (
            element
            for element
            in root.iter(
                _svg_tag("g")
            )
            if element.get("id")
            == "room-dimension-scale"
        ),
        root,
    )
    labels = []
    for index, (product, obj) in enumerate(zip(selected_products, selected_objects)):
        labels.append(
            _add_selected_product(
                drawing_parent,
                product,
                obj,
                index=index,
                floor_box=floor_box,
            )
        )

    # Labels are appended after all furniture groups so they always remain
    # at the top of the SVG paint order.
    label_layer = ET.SubElement(
        drawing_parent,
        _svg_tag("g"),
        {
            "id": "selected-product-label-layer",
        },
    )
    for label in labels:
        _add_text(
            label_layer,
            x=label["x"],
            y=label["y"],
            text=label["text"],
            owner_id=label["owner_id"],
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    tree.write(output_path, encoding="utf-8", xml_declaration=True)
    return str(output_path)


def prepare_floorplan_edit_markup(
    svg_markup: str,
    layout: dict[str, Any],
) -> str:
    """Wrap original scene objects with the same transform controls as additions."""
    root = ET.fromstring(svg_markup)
    floor_x, floor_y, floor_width, floor_height = _floor_box(root)

    def parent_of(target: ET.Element) -> ET.Element | None:
        return next(
            (
                parent
                for parent in root.iter()
                if target in list(parent)
            ),
            None,
        )

    for index, obj in enumerate(layout.get("objects") or []):
        item_type = str(obj.get("type") or "").lower()
        if item_type in {"door", "window"}:
            continue
        scene_id = str(obj.get("scene_id") or f"object_{index}")
        wrapper_id = f"editable-{scene_id}"
        if any(
            element.get("id") == wrapper_id
            for element in root.iter()
        ):
            continue
        source_group = next(
            (
                element
                for element in root.iter()
                if element.get("id") == scene_id
            ),
            None,
        )
        if source_group is None:
            continue
        parent = parent_of(source_group)
        if parent is None:
            continue

        center_x = floor_x + float(obj.get("x", 0.5)) * floor_width
        center_y = floor_y + float(obj.get("y", 0.5)) * floor_height
        object_width = max(
            52.0,
            float(obj.get("w") or 0.12) * floor_width,
        )
        object_height = max(
            52.0,
            float(obj.get("h") or 0.12) * floor_height,
        )
        position = list(parent).index(source_group)
        parent.remove(source_group)
        wrapper = ET.Element(
            _svg_tag("g"),
            {
                "id": wrapper_id,
                "transform": f"translate({center_x:.3f} {center_y:.3f})",
                "data-draggable": "true",
                "data-rotatable": "true",
                "data-resizable": "true",
                "data-original-furniture": "true",
                "data-scene-id": scene_id,
                "data-tx": f"{center_x:.3f}",
                "data-ty": f"{center_y:.3f}",
                "data-origin-x": f"{center_x:.3f}",
                "data-origin-y": f"{center_y:.3f}",
                "data-angle": "0",
                "data-scale": "1",
            },
        )
        original_transform = str(source_group.get("transform") or "").strip()
        source_group.set(
            "transform",
            (
                f"translate({-center_x:.3f} {-center_y:.3f}) "
                f"{original_transform}"
            ).strip(),
        )
        wrapper.append(source_group)

        controls = ET.SubElement(
            wrapper,
            _svg_tag("g"),
            {
                "class": "floorplan-edit-control",
                "style": "display:none",
                "data-edit-control": "true",
            },
        )
        control_y = -object_height / 2 - 15
        for control_x, attribute, value, text, title in (
            (
                object_width / 2 - 43,
                "data-scale-handle",
                "shrink",
                "−",
                "가구 축소",
            ),
            (
                object_width / 2 - 13,
                "data-scale-handle",
                "grow",
                "+",
                "가구 확대",
            ),
            (
                object_width / 2 + 17,
                "data-rotate-handle",
                "clockwise",
                "↻",
                "가구 회전",
            ),
        ):
            handle = ET.SubElement(
                controls,
                _svg_tag("g"),
                {
                    attribute: value,
                    "transform": f"translate({control_x:.1f} {control_y:.1f})",
                },
            )
            ET.SubElement(
                handle,
                _svg_tag("circle"),
                {
                    "r": "11",
                    "fill": "#fffdf9",
                    "stroke": "#996039",
                    "stroke-width": "1.5",
                },
            )
            label = ET.SubElement(
                handle,
                _svg_tag("text"),
                {
                    "x": "0",
                    "y": "4",
                    "text-anchor": "middle",
                    "font-family": "Arial, sans-serif",
                    "font-size": "15",
                    "font-weight": "bold",
                    "fill": "#75411f",
                    "aria-label": title,
                },
            )
            label.text = text
        parent.insert(position, wrapper)

    return ET.tostring(root, encoding="unicode")


def sanitize_floorplan_edit_svg(svg_markup: str) -> str:
    """Validate an edited in-browser floorplan before persisting it."""
    svg_text = _extract_svg(svg_markup)
    root = ET.fromstring(svg_text)
    if len(list(root.iter())) > 6000:
        raise ValueError("저장할 평면도 SVG가 너무 복잡합니다.")
    for element in root.iter():
        for name, value in element.attrib.items():
            local_name = name.rsplit("}", 1)[-1].lower()
            lowered = str(value).lower()
            if (
                local_name.startswith("on")
                or "javascript:" in lowered
                or "data:text/html" in lowered
            ):
                raise ValueError("평면도 SVG에 실행 가능한 속성이 포함됐습니다.")
    return ET.tostring(root, encoding="unicode")


def apply_floorplan_edits_to_layout(
    svg_markup: str,
    layout: dict[str, Any],
) -> dict[str, Any]:
    """Persist edited centers and footprints for later product placement."""
    root = ET.fromstring(svg_markup)
    floor_x, floor_y, floor_width, floor_height = _floor_box(root)
    edited = json.loads(json.dumps(layout))
    by_scene_id = {
        str(obj.get("scene_id") or ""): obj
        for obj in edited.get("objects") or []
    }
    for element in root.iter():
        if element.get("data-original-furniture") != "true":
            continue
        obj = by_scene_id.get(
            str(element.get("data-scene-id") or "")
        )
        if obj is None:
            continue
        try:
            center_x = float(element.get("data-tx") or 0)
            center_y = float(element.get("data-ty") or 0)
            scale = min(
                1.8,
                max(0.5, float(element.get("data-scale") or 1)),
            )
            angle = float(element.get("data-angle") or 0) % 360
        except (TypeError, ValueError):
            continue
        obj["x"] = min(
            1.0,
            max(0.0, (center_x - floor_x) / floor_width),
        )
        obj["y"] = min(
            1.0,
            max(0.0, (center_y - floor_y) / floor_height),
        )
        width = float(obj.get("w") or 0.12) * scale
        height = float(obj.get("h") or 0.12) * scale
        if 45 <= angle % 180 <= 135:
            width, height = height, width
        obj["w"] = min(1.0, max(0.02, width))
        obj["h"] = min(1.0, max(0.02, height))
        obj["user_rotation"] = angle
        obj["user_scale"] = scale
    return edited
