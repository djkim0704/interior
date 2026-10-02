"""상품의 실제 가로·세로·높이를 찾는다 (항목 6·14).

쇼핑 검색 결과에는 치수가 따로 오지 않는다. 그래서 아래 순서로 찾고, 어디서 찾았는지
dimension_source로 남긴다. 3D·배치에 쓰인 크기가 실측인지 추정인지 구분해야 하기 때문이다.

  1) title / snippet      제목·요약의 "1200x600x750", "W120 D60 H75cm" 같은 표기
  2) page                 상품 페이지(JSON-LD, 메타 설명, 본문 텍스트)
  3) image                Gemini가 상품 사진 속 치수표에서 읽은 값(product_attributes)
  4) ai_estimate          Gemini가 상품 사진·제목으로 추정한 크기(product_attributes)

어디서도 못 찾으면 None이다. 종류별 표준 크기나 규격표로 지어내지 않는다.

치수 축 규약은 Scene Graph와 같다. w = 뒷면(헤드보드·등받이)과 나란한 폭,
d = 앞뒤 깊이(침대는 길이), h = 높이. 단위는 미터.
"""
from __future__ import annotations

import hashlib
import html as html_lib
import json
import os
import time
import re
from pathlib import Path
from typing import Any

from . import scene_graph

MIN_M, MAX_M = 0.08, 4.0
PAGE_TIMEOUT_S = 6
PAGE_MAX_BYTES = 2_000_000

_NUM = r"(\d{1,4}(?:[.,]\d{1,2})?)"
_UNIT = r"\s*(mm|cm|m|밀리|센치|센티|미터)?"
_SEP = r"\s*[xX×*✕＊]\s*"
TRIPLE = re.compile(_NUM + _UNIT + _SEP + _NUM + _UNIT + r"(?:" + _SEP + _NUM + _UNIT + r")?", re.I)
LABELS = {
    "w": r"(?:W|가로|폭|너비|width)",
    "d": r"(?:D|세로|깊이|depth|길이|L)",
    "h": r"(?:H|높이|height)",
}
LABELED = {
    axis: re.compile(r"(?<![A-Za-z])" + label + r"\s*[:：=]?\s*" + _NUM + _UNIT, re.I)
    for axis, label in LABELS.items()
}

def _to_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def _unit_scale(unit: str | None, values: list[float]) -> float:
    unit = (unit or "").lower()
    if unit in {"mm", "밀리"}:
        return 0.001
    if unit in {"cm", "센치", "센티"}:
        return 0.01
    if unit in {"m", "미터"}:
        return 1.0
    # 단위가 없으면 크기로 추정한다. 가구 치수에서 300 이상은 거의 항상 mm다
    biggest = max(values)
    if biggest >= 300:
        return 0.001
    if biggest >= 20:
        return 0.01
    return 1.0


def _plausible(values: list[float]) -> bool:
    return all(MIN_M <= v <= MAX_M for v in values)


def parse_dimensions(text: str) -> dict[str, Any] | None:
    """텍스트에서 가로·세로(·높이)를 찾는다. 없거나 말이 안 되면 None."""
    if not text:
        return None
    text = html_lib.unescape(str(text))

    # 1) 축 이름이 붙은 표기가 가장 믿을 만하다 (W1200 D600 H750)
    labeled: dict[str, float] = {}
    units: dict[str, str | None] = {}
    for axis, pattern in LABELED.items():
        match = pattern.search(text)
        if match:
            labeled[axis] = _to_float(match.group(1))
            units[axis] = match.group(2)
    if "w" in labeled and "d" in labeled:
        unit = next((u for u in units.values() if u), None)
        scale = _unit_scale(unit, list(labeled.values()))
        values = {k: v * scale for k, v in labeled.items()}
        if _plausible(list(values.values())):
            return {
                "w_m": round(values["w"], 3),
                "d_m": round(values["d"], 3),
                "h_m": round(values["h"], 3) if "h" in values else None,
                "raw": text[:0] or None,
                "pattern": "labeled",
            }

    # 2) 1200x600x750 형태
    for match in TRIPLE.finditer(text):
        numbers = [match.group(i) for i in (1, 3, 5)]
        unit = next((u for u in (match.group(6), match.group(4), match.group(2)) if u), None)
        values = [_to_float(n) for n in numbers if n]
        if len(values) < 2:
            continue
        scale = _unit_scale(unit, values)
        meters = [v * scale for v in values]
        if not _plausible(meters):
            continue
        return {
            "w_m": round(meters[0], 3),
            "d_m": round(meters[1], 3),
            "h_m": round(meters[2], 3) if len(meters) > 2 else None,
            "raw": match.group(0),
            "pattern": "triple",
        }
    return None


def orient(kind: str, dims: dict[str, Any]) -> dict[str, Any]:
    """판매처마다 가로·세로 순서가 다르다. 타입별 상식으로 축을 맞춘다.

    침대는 길이(d)가 폭(w)보다 길어야 하고, 소파·책상·수납장은 폭(w)이 깊이(d)보다 길다.
    """
    w, d = float(dims["w_m"]), float(dims["d_m"])
    if kind == "bed" and w > d:
        w, d = d, w
    elif kind in {"sofa", "desk", "cabinet", "dresser", "wardrobe", "shelf", "bench", "tv", "vanity"} and d > w:
        w, d = d, w
    return {**dims, "w_m": w, "d_m": d}


