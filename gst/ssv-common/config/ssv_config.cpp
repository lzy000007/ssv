#include "ssv_config.hpp"

#include <algorithm>
#include <charconv>
#include <cctype>
#include <cmath>
#include <cstdlib>
#include <filesystem>
#include <initializer_list>
#include <optional>
#include <stdexcept>
#include <string_view>
#include <system_error>

#include <yaml-cpp/yaml.h>

namespace ssv {

SsvConfigError::SsvConfigError(
    SsvConfigErrorKind kind,
    std::string path,
    std::string message)
    : std::runtime_error(std::move(message))
    , kind_(kind)
    , path_(std::move(path))
{
}

SsvConfigErrorKind SsvConfigError::kind() const noexcept
{
    return kind_;
}

const std::string &SsvConfigError::path() const noexcept
{
    return path_;
}

SsvExitCode SsvConfigError::exit_code() const noexcept
{
    return SsvExitCode::InvalidConfiguration;
}

namespace {

std::string resolve_config_path(const std::string &explicit_path)
{
    if (!explicit_path.empty()) {
        if (std::filesystem::exists(explicit_path))
            return explicit_path;
        throw SsvConfigError(
            SsvConfigErrorKind::FileNotFound,
            "config",
            "config file not found: " + explicit_path);
    }

    const char *env_path = std::getenv("SSV_CONFIG_PATH");
    if (env_path && std::filesystem::exists(env_path))
        return env_path;
    if (std::filesystem::exists("ssv.yaml"))
        return "ssv.yaml";
    if (std::filesystem::exists("config/ssv.yaml"))
        return "config/ssv.yaml";
    if (std::filesystem::exists("/etc/ssv/ssv.yaml"))
        return "/etc/ssv/ssv.yaml";

    throw SsvConfigError(
        SsvConfigErrorKind::FileNotFound,
        "config",
        "no config file found; searched SSV_CONFIG_PATH, ssv.yaml, "
        "config/ssv.yaml, and /etc/ssv/ssv.yaml");
}

YAML::Node read_yaml(const std::string &path)
{
    try {
        return YAML::LoadFile(path);
    } catch (const YAML::BadFile &error) {
        throw SsvConfigError(
            SsvConfigErrorKind::FileNotFound,
            "config",
            error.what());
    } catch (const YAML::ParserException &error) {
        throw SsvConfigError(
            SsvConfigErrorKind::ParseError,
            "config",
            error.what());
    }
}

constexpr std::string_view YAML_STRING_TAG = "tag:yaml.org,2002:str";
constexpr std::string_view YAML_INTEGER_TAG = "tag:yaml.org,2002:int";
constexpr std::string_view YAML_FLOAT_TAG = "tag:yaml.org,2002:float";
constexpr std::string_view YAML_BOOLEAN_TAG = "tag:yaml.org,2002:bool";

[[noreturn]] void throw_invalid_type(std::string_view path)
{
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidType,
        std::string(path),
        std::string(path) + " has the wrong YAML type");
}

template <typename T>
bool can_decode(const YAML::Node &node)
{
    try {
        static_cast<void>(node.as<T>());
        return true;
    } catch (const YAML::Exception &) {
        return false;
    }
}

template <typename T>
bool has_yaml_scalar_type(const YAML::Node &node);

template <>
bool has_yaml_scalar_type<std::string>(const YAML::Node &node)
{
    if (!node.IsScalar())
        return false;

    const auto tag = node.Tag();
    if (tag == "!" || tag == YAML_STRING_TAG)
        return true;
    if (tag != "?")
        return false;

    return !can_decode<bool>(node)
        && !can_decode<long long>(node)
        && !can_decode<double>(node);
}

template <>
bool has_yaml_scalar_type<int>(const YAML::Node &node)
{
    if (!node.IsScalar())
        return false;
    const auto tag = node.Tag();
    return (tag == "?" || tag == YAML_INTEGER_TAG)
        && can_decode<int>(node);
}

template <>
bool has_yaml_scalar_type<float>(const YAML::Node &node)
{
    if (!node.IsScalar())
        return false;
    const auto tag = node.Tag();
    return (tag == "?" || tag == YAML_INTEGER_TAG || tag == YAML_FLOAT_TAG)
        && can_decode<float>(node);
}

template <>
bool has_yaml_scalar_type<bool>(const YAML::Node &node)
{
    if (!node.IsScalar())
        return false;
    const auto tag = node.Tag();
    return (tag == "?" || tag == YAML_BOOLEAN_TAG)
        && can_decode<bool>(node);
}

template <typename T>
T node_as(const YAML::Node &node, std::string_view path)
{
    if (!has_yaml_scalar_type<T>(node))
        throw_invalid_type(path);
    try {
        return node.as<T>();
    } catch (const YAML::Exception &) {
        throw_invalid_type(path);
    }
}

template <typename T>
T get_or(
    const YAML::Node &node,
    std::string_view key,
    const T &fallback,
    std::string_view path)
{
    const auto child = node[std::string(key)];
    if (!child)
        return fallback;
    return node_as<T>(child, path);
}

void reject_unknown_keys(
    const YAML::Node &node,
    std::string_view path,
    std::initializer_list<std::string_view> allowed)
{
    for (const auto &entry : node) {
        const auto mapping_path = path.empty() ? "root" : path;
        const auto key = node_as<std::string>(entry.first, mapping_path);
        bool known = false;
        for (const auto candidate : allowed) {
            if (key == candidate) {
                known = true;
                break;
            }
        }
        if (!known) {
            const auto key_path = path.empty()
                ? key
                : std::string(path) + "." + key;
            throw SsvConfigError(
                SsvConfigErrorKind::UnknownKey,
                key_path,
                "unknown configuration key: " + key_path);
        }
    }
}

void require_map(const YAML::Node &node, std::string_view path)
{
    if (!node.IsMap()) {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidType,
            std::string(path),
            std::string(path) + " must be a mapping");
    }
}

void require_sequence(const YAML::Node &node, std::string_view path)
{
    if (!node.IsSequence()) {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidType,
            std::string(path),
            std::string(path) + " must be a sequence");
    }
}

