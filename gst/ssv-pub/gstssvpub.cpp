#include "gstssvpub.hpp"
#include "ssv_logging.hpp"
#include "ssv_meta.hpp"

#include <gst/video/video.h>
#include <hiredis/hiredis.h>
#include <nlohmann/json.hpp>

#include <chrono>
#include <cstdint>
#include <cstring>
#include <ctime>
#include <exception>
#include <memory>
#include <new>
#include <string>
#include <unordered_map>
#include <utility>

GST_DEBUG_CATEGORY_STATIC(ssv_pub_debug);

struct _SsvPub {
    GstBaseTransform parent;

    gchar *source_id;
    SsvSourceContext *source_context;
    gchar *redis_host;
    gint redis_port;
    gchar *stream_key;
    gint publish_cooldown_ms;
    gchar *event_type;
    gchar *severity;
    gchar *rule_id;
    gchar *rule_version;
    gchar *rule_facts_json;

    redisContext *redis_ctx;
    std::shared_ptr<SsvSourceMeta> meta_owner;
    SsvSourceMeta *meta;
    std::unordered_map<int, std::int64_t> track_last_published_ms;
};

enum {
    PROP_0,
    PROP_SOURCE_ID,
    PROP_SOURCE_CONTEXT,
    PROP_REDIS_HOST,
    PROP_REDIS_PORT,
    PROP_STREAM_KEY,
    PROP_PUBLISH_COOLDOWN_MS,
    PROP_EVENT_TYPE,
    PROP_SEVERITY,
    PROP_RULE_ID,
    PROP_RULE_VERSION,
    PROP_RULE_FACTS_JSON,
};

G_DEFINE_TYPE(SsvPub, ssv_pub, GST_TYPE_BASE_TRANSFORM)

static GstStaticPadTemplate sink_template = GST_STATIC_PAD_TEMPLATE(
    "sink", GST_PAD_SINK, GST_PAD_ALWAYS,
    GST_STATIC_CAPS(
        "video/x-raw, format=(string)RGBA, "
        "width=(int)[1, MAX], height=(int)[1, MAX], "
        "framerate=(fraction)[0, MAX]"));

static GstStaticPadTemplate src_template = GST_STATIC_PAD_TEMPLATE(
    "src", GST_PAD_SRC, GST_PAD_ALWAYS,
    GST_STATIC_CAPS(
        "video/x-raw, format=(string)RGBA, "
        "width=(int)[1, MAX], height=(int)[1, MAX], "
        "framerate=(fraction)[0, MAX]"));

// ── Redis helpers ──────────────────────────────────────────────────────

std::string
ssv_pub_build_event_payload(const SsvTrackedFrame &frame, std::int64_t timestamp_ms) {
    return ssv_pub_build_event_payload(
        frame, timestamp_ms, ssv::SsvEventRuleConfig {});
}

std::string
ssv_pub_build_event_payload(
    const SsvTrackedFrame &frame,
    std::int64_t timestamp_ms,
    const ssv::SsvEventRuleConfig &event_rule) {
    using json = nlohmann::json;

    json detections_arr = json::array();
    for (const auto &object : frame.objects) {
        const auto &d = object.detection;
        json det_obj = {
            {"class", d.class_name},
            {"class_id", d.class_id},
            {"confidence", d.confidence},
            {"bbox", {d.x1, d.y1, d.x2, d.y2}},
            {"track_id", object.track_id},
            {"track_state", object.track_state},
            {"occluded", object.occluded}
        };
        detections_arr.push_back(det_obj);
    }

    json msg = {
        {"type", "detection"},
        {"source", frame.source_id},
        {"timestamp_ms", timestamp_ms},
        {"frame_id", frame.frame_id},
        {"detections", detections_arr}
    };

    if (!event_rule.event_type.empty()) {
        msg["event_type"] = event_rule.event_type;
        msg["severity"] = event_rule.severity;
        msg["rule_id"] = event_rule.rule_id;
        msg["rule_version"] = event_rule.rule_version;
        msg["rule_facts"] = json::parse(event_rule.rule_facts_json);
    }

    return msg.dump();
}

