# 하나의 이미지로 세 컴포넌트를 띄운다. 기본 CMD 는 Control API 이고,
# Worker 는 K8s Deployment 의 command 로 덮어쓴다.
#   Build Worker : ["python", "-m", "app.workers.build_worker"]
#   Deploy Worker: ["python", "-m", "app.workers.deploy_worker"]
#   Migration    : ["alembic", "upgrade", "head"]

FROM python:3.13-slim AS base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# --- builder: 의존성만 /opt/venv 에 설치 ---
FROM base AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.21 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY pyproject.toml uv.lock .python-version ./
# 레포 구성 분석기 wheel(uv path 의존성). 버전·sha256 은 vendor/iris-analyzer-manifest.json
COPY vendor ./vendor
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# --- runner ---
FROM base AS runner
ENV PATH="/opt/venv/bin:$PATH"
WORKDIR /app

RUN groupadd --system --gid 1001 app && \
    useradd --system --uid 1001 --gid app --no-create-home app

COPY --from=builder /opt/venv /opt/venv
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini ./

# K8s runAsNonRoot 가 검증할 수 있도록 숫자 UID 로 지정한다.
USER 1001:1001

EXPOSE 8000
# exec 형식이어야 SIGTERM 이 셸을 거치지 않고 Python 프로세스에 전달된다.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
