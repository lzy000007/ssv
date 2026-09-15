# Agent 运行配置统一计划

## 步骤 1：收口主配置

- 为 SQLite、embedding 服务地址和 Qdrant URL 增加严格 YAML 字段。
- Agent 启动时把 YAML 解析结果传给现有存储与工具边界。
- 知识入库命令使用相同 YAML 字段。
- 更新配置模板和 Agent 配置文档。

## 步骤 2：整理工作区

- 初始化仓库声明的子模块。
- 清除无内容差异的换行状态。
- 检查 Python 语法和 Git diff。