bool
ssv_pub_snapshot_is_current(
    const SsvSourceContext &source_context,
    const SsvTrackedFrame &frame)
{
    const auto source_id = source_context.source_id();
    const auto meta = source_context.meta();
    return meta != nullptr
        && ssv_pub_snapshot_is_current(source_id, *meta, frame);
}

bool
ssv_pub_snapshot_is_current(
    std::string_view source_id,
    const SsvSourceMeta &meta,
    const SsvTrackedFrame &frame)
{
    return !source_id.empty() && frame.source_id == source_id
        && meta.source_id() == source_id
        && meta.generation() == frame.timing.generation;
}

bool
ssv_pub_snapshot_is_current(
    std::string_view source_id,
    const SsvTrackedFrame &frame)
{
    const auto meta = ssv_meta(source_id);
    return meta != nullptr
        && ssv_pub_snapshot_is_current(source_id, *meta, frame);
}

static std::int64_t
ssv_steady_now_ms()
{
    return std::chrono::duration_cast<std::chrono::milliseconds>(
        std::chrono::steady_clock::now().time_since_epoch()).count();
}

bool
ssv_pub_should_publish(
    const std::vector<SsvTrackedObject> &objects,
    int cooldown_ms,
    std::int64_t now_ms,
    std::unordered_map<int, std::int64_t> &last_published_ms)
{
    if (cooldown_ms <= 0 || objects.empty())
        return true;

    bool state_edge = false;
    for (const auto &object : objects) {
        if (object.track_id >= 0
            && (object.track_state == SSV_TRACK_NEW
                || object.track_state == SSV_TRACK_LOST
                || object.track_state == SSV_TRACK_DEAD)) {
            state_edge = true;
            break;
        }
    }

    bool cooldown_expired = false;
    if (!state_edge) {
        for (const auto &object : objects) {
            const auto found = last_published_ms.find(object.track_id);
            if (found == last_published_ms.end()
                || now_ms - found->second >= cooldown_ms) {
                cooldown_expired = true;
                break;
            }
        }
    }

    if (!state_edge && !cooldown_expired)
        return false;

    for (const auto &object : objects)
        last_published_ms[object.track_id] = now_ms;

    // 只保留冷却窗口内的目标，避免长时间运行后 map 无限增长。
    if (last_published_ms.size() > 256) {
        for (auto it = last_published_ms.begin();
             it != last_published_ms.end();) {
            if (now_ms - it->second >= cooldown_ms)
                it = last_published_ms.erase(it);
            else
                ++it;
        }
    }
    return true;
}

static gboolean
ssv_pub_bind_source(SsvPub *self)
{
    try {
        if (self->source_context != nullptr) {
            if (self->source_context->source_id() != self->source_id) {
                GST_ELEMENT_ERROR(self, RESOURCE, SETTINGS,
                    ("source-context does not match source-id"),
                    ("source-id=%s, context-source=%s",
                        self->source_id,
                        std::string(self->source_context->source_id()).c_str()));
                return FALSE;
            }
            self->meta_owner = self->source_context->meta();
        } else {
            self->meta_owner = ssv_meta(self->source_id);
        }
        self->meta = self->meta_owner.get();
        return self->meta != nullptr;
    } catch (const std::exception &error) {
        GST_ELEMENT_ERROR(self, RESOURCE, SETTINGS,
            ("source metadata context is invalid"), ("%s", error.what()));
        return FALSE;
    }
}

static gboolean
ssv_pub_redis_connect(SsvPub *self) {
    if (self->redis_ctx) {
        redisFree(self->redis_ctx);
        self->redis_ctx = nullptr;
    }

    struct timeval timeout = { 2, 0 };
    self->redis_ctx = redisConnectWithTimeout(self->redis_host, self->redis_port, timeout);

    if (!self->redis_ctx || self->redis_ctx->err) {
        if (self->redis_ctx) {
            GST_ERROR_OBJECT(self, "Redis connect failed: %s", self->redis_ctx->errstr);
            redisFree(self->redis_ctx);
            self->redis_ctx = nullptr;
        } else {
            GST_ERROR_OBJECT(self, "Redis connect failed: allocation error");
        }
        return FALSE;
    }

    GST_INFO_OBJECT(self, "connected to Redis at %s:%d", self->redis_host, self->redis_port);
    return TRUE;
}

