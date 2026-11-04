#include "pipeline/ssv_pipeline_builder.hpp"
#include "pipeline/ssv_pipeline_topology.hpp"
#include "ssv_inference_test_service.hpp"

#include <gst/gst.h>

#include <algorithm>
#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <initializer_list>
#include <string>
#include <unistd.h>
#include <vector>

namespace {

void mark_finalized(gpointer data, GObject *)
{
    *static_cast<bool *>(data) = true;
}

struct FinalizationRecord {
    std::vector<std::string> *events;
    const char *name;
};

void record_finalized(gpointer data, GObject *)
{
    const auto &record = *static_cast<FinalizationRecord *>(data);
    record.events->push_back(record.name);
}

ssv::SsvConfig make_config()
{
    ssv::SsvConfig config;
    config.evidence_cache.directory =
        (std::filesystem::temp_directory_path()
            / ("ssv-pipeline-builder-default-cache-"
                + std::to_string(::getpid())))
            .string();
    ssv::SsvSourceConfig source;
    source.id = "camera-01";
    source.uri = "rtsp://127.0.0.1/test";
    source.event_rule.event_type = "person_without_helmet";
    source.event_rule.severity = "high";
    source.event_rule.rule_id = "rule-1";
    source.event_rule.rule_version = "v1";
    source.event_rule.rule_facts_json = R"({"helmet_required":true})";
    source.decode.mode = ssv::SsvDecodeMode::Vaapi;
    source.decode.device = {
        ssv::SsvDecodeDeviceKind::Drm,
        "/dev/dri/renderD129",
    };
    config.sources.push_back(std::move(source));
    config.display.fps = 24;
    config.inference.analysis_fps = 12;
    return config;
}

ssv::SsvHardwareCapabilities make_registry()
{
    ssv::SsvHardwareCapabilities capabilities;
    capabilities.gstreamer_elements = {
        "rtspsrc",
        "capsfilter",
        "rtph264depay",
        "h264parse",
        "varenderD129h264dec",
        "varenderD129postproc",
        "clocksync",
        "tee",
        "queue",
        "videorate",
        "fakesink",
        "ssvinfer",
        "ssvtrack",
        "ssvpub",
        "gtkglsink",
        "glupload",
        "glcolorconvert",
        "gtksink",
        "videoconvert",
        "videoscale",
        "cudadownload",
        "avdec_h264",
        "openh264enc",
        "rtph264pay",
        "rtspclientsink",
        "splitmuxsink",
        "h264timestamper",
        "mp4mux",
    };
    capabilities.onnxruntime_available = true;
    capabilities.tensorrt_engine_available = false;
    return capabilities;
}

ssv::SsvCapabilitySnapshot snapshot_of(
    const ssv::SsvHardwareCapabilities &capabilities)
{
    return ssv::SsvCapabilitySnapshot(capabilities);
}

bool register_test_rtsp_sink_alias()
{
    auto *existing = gst_registry_lookup_feature(
        gst_registry_get(), "rtspclientsink");
    if (existing != nullptr) {
        gst_object_unref(existing);
        return false;
    }

    auto *fakesink = gst_element_factory_make("fakesink", nullptr);
    assert(fakesink != nullptr);
    const auto element_type = G_OBJECT_TYPE(fakesink);
    const gboolean registered = gst_element_register(
        nullptr, "rtspclientsink", GST_RANK_NONE, element_type);
    gst_object_unref(fakesink);
    assert(registered);
    return true;
}

void unregister_test_rtsp_sink_alias(bool registered)
{
    if (!registered)
        return;
    auto *feature = gst_registry_lookup_feature(
        gst_registry_get(), "rtspclientsink");
    assert(feature != nullptr);
    gst_registry_remove_feature(gst_registry_get(), feature);
    gst_object_unref(feature);
}


std::vector<std::string> factories(
    const std::vector<ssv::pipeline_internal::PipelineStage> &stages)
{
    std::vector<std::string> result;
    result.reserve(stages.size());
    for (const auto &stage : stages)
        result.push_back(stage.factory);
    return result;
}

const ssv::SsvPipelineContractExpectation &contract_at(
    const ssv::pipeline_internal::PipelineTopology &topology,
    ssv::SsvPipelineBoundary boundary)
{
    const auto contract = std::ranges::find(
        topology.contracts,
        boundary,
        &ssv::SsvPipelineContractExpectation::boundary);
    assert(contract != topology.contracts.end());
    return *contract;
}

void test_va_topology_freezes_order_backpressure_and_rate_contracts()
{
    const auto config = make_config();
    const auto registry = make_registry();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};

    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, model);

    assert(factories(topology.source_path) == std::vector<std::string>({
        "rtspsrc",
        "capsfilter",
        "rtph264depay",
        "h264parse",
    }));
    assert(topology.encoded_tee);
    assert(topology.encoded_tee->factory == "tee");
    assert(topology.evidence_cache.has_value());
    assert(factories(topology.decode_path) == std::vector<std::string>({
        "varenderD129h264dec",
        "capsfilter",
        "clocksync",
        "tee",
    }));
    assert(topology.depay_wait_for_keyframe);
    assert(topology.depay_request_keyframe);
    assert(topology.decode_caps
        == "video/x-raw(memory:VAMemory),format=NV12");
    assert(topology.display.has_value());
    assert(topology.display->queue_capacity == 1);
    assert(topology.display->leaky_downstream);
    assert(topology.display->drop_only);
    assert(topology.display->max_rate == 24);
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "varenderD129postproc",
            "capsfilter",
            "glupload",
            "glcolorconvert",
            "capsfilter",
            "gtkglsink",
        }));
    assert(topology.display_upload_caps
        == "video/x-raw(memory:DMABuf),format=NV12");
    assert(topology.display_sink_caps
        == "video/x-raw(memory:GLMemory),format=RGBA");
    assert(std::ranges::none_of(
        topology.display->stages,
        [](const auto &stage) { return stage.factory == "ssvoverlay"; }));
    assert(topology.analysis.has_value());
    assert(topology.analysis->queue_capacity == 1);
    assert(topology.analysis->leaky_downstream);
    assert(topology.analysis->drop_only);
    assert(topology.analysis->max_rate == 12);
    assert(factories(topology.analysis->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "varenderD129postproc",
            "capsfilter",
            "ssvinfer",
            "ssvtrack",
            "ssvpub",
            "fakesink",
        }));
    assert(std::ranges::none_of(
        topology.source_path,
        [](const auto &stage) { return stage.factory == "videoconvert"; }));
    assert(topology.analysis_host_caps
        == "video/x-raw,format=RGBA,width=640,height=640,pixel-aspect-ratio=1/1");
    const auto &decode_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::DecodeTee);
    const auto &display_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::DisplayUpload);
    const auto &display_sink_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::DisplaySink);
    const auto &analysis_gpu_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::AnalysisGpuInput);
    const auto &analysis_host_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::AnalysisHost);
    assert(decode_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {ssv::SsvMemoryKind::VaMemory});
    assert(display_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {ssv::SsvMemoryKind::DmaBuf});
    assert(display_sink_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {ssv::SsvMemoryKind::GlMemory});
    assert(analysis_gpu_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {ssv::SsvMemoryKind::VaMemory});
    assert(analysis_host_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::SystemMemory});
}

