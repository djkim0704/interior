from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation import metrics  # noqa: E402
from model2 import placement_solver, scene_graph  # noqa: E402


def _graph(objects: list[dict], width: float = 4.0, depth: float = 4.0) -> dict:
    graph = {
        "schema": scene_graph.SCHEMA,
        "room": {"width_m": width, "depth_m": depth, "estimated": False, "scale_source": "user"},
        "objects": [],
    }
    for index, obj in enumerate(objects):
        graph["objects"].append(
            {
                "category": obj["type"],
                "label": obj["type"],
                "rotation_deg": 0.0,
                "wall": "none",
                "confidence": 0.8,
                "source": "ai",
                "source_index": index,
                **obj,
            }
        )
    return graph


def _by_id(graph: dict) -> dict:
    return {o["id"]: o for o in graph["objects"]}


class CollisionTests(unittest.TestCase):
    def test_overlapping_furniture_is_separated_with_small_move(self) -> None:
        graph = _graph(
            [
                {"id": "bed", "type": "bed", "cx": 1.0, "cy": 1.2, "w_m": 1.5, "d_m": 2.0, "wall": "left", "rotation_deg": 270.0},
                {"id": "shelf", "type": "shelf", "cx": 1.6, "cy": 1.2, "w_m": 0.8, "d_m": 0.4},
            ]
        )
        placement_solver.solve(graph, walkway=False)
        objects = [dict(o, wall_mounted=False) for o in graph["objects"]]
        self.assertEqual(metrics.collision_metrics(objects)["colliding_pairs"], 0)
        moved = next(a for a in graph["solver_adjustments"] if a["object_id"] == "shelf")
        self.assertEqual(moved["reason"], "collision")

    def test_locked_object_never_moves(self) -> None:
        graph = _graph(
            [
                {"id": "desk", "type": "desk", "cx": 2.0, "cy": 2.0, "w_m": 1.2, "d_m": 0.6, "source": "user"},
                {"id": "cabinet", "type": "cabinet", "cx": 2.2, "cy": 2.1, "w_m": 0.8, "d_m": 0.45},
            ]
        )
        placement_solver.solve(graph, walkway=False)
        desk = _by_id(graph)["desk"]
        self.assertEqual((desk["cx"], desk["cy"]), (2.0, 2.0))
        self.assertNotEqual((_by_id(graph)["cabinet"]["cx"], _by_id(graph)["cabinet"]["cy"]), (2.2, 2.1))

    def test_rug_and_tucked_chair_are_left_alone(self) -> None:
        graph = _graph(
            [
                {"id": "desk", "type": "desk", "cx": 2.0, "cy": 0.4, "w_m": 1.2, "d_m": 0.6, "wall": "top"},
                {"id": "chair", "type": "chair", "cx": 2.0, "cy": 0.7, "w_m": 0.5, "d_m": 0.5},
                {"id": "rug", "type": "rug", "cx": 2.0, "cy": 1.0, "w_m": 2.0, "d_m": 1.5},
            ]
        )
        placement_solver.solve(graph, walkway=False)
        self.assertEqual(graph["solver_adjustments"], [])

    def test_furniture_pushed_back_inside_room(self) -> None:
        graph = _graph([{"id": "sofa", "type": "sofa", "cx": 3.8, "cy": 2.0, "w_m": 1.8, "d_m": 0.9, "wall": "right", "rotation_deg": 90.0}])
        placement_solver.solve(graph, walkway=False)
        sofa = _by_id(graph)["sofa"]
        # 90도 회전이라 평면도 가로는 깊이 0.9m. 오른쪽 벽 안쪽에 딱 붙는다
        self.assertAlmostEqual(sofa["cx"], 4.0 - 0.45, places=3)

    def test_wall_anchored_furniture_slides_along_its_wall(self) -> None:
        graph = _graph(
            [
                {"id": "wardrobe", "type": "wardrobe", "cx": 1.0, "cy": 0.3, "w_m": 1.2, "d_m": 0.6, "wall": "top"},
                {"id": "desk", "type": "desk", "cx": 1.3, "cy": 0.3, "w_m": 1.0, "d_m": 0.6, "wall": "top"},
            ]
        )
        placement_solver.solve(graph, walkway=False)
        desk = _by_id(graph)["desk"]
        self.assertAlmostEqual(desk["cy"], 0.3, places=3)
        self.assertGreaterEqual(desk["cx"], 1.0 + 0.6 + 0.5 - 0.05)

    def test_door_swing_area_is_kept_clear(self) -> None:
        graph = _graph(
            [
                {"id": "door", "type": "door", "cx": 2.0, "cy": 0.04, "w_m": 0.9, "d_m": 0.08, "wall": "top"},
                {"id": "shelf", "type": "shelf", "cx": 2.0, "cy": 0.3, "w_m": 0.8, "d_m": 0.4},
            ]
        )
        placement_solver.solve(graph, walkway=False)
        shelf = placement_solver._corners(_by_id(graph)["shelf"])
        zone = placement_solver._door_keepouts(graph["objects"], graph["room"])[0]
        self.assertLessEqual(placement_solver.overlap(shelf, zone), placement_solver.OVERLAP_TOLERANCE_M2)

    def test_shrinks_a_little_instead_of_moving_far(self) -> None:
        # 좁은 방에서 가까운 빈자리가 없으면 조금 줄여서 원래 자리 근처에 둔다
        graph = _graph(
            [
                {"id": "bed", "type": "bed", "cx": 0.8, "cy": 1.0, "w_m": 1.6, "d_m": 2.0, "wall": "top"},
                {"id": "wardrobe", "type": "wardrobe", "cx": 2.2, "cy": 0.3, "w_m": 1.0, "d_m": 0.6, "wall": "top"},
                {"id": "desk", "type": "desk", "cx": 1.75, "cy": 1.3, "w_m": 0.6, "d_m": 0.5},
            ],
            width=2.5,
            depth=2.2,
        )
        placement_solver.solve(graph, walkway=False)
        resized = [a for a in graph["solver_adjustments"] if a["reason"] == "collision_resize"]
        self.assertTrue(resized)
        self.assertGreaterEqual(min(a["scale"] for a in resized), placement_solver.SHRINK_STEPS[-1])
        for adjustment in graph["solver_adjustments"]:
            moved = ((adjustment["to"][0] - adjustment["from"][0]) ** 2 + (adjustment["to"][1] - adjustment["from"][1]) ** 2) ** 0.5
            self.assertLessEqual(moved, placement_solver.NEAR_SHIFT_M + 1e-6)
        objects = [dict(o, wall_mounted=False) for o in graph["objects"]]
        self.assertEqual(metrics.collision_metrics(objects)["colliding_pairs"], 0)


