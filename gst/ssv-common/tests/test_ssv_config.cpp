#include "ssv_config.hpp"

#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <span>
#include <string>
#include <string_view>
#include <utility>
#include <variant>
#include <vector>

namespace {

class ScopedEnvironmentVariable {
public:
    explicit ScopedEnvironmentVariable(std::string name)
        : name_(std::move(name))
    {
        if (const char *value = std::getenv(name_.c_str())) {
            had_value_ = true;
            original_value_ = value;
        }
        unsetenv(name_.c_str());
    }

    ~ScopedEnvironmentVariable()
    {
        if (had_value_)
            setenv(name_.c_str(), original_value_.c_str(), 1);
        else
            unsetenv(name_.c_str());
    }

    ScopedEnvironmentVariable(const ScopedEnvironmentVariable &) = delete;
    ScopedEnvironmentVariable &operator=(
        const ScopedEnvironmentVariable &) = delete;

private:
    std::string name_;
    bool had_value_ = false;
    std::string original_value_;
};

class ScopedConfigEnvironment {
public:
    ScopedConfigEnvironment()
        : original_directory_(std::filesystem::current_path())
    {
        char path[] = "/tmp/ssv-config-test-XXXXXX";
        const char *created = mkdtemp(path);
        assert(created != nullptr);
        temporary_directory_ = created;
        std::filesystem::current_path(temporary_directory_);
    }

    ~ScopedConfigEnvironment()
    {
        std::filesystem::current_path(original_directory_);
        std::filesystem::remove_all(temporary_directory_);
    }

    std::filesystem::path write(
        std::string_view name,
        std::string_view contents) const
    {
        const auto path = temporary_directory_ / name;
        std::ofstream(path) << contents;
        return path;
    }

private:
    ScopedEnvironmentVariable config_path_ { "SSV_CONFIG_PATH" };
    ScopedEnvironmentVariable rtsp_url_ { "SSV_RTSP_URL" };
    ScopedEnvironmentVariable redis_host_ { "REDIS_HOST" };
    ScopedEnvironmentVariable redis_port_ { "REDIS_PORT" };
    std::filesystem::path original_directory_;
    std::filesystem::path temporary_directory_;
};

void expect_config_error(
    std::string_view yaml,
    ssv::SsvConfigErrorKind expected_kind,
    std::string_view expected_path)
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("invalid.yaml", yaml);
    try {
        static_cast<void>(ssv::ssv_config_load(path.string()));
        assert(false && "invalid configuration was accepted");
    } catch (const ssv::SsvConfigError &error) {
        assert(error.kind() == expected_kind);
        assert(error.path() == expected_path);
    }
}

void expect_cli_error(
    std::initializer_list<std::string_view> arguments,
    ssv::SsvConfigErrorKind expected_kind,
    std::string_view expected_path)
{
    const std::vector<std::string_view> values(arguments);
    try {
        static_cast<void>(ssv::ssv_run_options_parse(values));
        assert(false && "invalid CLI arguments were accepted");
    } catch (const ssv::SsvConfigError &error) {
        assert(error.kind() == expected_kind);
        assert(error.path() == expected_path);
    }
}