bool is_blank(std::string_view value)
{
    return value.empty() || std::all_of(
        value.begin(), value.end(), [](unsigned char character) {
            return std::isspace(character) != 0;
        });
}

[[noreturn]] void throw_invalid_value(
    std::string_view path,
    std::string message)
{
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        std::string(path),
        std::move(message));
}

std::optional<std::string_view> environment_value(const char *name)
{
    const char *value = std::getenv(name);
    if (value == nullptr || *value == '\0')
        return std::nullopt;
    return value;
}

void validate_rtsp_uri(std::string_view uri, std::string_view path)
{
    if ((!uri.starts_with("rtsp://") && !uri.starts_with("rtsps://"))
        || uri.ends_with("://")) {
        throw_invalid_value(
            path, std::string(path) + " must be a non-empty RTSP URI");
    }
}

void validate_unit_interval(float value, std::string_view path)
{
    if (!std::isfinite(value) || value < 0.0F || value > 1.0F) {
        throw_invalid_value(path,
            std::string(path) + " must be between 0 and 1");
    }
}

void validate_optional_string_field(
    const YAML::Node &node,
    std::string_view key,
    std::string_view path,
    bool require_non_empty = false)
{
    const auto child = node[std::string(key)];
    if (!child || child.IsNull())
        return;

    const auto value = node_as<std::string>(child, path);
    if (require_non_empty && value.empty()) {
        throw_invalid_value(
            path, std::string(path) + " must not be empty");
    }
}

YAML::Node required_node(
    const YAML::Node &node,
    std::string_view key,
    std::string_view path)
{
    const auto child = node[std::string(key)];
    if (!child || child.IsNull()) {
        throw SsvConfigError(
            SsvConfigErrorKind::MissingRequired,
            std::string(path),
            std::string(path) + " is required");
    }
    return child;
}

std::array<float, 3> parse_float_triplet(
    const YAML::Node &node,
    std::string_view path)
{
    require_sequence(node, path);
    if (node.size() != 3) {
        throw_invalid_value(
            path, std::string(path) + " must contain exactly 3 values");
    }

    std::array<float, 3> values {};
    for (std::size_t index = 0; index < values.size(); ++index) {
        const auto item_path = std::string(path) + "["
            + std::to_string(index) + "]";
        values[index] = node_as<float>(node[index], item_path);
        if (!std::isfinite(values[index])) {
            throw_invalid_value(
                item_path, item_path + " must be finite");
        }
    }
    return values;
}

SsvPreprocessConfig parse_preprocess(const YAML::Node &node)
{
    constexpr std::string_view path = "inference.model.preprocess";
    require_map(node, path);
    reject_unknown_keys(
        node,
        path,
        {"color_order", "resize", "normalization", "execution"});

    SsvPreprocessConfig preprocess;
    const auto color_order = node_as<std::string>(
        required_node(node, "color_order", std::string(path) + ".color_order"),
        std::string(path) + ".color_order");
    if (color_order == "rgb") {
        preprocess.color_order = SsvInputColorOrder::Rgb;
    } else if (color_order == "bgr") {
        preprocess.color_order = SsvInputColorOrder::Bgr;
    } else {
        throw_invalid_value(
            std::string(path) + ".color_order",
            std::string(path) + ".color_order must be rgb or bgr");
    }

    const auto resize = node_as<std::string>(
        required_node(node, "resize", std::string(path) + ".resize"),
        std::string(path) + ".resize");
    if (resize == "letterbox") {
        preprocess.resize_mode = SsvResizeMode::Letterbox;
    } else if (resize == "stretch") {
        preprocess.resize_mode = SsvResizeMode::Stretch;
    } else {
        throw_invalid_value(
            std::string(path) + ".resize",
            std::string(path) + ".resize must be letterbox or stretch");
    }

    const auto execution = get_or<std::string>(
        node, "execution", "auto", std::string(path) + ".execution");
    if (execution == "auto") {
        preprocess.execution = SsvPreprocessExecution::Auto;
    } else if (execution == "cpu") {
        preprocess.execution = SsvPreprocessExecution::Cpu;
    } else if (execution == "cuda") {
        preprocess.execution = SsvPreprocessExecution::Cuda;
    } else {
        throw_invalid_value(
            std::string(path) + ".execution",
            std::string(path) + ".execution must be auto, cpu or cuda");
    }

    const auto normalization = required_node(
        node, "normalization", std::string(path) + ".normalization");
    require_map(normalization, std::string(path) + ".normalization");
    reject_unknown_keys(
        normalization,
        std::string(path) + ".normalization",
        {"scale", "mean", "std"});

    const auto scale_path = std::string(path) + ".normalization.scale";
    preprocess.normalization.scale = node_as<float>(
        required_node(node["normalization"], "scale", scale_path),
        scale_path);
    if (!std::isfinite(preprocess.normalization.scale)
        || preprocess.normalization.scale <= 0.0F) {
        throw_invalid_value(
            scale_path, scale_path + " must be finite and positive");
    }

    const auto mean_path = std::string(path) + ".normalization.mean";
    preprocess.normalization.mean = parse_float_triplet(
        required_node(node["normalization"], "mean", mean_path), mean_path);

    const auto std_path = std::string(path) + ".normalization.std";
    preprocess.normalization.std = parse_float_triplet(
        required_node(node["normalization"], "std", std_path), std_path);
    for (std::size_t index = 0;
         index < preprocess.normalization.std.size();
         ++index) {
        if (preprocess.normalization.std[index] <= 0.0F) {
            const auto item_path = std_path + "["
                + std::to_string(index) + "]";
            throw_invalid_value(
                item_path, item_path + " must be positive");
        }
    }
    return preprocess;
}