void test_va_hardware_pair_keeps_frames_in_va_memory_until_encode()
{
    auto config = make_config();
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";
    config.inference.enabled = false;
    config.tracking.enabled = false;
    auto registry = make_registry();
    registry.gstreamer_elements.push_back("varenderD129h264enc");

    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, std::nullopt);

    assert(plan.codec.path == ssv::SsvCodecPath::HardwarePair);
    assert(topology.display_download_caps.empty());
    assert(topology.display_encode_caps
        == "video/x-raw(memory:VAMemory),format=NV12");
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "capsfilter",
            "varenderD129h264enc",
            "h264parse",
            "rtph264pay",
            "rtspclientsink",
        }));
    const auto &encode_contract = contract_at(
        topology, ssv::SsvPipelineBoundary::DisplayEncodeInput);
    assert(encode_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::VaMemory});
}

void test_nvdec_downloads_cuda_frames_for_cpu_consumers()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Nvdec;
    config.sources.front().decode.device = {
        ssv::SsvDecodeDeviceKind::Cuda,
        "2",
    };
    config.display.backend = ssv::SsvDisplayBackend::GtkSink;
    auto registry = make_registry();
    registry.gstreamer_elements.push_back("nvh264device2dec");

    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};
    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, model);

    assert(plan.codec.path == ssv::SsvCodecPath::DecodeOnly);
    assert(topology.decode_caps
        == "video/x-raw(memory:CUDAMemory),format=NV12");
    assert(topology.display_download_caps == "video/x-raw,format=NV12");
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "cudadownload",
            "capsfilter",
            "videoconvert",
            "capsfilter",
            "gtksink",
        }));
    assert(factories(topology.analysis->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "cudadownload",
            "videoscale",
            "videoconvert",
            "capsfilter",
            "ssvinfer",
            "ssvtrack",
            "ssvpub",
            "fakesink",
        }));
    const auto &decode_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::DecodeTee);
    const auto &analysis_gpu_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::AnalysisGpuInput);
    const auto &analysis_host_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::AnalysisHost);
    assert(decode_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::CudaMemory});
    assert(analysis_gpu_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::CudaMemory});
    assert(analysis_host_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::SystemMemory});
}

void test_evidence_cache_topology_is_mandatory()
{
    auto config = make_config();
    const auto registry = make_registry();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};

    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, model);
    assert(factories(topology.source_path) == std::vector<std::string>({
        "rtspsrc",
        "capsfilter",
        "rtph264depay",
        "h264parse",
    }));
    assert(topology.encoded_tee);
    assert(topology.encoded_tee->factory == "tee");
    assert(topology.encoded_tee->name == "encoded-tee");
    assert(factories(topology.decode_path) == std::vector<std::string>({
        "varenderD129h264dec",
        "capsfilter",
        "clocksync",
        "tee",
    }));
    assert(topology.evidence_cache.has_value());
    assert(topology.evidence_cache->queue_capacity == 32);
    assert(topology.evidence_cache->leaky_downstream);
    assert(!topology.evidence_cache->drop_only);
    assert(factories(topology.evidence_cache->stages)
        == std::vector<std::string>({"queue", "h264timestamper", "splitmuxsink"}));
    assert(topology.required_factories
        == std::vector<std::string>({"mp4mux"}));
}

void test_rtsp_topology_uses_encoded_gstreamer_output()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";

    const auto registry = make_registry();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};
    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, model);

    assert(plan.display_backend
        == ssv::SsvResolvedDisplayBackend::RtspClientSink);
    assert(!ssv::ssv_display_backend_requires_window(
        *plan.display_backend));
    assert(topology.display_sink_caps.empty());
    assert(topology.display_encode_caps == "video/x-raw,format=I420");
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "videoconvert",
            "capsfilter",
            "openh264enc",
            "h264parse",
            "rtph264pay",
            "rtspclientsink",
        }));
    const auto &encode_contract = contract_at(
        topology, ssv::SsvPipelineBoundary::DisplayEncodeInput);
    assert(encode_contract.format == ssv::SsvPixelFormat::I420);
    assert(encode_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::SystemMemory});
}

