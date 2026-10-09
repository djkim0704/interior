# 인테리어 웹앱 이미지. CI에서는 이 이미지 안에서 pytest를 돌리고, 실행 시에는 Flask 서버를 띄운다.
# Python 3.12: 3.13 이상은 torch·ultralytics 휠이 늦게 올라와 설치가 깨질 수 있다(AGENTS.md 2)
FROM python:3.12-slim

# opencv(ultralytics 의존성)가 libGL·glib 없이 import 단계에서 죽는다
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# torch를 CPU 전용으로 먼저 받는다. 그냥 두면 ultralytics가 CUDA판(수 GB)을 끌고 온다.
# requirements.txt만 먼저 복사해 두면 코드만 바뀐 빌드에서는 이 무거운 층을 캐시로 재사용한다
COPY requirements.txt .
RUN pip install torch torchvision --index-url https://download.pytorch.org/whl/cpu \
    && pip install -r requirements.txt

COPY . .

# app.py가 같은 폴더의 모듈(ai_backend 등)을 바로 import하므로 backend에서 실행한다.
# app.run()은 127.0.0.1에만 열려 컨테이너 밖에서 접속할 수 없어 flask CLI로 0.0.0.0에 띄운다.
# 키(GEMINI_API_KEY 등)는 이미지에 넣지 않고 실행할 때 --env-file .env 로 넘긴다
WORKDIR /app/backend
EXPOSE 5000
CMD ["flask", "--app", "app", "run", "--host", "0.0.0.0", "--port", "5000"]
