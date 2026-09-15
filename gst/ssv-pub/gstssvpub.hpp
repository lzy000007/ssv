#pragma once

#include "ssv_meta.hpp"
#include "ssv_config.hpp"

#include <gst/base/gstbasetransform.h>

#include <cstdint>
#include <string>
#include <string_view>
#include <unordered_map>
#include <vector>

std::string ssv_pub_build_event_payload(
    const SsvTrackedFrame &frame,
    std::int64_t timestamp_ms);
std::string ssv_pub_build_event_payload(
    const SsvTrackedFrame &frame,
    std::int64_t timestamp_ms,
    const ssv::SsvEventRuleConfig &event_rule);
bool ssv_pub_snapshot_is_current(
    const SsvSourceContext &source_context,
    const SsvTrackedFrame &frame);
bool ssv_pub_snapshot_is_current(
    std::string_view source_id,
    const SsvSourceMeta &meta,
    const SsvTrackedFrame &frame);
bool ssv_pub_snapshot_is_current(
    std::string_view source_id,
    const SsvTrackedFrame &frame);
bool ssv_pub_should_publish(
    const std::vector<SsvTrackedObject> &objects,
    int cooldown_ms,
    std::int64_t now_ms,
    std::unordered_map<int, std::int64_t> &last_published_ms);

G_BEGIN_DECLS

#define SSV_TYPE_PUB (ssv_pub_get_type())
G_DECLARE_FINAL_TYPE(SsvPub, ssv_pub, SSV, PUB, GstBaseTransform)

G_END_DECLS
