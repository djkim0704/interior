"""실내 사진 → 배치 JSON → 탑뷰 가이드 → 탑뷰 일러스트 실험.

기존 웹/model1과 연결하지 않는 독립 실행 파일이다.

사용 예:
    python model2/topdown_experiment/run.py room.jpg
    python model2/topdown_experiment/run.py room.jpg --analysis-only
"""
from __future__ import annotations

import argparse
import io
import json
import math
import mimetypes
import os
import re
import sys
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image, ImageDraw, ImageFont

try:
    from .illustrator import render_illustration
    from .layout_solver import ALLOWED_RELATIONS, solve_layout
    from .renderer_3d import render_room_3d
except ImportError:
    # 파일을 직접 실행할 때도 동작하도록 하는 호환 경로
    from illustrator import render_illustration
    from layout_solver import ALLOWED_RELATIONS, solve_layout
    from renderer_3d import render_room_3d


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "model2" / "output" / "topdown_experiment"
DEFAULT_ANALYSIS_MODEL = "gemini-3.6-flash"
DEFAULT_IMAGE_MODEL = "gemini-2.5-flash-image"

ANALYSIS_PROMPT = """
You are reconstructing an approximate 2D top-down room layout from one interior
photo. Infer only what is visually supported. Return JSON only, without markdown.

Coordinate system:
- x and y are object center positions normalized to 0..1 inside the room.
- x=0 is the left wall, x=1 the right wall, y=0 the far wall, y=1 the near wall.
- width and depth are normalized room fractions.
- rotation_deg is clockwise in a top-down view.

Schema:
{
  "room": {
    "shape": "rectangle",
    "aspect_ratio_width_to_depth": 0.5,
    "floor_color": "#6b4935",
    "wall_color": "#f4efe4",
    "summary_ko": "짧은 설명"
  },
  "objects": [
    {
      "id": "bed_1",
      "category": "bed",
      "label_ko": "침대",
      "x": 0.5,
      "y": 0.4,
      "width": 0.45,
      "depth": 0.55,
      "rotation_deg": 0,
      "wall_anchors": ["top", "left"],
      "relations": [
        {"type": "left_of", "target": "cabinet_1", "gap": 0.02},
        {"type": "above", "target": "desk_1", "gap": 0.02}
      ],
      "color": "#c89a83",
      "material": "fabric",
      "pattern": "red beige check",
      "confidence": 0.9
    }
  ],
  "uncertainties": ["보이지 않아 추정한 부분"]
}

Include major furniture, rugs, plants, doors, windows, lamps and distinctive
decorations. Preserve relative placement, colors and patterns. Do not invent
additional large furniture. For every object touching or closely aligned to a
wall, include wall_anchors using any of "top", "bottom", "left", "right".
Wall relationships are more important than approximate center coordinates.
Describe reliable object relationships in relations. Allowed relation types are:
left_of, right_of, above, below, near, aligned_x, aligned_y. A relation target
must be another object id from this same JSON. Do not add uncertain relations.
Keep every numeric value within its stated range.
""".strip()

IMAGE_PROMPT = """
Create a clean 2D orthographic top-down interior illustration using the two
references:
1) the original room photograph for furniture identity, colors, materials and
   distinctive patterns;
2) the schematic layout guide for object placement and scale.

Requirements:
- Directly overhead, perfectly orthographic view. No perspective and no visible
  vertical walls.
- Preserve the guide's relative positions and sizes.
- Preserve every major visible item from the original photo.
- Preserve distinctive fabric patterns, wood colors, rug colors and decor.
- Draw a polished, warm editorial floor-plan illustration with subtle outlines.
- The full rectangular room must fit inside the image with a small margin.
- Do not add any labels, words, measurements, people or extra furniture.
- Do not copy the camera perspective of the original photo.
- If an area was not visible, keep it simple instead of inventing furniture.
""".strip()

CATEGORY_COLORS = {
    "bed": "#d8a6a0",
    "desk": "#eee7d5",
    "chair": "#9a7655",
    "dresser": "#9b704b",
    "cabinet": "#9b704b",
    "nightstand": "#b4865c",
    "rug": "#d8c49e",
    "sofa": "#b8785b",
    "plant": "#78966b",
    "door": "#dfc39d",
    "window": "#b9d8e6",
    "lamp": "#f0d57b",
}


def _safe_stem(path: Path) -> str:
    stem = re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", path.stem).strip("_")
    return stem or "room"


def _mime_type(path: Path) -> str:
    guessed, _ = mimetypes.guess_type(path.name)
    return guessed or "image/jpeg"


def _image_part(path: Path, max_side: int = 1600) -> types.Part:
    with Image.open(path) as source:
        image = source.convert("RGB")
        scale = min(1.0, max_side / max(image.size))
        if scale < 1.0:
            image = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.Resampling.LANCZOS,
            )
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=92)
    return types.Part.from_bytes(data=buffer.getvalue(), mime_type="image/jpeg")


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