void test_loads_complete_config()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("complete.yaml", R"yaml(
version: "2.0"
logging:
  cpp_debug_level: "ssv*:5"
  python_log_level: "DEBUG"
redis:
  host: "redis.internal"
  port: 6380
  db: 2
  stream_key: "custom:events"
  consumer_group: "custom-agent"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1:8554/test"
    codec: "h264"
    protocols: "tcp"
    latency_ms: 120
    decode:
      mode: "vaapi"
      device: "drm:/dev/dri/renderD128"
display:
  enabled: true
  backend: "gtkglsink"
  fps: 25
  gl_backend: "wayland"
  overlay:
    enabled: true
    font:
      face: "bold"
      size: 14
    motion_prediction:
      enabled: true
      max_horizon_ms: 250
inference:
  enabled: true
  analysis_fps: 0
  model:
    path: "models/model.onnx"
    family: "yolo"
    output_format: "yolov8"
    label_map: "config/model-labels/coco80.txt"
    preprocess:
      color_order: "rgb"
      resize: "letterbox"
      normalization:
        scale: 0.00392156862745098
        mean: [0.0, 0.0, 0.0]
        std: [1.0, 1.0, 1.0]
  runtime:
    type: "onnxruntime"
    providers:
      mode: "explicit"
      order: ["openvino", "cpu"]
    device_id: 1
    precision: "fp16"
    cpu_threads: 4
    cache:
      enabled: true
      directory: ""
  confidence_threshold: 0.6
  target_class: "person"
tracking:
  enabled: true
  track_threshold: 0.55
  track_buffer: 45
  match_threshold: 0.35
  mock_track: false
  publish_cooldown_ms: 12000
  gmc:
    method: "sparse-opt-flow"
    downscale: 3
agent:
  state_machine_timeout: 240
  max_retries: 5
  model_name: "gpt-test"
  output_dir: "outputs-test"
  dedup_enabled: false
  dedup_cooldown_seconds: 12.5
  knowledge:
    backend: "qdrant"
    qdrant_path: "/var/lib/ssv/qdrant"
    min_score: 0.65
)yaml");

    const auto config = ssv::ssv_config_load(path.string());

    assert(config.version == "2.0");
    assert(config.logging.cpp_debug_level == "ssv*:5");
    assert(config.redis.host == "redis.internal");
    assert(config.sources.size() == 1);
    assert(config.sources.front().id == "camera-01");
    assert(config.sources.front().codec == "h264");
    assert(config.sources.front().decode.mode == ssv::SsvDecodeMode::Vaapi);
    assert(config.sources.front().decode.device.kind ==
        ssv::SsvDecodeDeviceKind::Drm);
    assert(config.sources.front().decode.device.value ==
        "/dev/dri/renderD128");
    assert(config.display.backend == ssv::SsvDisplayBackend::GtkGlSink);
    assert(config.display.overlay.font.size == 14);
    assert(config.inference.analysis_fps == 0);
    assert(std::holds_alternative<ssv::SsvOnnxRuntimeConfig>(
        config.inference.runtime));
    const auto &runtime = std::get<ssv::SsvOnnxRuntimeConfig>(
        config.inference.runtime);
    assert(runtime.providers.mode == ssv::SsvProviderMode::Explicit);
    assert(runtime.providers.order.size() == 2);
    assert(runtime.providers.order.front() == ssv::SsvProvider::OpenVino);
    assert(runtime.cpu_threads == 4);
    assert(runtime.cache.directory.empty());
    assert(config.inference.model.preprocess.has_value());
    assert(config.inference.model.preprocess->color_order ==
        ssv::SsvInputColorOrder::Rgb);
    assert(config.inference.model.preprocess->resize_mode ==
        ssv::SsvResizeMode::Letterbox);
    assert(config.inference.model.preprocess->execution ==
        ssv::SsvPreprocessExecution::Auto);
    assert(config.inference.model.preprocess->normalization.scale
        == 0.00392156862745098F);
    assert(config.inference.model.preprocess->normalization.mean[0] == 0.0F);
    assert(config.inference.model.preprocess->normalization.mean[1] == 0.0F);
    assert(config.inference.model.preprocess->normalization.mean[2] == 0.0F);
    assert(config.inference.model.preprocess->normalization.std[0] == 1.0F);
    assert(config.inference.model.preprocess->normalization.std[1] == 1.0F);
    assert(config.inference.model.preprocess->normalization.std[2] == 1.0F);
    assert(config.tracking.gmc.method ==
        ssv::SsvGmcMethod::SparseOpticalFlow);
    assert(config.tracking.publish_cooldown_ms == 12000);
    assert(config.agent.max_retries == 5);
    assert(config.agent.model_name == "gpt-test");
    assert(config.agent.output_dir == "outputs-test");
    assert(!config.agent.dedup_enabled);
    assert(config.agent.dedup_cooldown_seconds == 12.5F);
}

void test_loads_example_config(std::string_view path)
{
    const auto config = ssv::ssv_config_load(std::string(path));

    assert(config.version == "2.0");
    assert(config.sources.size() == 1);
    assert(!config.sources.front().id.empty());
    assert(config.tracking.publish_cooldown_ms == 30000);
    assert(config.agent.dedup_enabled);
    assert(config.agent.dedup_cooldown_seconds == 30.0F);
}

void test_parses_explicit_preprocess_semantics()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("preprocess.yaml", R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    preprocess:
      color_order: "bgr"
      resize: "stretch"
      execution: "cpu"
      normalization:
        scale: 0.01
        mean: [0.1, 0.2, 0.3]
        std: [0.9, 1.0, 1.1]
)yaml");

    const auto config = ssv::ssv_config_load(path.string());
    assert(config.inference.model.preprocess.has_value());
    const auto &preprocess = *config.inference.model.preprocess;
    assert(preprocess.color_order == ssv::SsvInputColorOrder::Bgr);
    assert(preprocess.resize_mode == ssv::SsvResizeMode::Stretch);
    assert(preprocess.execution == ssv::SsvPreprocessExecution::Cpu);
    assert(preprocess.normalization.scale == 0.01F);
    assert(preprocess.normalization.mean[0] == 0.1F);
    assert(preprocess.normalization.mean[1] == 0.2F);
    assert(preprocess.normalization.mean[2] == 0.3F);
    assert(preprocess.normalization.std[0] == 0.9F);
    assert(preprocess.normalization.std[1] == 1.0F);
    assert(preprocess.normalization.std[2] == 1.1F);
}

