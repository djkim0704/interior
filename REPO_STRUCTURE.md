# 인테리어 프로젝트 — 레포 구조

`interior/`는 **단일 git 레포**로, 관심사별로 폴더를 분리했습니다.
프론트/백엔드/model1/model2가 한 프로세스에서 서로 import하므로 **가상환경·requirements는 루트 하나**로 통일합니다.

## 최종 구조

```
interior/
├── frontend/                  # 사용자 화면
│   ├── templates/             # Jinja2 템플릿 16개 (layout/navbar/footer + 화면 13개)
│   └── static/
│       ├── css/  js/          # 화면 스타일·스크립트 (bootstrap은 CDN)
│       └── uploads/ generated/  # 런타임 산출물 (gitignore, .gitkeep만 커밋)
│
├── backend/                   # Flask 서버 (라우팅·API·DB)
│   ├── app.py                 # 진입점. 라우트 24개 + 세션 + YOLO 가구탐지
│   ├── auth.py                # 로그인·회원가입 Blueprint (auth_bp)
│   ├── extensions.py          # db(SQLAlchemy)·login_manager 인스턴스
│   ├── models.py              # User·SavedDesign 테이블
│   ├── product_recommendation.py  # 네이버쇼핑 검색 + 무드 분석 + CLIP 재정렬
│   ├── product_profiles.py    # 상품 카테고리·검색어 프로파일
│   ├── ai_backend.py          # 이미지 생성 mock 헬퍼
│   └── app.db                 # SQLite (gitignore)
│                              # (yolov8n.pt: YOLO 가중치는 첫 실행 시 자동 다운로드)
│
├── model2/                    # ⭐ 현재 기본 평면도 생성 경로
│   ├── web_floorplan.py       # 사진 → Gemini → 평면도 SVG. generate_floorplan_for_web
│   │                          #  + 평면도 편집(prepare/apply/sanitize) + 상품 시각 프로파일
│   ├── gemini_svg_experiment.py   # Gemini SVG 저수준 호출(_extract_svg, generate_svg_text)
│   ├── product_icon_svg.py    # 상품 사진 → 평면도용 아이콘 SVG
│   ├── topdown_experiment/    # 방 구조 분석(analyze_room) — web_floorplan이 사용
│   │   ├── run.py  illustrator.py  layout_solver.py  renderer_3d.py
│   ├── run_gemini_features.py # mood_pipeline.gemini_extract 실행 래퍼
│   └── notebooks/06_gemini_features.ipynb   # ⚠ 실행 불가 (아래 "알려진 부채" 참고)
│
├── model1/                    # 사진 → 2D 평면도 (Gemini) — 폴백 경로
│   ├── __init__.py
│   ├── interior_to_floorplan.py  # 하위 호환 re-export 레이어 (기존 import 경로 유지)
│   ├── config.py              # 상수·.env 로드·프롬프트 로드(load_prompt)
│   ├── client.py              # Gemini 클라이언트 생성·API 재시도(429/503)
│   ├── io_utils.py            # 파일/이미지 IO·산출물 경로·캐시 판정·텍스트 파서
│   ├── gemini_steps.py        # Gemini 저수준 호출(분석/평면도/layout/SVG/교정)
│   ├── pipeline.py            # 고수준 단계(run_*_step)·convert_image·generate_floorplan_for_web
│   ├── render.py              # 노트북 시각화(matplotlib/HTML)
│   └── cli.py                 # CLI main() — 배치 처리 진입점
│
├── mood_pipeline/             # ⭐ 루트 공용 패키지 (model1·model2·backend 공유)
│   ├── config.py              # PROJECT_ROOT 및 경로·모델 상수
│   ├── gemini_extract.py      # Gemini 특징 추출 + UMAP
│   ├── preprocess.py          # 이미지 수집·검증·중복제거 (mood_search_v1도 이걸 재사용)
│   └── rule_based_svg.py      # layout JSON → SVG 렌더러(무료), 프롬프트는 루트 prompts에서 로드
│
├── mood_search_v1/            # ⭐ 무드 검색 (CLIP) — backend /mood-search 구현
│   ├── __init__.py            # lazy export (PEP 562) — 무거운 의존성 지연 로드
│   ├── search.py              # 프롬프트 → 무드/이미지 검색 (런타임)
│   ├── embed.py               # CLIP 임베딩 (search가 사용)
│   ├── korean_mood_router.py  # 한글 무드어 라우팅   ┐ search 전용
│   ├── semantic_axes.py       # 의미축 감지                ┘
│   ├── config.py              # 이 패키지 전용 경로·상수
│   ├── preprocess.py          # mood_pipeline.preprocess re-export
│   └── build_library.py  cluster.py  label.py   # 오프라인 데이터 생성 전용 (런타임 미사용)
│
├── prompts/                   # ⭐ 모든 Gemini 프롬프트 (*.txt) — model1·mood_pipeline 공유
│   ├── analysis.txt  floorplan.txt  layout_detail.txt
│   ├── layout_refine.txt  svg_floorplan.txt   # ← model1
│   └── rule_based_layout.txt                  # ← mood_pipeline/rule_based_svg
│
├── tests/                     # unittest 32건 (루트를 import 경로에 두고 실행)
├── docs/                      # 설계 검토 문서 (구현 완료분 기록)
├── images/                    # 무드 원본 이미지 (images/final)
├── data/                      # 임베딩·클러스터·번역 캐시 (gitignore, 재생성 가능)
├── mood_library/              # 무드 라이브러리 산출물 (gitignore, 재생성 가능)
├── output/floorplans/         # 평면도 생성 결과 (gitignore, .gitkeep만)
│
├── requirements.txt           # 통합 의존성 (venv 한 번)
├── .env / .env.example        # 공통 환경변수
├── venv/                      # 가상환경 (gitignore)
└── .gitignore
```