def _extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.I)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("Gemini 응답에서 JSON 객체를 찾지 못했습니다.")
    value = json.loads(cleaned[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("배치 분석 결과가 JSON 객체가 아닙니다.")
    return value


def _number(value: Any, default: float, low: float, high: float) -> float:
    try:
        return max(low, min(high, float(value)))
    except (TypeError, ValueError):
        return default


def normalize_layout(layout: dict[str, Any]) -> dict[str, Any]:
    room = layout.get("room")
    if not isinstance(room, dict):
        room = {}
    room["aspect_ratio_width_to_depth"] = _number(
        room.get("aspect_ratio_width_to_depth"), 0.75, 0.35, 2.5
    )
    room.setdefault("shape", "rectangle")
    room.setdefault("floor_color", "#6b4935")
    room.setdefault("wall_color", "#f4efe4")
    layout["room"] = room

    normalized_objects: list[dict[str, Any]] = []
    for index, raw in enumerate(layout.get("objects") or []):
        if not isinstance(raw, dict):
            continue
        category = str(raw.get("category") or "object").strip().lower()
        raw["id"] = str(raw.get("id") or f"{category}_{index + 1}")
        raw["category"] = category
        raw["label_ko"] = str(raw.get("label_ko") or category)
        raw["x"] = _number(raw.get("x"), 0.5, 0.0, 1.0)
        raw["y"] = _number(raw.get("y"), 0.5, 0.0, 1.0)
        raw["width"] = _number(raw.get("width"), 0.15, 0.025, 1.0)
        raw["depth"] = _number(raw.get("depth"), 0.15, 0.025, 1.0)
        raw["rotation_deg"] = _number(raw.get("rotation_deg"), 0.0, -360.0, 360.0)
        raw["confidence"] = _number(raw.get("confidence"), 0.5, 0.0, 1.0)
        anchors = raw.get("wall_anchors") or []
        if isinstance(anchors, str):
            anchors = [anchors]
        anchors = [
            str(anchor).lower()
            for anchor in anchors
            if str(anchor).lower() in {"top", "bottom", "left", "right"}
        ]
        raw["wall_anchors"] = anchors
        relations = []
        for relation in raw.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            relation_type = str(relation.get("type") or "").lower()
            target = str(relation.get("target") or "")
            if relation_type in ALLOWED_RELATIONS and target:
                relations.append(
                    {
                        "type": relation_type,
                        "target": target,
                        "gap": _number(relation.get("gap"), 0.018, 0.008, 0.08),
                    }
                )
        raw["relations"] = relations

        normalized_objects.append(raw)
    layout["objects"] = normalized_objects
    layout.setdefault("uncertainties", [])
    return solve_layout(layout)


def analyze_room(
    client: genai.Client,
    input_path: Path,
    model: str,
) -> dict[str, Any]:
    response = client.models.generate_content(
        model=model,
        contents=[ANALYSIS_PROMPT, _image_part(input_path)],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            temperature=0.1,
            # thinking 계열 모델은 추론 토큰도 이 한도에서 깎아간다.
            # 4096이면 객체가 많은 방에서 JSON이 잘려 json.loads가 깨진다.
            max_output_tokens=8192,
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini가 배치 분석 JSON을 반환하지 않았습니다.")
    return normalize_layout(_extract_json(response.text))


def _font(size: int) -> ImageFont.ImageFont:
    candidates = (
        Path("C:/Windows/Fonts/malgun.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return ImageFont.truetype(str(candidate), size=size)
    return ImageFont.load_default()


def _oriented_box(
    center_x: float,
    center_y: float,
    width: float,
    depth: float,
    angle_degrees: float,
) -> list[tuple[float, float]]:
    angle = math.radians(angle_degrees)
    cosine, sine = math.cos(angle), math.sin(angle)
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


def render_layout_guide(layout: dict[str, Any], output_path: Path) -> None:
    canvas = 1200
    margin = 70
    room = layout["room"]
    aspect = float(room["aspect_ratio_width_to_depth"])
    available = canvas - 2 * margin
    if aspect >= 1:
        room_width = available
        room_depth = available / aspect
    else:
        room_depth = available
        room_width = available * aspect
    left = (canvas - room_width) / 2
    top = (canvas - room_depth) / 2

    image = Image.new("RGB", (canvas, canvas), "#ece9e2")
    draw = ImageDraw.Draw(image)
    draw.rectangle(
        (left, top, left + room_width, top + room_depth),
        fill=str(room.get("floor_color") or "#6b4935"),
        outline="#292722",
        width=10,
    )

    label_font = _font(25)
    for obj in layout["objects"]:
        center_x = left + float(obj["x"]) * room_width
        center_y = top + float(obj["y"]) * room_depth
        width = float(obj["width"]) * room_width
        depth = float(obj["depth"]) * room_depth
        points = _oriented_box(
            center_x, center_y, width, depth, float(obj["rotation_deg"])
        )
        fill = str(
            obj.get("color")
            or CATEGORY_COLORS.get(str(obj["category"]), "#c2b7a3")
        )
        draw.polygon(points, fill=fill, outline="#24221f", width=4)
        label = str(obj["label_ko"])
        bbox = draw.textbbox((0, 0), label, font=label_font, stroke_width=2)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        draw.text(
            (center_x - text_width / 2, center_y - text_height / 2),
            label,
            font=label_font,
            fill="white",
            stroke_fill="#28251f",
            stroke_width=3,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)


def generate_topdown_image(
    client: genai.Client,
    input_path: Path,
    guide_path: Path,
    output_path: Path,
    model: str,
    aspect_ratio: str,
) -> str:
    response = client.models.generate_content(
        model=model,
        contents=[
            IMAGE_PROMPT,
            _image_part(input_path),
            _image_part(guide_path),
        ],
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect_ratio),
        ),
    )

    notes: list[str] = []
    parts = []
    if response.candidates and response.candidates[0].content:
        parts = response.candidates[0].content.parts or []
    for part in parts:
        if getattr(part, "text", None):
            notes.append(str(part.text))
        inline_data = getattr(part, "inline_data", None)
        if inline_data and inline_data.data:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(inline_data.data)
            return "\n".join(notes).strip()
    raise RuntimeError(
        "Gemini 이미지 모델이 이미지를 반환하지 않았습니다. "
        + ("\n".join(notes).strip() or "텍스트 설명도 없습니다.")
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="실내 사진을 Gemini로 탑뷰 평면 일러스트로 변환합니다."
    )
    parser.add_argument("image", type=Path, help="입력 실내 사진 경로")
    parser.add_argument(
        "--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT, help="출력 루트"
    )
    parser.add_argument(
        "--analysis-model",
        default=os.getenv("GEMINI_ANALYSIS_MODEL", DEFAULT_ANALYSIS_MODEL),
    )
    parser.add_argument(
        "--image-model",
        default=os.getenv("GEMINI_IMAGE_MODEL", DEFAULT_IMAGE_MODEL),
    )
    parser.add_argument(
        "--aspect-ratio",
        default="3:4",
        choices=("1:1", "2:3", "3:2", "3:4", "4:3", "4:5", "5:4", "9:16", "16:9"),
    )
    parser.add_argument(
        "--analysis-only",
        action="store_true",
        help="JSON과 가이드까지만 만들고 이미지 생성 API는 호출하지 않음",
    )
    parser.add_argument(
        "--reuse-layout",
        action="store_true",
        help="기존 layout.json이 있으면 분석 API 호출을 생략",
    )
    return parser


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args()
    input_path = args.image.expanduser().resolve()
    if not input_path.is_file():
        print(f"[오류] 입력 이미지를 찾을 수 없습니다: {input_path}", file=sys.stderr)
        return 2

    run_dir = args.output_root.expanduser().resolve() / _safe_stem(input_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    layout_path = run_dir / "layout.json"
    guide_path = run_dir / "layout_guide.png"
    generated_path = run_dir / "topdown_raw.png"
    metadata_path = run_dir / "run_metadata.json"

    try:
        client = _client()
        if args.reuse_layout and layout_path.exists():
            layout = normalize_layout(
                json.loads(layout_path.read_text(encoding="utf-8"))
            )
            layout_path.write_text(
                json.dumps(layout, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[1/4] 기존 분석 재사용: {layout_path}")
        else:
            print(f"[1/4] 공간 분석 중: {args.analysis_model}")
            layout = analyze_room(client, input_path, args.analysis_model)
            layout_path.write_text(
                json.dumps(layout, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"      저장: {layout_path}")

        print("[2/4] 탑뷰 배치 가이드 생성 중")
        render_layout_guide(layout, guide_path)
        print(f"      저장: {guide_path}")

        illustrated_path = run_dir / "topdown_illustrated.png"
        print("[3/4] 무료 탑뷰 일러스트 렌더링 중")
        render_illustration(layout, illustrated_path)
        print(f"      저장: {illustrated_path}")

        rendered_3d_path = run_dir / "topdown_3d.png"
        render_room_3d(layout, rendered_3d_path)
        print(f"      3D 저장: {rendered_3d_path}")

        metadata: dict[str, Any] = {
            "input": str(input_path),
            "analysis_model": args.analysis_model,
            "image_model": None if args.analysis_only else args.image_model,
            "layout": str(layout_path),
            "guide": str(guide_path),
            "illustrated": str(illustrated_path),
            "rendered_3d": str(rendered_3d_path),
            "generated": None,
        }

        if args.analysis_only:
            print("[4/4] --analysis-only: 유료 이미지 생성 API는 생략했습니다.")
        else:
            print(f"[4/4] Gemini 이미지 생성 중: {args.image_model}")
            notes = generate_topdown_image(
                client,
                input_path,
                guide_path,
                generated_path,
                args.image_model,
                args.aspect_ratio,
            )
            metadata["generated"] = str(generated_path)
            metadata["model_notes"] = notes
            print(f"      저장: {generated_path}")

        metadata_path.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[완료] 결과 폴더: {run_dir}")
        return 0
    except Exception as exc:
        error_path = run_dir / "error.txt"
        error_path.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        print(f"[실패] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"       오류 기록: {error_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
