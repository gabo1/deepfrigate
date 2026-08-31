# Vehicle embedding model

## Provenance

- Model: PP-ShiTuV2 `general_PPLCNetV2_base_pretrained_v1.0`
- Architecture: PPLCNetV2 base with BNNeck
- Source project: PaddleClas `v2.6.0`
- Source commit: `1cde9bce0c1c0ed8dce87d11fcb039c6e3502d03`
- Official archive:
  `https://paddle-imagenet-models-name.bj.bcebos.com/dygraph/rec/models/inference/PP-ShiTuV2/general_PPLCNetV2_base_pretrained_v1.0_infer.tar`
- Archive SHA-256:
  `b55f63f2f93eb42b0ff232154180f5e54d99da0891a200a2439d7a0a0ef8c026`

`prepare_model.sh` downloads and verifies that archive, then converts it with
PaddlePaddle 3.3.1 and Paddle2ONNX 2.1.0. The resulting ONNX contract is
`x [N,3,224,224] FP32 -> fetch_name_0 [N,512] FP32`.

`build_engine.sh` uses TensorRT 10.14 from Triton 26.01 to build an FP16 engine
for batches 1-32. TensorRT plans are GPU/runtime-specific and must be rebuilt
on the target hardware. The validated Tesla T4 plan has SHA-256
`34c178afd625373727258af610024888a752d61ac2c214e749eda4aca219917f`.

Both generated files under `1/` are excluded from source control.

## Validation

The T4 FP16 output was compared with ONNX Runtime FP32 using the same fixed
input. Cosine similarity after L2 normalization was `0.999982`.

Run the repeatable Triton batch sweep with:

```bash
docker run --rm --network host \
  -v "$PWD/models/vehicle-embedding/benchmark.py:/benchmark.py:ro" \
  deepfrigate-ai-router python /benchmark.py
```

Run `validate_similarity.py` after collecting live samples in Qdrant. It checks
that byte-identical crops produce consistent vectors and reports the score
distribution for distinct crops. Distinct pixels are not ground-truth distinct
vehicles, so this check cannot establish an identity threshold.

## Licensing note

PaddleClas source code is Apache-2.0. The official model archive does not
include a separate weights license, and the provider documentation does not
unambiguously grant commercial redistribution rights for the weights.
DeepFrigate therefore downloads the archive during local model preparation
instead of redistributing it. Obtain legal confirmation from PaddlePaddle/Baidu
before distributing the original or converted weights outside the organization.