static void
ssv_pub_redis_publish(SsvPub *self, const SsvTrackedFrame &frame) {
    if (!self->redis_ctx)
        return;

    ssv::SsvEventRuleConfig event_rule {
        self->event_type,
        self->severity,
        self->rule_id,
        self->rule_version,
        self->rule_facts_json,
    };
    std::string payload = ssv_pub_build_event_payload(
        frame, std::time(nullptr) * 1000LL, event_rule);

    auto *reply = (redisReply *)redisCommand(self->redis_ctx,
        "XADD %s * event %s",
        self->stream_key, payload.c_str());

    if (!reply) {
        GST_WARNING_OBJECT(self, "Redis XADD failed, reconnecting...");
        ssv_pub_redis_connect(self);
        return;
    }
    if (reply->type == REDIS_REPLY_ERROR) {
        GST_WARNING_OBJECT(self, "Redis error: %s", reply->str);
    }
    freeReplyObject(reply);

    GST_DEBUG_OBJECT(self, "published frame %" G_GUINT64_FORMAT " with %zu detections",
        frame.frame_id, frame.objects.size());
}

// ── GstBaseTransform callbacks ────────────────────────────────────────

static gboolean
ssv_pub_start(GstBaseTransform *trans) {
    auto *self = SSV_PUB(trans);
    if (!self->source_id || self->source_id[0] == '\0') {
        GST_ELEMENT_ERROR(self, RESOURCE, SETTINGS,
            ("source-id must not be empty"), (nullptr));
        return FALSE;
    }
    if (!ssv_pub_bind_source(self))
        return FALSE;
    return ssv_pub_redis_connect(self);
}

static gboolean
ssv_pub_stop(GstBaseTransform *trans) {
    auto *self = SSV_PUB(trans);
    if (self->redis_ctx) {
        redisFree(self->redis_ctx);
        self->redis_ctx = nullptr;
    }
    self->meta = nullptr;
    self->meta_owner.reset();
    return TRUE;
}

static GstFlowReturn
ssv_pub_transform_ip(GstBaseTransform *trans, GstBuffer *buf) {
    (void)buf;
    auto *self = SSV_PUB(trans);

    if (!self->meta)
        return GST_FLOW_OK;
    auto consumed = self->meta->consume_tracked();
    if (consumed.result != SsvMetaResult::Consumed || !consumed.frame)
        return GST_FLOW_OK;

    const auto snapshot = std::move(consumed.frame);
    if (snapshot->objects.empty())
        return GST_FLOW_OK;
    if (!self->meta
        || !ssv_pub_snapshot_is_current(
            self->source_id, *self->meta, *snapshot)) {
        return GST_FLOW_OK;
    }
    if (!ssv_pub_should_publish(
            snapshot->objects,
            self->publish_cooldown_ms,
            ssv_steady_now_ms(),
            self->track_last_published_ms)) {
        return GST_FLOW_OK;
    }
    ssv_pub_redis_publish(self, *snapshot);

    return GST_FLOW_OK;
}

// ── Properties ─────────────────────────────────────────────────────────

