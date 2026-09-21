"""Gemini 텍스트 모델로 상세 탑뷰 SVG를 직접 생성하는 무료 실험.

이미지 생성 모델을 호출하지 않는다. Gemini 멀티모달 텍스트 출력으로 SVG
코드를 받은 뒤 설치된 Edge/Chrome을 이용해 PNG로 렌더링한다.
"""
from __future__ import annotations

import argparse
import io
import json
import mimetypes
import os
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types
from PIL import Image


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "model2" / "output" / "gemini_svg_experiment"
DEFAULT_MODEL = "gemini-2.5-flash"

SVG_PROMPT = """
You are an expert interior illustrator and SVG artist.
Create one finished, polished top-down room illustration from the supplied
interior photograph and layout JSON.

The target style is a warm hand-drawn architectural interior illustration:
- Directly overhead orthographic composition, not an oblique perspective.
- Objects must feel volumetric through their own construction: curved cushions,
  folded blankets, rounded upholstery, visible frames, drawer fronts, chair
  backs and legs, cabinet tops, lamps, books and small decor.
- Do NOT fake volume by extruding a flat silhouette or adding a large offset copy.
- Use restrained local shadows, gradients, highlights and curved paths only to
  describe the actual form of each object.
- Preserve furniture identity, placement, colors, materials and distinctive
  patterns from the photograph.
- Use the layout JSON positions and wall relationships. Do not rearrange items.
- Render rugs flat on the floor; render furniture above them.
- Add concise Korean labels near the major objects with a white fill and subtle
  dark outline. Never place labels over important small details.

Technical requirements:
- Return ONLY a complete <svg>...</svg>, no Markdown or explanation.
- viewBox="0 0 1024 1024", width="1024", height="1024".
- Self-contained SVG only. No external images, URLs, fonts, scripts,
  foreignObject, animation or embedded raster data.
- Use SVG defs for safe gradients, filters and reusable patterns.
- Wrap every scene object in one top-level <g> whose id exactly matches the
  corresponding layout JSON object id (for example id="bed_1"). Wrap its Korean
  label in a separate <g id="label-bed_1">. These IDs are mandatory because the
  application removes selected furniture by ID.
- Include a room boundary and a detailed wood or tile floor.
- Keep every visible shape inside the canvas.
- Draw object-specific geometry rather than generic rectangles.
- Use readable Korean text with font-family="Malgun Gothic, sans-serif".
- Keep the complete SVG compact and under 12,000 output tokens. Prefer reusable
  groups and patterns over thousands of repeated primitive elements.
""".strip()


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


def _extract_svg(raw: str) -> str:
    text = raw.strip()
    start = text.find("<svg")
    if start < 0:
        raise ValueError("Gemini 응답에서 SVG 시작 태그를 찾지 못했습니다.")
    end = text.rfind("</svg>")
    svg = (
        text[start : end + len("</svg>")]
        if end >= start
        else text[start:] + "\n</svg>"
    )
    root = ET.fromstring(svg)
    if not root.tag.endswith("svg"):
        raise ValueError("출력 루트가 SVG가 아닙니다.")
    forbidden_tags = {"script", "foreignObject", "animate", "set"}
    forbidden_attributes = {"href", "{http://www.w3.org/1999/xlink}href"}
    for element in root.iter():
        local_tag = element.tag.rsplit("}", 1)[-1]
        if local_tag in forbidden_tags:
            raise ValueError(f"허용하지 않는 SVG 요소가 포함됐습니다: {local_tag}")
        for attribute, value in element.attrib.items():
            if attribute in forbidden_attributes or "url(http" in value.lower():
                raise ValueError("외부 리소스를 참조하는 SVG는 허용하지 않습니다.")
    return svg


def _find_browser() -> Path:
    candidates = (
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise RuntimeError("SVG를 PNG로 변환할 Edge 또는 Chrome을 찾지 못했습니다.")


def _render_png(svg_path: Path, png_path: Path, profile_dir: Path) -> None:
    browser = _find_browser()
    profile_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(browser),
        "--headless=new",
        "--hide-scrollbars",
        "--disable-extensions",
        "--disable-background-networking",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile_dir}",
        "--window-size=1024,1024",
        f"--screenshot={png_path}",
        svg_path.resolve().as_uri(),
    ]
    completed = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
    )
    if completed.returncode != 0 or not png_path.exists():
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"브라우저 SVG 렌더링 실패: {detail}")


def _safe_stem(path: Path) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣_-]+", "_", path.stem).strip("_") or "room"


