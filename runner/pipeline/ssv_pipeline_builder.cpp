#include "ssv_pipeline_builder.hpp"
#include "ssv_pipeline_contract.hpp"
#include "ssv_pipeline_contract_internal.hpp"
#include "ssv_pipeline_topology.hpp"

#include <gst/gst.h>

#include <algorithm>
#include <memory>
#include <sstream>
#include <unordered_map>
#include <utility>

namespace ssv {
namespace {

using pipeline_internal::PipelineBranchTopology;
using pipeline_internal::PipelineStage;
using pipeline_internal::PipelineTopology;

PipelineStage stage(std::string factory, std::string name)
{
    return {std::move(factory), std::move(name)};
}

SsvPipelineContractExpectation contract_from_caps(
    SsvPipelineBoundary boundary,
    const SsvVideoCaps &caps,
    int width = 0,
    int height = 0)
{
    return {boundary, caps.format, {caps.memory}, width, height};
}

const SsvVideoCaps &require_expected_caps(
    const std::optional<SsvVideoCaps> &caps,
    std::string_view boundary)
{
    if (!caps) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "resolved pipeline plan is missing " + std::string(boundary)
                + " caps");
    }
    return *caps;
}

std::string model_caps(const infer::SsvModelContract &contract)
{
    std::ostringstream caps;
    caps << "video/x-raw,format=RGBA,width=" << contract.width
         << ",height=" << contract.height
         << ",pixel-aspect-ratio=1/1";
    return caps.str();
}

void require_registered(
    const SsvHardwareCapabilities &registry,
    const std::vector<PipelineStage> &stages)
{
    for (const auto &item : stages) {
        if (registry.has_gstreamer_element(item.factory))
            continue;
        throw SsvPipelineBuilderError(
            SsvExitCode::CapabilityUnavailable,
            "capability.pipeline",
            "required GStreamer element is unavailable: " + item.factory);
    }
}

using ElementMap = std::unordered_map<std::string, GstElement *>;

GstElement *add_element(
    GstElement *pipeline,
    const PipelineStage &stage,
    ElementMap &elements)
{
    SsvPipelinePtr element(
        gst_element_factory_make(stage.factory.c_str(), stage.name.c_str()));
    if (!element) {
        throw SsvPipelineBuilderError(
            SsvExitCode::CapabilityUnavailable,
            "capability.pipeline",
            "failed to create GStreamer element: " + stage.factory);
    }
    if (!gst_bin_add(GST_BIN(pipeline), element.get())) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "failed to add GStreamer element: " + stage.name);
    }
    auto *result = element.release();
    elements.emplace(stage.name, result);
    return result;
}

GstElement *required_element(
    const ElementMap &elements,
    std::string_view name)
{
    const auto found = elements.find(std::string(name));
    if (found == elements.end()) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "pipeline topology is missing element: " + std::string(name));
    }
    return found->second;
}

void set_caps(GstElement *filter, const std::string &description)
{
    std::unique_ptr<GstCaps, decltype(&gst_caps_unref)> caps(
        gst_caps_from_string(description.c_str()), gst_caps_unref);
    if (!caps) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "invalid pipeline caps: " + description);
    }
    g_object_set(filter, "caps", caps.get(), nullptr);
}

void configure_resize_borders(
    GstElement *element,
    bool add_borders)
{
    const auto *property = g_object_class_find_property(
        G_OBJECT_GET_CLASS(element), "add-borders");
    if (property == nullptr
        || (property->flags & G_PARAM_WRITABLE) == 0) {
        throw SsvPipelineBuilderError(
            SsvExitCode::CapabilityUnavailable,
            "capability.pipeline",
            std::string("resize element does not support writable add-borders: ")
                + GST_ELEMENT_NAME(element));
    }
    g_object_set(element, "add-borders", add_borders, nullptr);
}

void link_elements(GstElement *upstream, GstElement *downstream)
{
    if (!gst_element_link(upstream, downstream)) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            std::string("failed to link ")
                + GST_ELEMENT_NAME(upstream) + " -> "
                + GST_ELEMENT_NAME(downstream));
    }
}