static void
ssv_pub_set_property(GObject *object, guint prop_id,
                      const GValue *value, GParamSpec *pspec) {
    auto *self = SSV_PUB(object);
    switch (prop_id) {
    case PROP_SOURCE_ID:
        g_free(self->source_id);
        self->source_id = g_value_dup_string(value);
        break;
    case PROP_SOURCE_CONTEXT:
        self->source_context = static_cast<SsvSourceContext *>(
            g_value_get_pointer(value));
        break;
    case PROP_REDIS_HOST:
        g_free(self->redis_host);
        self->redis_host = g_value_dup_string(value);
        break;
    case PROP_REDIS_PORT:
        self->redis_port = g_value_get_int(value);
        break;
    case PROP_STREAM_KEY:
        g_free(self->stream_key);
        self->stream_key = g_value_dup_string(value);
        break;
    case PROP_PUBLISH_COOLDOWN_MS:
        self->publish_cooldown_ms = g_value_get_int(value);
        break;
    case PROP_EVENT_TYPE:
        g_free(self->event_type);
        self->event_type = g_value_dup_string(value);
        break;
    case PROP_SEVERITY:
        g_free(self->severity);
        self->severity = g_value_dup_string(value);
        break;
    case PROP_RULE_ID:
        g_free(self->rule_id);
        self->rule_id = g_value_dup_string(value);
        break;
    case PROP_RULE_VERSION:
        g_free(self->rule_version);
        self->rule_version = g_value_dup_string(value);
        break;
    case PROP_RULE_FACTS_JSON:
        g_free(self->rule_facts_json);
        self->rule_facts_json = g_value_dup_string(value);
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
    }
}

static void
ssv_pub_get_property(GObject *object, guint prop_id,
                      GValue *value, GParamSpec *pspec) {
    auto *self = SSV_PUB(object);
    switch (prop_id) {
    case PROP_SOURCE_ID:
        g_value_set_string(value, self->source_id);
        break;
    case PROP_SOURCE_CONTEXT:
        g_value_set_pointer(value, self->source_context);
        break;
    case PROP_REDIS_HOST:
        g_value_set_string(value, self->redis_host);
        break;
    case PROP_REDIS_PORT:
        g_value_set_int(value, self->redis_port);
        break;
    case PROP_STREAM_KEY:
        g_value_set_string(value, self->stream_key);
        break;
    case PROP_PUBLISH_COOLDOWN_MS:
        g_value_set_int(value, self->publish_cooldown_ms);
        break;
    case PROP_EVENT_TYPE:
        g_value_set_string(value, self->event_type);
        break;
    case PROP_SEVERITY:
        g_value_set_string(value, self->severity);
        break;
    case PROP_RULE_ID:
        g_value_set_string(value, self->rule_id);
        break;
    case PROP_RULE_VERSION:
        g_value_set_string(value, self->rule_version);
        break;
    case PROP_RULE_FACTS_JSON:
        g_value_set_string(value, self->rule_facts_json);
        break;
    default:
        G_OBJECT_WARN_INVALID_PROPERTY_ID(object, prop_id, pspec);
    }
}

// ── Class / instance init ──────────────────────────────────────────────

static void
ssv_pub_finalize(GObject *object) {
    auto *self = SSV_PUB(object);
    g_free(self->source_id);
    g_free(self->redis_host);
    g_free(self->stream_key);
    g_free(self->event_type);
    g_free(self->severity);
    g_free(self->rule_id);
    g_free(self->rule_version);
    g_free(self->rule_facts_json);
    if (self->redis_ctx)
        redisFree(self->redis_ctx);
    self->meta_owner.reset();
    self->meta_owner.~shared_ptr<SsvSourceMeta>();
    self->track_last_published_ms.~unordered_map<int, std::int64_t>();
    G_OBJECT_CLASS(ssv_pub_parent_class)->finalize(object);
}

