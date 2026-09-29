# DeepSeek V4.1 Flash — four Spark switchless Ring

请先阅读 `INSTALLATION.zh-CN.md`。本包提供四台单GPU DGX Spark的复现步骤，不在下载后自动执行。

默认模型是官方 native 混合精度，不是旧 Mia EXL3 Abliterated。保留旧模型、镜像、Gateway配置和物理接线备份。

流程：填写配置 → 维护授权 → 实际接线 → 临时IPv4/GID → 每边RoCE → 固定镜像 → 四rank NCCL Mesh → 固定权重hash → smoke32k → text256k → 可选1M。

本包不含权重、Docker镜像二进制、API密钥或第三方完整源码。`scripts/fetch_sources.sh`下载固定上游源码；Dockerfile只装配独立镜像，不修改宿主机Python。

配置模板：`config/cluster.example.json`。复制到`config/cluster.json`并填完所有FILL项，再执行。不同机器使用不同`--rank`参数，同一份JSON必须包含全部四节点。

本地检查：

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
bash -n scripts/fetch_sources.sh
```

37项本地测试通过不代表实际GPU/NCCL/模型验证。必须按正文保留全部现场验收证据。
