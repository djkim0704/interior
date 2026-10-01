# 평가 지표

특허 실험(기존 방식 vs 개선 방식)에 쓰는 수치의 정의와 측정 방법이다.
측정 코드는 [evaluation/metrics.py](../../evaluation/metrics.py)와
[scripts/eval_pipeline.py](../../scripts/eval_pipeline.py)에 있다.
**지표 정의를 바꾸면 기준값도 다시 측정해야 비교가 성립한다.**

## 실행

```bash
# 이미 생성된 결과물(frontend/static/generated)을 측정한다. API 호출 0회.
venv\Scripts\python.exe scripts/eval_pipeline.py cached --out docs/metrics/<이름>.json

# 같은 분석 JSON으로 현재 코드의 layout·SVG를 다시 만들어 측정한다. API 호출 0회.
# Gemini 분석 결과가 같으므로 기존 방식과 현재 방식의 차이만 비교된다.
venv\Scripts\python.exe scripts/eval_pipeline.py cached --rebuild --out docs/metrics/<이름>.json

# 사진 폴더를 실제 파이프라인에 통과시킨다.
#   --record : 실제로 호출하고 응답을 저장한다 (유료)
#   --replay : 저장된 응답만 쓴다. API 호출 0회. 응답이 없으면 호출하지 않고 실패한다
venv\Scripts\python.exe scripts/eval_pipeline.py run tests/fixtures/rooms --record --out docs/metrics/<이름>.json
venv\Scripts\python.exe scripts/eval_pipeline.py run tests/fixtures/rooms --replay
```

`run` 모드의 폴더 구조는 다음과 같다.

```
tests/fixtures/rooms/<방 이름>/
  photo.jpg            필수
  ground_truth.json    선택. 있으면 정확도를 측정한다
  room.json            선택. 사용자가 입력한 방 치수를 흉내 낸다. {"width_m": 3.2, "depth_m": 4.0}
```

## Gemini 호출 기록 (`model2/gemini_telemetry.py`)

모든 Gemini 클라이언트가 이 모듈을 거친다. 호출할 때마다
`output/metrics/gemini_calls.jsonl`에 한 줄씩 기록한다.

- 기록 항목: 모델, 호출 위치, 소요 시간, 토큰(입력·출력·thinking), 종료 사유
- 환경변수:
  - `GEMINI_REPLAY_MODE`: `off` / `record` / `replay`
  - `GEMINI_REPLAY_DIR`: 저장한 응답을 둘 위치
  - `GEMINI_CALL_LOG`: 기록 파일 위치. `off`로 두면 기록하지 않는다
- 집계 단위: `generate_content` 호출 한 번이 1회다. SDK 내부 재시도(`HttpRetryOptions`)로 다시 보낸 HTTP 요청은 바깥에서 보이지 않으므로 따로 세지 않는다.

## 좌표 규약

모든 지표는 미터 단위다. 원점은 방의 좌상단이고, x는 가로, y는 깊이 방향이다.

- **3D:** three.js가 **실제로 그리는 모양**을 기준으로 한다.
  - `w_m`은 x축, `d_m`은 z축으로 만든 뒤 `rotation_deg`만큼 회전한다.
  - 벽에 거는 객체는 좌표를 무시하고 벽면에서 0.04m 떨어진 곳에 붙인다.
- **2D:** 사용자가 보는 `*_model2_floorplan.svg`에서 `<g id>` 그룹이 실제로 그려진 외접 박스를 쓴다.
  - transform을 끝까지 누적해 계산한다.
  - 바닥 사각형을 방 크기에 맞춰 미터로 환산한다.
  - 곡선은 제어점 기준으로 근사하므로 박스가 약간 크게 잡힌다. 그래서 오차는 실제보다 보수적으로(크게) 나온다.

## 지표 정의