void validate_worker_common(
    const YAML::Node &node,
    std::string_view path)
{
    const auto poll_path = std::string(path) + ".poll_interval_ms";
    const auto poll_interval_ms = get_or<int>(
        node, "poll_interval_ms", 1000, poll_path);
    if (poll_interval_ms <= 0) {
        throw_invalid_value(
            poll_path, poll_path + " must be positive");
    }

    const auto lease_path = std::string(path) + ".lease_ms";
    const auto lease_ms = get_or<int>(node, "lease_ms", 30000, lease_path);
    if (lease_ms <= 0) {
        throw_invalid_value(lease_path, lease_path + " must be positive");
    }

    const auto retries_path = std::string(path) + ".max_retries";
    const auto max_retries = get_or<int>(node, "max_retries", 3, retries_path);
    if (max_retries < 1) {
        throw_invalid_value(
            retries_path, retries_path + " must be positive");
    }

    const auto delay_path = std::string(path) + ".retry_delay_ms";
    const auto retry_delay_ms = get_or<int>(
        node, "retry_delay_ms", 1000, delay_path);
    if (retry_delay_ms < 0) {
        throw_invalid_value(
            delay_path, delay_path + " must not be negative");
    }
    static_cast<void>(get_or<bool>(
        node, "enabled", false, std::string(path) + ".enabled"));
}

void validate_review_extension(const YAML::Node &node)
{
    constexpr std::string_view path = "agent.review";
    require_map(node, path);
    reject_unknown_keys(node, path, {
        "enabled",
        "poll_interval_ms",
        "lease_ms",
        "max_retries",
        "retry_delay_ms",
        "policy_id",
        "model_id",
    });
    validate_worker_common(node, path);
    static_cast<void>(get_or<std::string>(
        node, "policy_id", "ssv-review.v1", "agent.review.policy_id"));
    validate_optional_string_field(node, "model_id", "agent.review.model_id");
}

void validate_indexing_extension(const YAML::Node &node)
{
    constexpr std::string_view path = "agent.indexing";
    require_map(node, path);
    reject_unknown_keys(node, path, {
        "enabled",
        "poll_interval_ms",
        "lease_ms",
        "max_retries",
        "retry_delay_ms",
        "embedding_backend",
        "embedding_model",
    });
    validate_worker_common(node, path);

    const auto backend = get_or<std::string>(
        node, "embedding_backend", "mock", "agent.indexing.embedding_backend");
    if (backend != "mock"
        && backend != "openai_compatible"
        && backend != "bge_m3") {
        throw_invalid_value(
            "agent.indexing.embedding_backend",
            "agent.indexing.embedding_backend is not supported");
    }
    validate_optional_string_field(
        node, "embedding_model", "agent.indexing.embedding_model");
}

void validate_knowledge_extension(const YAML::Node &node)
{
    constexpr std::string_view path = "agent.knowledge";
    require_map(node, path);
    reject_unknown_keys(node, path, {
        "backend",
        "qdrant_path",
        "min_score",
    });

    const auto backend = get_or<std::string>(
        node, "backend", "local_markdown", "agent.knowledge.backend");
    if (backend != "local_markdown"
        && backend != "qdrant"
        && backend != "mock") {
        throw_invalid_value(
            "agent.knowledge.backend",
            "agent.knowledge.backend is not supported");
    }

    const auto qdrant_path = get_or<std::string>(
        node, "qdrant_path", "data/qdrant", "agent.knowledge.qdrant_path");
    if (is_blank(qdrant_path)) {
        throw_invalid_value(
            "agent.knowledge.qdrant_path",
            "agent.knowledge.qdrant_path must not be blank");
    }

    const auto min_score = get_or<float>(
        node, "min_score", 0.5F, "agent.knowledge.min_score");
    if (!std::isfinite(min_score) || min_score < -1.0F || min_score > 1.0F) {
        throw_invalid_value(
            "agent.knowledge.min_score",
            "agent.knowledge.min_score must be between -1 and 1");
    }
}

void validate_agent_extensions(const YAML::Node &agent)
{
    if (const auto evidence_roots = agent["evidence_roots"]) {
        constexpr std::string_view path = "agent.evidence_roots";
        require_sequence(evidence_roots, path);
        for (std::size_t index = 0; index < evidence_roots.size(); ++index) {
            const auto item_path =
                std::string(path) + "[" + std::to_string(index) + "]";
            const auto root = node_as<std::string>(
                evidence_roots[index], item_path);
            if (!std::filesystem::path(root).is_absolute()) {
                throw_invalid_value(
                    item_path, item_path + " must be an absolute path");
            }
        }
    }
    if (const auto review = agent["review"])
        validate_review_extension(review);
    if (const auto indexing = agent["indexing"])
        validate_indexing_extension(indexing);
    if (const auto knowledge = agent["knowledge"])
        validate_knowledge_extension(knowledge);
}

std::string format_log_value(std::string_view value)
{
    const auto safe = !value.empty() && std::all_of(
        value.begin(), value.end(), [](unsigned char character) {
            return std::isalnum(character) != 0
                || character == '_'
                || character == '-'
                || character == '.'
                || character == '/'
                || character == ':';
        });
    if (safe)
        return std::string(value);

    std::string result = "\"";
    for (const auto character : value) {
        switch (character) {
        case '\\':
            result += "\\\\";
            break;
        case '"':
            result += "\\\"";
            break;
        case '\n':
            result += "\\n";
            break;
        case '\r':
            result += "\\r";
            break;
        case '\t':
            result += "\\t";
            break;
        default:
            result += character;
            break;
        }
    }
    result += '"';
    return result;
}

SsvDecodeMode parse_decode_mode(
    const std::string &value,
    std::string_view path)
{
    if (value == "auto")
        return SsvDecodeMode::Auto;
    if (value == "vaapi")
        return SsvDecodeMode::Vaapi;
    if (value == "nvdec")
        return SsvDecodeMode::Nvdec;
    if (value == "software")
        return SsvDecodeMode::Software;
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        std::string(path),
        std::string(path) + " has invalid value: " + value);
}

bool is_decimal(std::string_view value)
{
    return !value.empty() && std::all_of(
        value.begin(), value.end(), [](unsigned char character) {
            return std::isdigit(character) != 0;
        });
}

