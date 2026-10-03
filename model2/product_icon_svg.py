"""Generate a safe, self-contained product icon SVG from one shopping photo."""
from __future__ import annotations

import hashlib
import os
import re
import xml.etree.ElementTree as ET
from typing import Any

from google.genai import types

from .gemini_floorplan_artwork import SOLID_HINTS
from .gemini_svg_experiment import _extract_svg


DEFAULT_ICON_SIZE = 200
DEFAULT_ICON_MODEL = "gemini-3.5-flash-lite"
HQ_ICON_MODEL = "gemini-3.6-flash"

# Large, visually prominent furniture benefits most from the more capable
# model. Include the category aliases used by the shopping/floorplan flow.
HQ_MODEL_CATEGORIES = {
    "bed",
    "sofa",
    "table",
    "desk",
    "coffee_table",
    "low_table",
}
TOPDOWN_ONLY_CATEGORIES = {"bed"}

TOPDOWN_VIEWPOINT_INSTRUCTION = """
The supplied photo is usually an angled product photograph. Mentally rotate
the camera to look straight down at the object from directly above.
- Use a true bird's-eye orthographic composition.
- Do not reuse the photograph's oblique or isometric camera angle.
""".strip()

FLEXIBLE_VIEWPOINT_INSTRUCTION = """
Use a directly overhead composition or a very gentle top-down isometric cue
when side construction is essential for recognizing this furniture. It must
still read as an object placed on a floorplan, not as a product photograph.
""".strip()

ICON_PROMPT_TEMPLATE = """
You are an expert interior illustrator and SVG artist.
Look at the supplied shopping photo and redraw the pictured furniture as a
polished small icon for a warm hand-drawn architectural floorplan. The icon
must look like this specific listing photo, not a generic category symbol.

Visual requirements:
{viewpoint_instruction}
- Treat the visible product pixels as authoritative. Preserve the pictured
  silhouette, proportions, dominant and secondary colors, material, section
  count, cushions, arms, frame, storage, legs and distinctive pattern.
- A shopping title can contain several option colors. Never average or mix
  those words. Use the color visibly shown in the supplied photograph.
- First identify the main product region and ignore colors from the wall,
  floor, empty background, text panel and surrounding furniture.
- Convert the photographic perspective into a directly overhead orthographic
  view. A very gentle isometric cue is allowed only inside the object where it
  explains its construction.
- Make the product immediately recognizable. Use curved paths, restrained
  local highlights and small gradients to describe actual volume. Do not fake
  depth with a large offset shadow or a thick duplicate silhouette.
- Exclude the room, floor, wall, people, dimensions, labels and unrelated
  props, but retain presentation components that define the photographed
  furniture's visible appearance. For example, a photographed bed may retain
  its visible mattress, duvet and pillows; a sofa retains its pictured seat
  and back cushions. Do not invent components that are absent from the photo.
- Category-specific fidelity:
  * bed: show the actual headboard/frame profile plus visible mattress,
    pillows and duvet colors or pattern; keep exposed legs/storage visible.
  * sofa/chair: preserve seat count, arm shape, back cushions, piping and legs.
  * rug: preserve outline, border, pile/pattern and main color distribution.
  * table/desk: preserve top shape, thickness, leg/base style and material.
  * cabinet/shelf/dresser/nightstand: preserve doors, drawers, shelves,
    handles, open spaces and top material.
  * lamp/plant/mirror: preserve the distinctive shade/foliage/frame outline.
- Occupy roughly 86% to 94% of the canvas while keeping a small transparent
  margin. Center the object and avoid large unused whitespace.
- Transparent background. Keep every visible shape inside the canvas.

Technical requirements:
- Return ONLY one complete <svg>...</svg>, without Markdown or explanation.
- Use viewBox="0 0 {size} {size}", width="{size}", height="{size}".
- Put the complete icon inside one top-level <g id="icon">.
- Self-contained vector SVG only. No image, external URL, font, script,
  foreignObject, animation, event handler or embedded raster data.
- Use only svg, g, defs, path, rect, circle, ellipse, line, polyline, polygon,
  linearGradient, radialGradient, stop, pattern and clipPath elements.
- Use SVG presentation attributes directly on elements. Do not use a style
  element or style attributes.
- Use at most 120 SVG elements. Keep paths compact.

Orientation: the BACK of the furniture (headboard, sofa back, the side that
goes against a wall) is at the TOP edge of the canvas (y=0); the front, where
a person approaches it, is at the bottom.

{solid_hints}
""".strip()


ALLOWED_TAGS = {
    "svg", "g", "defs", "path", "rect", "circle", "ellipse", "line",
    "polyline", "polygon", "linearGradient", "radialGradient", "stop",
    "pattern", "clipPath",
}