| 지표 | 정의 |
|---|---|
| `sync_2d_3d.mean_center_error_m` | 같은 id의 가구에 대해, 2D 외접 박스 중심과 3D 바닥면 외접 박스 중심 사이의 거리를 평균한 값 |
| `sync_2d_3d.mean_iou` | 위 두 박스의 IoU 평균 |
| `id_preserved_in_3d` | 3D 객체 id가 scene id와 그대로 일치한 비율. 낮으면 2D와 3D 사이의 편집 연동이 불가능하다 |
| `unknown_type_in_3d_ratio` | 3D에서 전용 형태 없이 상자(`unknown`)로 그려진 객체의 비율 |
| `object_collision_rate` | 다른 가구와 겹친 가구의 비율. "겹침"은 0.01m²를 넘고 작은 쪽 면적의 5%를 넘을 때로 본다. 다음은 제외한다: 러그, 문·창, 벽걸이 객체, 책상·식탁 아래로 들어간 의자 |
| `wall_penetration_rate` | 바닥면이 방 밖으로 0.005m² 넘게 나간 가구의 비율 |
| `reachable_floor_ratio` | 가구가 없는 바닥 중, 문에서 폭 0.6m 통로로 걸어서 닿는 바닥의 비율. 5cm 격자로 계산한다 |
| `furniture_access_rate` | 앞에 사람이 설 수 있는 가구의 비율. 도달 가능한 칸이 가구 바닥면에서 0.4m 이내에 있으면 설 수 있다고 본다 |
| `doors_blocked` | 문 바로 안쪽 칸이 막힌 문의 수 |
| `rooms_without_door` | 문이 검출되지 않은 방의 비율. 이런 방은 가장 큰 빈 영역을 시작점으로 동선을 잰다 |
| `accuracy.*` | 정답과 비교한 정확도. 같은 카테고리이고 중심 간 거리가 1m 이내인 객체끼리 헝가리안 매칭한다. 그 결과로 precision, recall, F1과 위치·크기·회전 오차를 낸다 |
| `api.*` | 방 1개당 호출 수, 실패 수, 토큰, API 소요 시간 |

## 정답 라벨 형식 (`ground_truth.json`)

```json
{
  "room": {"width_m": 3.2, "depth_m": 4.1},
  "objects": [
    {"category": "bed",  "cx": 1.05, "cy": 1.10, "w_m": 1.50, "d_m": 2.00, "rotation_deg": 0},
    {"category": "desk", "cx": 2.80, "cy": 3.60, "w_m": 1.20, "d_m": 0.60, "rotation_deg": 180},
    {"category": "door", "cx": 3.20, "cy": 0.60, "w_m": 0.90, "d_m": 0.10, "rotation_deg": 90}
  ]
}
```

- 좌표 규약은 위와 같다. `rotation_deg`는 위에서 본 시계방향 각도이고, 0은 등을 위쪽 벽에 붙인 상태다.
- 치수는 줄자로 잰 실측값을 쓴다. 측정할 수 없는 가구는 빼는 편이 낫다(빼면 recall만 영향을 받는다).

## 비교 결과 (기존 → 1단계 → 2단계)

같은 Gemini 분석 JSON을 각 단계의 코드로 처리해 비교한다. 분석 입력이 같으므로
차이는 모두 처리 구조에서 나온 것이다.

- **방 13개, 중복 제거.** 캐시 결과물 75건은 같은 사진을 여러 번 올린 것이 대부분이었다.
  분석 JSON 내용이 같은 것을 하나로 묶으니 서로 다른 방은 13개였다. 중복을 그대로 세면
  22번 들어간 방 하나가 평균을 좌우하므로, 아래 표는 모두 중복을 뺀 값이다.
- **방 집합이 같다.** 세 결과 모두 같은 13개 방이다(`resummarize --only-names-from`).
- **출처**
  - 기존: [baseline_cached_dedup.json](baseline_cached_dedup.json). 커밋 `593adf0`의 산출물이다.
  - 1단계: [phase1_cached_rebuild_dedup.json](phase1_cached_rebuild_dedup.json)
  - 2단계: [phase2_cached_rebuild.json](phase2_cached_rebuild.json)
  - 중복을 포함한 원래 측정값은 `baseline_cached.json`, `phase1_cached_rebuild.json`에 참고용으로 남겨 둔다.
- **정답이 없어** 공간 분석 정확도와 API 지표는 아직 없다.

