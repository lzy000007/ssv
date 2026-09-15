#include "gstssvpub.hpp"
#include "ssv_meta.hpp"

#include <cassert>
#include <cstdio>
#include <memory>
#include <string>
#include <utility>

#include <nlohmann/json.hpp>

int main()
{
    SsvTrackedFrame frame;
    frame.frame_id = 170;
    frame.source_id = "camera-01";
    frame.timing = {5 * GST_SECOND, GST_SECOND / 5, 9};

    SsvTrackedObject object;
    std::snprintf(
        object.detection.class_name,
        sizeof(object.detection.class_name),
        "person");
    object.detection.confidence = 0.72F;
    object.detection.x1 = 0.1F;
    object.detection.y1 = 0.2F;
    object.detection.x2 = 0.3F;
    object.detection.y2 = 0.4F;
    object.detection.class_id = 0;
    object.track_id = 12;
    object.track_state = SSV_TRACK_MATCHED;
    object.occluded = true;
    frame.objects.push_back(object);

    ssv::SsvEventRuleConfig event_rule;
    event_rule.event_type = "person_without_helmet";
    event_rule.severity = "high";
    event_rule.rule_id = "GB26860-2011-5.2.2";
    event_rule.rule_version = "GB/T 26860-2011";
    event_rule.rule_facts_json = R"({"helmet_required":true,"subject":"person"})";

    const auto payload = ssv_pub_build_event_payload(
        frame, 1234567890LL, event_rule);
    const auto message = nlohmann::json::parse(payload);
    assert(message.size() == 10);
    assert(message["type"] == "detection");
    assert(message["source"] == "camera-01");
    assert(message["timestamp_ms"] == 1234567890LL);
    assert(message["frame_id"] == 170);
    assert(message["event_type"] == "person_without_helmet");
    assert(message["severity"] == "high");
    assert(message["rule_id"] == "GB26860-2011-5.2.2");
    assert(message["rule_version"] == "GB/T 26860-2011");
    assert(message["rule_facts"] == nlohmann::json({
        {"helmet_required", true}, {"subject", "person"}}));
    assert(!message.contains("media_pts_ns"));
    assert(!message.contains("stream_generation"));

    const auto &serialized = message["detections"].at(0);
    assert(serialized.size() == 7);
    assert(serialized["class"] == "person");
    assert(serialized["class_id"] == 0);
    assert(serialized["confidence"] == object.detection.confidence);
    assert(serialized["bbox"] == nlohmann::json::array(
        {object.detection.x1, object.detection.y1,
         object.detection.x2, object.detection.y2}));
    assert(serialized["track_id"] == 12);
    assert(serialized["track_state"] == SSV_TRACK_MATCHED);
    assert(serialized["occluded"] == true);

    SsvTrackedFrame default_source_frame = frame;
    default_source_frame.source_id = "pipeline-0";
    const auto default_source_payload = nlohmann::json::parse(
        ssv_pub_build_event_payload(default_source_frame, 1234567890LL));
    assert(default_source_payload["source"] == "pipeline-0");
    assert(default_source_payload["timestamp_ms"] == 1234567890LL);

    const std::string race_source = "pub-reset-race-test";
    auto source = ssv_meta(race_source);
    SsvTimelineCursor timeline(source);
    const auto initial = timeline.on_segment({0, 0, 0, 1.0});
    SsvTrackedFrame in_flight;
    in_flight.source_id = race_source;
    in_flight.timing = {GST_SECOND, GST_SECOND / 5, initial.generation};
    const auto snapshot = std::make_shared<const SsvTrackedFrame>(
        std::move(in_flight));
    assert(ssv_pub_snapshot_is_current(race_source, *snapshot));
    assert(ssv_pub_snapshot_is_current(race_source, *source, *snapshot));

    const auto reset = timeline.on_lifecycle_reset();
    assert(reset.generation == initial.generation + 1);
    assert(!ssv_pub_snapshot_is_current(race_source, *snapshot));
    assert(!ssv_pub_snapshot_is_current(race_source, *source, *snapshot));
    return 0;
}
