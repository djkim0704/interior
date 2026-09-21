from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from model2.floorplan_3d import (  # noqa: E402
    DEFAULT_CEILING_M,
    DEFAULT_LONG_SIDE_M,
    TYPE_PRESETS,
    WALL_ROTATION,
    build_scene,
    convert_object,
    resolve_room,
)


class ResolveRoomTests(unittest.TestCase):
    def test_user_dimensions_win_over_layout_and_estimate(self) -> None:
        layout = {"room": {"aspect_ratio": 0.5, "width_m": 9.0, "depth_m": 9.0}}
        room = resolve_room(layout, {"width_m": 3.6, "depth_m": 5.0, "ceiling_m": 2.3})
        self.assertEqual(room["width_m"], 3.6)
        self.assertEqual(room["depth_m"], 5.0)
        self.assertEqual(room["ceiling_m"], 2.3)
        self.assertFalse(room["estimated"])

    def test_layout_dimensions_used_when_no_user_input(self) -> None:
        layout = {"room": {"aspect_ratio": 0.5, "width_m": 2.5, "depth_m": 4.5}}
        room = resolve_room(layout, None)
        self.assertEqual(room["width_m"], 2.5)
        self.assertEqual(room["depth_m"], 4.5)
        self.assertFalse(room["estimated"])

    def test_estimates_from_aspect_ratio_and_flags_it(self) -> None:
        # 세로가 긴 방(가로÷세로 < 1) → 긴 변인 depth 를 기준 길이로 잡는다
        room = resolve_room({"room": {"aspect_ratio": 0.72}}, None)
        self.assertTrue(room["estimated"])
        self.assertEqual(room["depth_m"], DEFAULT_LONG_SIDE_M)
        self.assertAlmostEqual(room["width_m"], DEFAULT_LONG_SIDE_M * 0.72, places=3)
        self.assertEqual(room["ceiling_m"], DEFAULT_CEILING_M)

    def test_estimates_wide_room_uses_width_as_long_side(self) -> None:
        room = resolve_room({"room": {"aspect_ratio": 1.6}}, None)
        self.assertEqual(room["width_m"], DEFAULT_LONG_SIDE_M)
        self.assertAlmostEqual(room["depth_m"], DEFAULT_LONG_SIDE_M / 1.6, places=3)

    def test_null_and_zero_dimensions_fall_through_to_estimate(self) -> None:
        # 실제 layout 에 width_m: null 이 들어오는 경우가 있다
        layout = {"room": {"aspect_ratio": 1.0, "width_m": None, "depth_m": 0}}
        room = resolve_room(layout, {"width_m": None, "depth_m": None})
        self.assertTrue(room["estimated"])
        self.assertEqual(room["width_m"], DEFAULT_LONG_SIDE_M)

    def test_absurd_aspect_ratio_is_clamped(self) -> None:
        narrow = resolve_room({"room": {"aspect_ratio": 0.001}}, None)
        wide = resolve_room({"room": {"aspect_ratio": 99}}, None)
        self.assertGreater(narrow["width_m"], 0)
        self.assertGreater(wide["depth_m"], 0)

    def test_missing_layout_does_not_raise(self) -> None:
        room = resolve_room(None, None)
        self.assertTrue(room["estimated"])
        self.assertGreater(room["width_m"], 0)


class ConvertObjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.room = {"width_m": 4.0, "depth_m": 5.0, "ceiling_m": 2.4}

    def test_normalized_coords_become_meters(self) -> None:
        obj = {"type": "bed", "label": "침대", "x": 0.25, "y": 0.5,
               "w": 0.5, "h": 0.4, "wall": "top", "scene_id": "bed_1"}
        result = convert_object(obj, self.room)
        self.assertEqual(result["cx"], 1.0)    # 0.25 * 4.0
        self.assertEqual(result["cy"], 2.5)    # 0.50 * 5.0
        self.assertEqual(result["w_m"], 2.0)   # 0.50 * 4.0
        self.assertEqual(result["d_m"], 2.0)   # 0.40 * 5.0
        self.assertEqual(result["id"], "bed_1")
        self.assertEqual(result["label"], "침대")

    def test_height_comes_from_type_preset(self) -> None:
        result = convert_object({"type": "shelf", "x": 0.5, "y": 0.5}, self.room)
        self.assertEqual(result["height_m"], TYPE_PRESETS["shelf"]["height_m"])

    def test_wall_maps_to_rotation(self) -> None:
        for wall, expected in WALL_ROTATION.items():
            result = convert_object(
                {"type": "desk", "x": 0.5, "y": 0.5, "wall": wall}, self.room
            )
            self.assertEqual(result["rotation_deg"], expected, wall)

    def test_missing_wall_is_inferred_from_position(self) -> None:
        near_top = convert_object({"type": "desk", "x": 0.5, "y": 0.05}, self.room)
        near_left = convert_object({"type": "desk", "x": 0.03, "y": 0.5}, self.room)
        self.assertEqual(near_top["wall"], "top")
        self.assertEqual(near_left["wall"], "left")

    def test_wall_none_is_preserved_without_rotation(self) -> None:
        result = convert_object(
            {"type": "low_table", "x": 0.5, "y": 0.5, "wall": "none"}, self.room
        )
        self.assertEqual(result["wall"], "none")
        self.assertEqual(result["rotation_deg"], 0.0)

    def test_unknown_type_falls_back_without_raising(self) -> None:
        result = convert_object({"type": "spaceship", "x": 0.5, "y": 0.5}, self.room)
        self.assertEqual(result["type"], "spaceship")
        self.assertEqual(result["height_m"], TYPE_PRESETS["unknown"]["height_m"])

    def test_window_is_wall_mounted_and_elevated(self) -> None:
        result = convert_object({"type": "window", "x": 0.5, "y": 0.0}, self.room)
        self.assertTrue(result["wall_mounted"])
        self.assertGreater(result["base_m"], 0)

    def test_rug_lies_flat_on_the_floor(self) -> None:
        result = convert_object({"type": "rug", "x": 0.5, "y": 0.5}, self.room)
        self.assertEqual(result["base_m"], 0.0)
        self.assertLess(result["height_m"], 0.05)

    def test_garbage_values_are_clamped_not_crashed(self) -> None:
        obj = {"type": "bed", "x": "abc", "y": None, "w": -3, "h": 99}
        result = convert_object(obj, self.room)
        self.assertGreaterEqual(result["cx"], 0.0)
        self.assertLessEqual(result["cx"], self.room["width_m"])
        self.assertGreater(result["w_m"], 0)
        self.assertLessEqual(result["d_m"], self.room["depth_m"])

    def test_non_dict_object_returns_none(self) -> None:
        self.assertIsNone(convert_object("bed", self.room))  # type: ignore[arg-type]


