#!/usr/bin/env bash
set -euo pipefail

MODEL_URL="https://paddleclas.bj.bcebos.com/models/PULC/inference/person_attribute_infer.tar"
MODEL_SHA256="576cc739749021298418e61dfa44362acf427a99e055f302c3f895d638a2bde4"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORK_DIR="$(mktemp -d)"
trap 'rm -rf "${WORK_DIR}"' EXIT

curl --fail --location --retry 3 \
  --output "${WORK_DIR}/model.tar" \
  "${MODEL_URL}"
echo "${MODEL_SHA256}  ${WORK_DIR}/model.tar" | sha256sum --check -
tar -xf "${WORK_DIR}/model.tar" -C "${WORK_DIR}"

MODEL_DIR="$(find "${WORK_DIR}" -name 'inference.pdmodel' -printf '%h\n' | head -n 1)"
if [[ -z "${MODEL_DIR}" ]]; then
  echo "inference.pdmodel not found in archive" >&2
  exit 1
fi

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
  --model_dir "${MODEL_DIR}" \
  --model_filename inference.pdmodel \
  --params_filename inference.pdiparams \
  --save_file "${SCRIPT_DIR}/1/model.onnx" \
  --opset_version 13 \
  --enable_onnx_checker True

"${WORK_DIR}/venv/bin/python" - "${SCRIPT_DIR}/1/model.onnx" <<'PY'
import sys

import numpy as np
import onnx
import onnx_graphsurgeon as gs

path = sys.argv[1]
model = onnx.load(path)
onnx.checker.check_model(model)
graph = gs.import_onnx(model)
if len(graph.inputs) != 1 or len(graph.outputs) != 1:
    raise SystemExit(f"expected 1 input and 1 output, got {graph.inputs} {graph.outputs}")

graph.inputs[0].name = "x"
graph.outputs[0].name = "scores"
for tensor in graph.inputs + graph.outputs:
    tensor.dtype = np.float32

has_sigmoid = any(node.op == "Sigmoid" for node in graph.nodes)
onnx.save(gs.export_onnx(graph), path)
model = onnx.load(path)
onnx.checker.check_model(model)
inputs = [value.name for value in model.graph.input]
outputs = [value.name for value in model.graph.output]
assert inputs == ["x"], inputs
assert outputs == ["scores"], outputs
print(f"Prepared {path} sigmoid_in_graph={has_sigmoid}")
PY
