#!/bin/bash
set -euo pipefail

runner="${1:?ssv-runner path is required}"
label_map_path="${2:?label map path is required}"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT

assert_fatal() {
    local output="$1"
    local status="$2"
    local expected_status="$3"
    local expected_stage="$4"
    local expected_source_id="$5"

    if [ "$status" -ne "$expected_status" ]; then
        printf '%s\n' "$output" >&2
        echo "expected exit code $expected_status, got $status" >&2
        exit 1
    fi
    if [ "$(grep -c '^event=fatal_error ' <<<"$output" || true)" -ne 1 ]; then
        printf '%s\n' "$output" >&2
        echo "expected exactly one fatal_error event" >&2
        exit 1
    fi
    for field in \
        "exit_code=$expected_status" \
        "stage=$expected_stage" \
        "source_id=$expected_source_id" \
        'error='; do
        if ! grep -Fq "$field" <<<"$output"; then
            printf '%s\n' "$output" >&2
            echo "fatal_error is missing $field" >&2
            exit 1
        fi
    done
    if [ "$(grep '^event=' <<<"$output" | tail -n 1)" \
        != "$(grep '^event=fatal_error ' <<<"$output")" ]; then
        printf '%s\n' "$output" >&2
        echo "fatal_error must be the final stable event" >&2
        exit 1
    fi
}

write_config() {
    local config_path="$1"
    local source_id="$2"
    local model_path="$3"

    cat >"$config_path" <<YAML
version: "2.0"
sources:
  - id: $source_id
    uri: rtsp://127.0.0.1/test
    codec: h264
    event_rule:
      event_type: person_without_helmet
      severity: high
      rule_id: test-rule
      rule_version: test-v1
      rule_facts:
        subject: person
    decode:
      mode: auto
      device: auto
display:
  enabled: false
inference:
  enabled: true
  model:
    path: "$model_path"
    family: yolo
    output_format: yolov8
    label_map: "$label_map_path"
    preprocess:
      color_order: rgb
      resize: letterbox
      normalization:
        scale: 0.00392156862745098
        mean: [0.0, 0.0, 0.0]
        std: [1.0, 1.0, 1.0]
  runtime:
    type: onnxruntime
    providers:
      mode: auto
YAML
}

config_path="$tmp_dir/model-error.yaml"
write_config \
    "$config_path" \
    "cli-model-error" \
    "$tmp_dir/missing-wrapper.onnx"

set +e
output="$("$runner" --config "$config_path" --headless 2>&1)"
status=$?
set -e
assert_fatal "$output" "$status" 4 inference.start cli-model-error

raw_model_path="$tmp_dir/raw-float-nchw.onnx"
raw_model_base64='CAoSCHNzdi10ZXN0OnAKGgoGaW1hZ2VzEgZvdXRwdXQiCElkZW50aXR5Eg5yYXctZmxvYXQtbmNod1ogCgZpbWFnZXMSFgoUCAESEAoCCAEKAggDCgIIAgoCCANiIAoGb3V0cHV0EhYKFAgBEhAKAggBCgIIAwoCCAIKAggDQgQKABAS'
printf '%s' "$raw_model_base64" | base64 --decode >"$raw_model_path"

raw_config_path="$tmp_dir/raw-model-error.yaml"
write_config "$raw_config_path" "cli-raw-model" "$raw_model_path"

set +e
raw_output="$("$runner" --config "$raw_config_path" --headless 2>&1)"
raw_status=$?
set -e
assert_fatal \
    "$raw_output" \
    "$raw_status" \
    4 \
    inference.start \
    cli-raw-model

set +e
invalid_output="$("$runner" --unknown-option 2>&1)"
invalid_status=$?
set -e
assert_fatal "$invalid_output" "$invalid_status" 2 cli unresolved