| 지표 | 기존 | 1단계 | 2단계 |
|---|---|---|---|
| 2D↔3D 중심 오차 (m) | 0.344 | 0.0002 | 0.0002 |
| 2D↔3D IoU | 0.426 | 0.952 | 0.999 |
| 10cm 넘게 어긋난 객체 | 52.9% | 0% | 0% |
| 3D id 보존율 | 6.6% | 100% | 100% |
| 3D에서 상자로 그려진 객체 | 25.6% | 2.3% | 2.3% |
| 가구 충돌률, 3D | 32.1% | 13.5% | **0%** |
| 충돌이 하나라도 있는 방, 3D | 69.2% | 23.1% | **0%** |
| 가구 충돌률, 2D | 16.7% | 18.5% | 3.3% |
| 벽 관통률, 3D | 16.1% | 1.0% | **0%** |
| 접근 가능한 가구 비율 | 70.0% | 71.9% | **89.2%** |
| 문에서(없으면 가장 큰 빈 영역에서) 닿는 바닥 비율 | 27.4% | 27.9% | 27.5% |
| 평면도 1장당 Gemini 호출 | 2회 | 1회 | 1회 |

### 단계별 원인

**기존 방식에서 드러난 문제**
- **3D에서 id가 사라진다.** `build_scene`이 `resolve_placement`를 거치면서 `scene_id`를 잃는다. 기준값을 잴 때는 순번(`idx`)으로 복원해 비교했다.
- **좌우 벽 가구는 3D에서 가로·세로가 뒤바뀐다.** `convert_placed`는 평면도 기준의 가로·세로를 그대로 `w_m`·`d_m`으로 넘기는데, three.js는 이 값을 회전시켜 그린다. 중복을 포함한 75건으로 재면 이것 하나가 충돌률을 약 17%p, 벽 관통률을 약 16%p 올린다.
- **2D SVG는 분석 JSON을 그대로 따르지 않는다.** Gemini가 그림을 그리면서 위치를 다시 정한다.
- **sofa, armchair, speaker 등이 `TYPE_MAP`에 없다.** 그래서 3D에서 상자로 그려진다.

**1단계(Scene Graph 단일화)에서 고친 것**
- 2D와 3D가 같은 Scene Graph를 재계산 없이 그린다. 위치·id·회전이 같아진다.
- 벽에 붙은 가구는 평면도에서 차지하는 영역을 유지한 채 벽 방향으로 회전한다. 가로·세로 뒤바뀜이 사라진다.
- 벽걸이 객체는 긴 변을 벽을 따라 두고 두께를 8cm 이하로 둔다.
- 2D SVG를 로컬에서 그려 Gemini 호출이 1회 줄었다.

**2단계(배치 보정, `model2/placement_solver.py`)에서 고친 것**
- **충돌:** 고정할 가구부터 확정하고 나머지는 원래 위치에서 가까운 빈자리로 옮긴다.
  - 고정 순서: 사용자 수정·상품 → 벽에 붙은 가구 → 큰 가구
  - 0.8m 안에 빈자리가 없으면 크기를 최대 20% 줄여 본다. 분석 크기가 실제보다 큰 경향이 있어서다.
  - 문 앞의 열림 공간은 비워 둔다.
- **동선:** 폭 60cm 통로로 닿지 않는 가구가 있으면, 그 가구나 앞을 막은 가구를 충돌 없이 닿게 되는 가장 가까운 자리로 옮긴다.
- **보정 이력:** 모든 이동과 축소는 `solver_adjustments`에 사유와 함께 남는다.
- **2D 충돌률이 0%가 아닌 이유:** 2D 지표는 SVG 외곽 직사각형으로 잰다. 그래서 비스듬히 놓인 가구는 실제 모양보다 크게 잡힌다(남은 1건이 이 경우). 3D 지표는 회전한 실제 바닥면으로 잰다.

**남은 문제**
- **접근 가능한 가구 89%:** 분석 크기가 커서 가구가 바닥의 44%(중앙값)를 차지한다. 실제 방은 대략 25~35%다. 옮길 공간 자체가 부족한 방이 남는다.
- **문 미검출(92%):** 사진에 문이 찍히는 경우가 드물다. 이런 방은 가장 큰 빈 영역을 기준으로 동선을 잰다.
- **방 치수:** 실측 입력이 없으면 기준 가구로 추정한다. 사진 한 장의 가구 크기에 오차가 커서, 정답 데이터로 다시 검증해야 한다.
