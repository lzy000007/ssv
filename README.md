# Site Safety Vision

安全帽佩戴视频监测分析系统。项目用 GStreamer C++ pipeline 完成实时视频检测、跟踪、显示和事件发布，用 Redis Streams 连接实时链路与 Python Agent；`./ssv` 是统一开发入口。

## 项目定位

```text
RTSP / 视频输入
       |
       v
./ssv -> C++ ssv-runner -> 解码 -> 检测 -> 跟踪 -> 显示/overlay
                                      |
                                      v
                               Redis Streams
                                      |
                                      v
                              Python Agent
```

- `runner/` 与 `gst/`：实时 pipeline、ONNX Runtime/TensorRT 推理、BoT-SORT 跟踪、GTK 显示和 Redis 发布。
- `scripts/ssv_cli/`：依赖准备、Meson 构建、模型工具、Redis 运维和测试编排。
- `agent/`：Redis 事件消费、SQLite `EventLedger`、可选复核 worker、可选向量索引 worker。
- `config/ssv.example.yaml`：实时 runner 与 Agent 共用的 YAML 模板。

Agent 在实时链路之外异步运行，不会阻塞每帧检测。当前仓库仍处于工程基线阶段：完整安全帽业务规则、真实模型效果评估、面向用户的 Web 前端和端到端报告闭环尚未完成。

## 依赖概览

| 类别 | 主要依赖 | 用途 |
| --- | --- | --- |
| C++ 构建 | C++17、Meson >= 1.1、Ninja、`pkg-config`、CMake | 编译 runner、插件和测试 |
| 视频与显示 | GStreamer >= 1.20（base/good/bad/tools）、GTK 3 | 解码、pipeline 和本地显示 |
| C++ 库 | `yaml-cpp`、`hiredis`、`nlohmann-json` | 配置、Redis 发布和事件序列化 |
| 推理与跟踪 | ONNX Runtime、OpenCV、BLAS/LAPACK | YOLO 推理、BoT-SORT GMC 和数学运行库 |
| Python CLI | Python >= 3.12、`uv`、PyYAML、redis | `./ssv`、模型工具和测试 |
| Python Agent | Python >= 3.12、`uv`、DeerFlow harness、`qdrant-client` | 事件消费、复核和索引 |
| 本地基础设施 | Docker Engine >= 24、Compose plugin、Redis、可访问的 RTSP 源 | 本地运行和联调 |
| 可选 GPU runtime | NVIDIA CUDA/TensorRT、Intel OpenVINO 或 AMD MIGraphX | 对应硬件加速 profile |

系统包、GPU profile、managed SDK 和 Docker 官方仓库配置见 [依赖与构建](docs/依赖与构建.md)。

## 快速开始

详细的系统依赖、检测链路和 Agent 配置见 [文档索引](docs/README.md)。下面只保留最短启动路径。

### 1. 使用 uv 准备 Python 环境

```bash
uv sync --extra dev --extra model
./ssv --help
```

`uv sync` 会在项目根目录创建或同步 `.venv`，不需要手工创建或激活虚拟环境。执行模型导出时改用 `model-export` extra：

```bash
uv sync --extra dev --extra model-export
```

### 2. 准备配置和原始 ONNX

```bash
cp config/ssv.example.yaml config/ssv.yaml
```

修改 `sources[0].uri` 和 Redis 配置。默认示例已将 `inference.model.path` 指向社区发布的原始 `float32 [1,3,H,W]` NCHW ONNX；预处理语义必须在 `inference.model.preprocess` 中显式配置，不需要执行 `model prepare`。

默认示例模型可用下面的命令导出：

```bash
./ssv model export
```

已有自己的原始 ONNX 时可以跳过导出，直接修改 `inference.model.path`。模型接入和安全帽 `.pt` 离线验证见 [YOLO 推理链路说明](docs/yolo推理链路说明.md) 和 [安全帽模型验证说明](docs/安全帽模型验证说明.md)。

### 3. 构建并检查插件

```bash
./ssv build --profile cpu
./ssv inspect
```

`--profile` 可选 `auto`、`cpu`、`nvidia`、`intel`、`amd`；选择规则和依赖来源见 [依赖与构建](docs/依赖与构建.md)。