void test_rtsp_overlay_topology_inserts_burn_in_stage()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";
    config.display.rtsp.burn_in_overlay = true;
    config.display.overlay.enabled = false;

    auto registry = make_registry();
    registry.gstreamer_elements.push_back("ssvoverlay");
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};
    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, model);

    assert(topology.display_overlay_caps == "video/x-raw,format=BGRx");
    assert(topology.display_encode_caps == "video/x-raw,format=I420");
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "videoconvert",
            "capsfilter",
            "ssvoverlay",
            "videoconvert",
            "capsfilter",
            "openh264enc",
            "h264parse",
            "rtph264pay",
            "rtspclientsink",
        }));
    const auto &overlay_contract = contract_at(
        topology, ssv::SsvPipelineBoundary::DisplayOverlayInput);
    assert(overlay_contract.format == ssv::SsvPixelFormat::Bgrx);
    assert(overlay_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::SystemMemory});
}

void test_rtsp_encoded_passthrough_topology_uses_encoded_tee()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";
    config.display.rtsp.encoded_passthrough = true;
    config.inference.enabled = false;
    config.tracking.enabled = false;

    auto registry = make_registry();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, std::nullopt);

    assert(topology.encoded_tee.has_value());
    assert(topology.display_from_encoded);
    assert(topology.display.has_value());
    assert(topology.decode_path.empty());
    assert(!topology.decoded_discard.has_value());
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "rtph264pay",
            "rtspclientsink",
        }));
}

void test_rtsp_encoded_passthrough_coexists_with_analysis_and_evidence()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";
    config.display.rtsp.encoded_passthrough = true;
    config.inference.enabled = true;
    config.tracking.enabled = true;

    const auto registry = make_registry();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};
    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, model);

    assert(topology.encoded_tee.has_value());
    assert(topology.display_from_encoded);
    assert(topology.decode_path.size() == 5);
    assert(topology.evidence_cache.has_value());
    assert(topology.analysis.has_value());
    assert(!topology.decoded_discard.has_value());
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "rtph264pay",
            "rtspclientsink",
        }));
    assert(factories(topology.evidence_cache->stages)
        == std::vector<std::string>({"queue", "h264timestamper", "splitmuxsink"}));
    assert(factories(topology.analysis->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "videoscale",
            "videoconvert",
            "capsfilter",
            "ssvinfer",
            "ssvtrack",
            "ssvpub",
            "fakesink",
        }));
}

void test_rtsp_encoded_passthrough_builder_skips_decoder()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";
    config.display.rtsp.encoded_passthrough = true;
    config.inference.enabled = false;
    config.tracking.enabled = false;

    auto registry = make_registry();
    const bool registered_rtsp_sink = register_test_rtsp_sink_alias();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, nullptr);

    auto *decoder = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "h264-decoder");
    auto *encoded_tee = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "encoded-tee");
    auto *pay = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "display-rtp-pay");
    auto *sink = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "display-sink");
    assert(decoder == nullptr);
    assert(encoded_tee != nullptr && pay != nullptr && sink != nullptr);
    if (decoder != nullptr)
        gst_object_unref(decoder);
    gst_object_unref(encoded_tee);
    gst_object_unref(pay);
    gst_object_unref(sink);
    instance.reset();
    unregister_test_rtsp_sink_alias(registered_rtsp_sink);
}

void test_rtsp_encoded_passthrough_builder_connects_analysis_and_evidence()
{
    namespace fs = std::filesystem;

    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";
    config.display.rtsp.encoded_passthrough = true;
    const auto cache_root = fs::temp_directory_path()
        / ("ssv-pipeline-builder-passthrough-cache-"
           + std::to_string(::getpid()));
    std::error_code error;
    fs::remove_all(cache_root, error);
    assert(!error);
    assert(fs::create_directories(cache_root, error));
    assert(!error);
    config.evidence_cache.directory = cache_root.string();

    config.inference.enabled = true;
    config.inference.model.path = "/bin/true";
    config.inference.model.output_format = "yolo_nx6";
    const char *label_map = std::getenv("SSV_TEST_LABEL_MAP_PATH");
    assert(label_map != nullptr && label_map[0] != '\0');
    config.inference.model.label_map = label_map;
    config.inference.model.preprocess = ssv::SsvPreprocessConfig {};
    config.tracking.enabled = true;

    auto registry = make_registry();
    const bool registered_rtsp_sink = register_test_rtsp_sink_alias();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    auto service = ssv::infer::ssv_inference_test_service_create(
        config.inference);
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, service.get());

    for (const char *name : {
             "encoded-tee",
             "h264-decoder",
             "decoded-tee",
             "evidence-cache-sink",
             "analysis-infer",
             "analysis-track",
             "analysis-publish",
             "display-rtp-pay",
             "display-sink",
         }) {
        auto *element = gst_bin_get_by_name(
            GST_BIN(instance.pipeline()), name);
        assert(element != nullptr);
        gst_object_unref(element);
    }

    instance.reset();
    ssv::infer::ssv_inference_service_stop(service.get());
    unregister_test_rtsp_sink_alias(registered_rtsp_sink);
    fs::remove_all(cache_root, error);
    assert(!error);
}

