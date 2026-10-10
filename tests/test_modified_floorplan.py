"""상품을 추가한 수정 평면도(2D). 외부 호출 없음."""
from __future__ import annotations

import re
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2 import scene_graph  # noqa: E402
from model2.scene_render_2d import render_svg  # noqa: E402
from model2.web_floorplan import create_modified_svg  # noqa: E402

SVG_NS = "{http://www.w3.org/2000/svg}"


def _graph() -> dict:
    return scene_graph.from_analysis(
        {
            "room": {"aspect_ratio_width_to_depth": 0.8},
            "objects": [
                {"id": "bed_1", "category": "bed", "label_ko": "침대", "x": 0.3, "y": 0.4,
                 "width": 0.5, "depth": 0.3, "rotation_deg": 0, "wall_anchors": ["left"], "confidence": 0.9},
            ],
        },
        width_m=3.2,
        depth_m=4.0,
    )


class ModifiedFloorplanTests(unittest.TestCase):
    def test_added_product_references_only_defined_filters(self) -> None:
        # 브라우저는 없는 필터를 참조한 요소를 그리지 않는다. scene_graph SVG에는
        # shadow-med가 없어서 추가한 상품이 3D에만 보이고 2D에서는 사라졌다
        graph = _graph()
        modified = scene_graph.ensure(
            {
                **graph,
                "objects": graph["objects"]
                + [
                    {"type": "table", "label": "테이블", "x": 0.6, "y": 0.6, "w": 0.15, "h": 0.1,
                     "source": "selected_product", "product_marker": 1},
                ],
            }
        )
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original.svg"
            original.write_text(render_svg(graph), encoding="utf-8")
            output = Path(tmp) / "modified.svg"
            create_modified_svg(
                original,
                graph,
                modified,
                remove_indices=set(),
                selected_products=[{"type": "table", "marker": 1, "title": "테이블"}],
                output_path=output,
            )
            root = ET.parse(output).getroot()

        ids = {element.get("id") for element in root.iter()}
        self.assertIn("selected-product-1", ids)
        defined = {element.get("id") for element in root.iter(f"{SVG_NS}filter")}
        for element in root.iter():
            match = re.fullmatch(r"url\(#([^)]+)\)", str(element.get("filter") or ""))
            if match:
                self.assertIn(match.group(1), defined, element.get("id"))


if __name__ == "__main__":
    unittest.main()
