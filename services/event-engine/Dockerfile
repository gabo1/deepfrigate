FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app
COPY services/event-engine/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/event-engine/app ./app
COPY services/event-engine/sql ./sql
COPY contracts/tracked-object-update.schema.json ./contracts/
COPY contracts/event.schema.json ./contracts/
COPY config/zones.json ./config/zones.json

FROM base AS test
COPY services/event-engine/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY services/event-engine/tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
CMD ["python", "-m", "app.main"]
