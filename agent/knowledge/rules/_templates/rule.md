---
kind: rule
rule_id: monitoring-example
version: v1
event_type: example_observation
severity: medium
aliases:
  - 监测规则示例
---

# 通用监测规则结构示例

## 原始依据

本文件是项目示例，不能支撑确定性结论。复制后应根据真实文件确定规则身份、事件类型和原文出处，业务解释单独记录。目录 `_templates` 不参与检索。

## 1.1 规则要求与适用条件

在正文说明监测对象、适用范围、违规和合规所需的观察。现场事实由事件 `rule_facts` 提供，模型结合登记证据核验，不根据检测标签补造事实。`rule_id` 与 `version` 共同标识规则版本。

## 证据不足

遮挡、模糊、目标过小、对象关系不明或相关时段缺失时保留 `uncertain`。未发现违规不等于确认合规，不能由外观推断不可见的性能或资质。
