#include "ssv_meta.hpp"

#include <atomic>
#include <cassert>
#include <cstdio>
#include <latch>
#include <limits>
#include <memory>
#include <stdexcept>
#include <string>
#include <string_view>
#include <thread>
#include <utility>
#include <vector>

namespace {

SsvDetection make_detection(float x1 = 0.1F)
{
    SsvDetection detection;
    std::snprintf(detection.class_name, sizeof(detection.class_name), "person");
    detection.confidence = 0.9F;
    detection.x1 = x1;
    detection.y1 = 0.2F;
    detection.x2 = x1 + 0.2F;
    detection.y2 = 0.4F;
    detection.class_id = 0;
    return detection;
}

SsvDetectionFrame make_detection_frame(
    std::string source_id,
    GstClockTime pts,
    std::uint64_t generation = 1,
    std::uint64_t frame_id = 0)
{
    SsvDetectionFrame frame;
    frame.frame_id = frame_id;
    frame.source_id = std::move(source_id);
    frame.timing = {pts, GST_SECOND / 5, generation};
    frame.detections.push_back(make_detection());
    return frame;
}

std::vector<SsvTrackedObject> make_tracked_objects()
{
    SsvTrackedObject object;
    object.detection = make_detection();
    object.track_id = 7;
    object.track_state = SSV_TRACK_MATCHED;
    return {object};
}

std::shared_ptr<SsvSourceMeta> initialized_source(
    std::string_view source_id)
{
    auto source = std::make_shared<SsvSourceMeta>(source_id);
    SsvTimelineCursor timeline(source);
    const auto update = timeline.on_segment({0, 0, 0, 1.0});
    assert(update.generation == 1);
    return source;
}

void test_detection_transfer_is_move_only_bounded_and_source_safe()
{
    auto source = initialized_source("camera-01");

    auto no_pts = make_detection_frame("camera-01", GST_CLOCK_TIME_NONE);
    assert(source->publish_detection(std::move(no_pts)) ==
           SsvMetaResult::NoPts);

    auto wrong_source = make_detection_frame("camera-02", GST_SECOND);
    assert(source->publish_detection(std::move(wrong_source)) ==
           SsvMetaResult::WrongSource);
    auto wrong_generation = make_detection_frame("camera-01", GST_SECOND, 2);
    assert(source->publish_detection(std::move(wrong_generation)) ==
           SsvMetaResult::WrongGeneration);

    auto valid = make_detection_frame("camera-01", GST_SECOND, 1, 0);
    const auto *storage = valid.detections.data();
    assert(source->publish_detection(std::move(valid)) ==
           SsvMetaResult::Published);
    auto occupied = make_detection_frame("camera-01", 2 * GST_SECOND, 1, 2);
    assert(source->publish_detection(std::move(occupied)) ==
           SsvMetaResult::Occupied);

    auto consumed = source->consume_detection();
    assert(consumed.result == SsvMetaResult::Consumed);
    assert(consumed.frame->frame_id == 0);
    assert(consumed.frame->detections.data() == storage);

    auto duplicate = make_detection_frame("camera-01", GST_SECOND, 1, 3);
    auto stale = make_detection_frame("camera-01", GST_SECOND / 2, 1, 4);
    assert(source->publish_detection(std::move(duplicate)) ==
           SsvMetaResult::DuplicatePts);
    assert(source->publish_detection(std::move(stale)) ==
           SsvMetaResult::StalePts);
}

void test_publish_normalizes_results_without_exposing_meta_helpers()
{
    auto source = initialized_source("camera-01");
    auto detections = make_detection_frame("camera-01", GST_SECOND);
    detections.detections.front().x1 = -0.1F;
    detections.detections.front().x2 = 1.1F;
    auto invalid = make_detection();
    invalid.x1 = std::numeric_limits<float>::quiet_NaN();
    detections.detections.push_back(invalid);
    auto infinite_confidence = make_detection();
    infinite_confidence.confidence = std::numeric_limits<float>::infinity();
    detections.detections.push_back(infinite_confidence);
    auto high_confidence = make_detection();
    high_confidence.confidence = 1.01F;
    detections.detections.push_back(high_confidence);
    auto outside = make_detection();
    outside.x1 = -0.2F;
    outside.x2 = -0.1F;
    detections.detections.push_back(outside);
    detections.detections.front().class_id = -9;

    assert(source->publish_detection(std::move(detections)) ==
           SsvMetaResult::Published);
    auto normalized = source->consume_detection();
    assert(normalized.frame->detections.size() == 1);
    assert(normalized.frame->detections.front().x1 == 0.0F);
    assert(normalized.frame->detections.front().x2 == 1.0F);
    assert(normalized.frame->detections.front().class_id == -1);

    auto objects = make_tracked_objects();
    objects.front().track_id = -9;
    objects.front().track_state = static_cast<SsvTrackState>(99);
    SsvTrackedObject invalid_object;
    invalid_object.detection = make_detection();
    invalid_object.detection.y2 = invalid_object.detection.y1;
    objects.push_back(invalid_object);

    assert(source->publish_tracked(
               std::move(*normalized.frame), std::move(objects)) ==
           SsvMetaResult::Published);
    auto tracked = source->consume_tracked();
    assert(tracked.frame->objects.size() == 1);
    assert(tracked.frame->objects.front().track_id == -1);
    assert(tracked.frame->objects.front().track_state == SSV_TRACK_NEW);
}

void test_tracked_publish_shares_one_snapshot_and_is_atomic()
{
    auto source = initialized_source("camera-01");
    auto first = make_detection_frame("camera-01", GST_SECOND, 1, 10);
    assert(source->publish_tracked(std::move(first), make_tracked_objects()) ==
           SsvMetaResult::Published);

    auto history = source->latest_tracked_at_or_before(GST_SECOND);
    auto publication = source->consume_tracked();
    assert(publication.result == SsvMetaResult::Consumed);
    assert(history != nullptr);
    assert(publication.frame.get() == history.get());

    auto second = make_detection_frame("camera-01", 2 * GST_SECOND, 1, 11);
    assert(source->publish_tracked(std::move(second), make_tracked_objects()) ==
           SsvMetaResult::Published);
    auto blocked = make_detection_frame("camera-01", 3 * GST_SECOND, 1, 12);
    assert(source->publish_tracked(std::move(blocked), make_tracked_objects()) ==
           SsvMetaResult::Occupied);
    auto still_second = source->latest_tracked_at_or_before(4 * GST_SECOND);
    assert(still_second != nullptr && still_second->frame_id == 11);
}

void test_history_is_causal_bounded_and_preserves_empty_snapshot()
{
    auto source = initialized_source("camera-01");
    for (std::uint64_t index = 0; index < 70; ++index) {
        auto frame = make_detection_frame(
            "camera-01", index * 10 * GST_MSECOND, 1, index);
        assert(source->publish_tracked(std::move(frame), make_tracked_objects()) ==
               SsvMetaResult::Published);
        assert(source->consume_tracked().result == SsvMetaResult::Consumed);
    }
    assert(source->history_depth() == 64);
    assert(source->latest_tracked_at_or_before(40 * GST_MSECOND) == nullptr);
    auto causal = source->latest_tracked_at_or_before(650 * GST_MSECOND);
    assert(causal != nullptr && causal->frame_id == 65);
    assert(source->latest_tracked_at_or_before(5 * GST_MSECOND) == nullptr);
    assert(source->latest_tracked_at_or_before(GST_CLOCK_TIME_NONE) == nullptr);

    auto empty = make_detection_frame("camera-01", 3 * GST_SECOND, 1, 80);
    assert(source->publish_tracked(std::move(empty), {}) ==
           SsvMetaResult::Published);
    assert(source->consume_tracked().result == SsvMetaResult::Consumed);
    auto cleared = source->latest_tracked_at_or_before(3 * GST_SECOND);
    assert(cleared != nullptr && cleared->objects.empty());
    assert(source->history_depth() == 1);
}

void test_reset_hides_old_generation_and_clears_both_stages()
{
    auto source = std::make_shared<SsvSourceMeta>("camera-01");
    SsvTimelineCursor timeline(source);
    timeline.on_segment({0, 0, 0, 1.0});

    auto detection = make_detection_frame("camera-01", GST_SECOND, 1, 4);
    assert(source->publish_detection(std::move(detection)) ==
           SsvMetaResult::Published);
    auto tracked = make_detection_frame("camera-01", GST_SECOND, 1, 4);
    assert(source->publish_tracked(std::move(tracked), make_tracked_objects()) ==
           SsvMetaResult::Published);
    auto held = source->latest_tracked_at_or_before(GST_SECOND);
    assert(held != nullptr);

    const auto reset = timeline.on_lifecycle_reset();
    assert(reset.generation == 2);
    assert(source->consume_detection().result == SsvMetaResult::Empty);
    assert(source->consume_tracked().result == SsvMetaResult::Empty);
    assert(source->latest_tracked_at_or_before(GST_SECOND) == nullptr);
    assert(held->timing.generation == 1);
    assert(source->stats().generation_resets == 2);
}

void test_concurrent_history_queries_keep_shared_snapshots_alive()
{
    auto source = initialized_source("camera-01");
    auto initial = make_detection_frame("camera-01", 0, 1, 0);
    assert(source->publish_tracked(std::move(initial), make_tracked_objects()) ==
           SsvMetaResult::Published);
    assert(source->consume_tracked().result == SsvMetaResult::Consumed);

    std::atomic<bool> done = false;
    std::atomic<std::uint64_t> reads = 0;
    std::latch initial_snapshot_held(1);
    std::thread consumer([&] {
        auto held = source->latest_tracked_at_or_before(0);
        assert(held != nullptr && held->frame_id == 0);
        reads.fetch_add(1, std::memory_order_relaxed);
        initial_snapshot_held.count_down();

        while (!done.load(std::memory_order_acquire)) {
            auto snapshot = source->latest_tracked_at_or_before(
                1000 * GST_SECOND);
            if (snapshot)
                reads.fetch_add(1, std::memory_order_relaxed);
        }
        assert(held->frame_id == 0);
    });

    initial_snapshot_held.wait();
    for (std::uint64_t index = 1; index <= 200; ++index) {
        auto frame = make_detection_frame(
            "camera-01", index * GST_MSECOND, 1, index);
        while (source->publish_tracked(
                   std::move(frame), make_tracked_objects()) ==
               SsvMetaResult::Occupied) {
            source->consume_tracked();
        }
        source->consume_tracked();
    }
    done.store(true, std::memory_order_release);
    consumer.join();
    assert(source->latest_tracked_at_or_before(0) == nullptr);
    assert(reads.load(std::memory_order_relaxed) > 0);
}

void test_registry_is_source_scoped_and_rejects_empty_source()
{
    auto camera_a = ssv_meta("meta-registry-camera-a");
    SsvTimelineCursor timeline_a(camera_a);
    timeline_a.on_segment({0, 0, 0, 1.0});
    auto camera_b = ssv_meta("meta-registry-camera-b");
    SsvTimelineCursor timeline_b(camera_b);
    timeline_b.on_segment({0, 0, 0, 1.0});
    assert(ssv_meta("meta-registry-camera-a").get() == camera_a.get());

    auto detection_a = make_detection_frame(
        "meta-registry-camera-a", GST_SECOND);
    auto detection_b = make_detection_frame(
        "meta-registry-camera-b", GST_SECOND);
    assert(camera_a->publish_detection(std::move(detection_a)) ==
           SsvMetaResult::Published);
    assert(camera_b->publish_detection(std::move(detection_b)) ==
           SsvMetaResult::Published);

    timeline_a.on_lifecycle_reset();
    assert(camera_a->consume_detection().result == SsvMetaResult::Empty);
    assert(camera_b->consume_detection().result == SsvMetaResult::Consumed);

    bool rejected = false;
    try {
        ssv_meta("");
    } catch (const std::invalid_argument &) {
        rejected = true;
    }
    assert(rejected);
}

void test_source_context_shares_registry_identity_and_owner()
{
    const auto source_id = "source-context-camera";
    auto registry_meta = ssv_meta(source_id);
    auto context = std::make_shared<SsvSourceContext>(source_id);

    assert(context->source_id() == source_id);
    assert(context->meta() == registry_meta);

    SsvTimelineCursor timeline(context->meta());
    const auto update = timeline.on_segment({0, 0, 0, 1.0});
    assert(update.generation == 1);
    assert(context->meta()->generation() == registry_meta->generation());
}

} // namespace

int main()
{
    test_detection_transfer_is_move_only_bounded_and_source_safe();
    test_publish_normalizes_results_without_exposing_meta_helpers();
    test_tracked_publish_shares_one_snapshot_and_is_atomic();
    test_history_is_causal_bounded_and_preserves_empty_snapshot();
    test_reset_hides_old_generation_and_clears_both_stages();
    test_concurrent_history_queries_keep_shared_snapshots_alive();
    test_registry_is_source_scoped_and_rejects_empty_source();
    test_source_context_shares_registry_identity_and_owner();
    return 0;
}