class BuildSceneTests(unittest.TestCase):
    def test_full_scene_from_real_layout_shape(self) -> None:
        layout = {
            "schema": "rule_based_v3",
            "room": {"aspect_ratio": 0.72, "width_m": None, "depth_m": None},
            "objects": [
                {"type": "bed", "label": "침대", "x": 0.228, "y": 0.278,
                 "w": 0.42, "h": 0.52, "wall": "top", "scene_id": "bed_1"},
                {"type": "desk", "label": "책상", "x": 0.8, "y": 0.3,
                 "w": 0.2, "h": 0.4, "wall": "right", "scene_id": "desk_1"},
            ],
        }
        scene = build_scene(layout, {"width_m": 3.6, "depth_m": 5.0, "ceiling_m": 2.3})
        self.assertEqual(len(scene["objects"]), 2)
        self.assertFalse(scene["room"]["estimated"])
        self.assertEqual(scene["objects"][1]["rotation_deg"], 90.0)
        self.assertIn("bed", scene["known_types"])

    def test_objects_stay_inside_the_room(self) -> None:
        layout = {
            "room": {"aspect_ratio": 1.0},
            "objects": [{"type": t, "x": 0.5, "y": 0.5, "w": 0.3, "h": 0.3}
                        for t in TYPE_PRESETS],
        }
        scene = build_scene(layout, {"width_m": 4.0, "depth_m": 4.0})
        for obj in scene["objects"]:
            self.assertGreaterEqual(obj["cx"], 0.0)
            self.assertLessEqual(obj["cx"], 4.0)
            self.assertGreaterEqual(obj["cy"], 0.0)
            self.assertLessEqual(obj["cy"], 4.0)
            self.assertGreater(obj["height_m"], 0.0)

    def test_every_supported_type_has_a_complete_preset(self) -> None:
        required = {"height_m", "base_m", "color", "wall_mounted"}
        for name, preset in TYPE_PRESETS.items():
            self.assertEqual(required, set(preset), name)
            self.assertGreater(preset["height_m"], 0, name)
            self.assertTrue(
                preset["color"].startswith("#") and len(preset["color"]) == 7, name
            )

    def test_empty_and_malformed_input_yields_empty_scene(self) -> None:
        for layout in ({}, {"objects": None}, {"objects": []}, None):
            scene = build_scene(layout, None)
            self.assertEqual(scene["objects"], [])
            self.assertGreater(scene["room"]["width_m"], 0)

    def test_malformed_entries_are_skipped_not_fatal(self) -> None:
        layout = {"objects": [{"type": "bed", "x": 0.5, "y": 0.5}, "junk", None, 42]}
        scene = build_scene(layout, None)
        self.assertEqual(len(scene["objects"]), 1)

    def test_ids_are_unique_when_scene_id_missing(self) -> None:
        layout = {"objects": [{"type": "chair", "x": 0.2, "y": 0.2},
                              {"type": "chair", "x": 0.8, "y": 0.8}]}
        scene = build_scene(layout, None)
        ids = [obj["id"] for obj in scene["objects"]]
        self.assertEqual(len(ids), len(set(ids)))