void test_builder_configures_rtsp_overlay_properties()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.backend = ssv::SsvDisplayBackend::RtspClientSink;
    config.display.rtsp.location = "rtsp://127.0.0.1:8554/ssv";
    config.display.rtsp.burn_in_overlay = true;
    config.display.overlay.enabled = false;
    config.display.overlay.font.face = "bold";
    config.display.overlay.font.size = 18;
    config.display.overlay.motion_prediction.enabled = false;
    config.display.overlay.motion_prediction.max_horizon_ms = 123;
    config.inference.enabled = false;
    config.tracking.enabled = false;

    auto registry = make_registry();
    registry.gstreamer_elements.push_back("ssvoverlay");
    const bool registered_rtsp_sink = register_test_rtsp_sink_alias();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, nullptr);

    auto *overlay = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "display-overlay");
    auto *overlay_caps = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "display-overlay-caps");
    auto *encode_caps = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "display-encode-caps");
    assert(overlay != nullptr && overlay_caps != nullptr
        && encode_caps != nullptr);

    gboolean enabled = FALSE;
    gboolean motion_prediction = TRUE;
    guint max_horizon_ms = 0;
    guint font_size = 0;
    gchar *source_id = nullptr;
    gchar *font_face = nullptr;
    gpointer source_context = nullptr;
    g_object_get(
        overlay,
        "enabled", &enabled,
        "source-id", &source_id,
        "source-context", &source_context,
        "motion-prediction", &motion_prediction,
        "max-horizon-ms", &max_horizon_ms,
        "font-face", &font_face,
        "font-size", &font_size,
        nullptr);
    assert(enabled);
    assert(std::string(source_id) == "camera-01");
    assert(source_context == instance.source_context().get());
    assert(!motion_prediction);
    assert(max_horizon_ms == 123);
    assert(std::string(font_face) == "bold");
    assert(font_size == 18);

    GstCaps *caps = nullptr;
    g_object_get(overlay_caps, "caps", &caps, nullptr);
    assert(caps != nullptr);
    gchar *caps_text = gst_caps_to_string(caps);
    assert(std::string(caps_text).find("format=(string)BGRx")
        != std::string::npos);
    g_free(caps_text);
    gst_caps_unref(caps);
    caps = nullptr;
    g_object_get(encode_caps, "caps", &caps, nullptr);
    assert(caps != nullptr);
    caps_text = gst_caps_to_string(caps);
    assert(std::string(caps_text).find("format=(string)I420")
        != std::string::npos);
    g_free(caps_text);
    gst_caps_unref(caps);

    g_free(source_id);
    g_free(font_face);
    gst_object_unref(overlay);
    gst_object_unref(overlay_caps);
    gst_object_unref(encode_caps);
    instance.reset();
    unregister_test_rtsp_sink_alias(registered_rtsp_sink);
}

void test_builder_configures_evidence_cache_queue()
{
    namespace fs = std::filesystem;

    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.enabled = false;
    config.inference.enabled = false;
    config.tracking.enabled = false;
    const auto cache_root = fs::temp_directory_path()
        / ("ssv-pipeline-builder-cache-" + std::to_string(::getpid()));
    std::error_code error;
    fs::remove_all(cache_root, error);
    assert(!error);
    assert(fs::create_directories(cache_root, error));
    assert(!error);
    config.evidence_cache.directory = cache_root.string();

    auto registry = make_registry();
    registry.gstreamer_elements = {
        "rtspsrc",
        "capsfilter",
        "rtph264depay",
        "h264parse",
        "avdec_h264",
        "videoconvert",
        "clocksync",
        "tee",
        "queue",
        "splitmuxsink",
        "h264timestamper",
        "mp4mux",
        "fakesink",
    };
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, nullptr);
    GstElement *queue = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "evidence-cache-queue");
    assert(queue != nullptr);
    gint max_size_buffers = 0;
    gint leaky = 0;
    g_object_get(
        queue,
        "max-size-buffers", &max_size_buffers,
        "leaky", &leaky,
        nullptr);
    assert(max_size_buffers == 32);
    assert(leaky == 2);
    gst_object_unref(queue);
    instance.reset();

    fs::remove_all(cache_root, error);
    assert(!error);
}

void test_plan_snapshot_reports_the_exact_missing_element()
{
    const auto config = make_config();
    auto registry = make_registry();
    std::erase(registry.gstreamer_elements, "clocksync");
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};
    try {
        static_cast<void>(ssv::pipeline_internal::resolve_topology(
            config,
            plan,
            model));
        assert(false && "missing clocksync was accepted");
    } catch (const ssv::SsvPipelineBuilderError &error) {
        assert(error.exit_code() == ssv::SsvExitCode::CapabilityUnavailable);
        assert(error.stage() == "capability.pipeline");
        assert(std::string(error.what()).find("clocksync")
            != std::string::npos);
    }
}

void test_gtksink_compatibility_does_not_require_dmabuf_export()
{
    auto config = make_config();
    config.display.backend = ssv::SsvDisplayBackend::GtkSink;
    const auto registry = make_registry();
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};

    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, model);

    assert(plan.display_backend == ssv::SsvResolvedDisplayBackend::GtkSink);
    assert(topology.display.has_value());
    assert(factories(topology.display->stages)
        == std::vector<std::string>({
            "queue",
            "videorate",
            "varenderD129postproc",
            "capsfilter",
            "videoconvert",
            "capsfilter",
            "gtksink",
        }));
    assert(topology.display_upload_caps.empty());
    assert(topology.display_download_caps
        == "video/x-raw,format=RGBA");
    assert(topology.display_sink_caps
        == "video/x-raw,format=BGRx");
    assert(std::ranges::none_of(
        topology.contracts,
        [](const auto &contract) {
            return contract.boundary
                == ssv::SsvPipelineBoundary::DisplayUpload;
        }));
    const auto &sink_contract =
        contract_at(topology, ssv::SsvPipelineBoundary::DisplaySink);
    assert(sink_contract.allowed_memories
        == std::vector<ssv::SsvMemoryKind> {
            ssv::SsvMemoryKind::SystemMemory});
}

