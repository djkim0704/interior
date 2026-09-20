# CLAUDE.md

이 저장소의 설치·실행 방법과 주의사항은 [AGENTS.md](AGENTS.md)에 정리되어 있다.
작업을 시작하기 전에 그 문서를 먼저 읽을 것.

특히 다음 항목은 실수하기 쉬우니 꼭 확인한다.

- **Gemini 무료 등급 한도** — flash 계열은 하루 20회뿐이다. 평면도 1장에
  1~2회를 쓰므로, 디버깅하며 반복 실행하면 금방 소진된다. 확인용 API 호출을
  함부로 늘리지 말 것. (AGENTS.md 7.1)
- **SVG의 ID 계약** — 평면도 SVG의 `<g id="bed_1">`이 배치 JSON의 id와
  어긋나면 화면은 멀쩡한데 "수정하기"만 조용히 죽는다. (AGENTS.md 7.2)
- **`.env`는 자동 리로드되지 않는다** — 값을 바꾸면 서버를 재시작해야 한다.
  `.py`는 저장만 하면 반영된다. (AGENTS.md 3)
- **캐시·세션** — 코드를 고쳐도 결과가 안 바뀌면 캐시나 세션을 의심한다.
  (AGENTS.md 7.5)

## 실행

```bash
venv\Scripts\python.exe backend/app.py     # Windows
venv/bin/python backend/app.py             # macOS / Linux
```

## 코드 스타일

`backend/app.py`는 한 줄을 짧게 끊어 쓰는 독특한 포매팅을 따른다. 기존 코드를
수정할 때는 주변 스타일에 맞추고, 전체 재포매팅은 하지 않는다.

주석은 한국어로 쓰되, **무엇을 하는지가 아니라 왜 그렇게 했는지**를 남긴다.
