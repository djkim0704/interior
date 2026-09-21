# 인테리어 프로젝트

사진 → 평면도/무드 분석(Gemini) → 인테리어 추천을 제공하는 Flask 풀스택 웹앱.

```
interior/
├── frontend/      # 화면 (Jinja2 템플릿 + static css/js) — Flask가 렌더링
├── backend/       # Flask 서버 (app.py = API + 화면 + 세션 한 곳에)
├── model1/        # 사진 → 2D 평면도 (Gemini) — 폴백 경로, 역할별 모듈로 분리
├── model2/        # 사진 → 평면도 SVG (web_floorplan.py = 현재 기본) + 무드 특징 추출
├── mood_pipeline/ # 루트 공용 패키지 (config, gemini_extract, rule_based_svg)
├── mood_search_v1/ # 무드 검색 (CLIP) — backend /mood-search 가 쓰는 구현
├── prompts/       # 모든 Gemini 프롬프트(*.txt) — model1·mood_pipeline 공유
├── tests/         # unittest (루트에서 실행)
├── images/ data/ mood_library/ output/   # 데이터·산출물
├── requirements.txt        # 통합 의존성 (가상환경 하나)
└── .env                    # API 키 (.env.example 복사해서 작성)
```

> **구조는 Flask 하나로 프론트+백엔드를 다 처리하는 풀스택 모놀리식**입니다.
> `app.py`를 실행하면 API와 화면이 함께 뜨며, 프론트를 따로 실행하지 않습니다.
> (React처럼 별도 프론트 서버 없음 → Docker도 불필요)

---

## 1. 준비

`.env` 파일에 API 키를 넣습니다.
```powershell
Copy-Item .env.example .env
# .env 를 열어 GEMINI_API_KEY / NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 채우기
```
- `GEMINI_API_KEY` — https://aistudio.google.com/apikey (model1/model2 필수)
- `NAVER_CLIENT_ID/SECRET` — https://developers.naver.com/apps (가구 추천용, 없으면 그 기능만 비활성)
- 모델명·옵션(`GEMINI_ANALYSIS_MODEL`, `GEMINI_THINKING_BUDGET`, `GEMINI_LAYOUT_REFINE` 등)은 선택 —
  `.env`에 넣으면 코드 수정 없이 덮어쓸 수 있습니다. 자세한 목록은 [REPO_STRUCTURE.md](REPO_STRUCTURE.md#환경변수-env).

> **가상환경(venv)은 git에 포함하지 않습니다.** 아래 방법으로 각자 생성하세요.

---

## 2. 실행 (Windows / PowerShell)

```powershell
cd interior
python -m venv venv                 # 최초 1회
venv\Scripts\activate
pip install -r requirements.txt     # 최초 1회 (torch 등, 수 분)

# 웹 서버 실행
cd backend
python app.py
```
> `activate`에서 실행 차단 오류 시 1회: `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

---

## 3. 접속 / 종료

- 브라우저: **http://127.0.0.1:5000**
- 종료: 터미널에서 `Ctrl + C`

---

## 4. 모델 스크립트 (CLI)

웹앱과 별개로 모델을 직접 돌릴 때. **반드시 루트에서** 실행 (mood_pipeline import 때문).
가상환경은 위에서 만든 것을 activate한 상태로.

```powershell
# 사진 → 평면도 (구 경로 python model1\interior_to_floorplan.py 도 그대로 동작)
python -m model1.cli --help

# 무드 특징 추출 + UMAP
python model2/run_gemini_features.py --help

```

---

## 참고

- 무드 검색(프롬프트→유사 이미지 CLIP 검색)은 `mood_search_v1/search.py` 구현이 backend `/mood-search`에서 쓰입니다.
  (같은 기능의 옛 사본이던 `mood_pipeline/search.py`는 삭제됨)
- `model2/notebooks/06_gemini_features.ipynb` 는 실험용이며 **현재 실행 불가**입니다.
  없는 모듈(`mood_pipeline/prompt_floorplan.py`)과 없는 함수(`resolve_recommended_image`,
  `render_rule_based_floorplan`, `plot_rule_based_floorplan`), 없는 파일(`requirements-ml.txt`,
  `requirements-gemini.txt`)에 의존합니다. 되살리려면 셀을 새로 써야 합니다.
- 테스트: 루트에서 `python -m pytest tests` 또는 `set PYTHONPATH=. && python tests\test_floorplan_edit.py`
  (`tests/`의 일부 파일은 sys.path 설정이 없어 루트가 import 경로에 있어야 합니다.)
- 실행 규칙 요약: **웹 서버는 `backend/`에서, 모델 스크립트는 루트에서.**
- 자세한 재구조화 기록은 [REPO_STRUCTURE.md](REPO_STRUCTURE.md) 참고.