def generate_product_icon_svg(
    client: Any,
    image_bytes: bytes,
    mime_type: str,
    *,
    title: str,
    category: str,
    model: str | None = None,
    size: int = DEFAULT_ICON_SIZE,
) -> str:
    """Ask Gemini for a direct SVG icon and return a sanitized SVG document."""
    normalized_category = (category or "").strip().lower()
    resolved_model = resolve_icon_model(normalized_category, model)
    # 아이콘을 3D로 밀어 올려 세우므로 비스듬한 그림이면 안 된다. 모든 종류를 위에서 본 그림으로
    viewpoint_instruction = TOPDOWN_VIEWPOINT_INSTRUCTION
    prompt = ICON_PROMPT_TEMPLATE.format(
        size=size,
        viewpoint_instruction=viewpoint_instruction,
        solid_hints=SOLID_HINTS,
    )
    prompt += (
        "\n\nPRODUCT INFO:"
        f"\n- category: {category}"
        f"\n- title: {title}"
        "\n- rendering mode: photographed appearance"
    )
    candidate_models = [resolved_model]
    # Free-tier quotas are tracked per model. If the high-quality model has
    # exhausted its daily allowance, retry once with Lite instead of silently
    # returning the generic local furniture template.
    if (
        model is None
        and resolved_model != DEFAULT_ICON_MODEL
    ):
        fallback_model = os.getenv(
            "GEMINI_ICON_MODEL",
            DEFAULT_ICON_MODEL,
        ).strip()
        if fallback_model and fallback_model not in candidate_models:
            candidate_models.append(fallback_model)
    # .env에 적은 모델이 없어졌거나(404) 막혀도 바로 로컬 아이콘으로 떨어지지 않게,
    # 종류별 기본 모델로 한 번 더 시도한다
    default_model = resolve_icon_model(normalized_category, None)
    if default_model and default_model not in candidate_models:
        candidate_models.append(default_model)

    failures: list[str] = []
    for candidate_model in candidate_models:
        try:
            response = client.models.generate_content(
                model=candidate_model,
                contents=[
                    prompt,
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type=mime_type,
                    ),
                ],
                config=types.GenerateContentConfig(
                    response_mime_type="text/plain",
                    temperature=0.2,
                    max_output_tokens=12000,
                ),
            )
            if not response.text:
                raise RuntimeError(
                    "Gemini가 상품 아이콘 SVG를 반환하지 않았습니다."
                )
            return sanitize_product_icon_svg(
                _extract_svg(str(response.text)),
                size=size,
            )
        except Exception as exc:
            failures.append(
                f"{candidate_model}: {type(exc).__name__}: {exc}"
            )

    raise RuntimeError(
        "상품 아이콘 Gemini 모델을 모두 사용할 수 없습니다. "
        + " | ".join(failures)
    )


def resolve_icon_model(
    category: str | None,
    override: str | None = None,
) -> str:
    """Choose the attached prototype's category-aware Gemini model."""
    if override and override.strip():
        return override.strip()
    normalized = (category or "").strip().lower()
    if normalized in HQ_MODEL_CATEGORIES:
        return os.getenv(
            "GEMINI_ICON_MODEL_HQ",
            HQ_ICON_MODEL,
        ).strip()
    return os.getenv(
        "GEMINI_ICON_MODEL",
        DEFAULT_ICON_MODEL,
    ).strip()


def sanitize_product_icon_svg(svg_text: str, *, size: int = DEFAULT_ICON_SIZE) -> str:
    """Validate direct SVG output and namespace local definition IDs."""
    root = ET.fromstring(svg_text)
    elements = list(root.iter())
    if len(elements) > 140:
        raise ValueError("상품 아이콘 SVG 요소가 너무 많습니다.")

    prefix = "pi-" + hashlib.sha256(svg_text.encode("utf-8")).hexdigest()[:10]
    id_map: dict[str, str] = {}
    for element in elements:
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in ALLOWED_TAGS:
            raise ValueError(f"상품 아이콘에 허용되지 않는 SVG 요소: {tag}")
        element_id = str(element.attrib.get("id") or "")
        if element_id:
            id_map[element_id] = f"{prefix}-{element_id}"

        for name, value in list(element.attrib.items()):
            local_name = name.rsplit("}", 1)[-1].lower()
            lowered = str(value).lower()
            if (
                local_name.startswith("on")
                or local_name in {"href", "style"}
                or "javascript:" in lowered
                or "data:" in lowered
                or "url(http" in lowered
            ):
                raise ValueError("상품 아이콘 SVG에 외부 또는 실행 속성이 포함됐습니다.")
            if local_name == "d" and len(str(value)) > 6000:
                raise ValueError("상품 아이콘 SVG path가 너무 복잡합니다.")

    for element in elements:
        element_id = str(element.attrib.get("id") or "")
        if element_id in id_map:
            element.set("id", id_map[element_id])
        for name, value in list(element.attrib.items()):
            rewritten = str(value)
            for old_id, new_id in id_map.items():
                rewritten = rewritten.replace(f"url(#{old_id})", f"url(#{new_id})")
                rewritten = rewritten.replace(f"#{old_id}", f"#{new_id}")
            element.set(name, rewritten)

    root.set("viewBox", root.attrib.get("viewBox") or f"0 0 {size} {size}")
    root.set("width", str(size))
    root.set("height", str(size))
    return ET.tostring(root, encoding="unicode")