void test_display_branch_build_failure_is_classified_for_auto_fallback()
{
    constexpr auto decoder_factory = "vassvtesth264dec";
    constexpr auto postproc_factory = "vassvtestpostproc";
    auto config = make_config();
    config.sources.front().decode.device.value = "/dev/dri/ssvtest";
    config.inference.enabled = false;
    config.tracking.enabled = false;
    auto registry = make_registry();
    registry.gstreamer_elements.push_back(decoder_factory);
    registry.gstreamer_elements.push_back(postproc_factory);
    assert(gst_element_register(
        nullptr, decoder_factory, GST_RANK_NONE, GST_TYPE_BIN));
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    assert(plan.display_fallback_allowed);
    assert(plan.codec.decode.decoder_factory == decoder_factory);
    assert(plan.codec.decode.va_postproc_factory == postproc_factory);

    try {
        static_cast<void>(ssv::SsvPipelineBuilder::build(
            config, plan, nullptr));
        assert(false && "display build failure lost its recovery stage");
    } catch (const ssv::SsvPipelineBuilderError &error) {
        assert(error.exit_code() == ssv::SsvExitCode::CapabilityUnavailable);
        assert(error.stage() == "display.start");
        assert(std::string(error.what()).find(postproc_factory)
            != std::string::npos);
    }
    auto *registered_feature = gst_registry_lookup_feature(
        gst_registry_get(), decoder_factory);
    assert(registered_feature != nullptr);
    gst_registry_remove_feature(gst_registry_get(), registered_feature);
    gst_object_unref(registered_feature);
}

void test_builder_realizes_the_internal_topology_and_element_properties()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.enabled = false;
    config.inference.enabled = false;
    config.tracking.enabled = false;
    auto registry = make_registry();
    registry.gstreamer_elements = {
        "rtspsrc",
        "capsfilter",
        "rtph264depay",
        "h264parse",
        "avdec_h264",
        "videoconvert",
        "clocksync",
        "tee",
        "queue",
        "fakesink",
        "splitmuxsink",
        "h264timestamper",
        "mp4mux",
    };
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, nullptr);
    assert(instance);
    assert(instance.display_attachment() == nullptr);
    auto source_context = instance.source_context();
    assert(source_context != nullptr);
    assert(source_context->source_id() == "camera-01");
    assert(source_context->meta() == ssv_meta("camera-01"));

    GstElement *depay = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "h264-depay");
    GstElement *queue = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "discard-queue");
    GstElement *decode_format = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "decode-format");
    GstElement *decode_caps = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "decode-memory-caps");
    GstElement *tee = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "decoded-tee");
    GstElement *sink = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "discard-sink");
    assert(depay != nullptr && queue != nullptr
        && decode_format != nullptr && decode_caps != nullptr
        && tee != nullptr && sink != nullptr);
    gboolean wait_for_keyframe = FALSE;
    gboolean request_keyframe = FALSE;
    gint max_size_buffers = 0;
    gint leaky = 0;
    gboolean sink_sync = TRUE;
    gboolean sink_async = TRUE;
    g_object_get(
        depay,
        "wait-for-keyframe", &wait_for_keyframe,
        "request-keyframe", &request_keyframe,
        nullptr);
    g_object_get(
        queue,
        "max-size-buffers", &max_size_buffers,
        "leaky", &leaky,
        nullptr);
    g_object_get(
        sink,
        "sync", &sink_sync,
        "async", &sink_async,
        nullptr);
    GstCaps *caps = nullptr;
    g_object_get(decode_caps, "caps", &caps, nullptr);
    assert(wait_for_keyframe && request_keyframe);
    assert(max_size_buffers == 1);
    assert(leaky == 2);
    assert(!sink_sync && !sink_async);
    assert(caps != nullptr);
    gchar *caps_text = gst_caps_to_string(caps);
    assert(std::string(caps_text).find("format=(string)NV12")
        != std::string::npos);

    g_free(caps_text);
    gst_caps_unref(caps);
    gst_object_unref(depay);
    gst_object_unref(queue);
    gst_object_unref(decode_format);
    gst_object_unref(decode_caps);
    gst_object_unref(sink);

    bool pipeline_finalized = false;
    g_object_weak_ref(
        G_OBJECT(instance.pipeline()), mark_finalized, &pipeline_finalized);
    GstPad *requested_pad = gst_element_get_static_pad(tee, "src_0");
    assert(requested_pad != nullptr);
    gst_object_unref(requested_pad);
    instance.reset();
    assert(pipeline_finalized);
    requested_pad = gst_element_get_static_pad(tee, "src_0");
    assert(requested_pad == nullptr);
    gst_object_unref(tee);
}

