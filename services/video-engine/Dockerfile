ARG DEEPSTREAM_IMAGE=nvcr.io/nvidia/deepstream:9.0-triton-multiarch
FROM ${DEEPSTREAM_IMAGE} AS base

RUN pip install --break-system-packages --no-cache-dir \
    PyYAML==6.0.3 \
    jsonschema \
    Pillow==11.3.0

WORKDIR /opt/deepfrigate
ENV PYTHONUNBUFFERED=1

FROM base AS test
RUN pip install --break-system-packages --no-cache-dir pytest
COPY services/video-engine/app ./app
COPY services/video-engine/config ./config
COPY services/video-engine/tests ./tests
COPY contracts ./contracts
ENTRYPOINT ["pytest", "-q", "tests"]

FROM base AS runtime
ENTRYPOINT ["python3", "-m", "app.main"]
