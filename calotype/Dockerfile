# Calotype API image. Build from the calotype/ directory:
#   docker build -t calotype calotype/
# The base image carries Chromium and its system libraries; its tag must match
# the playwright version pinned in requirements.txt.
FROM mcr.microsoft.com/playwright/python:v1.65.2-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PORT=8080 \
    CALOTYPE_DB_PATH=/data/calotype.db

WORKDIR /app

COPY requirements.txt /app/calotype/requirements.txt
RUN pip install -r /app/calotype/requirements.txt

# Application code only; tests and local data stay out (see .dockerignore).
COPY . /app/calotype

# Run as the image's unprivileged user. /data holds the SQLite file; on Cloud
# Run it is ephemeral unless a volume is mounted there. On Cloud Run, set
# CALOTYPE_STORAGE=firestore instead (see README); it needs no file or key.
RUN mkdir -p /data && chown pwuser:pwuser /data
USER pwuser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import os,urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8080\")}/healthz', timeout=4)"

CMD ["python", "-m", "calotype"]