class SvgPlacementParityTests(unittest.TestCase):
    """3D 좌표는 2D 평면도와 같아야 한다.

    rule_based_svg 는 layout 의 x·y·w·h 를 그대로 그리지 않는다. 표준 크기 대체,
    벽 맞춤, 자동 패킹, 겹침 해소, 격자 스냅을 거친다. 3D가 raw layout 을 쓰면
    2D와 어긋나므로 같은 파이프라인을 통과한 결과를 쓰는지 확인한다.
    """

    ROOM = {"width_m": 3.0, "depth_m": 4.2, "ceiling_m": 2.3}

    def _layout(self, objects):
        return {"room": {"aspect_ratio": 0.72}, "objects": objects}

    def test_scene_reports_svg_placement(self) -> None:
        scene = build_scene(
            self._layout([{"type": "bed", "x": 0.23, "y": 0.28, "w": 0.42, "h": 0.52}]),
            self.ROOM,
        )
        self.assertEqual("svg", scene["placement"])

    def test_added_product_with_zero_size_gets_a_real_footprint(self) -> None:
        # create_modified_floorplan 은 새로 추가한 상품에 w=0, h=0 을 넣는다.
        # 예전 어댑터는 이걸 최소치(0.02)로 кламп해서 3D에서 6cm 조각이 됐다.
        layout = self._layout([
            {"type": "desk", "label": "책상", "x": 0.24, "y": 0.24,
             "w": 0.0, "h": 0.0, "wall": "top",
             "source": "selected_product", "product_marker": 1},
        ])
        desk = build_scene(layout, self.ROOM)["objects"][0]
        self.assertGreater(desk["w_m"], 0.5, "추가 상품이 여전히 찌그러집니다")
        self.assertGreater(desk["d_m"], 0.3, "추가 상품이 여전히 찌그러집니다")

    def test_placement_matches_rule_based_svg_exactly(self) -> None:
        import copy

        from mood_pipeline.rule_based_svg import resolve_placement

        objects = [
            {"type": "bed", "label": "침대", "x": 0.23, "y": 0.28,
             "w": 0.42, "h": 0.52, "wall": "top"},
            {"type": "desk", "label": "책상", "x": 0.24, "y": 0.24,
             "w": 0.0, "h": 0.0, "wall": "top", "source": "selected_product"},
            {"type": "cabinet", "label": "수납장", "x": 0.5, "y": 0.24,
             "w": 0.0, "h": 0.0, "wall": "left"},
        ]
        layout = self._layout(objects)

        _, placed, canvas = resolve_placement(copy.deepcopy(layout))
        scene = build_scene(copy.deepcopy(layout), self.ROOM)

        self.assertEqual(len(placed), len(scene["objects"]))
        for svg_obj, obj_3d in zip(placed, scene["objects"]):
            self.assertEqual(svg_obj["type"], obj_3d["type"])
            # 픽셀 좌표를 방 비율로 되돌린 뒤 미터로 환산한 값과 일치해야 한다
            expected_cx = (
                (svg_obj["cx"] - canvas["margin_x"]) / canvas["room_w"]
            ) * self.ROOM["width_m"]
            expected_w = (svg_obj["w"] / canvas["room_w"]) * self.ROOM["width_m"]
            expected_d = (svg_obj["h"] / canvas["room_h"]) * self.ROOM["depth_m"]
            self.assertAlmostEqual(expected_cx, obj_3d["cx"], places=3)
            self.assertAlmostEqual(expected_w, obj_3d["w_m"], places=3)
            self.assertAlmostEqual(expected_d, obj_3d["d_m"], places=3)

    def test_purchased_furniture_is_flagged_for_the_viewer(self) -> None:
        layout = self._layout([
            {"type": "bed", "label": "침대", "x": 0.23, "y": 0.28,
             "w": 0.42, "h": 0.52, "wall": "top"},
            {"type": "desk", "label": "책상", "x": 0.24, "y": 0.24, "w": 0.0, "h": 0.0,
             "wall": "top", "source": "selected_product", "product_marker": 3,
             "product_title": "원목 책상 1200"},
        ])
        by_type = {o["type"]: o for o in build_scene(layout, self.ROOM)["objects"]}
        self.assertTrue(by_type["desk"]["is_product"])
        self.assertEqual(3, by_type["desk"]["marker"])
        self.assertEqual("원목 책상 1200", by_type["desk"]["product_title"])
        self.assertFalse(by_type["bed"]["is_product"])
        self.assertIsNone(by_type["bed"]["marker"])

    def test_objects_stay_inside_the_room_after_svg_placement(self) -> None:
        layout = self._layout([
            {"type": t, "x": 0.5, "y": 0.5, "w": 0.0, "h": 0.0} for t in TYPE_PRESETS
        ])
        for obj in build_scene(layout, self.ROOM)["objects"]:
            self.assertGreaterEqual(obj["cx"], 0.0, obj["type"])
            self.assertLessEqual(obj["cx"], self.ROOM["width_m"], obj["type"])
            self.assertGreaterEqual(obj["cy"], 0.0, obj["type"])
            self.assertLessEqual(obj["cy"], self.ROOM["depth_m"], obj["type"])

    def test_falls_back_to_raw_layout_when_renderer_unavailable(self) -> None:
        import model2.floorplan_3d as module

        original = module._placed_objects
        module._placed_objects = lambda layout: (_ for _ in ()).throw(
            ImportError("renderer missing")
        )
        try:
            scene = build_scene(
                self._layout([{"type": "bed", "x": 0.3, "y": 0.3, "w": 0.4, "h": 0.5}]),
                self.ROOM,
            )
        finally:
            module._placed_objects = original

        self.assertEqual("raw", scene["placement"])
        self.assertEqual(1, len(scene["objects"]))