SsvDecodeDevice parse_decode_device(
    const std::string &value,
    std::string_view path)
{
    if (value == "auto")
        return {};
    if (value.starts_with("drm:")) {
        const auto device = value.substr(4);
        constexpr std::string_view prefix = "/dev/dri/renderD";
        if (device.starts_with(prefix)
            && is_decimal(std::string_view(device).substr(prefix.size()))) {
            return {SsvDecodeDeviceKind::Drm, device};
        }
    } else if (value.starts_with("cuda:")) {
        const auto device = value.substr(5);
        if (is_decimal(device))
            return {SsvDecodeDeviceKind::Cuda, device};
    }
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        std::string(path),
        std::string(path)
            + " must be auto, drm:/dev/dri/renderD<N>, or cuda:<N>");
}

SsvDisplayBackend parse_display_backend(const std::string &value)
{
    if (value == "auto")
        return SsvDisplayBackend::Auto;
    if (value == "gtkglsink")
        return SsvDisplayBackend::GtkGlSink;
    if (value == "gtksink")
        return SsvDisplayBackend::GtkSink;
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        "display.backend",
        "display.backend has invalid value: " + value);
}

SsvGlBackend parse_gl_backend(const std::string &value)
{
    if (value == "auto")
        return SsvGlBackend::Auto;
    if (value == "x11")
        return SsvGlBackend::X11;
    if (value == "wayland")
        return SsvGlBackend::Wayland;
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        "display.gl_backend",
        "display.gl_backend has invalid value: " + value);
}

SsvProvider parse_provider(
    const std::string &value,
    std::string_view path)
{
    if (value == "tensorrt")
        return SsvProvider::TensorRt;
    if (value == "cuda")
        return SsvProvider::Cuda;
    if (value == "openvino")
        return SsvProvider::OpenVino;
    if (value == "migraphx")
        return SsvProvider::MiGraphX;
    if (value == "cpu")
        return SsvProvider::Cpu;
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        std::string(path),
        std::string(path) + " has invalid value: " + value);
}

SsvPrecision parse_precision(const std::string &value)
{
    if (value == "auto")
        return SsvPrecision::Auto;
    if (value == "fp32")
        return SsvPrecision::Fp32;
    if (value == "fp16")
        return SsvPrecision::Fp16;
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        "inference.runtime.precision",
        "inference.runtime.precision has invalid value: " + value);
}

SsvGmcMethod parse_gmc_method(const std::string &value)
{
    if (value == "sparse-opt-flow")
        return SsvGmcMethod::SparseOpticalFlow;
    if (value == "none")
        return SsvGmcMethod::None;
    throw SsvConfigError(
        SsvConfigErrorKind::InvalidValue,
        "tracking.gmc.method",
        "tracking.gmc.method has invalid value: " + value);
}

SsvSourceConfig parse_source(
    const YAML::Node &node,
    std::string_view path)
{
    require_map(node, path);
    reject_unknown_keys(node, path, {
        "id",
        "uri",
        "codec",
        "protocols",
        "latency_ms",
        "decode",
    });

    SsvSourceConfig source;
    source.id = get_or<std::string>(
        node, "id", source.id, std::string(path) + ".id");
    if (is_blank(source.id)) {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            std::string(path) + ".id",
            std::string(path) + ".id must not be empty");
    }
    source.uri = get_or<std::string>(
        node, "uri", source.uri, std::string(path) + ".uri");
    validate_rtsp_uri(source.uri, std::string(path) + ".uri");
    source.codec = get_or<std::string>(
        node, "codec", source.codec, std::string(path) + ".codec");
    if (source.codec != "h264") {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            std::string(path) + ".codec",
            std::string(path) + ".codec must be h264");
    }
    source.protocols =
        get_or<std::string>(node,
            "protocols",
            source.protocols,
            std::string(path) + ".protocols");
    if (source.protocols != "tcp") {
        throw_invalid_value(
            std::string(path) + ".protocols",
            std::string(path) + ".protocols must be tcp");
    }
    source.latency_ms = get_or<int>(node,
        "latency_ms",
        source.latency_ms,
        std::string(path) + ".latency_ms");
    if (source.latency_ms < 0) {
        throw_invalid_value(
            std::string(path) + ".latency_ms",
            std::string(path) + ".latency_ms must not be negative");
    }

    if (const auto decode = node["decode"]) {
        require_map(decode, std::string(path) + ".decode");
        reject_unknown_keys(
            decode, std::string(path) + ".decode", {"mode", "device"});
        source.decode.mode = parse_decode_mode(
            get_or<std::string>(decode,
                "mode",
                "auto",
                std::string(path) + ".decode.mode"),
            std::string(path) + ".decode.mode");
        source.decode.device = parse_decode_device(
            get_or<std::string>(decode,
                "device",
                "auto",
                std::string(path) + ".decode.device"),
            std::string(path) + ".decode.device");
    }
    return source;
}

