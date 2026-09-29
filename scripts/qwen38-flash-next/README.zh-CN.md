# Qwen3.8 主选权重下载

固定模型：`gorbatjovy/qwen3.8-flash-next-abliterated-NVFP4-plefp8`，revision `2065365912e46b205c64a70ac5b85b4869674d31`。

执行清单：[download-plan.json](../../plans/qwen38-flash-next/download-plan.json)，29 文件、135,234,133,076 字节。其中 17 个 safetensors 权重，另有配置、模板、tokenizer、索引、许可证与说明。10 个普通 Git blobs 由 Mac 取回验证后复制；19 个 LFS 文件通过 head UID 996 直连下载。

适配自本项目已经实际使用的 `scripts/longcat-avatar15/` 下载器。隔离修改仅涉及模型路径、repo/revision、总大小、文件数量、锁与临时文件前缀；未修改原下载器。保留小文件 Git SHA1、大文件 SHA256、401/403 硬暂停、有限网络重试、`.part` 续传及校验后原子发布。不会启动模型或变更网络。

```bash
python3 scripts/qwen38-flash-next/download-queue.py validate-plan
python3 scripts/qwen38-flash-next/test_downloads.py
python3 scripts/qwen38-flash-next/download-queue.py metadata
python3 scripts/qwen38-flash-next/download-queue.py download --limit 32M
python3 scripts/qwen38-flash-next/download-queue.py verify
```

恢复前先看 `logs/qwen38-flash-next/active-download.json` 的进程和日志。若控制器仍在运行，不启动第二个；若本地控制器退出但远端 worker 仍持锁，先检查该 worker，不覆盖控制脚本。正常恢复使用同一 `download` 命令，已完成文件重新校验后跳过，分片续传。

远端目录：`/home/operator/dgx-models/qwen38-flash-next/weights/qwen3.8-flash-next-abliterated-NVFP4-plefp8/`。

本地公开日志：`logs/qwen38-flash-next/download-*.log`；远端公开日志：`/home/operator/dgx-models/qwen38-flash-next/download-logs/`。禁止打印 `/run` 中临时 URL 或原始 transfer 日志，里面可能有签名参数。

首次启动使用 `caffeinate -i` 随下载控制器存活，避免 Mac 空闲睡眠中断控制队列；不会改系统睡眠设置。关机、断网或合盖仍可能影响下一文件调度，已经启动的远端分片下载可独立继续。

2026-09-09 检查：7 个本地 fixture/mock 测试通过、bash 语法检查通过，真实 HF 固定 revision 的全部 29 文件元数据核验通过。模拟 HTTP 503 是单元测试输入，不是实际下载出错。自审确认路径前缀与身份隔离、签名 URL 不进入 argv/公开日志、损坏文件不会被静默覆盖。这些检查不能替代真实权重下载验收。

首次执行修正：Mac 跟随 LFS 重定向去探测 CDN 会失败，而直接读取官方 HF 的 302 签名重定向成功。因此本 adapter 的 HEAD 只取官方重定向，验证 HTTPS 与 CDN 域名后交给 head UID 996 连接；实际 CDN 的 401/403 仍由 remote worker 硬暂停。新增对应模拟测试后共 8 项通过，未改变其他下载器。22:11 已启动首个真实权重分片。

22:35 修正：瞬时 SSH 状态读取失败不能当作 worker 未启动。已经观察到 RUNNING 后继续有界等待，SSH exit 255 同样保留原 worker，不重复启动。新增 RUNNING → SSH 255 → PASS 模拟用例，共 9 项通过。此前首个 10.003 GB 权重已真实下载并校验，第二分片因传输失败保留 `.part/.aria2` 后续传。
