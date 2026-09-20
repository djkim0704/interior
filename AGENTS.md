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

Python **3.12** 기준이다. 저장소 루트(이 파일이 있는 디렉터리)에서:

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
GEMINI_SVG_MODEL=gemini-3.6-flash           # 2단계 평면도 SVG (무료 RPD 20)
FLOORPLAN_CACHE=1                           # 평면도 캐시 (30분 TTL)
ENABLE_GEMINI_SVG_RENDER=true               # /preview-3d 의 AI 입체 SVG
```

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

### 7.2 SVG의 ID 계약

2단계가 만드는 평면도 SVG는 배치 JSON의 객체 id와 **정확히 같은** 그룹 id를
가져야 한다.

```json
{ "id": "bed_1", "category": "bed", ... }
```
```html
<g id="bed_1">...</g>          <!-- 가구 본체 -->
<g id="label-bed_1">...</g>    <!-- 라벨 -->
```

`backend/app.py`의 수정·삭제 기능이 이 id로 가구를 찾는다. 어긋나면
**평면도는 정상으로 보이는데 "수정하기"만 조용히 죽는다.** 에러가 안 난다.

그래서 `GEMINI_SVG_MODEL`을 가벼운 모델로 바꿀 때는 반드시 실제로 편집이
되는지 확인해야 한다. 1단계(`GEMINI_LAYOUT_MODEL`)는 `normalize_layout`이
빠진 값을 메워주므로 가벼운 모델로 바꿔도 비교적 안전하다.

### 7.3 Gemini 클라이언트는 변수에 담아야 한다

```python
# 안 된다 — Client가 임시객체라 GC가 수거하며 커넥션을 닫는다
response = _client().models.generate_content(...)
#   -> RuntimeError: Cannot send a request, as the client has been closed.

# 이렇게
client = _client()
response = client.models.generate_content(...)
```

### 7.4 SerpApi는 느리다

캐시 안 된 검색이 **중앙값 12초**다(10초를 넘기는 게 정상). 그래서
`SERPAPI_TIMEOUT=30`이고, 가구 종류별 검색은 `provider.prefetch()`로
병렬 조회한다. 순차로 돌리면 13종에 2분 30초가 걸린다.

무료 플랜은 **월 250회**다.

### 7.5 세션·캐시 때문에 코드 수정이 안 보일 수 있다

| | 대처 |
|---|---|
| 세션에 남은 값 | 브라우저 쿠키 삭제 후 플로우 처음부터 |
| 평면도 캐시 | `frontend/static/generated/`의 해당 파일 삭제 (또는 30분 대기) |
| 렌더 실패 쿨다운 | `gemini_room_svg_v1/`의 `.failed` 파일 삭제 |

### 7.6 현재 임시로 건너뛴 단계가 있다

`/furniture-choice`와 `/product-selection`은 지금 `/result`로 리다이렉트만
한다. 원래 코드는 그 아래에 그대로 살아 있고, 각 함수 맨 위의 `return` 두
줄만 지우면 복구된다. `backend/app.py`에서 `[임시]` 주석으로 표시해 뒀다.

---

## 8. 테스트

`pytest`는 `requirements.txt`에 없으므로 따로 설치해야 한다.

```bash
venv\Scripts\python.exe -m pip install pytest      # Windows
venv/bin/python -m pip install pytest              # macOS / Linux

venv\Scripts\python.exe -m pytest tests/ -q
```

`tests/test_product_recommendation.py`는 `MockProvider`를 써서 SerpApi를
호출하지 않는다. 테스트는 API 키 없이 돌아가야 하며, 새로 테스트를 추가할
때도 실제 API를 부르지 않도록 한다.