SsvDisplayConfig parse_display(const YAML::Node &node)
{
    SsvDisplayConfig display;
    if (!node)
        return display;

    require_map(node, "display");
    reject_unknown_keys(node, "display", {
        "enabled",
        "backend",
        "fps",
        "gl_backend",
        "overlay",
    });

    display.enabled = get_or<bool>(
        node, "enabled", display.enabled, "display.enabled");
    display.backend = parse_display_backend(
        get_or<std::string>(
            node, "backend", "auto", "display.backend"));
    display.fps = get_or<int>(
        node, "fps", display.fps, "display.fps");
    if (display.fps <= 0)
        throw_invalid_value("display.fps", "display.fps must be positive");
    display.gl_backend = parse_gl_backend(
        get_or<std::string>(
            node, "gl_backend", "auto", "display.gl_backend"));

    if (const auto overlay = node["overlay"]) {
        require_map(overlay, "display.overlay");
        reject_unknown_keys(
            overlay, "display.overlay", {"enabled", "font", "motion_prediction"});
        display.overlay.enabled = get_or<bool>(overlay,
            "enabled",
            display.overlay.enabled,
            "display.overlay.enabled");
        if (const auto font = overlay["font"]) {
            require_map(font, "display.overlay.font");
            reject_unknown_keys(
                font, "display.overlay.font", {"face", "size"});
            display.overlay.font.face = get_or<std::string>(
                font,
                "face",
                display.overlay.font.face,
                "display.overlay.font.face");
            if (display.overlay.font.face != "regular"
                && display.overlay.font.face != "bold") {
                throw SsvConfigError(
                    SsvConfigErrorKind::InvalidValue,
                    "display.overlay.font.face",
                    "display.overlay.font.face must be regular or bold");
            }
            display.overlay.font.size = get_or<int>(font,
                "size",
                display.overlay.font.size,
                "display.overlay.font.size");
            if (display.overlay.font.size < 7
                || display.overlay.font.size > 64) {
                throw_invalid_value(
                    "display.overlay.font.size",
                    "display.overlay.font.size must be between 7 and 64");
            }
        }
        if (const auto prediction = overlay["motion_prediction"]) {
            require_map(
                prediction, "display.overlay.motion_prediction");
            reject_unknown_keys(
                prediction,
                "display.overlay.motion_prediction",
                {"enabled", "max_horizon_ms"});
            display.overlay.motion_prediction.enabled = get_or<bool>(
                prediction,
                "enabled",
                display.overlay.motion_prediction.enabled,
                "display.overlay.motion_prediction.enabled");
            display.overlay.motion_prediction.max_horizon_ms = get_or<int>(
                prediction,
                "max_horizon_ms",
                display.overlay.motion_prediction.max_horizon_ms,
                "display.overlay.motion_prediction.max_horizon_ms");
            if (display.overlay.motion_prediction.max_horizon_ms < 1
                || display.overlay.motion_prediction.max_horizon_ms > 300) {
                throw_invalid_value(
                    "display.overlay.motion_prediction.max_horizon_ms",
                    "motion prediction horizon must be between 1 and 300 ms");
            }
        }
    }
    return display;
}

SsvModelConfig parse_model(const YAML::Node &node)
{
    SsvModelConfig model;
    if (!node)
        return model;

    require_map(node, "inference.model");
    reject_unknown_keys(node, "inference.model", {
        "path",
        "manifest",
        "family",
        "output_format",
        "label_map",
        "preprocess",
    });

    model.path = get_or<std::string>(
        node, "path", model.path, "inference.model.path");
    if (const auto manifest = node["manifest"])
        model.manifest = node_as<std::string>(
            manifest, "inference.model.manifest");
    model.family = get_or<std::string>(
        node, "family", model.family, "inference.model.family");
    model.output_format = get_or<std::string>(node,
        "output_format",
        model.output_format,
        "inference.model.output_format");
    model.label_map = get_or<std::string>(node,
        "label_map",
        model.label_map,
        "inference.model.label_map");
    if (const auto preprocess = node["preprocess"];
        preprocess && !preprocess.IsNull()) {
        model.preprocess = parse_preprocess(preprocess);
    }
    if (is_blank(model.label_map)) {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            "inference.model.label_map",
            "inference.model.label_map must not be empty");
    }
    if (model.family != "yolo") {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            "inference.model.family",
            "inference.model.family must be yolo");
    }
    if (model.output_format != "yolov5"
        && model.output_format != "yolov8"
        && model.output_format != "yolo_nx6") {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            "inference.model.output_format",
            "inference.model.output_format is not supported");
    }
    return model;
}

SsvProviderConfig parse_providers(const YAML::Node &node)
{
    SsvProviderConfig providers;
    if (!node)
        return providers;

    constexpr std::string_view provider_path =
        "inference.runtime.providers";
    require_map(node, provider_path);
    reject_unknown_keys(node, provider_path, {"mode", "order"});

    const auto mode = get_or<std::string>(node,
        "mode",
        "auto",
        "inference.runtime.providers.mode");
    if (mode == "auto") {
        providers.mode = SsvProviderMode::Auto;
    } else if (mode == "explicit") {
        providers.mode = SsvProviderMode::Explicit;
    } else {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            std::string(provider_path) + ".mode",
            "inference.runtime.providers.mode must be auto or explicit");
    }

    if (const auto order = node["order"]) {
        if (providers.mode == SsvProviderMode::Auto) {
            throw SsvConfigError(
                SsvConfigErrorKind::ConflictingFields,
                std::string(provider_path) + ".order",
                "auto provider mode must not define order");
        }
        require_sequence(
            order, "inference.runtime.providers.order");
        if (order.size() == 0) {
            throw SsvConfigError(
                SsvConfigErrorKind::InvalidValue,
                std::string(provider_path) + ".order",
                "explicit provider order must not be empty");
        }
        std::size_t index = 0;
        for (const auto &entry : order) {
            providers.order.push_back(parse_provider(
                node_as<std::string>(entry,
                    std::string(provider_path) + ".order["
                        + std::to_string(index) + "]"),
                std::string(provider_path) + ".order["
                    + std::to_string(index) + "]"));
            ++index;
        }
    } else if (providers.mode == SsvProviderMode::Explicit) {
        throw SsvConfigError(
            SsvConfigErrorKind::MissingRequired,
            std::string(provider_path) + ".order",
            "explicit provider mode requires order");
    }
    return providers;
}

std::optional<int> parse_cpu_threads(const YAML::Node &node)
{
    constexpr std::string_view path = "inference.runtime.cpu_threads";
    if (has_yaml_scalar_type<std::string>(node)) {
        if (node_as<std::string>(node, path) == "auto")
            return std::nullopt;
        throw_invalid_value(path,
            "inference.runtime.cpu_threads must be auto or a positive integer");
    }
    if (!has_yaml_scalar_type<int>(node))
        throw_invalid_type(path);

    const auto value = node_as<int>(node, path);
    if (value <= 0) {
        throw_invalid_value(path,
            "inference.runtime.cpu_threads must be auto or a positive integer");
    }
    return value;
}

