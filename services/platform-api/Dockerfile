FROM python:3.12-slim AS base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8080

# Archify (MIT, zero-dependency Node renderer) for /v1/pipelines/diagram.html.
# Pinned commit; the Node binary is copied from the official image (glibc,
# Debian bookworm binary runs on the trixie-based python image).
FROM node:20-bookworm-slim AS archify
ARG ARCHIFY_REPO=https://github.com/tt-a1i/archify.git
ARG ARCHIFY_REF=920543baa1c6137803c5b45a69d8977152773d35
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && git clone --filter=blob:none --no-checkout "$ARCHIFY_REPO" /src \
    && git -C /src checkout --quiet "$ARCHIFY_REF" \
    && mv /src/archify /opt/archify \
    && rm -rf /opt/archify/test /opt/archify/benchmarks /src

FROM base AS test
RUN pip install --no-cache-dir pytest
COPY tests ./tests
ENV DATABASE_URL=postgresql://test:test@postgres/test
ENV PYTHONPATH=/app
ENTRYPOINT ["pytest", "-q", "tests"]

FROM base AS runtime
COPY --from=node:20-bookworm-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=archify /opt/archify /opt/archify
ENV ARCHIFY_DIR=/opt/archify NODE_BIN=/usr/local/bin/node
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
