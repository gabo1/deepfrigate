#!/usr/bin/env sh
# ReIdentificationNet deployable_v1.0 (NGC, public download, ~96 MB).
set -eu
cd "$(dirname "$0")"
URL="https://api.ngc.nvidia.com/v2/models/nvidia/tao/reidentificationnet/versions/deployable_v1.0/files/resnet50_market1501.etlt"
[ -s resnet50_market1501.etlt ] && { echo "resnet50_market1501.etlt ya existe"; exit 0; }
curl -fL --retry 3 -o resnet50_market1501.etlt "$URL"
ls -la resnet50_market1501.etlt
