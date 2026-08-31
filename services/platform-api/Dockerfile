FROM python:3.12-slim AS base

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app

EXPOSE 8080

FROM base AS test
RUN pip install --no-cache-dir pytest
COPY tests ./tests
ENV DATABASE_URL=postgresql://test:test@postgres/test
ENV PYTHONPATH=/app
ENTRYPOINT ["pytest", "-q", "tests"]

FROM base AS runtime
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
