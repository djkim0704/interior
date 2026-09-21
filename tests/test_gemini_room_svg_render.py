import xml.etree.ElementTree as ET

import pytest

from model2.gemini_room_svg_render import _room_svg_models, _sanitize_svg


def test_room_svg_models_preserve_order_and_remove_duplicates(monkeypatch):
    monkeypatch.setenv(
        "GEMINI_ROOM_SVG_FALLBACK_MODELS",
        "gemini-backup-a, gemini-primary, gemini-backup-b",
    )

    assert _room_svg_models("gemini-primary") == [
        "gemini-primary",
        "gemini-backup-a",
        "gemini-backup-b",
    ]


def test_sanitize_svg_expands_safe_inline_style():
    source = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <defs>
        <linearGradient id="wood"><stop style="stop-color:#8b5a2b;stop-opacity:.8" /></linearGradient>
        <filter id="shadow"><feGaussianBlur stdDeviation="4" /></filter>
      </defs>
      <rect style="fill:url(#wood);stroke:#222;stroke-width:2;filter:url(#shadow)" />
    </svg>
    """

    sanitized = _sanitize_svg(source)
    root = ET.fromstring(sanitized)
    elements = list(root.iter())
    stop = next(item for item in elements if item.tag.endswith("stop"))
    rect = next(item for item in elements if item.tag.endswith("rect"))

    assert "style" not in stop.attrib
    assert stop.attrib["stop-color"] == "#8b5a2b"
    assert stop.attrib["stop-opacity"] == ".8"
    assert "style" not in rect.attrib
    assert rect.attrib["fill"] == "url(#wood)"
    assert rect.attrib["stroke"] == "#222"
    assert rect.attrib["filter"] == "url(#shadow)"


def test_sanitize_svg_drops_unsafe_inline_style_value():
    source = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <rect style="fill:url(https://example.com/image.svg);stroke:#222" />
    </svg>
    """

    sanitized = _sanitize_svg(source)
    root = ET.fromstring(sanitized)
    rect = next(item for item in root.iter() if item.tag.endswith("rect"))

    assert "style" not in rect.attrib
    assert "fill" not in rect.attrib
    assert rect.attrib["stroke"] == "#222"


def test_sanitize_svg_rejects_event_handler():
    source = """
    <svg xmlns="http://www.w3.org/2000/svg">
      <rect fill="#fff" onclick="alert(1)" />
    </svg>
    """

    with pytest.raises(ValueError, match="허용하지 않는 SVG 속성"):
        _sanitize_svg(source)
