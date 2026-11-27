
FROM node:22-slim AS dashboard

WORKDIR /ui
# manifests first so the dependency layer caches across UI-only changes
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci
COPY dashboard/ ./
ENV VITE_BASE=/app/ \
    VITE_API_TARGET=""
RUN npm run build


FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# copy metadata first so the dependency layer caches across code-only changes
COPY pyproject.toml README.md ./
COPY app ./app
COPY migrations ./migrations
COPY scripts ./scripts

# install the project (and the optional tracing extra) from the pinned pyproject
RUN pip install ".[observability]"

# The crawl engine's browser plus the shared libraries it links against.
# `--with-deps` is the apt half; skipping it yields a Chromium that starts and
# then dies on a missing libnss3. The version comes from the crawl4ai pin, so
# image and library cannot drift apart.
RUN playwright install --with-deps chromium

# the built SPA, served by the API at /app
COPY --from=dashboard /ui/dist ./dashboard/dist

# Cloud Run mounts the Secret Manager payload at /app/secrets/.env. Create the
# parent here so the mount target is a path the platform can write into without
# relying on it creating intermediate directories. An empty directory is the
# safe case: Cloud Run's documented caveat is that a mount hides what is already
# at the path, and there is nothing here to hide.
RUN mkdir -p /app/secrets

# scale-to-zero platforms inject $PORT; default to 8000 for local `docker run`
ENV PORT=8000
EXPOSE 8000

# validate_runtime() runs at startup inside the lifespan and fails fast on a bad
# adapter/secret combination, so a misconfigured container exits rather than
# serving in an unsafe state.
CMD ["sh", "-c", "uvicorn app.api.main:app --host 0.0.0.0 --port ${PORT}"]
