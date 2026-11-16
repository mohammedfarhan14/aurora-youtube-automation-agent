# syntax=docker/dockerfile:1
# API and lightweight workers use the base image; the media worker opts into FFmpeg.
FROM python:3.12-slim

ARG INSTALL_FFMPEG=false
ARG INSTALL_CHROMIUM=false

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/backend/api

WORKDIR /app

# Keep media executables independent of application-source changes so
# persistent BuildKit caches can reuse this layer. Chromium is isolated to
# the media image, so API and lightweight worker images remain small.
RUN if [ "$INSTALL_FFMPEG" = "true" ] || [ "$INSTALL_CHROMIUM" = "true" ]; then \
      apt-get update \
      && if [ "$INSTALL_FFMPEG" = "true" ]; then \
           apt-get install --no-install-recommends -y ffmpeg \
           && ffmpeg -hide_banner -encoders | grep -q libx264 \
           && ffprobe -version >/dev/null; \
         fi \
      && if [ "$INSTALL_CHROMIUM" = "true" ]; then \
           apt-get install --no-install-recommends -y chromium fonts-dejavu-core \
           && chromium --version; \
         fi \
      && rm -rf /var/lib/apt/lists/*; \
    fi

COPY backend/api/requirements.lock /app/requirements.lock

RUN python -m pip install --no-cache-dir --require-hashes -r /app/requirements.lock

COPY backend/api/pyproject.toml /app/backend/api/pyproject.toml
COPY backend/api/app /app/backend/api/app
COPY backend/api/migrations /app/backend/api/migrations
COPY backend/api/alembic.ini /app/backend/api/alembic.ini
COPY infrastructure/docker/entrypoint.py /app/entrypoint.py

RUN mkdir -p /app/backend/api/var/media \
    && groupadd --system aurora \
    && useradd --system --gid aurora --home-dir /app --no-create-home aurora \
    && chown -R aurora:aurora /app

WORKDIR /app/backend/api
USER aurora

EXPOSE 8000

ENTRYPOINT ["python", "/app/entrypoint.py"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