# ---------------------------------------------------------------- 상품 페이지

def extract_from_page(page_html: str) -> dict[str, Any] | None:
    """상품 페이지 HTML에서 치수를 찾는다. JSON-LD → 메타 설명 → 본문 순."""
    if not page_html:
        return None
    # JSON-LD의 Product.width/depth/height
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page_html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except (ValueError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            parts = []
            for key in ("width", "depth", "height"):
                value = item.get(key)
                if isinstance(value, dict):
                    value = f"{value.get('value', '')}{value.get('unitText') or value.get('unitCode') or ''}"
                parts.append(str(value or ""))
            if parts[0] and parts[1]:
                found = parse_dimensions(f"W{parts[0]} D{parts[1]} H{parts[2]}")
                if found:
                    return {**found, "pattern": "json_ld"}
            for key in ("description", "name"):
                found = parse_dimensions(str(item.get(key) or ""))
                if found:
                    return found
    for content in re.findall(r'<meta[^>]+(?:name|property)="(?:og:)?description"[^>]+content="([^"]*)"', page_html, re.I):
        found = parse_dimensions(content)
        if found:
            return found
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", page_html, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    # 본문은 상품 코드·가격 같은 숫자가 많아서, '사이즈/규격/치수' 근처만 본다
    for match in re.finditer(r"(사이즈|규격|치수|크기|size|dimension)", text, re.I):
        found = parse_dimensions(text[match.start() : match.start() + 160])
        if found:
            return found
    return None


def fetch_page(url: str, cache_dir: Path) -> str | None:
    """상품 페이지를 받아 온다. 결과(실패 포함)를 캐시해 같은 상품은 다시 받지 않는다.

    PRODUCT_PAGE_FETCH=0이면 받지 않는다. 쇼핑몰이 차단하거나 느리면 None.
    """
    if os.getenv("PRODUCT_PAGE_FETCH", "1").strip().lower() in {"0", "false", "no", "off"}:
        return None
    if not url.startswith(("http://", "https://")):
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
    path = cache_dir / f"{key}.html"
    failed = cache_dir / f"{key}.failed"
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    if failed.exists():
        # 쇼핑몰 일시 장애로 영영 못 받는 일이 없도록 실패 기록은 하루만 유효하다
        if time.time() - failed.stat().st_mtime < 24 * 3600:
            return None
        failed.unlink(missing_ok=True)
    try:
        import requests

        with requests.Session() as session:
            session.trust_env = False
            response = session.get(
                url,
                timeout=PAGE_TIMEOUT_S,
                headers={"User-Agent": "Mozilla/5.0 (compatible; interior-dimension-bot/1.0)"},
                stream=True,
            )
            response.raise_for_status()
            body = response.raw.read(PAGE_MAX_BYTES, decode_content=True)
        text = body.decode(response.encoding or "utf-8", errors="replace")
    except Exception as exc:
        failed.write_text(str(exc)[:300], encoding="utf-8")
        return None
    path.write_text(text, encoding="utf-8")
    return text


# ---------------------------------------------------------------- 종합

def resolve(product: dict[str, Any], cache_dir: str | Path | None = None, *, fetch: bool = True) -> dict[str, Any]:
    """상품 하나의 치수를 찾는다. 근거는 dimension_source, 못 찾으면 None."""
    kind = scene_graph.object_type(product.get("type"))
    title = str(product.get("title") or "")
    snippet = " ".join(str(product.get(k) or "") for k in ("snippet", "category1", "extensions"))

    candidates: list[tuple[str, dict[str, Any] | None]] = [
        ("title", parse_dimensions(title)),
        ("snippet", parse_dimensions(snippet)),
    ]
    found = next(((source, dims) for source, dims in candidates if dims), None)
    if found is None and fetch and cache_dir is not None and product.get("link"):
        page = fetch_page(str(product["link"]), Path(cache_dir) / "product_pages_v1")
        dims = extract_from_page(page or "")
        if dims:
            found = ("page", dims)
    if found is None:
        image_dims = ((product.get("visual_profile") or {}).get("dimensions") or {})
        if image_dims.get("w_m") and image_dims.get("d_m"):
            found = ("image", {**image_dims, "pattern": "image_table"})
    estimate = (product.get("visual_profile") or {}).get("estimated_dimensions") or {}
    if found is None and estimate.get("w_m") and estimate.get("d_m"):
        found = ("ai_estimate", {**estimate, "pattern": "ai_estimate"})
    if found is None:
        return None

    source, dims = found
    # 치수 표기에 높이가 없으면(가로×세로만) Gemini 추정 높이로 채운다
    if not dims.get("h_m") and estimate.get("h_m"):
        dims = {**dims, "h_m": estimate["h_m"]}
    dims = orient(kind, dims)
    return {
        "w_m": round(float(dims["w_m"]), 3),
        "d_m": round(float(dims["d_m"]), 3),
        "h_m": round(float(dims["h_m"]), 3) if dims.get("h_m") else None,
        "dimension_source": source,
        # 실측(제목·설명·페이지·사진 속 치수표)인지, AI가 사진으로 추정한 값인지
        "measured": source in {"title", "snippet", "page", "image"},
        "raw": dims.get("raw"),
    }