class RequestPadLease {
public:
    RequestPadLease(GstElement *owner, GstPad *pad)
        : owner_(GST_ELEMENT(gst_object_ref(owner)))
        , pad_(pad)
    {
    }

    ~RequestPadLease() { reset(); }

    RequestPadLease(const RequestPadLease &) = delete;
    RequestPadLease &operator=(const RequestPadLease &) = delete;

    RequestPadLease(RequestPadLease &&other) noexcept
        : owner_(std::exchange(other.owner_, nullptr))
        , pad_(std::exchange(other.pad_, nullptr))
    {
    }

    RequestPadLease &operator=(RequestPadLease &&other) noexcept
    {
        if (this == &other)
            return *this;
        reset();
        owner_ = std::exchange(other.owner_, nullptr);
        pad_ = std::exchange(other.pad_, nullptr);
        return *this;
    }

    [[nodiscard]] GstPad *get() const noexcept { return pad_; }

private:
    void reset() noexcept
    {
        if (pad_ != nullptr) {
            gst_element_release_request_pad(owner_, pad_);
            gst_object_unref(pad_);
            pad_ = nullptr;
        }
        if (owner_ != nullptr) {
            gst_object_unref(owner_);
            owner_ = nullptr;
        }
    }

    GstElement *owner_ = nullptr;
    GstPad *pad_ = nullptr;
};

RequestPadLease link_tee_branch(GstElement *tee, GstElement *downstream)
{
    RequestPadLease tee_pad(
        tee, gst_element_request_pad_simple(tee, "src_%u"));
    std::unique_ptr<GstPad, decltype(&gst_object_unref)> sink_pad(
        gst_element_get_static_pad(downstream, "sink"), gst_object_unref);
    if (tee_pad.get() == nullptr || !sink_pad) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "failed to acquire pads for decoded tee branch");
    }

    const auto link_result = gst_pad_link(tee_pad.get(), sink_pad.get());
    if (link_result != GST_PAD_LINK_OK) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            std::string("failed to link decoded tee -> ")
                + GST_ELEMENT_NAME(downstream));
    }
    return tee_pad;
}

void link_stages(
    const std::vector<PipelineStage> &stages,
    const ElementMap &elements,
    std::size_t first = 0)
{
    if (stages.size() <= first + 1)
        return;
    for (std::size_t index = first; index + 1 < stages.size(); ++index) {
        link_elements(
            required_element(elements, stages[index].name),
            required_element(elements, stages[index + 1].name));
    }
}

void configure_queue(
    GstElement *queue,
    const PipelineBranchTopology &branch)
{
    g_object_set(
        queue,
        "max-size-buffers", branch.queue_capacity,
        "max-size-bytes", static_cast<guint>(0),
        "max-size-time", static_cast<guint64>(0),
        nullptr);
    gst_util_set_object_arg(
        G_OBJECT(queue), "leaky",
        branch.leaky_downstream ? "downstream" : "no");
}

void configure_rate(
    GstElement *rate,
    const PipelineBranchTopology &branch)
{
    g_object_set(rate, "drop-only", branch.drop_only, nullptr);
    if (branch.max_rate)
        g_object_set(rate, "max-rate", *branch.max_rate, nullptr);
}

void configure_va_device(
    GstElement *element,
    const SsvDecodePlan &decode)
{
    if (decode.device.kind != SsvDecodeDeviceKind::Drm)
        return;
    const auto *spec = g_object_class_find_property(
        G_OBJECT_GET_CLASS(element), "device-path");
    if (spec == nullptr) {
        const std::string factory =
            GST_OBJECT_NAME(gst_element_get_factory(element));
        const auto basename = decode.device.value.substr(
            decode.device.value.find_last_of('/') + 1);
        if (factory.find(basename) != std::string::npos)
            return;
        throw SsvPipelineBuilderError(
            SsvExitCode::CapabilityUnavailable,
            "capability.decode.device",
            "VA element does not expose its device path");
    }
    if ((spec->flags & G_PARAM_WRITABLE) != 0) {
        g_object_set(element, "device-path", decode.device.value.c_str(), nullptr);
    }
    if ((spec->flags & G_PARAM_READABLE) != 0) {
        gchar *actual = nullptr;
        g_object_get(element, "device-path", &actual, nullptr);
        const bool matches = actual != nullptr && decode.device.value == actual;
        g_free(actual);
        if (!matches) {
            throw SsvPipelineBuilderError(
                SsvExitCode::CapabilityUnavailable,
                "capability.decode.device",
                "VA element resolved a different DRM render node");
        }
    }
}

