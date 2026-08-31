// Converts Ultralytics YOLO26 end-to-end output into DeepStream detections.
#include "nvdsinfer_custom_impl.h"

#include <algorithm>
#include <vector>

extern "C" bool NvDsInferParseYolo26(
    std::vector<NvDsInferLayerInfo> const& output_layers,
    NvDsInferNetworkInfo const& network_info,
    NvDsInferParseDetectionParams const& detection_params,
    std::vector<NvDsInferObjectDetectionInfo>& objects) {
  if (output_layers.size() != 1 || output_layers[0].buffer == nullptr) {
    return false;
  }

  const auto& layer = output_layers[0];
  if (layer.inferDims.numDims != 2 || layer.inferDims.d[1] != 6) {
    return false;
  }

  const auto* detections = static_cast<const float*>(layer.buffer);
  const int count = layer.inferDims.d[0];
  for (int index = 0; index < count; ++index) {
    const float* detection = detections + (index * 6);
    const float confidence = detection[4];
    const int class_id = static_cast<int>(detection[5]);
    if (class_id < 0 ||
        static_cast<unsigned int>(class_id) >=
            detection_params.numClassesConfigured) {
      continue;
    }
    // nvinferserver initializes unspecified per-class thresholds to zero. Use
    // the configured value where present and the detector default otherwise,
    // so the 300 post-NMS slots do not become 300 tracked objects per frame.
    const float configured_threshold =
        detection_params.perClassPreclusterThreshold[class_id];
    const float threshold =
        configured_threshold > 0.0F ? configured_threshold : 0.25F;
    if (confidence < threshold) {
      continue;
    }

    const float left = std::clamp(detection[0], 0.0F, static_cast<float>(network_info.width));
    const float top = std::clamp(detection[1], 0.0F, static_cast<float>(network_info.height));
    const float right = std::clamp(detection[2], left, static_cast<float>(network_info.width));
    const float bottom = std::clamp(detection[3], top, static_cast<float>(network_info.height));
    if (right <= left || bottom <= top) {
      continue;
    }

    NvDsInferObjectDetectionInfo object = {};
    object.classId = class_id;
    object.left = left;
    object.top = top;
    object.width = right - left;
    object.height = bottom - top;
    object.detectionConfidence = confidence;
    objects.push_back(object);
  }
  return true;
}

CHECK_CUSTOM_PARSE_FUNC_PROTOTYPE(NvDsInferParseYolo26);
