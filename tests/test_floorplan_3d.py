from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2 import scene_graph  # noqa: E402
from model2.floorplan_3d import build_scene  # noqa: E402


def _analysis(objects, **room):
    return {"room": {"aspect_ratio_width_to_depth": 1.0, **room}, "objects": objects}


def _obj(oid, category, x, y, width, depth, **extra):
    return {"id": oid, "category": category, "label_ko": oid, "x": x, "y": y,
            "width": width, "depth": depth, "confidence": 0.9, **extra}


class HeightFromAnalysisTests(unittest.TestCase):
    """높이는 공간 분석이 사진에서 추정한 값이다. 종류별 고정 높이표는 없다."""

    def _scene(self, objects, **room):
        graph = scene_graph.from_analysis(_analysis(objects, **room), width_m=4.0, depth_m=4.0)
        return {o["id"]: o for o in build_scene(graph)["objects"]}, graph

    def test_estimated_height_and_elevation_pass_through(self) -> None:
        objects, _ = self._scene([
            _obj("low_1", "coffee_table", 0.5, 0.5, 0.2, 0.15, height_m=0.35),
            _obj("window_1", "window", 0.5, 0.0, 0.3, 0.02, height_m=1.2, elevation_m=0.9, wall_anchors=["top"]),
        ])
        self.assertEqual(objects["low_1"]["height_m"], 0.35)
        self.assertEqual((objects["window_1"]["height_m"], objects["window_1"]["base_m"]), (1.2, 0.9))
        self.assertTrue(objects["window_1"]["wall_mounted"])

    def test_missing_height_borrows_the_room_ratio(self) -> None:
        # 같은 방에서 높이를 아는 가구의 '높이 ÷ 긴 변' 비율을 빌린다
        objects, graph = self._scene([
            _obj("desk_1", "desk", 0.3, 0.3, 0.25, 0.15, height_m=0.5),
            _obj("desk_2", "desk", 0.7, 0.7, 0.25, 0.15),
        ])
        self.assertAlmostEqual(objects["desk_2"]["height_m"], 0.5, places=2)
        self.assertEqual(next(o for o in graph["objects"] if o["id"] == "desk_2")["h_source"], "room_ratio")

    def test_ceiling_from_analysis_or_tallest_object(self) -> None:
        _, graph = self._scene([_obj("a", "shelf", 0.5, 0.5, 0.2, 0.1, height_m=2.0)], ceiling_height_m=2.6)
        self.assertEqual(graph["room"]["ceiling_m"], 2.6)
        _, graph = self._scene([_obj("a", "shelf", 0.5, 0.5, 0.2, 0.1, height_m=2.1)])
        self.assertEqual(graph["room"]["ceiling_m"], 2.1)

    def test_old_layout_format_is_not_built(self) -> None:
        scene = build_scene({"room": {"aspect_ratio": 1.0}, "objects": [{"type": "bed", "x": 0.5, "y": 0.5}]})
        self.assertEqual((scene["objects"], scene["placement"]), ([], "unsupported"))

    def test_object_without_size_is_dropped_not_invented(self) -> None:
        graph = scene_graph.from_analysis(_analysis([_obj("a", "bed", 0.5, 0.5, 0.4, 0.5, height_m=0.5)]), width_m=4.0, depth_m=4.0)
        graph["objects"].append({"type": "desk", "x": 0.5, "y": 0.5, "w": 0.0, "h": 0.0, "source": "selected_product"})
        self.assertEqual([o["type"] for o in scene_graph.ensure(graph)["objects"]], ["bed"])