void test_builder_passes_one_source_context_to_analysis_plugins()
{
    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Software;
    config.sources.front().decode.device = {};
    config.display.enabled = false;
    config.inference.enabled = true;
    config.inference.model.path = "/bin/true";
    config.inference.model.output_format = "yolo_nx6";
    const char *label_map = std::getenv("SSV_TEST_LABEL_MAP_PATH");
    assert(label_map != nullptr && label_map[0] != '\0');
    config.inference.model.label_map = label_map;
    config.inference.model.preprocess = ssv::SsvPreprocessConfig {};
    config.tracking.enabled = true;
    config.tracking.publish_cooldown_ms = 12345;

    auto registry = make_registry();
    for (const char *factory : {
             "avdec_h264",
             "videorate",
             "videoscale",
             "videoconvert",
         }) {
        registry.gstreamer_elements.push_back(factory);
    }
    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    auto service_config = config.inference;
    auto service = ssv::infer::ssv_inference_test_service_create(
        service_config);
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, service.get());

    auto source_context = instance.source_context();
    assert(source_context != nullptr);
    GstElement *infer = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "analysis-infer");
    GstElement *track = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "analysis-track");
    GstElement *publish = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "analysis-publish");
    GstElement *analysis_scale = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "analysis-scale");
    assert(infer != nullptr && track != nullptr && publish != nullptr);
    assert(analysis_scale != nullptr);

    gpointer infer_context = nullptr;
    gpointer track_context = nullptr;
    gpointer publish_context = nullptr;
    g_object_get(infer, "source-context", &infer_context, nullptr);
    g_object_get(track, "source-context", &track_context, nullptr);
    g_object_get(publish, "source-context", &publish_context, nullptr);
    assert(infer_context == source_context.get());
    assert(track_context == source_context.get());
    assert(publish_context == source_context.get());

    gint publish_cooldown_ms = 0;
    g_object_get(publish, "publish-cooldown-ms", &publish_cooldown_ms, nullptr);
    assert(publish_cooldown_ms == 12345);

    gchar *event_type = nullptr;
    gchar *severity = nullptr;
    gchar *rule_id = nullptr;
    gchar *rule_version = nullptr;
    gchar *rule_facts_json = nullptr;
    g_object_get(
        publish,
        "event-type", &event_type,
        "severity", &severity,
        "rule-id", &rule_id,
        "rule-version", &rule_version,
        "rule-facts-json", &rule_facts_json,
        nullptr);
    assert(std::string(event_type) == "person_without_helmet");
    assert(std::string(severity) == "high");
    assert(std::string(rule_id) == "rule-1");
    assert(std::string(rule_version) == "v1");
    assert(std::string(rule_facts_json) == R"({"helmet_required":true})");
    g_free(event_type);
    g_free(severity);
    g_free(rule_id);
    g_free(rule_version);
    g_free(rule_facts_json);

    gboolean add_borders = FALSE;
    g_object_get(analysis_scale, "add-borders", &add_borders, nullptr);
    assert(add_borders);

    gst_object_unref(infer);
    gst_object_unref(track);
    gst_object_unref(publish);
    gst_object_unref(analysis_scale);
    instance.reset();
    ssv::infer::ssv_inference_service_stop(service.get());

    config.inference.model.preprocess->resize_mode =
        ssv::SsvResizeMode::Stretch;
    service_config = config.inference;
    const auto stretch_plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    auto stretch_service = ssv::infer::ssv_inference_test_service_create(
        service_config);
    auto stretch_instance = ssv::SsvPipelineBuilder::build(
        config, stretch_plan, stretch_service.get());
    analysis_scale = gst_bin_get_by_name(
        GST_BIN(stretch_instance.pipeline()), "analysis-scale");
    assert(analysis_scale != nullptr);
    add_borders = TRUE;
    g_object_get(analysis_scale, "add-borders", &add_borders, nullptr);
    assert(!add_borders);
    gst_object_unref(analysis_scale);
    stretch_instance.reset();
    ssv::infer::ssv_inference_service_stop(stretch_service.get());
}

void test_pipeline_instance_releases_attachment_before_pipeline()
{
    std::vector<std::string> events;
    FinalizationRecord timing_record {&events, "timing-pad"};
    FinalizationRecord sink_record {&events, "sink"};
    FinalizationRecord pipeline_record {&events, "pipeline"};
    GstElement *pipeline = gst_pipeline_new("instance-ownership-test");
    GstElement *sink = gst_element_factory_make("fakesink", nullptr);
    GstElement *timing_element = gst_element_factory_make(
        "identity", "display-timing");
    assert(pipeline != nullptr && sink != nullptr
        && timing_element != nullptr);
    assert(gst_bin_add(GST_BIN(pipeline), timing_element));
    assert(gst_bin_add(GST_BIN(pipeline), sink));
    GstPad *timing_pad = gst_element_get_static_pad(
        timing_element, "src");
    assert(timing_pad != nullptr);
    g_object_weak_ref(
        G_OBJECT(timing_pad), record_finalized, &timing_record);
    g_object_weak_ref(G_OBJECT(sink), record_finalized, &sink_record);
    g_object_weak_ref(
        G_OBJECT(pipeline), record_finalized, &pipeline_record);

    {
        ssv::SsvDisplayAttachment attachment(sink, timing_pad);
        gst_object_unref(timing_pad);
        ssv::SsvPipelineInstance instance(
            ssv::SsvPipelinePtr(pipeline), std::move(attachment));
        assert(instance.pipeline() == pipeline);
        assert(instance.display_attachment() != nullptr);
        assert(instance.display_attachment()->sink() == sink);
        assert(instance.display_attachment()->timing_pad() == timing_pad);
    }

    assert(events.size() == 3);
    assert(events.back() == "pipeline");
    assert(std::ranges::count(events, "timing-pad") == 1);
    assert(std::ranges::count(events, "sink") == 1);
}

