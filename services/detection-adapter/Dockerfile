FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app
COPY services/detection-adapter/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/detection-adapter/app ./app
COPY contracts/tracked-object-update.schema.json ./contracts/
COPY config/zones.json ./config/

FROM base AS test
COPY services/detection-adapter/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY services/detection-adapter/tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
CMD ["python", "-m", "app.main"]
