"""공간 분석 → 2D/3D 파이프라인 평가.

특허 실험(기존 방식 vs 개선 방식)의 수치를 만드는 스크립트다. 두 가지로 돈다.

  cached : frontend/static/generated 에 이미 만들어진 결과물을 다시 측정한다.
           API를 부르지 않는다. 2D↔3D 오차, 충돌률, 벽 관통률, 동선만 나온다.
  run    : 사진 폴더를 실제 파이프라인에 통과시킨다. 위 지표에 더해
           API 호출 수·토큰·처리시간, 그리고 정답 파일이 있으면 정확도까지 낸다.
           --replay 를 주면 저장된 응답만 써서 API 호출이 0회다.

사용 예
  venv\\Scripts\\python.exe scripts/eval_pipeline.py cached --out docs/metrics/baseline_cached.json
  venv\\Scripts\\python.exe scripts/eval_pipeline.py run tests/fixtures/rooms --record --out docs/metrics/baseline.json
  venv\\Scripts\\python.exe scripts/eval_pipeline.py run tests/fixtures/rooms --replay

run 모드의 사진 폴더 구조 (docs/metrics/README.md 참고)
  rooms/<이름>/photo.jpg            필수
  rooms/<이름>/ground_truth.json    선택 — 있으면 정확도 측정
  rooms/<이름>/room.json            선택 — 사용자가 입력한 방 치수 흉내
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evaluation import metrics, svg_geometry  # noqa: E402
from model2.floorplan_3d import build_scene  # noqa: E402

GENERATED = ROOT / "frontend" / "static" / "generated"
WALL_GAP_M = 0.04  # floorplan_3d.js 의 wall_mounted 배치와 같은 값


def rendered_objects(scene3d: dict[str, Any]) -> list[dict[str, Any]]:
    """three.js가 실제로 그리는 위치로 옮긴다.

    floorplan_3d.js는 벽걸이 객체의 좌표를 무시하고 벽면에 붙인다. 지표가
    화면과 같아야 하므로 그 규칙을 여기서 똑같이 재현한다.
    """
    room = scene3d["room"]
    w, d = float(room["width_m"]), float(room["depth_m"])
    out = []
    for obj in scene3d.get("objects") or []:
        item = dict(obj)
        if item.get("wall_mounted"):
            wall = item.get("wall")
            if wall == "top":
                item["cy"] = WALL_GAP_M
            elif wall == "bottom":
                item["cy"] = d - WALL_GAP_M
            elif wall == "left":
                item["cx"] = WALL_GAP_M
            elif wall == "right":
                item["cx"] = w - WALL_GAP_M
        out.append(item)
    return out


def recover_ids(objects_3d: list[dict[str, Any]], layout: dict[str, Any]) -> tuple[list[dict[str, Any]], float | None]:
    """3D 객체 id를 scene id로 되돌린다.

    기존 build_scene은 resolve_placement를 거치며 scene_id를 잃고 "{type}_{idx}"
    형태의 id를 만든다. 측정은 같은 가구끼리 비교해야 하므로 idx(layout 순번)로
    scene_id를 복원한다. 복원 없이 맞았던 비율은 따로 돌려줘 결함 자체도 기록한다.
    """
    layout_objects = layout.get("objects") or []
    scene_ids = {str(o.get("scene_id")) for o in layout_objects if o.get("scene_id")}
    preserved = sum(1 for o in objects_3d if str(o.get("id")) in scene_ids)
    out = []
    for obj in objects_3d:
        item = dict(obj)
        object_id = str(item.get("id"))
        if object_id not in scene_ids:
            suffix = object_id.rsplit("_", 1)[-1]
            if suffix.isdigit() and int(suffix) < len(layout_objects):
                recovered = layout_objects[int(suffix)].get("scene_id")
                if recovered:
                    item["id"] = str(recovered)
                    item["id_recovered"] = True
        out.append(item)
    ratio = round(preserved / len(objects_3d), 4) if objects_3d else None
    return out, ratio


def _plan_from_layout(layout: dict[str, Any], room_override: dict[str, Any] | None) -> dict[str, Any] | None:
    if room_override:
        return room_override
    room = layout.get("room") or {}
    if room.get("width_m") and room.get("depth_m"):
        return {"width_m": room["width_m"], "depth_m": room["depth_m"]}
    return None


def evaluate_outputs(
    scene: dict[str, Any],
    layout: dict[str, Any],
    svg_path: Path,
    *,
    room_override: dict[str, Any] | None = None,
    truth: dict[str, Any] | None = None,
    clearance: float = metrics.DEFAULT_CLEARANCE_M,
) -> dict[str, Any]:
    scene3d = build_scene(layout, _plan_from_layout(layout, room_override))
    room = scene3d["room"]
    objects_3d, id_preserved_ratio = recover_ids(rendered_objects(scene3d), layout)

    ids = [str(o.get("id")) for o in scene.get("objects") or [] if o.get("id")]
    root = svg_geometry.load(svg_path)
    floor = svg_geometry.floor_box(root, ids)
    boxes_m: dict[str, tuple[float, float, float, float]] = {}
    if floor is not None:
        fx0, fy0, fx1, fy1 = floor
        sx = float(room["width_m"]) / (fx1 - fx0)
        sy = float(room["depth_m"]) / (fy1 - fy0)
        for object_id, (x0, y0, x1, y1) in svg_geometry.group_boxes(root, ids).items():
            boxes_m[object_id] = ((x0 - fx0) * sx, (y0 - fy0) * sy, (x1 - fx0) * sx, (y1 - fy0) * sy)

    # 2D 평면도 자체의 충돌률. SVG 외접 박스를 회전 0인 바닥면으로 본다.
    type_by_id = {str(o.get("id")): o for o in objects_3d}
    objects_2d = []
    for object_id, (x0, y0, x1, y1) in boxes_m.items():
        source = type_by_id.get(object_id, {})
        objects_2d.append(
            {
                "id": object_id,
                "type": source.get("type", "unknown"),
                "wall_mounted": source.get("wall_mounted", False),
                "cx": (x0 + x1) / 2,
                "cy": (y0 + y1) / 2,
                "w_m": x1 - x0,
                "d_m": y1 - y0,
                "rotation_deg": 0.0,
            }
        )

    result: dict[str, Any] = {
        "room": {k: room[k] for k in ("width_m", "depth_m", "estimated")},
        "placement": scene3d.get("placement"),
        # 복원 없이 3D id가 scene id와 일치한 비율. 낮으면 2D↔3D 편집 연동이 불가능하다
        "id_preserved_in_3d": id_preserved_ratio,
        "object_count": len(objects_3d),
        "svg_floor_found": floor is not None,
        "sync_2d_3d": metrics.sync_metrics(boxes_m, objects_3d, ids),
        "collision_3d": metrics.collision_metrics(objects_3d),
        "collision_2d": metrics.collision_metrics(objects_2d),
        "wall_3d": metrics.wall_metrics(objects_3d, room),
        "walkway_3d": metrics.walkway_metrics(objects_3d, room, clearance=clearance),
    }
    if truth:
        category_by_id = {str(o.get("id")): o.get("category") for o in scene.get("objects") or []}
        predicted = [dict(o, category=category_by_id.get(str(o.get("id")), o.get("type"))) for o in objects_3d]
        result["accuracy"] = metrics.accuracy_metrics(predicted, room, truth)
    return result


# ---------------------------------------------------------------- cached 모드

def _scene_digest(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def cached_runs(directory: Path, dedupe: bool = True) -> list[str]:
    """측정할 캐시 이름 목록.

    같은 사진을 여러 번 올리면 분석 JSON이 그대로 복사된다(캐시 재사용). 그대로
    세면 한 방이 수십 번 집계돼 평균을 좌우하므로, 분석 JSON 내용이 같은 것은
    하나만 남긴다.
    """
    stems = []
    seen: set[str] = set()
    for scene_path in sorted(directory.glob("*_model2_scene.json")):
        stem = scene_path.name[: -len("_model2_scene.json")]
        if (directory / f"{stem}_model2_layout.json").exists() and (
            directory / f"{stem}_model2_floorplan.svg"
        ).exists():
            if dedupe:
                digest = _scene_digest(scene_path)
                if digest in seen:
                    continue
                seen.add(digest)
            stems.append(stem)
    return stems


def run_cached(directory: Path, clearance: float, rebuild: bool = False, dedupe: bool = True) -> list[dict[str, Any]]:
    """캐시된 결과물을 측정한다.

    rebuild=True면 같은 분석 JSON에서 현재 코드로 layout과 SVG를 다시 만든다.
    Gemini 분석 결과가 같으므로 기존 방식과 개선 방식의 차이만 비교된다.
    """
    rows = []
    rebuild_dir = ROOT / "output" / "eval" / "rebuild"
    for stem in cached_runs(directory, dedupe=dedupe):
        entry: dict[str, Any] = {"name": stem}
        try:
            scene = json.loads((directory / f"{stem}_model2_scene.json").read_text(encoding="utf-8"))
            layout = json.loads((directory / f"{stem}_model2_layout.json").read_text(encoding="utf-8"))
            svg_path = directory / f"{stem}_model2_floorplan.svg"
            if rebuild:
                from model2 import scene_graph
                from model2.scene_render_2d import render_svg

                old_room = layout.get("room") or {}
                layout = scene_graph.from_analysis(
                    scene,
                    width_m=old_room.get("width_m"),
                    depth_m=old_room.get("depth_m"),
                )
                rebuild_dir.mkdir(parents=True, exist_ok=True)
                svg_path = rebuild_dir / f"{stem}.svg"
                svg_path.write_text(render_svg(layout), encoding="utf-8")
                (rebuild_dir / f"{stem}.json").write_text(
                    json.dumps(layout, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                entry["scale_source"] = layout["room"].get("scale_source")
            entry.update(evaluate_outputs(scene, layout, svg_path, clearance=clearance))
        except Exception as exc:  # 한 건이 깨져도 나머지는 측정한다
            entry["error"] = f"{type(exc).__name__}: {exc}"
        rows.append(entry)
    return rows


# ---------------------------------------------------------------- run 모드

def _room_dirs(directory: Path) -> list[Path]:
    dirs = []
    for child in sorted(directory.iterdir()):
        if child.is_dir() and any((child / f"photo{ext}").exists() for ext in (".jpg", ".jpeg", ".png", ".webp")):
            dirs.append(child)
    return dirs


def _photo(room_dir: Path) -> Path:
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = room_dir / f"photo{ext}"
        if candidate.exists():
            return candidate
    raise FileNotFoundError(room_dir)


def _read_json(path: Path) -> dict[str, Any] | None:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _api_stats(log_path: Path, run_tag: str) -> dict[str, Any]:
    calls = []
    if log_path.exists():
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("run") == run_tag:
                calls.append(record)
    by_caller: dict[str, int] = {}
    for call in calls:
        by_caller[call.get("caller", "?")] = by_caller.get(call.get("caller", "?"), 0) + 1

    def total(key: str) -> int:
        return sum(int(c.get(key) or 0) for c in calls)

    return {
        "calls": len(calls),
        "failed_calls": sum(1 for c in calls if not c.get("ok")),
        "replayed_calls": sum(1 for c in calls if c.get("replayed")),
        "by_caller": by_caller,
        "prompt_tokens": total("prompt_tokens"),
        "output_tokens": total("output_tokens"),
        "thoughts_tokens": total("thoughts_tokens"),
        "total_tokens": total("total_tokens"),
        # 재생 모드에서도 녹화 당시 걸린 시간을 쓴다
        "api_latency_s": round(sum(float(c.get("latency_s") or 0) for c in calls), 3),
        "models": sorted({str(c.get("model")) for c in calls}),
    }


def run_live(directory: Path, clearance: float, mode: str, tag: str) -> list[dict[str, Any]]:
    os.environ["GEMINI_REPLAY_MODE"] = mode
    if mode == "replay":
        # 재생은 네트워크를 쓰지 않지만 _client()가 키 존재를 검사한다
        os.environ.setdefault("GEMINI_API_KEY", "replay-only")

    from model2 import gemini_telemetry
    from model2.web_floorplan import generate_floorplan_for_web

    log_path = gemini_telemetry._log_path() or gemini_telemetry.DEFAULT_CALL_LOG
    out_root = ROOT / "output" / "eval" / tag
    rows = []
    for room_dir in _room_dirs(directory):
        name = room_dir.name
        run_tag = f"{tag}:{name}"
        room_override = _read_json(room_dir / "room.json")
        truth = _read_json(room_dir / "ground_truth.json")
        entry: dict[str, Any] = {"name": name, "has_ground_truth": truth is not None}
        output_dir = out_root / name
        started = time.perf_counter()
        try:
            with gemini_telemetry.run_context(run_tag):
                result = generate_floorplan_for_web(
                    _photo(room_dir),
                    output_dir,
                    skip_existing=False,
                    room_width=(room_override or {}).get("width_m"),
                    room_depth=(room_override or {}).get("depth_m"),
                )
            entry["pipeline_s"] = round(time.perf_counter() - started, 3)
            entry["models"] = {"layout": result.get("layout_model"), "svg": result.get("svg_model")}
            stem = _photo(room_dir).stem
            scene = json.loads((output_dir / f"{stem}_model2_scene.json").read_text(encoding="utf-8"))
            layout = json.loads(Path(result["layout_file"]).read_text(encoding="utf-8"))
            entry.update(
                evaluate_outputs(
                    scene,
                    layout,
                    Path(result["svg_path"]),
                    room_override=room_override,
                    truth=truth,
                    clearance=clearance,
                )
            )
        except Exception as exc:
            entry["pipeline_s"] = round(time.perf_counter() - started, 3)
            entry["error"] = f"{type(exc).__name__}: {exc}"
        entry["api"] = _api_stats(log_path, run_tag)
        rows.append(entry)
    return rows


# ---------------------------------------------------------------- 요약

def _collect(rows: list[dict[str, Any]], *path: str) -> list[float]:
    values = []
    for row in rows:
        node: Any = row
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            values.append(float(node))
    return values


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if "error" not in r]

    def stat(*path: str) -> dict[str, Any] | None:
        values = _collect(ok, *path)
        if not values:
            return None
        return {
            "mean": round(statistics.fmean(values), 4),
            "median": round(statistics.median(values), 4),
            "max": round(max(values), 4),
            "n": len(values),
        }

    sync_rows = [o for r in ok for o in (r.get("sync_2d_3d") or {}).get("objects", [])]
    summary = {
        "rooms": len(rows),
        "rooms_ok": len(ok),
        "rooms_failed": len(rows) - len(ok),
        "sync_center_error_m": stat("sync_2d_3d", "mean_center_error_m"),
        "sync_iou": stat("sync_2d_3d", "mean_iou"),
        # 객체 단위로 펼친 2D↔3D 오차 (방마다 객체 수가 달라 따로 낸다)
        "sync_objects": {
            "n": len(sync_rows),
            "mean_center_error_m": metrics._mean([o["center_error_m"] for o in sync_rows]),
            "over_10cm_ratio": metrics._ratio(sum(1 for o in sync_rows if o["center_error_m"] > 0.10), len(sync_rows)),
        },
        "id_preserved_in_3d": stat("id_preserved_in_3d"),
        "unknown_type_in_3d_ratio": metrics._ratio(
            sum(len((r.get("sync_2d_3d") or {}).get("unknown_type_in_3d", [])) for r in ok),
            sum(int(r.get("object_count") or 0) for r in ok),
        ),
        "object_collision_rate_3d": stat("collision_3d", "object_collision_rate"),
        "object_collision_rate_2d": stat("collision_2d", "object_collision_rate"),
        "rooms_with_collision_3d": metrics._ratio(
            sum(1 for r in ok if (r.get("collision_3d") or {}).get("colliding_pairs")), len(ok)
        ),
        "wall_penetration_rate_3d": stat("wall_3d", "wall_penetration_rate"),
        "reachable_floor_ratio": stat("walkway_3d", "reachable_floor_ratio"),
        "furniture_access_rate": stat("walkway_3d", "furniture_access_rate"),
        "rooms_door_blocked": metrics._ratio(
            sum(1 for r in ok if (r.get("walkway_3d") or {}).get("doors_blocked")), len(ok)
        ),
        "rooms_without_door": metrics._ratio(
            sum(1 for r in ok if (r.get("walkway_3d") or {}).get("door_count") == 0), len(ok)
        ),
        "room_size_estimated_ratio": metrics._ratio(
            sum(1 for r in ok if (r.get("room") or {}).get("estimated")), len(ok)
        ),
    }
    if any("api" in r for r in rows):
        summary["api_calls_per_room"] = stat("api", "calls")
        summary["tokens_per_room"] = stat("api", "total_tokens")
        summary["api_latency_s"] = stat("api", "api_latency_s")
        summary["pipeline_s"] = stat("pipeline_s")
    if any(r.get("accuracy") for r in ok):
        summary["accuracy"] = {
            key: stat("accuracy", key)
            for key in ("precision", "recall", "f1", "mean_center_error_m", "mean_width_error_m", "mean_depth_error_m", "mean_rotation_error_deg")
        }
    return summary


def _git_commit() -> str | None:
    try:
        return subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, text=True).strip()
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    cached = sub.add_parser("cached", help="이미 생성된 결과물을 측정 (API 0회)")
    cached.add_argument("--dir", type=Path, default=GENERATED)
    cached.add_argument("--rebuild", action="store_true", help="같은 분석 JSON으로 현재 코드의 layout·SVG를 다시 만들어 측정")
    cached.add_argument("--keep-duplicates", action="store_true", help="같은 분석 JSON(같은 사진 재업로드)도 따로 센다")
    resum = sub.add_parser("resummarize", help="기존 결과 JSON을 중복 제거해 다시 요약")
    resum.add_argument("report", type=Path)
    resum.add_argument("--dir", type=Path, default=GENERATED)
    resum.add_argument("--only-names-from", type=Path, default=None, help="이 결과 JSON에 있는 방만 남긴다(같은 방 집합으로 비교할 때)")
    live = sub.add_parser("run", help="사진 폴더를 파이프라인에 통과시켜 측정")
    live.add_argument("rooms", type=Path)
    group = live.add_mutually_exclusive_group()
    group.add_argument("--record", action="store_true", help="실제 호출 + 응답 저장 (유료)")
    group.add_argument("--replay", action="store_true", help="저장된 응답만 사용 (API 0회)")
    live.add_argument("--tag", default=None, help="실행 이름 (기본: 자동)")
    resum.add_argument("--out", type=Path, default=None)
    for p in (cached, live):
        p.add_argument("--out", type=Path, default=None, help="결과 JSON 경로")
        p.add_argument("--clearance", type=float, default=metrics.DEFAULT_CLEARANCE_M)
        p.add_argument("--label", default="", help="결과에 남길 설명 (예: baseline)")
    args = parser.parse_args(argv)

    if args.command == "resummarize":
        report = json.loads(args.report.read_text(encoding="utf-8"))
        keep = set(cached_runs(args.dir, dedupe=True))
        if args.only_names_from:
            other = json.loads(args.only_names_from.read_text(encoding="utf-8"))
            keep &= {r["name"] for r in other["rooms"]}
        report["rooms"] = [r for r in report["rooms"] if r["name"] in keep]
        report["summary"] = summarize(report["rooms"])
        report["deduplicated"] = True
        text = json.dumps(report, ensure_ascii=False, indent=2)
        out = args.out or args.report
        out.write_text(text, encoding="utf-8")
        print(f"저장: {out}")
        print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
        return 0

    if args.command == "cached":
        rows = run_cached(args.dir, args.clearance, rebuild=args.rebuild, dedupe=not args.keep_duplicates)
        mode = "cached_rebuild" if args.rebuild else "cached"
    else:
        mode = "replay" if args.replay else "record" if args.record else "off"
        tag = args.tag or f"eval-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:4]}"
        rows = run_live(args.rooms, args.clearance, mode, tag)

    report = {
        "label": args.label,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "git_commit": _git_commit(),
        "mode": mode,
        "config": {
            "clearance_m": args.clearance,
            "grid_m": metrics.GRID_M,
            "collision_min_area_m2": metrics.COLLISION_MIN_AREA_M2,
            "collision_min_fraction": metrics.COLLISION_MIN_FRACTION,
        },
        "summary": summarize(rows),
        "rooms": rows,
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
        print(f"저장: {args.out}")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