### 4. 启动 Redis、Qdrant 并运行

```bash
./ssv redis start
./ssv run --headless
```

有桌面会话时可运行：

```bash
./ssv run --display --overlay
```

Agent 是独立进程，需要时另开终端执行：

```bash
./ssv agent
```

## 常用命令

| 命令 | 用途 |
| --- | --- |
| `./ssv build [--profile PROFILE]` | 准备依赖并编译 runner、插件和测试 |
| `./ssv clean` | 清理 Meson 构建目录 |
| `./ssv run [RUNNER_ARGS]` | 启动 C++ 实时链路 |
| `./ssv inspect` | 检查 GStreamer 插件是否注册 |
| `./ssv test` | 编排 Python、C++、Agent 和契约测试 |
| `./ssv redis start\|stop` | 启动和停止本地 Docker Redis、Qdrant |
| `./ssv cache status\|clear` | 查看和清空 SSV 运行时缓存 |
| `./ssv agent [--config PATH]` | 启动 Python Agent |
| `./ssv model export` | 导出示例 YOLOv8n 原始 ONNX |
| `./ssv model manifest ...` | 从原始 ONNX 和 TensorRT engine 生成 schema v2 manifest |
| `./ssv model verify ...` | 验证安全帽 `.pt` 模型 |

`run` 的 `--display`、`--headless`、`--overlay` 和 `--display-backend` 参数只覆盖本次进程的显示设置，不会改写 YAML。

`./ssv cache status` 查看当前配置 Stream 的 entries、consumer group pending、Agent 去重 key，以及 EventLedger SQLite 的事件和 durable job 数量。`./ssv cache clear` 直接清空 Redis Stream、`ssv:agent:dedup:*` 去重 key 和 EventLedger SQLite 运行时表；`--dry-run` 只统计、不删除。清理前应先停止 `./ssv run` 和 `./ssv agent`，否则新事件可能立即重新写入。该命令不会删除 `agent/outputs`、Qdrant、DeerFlow checkpointer、其他 Redis key 或 Docker 容器。

Redis 与 SQLite 的清理分别执行，不提供跨存储原子事务。若命令返回失败，先根据输出确认两边已完成的范围，停止相关服务后重新执行 `./ssv cache clear`。

## 配置文件

实时 runner 的配置搜索顺序为：显式 `--config`、`SSV_CONFIG_PATH`、项目根 `ssv.yaml`、`config/ssv.yaml`、`/etc/ssv/ssv.yaml`。示例文件不会自动参与搜索。

常用临时环境变量只有 `SSV_CONFIG_PATH`、`SSV_RTSP_URL`、`REDIS_HOST`、`REDIS_PORT` 和 `GST_DEBUG`；运行参数、模型参数、显示参数和跟踪参数写入 YAML。完整字段说明见 [检测前端配置](docs/检测前端配置.md)。

## 测试

```bash
./ssv test
```

也可以按模块运行 `meson test -C build --print-errorlogs` 和：

```bash
cd agent
--extra dev pytest
```

真实 RTSP、GPU、显示、模型或外部 Agent provider 的结果取决于本机环境。

## 文档

- [文档索引](docs/README.md)
- [依赖与构建](docs/依赖与构建.md)
- [检测前端配置](docs/检测前端配置.md)
- [Agent 配置](docs/Agent配置.md)
- [YOLO 推理链路说明](docs/yolo推理链路说明.md)
- [安全帽模型验证说明](docs/安全帽模型验证说明.md)
- [系统设计](docs/specs/2026-05-21-安全帽佩戴视频监测分析系统设计.md)
- [Roadmap](docs/roadmap.md)

`docs/specs/` 保存需求和接口约束，`docs/plans/` 保存实施计划；跨模块改动应先更新对应 spec/plan。历史 spec/plan 保留当时的设计事实，不代表当前运行时契约。

## 目录概览

```text
runner/                 C++ 实时 runner
gst/                    GStreamer C++ 插件与共享模块
agent/                  Python Agent 与测试
config/                 YAML 模板和 label map
docker/                 Docker Compose 开发依赖
scripts/ssv_cli/        Python 统一 CLI、依赖 provider 和测试编排
docs/                   使用文档、设计文档、spec、plan 和 roadmap
```