SsvRuntimeConfig parse_runtime(const YAML::Node &node)
{
    if (!node)
        return SsvOnnxRuntimeConfig {};

    require_map(node, "inference.runtime");
    const auto type = get_or<std::string>(node,
        "type",
        "onnxruntime",
        "inference.runtime.type");
    if (type == "tensorrt-engine") {
        for (const auto forbidden : {
                 "providers", "precision", "cpu_threads", "cache"}) {
            if (node[forbidden]) {
                const auto path =
                    std::string("inference.runtime.") + forbidden;
                throw SsvConfigError(
                    SsvConfigErrorKind::ConflictingFields,
                    path,
                    path + " is not allowed for tensorrt-engine");
            }
        }
        reject_unknown_keys(
            node, "inference.runtime", {"type", "device_id"});
        SsvTensorRtEngineConfig runtime;
        runtime.device_id = get_or<int>(node,
            "device_id",
            runtime.device_id,
            "inference.runtime.device_id");
        if (runtime.device_id < 0) {
            throw_invalid_value(
                "inference.runtime.device_id",
                "inference.runtime.device_id must not be negative");
        }
        return runtime;
    }
    if (type != "onnxruntime") {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            "inference.runtime.type",
            "inference.runtime.type has invalid value: " + type);
    }

    reject_unknown_keys(node, "inference.runtime", {
        "type",
        "providers",
        "device_id",
        "precision",
        "cpu_threads",
        "cache",
    });

    SsvOnnxRuntimeConfig runtime;
    runtime.providers = parse_providers(node["providers"]);
    runtime.device_id = get_or<int>(node,
        "device_id",
        runtime.device_id,
        "inference.runtime.device_id");
    if (runtime.device_id < 0) {
        throw_invalid_value(
            "inference.runtime.device_id",
            "inference.runtime.device_id must not be negative");
    }
    runtime.precision = parse_precision(
        get_or<std::string>(node,
            "precision",
            "auto",
            "inference.runtime.precision"));
    if (const auto cpu_threads = node["cpu_threads"])
        runtime.cpu_threads = parse_cpu_threads(cpu_threads);
    if (const auto cache = node["cache"]) {
        require_map(cache, "inference.runtime.cache");
        reject_unknown_keys(
            cache, "inference.runtime.cache", {"enabled", "directory"});
        runtime.cache.enabled = get_or<bool>(cache,
            "enabled",
            runtime.cache.enabled,
            "inference.runtime.cache.enabled");
        runtime.cache.directory = get_or<std::string>(
            cache,
            "directory",
            runtime.cache.directory,
            "inference.runtime.cache.directory");
    }
    return runtime;
}

SsvInferenceConfig parse_inference(const YAML::Node &node)
{
    SsvInferenceConfig inference;
    if (!node)
        return inference;

    require_map(node, "inference");
    reject_unknown_keys(node, "inference", {
        "enabled",
        "analysis_fps",
        "model",
        "runtime",
        "confidence_threshold",
        "target_class",
    });

    inference.enabled = get_or<bool>(
        node, "enabled", inference.enabled, "inference.enabled");
    inference.analysis_fps = get_or<int>(node,
        "analysis_fps",
        inference.analysis_fps,
        "inference.analysis_fps");
    if (inference.analysis_fps < 0) {
        throw_invalid_value(
            "inference.analysis_fps",
            "inference.analysis_fps must not be negative");
    }
    inference.model = parse_model(node["model"]);
    inference.runtime = parse_runtime(node["runtime"]);
    if (std::holds_alternative<SsvTensorRtEngineConfig>(inference.runtime)) {
        if (!inference.model.manifest
            || is_blank(*inference.model.manifest)) {
            throw SsvConfigError(
                SsvConfigErrorKind::MissingRequired,
                "inference.model.manifest",
                "tensorrt-engine requires inference.model.manifest");
        }
    } else if (inference.model.manifest) {
        throw SsvConfigError(
            SsvConfigErrorKind::ConflictingFields,
            "inference.model.manifest",
            "onnxruntime must not define inference.model.manifest");
    }
    inference.confidence_threshold = get_or<float>(
        node,
        "confidence_threshold",
        inference.confidence_threshold,
        "inference.confidence_threshold");
    validate_unit_interval(inference.confidence_threshold,
        "inference.confidence_threshold");
    inference.target_class = get_or<std::string>(node,
        "target_class",
        inference.target_class,
        "inference.target_class");
    if (inference.enabled && !inference.model.preprocess) {
        throw SsvConfigError(
            SsvConfigErrorKind::MissingRequired,
            "inference.model.preprocess",
            "inference.model.preprocess is required when inference is enabled");
    }
    return inference;
}

SsvTrackingConfig parse_tracking(const YAML::Node &node)
{
    SsvTrackingConfig tracking;
    if (!node)
        return tracking;

    require_map(node, "tracking");
    reject_unknown_keys(node, "tracking", {
        "enabled",
        "track_threshold",
        "track_buffer",
        "match_threshold",
        "mock_track",
        "publish_cooldown_ms",
        "gmc",
    });

    tracking.enabled = get_or<bool>(
        node, "enabled", tracking.enabled, "tracking.enabled");
    tracking.track_threshold = get_or<float>(
        node,
        "track_threshold",
        tracking.track_threshold,
        "tracking.track_threshold");
    validate_unit_interval(
        tracking.track_threshold, "tracking.track_threshold");
    tracking.track_buffer = get_or<int>(node,
        "track_buffer",
        tracking.track_buffer,
        "tracking.track_buffer");
    if (tracking.track_buffer < 1 || tracking.track_buffer > 300) {
        throw_invalid_value(
            "tracking.track_buffer",
            "tracking.track_buffer must be between 1 and 300");
    }
    tracking.match_threshold = get_or<float>(
        node,
        "match_threshold",
        tracking.match_threshold,
        "tracking.match_threshold");
    validate_unit_interval(
        tracking.match_threshold, "tracking.match_threshold");
    tracking.mock_track = get_or<bool>(node,
        "mock_track",
        tracking.mock_track,
        "tracking.mock_track");
    tracking.publish_cooldown_ms = get_or<int>(node,
        "publish_cooldown_ms",
        tracking.publish_cooldown_ms,
        "tracking.publish_cooldown_ms");
    if (tracking.publish_cooldown_ms < 0) {
        throw_invalid_value(
            "tracking.publish_cooldown_ms",
            "tracking.publish_cooldown_ms must not be negative");
    }
    if (const auto gmc = node["gmc"]) {
        require_map(gmc, "tracking.gmc");
        reject_unknown_keys(gmc, "tracking.gmc", {"method", "downscale"});
        tracking.gmc.method = parse_gmc_method(
            get_or<std::string>(gmc,
                "method",
                "sparse-opt-flow",
                "tracking.gmc.method"));
        tracking.gmc.downscale = get_or<int>(gmc,
            "downscale",
            tracking.gmc.downscale,
            "tracking.gmc.downscale");
        if (tracking.gmc.downscale < 1 || tracking.gmc.downscale > 8) {
            throw_invalid_value(
                "tracking.gmc.downscale",
                "tracking.gmc.downscale must be between 1 and 8");
        }
    }
    return tracking;
}