void test_pipeline_instance_failure_releases_transferred_attachment()
{
    std::vector<std::string> events;
    FinalizationRecord timing_record {&events, "timing-pad"};
    FinalizationRecord sink_record {&events, "sink"};
    GstElement *sink = gst_element_factory_make("fakesink", nullptr);
    GstPad *timing_pad = gst_pad_new("timing", GST_PAD_SRC);
    assert(sink != nullptr && timing_pad != nullptr);
    g_object_weak_ref(
        G_OBJECT(timing_pad), record_finalized, &timing_record);
    g_object_weak_ref(G_OBJECT(sink), record_finalized, &sink_record);

    try {
        ssv::SsvDisplayAttachment attachment(sink, timing_pad);
        gst_object_unref(timing_pad);
        gst_object_unref(sink);
        ssv::SsvPipelineInstance instance(
            ssv::SsvPipelinePtr {}, std::move(attachment));
        assert(false && "pipeline instance accepted a null pipeline");
    } catch (const std::invalid_argument &error) {
        assert(std::string(error.what()).find("GStreamer pipeline")
            != std::string::npos);
    }

    assert(events == std::vector<std::string>({
        "timing-pad",
        "sink",
    }));
}

void test_pipeline_instance_rejects_attachment_outside_pipeline()
{
    std::vector<std::string> events;
    FinalizationRecord timing_record {&events, "timing-pad"};
    FinalizationRecord sink_record {&events, "sink"};
    FinalizationRecord pipeline_record {&events, "pipeline"};
    GstElement *pipeline = gst_pipeline_new("association-test");
    GstElement *sink = gst_element_factory_make("fakesink", nullptr);
    GstPad *timing_pad = gst_pad_new("timing", GST_PAD_SRC);
    assert(pipeline != nullptr && sink != nullptr && timing_pad != nullptr);
    g_object_weak_ref(
        G_OBJECT(timing_pad), record_finalized, &timing_record);
    g_object_weak_ref(G_OBJECT(sink), record_finalized, &sink_record);
    g_object_weak_ref(
        G_OBJECT(pipeline), record_finalized, &pipeline_record);

    try {
        ssv::SsvDisplayAttachment attachment(sink, timing_pad);
        gst_object_unref(timing_pad);
        gst_object_unref(sink);
        ssv::SsvPipelineInstance instance(
            ssv::SsvPipelinePtr(pipeline), std::move(attachment));
        assert(false && "pipeline instance accepted an unrelated attachment");
    } catch (const std::invalid_argument &error) {
        assert(std::string(error.what()).find("belong to its pipeline")
            != std::string::npos);
    }

    assert(events == std::vector<std::string>({
        "timing-pad",
        "sink",
        "pipeline",
    }));
}

void test_pipeline_instance_classifies_message_origins()
{
    GstElement *pipeline = gst_pipeline_new("message-origin-test");
    GstElement *display_element = gst_element_factory_make(
        "identity", "branch-transform");
    GstElement *timing_element = gst_element_factory_make(
        "identity", "timing-caps");
    GstElement *sink = gst_element_factory_make(
        "fakesink", "opaque-sink");
    GstElement *other = gst_element_factory_make(
        "identity", "analysis-transform");
    assert(pipeline != nullptr && display_element != nullptr
        && timing_element != nullptr && sink != nullptr && other != nullptr);
    assert(gst_bin_add(GST_BIN(pipeline), display_element));
    assert(gst_bin_add(GST_BIN(pipeline), timing_element));
    assert(gst_bin_add(GST_BIN(pipeline), sink));
    assert(gst_bin_add(GST_BIN(pipeline), other));
    GstPad *timing_pad = gst_element_get_static_pad(
        timing_element, "src");
    assert(timing_pad != nullptr);
    ssv::SsvDisplayAttachment attachment(sink, timing_pad);
    gst_object_unref(timing_pad);
    std::vector<GstElement *> display_elements {
        display_element,
        timing_element,
        sink,
    };
    ssv::SsvPipelineInstance instance(
        ssv::SsvPipelinePtr(pipeline),
        std::move(attachment),
        {},
        {},
        std::move(display_elements),
        sink);

    const auto expect_origin = [&instance](
                                   GstObject *source,
                                   ssv::SsvPipelineMessageOrigin expected) {
        GstMessage *message = gst_message_new_application(
            source, gst_structure_new_empty("ssv-test-message"));
        assert(message != nullptr);
        assert(instance.message_origin(message) == expected);
        gst_message_unref(message);
    };
    expect_origin(
        GST_OBJECT(sink), ssv::SsvPipelineMessageOrigin::DisplaySink);
    expect_origin(
        GST_OBJECT(display_element),
        ssv::SsvPipelineMessageOrigin::DisplayBranch);
    expect_origin(
        GST_OBJECT(timing_element),
        ssv::SsvPipelineMessageOrigin::DisplayBranch);
    expect_origin(GST_OBJECT(other), ssv::SsvPipelineMessageOrigin::Other);
    assert(instance.message_origin(nullptr)
        == ssv::SsvPipelineMessageOrigin::Other);

    GstElement *outsider = gst_element_factory_make(
        "identity", "display-outsider");
    assert(outsider != nullptr);
    expect_origin(
        GST_OBJECT(outsider), ssv::SsvPipelineMessageOrigin::Other);
    gst_object_unref(outsider);

    GstElement *rtsp_pipeline = gst_pipeline_new("rtsp-message-origin-test");
    GstElement *rtsp_branch = gst_element_factory_make(
        "identity", "encoder");
    GstElement *rtsp_sink = gst_element_factory_make(
        "fakesink", "sink");
    assert(rtsp_pipeline != nullptr && rtsp_branch != nullptr
        && rtsp_sink != nullptr);
    assert(gst_bin_add(GST_BIN(rtsp_pipeline), rtsp_branch));
    assert(gst_bin_add(GST_BIN(rtsp_pipeline), rtsp_sink));
    std::vector<GstElement *> rtsp_elements {rtsp_branch, rtsp_sink};
    ssv::SsvPipelineInstance rtsp_instance {
        ssv::SsvPipelinePtr(rtsp_pipeline),
        {},
        {},
        {},
        std::move(rtsp_elements),
        rtsp_sink};
    const auto expect_rtsp_origin = [&rtsp_instance](
                                        GstObject *source,
                                        ssv::SsvPipelineMessageOrigin expected) {
        GstMessage *message = gst_message_new_application(
            source, gst_structure_new_empty("ssv-test-message"));
        assert(message != nullptr);
        assert(rtsp_instance.message_origin(message) == expected);
        gst_message_unref(message);
    };
    expect_rtsp_origin(
        GST_OBJECT(rtsp_sink), ssv::SsvPipelineMessageOrigin::DisplaySink);
    expect_rtsp_origin(
        GST_OBJECT(rtsp_branch), ssv::SsvPipelineMessageOrigin::DisplayBranch);
}