class WalkwayTests(unittest.TestCase):
    def test_moves_blocking_furniture_to_open_access(self) -> None:
        # 문 앞을 가로지르는 선반 때문에 방 안쪽 침대에 닿지 못하는 상황
        graph = _graph(
            [
                {"id": "door", "type": "door", "cx": 0.6, "cy": 0.04, "w_m": 0.9, "d_m": 0.08, "wall": "top"},
                {"id": "bed", "type": "bed", "cx": 2.9, "cy": 3.0, "w_m": 1.4, "d_m": 2.0, "wall": "bottom", "rotation_deg": 180.0},
                # 양쪽에 40cm씩만 남아 폭 60cm 통로가 안 나온다. 오른쪽으로 40cm 밀면 왼쪽이 열린다
                {"id": "shelf", "type": "shelf", "cx": 2.0, "cy": 1.45, "w_m": 3.2, "d_m": 0.35, "source": "ai"},
            ],
            width=4.0,
            depth=4.0,
        )
        before = placement_solver.walkway_state(graph["objects"], graph["room"])
        self.assertIn("bed", before["inaccessible"])
        placement_solver.solve(graph)
        self.assertEqual(graph["walkway"]["inaccessible"], [])
        self.assertTrue(any(a["reason"] == "walkway" for a in graph["solver_adjustments"]))


class IntegrationTests(unittest.TestCase):
    def test_new_product_is_placed_without_moving_existing_furniture(self) -> None:
        graph = _graph(
            [
                {"id": "bed", "type": "bed", "cx": 1.0, "cy": 1.2, "w_m": 1.5, "d_m": 2.0, "wall": "left", "rotation_deg": 270.0},
            ]
        )
        graph = scene_graph.sync_legacy(graph)
        bed_before = (graph["objects"][0]["cx"], graph["objects"][0]["cy"])
        # 기존 라우트처럼 legacy 필드만 채운 상품을 침대 위 고정 슬롯에 끼운다
        graph["objects"].append(
            {"type": "shelf", "label": "선반", "x": 0.24, "y": 0.24, "w": 0.0, "h": 0.0,
             "wall": "none", "source": "selected_product", "product_marker": 1}
        )
        fixed = scene_graph.ensure(graph)
        bed, product = fixed["objects"]
        self.assertEqual((bed["cx"], bed["cy"]), bed_before)
        self.assertEqual(
            placement_solver.overlap(placement_solver._corners(bed), placement_solver._corners(product)),
            0.0,
        )


if __name__ == "__main__":
    unittest.main()