void apply_deployment_overrides(SsvConfig &config)
{
    if (const auto uri = environment_value("SSV_RTSP_URL")) {
        validate_rtsp_uri(*uri, "environment.SSV_RTSP_URL");
        config.sources.front().uri = *uri;
    }
    if (const auto host = environment_value("REDIS_HOST"))
        config.redis.host = *host;
    if (const auto port_text = environment_value("REDIS_PORT")) {
        int port = 0;
        const auto result = std::from_chars(
            port_text->data(), port_text->data() + port_text->size(), port);
        if (result.ec != std::errc {}
            || result.ptr != port_text->data() + port_text->size()
            || port < 1
            || port > 65535) {
            throw_invalid_value("environment.REDIS_PORT",
                "REDIS_PORT must be an integer between 1 and 65535");
        }
        config.redis.port = port;
    }
}

SsvConfig parse_and_validate(const YAML::Node &root)
{
    require_map(root, "root");
    reject_unknown_keys(root, "", {
        "version",
        "logging",
        "redis",
        "sources",
        "display",
        "inference",
        "tracking",
        "agent",
    });

    if (!root["version"]) {
        throw SsvConfigError(
            SsvConfigErrorKind::MissingRequired,
            "version",
            "version is required");
    }
    if (!root["sources"]) {
        throw SsvConfigError(
            SsvConfigErrorKind::MissingRequired,
            "sources",
            "sources is required");
    }

    SsvConfig config;
    config.version = node_as<std::string>(root["version"], "version");
    if (config.version != "2.0") {
        throw SsvConfigError(
            SsvConfigErrorKind::InvalidValue,
            "version",
            "version must be \"2.0\"");
    }

    if (const auto logging = root["logging"]) {
        require_map(logging, "logging");
        reject_unknown_keys(
            logging, "logging", {"cpp_debug_level", "python_log_level"});
        config.logging.cpp_debug_level = get_or<std::string>(
            logging,
            "cpp_debug_level",
            config.logging.cpp_debug_level,
            "logging.cpp_debug_level");
        config.logging.python_log_level = get_or<std::string>(
            logging,
            "python_log_level",
            config.logging.python_log_level,
            "logging.python_log_level");
    }
    if (const auto redis = root["redis"]) {
        require_map(redis, "redis");
        reject_unknown_keys(redis, "redis", {
            "host",
            "port",
            "db",
            "stream_key",
            "consumer_group",
            // These fields are owned by the Python Agent but share this YAML.
            "reclaim_idle_ms",
            "reclaim_batch_size",
            "consumer_name",
        });
        config.redis.host = get_or<std::string>(
            redis, "host", config.redis.host, "redis.host");
        config.redis.port = get_or<int>(
            redis, "port", config.redis.port, "redis.port");
        if (config.redis.port < 1 || config.redis.port > 65535) {
            throw_invalid_value(
                "redis.port", "redis.port must be between 1 and 65535");
        }
        config.redis.db =
            get_or<int>(redis, "db", config.redis.db, "redis.db");
        if (config.redis.db < 0) {
            throw_invalid_value(
                "redis.db", "redis.db must not be negative");
        }
        config.redis.stream_key = get_or<std::string>(
            redis,
            "stream_key",
            config.redis.stream_key,
            "redis.stream_key");
        config.redis.consumer_group = get_or<std::string>(
            redis,
            "consumer_group",
            config.redis.consumer_group,
            "redis.consumer_group");
        const auto reclaim_idle_ms = get_or<int>(
            redis,
            "reclaim_idle_ms",
            60000,
            "redis.reclaim_idle_ms");
        if (reclaim_idle_ms <= 0) {
            throw_invalid_value(
                "redis.reclaim_idle_ms",
                "redis.reclaim_idle_ms must be positive");
        }
        const auto reclaim_batch_size = get_or<int>(
            redis,
            "reclaim_batch_size",
            10,
            "redis.reclaim_batch_size");
        if (reclaim_batch_size < 1 || reclaim_batch_size > 100) {
            throw_invalid_value(
                "redis.reclaim_batch_size",
                "redis.reclaim_batch_size must be between 1 and 100");
        }
        validate_optional_string_field(
            redis, "consumer_name", "redis.consumer_name", true);
    }
    if (const auto sources = root["sources"]) {
        require_sequence(sources, "sources");
        if (sources.size() != 1) {
            throw SsvConfigError(
                SsvConfigErrorKind::InvalidValue,
                "sources",
                "sources must contain exactly one item");
        }
        std::size_t index = 0;
        for (const auto &source : sources) {
            config.sources.push_back(parse_source(
                source,
                "sources[" + std::to_string(index) + "]"));
            ++index;
        }
    }
    config.display = parse_display(root["display"]);
    config.inference = parse_inference(root["inference"]);
    config.tracking = parse_tracking(root["tracking"]);
    if (const auto agent = root["agent"]) {
        require_map(agent, "agent");
        reject_unknown_keys(
            agent, "agent", {
                "state_machine_timeout",
                "max_retries",
                "model_name",
                "output_dir",
                "dedup_enabled",
                "dedup_cooldown_seconds",
                // The Python Agent owns these shared-config extensions.
                "evidence_roots",
                "review",
                "indexing",
                "knowledge",
            });
        config.agent.state_machine_timeout = get_or<int>(
            agent,
            "state_machine_timeout",
            config.agent.state_machine_timeout,
            "agent.state_machine_timeout");
        if (config.agent.state_machine_timeout <= 0) {
            throw_invalid_value("agent.state_machine_timeout",
                "agent.state_machine_timeout must be positive");
        }
        config.agent.max_retries = get_or<int>(agent,
            "max_retries",
            config.agent.max_retries,
            "agent.max_retries");
        if (config.agent.max_retries < 0) {
            throw_invalid_value(
                "agent.max_retries", "agent.max_retries must not be negative");
        }
        config.agent.model_name = get_or<std::string>(
            agent,
            "model_name",
            config.agent.model_name,
            "agent.model_name");
        config.agent.output_dir = get_or<std::string>(
            agent,
            "output_dir",
            config.agent.output_dir,
            "agent.output_dir");
        config.agent.dedup_enabled = get_or<bool>(
            agent,
            "dedup_enabled",
            config.agent.dedup_enabled,
            "agent.dedup_enabled");
        config.agent.dedup_cooldown_seconds = get_or<float>(
            agent,
            "dedup_cooldown_seconds",
            config.agent.dedup_cooldown_seconds,
            "agent.dedup_cooldown_seconds");
        if (config.agent.dedup_cooldown_seconds <= 0) {
            throw_invalid_value(
                "agent.dedup_cooldown_seconds",
                "agent.dedup_cooldown_seconds must be positive");
        }
        validate_agent_extensions(agent);
    }
    return config;
}

} // namespace

