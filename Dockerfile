# Fly.io 배포용 (Render 는 render.yaml 로 충분해 이 파일이 필요 없습니다)
FROM python:3.12-slim

WORKDIR /app
COPY . .

# 설치할 의존성 없음 — 표준 라이브러리만 사용합니다
ENV PORT=8080 \
    HOST=0.0.0.0 \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# NOTION_TOKEN / DASHBOARD_PASSWORD 는 `fly secrets set` 으로 주입합니다.
# 비밀번호가 없으면 dashboard.py 가 시작을 거부합니다 (의도된 동작).
CMD ["python3", "dashboard.py", "--ttl", "120"]
