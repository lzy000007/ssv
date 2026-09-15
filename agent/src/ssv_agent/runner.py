"""DeerFlowClient 单事件复核执行。"""

from __future__ import annotations

from typing import Any

from deerflow.client import DeerFlowClient

from ssv_agent.prompt import build_review_prompt
from ssv_agent.review_context import ReviewContext
from ssv_agent.review_context import RuleRetrievalContext


def extract_final_answer(values_data: dict[str, Any]) -> str | None:
    """从 values 事件提取最终 AI 消息。"""
    messages = values_data.get("messages") or []
    for message in reversed(messages):
        if isinstance(message, dict) and message.get("type") == "ai":
            content = message.get("content") or ""
            if content:
                return content
    return None


def run_review(
    client: DeerFlowClient,
    context: ReviewContext,
    rule_context: RuleRetrievalContext | None = None,
) -> str:
    """执行一次复核，返回最终文本；异常向上抛出。"""
    thread_id = f"event-{context.event_id}"
    final_text = ""
    accumulated: list[str] = []

    for event in client.stream(
        thread_id=thread_id,
        message=build_review_prompt(context, rule_context),
    ):
        if event.type == "values":
            answer = extract_final_answer(event.data)
            if answer is not None:
                final_text = answer
        elif event.type == "messages-tuple" and event.data.get("type") == "ai":
            accumulated.append(event.data.get("content") or "")

    if not final_text:
        final_text = "".join(accumulated)
    return final_text.strip()
