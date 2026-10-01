# ---- 1) build the web console (ui/ -> app/static/ui)
FROM node:20-alpine AS ui
WORKDIR /src/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN mkdir -p /src/app/static && npm run build

# ---- 2) the API
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY --from=ui /src/app/static/ui ./app/static/ui
# /app/cache holds downloaded Parquet files (a Docker volume; created here so the volume
# belongs to the non-root user)
RUN useradd --system --uid 10001 api && mkdir -p /app/cache && chown -R api /app
USER api

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz').status==200 else 1)"

# WORKERS (default 2): each worker runs its own searches; DuckDB memory is per search.
CMD ["sh", "-c", "exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080 --workers ${WORKERS:-2} --proxy-headers --no-server-header"]
