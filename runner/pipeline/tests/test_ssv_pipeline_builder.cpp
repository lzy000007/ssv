#include "pipeline/ssv_pipeline_builder.hpp"
#include "pipeline/ssv_pipeline_topology.hpp"
#include "ssv_inference_test_service.hpp"

#include <gst/gst.h>

#include <algorithm>
#include <cassert>
#include <cstdlib>
#include <initializer_list>
#include <string>
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
    return {{
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
    }, true, false};
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
    const auto plan = ssv::SsvPipelinePlan::resolve(config, registry);
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};

    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, registry, model);

    assert(factories(topology.prefix) == std::vector<std::string>({
        "rtspsrc",
        "capsfilter",
        "rtph264depay",
        "h264parse",
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
        topology.prefix,
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

void test_fake_registry_reports_the_exact_missing_element()
{
    const auto config = make_config();
    auto registry = make_registry();
    const auto plan = ssv::SsvPipelinePlan::resolve(config, registry);
    std::erase(registry.gstreamer_elements, "clocksync");
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};
    try {
        static_cast<void>(ssv::pipeline_internal::resolve_topology(
            config,
            plan,
            registry,
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
    const auto plan = ssv::SsvPipelinePlan::resolve(config, registry);
    const ssv::infer::SsvModelContract model {
        640, 640, ssv::SsvResizeMode::Letterbox};

    const auto topology = ssv::pipeline_internal::resolve_topology(
        config, plan, registry, model);

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
    const auto plan = ssv::SsvPipelinePlan::resolve(config, registry);
    assert(plan.display_fallback_allowed);
    assert(plan.decode.decoder_factory == decoder_factory);
    assert(plan.decode.va_postproc_factory == postproc_factory);

    try {
        static_cast<void>(ssv::SsvPipelineBuilder::build(
            config, plan, registry, nullptr));
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
    };
    const auto plan = ssv::SsvPipelinePlan::resolve(config, registry);
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, registry, nullptr);
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
    const auto plan = ssv::SsvPipelinePlan::resolve(config, registry);
    auto service_config = config.inference;
    auto service = ssv::infer::ssv_inference_test_service_create(
        service_config);
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, registry, service.get());

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
    const auto stretch_plan = ssv::SsvPipelinePlan::resolve(config, registry);
    auto stretch_service = ssv::infer::ssv_inference_test_service_create(
        service_config);
    auto stretch_instance = ssv::SsvPipelineBuilder::build(
        config, stretch_plan, registry, stretch_service.get());
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
        "identity", "display-transform");
    GstElement *timing_element = gst_element_factory_make(
        "identity", "display-timing");
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
    ssv::SsvPipelineInstance instance(
        ssv::SsvPipelinePtr(pipeline), std::move(attachment));

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
}

void test_nvdec_decoder_disables_qos_and_discards_corrupted_frames()
{
    const ssv::SsvSystemHardwareCapabilitiesProbe probe;
    const auto registry = probe.detect();
    if (!registry.has_gstreamer_element("nvh264dec"))
        return;

    auto config = make_config();
    config.sources.front().decode.mode = ssv::SsvDecodeMode::Nvdec;
    config.sources.front().decode.device = {
        ssv::SsvDecodeDeviceKind::Cuda,
        "0",
    };
    config.display.enabled = false;
    config.inference.enabled = false;
    config.tracking.enabled = false;

    const auto plan = ssv::SsvPipelinePlan::resolve(config, registry);
    auto instance = ssv::SsvPipelineBuilder::build(
        config, plan, registry, nullptr);
    GstElement *decoder = gst_bin_get_by_name(
        GST_BIN(instance.pipeline()), "h264-decoder");
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
    gst_init(&argc, &argv);
    test_va_topology_freezes_order_backpressure_and_rate_contracts();
    test_fake_registry_reports_the_exact_missing_element();
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
    return 0;
}
