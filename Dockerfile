FROM python:3.12-slim

# Patch the base image's own OS packages before installing anything else —
# python:3.12-slim's Debian package snapshot lags behind Debian's security
# updates, which is what Trivy's docker-security CI job (.github/workflows/
# security.yml) flags as OS-level HIGH/CRITICAL CVEs even though none of our
# own code or Python dependencies are involved. Re-run whenever that job
# starts failing again with new CVEs in util-linux/bsdutils/login etc.
RUN apt-get update && apt-get upgrade -y && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY alembic ./alembic
COPY docker/entrypoint.sh ./entrypoint.sh

RUN pip install --no-cache-dir . \
    && useradd --create-home --shell /usr/sbin/nologin hermes \
    && chmod +x ./entrypoint.sh \
    && chown -R hermes:hermes /app

USER hermes

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)" || exit 1

ENTRYPOINT ["./entrypoint.sh"]