void test_requires_preprocess_when_inference_is_enabled()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    path: "models/raw.onnx"
)yaml",
        ssv::SsvConfigErrorKind::MissingRequired,
        "inference.model.preprocess");
}

void test_parses_cuda_preprocess_execution()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("preprocess-cuda.yaml", R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    preprocess:
      color_order: "rgb"
      resize: "letterbox"
      execution: "cuda"
      normalization:
        scale: 1.0
        mean: [0.0, 0.0, 0.0]
        std: [1.0, 1.0, 1.0]
)yaml");

    const auto config = ssv::ssv_config_load(path.string());
    assert(config.inference.model.preprocess->execution ==
        ssv::SsvPreprocessExecution::Cuda);
}

void test_rejects_invalid_preprocess_fields()
{
    struct Case {
        std::string_view field;
        std::string_view path;
    };
    const Case cases[] = {
        {"      color_order: \"rgb\"\n"
         "      resize: \"letterbox\"\n"
         "      execution: \"metal\"\n"
         "      normalization:\n"
         "        scale: 0.00392156862745098\n"
         "        mean: [0.0, 0.0, 0.0]\n"
         "        std: [1.0, 1.0, 1.0]\n",
            "inference.model.preprocess.execution"},
        {"      color_order: \"rgb\"\n"
         "      resize: \"letterbox\"\n"
         "      execution: 1\n"
         "      normalization:\n"
         "        scale: 0.00392156862745098\n"
         "        mean: [0.0, 0.0, 0.0]\n"
         "        std: [1.0, 1.0, 1.0]\n",
            "inference.model.preprocess.execution"},
        {"      color_order: \"yuv\"\n"
         "      resize: \"letterbox\"\n"
         "      normalization:\n"
         "        scale: 0.00392156862745098\n"
         "        mean: [0.0, 0.0, 0.0]\n"
         "        std: [1.0, 1.0, 1.0]\n",
            "inference.model.preprocess.color_order"},
        {"      color_order: \"rgb\"\n"
         "      resize: \"crop\"\n"
         "      normalization:\n"
         "        scale: 0.00392156862745098\n"
         "        mean: [0.0, 0.0, 0.0]\n"
         "        std: [1.0, 1.0, 1.0]\n",
            "inference.model.preprocess.resize"},
        {"      color_order: \"rgb\"\n"
         "      resize: \"letterbox\"\n"
         "      normalization:\n"
         "        scale: 0.00392156862745098\n"
         "        mean: [0.0, 0.0]\n"
         "        std: [1.0, 1.0, 1.0]\n",
            "inference.model.preprocess.normalization.mean"},
        {"      color_order: \"rgb\"\n"
         "      resize: \"letterbox\"\n"
         "      normalization:\n"
         "        scale: .nan\n"
         "        mean: [0.0, 0.0, 0.0]\n"
         "        std: [1.0, 1.0, 1.0]\n",
            "inference.model.preprocess.normalization.scale"},
        {"      color_order: \"rgb\"\n"
         "      resize: \"letterbox\"\n"
         "      normalization:\n"
         "        scale: 0.00392156862745098\n"
         "        mean: [0.0, 0.0, 0.0]\n"
         "        std: [1.0, 0.0, 1.0]\n",
            "inference.model.preprocess.normalization.std[1]"},
    };

    for (const auto &test_case : cases) {
        const auto yaml = std::string(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    preprocess:
)yaml") + std::string(test_case.field);
        expect_config_error(
            yaml,
            test_case.path == "inference.model.preprocess.execution"
                && test_case.field.find("execution: 1") != std::string_view::npos
                ? ssv::SsvConfigErrorKind::InvalidType
                : ssv::SsvConfigErrorKind::InvalidValue,
            test_case.path);
    }

    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    preprocess:
      color_order: "rgb"
      resize: "letterbox"
      normalization:
        scale: 0.00392156862745098
        mean: [0.0, 0.0, 0.0]
        std: [1.0, 1.0, 1.0]
        extra: 1
)yaml",
        ssv::SsvConfigErrorKind::UnknownKey,
        "inference.model.preprocess.normalization.extra");
}

void test_resolves_config_path_from_environment()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("environment-config.yaml", R"yaml(
version: "2.0"
sources:
  - id: "environment-source"
    uri: "rtsp://environment.example/stream"
)yaml");
    setenv("SSV_CONFIG_PATH", path.c_str(), 1);

    const auto config = ssv::ssv_config_load();

    assert(config.sources.size() == 1);
    assert(config.sources.front().id == "environment-source");
}