void configure_decoder_boolean(
    GstElement *decoder,
    const SsvDecodePlan &decode,
    const char *property_name,
    gboolean value)
{
    const auto *property = g_object_class_find_property(
        G_OBJECT_GET_CLASS(decoder), property_name);
    const bool writable_boolean = property != nullptr
        && property->value_type == G_TYPE_BOOLEAN
        && (property->flags & G_PARAM_WRITABLE) != 0;
    if (!writable_boolean) {
        if (decode.backend == SsvDecodeBackend::Nvdec) {
            throw SsvPipelineBuilderError(
                SsvExitCode::CapabilityUnavailable,
                "capability.pipeline",
                std::string("NVDEC decoder does not expose writable ")
                    + property_name);
        }
        return;
    }
    g_object_set(decoder, property_name, value, nullptr);
}

void configure_decoder(
    GstElement *decoder,
    const SsvDecodePlan &decode)
{
    configure_va_device(decoder, decode);
    configure_decoder_boolean(decoder, decode, "qos", FALSE);
    configure_decoder_boolean(
        decoder, decode, "discard-corrupted-frames", TRUE);
}

struct DynamicRtspLink {
    GstElement *target;
};

void destroy_dynamic_rtsp_link(gpointer data, GClosure *)
{
    auto *link = static_cast<DynamicRtspLink *>(data);
    gst_object_unref(link->target);
    delete link;
}

void post_dynamic_link_error(DynamicRtspLink *link, const char *message)
{
    GError *error = g_error_new_literal(
        ssv_pipeline_contract_error_quark(), 1, message);
    GstMessage *bus_message = gst_message_new_error(
        GST_OBJECT(link->target), error, "RTSP dynamic pad link failed");
    g_error_free(error);
    gst_element_post_message(link->target, bus_message);
}

void on_rtsp_pad_added(GstElement *, GstPad *source_pad, gpointer data)
{
    auto *link = static_cast<DynamicRtspLink *>(data);
    std::unique_ptr<GstCaps, decltype(&gst_caps_unref)> caps(
        gst_pad_get_current_caps(source_pad), gst_caps_unref);
    if (!caps)
        caps.reset(gst_pad_query_caps(source_pad, nullptr));
    if (!caps || gst_caps_is_empty(caps.get()))
        return;
    const auto *structure = gst_caps_get_structure(caps.get(), 0);
    const auto *encoding = gst_structure_get_string(
        structure, "encoding-name");
    if (!gst_structure_has_name(structure, "application/x-rtp")
        || g_strcmp0(encoding, "H264") != 0) {
        return;
    }

    std::unique_ptr<GstPad, decltype(&gst_object_unref)> sink_pad(
        gst_element_get_static_pad(link->target, "sink"), gst_object_unref);
    if (!sink_pad || gst_pad_is_linked(sink_pad.get()))
        return;
    if (gst_pad_link(source_pad, sink_pad.get()) != GST_PAD_LINK_OK)
        post_dynamic_link_error(link, "failed to link the H.264 RTP pad");
}

const SsvPipelineContractExpectation &contract_at(
    const PipelineTopology &topology,
    SsvPipelineBoundary boundary)
{
    const auto found = std::ranges::find_if(
        topology.contracts,
        [&](const auto &contract) { return contract.boundary == boundary; });
    if (found == topology.contracts.end()) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "pipeline topology is missing a boundary contract");
    }
    return *found;
}

