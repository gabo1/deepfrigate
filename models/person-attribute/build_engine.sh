#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODEL_DIR="${SCRIPT_DIR}/1"
TRITON_IMAGE="${TRITON_IMAGE:-nvcr.io/nvidia/tritonserver:26.01-py3}"

if [[ ! -f "${MODEL_DIR}/model.onnx" ]]; then
  echo "Missing ${MODEL_DIR}/model.onnx; run prepare_model.sh first." >&2
  exit 1
fi

docker run --rm --gpus all \
  --volume "${MODEL_DIR}:/work" \
  "${TRITON_IMAGE}" \
  /usr/src/tensorrt/bin/trtexec \
    --onnx=/work/model.onnx \
    --saveEngine=/work/model.plan \
    --fp16 \
    --minShapes=x:1x3x256x192 \
    --optShapes=x:8x3x256x192 \
    --maxShapes=x:16x3x256x192 \
    --builderOptimizationLevel=5 \
    --skipInference