class Step6WiringTests(unittest.TestCase):
    """마지막 단계(STEP 5, 3D 배치 확인) 화면이 실제로 연결돼 있는지. Flask 없이 소스로 확인한다."""

    def setUp(self) -> None:
        self.app_src = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")

    def test_preview_3d_route_is_registered(self) -> None:
        self.assertIn('@app.route("/preview-3d")', self.app_src)
        self.assertIn("def preview_3d():", self.app_src)

    def test_route_prefers_the_layout_reflecting_user_choices(self) -> None:
        # 우선순위가 뒤집히면 3D가 사용자 선택을 반영하지 못한다
        block = self.app_src.split("def resolve_final_layout_path():", 1)[1]
        block = block.split("@app.route", 1)[0]
        order = [
            block.index("modified_layout_file"),
            block.index("edited_floorplan_layout_file"),
            block.index("floorplan_layout_file"),
        ]
        self.assertEqual(order, sorted(order), "layout 우선순위가 바뀌었습니다")

    def test_dev_cache_shortcut_is_debug_only(self) -> None:
        # 세션을 조작하는 개발용 경로이므로 운영에서 열려 있으면 안 된다
        block = self.app_src.split("def dev_use_cached():", 1)[1]
        block = block.split("@app.route", 1)[0]
        self.assertIn("if not app.debug:", block)
        self.assertIn("abort(404)", block)

    def test_dev_cache_shortcut_rejects_path_traversal(self) -> None:
        block = self.app_src.split("def dev_use_cached():", 1)[1]
        block = block.split("@app.route", 1)[0]
        # 파일명만 취하고, 캐시 완비 목록에 있는지까지 확인해야 한다
        self.assertIn("os.path.basename(", block)
        self.assertIn("if safe_name not in available:", block)

    def test_dev_cache_shortcut_skips_the_floorplan_step(self) -> None:
        block = self.app_src.split("def dev_use_cached():", 1)[1]
        block = block.split("@app.route", 1)[0]
        # /floorplan 을 거치지 않으므로 layout 경로를 직접 세션에 넣어야
        # preview_3d 의 resolve_final_layout_path 가 찾을 수 있다
        self.assertIn('"floorplan_layout_file"', block)
        self.assertIn("_model2_layout.json", block)
        self.assertIn('"preview_3d"', block)

    def test_cached_upload_scan_requires_the_full_cache_set(self) -> None:
        block = self.app_src.split("def cached_floorplan_uploads():", 1)[1]
        block = block.split("@app.route", 1)[0]
        for part in ("_model2_scene.json", "_model2_layout.json",
                     "_model2_floorplan.svg"):
            self.assertIn(part, block)

    def test_result_page_links_to_3d_step(self) -> None:
        result = (ROOT / "frontend" / "templates" / "result.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("url_for('preview_3d')", result)

    def test_3d_step_template_autostarts_the_viewer(self) -> None:
        page = (ROOT / "frontend" / "templates" / "preview_3d.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("STEP 5", page)
        self.assertIn('data-autostart="true"', page)
        # importmap 이 module 스크립트보다 먼저 와야 bare specifier 가 해석된다
        self.assertLess(page.index('type="importmap"'), page.index('type="module"'))

    def test_viewer_script_honours_autostart(self) -> None:
        js = (ROOT / "frontend" / "static" / "js" / "floorplan_3d.js").read_text(
            encoding="utf-8"
        )
        self.assertIn("dataset.autostart", js)
        self.assertIn("if (autostart) show3d();", js)


class TypeVocabularyAlignmentTests(unittest.TestCase):
    """타입 목록이 layout(파이썬)·구매 목록·프롬프트에 흩어져 있어 여기서 묶어둔다."""

    def _layout_vocabulary(self) -> set[str]:
        import re

        source = (ROOT / "mood_pipeline" / "rule_based_svg.py").read_text(encoding="utf-8")
        block = re.search(r"LAYOUT_OBJECT_TYPES = \[(.*?)\]", source, re.S)
        self.assertIsNotNone(block, "LAYOUT_OBJECT_TYPES 를 찾지 못했습니다")
        return set(re.findall(r'"(\w+)"', block.group(1)))

    def _purchasable_types(self) -> set[str]:
        import re

        source = (ROOT / "backend" / "app.py").read_text(encoding="utf-8")
        block = re.search(r"PURCHASE_LABELS = \{(.*?)\n\}", source, re.S)
        self.assertIsNotNone(block, "PURCHASE_LABELS 를 찾지 못했습니다")
        return set(re.findall(r'"(\w+)"\s*:', block.group(1)))

    def test_every_purchasable_type_is_in_the_layout_vocabulary(self) -> None:
        # 예전에는 소파·옷장·서랍장·벤치를 구매할 수 있는데 어휘에 없어서,
        # 구매하면 2D·3D 모두 unknown 회색 박스로 그려졌다.
        missing = self._purchasable_types() - self._layout_vocabulary()
        self.assertEqual(
            set(),
            missing,
            f"구매 가능하지만 layout 어휘에 없음(unknown 으로 그려집니다): {sorted(missing)}",
        )

    def test_wall_fixed_types_are_excluded_from_dragging_and_snapping(self) -> None:
        source = (ROOT / "mood_pipeline" / "rule_based_svg.py").read_text(encoding="utf-8")
        self.assertIn("FIXED_TYPES", source)
        # 드래그 판정과 격자 스냅이 같은 집합을 써야 벽 요소가 떠다니지 않는다
        self.assertIn("draggable = obj[\"type\"] not in FIXED_TYPES", source)
        self.assertIn('if o["type"] in FIXED_TYPES:', source)

    def test_synonyms_are_absorbed_into_the_vocabulary(self) -> None:
        from mood_pipeline.rule_based_svg import norm_type

        expected = {
            "couch": "sofa",
            "closet": "wardrobe",
            "chest_of_drawers": "dresser",
            "television": "tv",
            "refrigerator": "fridge",
            "air_conditioner": "aircon",
            "washing_machine": "washer",
            "dressing_table": "vanity",
            "bedside_table": "nightstand",
            "office_chair": "desk_chair",
            "curtains": "curtain",
        }
        for raw, canonical in expected.items():
            self.assertEqual(canonical, norm_type(raw), raw)

    def test_prompt_lists_the_same_types_as_the_code(self) -> None:
        # 프롬프트에 타입 목록이 하드코딩돼 있어 코드만 고치면 Gemini가 새 타입을 모른다
        prompt = (ROOT / "prompts" / "rule_based_layout.txt").read_text(encoding="utf-8")
        listed = prompt.split('"type": "one of:', 1)[1].split('"', 1)[0]
        named = {t.strip() for t in listed.split(",") if t.strip()}
        missing = self._layout_vocabulary() - named
        self.assertEqual(
            set(), missing, f"프롬프트에 빠진 타입(Gemini가 못 씀): {sorted(missing)}"
        )



class StepLabelTests(unittest.TestCase):
    """건너뛴 페이지(가구 선택·상품 선택)가 있어도 보이는 단계 번호는 1~5로 이어진다."""

    def test_visible_steps_are_sequential(self) -> None:
        pages = ["prompt", "upload", "floorplan", "result", "preview_3d"]
        for number, name in enumerate(pages, start=1):
            page = (ROOT / "frontend" / "templates" / f"{name}.html").read_text(encoding="utf-8")
            self.assertRegex(page, rf"STEP\s+{number}(?!\d)", name)

    def test_footer_links_to_repository(self) -> None:
        footer = (ROOT / "frontend" / "templates" / "footer.html").read_text(encoding="utf-8")
        self.assertIn('href="https://github.com/dongdongjun123/interior"', footer)


if __name__ == "__main__":
    unittest.main(verbosity=2)