SsvConfig ssv_config_load(const std::string &path)
{
    const auto resolved = resolve_config_path(path);
    auto config = parse_and_validate(read_yaml(resolved));
    apply_deployment_overrides(config);
    return config;
}

SsvRunOptions ssv_run_options_parse(
    std::span<const std::string_view> arguments)
{
    SsvRunOptions options;
    bool seen_config = false;
    bool seen_display_backend = false;
    for (std::size_t index = 0; index < arguments.size(); ++index) {
        const auto argument = arguments[index];
        if (argument == "--config") {
            if (seen_config) {
                throw SsvConfigError(
                    SsvConfigErrorKind::ConflictingFields,
                    "cli.config",
                    "--config may only be specified once");
            }
            seen_config = true;
            if (++index == arguments.size()
                || arguments[index].empty()
                || arguments[index].starts_with("--")) {
                throw SsvConfigError(
                    SsvConfigErrorKind::MissingRequired,
                    "cli.config",
                    "--config requires a non-empty path");
            }
            options.config_path = arguments[index];
        } else if (argument == "--display") {
            if (options.overrides.display_enabled == false) {
                throw SsvConfigError(
                    SsvConfigErrorKind::ConflictingFields,
                    "cli.display",
                    "--display and --headless are mutually exclusive");
            }
            options.overrides.display_enabled = true;
        } else if (argument == "--headless") {
            if (options.overrides.display_enabled == true) {
                const auto path = options.overrides.overlay_enabled
                    ? "cli.overlay"
                    : "cli.display";
                throw SsvConfigError(
                    SsvConfigErrorKind::ConflictingFields,
                    path,
                    "--headless conflicts with display output");
            }
            options.overrides.display_enabled = false;
        } else if (argument == "--overlay") {
            if (options.overrides.display_enabled == false) {
                throw SsvConfigError(
                    SsvConfigErrorKind::ConflictingFields,
                    "cli.overlay",
                    "--overlay and --headless are mutually exclusive");
            }
            options.overrides.display_enabled = true;
            options.overrides.overlay_enabled = true;
        } else if (argument == "--display-backend") {
            if (seen_display_backend) {
                throw SsvConfigError(
                    SsvConfigErrorKind::ConflictingFields,
                    "cli.display_backend",
                    "--display-backend may only be specified once");
            }
            seen_display_backend = true;
            if (++index == arguments.size()
                || arguments[index].empty()
                || arguments[index].starts_with("--")) {
                throw SsvConfigError(
                    SsvConfigErrorKind::MissingRequired,
                    "cli.display_backend",
                    "--display-backend requires gtkglsink or gtksink");
            }
            if (arguments[index] == "gtkglsink") {
                options.overrides.display_backend =
                    SsvDisplayBackend::GtkGlSink;
            } else if (arguments[index] == "gtksink") {
                options.overrides.display_backend =
                    SsvDisplayBackend::GtkSink;
            } else {
                throw SsvConfigError(
                    SsvConfigErrorKind::InvalidValue,
                    "cli.display_backend",
                    "--display-backend must be gtkglsink or gtksink");
            }
        } else {
            throw SsvConfigError(
                SsvConfigErrorKind::UnknownKey,
                "cli.arguments",
                "unknown run argument: " + std::string(argument));
        }
    }
    return options;
}

void ssv_config_apply_overrides(
    SsvConfig &config,
    const SsvConfigOverrides &overrides)
{
    if (overrides.display_enabled)
        config.display.enabled = *overrides.display_enabled;
    if (overrides.overlay_enabled)
        config.display.overlay.enabled = *overrides.overlay_enabled;
    if (overrides.display_backend)
        config.display.backend = *overrides.display_backend;
}

SsvFatalError ssv_make_fatal_error(
    const SsvConfigError &error,
    std::string source_id)
{
    const auto stage = error.path().starts_with("cli")
        ? "cli"
        : "config";
    return {
        error.exit_code(),
        stage,
        std::move(source_id),
        error.what(),
    };
}

std::string ssv_format_fatal_error(const SsvFatalError &error)
{
    const auto exit_code = static_cast<int>(error.exit_code);
    if (exit_code < 2 || exit_code > 7
        || is_blank(error.stage)
        || is_blank(error.source_id)
        || is_blank(error.error)) {
        throw std::invalid_argument(
            "fatal_error requires a non-zero exit code, stage, source_id, and error");
    }

    return "event=fatal_error exit_code=" + std::to_string(exit_code)
        + " stage=" + format_log_value(error.stage)
        + " source_id=" + format_log_value(error.source_id)
        + " error=" + format_log_value(error.error);
}

} // namespace ssv
