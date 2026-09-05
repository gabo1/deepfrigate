# Vehicle attribute model

## Provenance

- Model: PULC `vehicle_attribute` (PP-LCNet_x1_0)
- Dataset: VeRi
- Source project: PaddleClas `v2.6.0`
- Official archive:
  `https://paddleclas.bj.bcebos.com/models/PULC/inference/vehicle_attribute_infer.tar`
- Archive SHA-256:
  `bba326342be959292ec628473d82a0dd9cea43d115e3b4364b4244a0fd104c8b`

`prepare_model.sh` downloads and verifies that archive, converts it with
PaddlePaddle 3.3.1 and Paddle2ONNX 2.1.0, and renames tensors to a stable
contract `x [N,3,192,256] FP32 -> scores [N,19] FP32`. The official
post-process applies Sigmoid; the router does the same when a value falls
outside `[0, 1]`.

`build_engine.sh` uses TensorRT 10.14 from Triton 26.01 to build an FP16 engine
for batches 1-16. TensorRT plans are GPU/runtime-specific and must be rebuilt
on the target hardware. The validated Tesla T4 plan has SHA-256
`f8cb5cf93ecc6262db8a5c8586352db413b2bec5164c2b1fbe2612d03b4d1e2f`.

Generated files under `1/` are excluded from source control.

## Decode

Post-process follows PaddleClas `VehicleAttribute`:

- Color: argmax of scores 0-9, threshold `0.5`
- Body type: argmax of scores 10-18, threshold `0.5`
- Below threshold: the field is omitted (not published as `unknown`)

Published attributes: `color` (yellow, orange, green, gray, red, blue,
white, golden, brown, black) and `body_type` (sedan, suv, van, hatchback,
mpv, pickup, bus, truck, estate). Persist in
`event.data.vehicle_attributes`. This is not make/model.

Input is landscape `[3,192,256]` (the person model is portrait
`[3,256,192]`). Do not reuse the person preprocess.

## Licensing note

PaddleClas source code is Apache-2.0. The official model archive does not
include a separate weights license, and the provider documentation does not
unambiguously grant commercial redistribution rights for the weights.
DeepFrigate therefore downloads the archive during local model preparation
instead of redistributing it. Obtain legal confirmation from PaddlePaddle/Baidu
before distributing the original or converted weights outside the organization.