[[noreturn]] void rethrow_display_start(
    const SsvPipelineBuilderError &error)
{
    throw SsvPipelineBuilderError(
        error.exit_code(), "display.start", error.what());
}

void watch_boundary(
    GstElement *element,
    const SsvPipelineContractExpectation &expectation,
    std::function<void(int, int)> on_geometry = {})
{
    std::unique_ptr<GstPad, decltype(&gst_object_unref)> pad(
        gst_element_get_static_pad(element, "src"), gst_object_unref);
    if (!pad) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            std::string("contract element has no src pad: ")
                + GST_ELEMENT_NAME(element));
    }
    try {
        pipeline_internal::watch_contract(
            element, pad.get(), expectation, std::move(on_geometry));
    } catch (const std::exception &error) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            std::string("failed to install contract probe at ")
                + GST_ELEMENT_NAME(element) + ": " + error.what());
    }
}

PipelineBranchTopology resolve_display_topology(
    const SsvConfig &config,
    const SsvPipelinePlan &plan)
{
    PipelineBranchTopology branch;
    branch.max_rate = config.display.fps;
    branch.stages = {
        stage("queue", "display-queue"),
        stage("videorate", "display-rate"),
    };
    if (plan.decode.backend == SsvDecodeBackend::Vaapi
        && plan.display_backend
            == SsvResolvedDisplayBackend::GtkGlSink) {
        branch.stages.push_back(stage(
            plan.decode.va_postproc_factory, "display-va-export"));
    }
    if (plan.display_backend == SsvResolvedDisplayBackend::GtkGlSink) {
        branch.stages.push_back(stage("capsfilter", "display-upload-caps"));
        branch.stages.push_back(stage("glupload", "display-gl-upload"));
        branch.stages.push_back(stage(
            "glcolorconvert", "display-gl-convert"));
        branch.stages.push_back(stage("capsfilter", "display-sink-caps"));
        branch.stages.push_back(stage("gtkglsink", "display-sink"));
    } else {
        if (plan.decode.backend == SsvDecodeBackend::Vaapi) {
            branch.stages.push_back(stage(
                plan.decode.va_postproc_factory, "display-va-download"));
            branch.stages.push_back(stage(
                "capsfilter", "display-download-caps"));
        }
        branch.stages.push_back(stage("videoconvert", "display-convert"));
        branch.stages.push_back(stage("capsfilter", "display-sink-caps"));
        branch.stages.push_back(stage("gtksink", "display-sink"));
    }
    return branch;
}

PipelineBranchTopology resolve_analysis_topology(
    const SsvConfig &config,
    const SsvPipelinePlan &plan)
{
    PipelineBranchTopology branch;
    if (config.inference.analysis_fps > 0)
        branch.max_rate = config.inference.analysis_fps;
    branch.stages = {
        stage("queue", "analysis-queue"),
        stage("videorate", "analysis-rate"),
    };
    if (plan.decode.backend == SsvDecodeBackend::Vaapi) {
        branch.stages.push_back(stage(
            plan.decode.va_postproc_factory, "analysis-va-postproc"));
    } else {
        branch.stages.push_back(stage("videoscale", "analysis-scale"));
        branch.stages.push_back(stage("videoconvert", "analysis-convert"));
    }
    branch.stages.push_back(stage("capsfilter", "analysis-host-caps"));
    branch.stages.push_back(stage("ssvinfer", "analysis-infer"));
    if (config.tracking.enabled) {
        branch.stages.push_back(stage("ssvtrack", "analysis-track"));
        branch.stages.push_back(stage("ssvpub", "analysis-publish"));
    }
    branch.stages.push_back(stage("fakesink", "analysis-boundary"));
    return branch;
}

} // namespace

SsvPipelineBuilderError::SsvPipelineBuilderError(
    SsvExitCode exit_code,
    std::string stage,
    std::string message)
    : std::runtime_error(std::move(message))
    , exit_code_(exit_code)
    , stage_(std::move(stage))
{
}

