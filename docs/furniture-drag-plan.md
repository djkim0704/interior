# 평면도 가구 드래그 이동 — 설계 검토

> **상태: 구현 완료.** 이 문서는 착수 전 설계 검토 기록으로 남겨둔 것이며 현행 코드의
> 사양서가 아니다. 실제 구현은 `frontend/static/js/floorplan_drag.js`,
> `frontend/static/js/floorplan_edit.js`, `model2/web_floorplan.py`의
> `prepare_floorplan_edit_markup` / `apply_floorplan_edits_to_layout`,
> 그리고 `backend/app.py`의 `save_floorplan_edit` 라우트에 있다.
> 아래 본문의 줄 번호·함수 이름은 작성 당시 기준이라 지금과 다를 수 있다.

> 목표: 사용자가 생성된 **2D 평면도(SVG)** 위에서 가구를 **드래그해 위치를 옮기고**,
> 옮긴 배치를 저장·재렌더할 수 있게 한다.
>
> 이 문서는 코드 작성 전 **설계 검토**다. 아래 사실은 실제 코드를 확인해 적었다.

---

## 지금 이미 있는 것 (재사용 가능한 기반)

드래그 이동은 "완전 새 기능"이 아니라, **이미 있는 layout 수정 파이프라인에 좌표 변경을 얹는 것**이다.

| 조각 | 현재 상태 | 위치 |
|---|---|---|
| layout 좌표계 | `x, y` 정규화 0~1 (가구 **중심**), `w, h` 비율 | `*_layout.json` |
| 정규화→픽셀 변환 | `cx = x*ROOM_W + MARGIN_X`, `cy = y*ROOM_H + MARGIN_Y` | `rule_based_svg.py` (ROOM_W=860, ROOM_H=560, MARGIN=90) |
| SVG 인라인 삽입 | `{{ svg_markup \| safe }}` → JS로 내부 요소 접근 가능 | `floorplan.html` |
| layout 수정→재렌더 저장 | `create_modified_floorplan()` (유지/제거가 이미 사용) | `backend/app.py:1557` |

즉 **"가구 유지/제거"가 layout을 고쳐 다시 그리는 경로를 이미 갖고 있다.** 드래그는 그 경로에 "좌표 갱신"을 추가하는 형태.

---

## 부족한 것 (새로 만들 4조각)

### ① 렌더러: 가구마다 식별자 부여
- 지금 각 가구 `<g>`에는 `data-source`(detected/product)만 있고 **layout 객체와 잇는 고유 ID가 없다.**
- 드래그하려면 "이 SVG 요소 = layout.objects[i]"라는 연결이 필수.
- **할 일:** 렌더러가 가구 `<g>`에 `data-index="{i}"`(layout objects 배열 인덱스)를 붙이게 한다.
  - ⚠️ `rule_based_svg.py`(dahyun 렌더러)를 건드림 → 기존 렌더 깨지지 않게 **속성 추가만**.

### ② 프론트: 드래그 상호작용 (JS)
- 인라인 SVG의 가구 `<g>`에 마우스 드래그 → 이동.
- **좌표 변환 주의:** 화면 픽셀 이동량을 SVG viewBox 좌표로, 다시 정규화(0~1)로 역변환해야 한다.
  - `norm_x = (svg_x - MARGIN_X) / ROOM_W` (렌더러 공식의 역).
- 방 경계(0~1) 밖으로 못 나가게 clamp.
- **파일:** `floorplan.html` 또는 새 `floorplan_drag.js`.

### ③ 백엔드: 옮긴 좌표 저장 API
- 드래그 결과(어느 가구가 어디로: `{index, x, y}` 목록)를 받아 layout JSON의 해당 객체 `x, y`를 갱신.
- **재사용:** `create_modified_floorplan()` 구조를 확장하거나 유사한 `/update-furniture-position` 라우트 추가.
- 저장 후 재렌더한 SVG를 반환 → 화면 갱신.

### ④ 재렌더 반영
- 저장된 layout으로 `rule_based_svg` 재렌더 → 새 SVG로 화면 교체.
- (선택) 드래그 중엔 프론트에서 즉시 미리보기, 저장 시 서버 재렌더로 확정.

---

## 좌표 변환 규약 (가장 중요 — 틀리면 가구가 엉뚱한 데로)

```
저장(layout) →  화면(SVG):  svg_x = x * ROOM_W + MARGIN_X    (x는 0~1 중심)
화면(SVG)   →  저장(layout): x = (svg_x - MARGIN_X) / ROOM_W  (0~1로 clamp)
ROOM_W = 860, ROOM_H = 560, MARGIN_X = MARGIN_Y = 90 (rule_based_svg.py 기준)
```
> 렌더러 상수가 바뀌면 이 공식도 함께 바뀌어야 한다. JS에 하드코딩하지 말고
> 렌더러가 SVG에 `data-room-w`/`data-room-h`/`data-margin`을 심어 프론트가 읽게 하면 안전.

---

## 단계별 진행 (작은 것부터, 각 단계 검증)

1. **① 식별자** — 렌더러가 가구 `<g>`에 `data-index` 부여. 기존 렌더 무손상 확인.
2. **② 드래그 UI** — 인라인 SVG에서 가구를 잡고 옮기는 JS(저장 없이 화면상 이동만). 좌표 역변환 검증.
3. **③ 저장 API** — 옮긴 좌표를 layout에 반영·저장. `create_modified_floorplan` 재사용.
4. **④ 재렌더** — 저장 후 새 SVG로 교체, 새로고침해도 유지되는지 확인.

> 각 단계는 독립적으로 동작·검증 가능하게 쪼갠다. 1만 해도 "식별자 붙음"을 확인할 수 있고,
> 2까지면 "옮겨는 지는데 저장 안 됨", 3~4에서 영속화.

---

## 주의점

- **dahyun 렌더러·backend를 건드린다** → 유지/제거 등 기존 기능이 안 깨지게 각 단계 검증 필수.
- **좌표 역변환이 핵심 버그 지점.** viewBox 스케일·마진을 정확히 반영해야 가구가 정위치에 놓인다.
- 모바일 터치까지 지원할지는 2단계에서 결정(pointer 이벤트로 하면 마우스·터치 겸용).
- 저장 충돌: 유지/제거로 만든 `modified_layout`과 드래그 좌표가 같은 파일을 고칠 수 있으니 경로·순서 정리.
