#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."; mkdir -p models
curl -fL -o models/blaze_face_short_range.tflite \
 https://storage.googleapis.com/mediapipe-models/face_detector/blaze_face_short_range/float16/1/blaze_face_short_range.tflite
