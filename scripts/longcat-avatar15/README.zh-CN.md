# LongCat Avatar 1.5 固定版本下载适配

范围：官方本地 INT8 主模型、8 步 DMD LoRA、Whisper 和 LongCat-Video 的
text encoder / tokenizer / VAE。不是“越狱”权重。

唯一清单是 [`plans/longcat-avatar15/download-plan.json`](../../plans/longcat-avatar15/download-plan.json)：
35 个文件，44,747,949,474 字节；两个 repo 都锁定完整提交 SHA。
安装范围和验收门槛见 `plan.zh-CN.md`（历史内部记录，公开版不包含），
原始现场要求见 `AGENTS.md`（历史内部记录，公开版不包含）。

## 操作命令

在 Mac 的项目根运行；默认 `--interface en1` 只影响 Mac 的 HF 控制请求。
接口不匹配时先检查本机路由，再明确传入正确接口或 `--interface ''`。

```bash
python3 scripts/longcat-avatar15/download-queue.py validate-plan
python3 scripts/longcat-avatar15/test_downloads.py
python3 scripts/longcat-avatar15/download-queue.py metadata
python3 scripts/longcat-avatar15/download-queue.py download --small-only
python3 scripts/longcat-avatar15/download-queue.py verify --small-only
```

`metadata` 只读取 Mac 端 HF API，并将公开元数据写入
`logs/longcat-avatar15/metadata.json`。`download --small-only` 在 head 创建/同步
模型专属控制脚本，并下载、两端核验 21 个普通 Git blobs；不下载任何 LFS 权重。
完整模型下载必须明确执行：

```bash
python3 scripts/longcat-avatar15/download-queue.py download --limit 32M
python3 scripts/longcat-avatar15/download-queue.py verify
```

完整下载会重新检查每个已存在文件的实际大小和内容哈希，校验通过才跳过。
`verify` 会同步控制脚本/清单，然后在 head 离线读取权重做完整核验；不会拉取权重。
首次控制器运行需要已有 `aria2c`、`python3`、`unshare`、`flock` 和已配置的 UID 996，
脚本不会替你安装系统软件或修改系统网络。

## 文件布局和日志

```text
/home/operator/dgx-models/longcat-avatar15/
  control/{download-artifact-remote.sh,verify-downloads.py,download-plan.json}
  weights/LongCat-Video-Avatar-1.5/{base_model_int8,lora,scheduler,whisper-large-v3}/
  weights/LongCat-Video/{text_encoder,tokenizer,vae}/
  weights/.download/longcat-NN.part[.aria2]
  download-logs/longcat-NN.{log,status}
```

两个模型目录并列。普通 Git blobs 在 Mac 下载并验证 Git blob SHA-1，再传入
head 相邻临时文件并重新验证；LFS 文件的 Mac 只请求签名 URL，实际 payload
由 head `dgx-hf-direct`（强制 UID 996）下载，默认限速 32 MiB/s。
可用 `--limit 16M` 等覆盖单进程速率，启动日志记录实际值；不修改全局网络策略。

每次启动/恢复 payload 都核实 `dgx-head` / `operator` 身份和两条实际路由：
UID 996 必须走 `wlP9s9`，`operator` 必须走 `dgx-vpn`。CDN DNS 复用
`doh.pub` 直连基线，只接受公网 IPv4；挂载独立 `/etc/hosts` 的操作只发生在
下载进程的私有 mount namespace，退出即释放。

临时权重和最终目录必须位于同一文件系统；大小和 SHA-256 全部通过后，用
`link()` 原子发布完整 inode，再移除临时名。若 final 同时被他人创建，发布报错，
不会覆盖已有文件。对 Git blobs 同样先验证后原子发布。

## 暂停和恢复

- 不读取、复制或传输任何 HF token。签名 URL 通过 SSH stdin 写入 head `/run`
  的 0600 控制文件；不进入命令行或公开日志。原始 aria2 日志保持 0600，退出时删除，
  对外仅保留错误码和文件 ID。
- 元数据/URL HTTP 最多 3 次尝试；payload 最多 3 次独立尝试。
  任一 401/403 会立刻硬暂停，退出码 3；不自动刷新重试权限错误。
- 断开 Mac 控制器不会自动杀死已启动的远端下载；重新运行前若 worker 仍持有锁，
  会明确暂停，避免两个下载者同时写文件。
- 单文件控制器观察窗口为 2 小时。到期暂停控制器并要求检查现场；不自动启动
  第二个 worker，也不会据此宣称远端已停止。
- `.part` / `.aria2` 支持续传。已完成 `.part` 若无 aria2 状态且大小匹配，必须先
  通过哈希才发布；发现完整但损坏的暂存文件会停住，保留现场，不自动删除重下。
- 已有最终文件若校验失败，立即停止，不覆盖或删除。应调查原因后再决定处理。

## 测试与尚未覆盖的内容

`test_downloads.py` 是本地 fixture / mock 自动化检查，不是真实 head 下载或模型推理。
覆盖：35 文件清单计数/总字节、Git/SHA256、截断/篡改/符号链接拒绝、固定上游
revision/size/hash、401/403 零重试、503 有界重试、URL 参数保密及既有文件损坏硬停。
远端 mount namespace、实际直连 CDN、断线恢复、大权重完整性和模型推理须由真实
执行回执证明。该子任务编写阶段未下载任何大权重，未修改远端或 Gateway。

2026-09-09 编写阶段实测：7 个本地 fixture 测试全部通过，`bash -n` 通过；
Mac 对两个官方 repo 的固定 revision API 元数据核验通过全部 35 个文件的
大小 / Git blob ID / LFS SHA-256；额外真实下载并验证了 853 字节
`base_model_int8/config.json` 的大小和 Git blob SHA-1，临时文件随后清理。
公开元数据回执位于 `logs/longcat-avatar15/metadata.json`。这些结果不包含远端
payload、模型加载或推理验收。
