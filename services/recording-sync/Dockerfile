FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app
COPY services/recording-sync/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/recording-sync/app ./app
COPY services/recording-sync/sql ./sql

FROM base AS test
COPY services/recording-sync/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY services/recording-sync/tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
CMD ["python", "-m", "app.main"]
