# Fly.io 배포용 (Render 는 render.yaml 로 충분해 이 파일이 필요 없습니다)
FROM python:3.12-slim

# git — REALTIME_SYNC=1 일 때 build/sync_realtime_data.py 가 자동으로
# commit·push 하기 위해 필요합니다 (그 외에는 사용하지 않음)
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY . .

# 파이썬 의존성 없음 — 표준 라이브러리만 사용합니다
ENV PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# NOTION_TOKEN / DASHBOARD_PASSWORD 는 `fly secrets set` 으로 주입합니다.
# 비밀번호가 없으면 dashboard.py 가 시작을 거부합니다 (의도된 동작).
CMD ["python3", "dashboard.py", "--ttl", "120"]