static void
ssv_pub_class_init(SsvPubClass *klass) {
    auto *gobject_class = G_OBJECT_CLASS(klass);
    auto *base_class = GST_BASE_TRANSFORM_CLASS(klass);
    auto *element_class = GST_ELEMENT_CLASS(klass);

    gobject_class->set_property = ssv_pub_set_property;
    gobject_class->get_property = ssv_pub_get_property;
    gobject_class->finalize = ssv_pub_finalize;

    g_object_class_install_property(gobject_class, PROP_SOURCE_ID,
        g_param_spec_string("source-id", "Source ID",
            "Perception metadata source identifier",
            "pipeline-0",
            (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                          G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PROP_SOURCE_CONTEXT,
        g_param_spec_pointer("source-context", "Source Context",
            "Borrowed SsvSourceContext owned by the pipeline",
            (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                          G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PROP_REDIS_HOST,
        g_param_spec_string("redis-host", "Redis Host",
            "Redis server hostname",
            "localhost", (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PROP_REDIS_PORT,
        g_param_spec_int("redis-port", "Redis Port",
            "Redis server port",
            1, 65535, 6379,
            (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PROP_STREAM_KEY,
        g_param_spec_string("stream-key", "Stream Key",
            "Redis Stream key for detection events",
            "ssv:events", (GParamFlags)(G_PARAM_READWRITE | G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PROP_PUBLISH_COOLDOWN_MS,
        g_param_spec_int("publish-cooldown-ms", "Publish Cooldown (ms)",
            "Minimum interval between published events for the same track; "
            "0 disables cooldown",
            0, G_MAXINT, 30000,
            (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                          G_PARAM_STATIC_STRINGS)));

    g_object_class_install_property(gobject_class, PROP_EVENT_TYPE,
        g_param_spec_string("event-type", "Event Type",
            "Configured event type published with detection events",
            nullptr, (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                                   G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(gobject_class, PROP_SEVERITY,
        g_param_spec_string("severity", "Severity",
            "Configured event severity published with detection events",
            nullptr, (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                                   G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(gobject_class, PROP_RULE_ID,
        g_param_spec_string("rule-id", "Rule ID",
            "Configured rule identifier published with detection events",
            nullptr, (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                                   G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(gobject_class, PROP_RULE_VERSION,
        g_param_spec_string("rule-version", "Rule Version",
            "Configured rule version published with detection events",
            nullptr, (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                                   G_PARAM_STATIC_STRINGS)));
    g_object_class_install_property(gobject_class, PROP_RULE_FACTS_JSON,
        g_param_spec_string("rule-facts-json", "Rule Facts JSON",
            "Configured JSON object of rule facts published with detection events",
            "{}", (GParamFlags)(G_PARAM_READWRITE | GST_PARAM_MUTABLE_READY |
                                 G_PARAM_STATIC_STRINGS)));

    gst_element_class_set_static_metadata(element_class,
        "SSV Redis Publisher",
        "Generic/Video",
        "Publish detection events to Redis Streams",
        "site-safety-vision");

    gst_element_class_add_static_pad_template(element_class, &sink_template);
    gst_element_class_add_static_pad_template(element_class, &src_template);

    base_class->start = ssv_pub_start;
    base_class->stop = ssv_pub_stop;
    base_class->transform_ip = ssv_pub_transform_ip;
    base_class->passthrough_on_same_caps = TRUE;
}

static void
ssv_pub_init(SsvPub *self) {
    new (&self->meta_owner) std::shared_ptr<SsvSourceMeta>();
    self->source_id = g_strdup("pipeline-0");
    self->source_context = nullptr;
    self->redis_host = g_strdup("localhost");
    self->redis_port = 6379;
    self->stream_key = g_strdup("ssv:events");
    self->publish_cooldown_ms = 30000;
    self->event_type = nullptr;
    self->severity = nullptr;
    self->rule_id = nullptr;
    self->rule_version = nullptr;
    self->rule_facts_json = g_strdup("{}");
    self->redis_ctx = nullptr;
    self->meta = nullptr;
    new (&self->track_last_published_ms)
        std::unordered_map<int, std::int64_t>();
}

// ── Plugin registration ────────────────────────────────────────────────

GST_ELEMENT_REGISTER_DEFINE(ssv_pub, "ssvpub",
    GST_RANK_NONE, SSV_TYPE_PUB)

static gboolean
plugin_init(GstPlugin *plugin) {
    SSV_GST_DEBUG_INIT(ssv_pub_debug, "ssv-pub");
    return GST_ELEMENT_REGISTER(ssv_pub, plugin);
}

GST_PLUGIN_DEFINE(
    GST_VERSION_MAJOR, GST_VERSION_MINOR,
    ssvpub,
    "SSV Redis Publisher Plugin",
    plugin_init,
    "0.1.0", "LGPL",
    "site-safety-vision",
    "https://github.com/site-safety-vision/site-safety-vision")
