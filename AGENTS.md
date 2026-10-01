# AGENTS.md

AI 코딩 에이전트(Codex, Claude Code 등)가 이 저장소를 clone한 직후 바로
환경을 세팅하고 실행할 수 있도록 정리한 문서다. 사람이 읽어도 된다.

---

## 1. 이 프로젝트가 하는 일

방 사진 한 장을 올리면 → 평면도(SVG)를 만들고 → 가구를 추천하고 →
결과 화면과 3D 미리보기를 보여주는 Flask 웹앱이다.

```
사진 업로드 → /floorplan (평면도 생성) → /result (결과) → /preview-3d (3D)
```

외부 API를 두 개 쓴다.

| API | 용도 | 없으면 |
|---|---|---|
| Google Gemini | 공간 분석, 평면도 SVG, 3D 렌더 SVG | 평면도가 안 만들어짐 |
| SerpApi (Google Shopping) | 가구 상품 추천 | 상품 목록만 빔 |

---

## 2. 설치

Python **3.12** 기준이다.

먼저 3.12가 있는지 본다. 최신 Python(3.13+)만 깔려 있으면 torch/ultralytics
휠이 아직 안 올라와 설치가 깨진다. 시스템 Python을 건드리지 않고 3.12만
따로 받으려면 [uv](https://docs.astral.sh/uv/)가 편하다.

```bash
py --list                  # Windows: 설치된 버전 확인
uv python install 3.12     # 없으면 uv 관리 디렉터리에 받는다(전역 PATH 영향 없음)
uv venv venv --python 3.12
uv pip install --python venv/Scripts/python.exe -r requirements.txt   # Windows
```

uv 없이 갈 거면 3.12를 직접 설치한 뒤 저장소 루트(이 파일이 있는 디렉터리)에서:

```bash
# Windows
python -m venv venv
venv\Scripts\python.exe -m pip install --upgrade pip
venv\Scripts\python.exe -m pip install -r requirements.txt

# macOS / Linux
python3 -m venv venv
venv/bin/python -m pip install --upgrade pip
venv/bin/python -m pip install -r requirements.txt
```

`requirements.txt`에 torch/transformers(CLIP)와 ultralytics(YOLO)가 들어 있어
첫 설치는 수 분 걸린다. YOLO 가중치(`yolov8n.pt`)는 최초 실행 시 자동으로
받는다.

---

## 3. 환경변수

`.env`는 키가 들어 있어 **저장소에 없다.** clone 후 직접 만들어야 한다.

```bash
cp .env.example .env
```

그리고 아래 값을 채운다. 나머지는 전부 코드에 기본값이 있어 비워도 동작한다.

| 변수 | 발급처 | 필수 |
|---|---|---|
| `GEMINI_API_KEY` | https://aistudio.google.com/apikey | 예 |
| `SERPAPI_API_KEY` | https://serpapi.com/manage-api-key | 상품 추천에만 |
| `FLASK_SECRET_KEY` | `python -c "import secrets; print(secrets.token_hex(32))"` | 권장 |

`GEMINI_API_KEY`는 `AIza`로 시작하는 39자여야 한다. `AQ.`로 시작하는 값은
OAuth 액세스 토큰이라 몇 시간 뒤 만료되며 `401 UNAUTHENTICATED`가 난다.

### 자주 건드리는 값

```ini
GEMINI_LAYOUT_MODEL=gemini-3.1-flash-lite   # 1단계 배치 분석 (무료 RPD 500)
GEMINI_SVG_MODEL=gemini-3.5-flash-lite      # 2D 가구 그림(또는 gemini 렌더러의 평면도 전체)
FLOORPLAN_2D_RENDERER=scene_graph           # 2D 평면도 렌더러 (7.2)
FLOORPLAN_2D_ARTWORK=gemini                 # 가구 겉모양: gemini | local (7.2)
FLOORPLAN_CACHE=1                           # 평면도 캐시 (30분 TTL)
ENABLE_GEMINI_SVG_RENDER=true               # /preview-3d 의 AI 입체 SVG
GEMINI_ROOM_SVG_MODEL=gemini-3.5-flash-lite # 입체 SVG 우선 모델
GEMINI_ROOM_SVG_FALLBACK_MODELS=gemini-3.1-flash-lite,gemini-2.5-flash-lite
GEMINI_FURNITURE_PARTS_MODEL=               # three.js 가구 형태 설계도 (7.9)
SERPAPI_CACHE_TTL_SECONDS=21600             # 상품 검색 결과 6시간 재사용
```

> `GEMINI_ROOM_SVG_MODEL` 에 `-lite` 계열을 두면 입체 SVG 가 원근도 그림자도
> 없는 납작한 도형으로 나온다. 실패가 아니라 "성공했지만 빈약한" 결과라
> 폴백도 걸리지 않는다. 품질이 필요하면 `gemini-3.6-flash` 로 올린다.

> **주의** `.env`는 Flask 자동 리로더의 감시 대상이 아니다. 값을 바꾸면
> **서버를 직접 재시작해야** 반영된다. `.py` 파일은 저장만 하면 자동 리로드된다.

---

## 4. 실행

```bash
# Windows
venv\Scripts\python.exe backend/app.py

# macOS / Linux
venv/bin/python backend/app.py
```

http://127.0.0.1:5000 에서 열린다. `debug=True`라 `.py` 수정은 자동 반영된다.

---

## 5. 저장소에 없는 데이터

용량 때문에 제외한 것들이다. **없어도 평면도 생성과 상품 추천은 동작한다.**
무드 이미지 검색 기능만 제한된다.

| 경로 | 크기 | 재생성 방법 |
|---|---|---|
| `images/final/` | 219MB | 원본 데이터셋. 샘플 3장만 커밋됨 |
| `data/` | 17MB | `mood_search_v1`의 embedding → clustering → labeling |
| `mood_library/` | 210MB | `mood_search_v1.run_build_library` |
| `frontend/static/generated/` | 런타임 | 앱이 실행하며 자동 생성 |

### 무드 라이브러리는 이미지만 채운다고 생기지 않는다

`images/final/`에 사진을 넣어도 `/mood-search`는 계속 500을 뱉는다. 검색이
읽는 건 원본이 아니라 `mood_library/index.json`이고, 그건 파이프라인을
돌려야 만들어진다. 폴더가 없어서가 아니라 **내용물이 없어서** 나는 오류라
빈 폴더를 만들어도 해결되지 않는다.

```
images/final/  →  run_embedding()   →  data/
               →  run_clustering()
               →  run_labeling()
               →  run_build_library()  →  mood_library/
```

`backend/app.py`가 기동할 때 `index.json`이 없으면 이 네 단계를 자동으로
돌린다. 그래서 보통은 서버를 한 번 띄우면 끝이고, 첫 기동만 오래 걸린다.

- **네 단계 모두 로컬 CLIP만 쓴다. Gemini 호출 0회**라 RPD와 무관하다
- 이미지 1265장 기준 **CPU로 약 4.4분**(GPU 불필요)
- `mood_library/`는 이미지를 복사하므로 디스크가 원본 크기만큼 더 필요하다
- `images/final/`이 비어 있으면 건너뛰고 안내만 남긴다. 무드 검색만 죽고
  평면도·상품 추천은 정상 동작한다
- 이미지를 더 넣어도 자동 재빌드되지 않는다. 갱신하려면 `mood_library/`를
  지우고 다시 띄운다

---

## 6. 코드 구조

```
backend/
  app.py                     Flask 라우트 전부 (약 4900줄)
  product_recommendation.py  SerpApi 검색 + 상품 랭킹
  models.py, auth.py         사용자 계정
model1/                      평면도 생성 (폴백 경로, 현재 미사용)
model2/
  web_floorplan.py           평면도 생성 (현재 사용 중)
  topdown_experiment/run.py  1단계: 사진 → 배치 JSON
  gemini_svg_experiment.py   2단계: 배치 JSON → 평면도 SVG
  gemini_room_svg_render.py  /preview-3d 의 입체 렌더 SVG
  gemini_furniture_parts.py  three.js 가구 형태 설계도 (7.9)
  gemini_retry.py            Gemini 503/429 재시도 공통 모듈
  floorplan_3d.py            배치 JSON → three.js 씬 데이터
mood_pipeline/, mood_search_v1/   무드 이미지 분석·검색
frontend/templates/, static/      Jinja 템플릿과 정적 파일
```

`FLOORPLAN_PROVIDER`가 model1/model2 중 어느 쪽을 쓸지 정한다.
기본값 `model2_gemini_svg`이므로 **model1은 현재 실행되지 않는다.**

---

## 7. 에이전트가 알아야 할 함정

### 7.1 Gemini 무료 등급 한도가 진짜 제약이다

| 모델 | 분당(RPM) | **하루(RPD)** |
|---|---|---|
| `gemini-3.x-flash` | 5 | **20** |
| `gemini-3.x-flash-lite` | 15 | **500** |

평면도 1장에 flash 계열을 1회, `/preview-3d`까지 가면 2회 쓴다.
즉 **하루 10~20바퀴가 한계**다. 디버깅하며 반복 실행하면 금방 소진된다.

- `503 UNAVAILABLE` = 구글 서버 혼잡(무료 등급 우선 거절). 코드 문제 아님
- `429 RESOURCE_EXHAUSTED` = 한도 초과. 분당이면 1분 뒤, 일일이면 태평양 자정까지 안 풀림
- 재시도는 `model2/gemini_retry.py`가 처리한다. 일일 한도는 재시도하지 않는다

**테스트할 때 API를 불필요하게 호출하지 말 것.** 캐시(`FLOORPLAN_CACHE=1`,
30분 TTL)를 켜 두고, 모델 비교가 필요할 때만 끈다.

### 7.2 Scene Graph와 SVG의 ID 계약

방 하나의 공간 정보는 **Scene Graph**(`model2/scene_graph.py`, `schema:
"scene_graph_v1"`) 한 곳에만 있다. 2D 평면도(`model2/scene_render_2d.py`)와
3D(`floorplan_3d.build_scene_from_graph`)는 둘 다 이걸 그대로 읽는다. 좌표를
다시 계산하지 않으므로 2D와 3D의 위치·크기·회전이 같다.

- 단위는 미터. `w_m`·`d_m`은 **가구 기준**(뒷면과 나란한 폭, 앞뒤 깊이)이고,
  `rotation_deg`는 위에서 본 시계방향(0 = 뒷면이 위쪽 벽).
- 기존 라우트가 읽는 정규화 필드(`x, y, w, h, wall, scene_id`)는 같은 파일에
  함께 저장되지만 **미터 값에서 계산한 뷰**다. 미터 값을 고친 뒤에는
  `scene_graph.sync_legacy()`(또는 `ensure()`)를 불러 다시 맞춘다.
- legacy 필드만 채워 넣은 객체(상품 추가 등)는 `ensure()`가 미터 값을 복원한다.
- 2D 렌더러를 Gemini로 되돌리려면 `FLOORPLAN_2D_RENDERER=gemini`. 개선 전후
  비교 실험용으로만 남겨 둔 경로다.
- 가구 **겉모양**은 Gemini가 그린다(`model2/gemini_floorplan_artwork.py`). 가구마다
  자기 크기 상자 안의 그림 조각만 받고, 렌더러가 그 외곽을 Scene Graph 바닥면에
  맞춰 끼운다. 그래서 그림이 풍성해도 좌표는 3D와 같다. 실패하면 그 가구만 코드의
  기본 모양으로 그린다. 캐시 키에 위치가 없어서 편집 후 재렌더에 호출이 없다.
  Gemini 3 계열은 `thinking_budget=0`을 400으로 거절하므로
  `gemini_svg_experiment.minimal_thinking(model)`을 쓴다.
- **편집은 `POST /api/scene/edit` 하나로 한다**(`model2/scene_edit.py`). 2D 검토
  패널, 3D "가구 옮기기", 결과 화면 3D가 모두 이 API에 연산(move·rotate·resize·
  remove·confirm·retype·add)을 보내고, 서버가 Scene Graph를 고쳐 2D·3D를 같이
  돌려준다. 사람이 고친 값은 `source="user"`, AI가 냈던 값은 `corrections`에 남는다.
  2D 드래그 편집기(`/floorplan/save-edit`)는 그대로 쓰되 결과는 같은 파일로 간다.
- 유지·제거·교체 선택은 **순번이 아니라 `scene_id`로** 가구를 찾는다. 검토 패널에서
  가구를 지우거나 추가하면 순번이 밀리기 때문이다(`choice_object_index`).
- 결과 화면 3D에서 옮긴 **상품** 위치는 `session["product_overrides"]`에 둔다. 수정
  평면도는 선택이 바뀔 때마다 기준 배치에서 새로 만들어지기 때문이다.
- `POST /api/scene/reanalyze`는 **확신이 낮은 가구만** 사진에서 잘라 Gemini에 다시
  묻는다(`model2/gemini_reanalyze.py`). 분석 프롬프트가 `photo_box`(사진 속 위치)를
  주기 때문에 가능하다. 결과는 `source="ai_refined"`로 표시되고 사람이 고친 가구는
  건드리지 않는다. Gemini 호출이므로 버튼을 눌렀을 때만 돈다.
- 상품 실측 치수는 `model2/product_dimensions.py`가 정한다(제목 → 설명 → 상품 페이지 →
  사진 속 치수표 → 규격(퀸·3인용) → 타입 기본값). 출처는 `dimension_source`, 실측
  여부는 `measured`로 남는다. 형태 속성은 `model2/product_attributes.py`(사진 → Gemini).
  `/add-product`가 새 상품만 분석한다(아이콘·속성·치수, 이미지 URL 단위 캐시).
- **추천은 무드 + 공간을 함께 본다**(`model2/spatial_fit.py`). 상품을 실제 치수로
  Scene Graph에 넣어 보정기로 자리를 찾게 한 뒤 공간 적합도(겹침·동선)와 크기
  적합도(교체 대상 또는 방 면적 대비)를 잰다. `recommend_furniture(spatial_scorer=...)`가
  무드 상위 15개만 평가해 무드 55% + 공간 30% + 크기 15%로 다시 정렬한다. 결과 화면의
  "추천받기"(`POST /api/recommendations`)와 검색(`/search-products?type=`)이 이걸 쓴다.
  추천 단계에서는 상품 페이지를 받지 않는다(제목·설명의 치수만 사용).
- 결과 화면(`/result`)이 통합 화면이다: 2D/3D 전환, 3D 옮기기, 유지·제거·교체,
  검색·추천(적합도 표시). `/furniture-choice`, `/product-selection`은 여전히 `[임시]`로
  건너뛴다.
- 배치 보정은 `model2/placement_solver.py` 하나가 맡는다(충돌·벽·문 앞·동선).
  분석 직후, 상품 추가(`ensure`, 새 객체만), 편집 저장(사용자가 옮긴 가구는
  고정, 동선 보정 없음) 때 돈다. 이동 사유는 `solver_adjustments`에 남는다.
  `source`가 `user`·`selected_product`인 가구는 움직이지 않는다.

평면도 SVG는 Scene Graph의 객체 id와 **정확히 같은** 그룹 id를 가져야 한다.

```json
{ "id": "bed_1", "category": "bed", ... }
```
```html
<g id="bed_1">...</g>          <!-- 가구 본체 -->
<g id="label-bed_1">...</g>    <!-- 라벨 -->
```

`backend/app.py`의 수정·삭제 기능이 이 id로 가구를 찾는다. 어긋나면
**평면도는 정상으로 보이는데 "수정하기"만 조용히 죽는다.** 에러가 안 난다.

기본 렌더러(`scene_graph`)는 이 계약을 코드로 보장한다. `gemini` 렌더러로
바꿨을 때만 모델이 id를 빠뜨릴 수 있으니, 그때는 실제로 편집이 되는지
확인해야 한다.

### 7.3 /preview-3d 의 "3D"는 두 가지다

`/preview-3d`에는 성격이 다른 두 화면이 **위아래로 나란히** 뜬다. 택일이
아니다. 템플릿의 `{% if svg_render_url %}` 블록은 위쪽 SVG만 감싸고,
three.js 뷰어는 그 바깥에 있어 항상 렌더된다.

| | 위치 | 만드는 주체 | 실패하면 |
|---|---|---|---|
| **AI 입체 SVG** | 위 | Gemini (`gemini_room_svg_render.py`) | 블록만 사라짐 |
| **3D 배치 화면** | 아래 | 로컬 three.js (`floorplan_3d.py` → `scene.json`) | — |

three.js 씬의 **배치**는 Gemini가 만들지 않는다. 배치 JSON에서 결정론적으로
계산하므로 좌표·치수가 정확하고 API 한도와 무관하게 항상 동작한다.

다만 **가구의 형태**는 Gemini가 거들 수 있다(7.9). 배치와 형태는 별개다.

> AI 입체 SVG를 만들지 못해 정확한 3D 배치 화면으로 대신합니다.

이 안내가 뜨는 건 **장식용 일러스트 한 장만 실패한 것**이고 3D 자체는
정상이다. 기능 고장으로 오해하기 쉽다. 실패 사유는 서버 콘솔의
`[gemini-room-svg] 생성 실패:` 줄과 `.failed` 파일에 남는다.

입체 SVG가 필요 없으면 `ENABLE_GEMINI_SVG_RENDER=false`로 끈다. 안내 문구도
사라지고 RPD도 아낀다.

### 7.4 Gemini 클라이언트는 변수에 담아야 한다

```python
# 안 된다 — Client가 임시객체라 GC가 수거하며 커넥션을 닫는다
response = _client().models.generate_content(...)
#   -> RuntimeError: Cannot send a request, as the client has been closed.

# 이렇게
client = _client()
response = client.models.generate_content(...)
```

### 7.5 SerpApi는 느리다

캐시 안 된 검색이 **중앙값 12초**다(10초를 넘기는 게 정상). 그래서
`SERPAPI_TIMEOUT=30`이고, 가구 종류별 검색은 `provider.prefetch()`로
병렬 조회한다. 순차로 돌리면 13종에 2분 30초가 걸린다. 성공한 검색 결과는
기본 6시간 동안 디스크에도 저장하므로 같은 검색어는 서버를 재시작해도 즉시
재사용한다. `SERPAPI_CACHE_TTL_SECONDS=0`이면 이 디스크 캐시를 끈다.

무료 플랜은 **월 250회**다.

### 7.6 문법 오류를 저장하면 서버가 죽고, 고쳐도 안 살아난다

`debug=True`라 `.py`를 저장하면 리로더가 바로 읽는다. 그 순간 파일이
`SyntaxError` 상태면 리로더가 프로세스째 종료한다. 문제는 **그 뒤 코드를
고쳐도 서버가 스스로 돌아오지 않는다**는 것이다. 감시하던 프로세스가 이미
죽었기 때문이다.

브라우저에 `ERR_CONNECTION_REFUSED`가 뜨면 코드 버그를 의심하기 전에 서버가
살아 있는지부터 본다.

```bash
venv\Scripts\python.exe -m py_compile backend/app.py   # 저장 직후 확인
```

편집 후 `py_compile`로 검증하는 습관을 들이면 이 상황을 대부분 피한다.

### 7.7 세션·캐시 때문에 코드 수정이 안 보일 수 있다

| | 대처 |
|---|---|
| 세션에 남은 값 | 브라우저 쿠키 삭제 후 플로우 처음부터 |
| 평면도 캐시 | `frontend/static/generated/`의 해당 파일 삭제 (또는 30분 대기) |
| 렌더 실패 쿨다운 | `gemini_room_svg_v1/`의 `.failed` 파일 삭제 |

### 7.8 현재 임시로 건너뛴 단계가 있다

`/furniture-choice`와 `/product-selection`은 지금 `/result`로 리다이렉트만
한다. 원래 코드는 그 아래에 그대로 살아 있고, 각 함수 맨 위의 `return` 두
줄만 지우면 복구된다. `backend/app.py`에서 `[임시]` 주석으로 표시해 뒀다.

### 7.9 three.js 가구 형태는 Gemini 설계도로 덮인다

> **현재 구조(4단계 이후):** three.js는 **배치만** 맡고, 가구 모양은 가구마다
> Gemini가 만든다(`gemini_furniture_parts.generate_object_parts`, `POST /api/scene/parts`).
> 상품은 상품 사진, 기존 가구는 방 사진에서 `photo_box`로 잘라 낸 부분을 함께 보낸다.
> 3D 화면은 먼저 배치를 그리고, 모양이 오면 같은 배치로 다시 세운다. 캐시는 가구
> 정체성 단위(위치 무관)라 편집 뒤에는 새로 들어온 가구만 묻는다. 부품은 색·재질을
> 가질 수 있다. 모양을 못 받은 가구는 `PARAMETRIC` 빌더(상품 형태 속성 반영)로 그린다.
> 아래 타입 단위 설계도(`generate_furniture_parts`)는 이전 방식으로 남아 있다.

three.js 의 가구 모양은 원래 `floorplan_3d.js` 의 `BUILDERS` 에 손으로 짜
넣은 상자 조합이다(27종). 종류가 늘수록 품질 편차가 커서, Gemini 에게 형태를
**부품 목록으로** 받아 덮어쓰는 경로를 뒀다(`gemini_furniture_parts.py`).

완성된 그림을 Gemini 에게 그리게 하는 입체 SVG(7.3)와 혼동하지 말 것. 이쪽은
그림이 아니라 좌표 JSON 만 받는다. 가벼운 작업이라 무료 등급 모델로도
생성되고, 재질·조명·원근은 three.js 가 GPU 로 처리한다.

```
배치 JSON → Gemini: 종류별 부품 목록 → three.js: buildFromParts()
                                        없으면 BUILDERS 로 폴백
```

주의할 점이 셋 있다.

**설계도는 전제 조건이 아니다.** 생성이 실패하면 예외를 올리지 않고 빈
dict 를 반환한다. three.js 는 그대로 기존 `BUILDERS` 로 그린다. 그래서
실패해도 화면이 나빠지지 않고, 안내 문구도 뜨지 않는다. 대신 조용히 넘어가므로
서버 콘솔의 `[gemini-furniture-parts] 생성 실패:` 줄을 봐야 원인을 알 수 있다.

**좌표는 미터가 아니라 비율이다.** 가구 자체 치수에 대한 0~1 값이라 방 크기나
배치가 달라져도 같은 설계도를 재사용한다. 그래서 캐시 키에 배치가 들어가지
않고, 배치를 바꿔가며 여러 번 들어와도 호출이 늘지 않는다. 규약(원점, y 는
밑면 기준, 뒷면이 -z)은 `gemini_furniture_parts.py` 의 docstring 에 있고
`floorplan_3d.js` 의 `box()`/`cylinder()` 와 맞춰야 한다.

**부품이 빈 항목은 버려야 한다.** `{"parts": []}` 를 그대로 넘기면 three.js 가
"설계도가 있다"고 믿고 `BUILDERS` 를 건너뛰어 **가구가 통째로 사라진다.**
`_coerce()` 에서 걸러내고 있으니 그 검증을 약화시키지 말 것.

모델은 `GEMINI_FURNITURE_PARTS_MODEL` 로 따로 지정한다. 비우면
`GEMINI_ANALYSIS_MODEL` 을 따른다.

---

## 8. 테스트

```bash
venv\Scripts\python.exe -m pytest tests/ -q     # Windows
venv/bin/python -m pytest tests/ -q             # macOS / Linux
```

`tests/test_product_recommendation.py`는 `MockProvider`를 써서 SerpApi를
호출하지 않는다. 테스트는 API 키 없이 돌아가야 하며, 새로 테스트를 추가할
때도 실제 API를 부르지 않도록 한다.