void test_nvdec_decoder_disables_qos_and_discards_corrupted_frames()
{
    const ssv::SsvSystemHardwareCapabilitiesProbe probe;
    const auto registry = probe.detect();
    if (!registry.has_gstreamer_element("nvh264dec"))
        return;

    for (const char *factory : {"nvh264dec", "cudadownload"}) {
        auto *element = gst_element_factory_make(factory, nullptr);
        if (element == nullptr)
            return;
        const bool exposes_device_selector =
            g_object_class_find_property(
                G_OBJECT_GET_CLASS(element), "cuda-device-id") != nullptr
            || g_object_class_find_property(
                G_OBJECT_GET_CLASS(element), "device-id") != nullptr
            || g_object_class_find_property(
                G_OBJECT_GET_CLASS(element), "gpu-id") != nullptr;
        gst_object_unref(element);
        if (!exposes_device_selector)
            return;
    }

    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Nvdec;
    config.sources.front().decode.device = {
        ssv::SsvDecodeDeviceKind::Cuda,
        "0",
    };
    config.display.enabled = false;
    config.inference.enabled = false;
    config.tracking.enabled = false;

    const auto plan = ssv::SsvPipelinePlan::resolve(
        config, snapshot_of(registry));
    std::optional<ssv::SsvPipelineInstance> instance;
    try {
        instance.emplace(ssv::SsvPipelineBuilder::build(
            config, plan, nullptr));
    } catch (const ssv::SsvPipelineBuilderError &error) {
        if (std::string(error.what()).find(
                "CUDA element does not expose its device selector")
            != std::string::npos) {
            return;
        }
        throw;
    }
    GstElement *decoder = gst_bin_get_by_name(
        GST_BIN(instance->pipeline()), "h264-decoder");
    assert(decoder != nullptr);

    gboolean qos = TRUE;
    gboolean discard_corrupted_frames = FALSE;
    g_object_get(
        decoder,
        "qos", &qos,
        "discard-corrupted-frames", &discard_corrupted_frames,
        nullptr);
    assert(!qos);
    assert(discard_corrupted_frames);

    gst_object_unref(decoder);
}

void test_system_registry_snapshot_includes_pipeline_elements()
{
    const ssv::SsvSystemHardwareCapabilitiesProbe probe;
    const auto registry = probe.detect();
    for (const char *factory : {
             "rtspsrc",
             "rtph264depay",
             "h264parse",
             "clocksync",
             "tee",
             "queue",
             "videorate",
             "capsfilter",
             "fakesink",
         }) {
        assert(registry.has_gstreamer_element(factory));
    }
}

} // namespace

int main(int argc, char **argv)
{
    const auto default_cache_root = std::filesystem::temp_directory_path()
        / ("ssv-pipeline-builder-default-cache-"
            + std::to_string(::getpid()));
    std::error_code default_cache_error;
    std::filesystem::remove_all(default_cache_root, default_cache_error);

    gst_init(&argc, &argv);
    test_va_topology_freezes_order_backpressure_and_rate_contracts();
    test_va_hardware_pair_keeps_frames_in_va_memory_until_encode();
    test_nvdec_downloads_cuda_frames_for_cpu_consumers();
    test_evidence_cache_topology_is_mandatory();
    test_rtsp_topology_uses_encoded_gstreamer_output();
    test_rtsp_overlay_topology_inserts_burn_in_stage();
    test_rtsp_encoded_passthrough_topology_uses_encoded_tee();
    test_rtsp_encoded_passthrough_coexists_with_analysis_and_evidence();
    test_rtsp_encoded_passthrough_builder_skips_decoder();
    test_rtsp_encoded_passthrough_builder_connects_analysis_and_evidence();
    test_builder_configures_rtsp_overlay_properties();
    test_builder_configures_evidence_cache_queue();
    test_plan_snapshot_reports_the_exact_missing_element();
    test_gtksink_compatibility_does_not_require_dmabuf_export();
    test_display_branch_build_failure_is_classified_for_auto_fallback();
    test_builder_realizes_the_internal_topology_and_element_properties();
    test_builder_passes_one_source_context_to_analysis_plugins();
    test_pipeline_instance_releases_attachment_before_pipeline();
    test_pipeline_instance_failure_releases_transferred_attachment();
    test_pipeline_instance_rejects_attachment_outside_pipeline();
    test_pipeline_instance_classifies_message_origins();
    test_nvdec_decoder_disables_qos_and_discards_corrupted_frames();
    test_system_registry_snapshot_includes_pipeline_elements();
    std::filesystem::remove_all(default_cache_root, default_cache_error);
    return 0;
}
