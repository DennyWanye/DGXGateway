# DGX Gateway

面向 DGX Spark 集群的模型控制面、MCP 服务与创作工作台。支持模型生命周期、互斥调度、持久任务、原编号恢复、图片/视频/文本任务、短剧素材与候选管理。

本仓库是 **2026-09-29 当前工作区的脱敏源码快照**，包括尚未进入旧私有仓库的 Gateway 与工作台代码。它不是生产数据备份，也不携带旧仓库历史。所有地址和账户均为示例，克隆后不能原样控制任何真实机器。

## 从这里开始

1. [安装 Gateway 与工作台](docs/INSTALL.zh-CN.md)
2. [模型总览、精度、安装入口和验证范围](docs/models/README.zh-CN.md)
3. [全部模型来源、固定版本与下载清单](docs/models/DOWNLOADS.zh-CN.md)
4. [四节点 H3 / FP8 / PDD8 / EasyCache](docs/models/H3-RING4.zh-CN.md)
5. [DeepSeek 与历史 Qwen 文本模型](docs/models/TEXT.zh-CN.md)
6. [ComfyUI 图像、音乐与其他视频模型](docs/models/COMFYUI.zh-CN.md)
7. [LongCat 与 PersonaPlex](docs/models/AVATAR-AUDIO.zh-CN.md)
8. [隐私处理与安全边界](SECURITY.md)、[第三方来源](THIRD_PARTY_NOTICES.md)、[发布检查](docs/PUBLICATION.zh-CN.md)

## 目录

| 目录 | 内容 |
|---|---|
| `DGXGateway/` | Python 控制面、持久任务、MCP、Web UI、drama 业务服务 |
| `ComfyUI/custom_nodes/dgx_remote_h3/` | Gateway 连接节点、React 工作台源码及现有构建产物 |
| `scripts/` | 下载、验证、运行时适配、锁、MCP 客户端、文档生成器 |
| `manifests/` | 模型 repo/revision/path、权重摘要与下载队列；不含权重 |
| `deployments/` | 四节点 DeepSeek Ring 安装包源码与示例配置 |
| `plans/MiniMaxH34DGX/h3-ring-kit-v2/` | H3 Ring4 Dockerfile、渲染器、适配补丁和示例配置 |
| `plans/h3-ring4-performance/` | 仅保留 PDD/EasyCache 构建所需源码与补丁，不含私人生成记录 |
| `tests/` | 现有测试源码；现场 GPU 测试须在自己的硬件上另行执行 |

保留原相对目录是为了让现有导入、运行脚本与构建器继续找到依赖；`plans/` 在公开版里仅包含白名单部署输入，不是完整内部工作日志。

## 当前支持与限制

Gateway 当前目录包含 H3 Ring4、DeepSeek V4.1 Ring4、Mia 双机文本、LongCat、ComfyUI、PersonaPlex，以及历史 H3 双机适配。历史适配和下载清单存在不表示它们是推荐路线或已经实测。

H3 当前生产组合是 **4节点 Ring4 + 官方 Ref2VA FP8 + FA 补丁 + PDD8（可选 EasyCache）**。近期两个8秒对照任务分别为约26分02秒/23分11秒，仅是特定素材的实测，不是通用速度保证。保留原始质量与配置回退开关。

本仓库尚未经过一套全新四机环境从零安装验收。阅读每个安装指南中的前置条件、现场适配项和已知缺口；不要将公开示例里的文档地址写进现有网卡配置。

公开源码不等于把推理端口公开到互联网。发布版示例默认回环监听、开启鉴权、关闭变更权限；部署者完成配置和权限核验后才能启用控制操作。