class Step6WiringTests(unittest.TestCase):
    """STEP 6(3D 배치 확인) 화면이 실제로 연결돼 있는지. Flask 없이 소스로 확인한다."""

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

    def test_result_page_links_to_step6(self) -> None:
        result = (ROOT / "frontend" / "templates" / "result.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("url_for('preview_3d')", result)

    def test_step6_template_autostarts_the_viewer(self) -> None:
        page = (ROOT / "frontend" / "templates" / "preview_3d.html").read_text(
            encoding="utf-8"
        )
        self.assertIn("STEP 6", page)
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
    """타입 목록이 layout(파이썬) ↔ 프리셋 ↔ 3D 렌더러(JS) 3곳에 흩어져 있다.
    한 곳만 고치고 나머지를 잊으면 가구가 조용히 기본 박스로 떨어지므로 여기서 묶어둔다."""

    def _layout_vocabulary(self) -> set[str]:
        import re

        source = (ROOT / "mood_pipeline" / "rule_based_svg.py").read_text(encoding="utf-8")
        block = re.search(r"LAYOUT_OBJECT_TYPES = \[(.*?)\]", source, re.S)
        self.assertIsNotNone(block, "LAYOUT_OBJECT_TYPES 를 찾지 못했습니다")
        return set(re.findall(r'"(\w+)"', block.group(1)))

    def _js_builders(self) -> set[str]:
        import re

        source = (
            ROOT / "frontend" / "static" / "js" / "floorplan_3d.js"
        ).read_text(encoding="utf-8")
        self.assertIn("const BUILDERS = {", source)
        body = source.split("const BUILDERS = {", 1)[1]
        return set(re.findall(r"^  ([a-z_]+)\(obj\)", body, re.M))

    def test_presets_cover_the_whole_layout_vocabulary(self) -> None:
        missing = self._layout_vocabulary() - set(TYPE_PRESETS)
        self.assertEqual(set(), missing, f"프리셋 누락: {sorted(missing)}")

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

    def test_purchasable_types_render_at_a_usable_size(self) -> None:
        room = {"width_m": 4.0, "depth_m": 4.0, "ceiling_m": 2.4}
        for item_type in sorted(self._purchasable_types()):
            layout = {
                "room": {"aspect_ratio": 1.0},
                # create_modified_floorplan 이 넣는 형태(크기 0)
                "objects": [{
                    "type": item_type, "x": 0.5, "y": 0.5, "w": 0.0, "h": 0.0,
                    "wall": "none", "source": "selected_product",
                }],
            }
            objects = build_scene(layout, room)["objects"]
            self.assertEqual(1, len(objects), item_type)
            obj = objects[0]
            self.assertEqual(item_type, obj["type"], f"{item_type} 이 다른 타입으로 변환됨")
            self.assertGreater(obj["w_m"], 0.2, f"{item_type} 이 찌그러집니다")
            self.assertGreater(obj["d_m"], 0.2, f"{item_type} 이 찌그러집니다")

    def test_no_furniture_pokes_through_the_ceiling(self) -> None:
        ceiling = 2.4
        layout = {
            "room": {"aspect_ratio": 1.0},
            "objects": [{"type": t, "x": 0.5, "y": 0.5, "w": 0.0, "h": 0.0}
                        for t in TYPE_PRESETS],
        }
        room = {"width_m": 4.0, "depth_m": 4.0, "ceiling_m": ceiling}
        for obj in build_scene(layout, room)["objects"]:
            top = obj["base_m"] + obj["height_m"]
            self.assertLessEqual(top, ceiling, f"{obj['type']} 이 천장을 뚫습니다 ({top}m)")

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

    def test_js_builders_match_presets_exactly(self) -> None:
        builders = self._js_builders()
        presets = set(TYPE_PRESETS)
        self.assertEqual(
            set(), presets - builders, f"JS 빌더 누락: {sorted(presets - builders)}"
        )
        self.assertEqual(
            set(), builders - presets, f"도달 불가 JS 빌더: {sorted(builders - presets)}"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
