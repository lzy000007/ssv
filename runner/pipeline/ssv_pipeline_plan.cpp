#include "ssv_pipeline_plan.hpp"

#include <cctype>
#include <array>
#include <string_view>
#include <variant>

namespace ssv {
namespace {

constexpr auto kGtkGlDmabufRequirement =
    "gtkglsink requires DMABuf input from the resolved decoder";

constexpr std::array<std::string_view, 3> kSoftwareEncoderFactories {
    "openh264enc",
    "x264enc",
    "avenc_h264",
};

bool is_blank(std::string_view value)
{
    for (const unsigned char character : value) {
        if (!std::isspace(character))
            return false;
    }
    return true;
}

bool is_valid_rtsp_location(std::string_view value)
{
    return !is_blank(value)
        && (value.starts_with("rtsp://") || value.starts_with("rtsps://"))
        && !value.ends_with("://");
}

[[noreturn]] void fail_plan(
    SsvExitCode exit_code,
    std::string stage,
    std::string message)
{
    throw SsvPipelinePlanError(
        exit_code, std::move(stage), std::move(message));
}

std::string va_device_factory(
    const SsvDecodeDevice &device,
    std::string_view suffix)
{
    if (device.kind != SsvDecodeDeviceKind::Drm)
        return {};
    const auto separator = device.value.find_last_of('/');
    const auto basename = separator == std::string::npos
        ? device.value
        : device.value.substr(separator + 1);
    return "va" + basename + std::string(suffix);
}

std::string nvdec_factory(const SsvDecodeDevice &device)
{
    if (device.kind != SsvDecodeDeviceKind::Cuda || device.value == "0")
        return "nvh264dec";
    return "nvh264device" + device.value + "dec";
}

std::string nvenc_factory(const SsvDecodeDevice &device)
{
    if (device.kind != SsvDecodeDeviceKind::Cuda || device.value == "0")
        return "nvh264enc";
    return "nvh264device" + device.value + "enc";
}

std::string require_factory(
    const SsvCapabilitySnapshot &capabilities,
    std::string preferred,
    std::string fallback,
    std::string_view purpose,
    std::string_view stage_name = "capability.decode")
{
    if (!preferred.empty()
        && capabilities.has_gstreamer_element(preferred)) {
        return preferred;
    }
    if (!fallback.empty()
        && capabilities.has_gstreamer_element(fallback)) {
        return fallback;
    }
    const auto missing = preferred.empty() ? fallback : preferred;
    fail_plan(
        SsvExitCode::CapabilityUnavailable,
        std::string(stage_name),
        "required GStreamer " + std::string(purpose)
            + " is unavailable: " + missing);
}

bool has_factory(
    const SsvCapabilitySnapshot &capabilities,
    std::string_view preferred,
    std::string_view fallback = {})
{
    const bool preferred_available = !preferred.empty()
        && capabilities.has_gstreamer_element(preferred);
    const bool fallback_available = !fallback.empty()
        && capabilities.has_gstreamer_element(fallback);
    return preferred_available || fallback_available;
}

SsvDecodePlan make_vaapi_plan(
    const SsvDecodeConfig &decode,
    const SsvCapabilitySnapshot &capabilities,
    bool software_fallback_allowed)
{
    return {
        .backend = SsvDecodeBackend::Vaapi,
        .device = decode.device,
        .decoder_factory = require_factory(
            capabilities,
            va_device_factory(decode.device, "h264dec"),
            "vah264dec",
            "VA H.264 decoder"),
        .va_postproc_factory = require_factory(
            capabilities,
            va_device_factory(decode.device, "postproc"),
            "vapostproc",
            "VA post-processor"),
        .software_fallback_allowed = software_fallback_allowed,
    };
}

SsvDecodePlan make_nvdec_plan(
    const SsvDecodeConfig &decode,
    const SsvCapabilitySnapshot &capabilities,
    bool software_fallback_allowed)
{
    const auto factory = nvdec_factory(decode.device);
    return {
        .backend = SsvDecodeBackend::Nvdec,
        .device = decode.device,
        .decoder_factory = require_factory(
            capabilities, factory, {}, "NVDEC H.264 decoder"),
        .va_postproc_factory = {},
        .software_fallback_allowed = software_fallback_allowed,
    };
}

SsvDecodePlan make_software_plan(
    const SsvDecodeConfig &decode,
    const SsvCapabilitySnapshot &capabilities,
    bool software_fallback_allowed)
{
    return {
        .backend = SsvDecodeBackend::Software,
        .device = decode.device,
        .decoder_factory = require_factory(
            capabilities, "avdec_h264", {}, "software H.264 decoder"),
        .va_postproc_factory = {},
        .software_fallback_allowed = software_fallback_allowed,
    };
}

SsvDecodePlan make_software_fallback_plan(
    const SsvDecodeConfig &decode,
    const SsvCapabilitySnapshot &capabilities)
{
    auto software = decode;
    software.mode = SsvDecodeMode::Software;
    software.device = {};
    return make_software_plan(software, capabilities, true);
}

SsvDecodePlan resolve_decode_plan(
    const SsvDecodeConfig &decode,
    const SsvCapabilitySnapshot &capabilities)
{
    const bool allow_software_fallback =
        decode.mode == SsvDecodeMode::Auto;

    switch (decode.mode) {
    case SsvDecodeMode::Auto:
        if (decode.device.kind == SsvDecodeDeviceKind::Drm) {
            const auto decoder = va_device_factory(
                decode.device, "h264dec");
            const auto postproc = va_device_factory(
                decode.device, "postproc");
            if (has_factory(capabilities, decoder, "vah264dec")
                && has_factory(capabilities, postproc, "vapostproc")) {
                return make_vaapi_plan(
                    decode, capabilities, allow_software_fallback);
            }
            return make_software_fallback_plan(decode, capabilities);
        }
        if (decode.device.kind == SsvDecodeDeviceKind::Cuda) {
            if (has_factory(capabilities, nvdec_factory(decode.device))) {
                return make_nvdec_plan(
                    decode, capabilities, allow_software_fallback);
            }
            return make_software_fallback_plan(decode, capabilities);
        }
        if (capabilities.has_gstreamer_element("vah264dec")
            && capabilities.has_gstreamer_element("vapostproc")) {
            return make_vaapi_plan(
                decode, capabilities, allow_software_fallback);
        }
        if (capabilities.has_gstreamer_element("nvh264dec")) {
            return make_nvdec_plan(
                decode, capabilities, allow_software_fallback);
        }
        return make_software_plan(
            decode, capabilities, allow_software_fallback);
    case SsvDecodeMode::Vaapi:
        return make_vaapi_plan(decode, capabilities, false);
    case SsvDecodeMode::Nvdec:
        return make_nvdec_plan(decode, capabilities, false);
    case SsvDecodeMode::Software:
        return make_software_plan(decode, capabilities, false);
    }
    fail_plan(
        SsvExitCode::InvalidConfiguration,
        "config",
        "unsupported decode mode");
}

SsvDecodePlan make_passthrough_decode_plan(const SsvDecodeConfig &decode)
{
    // Encoded RTSP passthrough has no decoded branch. Keep the public plan
    // shape stable, but make the unused decoder capability explicit.
    return {
        .backend = SsvDecodeBackend::Software,
        .device = decode.device,
        .decoder_factory = "not-applicable",
        .va_postproc_factory = {},
        .software_fallback_allowed = false,
    };
}

SsvMemoryKind decode_memory(SsvDecodeBackend backend)
{
    switch (backend) {
    case SsvDecodeBackend::Vaapi:
        return SsvMemoryKind::VaMemory;
    case SsvDecodeBackend::Nvdec:
        return SsvMemoryKind::CudaMemory;
    case SsvDecodeBackend::Software:
        return SsvMemoryKind::SystemMemory;
    }
    return SsvMemoryKind::Unknown;
}

std::optional<std::string> find_encoder_factory(
    const SsvCapabilitySnapshot &capabilities,
    std::string preferred,
    std::string fallback)
{
    if (!preferred.empty()
        && capabilities.has_gstreamer_element(preferred)) {
        return preferred;
    }
    if (!fallback.empty()
        && capabilities.has_gstreamer_element(fallback)) {
        return fallback;
    }
    return std::nullopt;
}

SsvEncodePlan make_software_encode_plan(
    const SsvCapabilitySnapshot &capabilities)
{
    for (const auto factory : kSoftwareEncoderFactories) {
        if (capabilities.has_gstreamer_element(factory)) {
            return {
                .backend = SsvEncodeBackend::Software,
                .device = {},
                .encoder_factory = std::string(factory),
                .input_memory = SsvMemoryKind::SystemMemory,
            };
        }
    }
    fail_plan(
        SsvExitCode::CapabilityUnavailable,
        "capability.encode",
        "RTSP display backend requires an H.264 encoder");
}

std::optional<SsvEncodePlan> resolve_hardware_encode_plan(
    const SsvDecodePlan &decode,
    const SsvCapabilitySnapshot &capabilities)
{
    // An explicit selector is copied to the encoder. With an Auto selector,
    // the matching generic factory pair uses the same backend default device.
    // The builder still validates explicit device properties when available.
    switch (decode.backend) {
    case SsvDecodeBackend::Vaapi: {
        const auto factory = find_encoder_factory(
            capabilities,
            va_device_factory(decode.device, "h264enc"),
            "vah264enc");
        if (!factory)
            return std::nullopt;
        return SsvEncodePlan {
            .backend = SsvEncodeBackend::Vaapi,
            .device = decode.device,
            .encoder_factory = *factory,
            .input_memory = SsvMemoryKind::VaMemory,
        };
    }
    case SsvDecodeBackend::Nvdec: {
        const auto factory = find_encoder_factory(
            capabilities, nvenc_factory(decode.device), "nvh264enc");
        if (!factory)
            return std::nullopt;
        return SsvEncodePlan {
            .backend = SsvEncodeBackend::Nvenc,
            .device = decode.device,
            .encoder_factory = *factory,
            .input_memory = SsvMemoryKind::CudaMemory,
        };
    }
    case SsvDecodeBackend::Software:
        return std::nullopt;
    }
    return std::nullopt;
}

SsvCodecPlan resolve_codec_plan(
    const SsvConfig &config,
    const SsvCapabilitySnapshot &capabilities,
    SsvDecodePlan decode,
    std::optional<SsvResolvedDisplayBackend> display_backend,
    bool encoded_passthrough,
    SsvPipelineResolveOptions options)
{
    SsvCodecPlan codec {
        .decode = std::move(decode),
        .encode = std::nullopt,
        .path = SsvCodecPath::Passthrough,
        .mixed_fallback_allowed = false,
        .fallbacks = {},
        .decode_fallbacks = {},
        .decoded_path_required = true,
    };
    if (display_backend != SsvResolvedDisplayBackend::RtspClientSink
        || encoded_passthrough) {
        codec.path = encoded_passthrough
            ? SsvCodecPath::Passthrough
            : SsvCodecPath::DecodeOnly;
        return codec;
    }

    const bool hardware_decoder = codec.decode.backend
        != SsvDecodeBackend::Software;
    const auto hardware_encoder = hardware_decoder
        ? resolve_hardware_encode_plan(codec.decode, capabilities)
        : std::nullopt;
    const bool needs_system_memory = config.display.rtsp.burn_in_overlay;

    if (!options.force_mixed_codec && hardware_encoder && !needs_system_memory) {
        codec.encode = hardware_encoder;
        codec.path = SsvCodecPath::HardwarePair;
        codec.mixed_fallback_allowed = true;
        return codec;
    }

    codec.encode = make_software_encode_plan(capabilities);
    if (hardware_decoder) {
        codec.path = SsvCodecPath::Mixed;
        if (!options.force_mixed_codec) {
            codec.fallbacks.push_back({
                .from = SsvCodecPath::HardwarePair,
                .to = SsvCodecPath::Mixed,
                .reason = needs_system_memory
                    ? "CPU overlay requires SystemMemory before encoding"
                    : "matching hardware H.264 encoder is unavailable",
            });
        }
    } else {
        codec.path = SsvCodecPath::Software;
    }
    return codec;
}

std::vector<SsvDecodeFallbackDecision> resolve_decode_fallbacks(
    const SsvDecodeConfig &decode,
    const SsvDecodePlan &resolved)
{
    if (decode.mode != SsvDecodeMode::Auto)
        return {};

    const auto va_reason =
        "VA H.264 decoder or post-processor is unavailable";
    const auto nvdec_reason = "NVDEC H.264 decoder is unavailable";
    if (decode.device.kind == SsvDecodeDeviceKind::Drm) {
        if (resolved.backend == SsvDecodeBackend::Software) {
            return {{
                SsvDecodeBackend::Vaapi,
                SsvDecodeBackend::Software,
                va_reason,
            }};
        }
        return {};
    }
    if (decode.device.kind == SsvDecodeDeviceKind::Cuda) {
        if (resolved.backend == SsvDecodeBackend::Software) {
            return {{
                SsvDecodeBackend::Nvdec,
                SsvDecodeBackend::Software,
                nvdec_reason,
            }};
        }
        return {};
    }
    if (resolved.backend == SsvDecodeBackend::Vaapi)
        return {};
    if (resolved.backend == SsvDecodeBackend::Nvdec) {
        return {{
            SsvDecodeBackend::Vaapi,
            SsvDecodeBackend::Nvdec,
            va_reason,
        }};
    }
    return {
        {
            SsvDecodeBackend::Vaapi,
            SsvDecodeBackend::Nvdec,
            va_reason,
        },
        {
            SsvDecodeBackend::Nvdec,
            SsvDecodeBackend::Software,
            nvdec_reason,
        },
    };
}

void validate_decode_device(const SsvDecodeConfig &decode)
{
    const bool compatible = [&] {
        switch (decode.mode) {
        case SsvDecodeMode::Auto:
            return true;
        case SsvDecodeMode::Vaapi:
            return decode.device.kind != SsvDecodeDeviceKind::Cuda;
        case SsvDecodeMode::Nvdec:
            return decode.device.kind != SsvDecodeDeviceKind::Drm;
        case SsvDecodeMode::Software:
            return decode.device.kind == SsvDecodeDeviceKind::Auto;
        }
        return false;
    }();
    if (!compatible) {
        fail_plan(
            SsvExitCode::InvalidConfiguration,
            "config.sources[0].decode.device",
            "decode device selector is incompatible with decode mode");
    }
}

bool decode_exports_dmabuf(SsvDecodeBackend backend)
{
    return backend == SsvDecodeBackend::Vaapi;
}

std::optional<SsvResolvedDisplayBackend> resolve_display_backend(
    const SsvDisplayConfig &display,
    const SsvCapabilitySnapshot &capabilities,
    SsvDecodeBackend decode_backend)
{
    if (!display.enabled)
        return std::nullopt;

    const bool gtk_gl_elements_available =
        capabilities.has_gstreamer_element("gtkglsink")
        && capabilities.has_gstreamer_element("glupload")
        && capabilities.has_gstreamer_element("glcolorconvert");
    const bool gtk_available =
        capabilities.has_gstreamer_element("gtksink");

    switch (display.backend) {
    case SsvDisplayBackend::Auto:
        if (gtk_gl_elements_available
            && decode_exports_dmabuf(decode_backend)) {
            return SsvResolvedDisplayBackend::GtkGlSink;
        }
        if (gtk_available)
            return SsvResolvedDisplayBackend::GtkSink;
        break;
    case SsvDisplayBackend::GtkGlSink:
        if (!decode_exports_dmabuf(decode_backend)) {
            fail_plan(
                SsvExitCode::CapabilityUnavailable,
                "capability.display",
                kGtkGlDmabufRequirement);
        }
        if (gtk_gl_elements_available)
            return SsvResolvedDisplayBackend::GtkGlSink;
        break;
    case SsvDisplayBackend::GtkSink:
        if (gtk_available)
            return SsvResolvedDisplayBackend::GtkSink;
        break;
    case SsvDisplayBackend::RtspClientSink:
        if (!is_valid_rtsp_location(display.rtsp.location)) {
            fail_plan(
                SsvExitCode::InvalidConfiguration,
                "display.rtsp.location",
                "RTSP display backend requires a publish location");
        }
        if (!capabilities.has_gstreamer_element("rtspclientsink")) {
            fail_plan(
                SsvExitCode::CapabilityUnavailable,
                "capability.display",
                "RTSP display backend requires rtspclientsink");
        }
        if (!capabilities.has_gstreamer_element("rtph264pay")) {
            fail_plan(
                SsvExitCode::CapabilityUnavailable,
                "capability.display",
                "RTSP display backend requires rtph264pay");
        }
        if (display.rtsp.burn_in_overlay
            && !capabilities.has_gstreamer_element("ssvoverlay")) {
            fail_plan(
                SsvExitCode::CapabilityUnavailable,
                "capability.display",
                "RTSP overlay requires ssvoverlay");
        }
        return SsvResolvedDisplayBackend::RtspClientSink;
    }
    fail_plan(
        SsvExitCode::CapabilityUnavailable,
        "capability.display",
        "required GTK display backend is unavailable");
}

std::string resolve_display_encoder_factory(
    std::optional<SsvResolvedDisplayBackend> display_backend,
    const SsvHardwareCapabilities &capabilities)
{
    if (display_backend != SsvResolvedDisplayBackend::RtspClientSink)
        return {};
    for (const auto factory : kSoftwareEncoderFactories) {
        if (capabilities.has_gstreamer_element(factory))
            return std::string(factory);
    }
    fail_plan(
        SsvExitCode::CapabilityUnavailable,
        "capability.display",
        "RTSP display backend requires an H.264 encoder");
}

std::vector<std::string> resolve_display_fallback_reasons(
    const SsvDisplayConfig &display,
    const SsvCapabilitySnapshot &capabilities,
    SsvDecodeBackend decode_backend,
    std::optional<SsvResolvedDisplayBackend> resolved_backend)
{
    std::vector<std::string> reasons;
    if (!display.enabled || display.backend != SsvDisplayBackend::Auto
        || resolved_backend != SsvResolvedDisplayBackend::GtkSink) {
        return reasons;
    }
    if (!decode_exports_dmabuf(decode_backend)) {
        reasons.emplace_back(kGtkGlDmabufRequirement);
    }
    for (const char *factory : {
             "gtkglsink",
             "glupload",
             "glcolorconvert",
         }) {
        if (!capabilities.has_gstreamer_element(factory)) {
            reasons.emplace_back(
                "missing GStreamer element: " + std::string(factory));
        }
    }
    return reasons;
}

std::optional<SsvInferenceBackend> resolve_inference_backend(
    const SsvInferenceConfig &inference,
    const SsvCapabilitySnapshot &capabilities)
{
    if (!inference.enabled)
        return std::nullopt;

    if (std::holds_alternative<SsvOnnxRuntimeConfig>(inference.runtime)) {
        if (capabilities.onnxruntime_available())
            return SsvInferenceBackend::OnnxRuntime;
        fail_plan(
            SsvExitCode::ModelInitializationFailed,
            "inference.resolve",
            "ONNX Runtime is unavailable");
    }
    if (capabilities.tensorrt_engine_available())
        return SsvInferenceBackend::TensorRtEngine;
    fail_plan(
        SsvExitCode::ModelInitializationFailed,
        "inference.resolve",
        "TensorRT engine runtime is unavailable");
}

SsvInferencePlan resolve_inference_plan(
    const SsvInferenceConfig &inference,
    const SsvCapabilitySnapshot &capabilities)
{
    const auto backend = resolve_inference_backend(inference, capabilities);
    if (!backend)
        return {};

    return {
        .backend = backend,
        .available_providers = {
            capabilities.onnxruntime_providers().begin(),
            capabilities.onnxruntime_providers().end(),
        },
    };
}

std::optional<SsvTrackingPlan> resolve_tracking_plan(
    const SsvConfig &config)
{
    if (!config.tracking.enabled)
        return std::nullopt;
    return SsvTrackingPlan {
        .nominal_frame_rate = config.inference.analysis_fps > 0
            ? std::optional<int> {config.inference.analysis_fps}
            : std::nullopt,
    };
}

SsvPipelineExpectedCaps resolve_expected_caps(
    const SsvCodecPlan &codec,
    std::optional<SsvResolvedDisplayBackend> display_backend,
    bool burn_in_overlay,
    bool encoded_passthrough,
    std::optional<SsvInferenceBackend> inference_backend)
{
    const bool hardware_decode =
        codec.decode.backend != SsvDecodeBackend::Software;
    SsvPipelineExpectedCaps caps {
        .decode_output = {
            SsvPixelFormat::Nv12,
            decode_memory(codec.decode.backend),
        },
        .display_upload_input = std::nullopt,
        .display_sink_input = std::nullopt,
        .display_overlay_input = std::nullopt,
        .display_encode_input = std::nullopt,
        .analysis_gpu_input = std::nullopt,
        .analysis_host_input = std::nullopt,
    };

    if (display_backend == SsvResolvedDisplayBackend::GtkGlSink) {
        caps.display_upload_input = SsvVideoCaps {
            SsvPixelFormat::Nv12,
            SsvMemoryKind::DmaBuf,
        };
        caps.display_sink_input = SsvVideoCaps {
            SsvPixelFormat::Rgba,
            SsvMemoryKind::GlMemory,
        };
    } else if (display_backend == SsvResolvedDisplayBackend::GtkSink) {
        caps.display_sink_input = SsvVideoCaps {
            SsvPixelFormat::Bgrx,
            SsvMemoryKind::SystemMemory,
        };
    } else if (display_backend == SsvResolvedDisplayBackend::RtspClientSink
        && !encoded_passthrough) {
        if (burn_in_overlay) {
            caps.display_overlay_input = SsvVideoCaps {
                SsvPixelFormat::Bgrx,
                SsvMemoryKind::SystemMemory,
            };
        }
        if (codec.encode
            && codec.encode->input_memory != SsvMemoryKind::SystemMemory) {
            caps.display_encode_input = SsvVideoCaps {
                SsvPixelFormat::Nv12,
                codec.encode->input_memory,
            };
        } else {
            caps.display_encode_input = SsvVideoCaps {
                SsvPixelFormat::I420,
                SsvMemoryKind::SystemMemory,
            };
        }
    }

    if (inference_backend) {
        if (hardware_decode) {
            caps.analysis_gpu_input = SsvVideoCaps {
                SsvPixelFormat::Nv12,
                decode_memory(codec.decode.backend),
            };
        }
        caps.analysis_host_input = SsvVideoCaps {
            SsvPixelFormat::Rgba,
            SsvMemoryKind::SystemMemory,
        };
    }
    return caps;
}

} // namespace

SsvPipelinePlanError::SsvPipelinePlanError(
    SsvExitCode exit_code,
    std::string stage,
    std::string message)
    : std::runtime_error(std::move(message))
    , exit_code_(exit_code)
    , stage_(std::move(stage))
{
}

SsvExitCode SsvPipelinePlanError::exit_code() const noexcept
{
    return exit_code_;
}

const std::string &SsvPipelinePlanError::stage() const noexcept
{
    return stage_;
}

SsvPipelinePlan SsvPipelinePlan::resolve(
    const SsvConfig &config,
    const SsvCapabilitySnapshot &capabilities,
    SsvPipelineResolveOptions options)
{
    if (config.sources.size() != 1 || is_blank(config.sources.front().id)) {
        fail_plan(
            SsvExitCode::InvalidConfiguration,
            "config",
            "pipeline plan requires exactly one source with a non-empty id");
    }

    const bool encoded_passthrough = config.display.enabled
        && config.display.backend == SsvDisplayBackend::RtspClientSink
        && config.display.rtsp.encoded_passthrough;
    if (encoded_passthrough && config.display.rtsp.burn_in_overlay) {
        fail_plan(
            SsvExitCode::InvalidConfiguration,
            "display.rtsp.encoded_passthrough",
            "encoded RTSP passthrough cannot be combined with burn-in overlay");
    }

    validate_decode_device(config.sources.front().decode);

    const bool decoded_path_required =
        !encoded_passthrough || config.inference.enabled;
    const auto decode = decoded_path_required
        ? resolve_decode_plan(config.sources.front().decode, capabilities)
        : make_passthrough_decode_plan(config.sources.front().decode);
    const auto display_backend =
        resolve_display_backend(config.display, capabilities, decode.backend);
    auto codec = resolve_codec_plan(
        config,
        capabilities,
        decode,
        display_backend,
        encoded_passthrough,
        options);
    codec.decoded_path_required = decoded_path_required;
    if (decoded_path_required) {
        codec.decode_fallbacks = resolve_decode_fallbacks(
            config.sources.front().decode, codec.decode);
    }
    const auto inference =
        resolve_inference_plan(config.inference, capabilities);

    return {
        .source_id = config.sources.front().id,
        .capability_snapshot = capabilities,
        .inference = inference,
        .codec = codec,
        .display_backend = display_backend,
        .display_encoded_passthrough = encoded_passthrough,
        .display_fallback_allowed = config.display.enabled
            && config.display.backend == SsvDisplayBackend::Auto
            && display_backend == SsvResolvedDisplayBackend::GtkGlSink
            && capabilities.has_gstreamer_element("gtksink"),
        .display_fallback_reasons = resolve_display_fallback_reasons(
            config.display, capabilities, decode.backend, display_backend),
        .tracking = resolve_tracking_plan(config),
        .expected_caps = resolve_expected_caps(
            codec,
            display_backend,
            config.display.rtsp.burn_in_overlay,
            encoded_passthrough,
            inference.backend),
    };
}

} // namespace ssv
