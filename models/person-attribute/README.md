# Person attribute model

## Provenance

- Model: PULC `person_attribute` (PP-LCNet_x1_0)
- Dataset: PA-100K
- Source project: PaddleClas `v2.6.0`
- Official archive:
  `https://paddleclas.bj.bcebos.com/models/PULC/inference/person_attribute_infer.tar`
- Archive SHA-256:
  `576cc739749021298418e61dfa44362acf427a99e055f302c3f895d638a2bde4`

`prepare_model.sh` downloads and verifies that archive, converts it with
PaddlePaddle 3.3.1 and Paddle2ONNX 2.1.0, and renames tensors to a stable
contract `x [N,3,256,192] FP32 -> scores [N,26] FP32`. The exported graph
already contains a Sigmoid (`sigmoid_in_graph=True`); the router does not
apply another one unless a value falls outside `[0, 1]`.

`build_engine.sh` uses TensorRT 10.14 from Triton 26.01 to build an FP16 engine
for batches 1-16. TensorRT plans are GPU/runtime-specific and must be rebuilt
on the target hardware. The validated Tesla T4 plan has SHA-256
`9eadb6a9f106f3775400f1024c2842caf7978c2f1e4b414c2c7c93b2b14c73cf`.

Generated files under `1/` are excluded from source control.

## Decode

Post-process follows PaddleClas `PersonAttribute`:

- Default threshold `0.5`
- Glasses threshold `0.3`
- Hold-object threshold `0.6`
- Argmax for age, orientation and bag
- One lower-garment label (threshold, argmax fallback)

Published attributes: gender, age, orientation, sleeve, lower garment,
glasses, hat, holding object and bag. Fabric patterns and boots are computed
but not published; they were noise on the operational camera crops.

## Licensing note

PaddleClas source code is Apache-2.0. The official model archive does not
include a separate weights license, and the provider documentation does not
unambiguously grant commercial redistribution rights for the weights.
DeepFrigate therefore downloads the archive during local model preparation
instead of redistributing it. Obtain legal confirmation from PaddlePaddle/Baidu
before distributing the original or converted weights outside the organization.
