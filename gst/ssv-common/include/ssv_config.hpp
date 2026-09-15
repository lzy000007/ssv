#pragma once

#include <array>
#include <optional>
#include <span>
#include <stdexcept>
#include <string>
#include <string_view>
#include <variant>
#include <vector>

namespace ssv {

enum class SsvExitCode : int {
    Success = 0,
    InvalidConfiguration = 2,
    CapabilityUnavailable = 3,
    ModelInitializationFailed = 4,
    PipelineContractFailed = 5,
    RuntimeFailure = 6,
    DisplayInitializationFailed = 7,
};

enum class SsvConfigErrorKind {
    FileNotFound,
    ParseError,
    MissingRequired,
    UnknownKey,
    InvalidType,
    InvalidValue,
    ConflictingFields,
};

class SsvConfigError : public std::runtime_error {
public:
    SsvConfigError(
        SsvConfigErrorKind kind,
        std::string path,
        std::string message);

    [[nodiscard]] SsvConfigErrorKind kind() const noexcept;
    [[nodiscard]] const std::string &path() const noexcept;
    [[nodiscard]] SsvExitCode exit_code() const noexcept;

private:
    SsvConfigErrorKind kind_;
    std::string path_;
};

enum class SsvDecodeMode {
    Auto,
    Vaapi,
    Nvdec,
    Software,
};

enum class SsvDecodeDeviceKind {
    Auto,
    Drm,
    Cuda,
};

struct SsvDecodeDevice {
    SsvDecodeDeviceKind kind = SsvDecodeDeviceKind::Auto;
    std::string value = "auto";
};

struct SsvDecodeConfig {
    SsvDecodeMode mode = SsvDecodeMode::Auto;
    SsvDecodeDevice device;
};

struct SsvEventRuleConfig {
    std::string event_type;
    std::string severity;
    std::string rule_id;
    std::string rule_version;
    std::string rule_facts_json = "{}";
};

struct SsvSourceConfig {
    std::string id;
    std::string uri;
    std::string codec = "h264";
    std::string protocols = "tcp";
    int latency_ms = 200;
    SsvEventRuleConfig event_rule;
    SsvDecodeConfig decode;
};

enum class SsvDisplayBackend {
    Auto,
    GtkGlSink,
    GtkSink,
};

enum class SsvGlBackend {
    Auto,
    X11,
    Wayland,
};

struct SsvOverlayFontConfig {
    std::string face = "regular";
    int size = 12;
};

struct SsvMotionPredictionConfig {
    bool enabled = true;
    int max_horizon_ms = 300;
};

struct SsvOverlayConfig {
    bool enabled = true;
    SsvOverlayFontConfig font;
    SsvMotionPredictionConfig motion_prediction;
};

struct SsvDisplayConfig {
    bool enabled = true;
    SsvDisplayBackend backend = SsvDisplayBackend::Auto;
    int fps = 30;
    SsvGlBackend gl_backend = SsvGlBackend::Auto;
    SsvOverlayConfig overlay;
};

enum class SsvProviderMode {
    Auto,
    Explicit,
};

enum class SsvProvider {
    TensorRt,
    Cuda,
    OpenVino,
    MiGraphX,
    Cpu,
};

enum class SsvPrecision {
    Auto,
    Fp32,
    Fp16,
};

struct SsvProviderConfig {
    SsvProviderMode mode = SsvProviderMode::Auto;
    std::vector<SsvProvider> order;
};

struct SsvCacheConfig {
    bool enabled = true;
    std::string directory;
};

struct SsvOnnxRuntimeConfig {
    SsvProviderConfig providers;
    int device_id = 0;
    SsvPrecision precision = SsvPrecision::Auto;
    std::optional<int> cpu_threads;
    SsvCacheConfig cache;
};

struct SsvTensorRtEngineConfig {
    int device_id = 0;
};

using SsvRuntimeConfig =
    std::variant<SsvOnnxRuntimeConfig, SsvTensorRtEngineConfig>;

enum class SsvInputColorOrder {
    Rgb,
    Bgr,
};

enum class SsvResizeMode {
    Letterbox,
    Stretch,
};

enum class SsvPreprocessExecution {
    Auto,
    Cpu,
    Cuda,
};

struct SsvNormalizationConfig {
    float scale = 1.0F;
    std::array<float, 3> mean {0.0F, 0.0F, 0.0F};
    std::array<float, 3> std {1.0F, 1.0F, 1.0F};
};

struct SsvPreprocessConfig {
    SsvInputColorOrder color_order = SsvInputColorOrder::Rgb;
    SsvResizeMode resize_mode = SsvResizeMode::Letterbox;
    SsvNormalizationConfig normalization;
    SsvPreprocessExecution execution = SsvPreprocessExecution::Auto;
};

struct SsvModelConfig {
    std::string path;
    std::optional<std::string> manifest;
    std::string family = "yolo";
    std::string output_format = "yolov8";
    std::string label_map = "config/model-labels/coco80.txt";
    std::optional<SsvPreprocessConfig> preprocess;
};

struct SsvInferenceConfig {
    bool enabled = true;
    int analysis_fps = 15;
    SsvModelConfig model;
    SsvRuntimeConfig runtime = SsvOnnxRuntimeConfig {};
    float confidence_threshold = 0.5F;
    std::string target_class = "person";
};

enum class SsvGmcMethod {
    SparseOpticalFlow,
    None,
};

struct SsvGmcConfig {
    SsvGmcMethod method = SsvGmcMethod::SparseOpticalFlow;
    int downscale = 2;
};

struct SsvTrackingConfig {
    bool enabled = true;
    float track_threshold = 0.5F;
    int track_buffer = 30;
    float match_threshold = 0.3F;
    bool mock_track = false;
    int publish_cooldown_ms = 30000;
    SsvGmcConfig gmc;
};

struct SsvLoggingConfig {
    std::string cpp_debug_level = "ssv*:4";
    std::string python_log_level = "INFO";
};

struct SsvRedisConfig {
    std::string host = "localhost";
    int port = 6379;
    int db = 0;
    std::string stream_key = "ssv:events";
    std::string consumer_group = "ssv-agent";
};

struct SsvAgentConfig {
    int state_machine_timeout = 300;
    int max_retries = 3;
    std::string model_name;
    std::string output_dir = "outputs";
    bool dedup_enabled = true;
    float dedup_cooldown_seconds = 30.0F;
};

struct SsvConfig {
    std::string version = "2.0";
    SsvLoggingConfig logging;
    SsvRedisConfig redis;
    std::vector<SsvSourceConfig> sources;
    SsvDisplayConfig display;
    SsvInferenceConfig inference;
    SsvTrackingConfig tracking;
    SsvAgentConfig agent;
};

struct SsvConfigOverrides {
    std::optional<bool> display_enabled;
    std::optional<bool> overlay_enabled;
    std::optional<SsvDisplayBackend> display_backend;
};

struct SsvRunOptions {
    std::string config_path;
    SsvConfigOverrides overrides;
};

struct SsvFatalError {
    SsvExitCode exit_code;
    std::string stage;
    std::string source_id;
    std::string error;
};

/// Load the strict runtime configuration. The example template is never
/// considered by the implicit search order.
SsvConfig ssv_config_load(const std::string &path = "");

SsvRunOptions ssv_run_options_parse(
    std::span<const std::string_view> arguments);

void ssv_config_apply_overrides(
    SsvConfig &config,
    const SsvConfigOverrides &overrides);

SsvFatalError ssv_make_fatal_error(
    const SsvConfigError &error,
    std::string source_id = "unresolved");

std::string ssv_format_fatal_error(const SsvFatalError &error);

} // namespace ssv
