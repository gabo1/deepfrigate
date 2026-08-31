#!/usr/bin/env bash
set -euo pipefail

# Build inside the same Triton/TensorRT release used at runtime. TensorRT plans
# are GPU- and runtime-specific, so never commit the generated artifact.
MODEL_NAME="${1:-yolo26s.pt}"
OUTPUT_DIR="${2:-models/object-detector/1}"

mkdir -p "${OUTPUT_DIR}"
echo "Export ${MODEL_NAME} to ONNX with end-to-end NMS, then build model.plan in the Triton image"
echo "The exact command is added after the NGC DeepStream image is available for compatibility validation"
