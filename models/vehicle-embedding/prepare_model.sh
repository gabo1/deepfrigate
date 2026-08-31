#!/usr/bin/env bash
set -euo pipefail

MODEL_URL="https://paddle-imagenet-models-name.bj.bcebos.com/dygraph/rec/models/inference/PP-ShiTuV2/general_PPLCNetV2_base_pretrained_v1.0_infer.tar"
MODEL_SHA256="b55f63f2f93eb42b0ff232154180f5e54d99da0891a200a2439d7a0a0ef8c026"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

curl --fail --location --retry 3 \
  --output "${WORK_DIR}/model.tar" \
  "${MODEL_URL}"
echo "${MODEL_SHA256}  ${WORK_DIR}/model.tar" | sha256sum --check -
tar -xf "${WORK_DIR}/model.tar" -C "${WORK_DIR}"

python3 -m venv "${WORK_DIR}/venv"
"${WORK_DIR}/venv/bin/pip" install --quiet \
  packaging==26.3 \
  paddlepaddle==3.3.1 \
  paddle2onnx==2.1.0 \
  onnx==1.17.0 \
  onnxruntime==1.29.0 \
  onnx-graphsurgeon==0.6.1 \
  sympy==1.14.0

mkdir -p "${SCRIPT_DIR}/1"
"${WORK_DIR}/venv/bin/paddle2onnx" \
  --model_dir "${WORK_DIR}/general_PPLCNetV2_base_pretrained_v1.0_infer" \
  --model_filename inference.pdmodel \
  --params_filename inference.pdiparams \
  --save_file "${SCRIPT_DIR}/1/model.onnx" \
  --opset_version 13 \
  --enable_onnx_checker True

"${WORK_DIR}/venv/bin/python" - "${SCRIPT_DIR}/1/model.onnx" <<'PY'
import sys

import onnx

model = onnx.load(sys.argv[1])
onnx.checker.check_model(model)
inputs = [(value.name, value.type.tensor_type.elem_type) for value in model.graph.input]
outputs = [(value.name, value.type.tensor_type.elem_type) for value in model.graph.output]
assert inputs == [("x", onnx.TensorProto.FLOAT)], inputs
assert outputs == [("fetch_name_0", onnx.TensorProto.FLOAT)], outputs
print(f"Prepared {sys.argv[1]}")
PY
