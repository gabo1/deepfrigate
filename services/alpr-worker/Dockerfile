# OpenALPR SDK (commercial, Rekor) exposed as a tiny HTTP service for the
# ai-router. The license is mounted at /etc/openalpr/license.conf.
ARG OPENALPR_SDK_IMAGE=openalpr/agent:4.1.13-sdk
FROM ${OPENALPR_SDK_IMAGE} AS base

ENV DEBIAN_FRONTEND=noninteractive PYTHONPATH=/app PIP_ROOT_USER_ACTION=ignore
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir "fastapi==0.115.6" "uvicorn==0.34.0"
WORKDIR /app
COPY services/alpr-worker/app ./app

# Do not start supervisord/alprd from the base image: only our API.
ENTRYPOINT []
EXPOSE 8080
CMD ["python3", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1", "--log-level", "warning"]