void test_explicit_config_path_precedes_environment_path()
{
    ScopedConfigEnvironment environment;
    const auto environment_path = environment.write(
        "environment-config.yaml", R"yaml(
version: "2.0"
sources:
  - id: "environment-source"
    uri: "rtsp://environment.example/stream"
)yaml");
    const auto explicit_path = environment.write(
        "explicit-config.yaml", R"yaml(
version: "2.0"
sources:
  - id: "explicit-source"
    uri: "rtsp://explicit.example/stream"
)yaml");
    setenv("SSV_CONFIG_PATH", environment_path.c_str(), 1);

    const auto config = ssv::ssv_config_load(explicit_path.string());

    assert(config.sources.size() == 1);
    assert(config.sources.front().id == "explicit-source");
}

void test_applies_deployment_environment_overrides()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("environment-overrides.yaml", R"yaml(
version: "2.0"
redis:
  host: "yaml-redis"
  port: 6379
sources:
  - id: "camera-01"
    uri: "rtsp://yaml.invalid/stream"
)yaml");
    setenv("SSV_RTSP_URL", "rtsps://camera.example/stream", 1);
    setenv("REDIS_HOST", "redis.example", 1);
    setenv("REDIS_PORT", "6380", 1);

    const auto config = ssv::ssv_config_load(path.string());

    assert(config.sources.front().uri == "rtsps://camera.example/stream");
    assert(config.redis.host == "redis.example");
    assert(config.redis.port == 6380);
}

void test_rejects_invalid_deployment_environment_port()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("invalid-environment-port.yaml", R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/stream"
)yaml");
    setenv("REDIS_PORT", "not-a-port", 1);

    try {
        static_cast<void>(ssv::ssv_config_load(path.string()));
        assert(false && "invalid REDIS_PORT was accepted");
    } catch (const ssv::SsvConfigError &error) {
        assert(error.kind() == ssv::SsvConfigErrorKind::InvalidValue);
        assert(error.path() == "environment.REDIS_PORT");
    }
}

void test_ignores_empty_deployment_environment_overrides()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("empty-environment-overrides.yaml", R"yaml(
version: "2.0"
redis:
  host: "yaml-redis"
  port: 6379
sources:
  - id: "camera-01"
    uri: "rtsp://yaml.example/stream"
)yaml");
    setenv("SSV_RTSP_URL", "", 1);
    setenv("REDIS_HOST", "", 1);
    setenv("REDIS_PORT", "", 1);

    const auto config = ssv::ssv_config_load(path.string());

    assert(config.sources.front().uri == "rtsp://yaml.example/stream");
    assert(config.redis.host == "yaml-redis");
    assert(config.redis.port == 6379);
}

void test_rejects_invalid_deployment_environment_uri()
{
    ScopedConfigEnvironment environment;
    const auto path = environment.write("invalid-environment-uri.yaml", R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/stream"
)yaml");
    setenv("SSV_RTSP_URL", "https://camera.example/stream", 1);

    try {
        static_cast<void>(ssv::ssv_config_load(path.string()));
        assert(false && "invalid SSV_RTSP_URL was accepted");
    } catch (const ssv::SsvConfigError &error) {
        assert(error.kind() == ssv::SsvConfigErrorKind::InvalidValue);
        assert(error.path() == "environment.SSV_RTSP_URL");
    }
}

void test_rejects_v1_version()
{
    expect_config_error(R"yaml(
version: "1.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "version");
}

void test_rejects_v1_pipeline_section()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
pipeline:
  analysis_fps: 15
)yaml",
        ssv::SsvConfigErrorKind::UnknownKey,
        "pipeline");
}

void test_rejects_legacy_source_decoder()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    decoder: "nvh264dec"
)yaml",
        ssv::SsvConfigErrorKind::UnknownKey,
        "sources[0].decoder");
}

void test_rejects_legacy_display_sink()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display:
  sink: "fakesink"
)yaml",
        ssv::SsvConfigErrorKind::UnknownKey,
        "display.sink");
}

void test_rejects_legacy_inference_flat_fields()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model_path: "models/raw.onnx"
)yaml",
        ssv::SsvConfigErrorKind::UnknownKey,
        "inference.model_path");
}

void test_rejects_explicit_empty_label_map()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    label_map: ""
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "inference.model.label_map");
}

void test_reports_wrong_yaml_type()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display:
  enabled: []
)yaml",
        ssv::SsvConfigErrorKind::InvalidType,
        "display.enabled");
}

void test_rejects_invalid_decode_mode()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    decode:
      mode: "magic"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources[0].decode.mode");
}

void test_requires_exactly_one_source()
{
    expect_config_error(R"yaml(
version: "2.0"
sources: []
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources");
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/one"
  - id: "camera-02"
    uri: "rtsp://127.0.0.1/two"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources");
}

void test_requires_non_empty_source_id()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: ""
    uri: "rtsp://127.0.0.1/test"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources[0].id");
}