## 평면도 생성 경로 2개 (model1 vs model2)

`.env`의 `FLOORPLAN_PROVIDER`가 어느 쪽을 쓸지 정합니다. 기본값은 `model2_gemini_svg`.

```
FLOORPLAN_PROVIDER=model2_gemini_svg  → model2/web_floorplan.py   (기본)
그 외 값                              → model1/interior_to_floorplan.py (폴백)
```

두 모듈 모두 `generate_floorplan_for_web(upload_path, out_dir, ...)` 시그니처를 맞춰
`backend/app.py`가 같은 방식으로 호출합니다.

## 경로 규칙 (왜 이렇게 배치했나)

- **`mood_pipeline`은 루트 공용 패키지** → model1의 `from mood_pipeline.config import ...`가 그대로 동작.
- **`mood_pipeline/config.py`의 `PROJECT_ROOT = 파일.parent.parent` = interior 루트.**
  그래서 `images/`, `data/`, `output/`, `prompts/`를 **루트 레벨**에 둬야 config 경로와 맞음.
- **프롬프트는 코드에 하드코딩하지 않고 루트 `prompts/*.txt`에서 로드**한다.
  model1(`config.load_prompt`)과 mood_pipeline(`rule_based_svg.load_prompt`)이 같은 폴더를 공유.
- **`.env`는 각 모듈 import 시점에 먼저 로드**한다(`model1/config.py`, `mood_pipeline/gemini_extract.py`).
  CLI 직접 실행이든 backend 경유든 `os.getenv(...)`가 항상 `.env` 값을 읽도록 하기 위함.
- `backend/app.py`는 프론트가 분리됐으므로 `Flask(template_folder=../frontend/templates, static_folder=../frontend/static)`.
- YOLO 가중치는 `backend/yolov8n.pt` (없으면 ultralytics가 자동 다운로드).
- **`mood_search_v1/__init__.py`는 lazy export**다. backend는 `search`만 쓰는데 예전에는
  `build_library`·`cluster`·`label`까지 즉시 import해서 Flask 기동마다 pandas·matplotlib·
  sklearn·umap이 함께 올라왔다. 하위 모듈은 이름을 꺼낼 때만 로드된다.

## model1 모듈 관계

`backend/app.py`와 노트북은 여전히 `from model1 import interior_to_floorplan`으로 쓸 수 있습니다.
`interior_to_floorplan.py`가 아래 모듈들의 공개 이름을 전부 re-export하는 **호환 레이어**이기 때문입니다.

```
interior_to_floorplan.py (호환 레이어, re-export)
  ├─ config.py       # 상수·프롬프트·.env
  ├─ client.py       # _get_client, _call_gemini_with_retry
  ├─ io_utils.py     # 경로·캐시·파서
  ├─ gemini_steps.py # analyze_interior, extract_rule_based_layout, refine_layout …
  ├─ pipeline.py     # run_*_step, convert_image, generate_floorplan_for_web ← backend 진입점
  ├─ render.py       # plot_*_figure, maybe_plot
  └─ cli.py          # main()
```

## 환경변수 (.env)

`.env.example`을 복사해 값을 채웁니다. 모두 루트 `.env` 하나에서 관리:

| 키 | 용도 | 기본값 |
|---|---|---|
| `GEMINI_API_KEY` | Gemini 인증 (필수) | — |
| `GEMINI_ANALYSIS_MODEL` | model1 공간 분석 모델 | `gemini-2.5-flash` |
| `GEMINI_IMAGE_MODEL` | model1 평면도 이미지 모델 | `gemini-2.5-flash-image` |
| `GEMINI_FEATURE_MODEL` | 무드 특징 추출 모델 | `gemini-2.5-flash-lite` |
| `GEMINI_PRODUCT_ICON_MODEL` | 상품 사진 → 아이콘 SVG 모델 | (빈값이면 카테고리별 기본값) |
| `GEMINI_THINKING_BUDGET` | 공간추론 thinking 예산(토큰) | `4096` |
| `GEMINI_LAYOUT_REFINE` | layout 자기교정 패스(0=끄기) | `0` |
| `GEMINI_MOOD_ANALYSIS_ENABLED` | 선택 무드 이미지 Gemini 분석 | `1` |
| `PRODUCT_CLIP_ENABLED` | 상품↔무드 CLIP 유사도 재정렬 | `1` |
| `FLOORPLAN_PROVIDER` | 평면도 생성 경로 선택 | `model2_gemini_svg` |
| `NAVER_CLIENT_ID` / `NAVER_CLIENT_SECRET` | backend 가구 추천(네이버 쇼핑) | — |

## 실행 방법

```bash
# 1) 가상환경 한 번만 (루트에서)
cd interior
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env           # 그리고 GEMINI_API_KEY / NAVER_* 채우기

# 2) 웹 서버 (backend 폴더 안에서 실행 — import ai_backend/product_recommendation이 상대경로라서)
cd backend
python app.py                  # http://127.0.0.1:5000

# 3) 모델 스크립트 (반드시 루트에서 — mood_pipeline import 때문)
cd interior
python -m model1.cli --help                 # 평면도 생성 (구: model1/interior_to_floorplan.py도 동작)
python model2/run_gemini_features.py --help # 무드 특징 추출
python model2/topdown_experiment/run.py room.jpg   # 방 구조 분석 실험

python -m pytest tests

