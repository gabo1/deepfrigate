# Video engine configuration

This directory will hold the DeepStream 9 source, streammux, nvinferserver and
NvTracker configuration. It is intentionally empty until the exact DeepStream
9 container image has been pulled and its `nvinferserver` protobuf schema is
validated against the selected Triton release.

The first source is `tienda` at 1280x720 H.264 and 10 FPS. Additional cameras
are added through `pipeline.yaml` after the one-camera pipeline produces
tracked metadata.