void test_enforces_provider_mode_and_order()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    type: "onnxruntime"
    providers:
      mode: "auto"
      order: ["cpu"]
)yaml",
        ssv::SsvConfigErrorKind::ConflictingFields,
        "inference.runtime.providers.order");
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    type: "onnxruntime"
    providers:
      mode: "explicit"
)yaml",
        ssv::SsvConfigErrorKind::MissingRequired,
        "inference.runtime.providers.order");
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    type: "onnxruntime"
    providers:
      mode: "explicit"
      order: []
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "inference.runtime.providers.order");
}

void test_enforces_runtime_discriminator()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    type: "unknown-runtime"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "inference.runtime.type");
    struct ForbiddenTensorRtField {
        std::string_view name;
        std::string_view yaml;
    };
    const ForbiddenTensorRtField forbidden_fields[] = {
        {"providers", "    providers:\n      mode: \"auto\"\n"},
        {"precision", "    precision: \"fp16\"\n"},
        {"cpu_threads", "    cpu_threads: 4\n"},
        {"cache", "    cache:\n      enabled: true\n"},
    };
    for (const auto &field : forbidden_fields) {
        const auto yaml = std::string(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    path: "models/model.engine"
    manifest: "models/model.engine.json"
  runtime:
    type: "tensorrt-engine"
)yaml") + std::string(field.yaml);
        expect_config_error(
            yaml,
            ssv::SsvConfigErrorKind::ConflictingFields,
            std::string("inference.runtime.") + std::string(field.name));
    }
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    path: "models/model.engine"
  runtime:
    type: "tensorrt-engine"
)yaml",
        ssv::SsvConfigErrorKind::MissingRequired,
        "inference.model.manifest");
}

void test_validates_cpu_threads()
{
    ScopedConfigEnvironment environment;
    const auto automatic_path = environment.write("cpu-auto.yaml", R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  model:
    preprocess:
      color_order: "rgb"
      resize: "letterbox"
      normalization:
        scale: 0.00392156862745098
        mean: [0.0, 0.0, 0.0]
        std: [1.0, 1.0, 1.0]
  runtime:
    type: "onnxruntime"
    cpu_threads: "auto"
)yaml");
    const auto automatic =
        ssv::ssv_config_load(automatic_path.string());
    const auto &runtime = std::get<ssv::SsvOnnxRuntimeConfig>(
        automatic.inference.runtime);
    assert(!runtime.cpu_threads.has_value());

    for (const auto value : {"0", "-1", "many"}) {
        const auto yaml = std::string(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    type: "onnxruntime"
    cpu_threads: )yaml") + value + "\n";
        const auto invalid_path = environment.write(
            std::string("cpu-") + value + ".yaml", yaml);
        try {
            static_cast<void>(
                ssv::ssv_config_load(invalid_path.string()));
            assert(false && "invalid cpu_threads was accepted");
        } catch (const ssv::SsvConfigError &error) {
            assert(error.kind() == ssv::SsvConfigErrorKind::InvalidValue);
            assert(error.path() == "inference.runtime.cpu_threads");
        }
    }
}

void test_validates_explicit_decode_devices()
{
    ScopedConfigEnvironment environment;
    const auto cuda_path = environment.write("cuda-device.yaml", R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    decode:
      mode: "nvdec"
      device: "cuda:2"
)yaml");
    const auto config = ssv::ssv_config_load(cuda_path.string());
    assert(config.sources.front().decode.device.kind ==
        ssv::SsvDecodeDeviceKind::Cuda);
    assert(config.sources.front().decode.device.value == "2");

    for (const auto selector : {
             "drm:renderD128", "cuda:-1", "cuda:gpu", "pci:0000:00:02.0"}) {
        const auto yaml = std::string(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    decode:
      device: ")yaml") + selector + "\"\n";
        const auto invalid_path = environment.write(
            std::string("device-") + std::to_string(selector[0]) + ".yaml",
            yaml);
        try {
            static_cast<void>(
                ssv::ssv_config_load(invalid_path.string()));
            assert(false && "invalid decode device was accepted");
        } catch (const ssv::SsvConfigError &error) {
            assert(error.kind() == ssv::SsvConfigErrorKind::InvalidValue);
            assert(error.path() == "sources[0].decode.device");
        }
    }
}

void test_rejects_deep_unknown_key()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display:
  overlay:
    font:
      weight: 600
)yaml",
        ssv::SsvConfigErrorKind::UnknownKey,
        "display.overlay.font.weight");
}

void test_rejects_unknown_keys_in_every_section()
{
    struct Case {
        std::string_view section;
        std::string_view path;
    };
    const Case cases[] = {
        {"logging:\n  extra: true", "logging.extra"},
        {"redis:\n  extra: true", "redis.extra"},
        {"inference:\n  model:\n    extra: true", "inference.model.extra"},
        {"inference:\n  runtime:\n    cache:\n      extra: true",
            "inference.runtime.cache.extra"},
        {"tracking:\n  extra: true", "tracking.extra"},
        {"tracking:\n  gmc:\n    extra: true", "tracking.gmc.extra"},
        {"agent:\n  extra: true", "agent.extra"},
        {"agent:\n  review:\n    extra: true", "agent.review.extra"},
        {"agent:\n  indexing:\n    extra: true", "agent.indexing.extra"},
        {"agent:\n  knowledge:\n    extra: true",
            "agent.knowledge.extra"},
    };

    for (const auto &test_case : cases) {
        const auto yaml = std::string(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
)yaml") + std::string(test_case.section) + "\n";
        expect_config_error(
            yaml,
            ssv::SsvConfigErrorKind::UnknownKey,
            test_case.path);
    }
}

