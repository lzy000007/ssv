"""从权威事件上下文构造稳定的规则检索查询。"""

from __future__ import annotations

import json
from typing import Any


def build_rule_query(case: Any) -> str:
    """按事件事实生成不依赖模型输出的规则查询。"""
    context = case.to_review_context() if hasattr(case, "to_review_context") else case
    detections = [
        {
            "class": detection.class_name,
            "class_id": detection.class_id,
            "track_id": detection.track_id,
        }
        for detection in context.detections
    ]
    values = {
        "event_type": context.event_type,
        "rule_id": context.rule_id,
        "rule_version": context.rule_version,
        "rule_facts": context.rule_facts,
        "detections": detections,
        "severity": context.severity,
        "question": context.question,
    }
    return "安全规则复核：" + json.dumps(values, ensure_ascii=False, sort_keys=True)