def _ensure_not_truncated(response: object) -> None:
    """토큰 한도로 잘린 응답을 내용 오류로 오인하지 않게 먼저 걸러낸다."""
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return
    if str(getattr(candidates[0], "finish_reason", "")).endswith("MAX_TOKENS"):
        usage = getattr(response, "usage_metadata", None)
        thoughts = getattr(usage, "thoughts_token_count", None) or 0
        raise RuntimeError(
            "Gemini 응답이 토큰 한도에 걸려 잘렸습니다"
            f"(thinking {thoughts} 토큰 소비). "
            "max_output_tokens를 늘리거나 thinking_budget=0으로 두세요."
        )


def generate_svg_text(
    client: genai.Client,
    image_path: Path,
    layout: dict,
    *,
    model: str,
) -> tuple[str, str]:
    """사진과 배치 JSON으로 검증된 SVG와 원문 응답을 반환한다."""
    layout_prompt = (
        SVG_PROMPT
        + "\n\nLAYOUT JSON:\n"
        + json.dumps(layout, ensure_ascii=False, indent=2)
    )
    response = client.models.generate_content(
        model=model,
        contents=[layout_prompt, _image_part(image_path)],
        config=types.GenerateContentConfig(
            response_mime_type="text/plain",
            temperature=0.2,
            max_output_tokens=20000,
            # thinking 토큰이 max_output_tokens를 함께 쓰므로 SVG가 잘릴 수 있다.
            # 배치 JSON이 이미 주어진 상태의 변환 작업이라 thinking을 끈다.
            thinking_config=types.ThinkingConfig(thinking_budget=0),
        ),
    )
    if not response.text:
        raise RuntimeError("Gemini가 SVG 텍스트를 반환하지 않았습니다.")
    _ensure_not_truncated(response)
    return _extract_svg(response.text), response.text


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Gemini 텍스트 출력으로 상세 탑뷰 SVG/PNG를 생성합니다."
    )
    parser.add_argument("image", type=Path)
    parser.add_argument("--layout", type=Path, required=True)
    parser.add_argument("--model", default=os.getenv("GEMINI_ANALYSIS_MODEL", DEFAULT_MODEL))
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--reuse-svg", action="store_true")
    parser.add_argument(
        "--reuse-raw",
        action="store_true",
        help="저장된 raw_response.txt를 복구해 사용하고 API를 호출하지 않음",
    )
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    input_path = args.image.expanduser().resolve()
    layout_path = args.layout.expanduser().resolve()
    if not input_path.is_file():
        print(f"[오류] 입력 사진을 찾을 수 없습니다: {input_path}", file=sys.stderr)
        return 2
    if not layout_path.is_file():
        print(f"[오류] 배치 JSON을 찾을 수 없습니다: {layout_path}", file=sys.stderr)
        return 2

    run_dir = args.output_root.expanduser().resolve() / _safe_stem(input_path)
    run_dir.mkdir(parents=True, exist_ok=True)
    svg_path = run_dir / "topdown_gemini.svg"
    png_path = run_dir / "topdown_gemini.png"
    error_path = run_dir / "error.txt"
    raw_response_path = run_dir / "raw_response.txt"

    try:
        layout = json.loads(layout_path.read_text(encoding="utf-8"))
        if args.reuse_svg and svg_path.exists():
            print(f"[1/2] 기존 SVG 재사용: {svg_path}")
        elif args.reuse_raw and raw_response_path.exists():
            svg_path.write_text(
                _extract_svg(raw_response_path.read_text(encoding="utf-8")),
                encoding="utf-8",
            )
            print(f"[1/2] 저장된 Gemini 응답 복구: {svg_path}")
        else:
            print(f"[1/2] Gemini 상세 SVG 생성 중: {args.model}")
            client = _client()
            svg_text, raw_text = generate_svg_text(
                client,
                input_path,
                layout,
                model=args.model,
            )
            raw_response_path.write_text(raw_text, encoding="utf-8")
            svg_path.write_text(svg_text, encoding="utf-8")
            print(f"      저장: {svg_path}")

        print("[2/2] SVG를 PNG로 렌더링 중")
        _render_png(svg_path, png_path, run_dir / ".browser_profile")
        print(f"      저장: {png_path}")
        print(f"[완료] 결과 폴더: {run_dir}")
        return 0
    except Exception as exc:
        error_path.write_text(f"{type(exc).__name__}: {exc}", encoding="utf-8")
        print(f"[실패] {type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"       오류 기록: {error_path}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