SsvExitCode SsvPipelineBuilderError::exit_code() const noexcept
{
    return exit_code_;
}

const std::string &SsvPipelineBuilderError::stage() const noexcept
{
    return stage_;
}

pipeline_internal::PipelineTopology pipeline_internal::resolve_topology(
    const SsvConfig &config,
    const SsvPipelinePlan &plan,
    const SsvHardwareCapabilities &registry,
    std::optional<infer::SsvModelContract> model_contract)
{
    if (config.sources.size() != 1
        || config.sources.front().id != plan.source_id) {
        throw SsvPipelineBuilderError(
            SsvExitCode::InvalidConfiguration,
            "pipeline.build",
            "pipeline config and resolved source must match");
    }
    if (plan.inference_backend && !model_contract) {
        throw SsvPipelineBuilderError(
            SsvExitCode::ModelInitializationFailed,
            "inference.model_contract",
            "inference pipeline requires a validated model contract");
    }

    PipelineTopology topology;
    topology.decode_caps = plan.decode.backend == SsvDecodeBackend::Vaapi
        ? "video/x-raw(memory:VAMemory),format=NV12"
        : "video/x-raw,format=NV12";
    topology.prefix = {
        stage("rtspsrc", "rtsp-source"),
        stage("capsfilter", "rtp-h264-caps"),
        stage("rtph264depay", "h264-depay"),
        stage("h264parse", "h264-parser"),
        stage(plan.decode.decoder_factory, "h264-decoder"),
    };
    if (plan.decode.backend == SsvDecodeBackend::Software) {
        // avdec_h264 commonly negotiates I420, while the shared decode
        // contract requires NV12. Convert before the capsfilter so software
        // decoding does not fail with not-negotiated.
        topology.prefix.push_back(stage("videoconvert", "decode-format"));
    }
    topology.prefix.push_back(stage("capsfilter", "decode-memory-caps"));
    topology.prefix.push_back(stage("clocksync", "decode-clock"));
    topology.prefix.push_back(stage("tee", "decoded-tee"));
    topology.contracts.push_back(contract_from_caps(
        SsvPipelineBoundary::DecodeTee,
        plan.expected_caps.decode_output));

    if (config.display.enabled) {
        topology.display = resolve_display_topology(config, plan);
        const auto &sink_caps = require_expected_caps(
            plan.expected_caps.display_sink_input, "display sink");
        if (plan.display_backend == SsvResolvedDisplayBackend::GtkGlSink) {
            const auto &upload_caps = require_expected_caps(
                plan.expected_caps.display_upload_input, "display upload");
            topology.display_upload_caps =
                "video/x-raw(memory:DMABuf),format=NV12";
            topology.contracts.push_back(contract_from_caps(
                SsvPipelineBoundary::DisplayUpload,
                upload_caps));
            topology.display_sink_caps =
                "video/x-raw(memory:GLMemory),format=RGBA";
        } else {
            if (plan.decode.backend == SsvDecodeBackend::Vaapi) {
                topology.display_download_caps =
                    "video/x-raw,format=RGBA";
            }
            topology.display_sink_caps = "video/x-raw,format=BGRx";
        }
        topology.contracts.push_back(contract_from_caps(
            SsvPipelineBoundary::DisplaySink, sink_caps));
    }
    if (config.inference.enabled) {
        topology.analysis = resolve_analysis_topology(config, plan);
        topology.analysis_host_caps = model_caps(*model_contract);
        if (plan.decode.backend == SsvDecodeBackend::Vaapi) {
            topology.contracts.push_back(contract_from_caps(
                SsvPipelineBoundary::AnalysisGpuInput,
                require_expected_caps(
                    plan.expected_caps.analysis_gpu_input,
                    "analysis GPU input")));
        }
        topology.contracts.push_back(contract_from_caps(
            SsvPipelineBoundary::AnalysisHost,
            require_expected_caps(
                plan.expected_caps.analysis_host_input,
                "analysis host"),
            model_contract->width,
            model_contract->height));
    }
    if (!topology.display && !topology.analysis) {
        topology.display = PipelineBranchTopology {
            .stages = {
                stage("queue", "discard-queue"),
                stage("fakesink", "discard-sink"),
            },
            .max_rate = std::nullopt,
        };
    }

    require_registered(registry, topology.prefix);
    if (topology.display)
        require_registered(registry, topology.display->stages);
    if (topology.analysis)
        require_registered(registry, topology.analysis->stages);
    return topology;
}