void test_reports_non_string_mapping_keys()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display:
  ? [enabled]
  : true
)yaml",
        ssv::SsvConfigErrorKind::InvalidType,
        "display");
}

void test_reports_structural_type_errors()
{
    expect_config_error(R"yaml(
version: "2.0"
sources:
  id: "camera-01"
  uri: "rtsp://127.0.0.1/test"
)yaml",
        ssv::SsvConfigErrorKind::InvalidType,
        "sources");
    expect_config_error(R"yaml(
version: "2.0"
sources: ["camera-01"]
)yaml",
        ssv::SsvConfigErrorKind::InvalidType,
        "sources[0]");
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display: []
)yaml",
        ssv::SsvConfigErrorKind::InvalidType,
        "display");
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    providers:
      mode: "explicit"
      order: "cpu"
)yaml",
        ssv::SsvConfigErrorKind::InvalidType,
        "inference.runtime.providers.order");
}

void test_requires_version_and_sources()
{
    expect_config_error(R"yaml(
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
)yaml",
        ssv::SsvConfigErrorKind::MissingRequired,
        "version");
    expect_config_error(R"yaml(
version: "2.0"
)yaml",
        ssv::SsvConfigErrorKind::MissingRequired,
        "sources");
}

void test_rejects_invalid_enum_values()
{
    struct Case {
        std::string_view section;
        std::string_view path;
    };
    const Case cases[] = {
        {"display:\n  backend: \"fakesink\"", "display.backend"},
        {"display:\n  gl_backend: \"drm\"", "display.gl_backend"},
        {"display:\n  overlay:\n    font:\n      face: \"serif\"",
            "display.overlay.font.face"},
        {"inference:\n  runtime:\n    providers:\n      mode: \"explicit\"\n      order: [\"rocm\"]",
            "inference.runtime.providers.order[0]"},
        {"inference:\n  runtime:\n    precision: \"int8\"",
            "inference.runtime.precision"},
        {"inference:\n  model:\n    family: \"transformer\"",
            "inference.model.family"},
        {"inference:\n  model:\n    output_format: \"raw\"",
            "inference.model.output_format"},
        {"tracking:\n  gmc:\n    method: \"dense\"", "tracking.gmc.method"},
    };

    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    codec: "h265"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources[0].codec");

    for (const auto &test_case : cases) {
        const auto yaml = std::string(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
)yaml") + std::string(test_case.section) + "\n";
        expect_config_error(
            yaml,
            ssv::SsvConfigErrorKind::InvalidValue,
            test_case.path);
    }
}

void test_reports_scalar_type_errors_with_paths()
{
    struct Case {
        std::string_view yaml;
        std::string_view path;
    };
    const Case cases[] = {
        {R"yaml(
version: 2.0
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
)yaml",
            "version"},
        {R"yaml(
version: []
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
)yaml",
            "version"},
        {R"yaml(
version: "2.0"
sources:
  - id: 7
    uri: "rtsp://127.0.0.1/test"
)yaml",
            "sources[0].id"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    latency_ms: "120"
)yaml",
            "sources[0].latency_ms"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    latency_ms: "slow"
)yaml",
            "sources[0].latency_ms"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display:
  enabled: "true"
)yaml",
            "display.enabled"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display:
  fps: []
)yaml",
            "display.fps"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  analysis_fps: "many"
)yaml",
            "inference.analysis_fps"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  confidence_threshold: "0.5"
)yaml",
            "inference.confidence_threshold"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    cache:
      directory: []
)yaml",
            "inference.runtime.cache.directory"},
        {R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
redis:
  port: []
)yaml",
            "redis.port"},
    };

    for (const auto &test_case : cases) {
        expect_config_error(
            test_case.yaml,
            ssv::SsvConfigErrorKind::InvalidType,
            test_case.path);
    }

    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
inference:
  runtime:
    cpu_threads: "4"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "inference.runtime.cpu_threads");
}

