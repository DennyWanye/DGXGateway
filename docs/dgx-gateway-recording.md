# DGX 双机运行记录与 AgentOS NAS 日备份

2026-09-05 已启用于 Mac Gateway。独立后台线程采集，不依赖浏览器页面。NAS 使用 AgentOS 既有免密 SSH，未改家庭 SMB 或公网登录规则。

## 保存范围与频率

- 每 15 秒：双机在线/启动标识/运行时间、统一内存总量与可用量、GPU 利用率/功耗/温度、CPU load、磁盘余量、两条高速网络链路及错误计数、模型状态及操作 ID/阶段。
- 每 60 秒：完整 `/proc/meminfo`、`vmstat`、CPU 累计计数、内存/CPU/IO pressure、磁盘与网络累计 IO、进程名/PID/RSS/CPU、GPU 进程、Docker 容器实际状态、内核版本，以及 OOM/NVIDIA Xid/IO/热告警/断链等故障事件。无应用参数、环境变量、密钥、提示词、媒体正文。
- 内核读取上限 300 条/64 KiB，超限显式记录 `journal_truncated`；读取失败保留错误状态。故障事件带 journal cursor 可去重。不是每个进程/每条日志的无限量录屏，15 秒间隔也不保证捕捉到瞬时峰值。
- 常规样本先在内存缓冲，5 分钟或 4 MiB 批量 append + fsync；新内核故障会提前刷盘。SIGTERM 正常关闭会刷盘。突然断电/kill -9 最多可能丢失尚未落盘的约 5 分钟数据；不能同时承诺零写盘和断电零丢失。
- 采集、缓冲、压缩都在 Mac；DGX 仅执行只读探测。无高频 `sync`、SMART 扫盘或 NAS 逐样本写入。

## 每日备份

每天 UTC 日期切换（北京时间 08:00）封存前一日 JSONL，gzip level 3 顺序压缩一次，SSH 传送到 AgentOS `/srv/nas/@family/DGXGateway-Records/`。首次启动补传已有未备份的封存日期；关闭 Gateway 或 Mac 休眠期间不执行，下次启动补传。后台检查日期每分钟一次，不访问 NAS；失败每小时重试。

接收器检查 `/srv/nas` mountpoint、固定 UUID `705084e5-2b86-4687-a114-ce0895ab1eee` 和 `.nas-online`，不向脱落磁盘留下的系统盘目录回退。NAS 预留 5 GiB，单包最多 2 GiB。写 `.partial`、fsync、重新读取并校验 SHA-256、原子改名、同步目录，然后返回回执。同名同内容可重试；不同内容拒绝覆盖。NAS 保留本月和上个月两个自然月的归档。每月 1 日北京时间 08:00 的日备份前，按文件名日期清理上上个月及更早文件；首次启用或月初停机时在恢复后补做。只处理专用目录内合法日期的 `dgx-YYYY-MM-DD[-acceptance].jsonl.gz` 普通文件，跳过目录、符号链接及其他文件。清理计划与结果写入 NAS `retention-YYYY-MM.jsonl`，本地记录月度回执，成功后当月不再重复扫描；失败每小时重试。过期的本地待传文件不会重新上传 NAS，未获备份回执的本地文件仍不自动删除。小型清理审计记录保留，用于追溯删除操作。

本地保留至少 7 天（从校验回执开始计时）；仅删除已有 NAS 校验回执的原始日志与压缩包。NAS 离线保留本地待传数据；本地低于 512 MiB 时暂停新增落盘并返回错误，缓冲有上限，不删除未备份的数据来掩盖问题。

## 接口与位置

- `GET /api/v1/recording/status`：最近采样、落盘、备份状态及错误。
- `GET /api/v1/recording/recent`：最近 240 条；Gateway 重启从最新文件最多读取 4 MiB 恢复。
- 本地：`~/.local/state/dgx-cluster-gateway/recordings/`，按 UTC 日分文件。
- 历史全量：本地 JSONL 或 NAS `.jsonl.gz`。首页趋势图显示最近一小时，首次打开/返回页面补载最近历史；用时间裁剪并限制最多720点，横轴显示本地时刻，缺失时段留空。后台历史约15秒采样，页面实时约5秒更新，切到后台暂停轮询，历史补载至少间隔60秒。
- 配置：`~/.config/dgx-cluster-gateway/gateway.toml` 的 `[recording]`。
- AgentOS 接收器：`~/.local/lib/dgx-gateway/receive-recording.py`；Python 由 `~/.local/state/dgx-recording-python` 的 Nix GC root 固定。

## 验证和恢复

聚焦测试覆盖批量落盘、字段排除、低磁盘空间、压缩还原、失败保留、重复传输、校验错误、路径限制。真实双机采集+NAS传送证据在 `evidence/dgx-recording/`；`*-acceptance.jsonl.gz` 是验收样本，不冒充完整一天。每日定时分支由测试覆盖，首次自然日轮转需到下个北京时间 08:00。

恢复时先校验本地回执 SHA-256，再 `gzip -t` 检查并解压到独立目录，不覆盖当前记录。记录文件不是系统镜像，也不包含模型权重或 Gateway 认证凭据。

停用：将 `recording.enabled=false` 并通过 `DGXGateway/manage.sh stop/start` 重启 Gateway；保留已有记录与 NAS 文件。原配置备份为 `gateway.before-recording.toml`。无需重启 DGX 模型或 AgentOS 主应用。

### 2026-09-06 完整实机验收

已验证正式日志自然新增落盘 19 条；同一生产备份函数将 20 条真实双机样本压缩传至独立 NAS 验收目录，读回解压和 SHA-256 均通过；同一生产清理函数真实删除过期测试文件，并验证上个月/本月/无关文件哈希保持不变及删除审计落盘。详见 `evidence/dgx-recording/e2e-20260906/REPORT.md`。这是手动触发同一生产逻辑的验收，自然日/月定时仍按原配置运行。
