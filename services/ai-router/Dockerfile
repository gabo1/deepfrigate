FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app
COPY services/ai-router/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/ai-router/app ./app
COPY contracts/tracked-object-update.schema.json ./contracts/

FROM base AS test
COPY services/ai-router/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY services/ai-router/tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
CMD ["python", "-m", "app.main"]