SsvPipelineInstance SsvPipelineBuilder::build(
    const SsvConfig &config,
    const SsvPipelinePlan &plan,
    const SsvHardwareCapabilities &registry,
    SsvInferenceService *inference_service)
{
    std::optional<infer::SsvModelContract> model_contract;
    if (plan.inference_backend) {
        if (inference_service == nullptr) {
            throw SsvPipelineBuilderError(
                SsvExitCode::ModelInitializationFailed,
                "inference.start",
                "inference pipeline requires a runner-owned service");
        }
        model_contract = infer::ssv_inference_service_model_contract(
            inference_service);
    }
    auto topology = pipeline_internal::resolve_topology(
        config, plan, registry, model_contract);

    auto request_pad_leases =
        std::make_shared<std::vector<RequestPadLease>>();
    SsvPipelinePtr pipeline(
        gst_pipeline_new("ssv-single-source"),
        SsvGstElementDeleter([request_pad_leases]() noexcept {
            request_pad_leases->clear();
        }));
    if (!pipeline) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "failed to create the GStreamer pipeline");
    }
    ElementMap elements;
    for (const auto &item : topology.prefix)
        add_element(pipeline.get(), item, elements);
    if (topology.display) {
        try {
            for (const auto &item : topology.display->stages)
                add_element(pipeline.get(), item, elements);
        } catch (const SsvPipelineBuilderError &error) {
            if (config.display.enabled)
                rethrow_display_start(error);
            throw;
        }
    }
    if (topology.analysis) {
        for (const auto &item : topology.analysis->stages)
            add_element(pipeline.get(), item, elements);
    }

    const auto &source_config = config.sources.front();
    auto source_context = std::make_shared<SsvSourceContext>(plan.source_id);
    auto *source = required_element(elements, "rtsp-source");
    g_object_set(
        source,
        "location", source_config.uri.c_str(),
        "latency", static_cast<guint>(source_config.latency_ms),
        nullptr);
    gst_util_set_object_arg(
        G_OBJECT(source), "protocols", source_config.protocols.c_str());

    set_caps(
        required_element(elements, "rtp-h264-caps"),
        "application/x-rtp,media=video,encoding-name=H264");
    g_object_set(
        required_element(elements, "h264-depay"),
        "wait-for-keyframe", topology.depay_wait_for_keyframe,
        "request-keyframe", topology.depay_request_keyframe,
        nullptr);
    set_caps(
        required_element(elements, "decode-memory-caps"),
        topology.decode_caps);
    g_object_set(
        required_element(elements, "decode-clock"),
        "sync", TRUE,
        nullptr);

    auto *decoder = required_element(elements, "h264-decoder");
    configure_decoder(decoder, plan.decode);

    if (topology.display) {
        try {
            configure_queue(
                required_element(
                    elements, topology.display->stages.front().name),
                *topology.display);
            for (const auto &item : topology.display->stages) {
                if (item.factory == "videorate") {
                    configure_rate(required_element(elements, item.name),
                                   *topology.display);
                } else if (item.factory == plan.decode.va_postproc_factory) {
                    configure_va_device(
                        required_element(elements, item.name), plan.decode);
                }
            }
            if (!topology.display_upload_caps.empty()) {
                set_caps(
                    required_element(elements, "display-upload-caps"),
                    topology.display_upload_caps);
            }
            if (!topology.display_download_caps.empty()) {
                set_caps(
                    required_element(elements, "display-download-caps"),
                    topology.display_download_caps);
            }
            if (config.display.enabled) {
                set_caps(
                    required_element(elements, "display-sink-caps"),
                    topology.display_sink_caps);
            }
            auto *sink = required_element(
                elements, topology.display->stages.back().name);
            if (g_object_class_find_property(
                    G_OBJECT_GET_CLASS(sink), "sync") != nullptr) {
                // gtksink 在 XWayland/软渲染下 sync=TRUE 时按时钟等待，实测
                // 只消费约 16fps 并反压 tee 拖慢分析分支；关闭 sync 让显示
                // 分支尽快消费，帧率由 display-rate 统一限制。
                g_object_set(sink, "sync", FALSE, nullptr);
            }
            if (g_object_class_find_property(
                    G_OBJECT_GET_CLASS(sink), "async") != nullptr) {
                g_object_set(sink, "async", FALSE, nullptr);
            }
        } catch (const SsvPipelineBuilderError &error) {
            if (config.display.enabled)
                rethrow_display_start(error);
            throw;
        }
    }

    if (topology.analysis) {
        if (!model_contract) {
            throw SsvPipelineBuilderError(
                SsvExitCode::ModelInitializationFailed,
                "inference.model_contract",
                "analysis pipeline requires a validated model contract");
        }
        const bool add_borders = model_contract->resize_mode
            == SsvResizeMode::Letterbox;
        configure_queue(
            required_element(elements, topology.analysis->stages.front().name),
            *topology.analysis);
        for (const auto &item : topology.analysis->stages) {
            if (item.factory == "videorate") {
                configure_rate(required_element(elements, item.name),
                               *topology.analysis);
            } else if (item.factory == "videoscale") {
                configure_resize_borders(
                    required_element(elements, item.name), add_borders);
            } else if (item.factory == plan.decode.va_postproc_factory) {
                auto *postproc = required_element(elements, item.name);
                configure_va_device(postproc, plan.decode);
                configure_resize_borders(postproc, add_borders);
            }
        }
        set_caps(
            required_element(elements, "analysis-host-caps"),
            topology.analysis_host_caps);
        auto *infer_element = required_element(elements, "analysis-infer");
        g_object_set(
            infer_element,
            "source-id", plan.source_id.c_str(),
            "source-context", source_context.get(),
            "inference-service", inference_service,
            nullptr);
        if (config.tracking.enabled) {
            auto *track = required_element(elements, "analysis-track");
            const char *gmc_method = config.tracking.gmc.method
                    == SsvGmcMethod::SparseOpticalFlow
                ? "sparse-opt-flow"
                : "none";
            g_object_set(
                track,
                "source-id", plan.source_id.c_str(),
                "source-context", source_context.get(),
                "track-thresh", config.tracking.track_threshold,
                "track-buffer", config.tracking.track_buffer,
                "match-thresh", config.tracking.match_threshold,
                "mock-track", config.tracking.mock_track,
                "gmc-method", gmc_method,
                "gmc-downscale", config.tracking.gmc.downscale,
                nullptr);
            if (plan.tracking && plan.tracking->nominal_frame_rate) {
                g_object_set(
                    track,
                    "frame-rate", *plan.tracking->nominal_frame_rate,
                    nullptr);
            }
            g_object_set(
                required_element(elements, "analysis-publish"),
                "source-id", plan.source_id.c_str(),
                "source-context", source_context.get(),
                "redis-host", config.redis.host.c_str(),
                "redis-port", config.redis.port,
                "stream-key", config.redis.stream_key.c_str(),
                "publish-cooldown-ms", config.tracking.publish_cooldown_ms,
                "event-type", source_config.event_rule.event_type.c_str(),
                "severity", source_config.event_rule.severity.c_str(),
                "rule-id", source_config.event_rule.rule_id.c_str(),
                "rule-version", source_config.event_rule.rule_version.c_str(),
                "rule-facts-json", source_config.event_rule.rule_facts_json.c_str(),
                nullptr);
        }
        auto *sink = required_element(
            elements, topology.analysis->stages.back().name);
        g_object_set(sink, "sync", FALSE, "async", FALSE, nullptr);
    }

    link_stages(topology.prefix, elements, 1);
    auto *tee = required_element(elements, "decoded-tee");
    if (topology.display) {
        try {
            link_stages(topology.display->stages, elements);
            request_pad_leases->push_back(link_tee_branch(
                tee,
                required_element(
                    elements, topology.display->stages.front().name)));
        } catch (const SsvPipelineBuilderError &error) {
            if (config.display.enabled)
                rethrow_display_start(error);
            throw;
        }
    }
    if (topology.analysis) {
        link_stages(topology.analysis->stages, elements);
        request_pad_leases->push_back(link_tee_branch(
            tee,
            required_element(
                elements, topology.analysis->stages.front().name)));
    }

    std::shared_ptr<SsvInferenceService> geometry_service;
    if (inference_service != nullptr) {
        geometry_service = {
            SSV_INFERENCE_SERVICE(g_object_ref(inference_service)),
            [](SsvInferenceService *service) { g_object_unref(service); },
        };
    }
    watch_boundary(
        required_element(elements, "decode-memory-caps"),
        contract_at(topology, SsvPipelineBoundary::DecodeTee),
        geometry_service
            ? std::function<void(int, int)>(
                  [geometry_service, source_id = plan.source_id](
                      int width, int height) {
                      infer::ssv_inference_service_update_source_geometry(
                          geometry_service.get(), source_id, width, height);
                  })
            : std::function<void(int, int)> {});
    if (config.display.enabled) {
        try {
            if (!topology.display_upload_caps.empty()) {
                watch_boundary(
                    required_element(elements, "display-upload-caps"),
                    contract_at(topology, SsvPipelineBoundary::DisplayUpload));
            }
            watch_boundary(
                required_element(elements, "display-sink-caps"),
                contract_at(topology, SsvPipelineBoundary::DisplaySink));
        } catch (const SsvPipelineBuilderError &error) {
            rethrow_display_start(error);
        }
    }
    if (topology.analysis) {
        if (plan.decode.backend == SsvDecodeBackend::Vaapi) {
            watch_boundary(
                required_element(elements, "analysis-rate"),
                contract_at(
                    topology, SsvPipelineBoundary::AnalysisGpuInput));
        }
        watch_boundary(
            required_element(elements, "analysis-host-caps"),
            contract_at(topology, SsvPipelineBoundary::AnalysisHost));
    }

    auto *rtp_caps = required_element(elements, "rtp-h264-caps");
    if (g_signal_lookup("pad-added", G_OBJECT_TYPE(source)) == 0) {
        throw SsvPipelineBuilderError(
            SsvExitCode::PipelineContractFailed,
            "pipeline.build",
            "RTSP source does not expose a dynamic pad signal");
    }
    auto dynamic_link = std::make_unique<DynamicRtspLink>(DynamicRtspLink {
        GST_ELEMENT(gst_object_ref(rtp_caps)),
    });
    g_signal_connect_data(
        source,
        "pad-added",
        G_CALLBACK(on_rtsp_pad_added),
        dynamic_link.get(),
        destroy_dynamic_rtsp_link,
        G_CONNECT_DEFAULT);
    static_cast<void>(dynamic_link.release());

    std::optional<SsvDisplayAttachment> display_attachment;
    if (config.display.enabled) {
        auto *sink = required_element(elements, "display-sink");
        auto *timing_element = required_element(
            elements, "display-sink-caps");
        std::unique_ptr<GstPad, decltype(&gst_object_unref)> timing_pad(
            gst_element_get_static_pad(timing_element, "src"),
            gst_object_unref);
        if (!timing_pad) {
            throw SsvPipelineBuilderError(
                SsvExitCode::PipelineContractFailed,
                "display.timing",
                "display timing element has no src pad");
        }
        display_attachment.emplace(sink, timing_pad.get());
    }

    return SsvPipelineInstance(
        std::move(pipeline),
        std::move(display_attachment),
        std::move(source_context));
}

} // namespace ssv
