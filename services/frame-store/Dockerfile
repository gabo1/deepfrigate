FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app
COPY services/frame-store/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/frame-store/app ./app
COPY contracts/frame-ref.schema.json ./contracts/

FROM base AS test
COPY services/frame-store/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY services/frame-store/tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
EXPOSE 8080
HEALTHCHECK --interval=5s --timeout=3s --retries=5 CMD \
  python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/healthz', timeout=2)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