void test_rejects_out_of_range_values()
{
    struct Case {
        std::string_view section;
        std::string_view path;
    };
    const Case cases[] = {
        {"display:\n  fps: 0", "display.fps"},
        {"display:\n  overlay:\n    font:\n      size: 65",
            "display.overlay.font.size"},
        {"display:\n  overlay:\n    motion_prediction:\n      max_horizon_ms: 0",
            "display.overlay.motion_prediction.max_horizon_ms"},
        {"inference:\n  analysis_fps: -1", "inference.analysis_fps"},
        {"inference:\n  runtime:\n    device_id: -1",
            "inference.runtime.device_id"},
        {"inference:\n  confidence_threshold: 1.1",
            "inference.confidence_threshold"},
        {"inference:\n  confidence_threshold: .nan",
            "inference.confidence_threshold"},
        {"redis:\n  port: 0", "redis.port"},
        {"redis:\n  port: 65536", "redis.port"},
        {"redis:\n  db: -1", "redis.db"},
        {"redis:\n  reclaim_idle_ms: 0", "redis.reclaim_idle_ms"},
        {"redis:\n  reclaim_batch_size: 101", "redis.reclaim_batch_size"},
        {"tracking:\n  track_threshold: -0.1",
            "tracking.track_threshold"},
        {"tracking:\n  match_threshold: 1.1",
            "tracking.match_threshold"},
        {"tracking:\n  publish_cooldown_ms: -1",
            "tracking.publish_cooldown_ms"},
        {"agent:\n  dedup_cooldown_seconds: 0",
            "agent.dedup_cooldown_seconds"},
        {"agent:\n  evidence_roots:\n    - relative",
            "agent.evidence_roots[0]"},
        {"agent:\n  review:\n    lease_ms: 0",
            "agent.review.lease_ms"},
        {"agent:\n  indexing:\n    embedding_backend: remote",
            "agent.indexing.embedding_backend"},
        {"agent:\n  knowledge:\n    backend: remote",
            "agent.knowledge.backend"},
        {"agent:\n  knowledge:\n    qdrant_path: \"  \"",
            "agent.knowledge.qdrant_path"},
        {"agent:\n  knowledge:\n    min_score: 1.1",
            "agent.knowledge.min_score"},
        {"tracking:\n  track_buffer: 0", "tracking.track_buffer"},
        {"tracking:\n  track_buffer: 301", "tracking.track_buffer"},
        {"tracking:\n  gmc:\n    downscale: 0", "tracking.gmc.downscale"},
        {"tracking:\n  gmc:\n    downscale: 9", "tracking.gmc.downscale"},
        {"agent:\n  state_machine_timeout: 0",
            "agent.state_machine_timeout"},
        {"agent:\n  max_retries: -1", "agent.max_retries"},
    };

    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "file:///tmp/video.mp4"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources[0].uri");
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    protocols: "udp"
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources[0].protocols");
    expect_config_error(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
    latency_ms: -1
)yaml",
        ssv::SsvConfigErrorKind::InvalidValue,
        "sources[0].latency_ms");

    for (const auto &test_case : cases) {
        const auto yaml = std::string(R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
)yaml") + std::string(test_case.section) + "\n";
        expect_config_error(
            yaml,
            ssv::SsvConfigErrorKind::InvalidValue,
            test_case.path);
    }
}

void test_reports_file_and_yaml_errors()
{
    ScopedConfigEnvironment environment;
    try {
        static_cast<void>(ssv::ssv_config_load("missing.yaml"));
        assert(false && "missing config file was accepted");
    } catch (const ssv::SsvConfigError &error) {
        assert(error.kind() == ssv::SsvConfigErrorKind::FileNotFound);
        assert(error.path() == "config");
    }

    const auto malformed = environment.write(
        "malformed.yaml", "version: [\nsources: []\n");
    try {
        static_cast<void>(ssv::ssv_config_load(malformed.string()));
        assert(false && "malformed YAML was accepted");
    } catch (const ssv::SsvConfigError &error) {
        assert(error.kind() == ssv::SsvConfigErrorKind::ParseError);
        assert(error.path() == "config");
    }
}

void test_cli_overrides_are_in_memory_only()
{
    ScopedConfigEnvironment environment;
    constexpr std::string_view yaml = R"yaml(
version: "2.0"
sources:
  - id: "camera-01"
    uri: "rtsp://127.0.0.1/test"
display:
  enabled: false
  backend: "auto"
  overlay:
    enabled: false
)yaml";
    const auto path = environment.write("cli-overrides.yaml", yaml);
    const std::vector<std::string_view> arguments = {
        "--config",
        path.native(),
        "--display",
        "--overlay",
        "--display-backend",
        "gtksink",
    };

    const auto options = ssv::ssv_run_options_parse(arguments);
    auto config = ssv::ssv_config_load(options.config_path);
    ssv::ssv_config_apply_overrides(config, options.overrides);

    assert(options.config_path == path.string());
    assert(config.display.enabled);
    assert(config.display.overlay.enabled);
    assert(config.display.backend == ssv::SsvDisplayBackend::GtkSink);
    std::ifstream input(path);
    const std::string persisted {
        std::istreambuf_iterator<char>(input),
        std::istreambuf_iterator<char>() };
    assert(persisted == yaml);
}

