FROM python:3.12-slim AS base

WORKDIR /app
ENV PYTHONPATH=/app
COPY services/alpr-bridge/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY services/alpr-bridge/app ./app
COPY contracts/tracked-object-update.schema.json ./contracts/

FROM base AS test
COPY services/alpr-bridge/requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt
COPY services/alpr-bridge/tests ./tests
CMD ["pytest", "-q"]

FROM base AS runtime
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--log-level", "warning"]
