FROM python@sha256:00faa2debb87529f9f0764e9491d8ba400a3678976616c3bd7cb193745ac20d1 AS python-base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 65532 autoswe \
    && useradd --uid 65532 --gid 65532 --no-create-home --shell /usr/sbin/nologin autoswe

COPY requirements.lock pyproject.toml README.md ./
RUN python -m pip install --no-deps --requirement requirements.lock

COPY agents ./agents
COPY apps ./apps
COPY domain ./domain
COPY evaluation ./evaluation
COPY execution ./execution
COPY infrastructure ./infrastructure
COPY knowledge ./knowledge
COPY messaging ./messaging
COPY migrations ./migrations
COPY observability ./observability
COPY persistence ./persistence
COPY planning ./planning
COPY policies ./policies
COPY tools ./tools
COPY workflows ./workflows
COPY alembic.ini ./alembic.ini

RUN python -m pip install --no-deps . \
    && python -m compileall -q /app \
    && find /app -type d -name __pycache__ -prune -exec rm -rf '{}' +

USER 65532:65532

FROM python-base AS platform

EXPOSE 8000
CMD ["uvicorn", "apps.api.main:create_production_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]

FROM nginx@sha256:a8b39bd9cf0f83869a2162827a0caf6137ddf759d50a171451b335cecc87d236 AS web

COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY apps/web /usr/share/nginx/html

RUN chown -R nginx:nginx /usr/share/nginx/html /etc/nginx/conf.d

USER nginx
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