void test_cli_rejects_invalid_and_conflicting_arguments()
{
    expect_cli_error(
        {"--sink", "fakesink"},
        ssv::SsvConfigErrorKind::UnknownKey,
        "cli.arguments");
    expect_cli_error(
        {"--config"},
        ssv::SsvConfigErrorKind::MissingRequired,
        "cli.config");
    expect_cli_error(
        {"--display-backend", "fakesink"},
        ssv::SsvConfigErrorKind::InvalidValue,
        "cli.display_backend");
    expect_cli_error(
        {"--display-backend", ""},
        ssv::SsvConfigErrorKind::MissingRequired,
        "cli.display_backend");
    expect_cli_error(
        {"--config", "one.yaml", "--config", "two.yaml"},
        ssv::SsvConfigErrorKind::ConflictingFields,
        "cli.config");
    expect_cli_error(
        {"--display", "--headless"},
        ssv::SsvConfigErrorKind::ConflictingFields,
        "cli.display");
    expect_cli_error(
        {"--headless", "--overlay"},
        ssv::SsvConfigErrorKind::ConflictingFields,
        "cli.overlay");
}

void test_exit_codes_and_fatal_error_contract()
{
    assert(static_cast<int>(ssv::SsvExitCode::Success) == 0);
    assert(static_cast<int>(ssv::SsvExitCode::InvalidConfiguration) == 2);
    assert(static_cast<int>(ssv::SsvExitCode::CapabilityUnavailable) == 3);
    assert(static_cast<int>(ssv::SsvExitCode::ModelInitializationFailed) == 4);
    assert(static_cast<int>(ssv::SsvExitCode::PipelineContractFailed) == 5);
    assert(static_cast<int>(ssv::SsvExitCode::RuntimeFailure) == 6);
    assert(static_cast<int>(ssv::SsvExitCode::DisplayInitializationFailed) == 7);

    const ssv::SsvConfigError config_error(
        ssv::SsvConfigErrorKind::InvalidValue,
        "cli.display_backend",
        "backend is invalid");
    assert(config_error.exit_code() ==
        ssv::SsvExitCode::InvalidConfiguration);
    const auto fatal = ssv::ssv_make_fatal_error(config_error);
    assert(fatal.exit_code == ssv::SsvExitCode::InvalidConfiguration);
    assert(fatal.stage == "cli");
    assert(fatal.source_id == "unresolved");
    assert(fatal.error == "backend is invalid");
    assert(ssv::ssv_format_fatal_error(fatal)
        == "event=fatal_error exit_code=2 stage=cli "
           "source_id=unresolved error=\"backend is invalid\"");

    try {
        static_cast<void>(ssv::ssv_format_fatal_error({
            ssv::SsvExitCode::InvalidConfiguration,
            "config",
            "",
            "missing source",
        }));
        assert(false && "fatal_error accepted an empty required field");
    } catch (const std::invalid_argument &) {
    }
}

} // namespace

int main(int argc, char **argv)
{
    if (argc != 2)
        return 1;

    test_loads_complete_config();
    test_loads_example_config(argv[1]);
    test_parses_explicit_preprocess_semantics();
    test_requires_preprocess_when_inference_is_enabled();
    test_parses_cuda_preprocess_execution();
    test_rejects_invalid_preprocess_fields();
    test_resolves_config_path_from_environment();
    test_explicit_config_path_precedes_environment_path();
    test_applies_deployment_environment_overrides();
    test_rejects_invalid_deployment_environment_port();
    test_ignores_empty_deployment_environment_overrides();
    test_rejects_invalid_deployment_environment_uri();
    test_rejects_v1_version();
    test_rejects_v1_pipeline_section();
    test_rejects_legacy_source_decoder();
    test_rejects_legacy_display_sink();
    test_rejects_legacy_inference_flat_fields();
    test_rejects_explicit_empty_label_map();
    test_reports_wrong_yaml_type();
    test_rejects_invalid_decode_mode();
    test_requires_exactly_one_source();
    test_requires_non_empty_source_id();
    test_enforces_provider_mode_and_order();
    test_enforces_runtime_discriminator();
    test_validates_cpu_threads();
    test_validates_explicit_decode_devices();
    test_rejects_deep_unknown_key();
    test_rejects_unknown_keys_in_every_section();
    test_reports_non_string_mapping_keys();
    test_reports_structural_type_errors();
    test_requires_version_and_sources();
    test_rejects_invalid_enum_values();
    test_reports_scalar_type_errors_with_paths();
    test_rejects_out_of_range_values();
    test_reports_file_and_yaml_errors();
    test_cli_overrides_are_in_memory_only();
    test_cli_rejects_invalid_and_conflicting_arguments();
    test_exit_codes_and_fatal_error_contract();
    return 0;
}
