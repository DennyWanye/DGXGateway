# MiniMax H3：4×DGX Spark 无交换机 Ring4 四 Profile 部署计划 V2

**修订日期：2026-09-18**  
**目标：先把四台机器共同生成一个视频的链路做成可重复、可恢复、可日常使用的服务，再逐步增加 Ref2VA 和 Heretic。**  
**交付对象：在用户本地机器上执行的 Agent／工程人员。**

> 本文在原《MiniMax H3 四台 Spark 无交换机 Ring 实验部署方案》和《先跑通、再好用执行补充》基础上修订，不是另选技术路线。四机共同生成仍是固定目标；单机、双机只作为定位问题的检查点。
>
> **本文、配置和脚本已完成本地静态／模拟测试，但未在用户的四台 GB10 上构建镜像、运行 FA、验证 RDMA 或生成 H3 视频。** 模型格式支持、配置检查通过、四机真正出片是不同证据层次。不要把本交付当作已验收安装记录。

---

## 0. 执行结论与阅读顺序

执行主线固定为：

```text
复用现网与已有资源
  → 校验同一运行时的 FA/LSE 与通信
  → P0：FL2VA 官方来源版，四机正常出片
  → P1：Ref2VA 官方来源版，单参考到多模态参考
  → P2：Ref2VA + BF16 Heretic 编码器，逐项 A/B 验收
  → 固定已经通过的主力 Profile、单任务队列与回退方法
  → P3：Hybrid 实验版；不阻塞前面三项交付
```

**预期主力候选是 P2；稳定回退是 P1，基础回退是 P0。** 只有在 P2 的角色一致性、视觉理解、声音和运行稳定性不出现不可接受退化时，才把它设为日常默认。不是因为模型名带 `Heretic` 就自动替代官方版。

开始执行前先读第 1–5 节。已有 V1 环境按第 4 节迁移，不要从头重装。权重准备看第 6 节；素材看第 7 节；四机启动看第 8 节；各 Profile 的任务看第 9–12 节；队列、切换与恢复看第 13–15 节。

### 0.1 文件优先级

1. **本 V2 主文档和 V2 新脚本**决定四 Profile、运行入口、执行顺序与验收。
2. `reference/` 下原安装文档保留为网络、镜像、内核／通信测试与历史设计参考；其“仅官方 FL2VA”的阶段限制已被本计划扩展。
3. 原 `scripts/run_node.sh` 继续用于 `kernel`、`comm`、`help`；**日常启动改为 `scripts/profile_node.py`，不要同时使用两套 serve 入口。**
4. `reference/` 下“先好用”补充中的保护、可恢复和四机目标仍适用；本文细化了队列实现、输入文件与 Profile 切换。

交付目录不含模型权重、用户凭据、真实主机地址。配置里的 `CHANGE_ME` 必须由现场读取的值替换。所有命令的主机、目录和 supervisor 均先核对再执行。

---

## 1. 四个 Profile 到底装什么

| 阶段／ID | 生成主体 | 编码器 | 用途与运行时策略 | 默认地位 |
|---|---|---|---|---|
| P0 `fl2va-official` | 官方 H3 `FL2VA/transformer` | 官方 H3 Qwen3-VL 组件 | T2VA、首帧、尾帧、首尾帧；DiT 在线 FP8 候选 | 先验收；保留 |
| P1 `ref2va-official` | 官方 H3 `Ref2VA/transformer` | 官方 H3 Qwen3-VL 组件 | 图片、视频、音频参考；DiT 在线 FP8 候选 | 官方对照／首选回退 |
| P2 `ref2va-heretic` | 与 P1 相同 | 兼容架构的 BF16 Heretic Qwen3-VL-32B | 只改变条件编码器；其余尽量与 P1 一致 | 高服从候选，验收后再提升 |
| P3 `hybrid-experimental` | Singularity v1.3 完整 INT8 Hybrid 文件 | **先用官方编码器** | T2VA／FL2VA／Ref2VA 实验；预量化权重不再在线量化 | 默认禁止启动，需要显式实验参数 |

P0/P1 的“官方”指**权重来源和未作 Heretic／微调替换的组件**，不意味着此 FP8 推理配方等同于完整原精度。文档、日志与 UI 建议显示 `官方来源 + DiT FP8`，而不是“满血无损”。

### 1.1 对此前建议的必要校正

**Heretic 不等于整个 H3 被“越狱”。** 本阶段替换的是同时参与文字与图像理解的条件编码器；DiT、视频 VAE、音频 VAE 仍使用官方组件。更少拒绝、更高服从性和画质不下降，均需要具体测试，不能从名称推出。[S1][S2][S3]

**Hybrid 不等于 uncensored。** 它描述一份生成主体可以覆盖多个任务的能力。作者微调目标、权重合并方式和内容倾向需要单独确认；本计划不会把 P3 自动标为“越狱版”。

**P3 第一轮不同时换 Heretic。** 先以官方编码器验证 Hybrid，本身通过后才另建“Hybrid + Heretic”实验修订。V2 的 P3 校验刻意阻止同时启用两种覆盖，避免一次引入两个无法归因的变量。

**不要先用 NVFP4 Heretic。** 本次选择上游明确列出的 BF16 兼容模型做 P1/P2 对照。NVFP4、INT8 编码器需要额外量化内核与元数据路径，是下一轮独立优化，不混入本轮首次排障。[S2]

---

## 2. 已核对的边界：支持什么，不证明什么

### 2.1 固定运行时与并行方式

```text
SGLang 源码：e970453b433e1467e22f6d925f270563417540b5
4 nodes × 1 local GPU/node
TP=1，Ulysses=1，Ring=4，SP=4
encoder-parallel=replicate
attention-backend=fa
eager；不启用 FSDP、torch.compile、Turbo、稀疏注意力与近似缓存
```

使用四个 rank 处理同一作业。**不是 DeepSeek 的 TP4，也不是四个独立视频实例。** 本次固定源码的 H3 Ring 校验要求 FA；不能用 cuDNN 替换后仍把结果当成这个已审查组合。[S4]

### 2.2 内存与 placement

Ring4 不把权重自动平均分成四份，也不提供透明的 512GB 显存池。编码器复制、VAE、临时量化和工作区都可能形成单机峰值；CPU/GPU 在 Spark 上共享统一内存。[S4][S8]

**新建、尚未验收的四份 Profile 模板采用 `performance_mode=auto`。** 这是对原 V1 强制 `speed` 的调整：允许当前框架选择受支持的驻留／offload 策略，而不是在首次装载时强制 resident。固定源码把部分自动逐层卸载策略限定在 `auto`／`memory` 模式；这不是切到 `auto` 就必然适配或更快的保证。[S4]

已有 `speed` 配置若确实通过，不为迁移盲目改变它：把 P0/P1/P2 的 `performance_mode` 保持为相同已验收值，四台同步并记账。P1 与 P2 比较中若 placement 不同，应分开报告，不把差异全归因于 Heretic。

### 2.3 请求和素材语义

FL2VA 约束首／尾关键帧，Ref2VA 接受多模态参考；参考不等于逐像素锁定、确定的动作复制或必然保持同一人。先用单个简单镜头，不从大量混合输入和长片开始。[S1]

V2 正式基线：**请求 4 秒、短边 768、24 FPS、带音频、`num_inference_steps=50`**。该参数在上游 H3 路径表示含终点的 sigma 网格点数，实际去噪调用次数应读日志；不要一概写成 50 次 NFE。`5` 仅作链路冒烟，不是已蒸馏的五步质量版。[S2][S7]

`quality="lossless"` 是框架策略值，**不会抵消 FP8、INT8、微调或剪枝造成的变化**。不要切成 `quality="high"`；固定版本该策略只审计了特定 H200 配方。[S4]

### 2.4 三层证据必须独立保存

| 层次 | 验证内容 | 不足以证明的内容 |
|---|---|---|
| 静态与字节一致 | 配置、锁定 revision、文件哈希、四个不同 rank | 模型已正确加载、RDMA 可用 |
| 四机功能 | 同一任务由四 rank 执行，音视频完整输出 | 高速网络已使用、长时间稳定 |
| 使用与传输 | 真实作业的网络证据、连续任务、恢复、人工画质检查 | 所有输入规模、所有 Profile 都通过 |

不要在报告中把 `PASS_OFFLINE_MATCH_ONLY` 改写成“部署成功”。

---

## 3. 目录、角色与配置

### 3.1 目录结构

```text
~/h3-ring-kit-v2/                   # 本次代码、配置、提示词、测试
  config/cluster.json              # 四台同一逻辑配置
  config/node.json                 # 每台不同的 rank、网卡与 HCA
  profiles/*.json                  # 四个 Profile；四台保持一致
  scripts/
  prompts/
  reference/                      # 原两份说明，不改动旧环境

/srv/h3-ring/
  models/MiniMax-H3/               # 保留并增量补齐官方 FL2VA/Ref2VA
  models/encoders/Heretic-Qwen3VL32B/
  models/hybrid/Singularity-v1.3/
  manifests/                      # locks、下载回执、输入与四机检查结果
  assets/acceptance-v1/            # 专用测试素材；只读挂载
  cache/
  runtime/<deployment-id>/
  logs/<deployment-id>/
  queue/                          # 仅 S0 的 SQLite 持久化队列
  acceptance/<阶段-日期>/          # 各 Profile 的实际验收
```

容器内固定映射：官方模型 `/models/h3`，Heretic `/models/heretic`，Hybrid `/models/hybrid`，**素材 `/data/h3`**。这些绑定为只读。生成运行目录与缓存独立可写。

### 3.2 配置的责任边界

`cluster.json` 记录真实模型目录、镜像名及其 image ID、head 控制 IP、端口。`node.json` 记录当前机器 rank、控制网卡、`ib`／`socket`、实际 HCA。Profile 只控制任务族、组件覆盖和量化／placement。

默认 API `127.0.0.1:30021`；分布式初始化端口 `20021`。先检查占用。分布式服务的工作节点端口／NCCL 控制连接也必须在可信网络内互通，不能只放行 API 端口就认为多机网络已配置。

**四台使用同一 deployment ID，不要各自运行 `date` 得到四个不同 ID。** 例如本轮统一使用 `p0-acceptance-001`；下次重启、切换或配置变化用新 ID。

---

## 4. 从旧 Plan 迁移，不破坏已经完成的部分

### 4.1 先保留旧目录

把新包放入 `~/h3-ring-kit-v2`，旧 `~/h3-ring-kit` 保留。不要解压覆盖旧目录。Markdown 单文件恢复方法见末尾附录。

四台分别执行以下基础准备；目录所有者按现场实际部署用户决定：

```bash
set -Eeuo pipefail
cd "$HOME/h3-ring-kit-v2"
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /srv/h3-ring
mkdir -p /srv/h3-ring/{manifests,assets,acceptance,cache,logs,runtime}
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

测试环境至少为 Python 3.10+；媒体测试需要 ffmpeg／ffprobe。没有这些工具时不要把被跳过的测试记为通过。GPU 部分另按第 5 节执行。

### 4.2 已有经过审核的 V1 配置

每台分别在新目录执行：

```bash
bash scripts/import_v1_config.sh "$HOME/h3-ring-kit"
```

此脚本会读取**本机经过审核的旧 shell `.env`**，只新建 `config/cluster.json` 和 `config/node.json`；已有目标文件会报错，不覆盖。不要对网上下载的、不可信的 `.env` 执行该导入，因为 shell 配置本身可执行代码。

导入后检查：

```bash
python3 -m json.tool config/cluster.json
python3 -m json.tool config/node.json
```

它不会自动迁移所有自定义网络插件、额外挂载或 supervisor 逻辑，也不会自动照搬旧 `performance-mode`。这些必须按旧日志和镜像内容逐项核对。**自定义 NCCL 插件依赖不能靠把 `NETWORK_MODE` 改成 `ib` 自动替代。**

### 4.3 尚未建立 V1 环境

按 `reference/MiniMax_H3_4x_DGX_Spark_Switchless_Ring_Experimental_CN.md` 的 G0–G4 完成主机盘点、独立镜像、FA/LSE 和通信。在新目录建立原 `cluster.env`／`node.env` 供诊断入口使用，审核后运行：

```bash
bash scripts/import_v1_config.sh "$PWD"
```

也可从 JSON example 独立创建配置，但不能凭聊天内容猜主机地址、HCA 或当前驱动版本。旧诊断脚本仍使用旧 `.env`，新启动脚本只使用 JSON；**改动一套后，另一套不会自动同步**。

### 4.4 必须保留的现网资料

读取并保存四台：GPU／驱动／系统、真实连接顺序、IP 路由、网卡与 RoCE 对应、原 NCCL 日志及插件、镜像 ID、模型目录、Gateway／supervisor 启停入口。网络盘点方法沿用原 G0。

停服、修改网络、安装额外系统组件前确认操作授权和恢复方法。本文件只授权本次设计，不等于远程连接或任意改动主机的授权。

**不做：** 删除 DeepSeek／Qwen、清空 HF cache、`docker system prune -a`、批量杀 Python、重装驱动、清空防火墙、覆盖 netplan、把现有配置切成四份独立推理。

---

## 5. 运行时与通信先通过，再扩大下载

### 5.1 基础镜像复用

如果已有镜像满足固定源码、ARM64、依赖一致性与 FA 测试，直接复用并记录真实 image ID。否则按原 Dockerfile 构建一次，由 S0 分发同一镜像，不让四台分别追随浮动依赖安装。

已有镜像可只读检查：

```bash
docker image inspect YOUR_ACTUAL_IMAGE --format '{{.Id}} {{.Architecture}}'
docker run --rm --network none YOUR_ACTUAL_IMAGE \
  python -c 'import torch; print(torch.__version__, torch.version.cuda)'
docker run --rm --network none YOUR_ACTUAL_IMAGE \
  git -C /opt/sglang-src rev-parse HEAD
```

`YOUR_ACTUAL_IMAGE` 必须替换。最后一条路径来自本套 Dockerfile，其他已验收镜像按它自己的实际源码路径检查。配置文件写着某个 SHA 不是镜像内部真的使用该 SHA 的证据。

### 5.2 诊断顺序

在原来已经正确配置 `.env` 的 kit 目录执行，或者在新目录审核建立同等诊断配置后执行：

```bash
# 每台分别：
bash scripts/run_node.sh kernel

# 相邻两台，rank 为 0/1，分别执行：
bash scripts/run_node.sh comm 2 ring
bash scripts/run_node.sh comm 2 collectives

# 四台 rank 0/1/2/3，每一组命令四台共同执行并结束后再做下一组：
bash scripts/run_node.sh comm 4 ring
bash scripts/run_node.sh comm 4 collectives
bash scripts/run_node.sh comm 4 allpairs
```

FA 测试必须验证 packed-varlen 与 LSE 的数值，不仅仅 `import torch`。通信测试是小规模正确性诊断，不是线速证明；正式 H3 仍需真实 shape 的通信和内存证据。

物理环相邻 P2P 通，不表示所有 collective 或非邻居控制通信都通。`NCCL_ALGO=Ring` 不能代替路由、HCA／GID 映射或所需网络插件。[S8][S9]

TCP 对照允许用于定位计算问题，但分别记为 `PASS_TCP_FUNCTIONAL`；正式 RDMA 目标必须有本次 H3 作业的真实日志和设备计数，不允许静默降级。

### 5.3 内存故障不要混为一谈

权重／必需组件装载失败，先修复该节点装载路径；不要用“4×128GB”跳过。单机高分辨率激活峰值 OOM 与装载失败不同，保留单机低负载通过证据后可以受控验证 Ring2／Ring4。每次只改一个已定位参数并重新记录峰值，不能同时开启多个优化掩盖问题。

---

## 6. 模型下载、复用与固定版本

### 6.1 固定候选资源

| 资产 ID | 仓库／文件 | V2 下载策略 |
|---|---|---|
| `official-fl` | `MiniMaxAI/MiniMax-H3` | 公共根索引 + `FL2VA/` 原始分区 |
| `official-ref` | 同一官方仓库、同一 revision | 公共根索引 + `Ref2VA/` 原始分区 |
| `encoder-heretic` | `llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic` | BF16 safetensors、分片索引、config；不替换官方 tokenizer |
| `hybrid-singularity` | `WarmBloodAban/Minimax-h3_Singularity` | 只选 `Minimax-h3_Singularity_ref2va_v1.3_int8.safetensors` 和说明文件 |

选定 Hybrid 是完整 INT8 文件，不是 Pruned 同名变体，也不是 W4A8／Turbo。仓库中名称相近的其他文件不得随意替换。[S5]

下载脚本先解析仓库为完整 commit SHA，锁定文件列表，再按固定 SHA 获取。**没有给 Hugging Face 模型编造一个“已验证版本号”**；第一次在线计划解析到的 revision 必须随验收记录保存。已安装官方版本优先使用旧 lock，不追最新版。[S10]

### 6.2 下载工具环境

优先复用原 `/srv/h3-ring/download-venv`。确实不存在时才建立：

```bash
python3 -m venv /srv/h3-ring/download-venv
/srv/h3-ring/download-venv/bin/python -m pip install huggingface_hub
/srv/h3-ring/download-venv/bin/python -m pip freeze \
  > /srv/h3-ring/manifests/download-tool.freeze.txt
```

下面 `PYHF` 指这个解释器。认证按 Hugging Face 官方工具保存，不把 token 写进 Profile、命令历史、镜像层或日志。执行前确认当前联网出口；脚本不替你绕过代理／VPN，不保证特定流量路径。

### 6.3 P0：复用原 FL2VA revision

S0 执行：

```bash
set -Eeuo pipefail
cd "$HOME/h3-ring-kit-v2"
PYHF=/srv/h3-ring/download-venv/bin/python
M=/srv/h3-ring/manifests
BASE=/srv/h3-ring/models/MiniMax-H3

# 已有原计划的 lock 时使用这一条；文件路径以现场存在的旧 lock 为准。
"$PYHF" scripts/assets_v2.py plan official-fl \
  --lock "$M/official-fl.lock.json" \
  --same-revision-as "$M/fl2va-model-lock.json"
```

从未下载、没有旧 lock 时，改用一次性首次计划：

```bash
"$PYHF" scripts/assets_v2.py plan official-fl \
  --lock "$M/official-fl.lock.json"
```

以上两种择一，不为文件不存在而胡乱伪造旧 lock。先阅读新 lock 中的 repo、SHA、文件大小，再执行：

```bash
"$PYHF" scripts/assets_v2.py download \
  --lock "$M/official-fl.lock.json" --dest "$BASE" \
  --receipt "$M/official-fl.receipt.json"
```

已有文件会与上游固定版本的内容哈希核对；一致则不重新下载。不一致就报错，不能静默覆盖旧权重。确需迁移新 revision 时使用新的目录和配置修订，保留旧快照。

### 6.4 P1：增量下载 Ref2VA，不换官方版本

P0 所需检查通过后，S0 执行：

```bash
"$PYHF" scripts/assets_v2.py plan official-ref \
  --lock "$M/official-ref.lock.json" \
  --same-revision-as "$M/official-fl.lock.json"

"$PYHF" scripts/assets_v2.py download \
  --lock "$M/official-ref.lock.json" --dest "$BASE" \
  --receipt "$M/official-ref.receipt.json" --reuse-official
```

`--reuse-official` 只在 Ref 文件与对应 FL 文件的**上游内容哈希完全一致**时，复制本地文件而不重复走外网。不会因为文件名相同就复用 transformer。使用独立 inode，避免后续改动一份意外影响另一份；因此减少外网下载不等于减少同等磁盘占用。

本计划下载原始 `FL2VA/` 和 `Ref2VA/` 布局，不同时下载顶层 Diffusers `transformer/`、`transformer_ref/` 的整套重复表示。[S1]

### 6.5 P2：只增加 BF16 Heretic 编码器

```bash
"$PYHF" scripts/assets_v2.py plan encoder-heretic \
  --lock "$M/encoder-heretic.lock.json"

"$PYHF" scripts/assets_v2.py download \
  --lock "$M/encoder-heretic.lock.json" \
  --dest /srv/h3-ring/models/encoders/Heretic-Qwen3VL32B \
  --receipt "$M/encoder-heretic.receipt.json"
```

首次计划不下载大权重。先在第 11 节核对模型 config，再决定完成下载；CPU 配置检查完整权重阶段还会验证 shard 是否齐全。

### 6.6 P3：通过前三阶段后才下载 Hybrid

```bash
"$PYHF" scripts/assets_v2.py plan hybrid-singularity \
  --lock "$M/hybrid-singularity.lock.json"

"$PYHF" scripts/assets_v2.py download \
  --lock "$M/hybrid-singularity.lock.json" \
  --dest /srv/h3-ring/models/hybrid/Singularity-v1.3 \
  --receipt "$M/hybrid-singularity.receipt.json"
```

先检查作者说明和适用许可，不因为第三方仓库显示某个 license 标签，就认定它可以覆盖基座模型的全部条款。这里不作许可证法律兼容性的结论。

### 6.7 分发到其他三台

只在 S0 下载外网权重，其他三台通过已有可信内网复制。以下 `h3-s1` 是你自己配置并核对的 SSH 别名，**不是脚本能自动发现的机器**。先用同样方法为 S2/S3建立实际别名或替换真实用户和控制 IP。

```bash
# 首先在对端创建相同目标目录，并确认有空间与写权限。
ssh h3-s1 'mkdir -p /srv/h3-ring/models/MiniMax-H3 /srv/h3-ring/manifests'

# 不删除对端文件；已有文件不覆盖，之后哈希检查会报告不同内容。
rsync -av --copy-links --ignore-existing --partial --progress --exclude=.cache \
  /srv/h3-ring/models/MiniMax-H3/ \
  h3-s1:/srv/h3-ring/models/MiniMax-H3/

rsync -av --ignore-existing \
  "$M/official-fl.lock.json" "$M/official-fl.receipt.json" \
  h3-s1:/srv/h3-ring/manifests/
```

P1／P2／P3按实际新增资产逐个同步相应 lock、receipt 和目录，S2、S3重复。**不要把 S0 的 `config/node.json` 同步覆盖到其他机器。** 若 `--ignore-existing` 保留了错误旧文件，哈希失败后在新快照目录修正，不直接增加 `--delete` 或全量强制覆盖。

每个接收节点执行，例如 P1：

```bash
python3 scripts/assets_v2.py verify \
  --dest /srv/h3-ring/models/MiniMax-H3 \
  --receipt /srv/h3-ring/manifests/official-ref.receipt.json
```

P2还验证 `encoder-heretic.receipt.json`；P3验证 Hybrid。锁定后的模型目录视为不可变，正式任务期间不复制／修改权重，不与测速并发执行全量哈希。

---

## 7. 素材准备：四台同路径、同内容，不暴露公网

### 7.1 第一组素材

使用用户有权处理的成人角色和授权声音，不需要网上下载示例来替代自己的镜头。第一组放在：

```text
/srv/h3-ring/assets/acceptance-v1/
  hero-front.jpg
  hero-side.jpg
  hero-halfbody.jpg
  scene.jpg
  start.jpg
  end.jpg
  motion-silent.mp4
  voice.wav
  rights-and-notes.txt
```

前三张应是同一角色，服装一致；scene 只说明空间和光线。动作视频先选一个短的简单动作，避免一开始就测试奔跑、复杂手部与多人遮挡。

**素材不是本 ZIP 提供的文件。** 本地执行者从用户已有授权素材选择并填写；请求生成器遇到缺文件会明确失败，不把占位地址发给模型。

### 7.2 先核对时长与音轨

```bash
ffprobe -v error -show_streams -show_format -of json \
  /srv/h3-ring/assets/acceptance-v1/motion-silent.mp4
ffprobe -v error -show_streams -show_format -of json \
  /srv/h3-ring/assets/acceptance-v1/voice.wav
```

原始参考视频若含声音，而当前只测动作，应另存无音轨副本；不覆盖原文件：

```bash
ffmpeg -nostdin -n -i /absolute/path/to/authorized-motion.mp4 \
  -t 4 -an -c:v libx264 -pix_fmt yuv420p \
  /srv/h3-ring/assets/acceptance-v1/motion-silent.mp4
```

确保原输入至少包含所需时长。初轮单段参考取 2–4 秒。模型卡上视频和音频单段均为 2–15 秒，视频类别总时长≤15秒、音频类别总时长≤15秒；图像≤9、视频≤3、音频≤3、混合≤12。带音轨视频可能引入音频参考，不能只按扩展名统计。[S1][S7]

V2 请求脚本只校验保守的数量、任务匹配和文件路径，**不自动探测输入时长／视频附带音轨**。执行者须完成上述 ffprobe 检查；不从第一轮就逼近输入上限。

### 7.3 锁定一组素材再分发

```bash
python3 scripts/manifest.py make \
  /srv/h3-ring/assets/acceptance-v1 \
  /srv/h3-ring/manifests/assets-acceptance-v1.sha256.json

ssh h3-s1 'mkdir -p /srv/h3-ring/assets/acceptance-v1'
rsync -av --ignore-existing --partial \
  /srv/h3-ring/assets/acceptance-v1/ \
  h3-s1:/srv/h3-ring/assets/acceptance-v1/
rsync -av --ignore-existing \
  /srv/h3-ring/manifests/assets-acceptance-v1.sha256.json \
  h3-s1:/srv/h3-ring/manifests/
```

其余节点同样处理；四台分别 `manifest.py verify`。素材变化时新建 `acceptance-v2` 和新清单，不改已经排队的旧素材。

### 7.4 URI 的具体含义

容器收到：

```text
file:///data/h3/acceptance-v1/hero-front.jpg
```

实际来自每台宿主机的：

```text
/srv/h3-ring/assets/acceptance-v1/hero-front.jpg
```

不是浏览器电脑路径，也不是只有 S0 存在的临时文件。上游支持服务端可见的本地 file URI；本套脚本把访问范围限定到专用素材目录，不接受任意外部链接和越界路径。[S2][S7]

---

## 8. 通用四机启动：每个 Profile 都遵循同一流程

### 8.1 首先排空并安全暂停旧服务

通过实际 Gateway／supervisor 暂停占用统一内存的旧模型和旧 H3；确认四台没有旧 H3 rank。已有原模型权重和镜像不删除。

V2 不接管你的现有 Gateway，不会自动 SSH 控制四台。它提供可以注册到现有管理层的**确定性前台启动命令**。现有管理层负责四节点生命周期；没有管理层且已确认独立模式安全时，才用 `--standalone-approved`。

### 8.2 四台分别生成本机检查回执

在四台 shell 中都设置同样的 PROFILE、DEPLOY（不要每台生成不同值）：

```bash
set -Eeuo pipefail
cd "$HOME/h3-ring-kit-v2"
PROFILE=fl2va-official
DEPLOY=p0-acceptance-001
RANK=$(python3 -c 'import json; print(json.load(open("config/node.json"))["rank"])')
mkdir -p "/srv/h3-ring/manifests/deploy/$DEPLOY"

python3 scripts/profile_node.py check "$PROFILE" \
  --deployment-id "$DEPLOY" --world 4 --full-hash \
  --report "/srv/h3-ring/manifests/deploy/$DEPLOY/r${RANK}.json"
```

检查包括：实际 image ID／ARM64、所需资产回执与哈希、Profile 配置、模型 revision、HCA／网卡基本存在性。它不证明 RDMA 路由或 GPU 正确性。任何节点失败先修复，不启动剩余节点凑数。

### 8.3 S0 汇总四份回执

```bash
scp "h3-s1:/srv/h3-ring/manifests/deploy/$DEPLOY/r1.json" \
  "/srv/h3-ring/manifests/deploy/$DEPLOY/"
scp "h3-s2:/srv/h3-ring/manifests/deploy/$DEPLOY/r2.json" \
  "/srv/h3-ring/manifests/deploy/$DEPLOY/"
scp "h3-s3:/srv/h3-ring/manifests/deploy/$DEPLOY/r3.json" \
  "/srv/h3-ring/manifests/deploy/$DEPLOY/"

python3 scripts/compare_nodes.py \
  "/srv/h3-ring/manifests/deploy/$DEPLOY/r0.json" \
  "/srv/h3-ring/manifests/deploy/$DEPLOY/r1.json" \
  "/srv/h3-ring/manifests/deploy/$DEPLOY/r2.json" \
  "/srv/h3-ring/manifests/deploy/$DEPLOY/r3.json" \
  --out "/srv/h3-ring/manifests/deploy/$DEPLOY/cluster-ready.json"
```

必须四个唯一 rank，逻辑配置、镜像、revision、文件回执和 deployment ID 指纹一致。主机本地 HCA 名称允许不同；**网卡拓扑是否正确仍由通信日志证明**。

回执不会被覆盖。重跑采用新 deployment ID，或先把旧回执归档，不伪装新检查沿用旧成功记录。

### 8.4 由管理层执行，或明确独立模式

**已有 Gateway／supervisor：** 四台分别渲染命令：

```bash
python3 scripts/profile_node.py render "$PROFILE" \
  --deployment-id "$DEPLOY" --world 4 \
  > "/srv/h3-ring/manifests/deploy/$DEPLOY/start-r${RANK}.command.txt"
```

它创建本次日志、缓存和运行目录，输出已引用具体本地目录的 `docker run ...`，但不启动容器。读取、审核后把该前台命令注册到实际管理层。保留容器标签、profile 指纹、只读挂载和日志映射；不要并行让旧任务管理器拉起另一套 H3。

**确实没有其他管理层：** 四台分别打开前台终端（或已有经过审核的会话管理工具），执行：

```bash
python3 scripts/profile_node.py run "$PROFILE" \
  --deployment-id "$DEPLOY" --world 4 --standalone-approved
```

四台各启动一次。**不要再外套 `torchrun --nproc-per-node=4`**。`--num-gpus=4` 在这条多节点入口表示全局 GPU 数，而每台仍使用 local CUDA device0。[S2]

新脚本默认运行时只校验文件大小；首次分发／任何变更后必须先做四台 `check --full-hash`，之后权重和配置保持只读。render 到 run 之间不得偷偷修改 Profile。

### 8.5 LIVE 检查，不能只看 API

S0：

```bash
curl --fail --silent http://127.0.0.1:30021/openapi.json \
  -o "/srv/h3-ring/manifests/deploy/$DEPLOY/openapi.json"
```

核对真实 schema 的 `POST /v1/videos`、查询、`/content`，以及任务与 target 参数。四台分别检查容器标签／启动日志：

```bash
python3 scripts/profile_node.py status "$PROFILE" --deployment-id "$DEPLOY"
```

需要确认四个 rank、world4、TP1/U1/R4、FA、replicate、实际 model variant、组件路径、量化范围、placement 和当前作业传输。P2尤其核对 tokenizer／processor 来自官方 H3 分区，而 encoder 权重来自 Heretic。

**在 LIVE 检查前，不激活队列。** `cluster-ready.json` 是离线回执，不是探测到四台服务健康的证明。

---

## 9. P0：官方 FL2VA 四机基础链路

### 9.1 创建请求，文件不覆盖

S0：

```bash
python3 scripts/request_v2.py --profile fl2va-official --task t2va \
  --prompt-file prompts/t2va.txt --seed 17 --smoke \
  --out requests/p0-smoke-001.json

python3 scripts/request_v2.py --profile fl2va-official --task t2va \
  --prompt-file prompts/t2va.txt --seed 17 \
  --out requests/p0-quality-001.json

python3 scripts/request_v2.py --profile fl2va-official --task fl2va \
  --prompt-file prompts/first_frame.txt \
  --first acceptance-v1/start.jpg --seed 17 \
  --out requests/p0-first-001.json
```

完成单机／双机最小定位测试后，按第 8 节四机启动。原 `video_client.py` 可作为诊断阶段的唯一提交入口，但**启用第 13 节统一队列后不要再旁路提交**。

先一条冒烟验证所有阶段，再正式请求。短边480不被实际快照接受时保留错误、修正测试配置并标注真实分辨率；不得删除参数校验后仍记为原测试。

### 9.2 P0 通过标准

先连续三条正常质量 T2VA，再完成首帧、尾帧、首尾帧的小组测试。尾帧 `--last acceptance-v1/end.jpg`；首尾帧同时给 `--first` 和 `--last`，提示词要相应说明起点和终点。

输出完整解码、有音轨，四 rank 对应同一作业；没有 OOM、worker 静默死亡、持续换页或通信停滞。视听检查记录动作、手部、镜头和音画同步。正常结束却不符合剧情的结果是质量失败，不能删除后只统计好样片。

P0配置通过后存为可回退基线，但不把其他分区一并标为通过。

---

## 10. P1：官方 Ref2VA，先简单后混合

完成官方 Ref 资产分发后，按第 14 节切换到 `ref2va-official`，新 deployment ID 使用 `p1-acceptance-001`。**不能只把现有 FL2VA 请求的 task 改成 ref2va 而不换服务分区。**

### 10.1 单角色图片

```bash
python3 scripts/request_v2.py --profile ref2va-official --task ref2va \
  --prompt-file prompts/ref_single.txt \
  --image acceptance-v1/hero-front.jpg --seed 17 \
  --out requests/p1-ref-one-001.json
```

### 10.2 三张同角色 + 一张场景

```bash
python3 scripts/request_v2.py --profile ref2va-official --task ref2va \
  --prompt-file prompts/ref_multi.txt \
  --image acceptance-v1/hero-front.jpg \
  --image acceptance-v1/hero-side.jpg \
  --image acceptance-v1/hero-halfbody.jpg \
  --image acceptance-v1/scene.jpg --seed 17 \
  --out requests/p1-ref-multi-001.json
```

提示词明确 `<Picture 1>`～`<Picture 3>`是同一成人角色的不同视角，`<Picture 4>`只作场景。不要让模型自己猜四张图片代表四个人还是同一个人。

### 10.3 角色图片 + 无音轨动作视频

```bash
python3 scripts/request_v2.py --profile ref2va-official --task ref2va \
  --prompt-file prompts/ref_motion.txt \
  --image acceptance-v1/hero-front.jpg \
  --video acceptance-v1/motion-silent.mp4 --seed 17 \
  --out requests/p1-ref-motion-001.json
```

### 10.4 角色图片 + 授权声音

```bash
python3 scripts/request_v2.py --profile ref2va-official --task ref2va \
  --prompt-file prompts/ref_voice.txt \
  --image acceptance-v1/hero-front.jpg \
  --audio acceptance-v1/voice.wav --seed 17 \
  --out requests/p1-ref-voice-001.json
```

声音参考不是保真克隆保证；先验证音频条件确实被读取、输出音画与对白可用。保持官方 H3 的 `<d>` 等特殊标记处理路径。[S1]

### 10.5 最后才测试混合与关键帧

上述各类单独通过后，再用小规模图＋动作＋声音的自定义提示词，并保留所有素材哈希。Ref2VA 加首／尾帧仍至少需要一个 `role=reference` 条件；纯关键帧任务用 P0。[S2][S7]

**V2 不先挑战最大参考数量、15秒输出或高并发。** P1初步验收建议五类输入各两个 seed，共十条；其中混合条件从最少数量开始。记录图片理解、服装／身份漂移、动作执行、场景、声音分别是否满足预期。

---

## 11. P2：Ref2VA Heretic，必须做真正的组件对照

### 11.1 CPU 架构检查

在已下载完整 Heretic 权重后：

```bash
python3 scripts/check_encoder.py \
  --official /srv/h3-ring/models/MiniMax-H3/Ref2VA/text_encoder \
  --candidate /srv/h3-ring/models/encoders/Heretic-Qwen3VL32B \
  > /srv/h3-ring/manifests/heretic-config-check.json
```

检查内容包括：Qwen3-VL 架构、5120 hidden width、所需层数、视觉塔、token ID 与 rope 参数的语义一致性，以及 safetensors 索引指向的文件是否齐全。新版 config 的 `rope_parameters` 与旧版的 `rope_scaling` 表示法先归一比较，不凭 JSON 字节不同直接判断不兼容。[S3][S6]

**这个检查只输出 `PASS_CONFIG_ONLY`。** 它不验证实际 tensor 的每一个 shape、GB10 kernel、native loader、隐藏状态选层或视频效果。出现配置不兼容先查原始资源和框架解析，不用修改 shape、跳过层或删除校验“修好”。

### 11.2 保留官方 tokenizer／processor

H3 使用专用 tokenizer 配置及 `<d>` 标记；它不是通用聊天模型的 tokenizer 可以无条件替换的部分。[S1]

在已验收运行时内做 CPU 检查（以下 IMAGE 从真实配置读取）：

```bash
IMAGE=$(python3 -c 'import json; print(json.load(open("config/cluster.json"))["image"])')
docker run --rm --network none \
  --mount "type=bind,src=$PWD,dst=/kit,readonly" \
  --mount type=bind,src=/srv/h3-ring/models/MiniMax-H3,dst=/models/h3,readonly \
  --mount type=bind,src=/srv/h3-ring/models/encoders/Heretic-Qwen3VL32B,dst=/models/heretic,readonly \
  "$IMAGE" python /kit/scripts/check_encoder.py \
  --official /models/h3/Ref2VA/text_encoder --candidate /models/heretic \
  --tokenizer /models/h3/Ref2VA/tokenizer
```

若镜像 CPU 路径会触发硬件初始化，以已审核诊断容器运行，但不要顺便启动一份生成服务。该测试只检查专用词存在、索引范围等；真实启动还要核对框架解析到的 tokenizer／processor 路径。**不得把 Heretic 仓库整目录覆盖进官方 `text_encoder/` 或 tokenizer 目录。**

### 11.3 四机切换与覆盖参数

P2 与 P1 的设计差异只增加：

```bash
--component-paths.text_encoder /models/heretic
```

仍用官方 Ref2VA DiT 与 VAE，原生 H3 条件提取器。完整组件覆盖方式由上游支持，但这不等于本次 GB10 Ring4 组合已通过。[S2][S3]

按第 14 节切到 `ref2va-heretic`，deployment ID `p2-acceptance-001`。四台都必须有同一 Heretic SHA及全部 shards；官方 tokenizer/processor不变。若日志显示意外量化编码器、解析到通用 tokenizer 或丢失视觉塔，停止验收并定位，不把能开始去噪当作成功。

### 11.4 A/B 不是只看“会不会拒绝”

使用 P1同一批请求 JSON、相同输入、seed、sigma网格、分辨率、精度／placement。提交到 P2时外层队列 Profile 改为 `ref2va-heretic`；Ref请求正文不必修改。

逐项记录：身份与服装保持、图像／动作参考理解、声音与对白、提示词遵循、细节闪烁、语义异常、拒绝／回避表现、峰值内存与总耗时。测试范围使用合法授权素材与正常创作任务。**一个聊天拒绝率基准不等于 H3条件特征的质量评价。**

如果只有 P2失败，优先检查该次新增的编码器、装载与内存差异，但不武断排除间歇网络或机器问题；保留同条件官方复测。

### 11.5 什么时候把 P2设成主力

完成与 P1匹配的十条输入测试；随后在最终候选配置上连续执行一组至少20条日常任务并做一次中断恢复。所有尝试均入账；技术完成与人工可用分开统计。

P2出现明显参考理解或身份一致性退化时不提升为默认，保留它作为可选实验；继续以 P1正常工作，并提交明确差异。不得因为用户希望高服从就隐藏质量代价。

---

## 12. P3：Hybrid 实验，不抢占基础交付

### 12.1 本轮只测试这一种组合

```text
Singularity v1.3 完整 INT8 Hybrid transformer
+ 官方 H3 编码器／tokenizer／processor／VAE
+ TP1/U1/Ring4/FA/eager
+ 50点基线采样；不叠加 Turbo LoRA
```

模型任务统一与采样行为是不同契约。作者要求额外 sampler／sigma时先确认当前原生框架能否复现；只加载成功不代表采样合同正确。[S2][S5]

### 12.2 依赖先隔离

INT8 ConvRot路径需要匹配的 `comfy-kitchen`。不在已经工作的容器里交互执行浮动 `pip install -U`。[S2]

现有镜像中已具备且验证了所需依赖时可复用；否则用 `Dockerfile.hybrid` 建独立镜像。由执行 Agent先查询可获得的版本、结合固定运行时完成小测试，再锁定具体版本；本文件不编造一个“SM121必然兼容”的版本号。

```bash
# 替换为已经核对的真实 image/tag 和确切版本，不照抄占位值。
BASE_IMAGE=YOUR_VERIFIED_BASE_IMAGE
KITCHEN_VERSION=YOUR_REVIEWED_EXACT_VERSION

docker build -f Dockerfile.hybrid \
  --build-arg BASE="$BASE_IMAGE" \
  --build-arg KITCHEN_VERSION="$KITCHEN_VERSION" \
  -t local/h3-ring-hybrid:v2 .
```

该层使用 `--no-deps` 防止安装器悄悄升级 torch；`pip check` 失败必须处理，不继续生成。额外依赖确实缺少时，逐项固定并形成新 Dockerfile修订，不能盲目忽略检查。

保存镜像ID、依赖freeze、基础镜像和构建日志，在新镜像中重跑 GB10 FA／必要量化算子／通信测试。镜像只构建一次再分发，原基础镜像保留。

为这个镜像新建 `config/cluster-hybrid.json`，仅替换实际 image 与 image_id。调用 P3时四台都显式传入：

```bash
python3 scripts/profile_node.py check hybrid-experimental \
  --cluster config/cluster-hybrid.json \
  --deployment-id p3-acceptance-001 --world 4 \
  --full-hash --allow-experiment \
  --report /srv/h3-ring/manifests/deploy/p3-acceptance-001/r0.json
```

上面 `r0` 仅 S0，其他节点对应本机 rank。汇总、render／run、status／stop都使用相同的 `--cluster config/cluster-hybrid.json` 和 `--allow-experiment`。

### 12.3 不能再次量化

P3实际增加：

```bash
--model-variant hybrid
--component-weights-paths.transformer /models/hybrid/Minimax-h3_Singularity_ref2va_v1.3_int8.safetensors
```

**不带 `--quantization fp8`，不添加第二个在线 transformer量化参数。** 新脚本会拒绝这种错误组合。只设置 `model-variant=hybrid` 而未加载真正的 Hybrid权重也不允许。[S2]

V2保守要求官方两个分区都已准备，供兼容组件与回退使用；这不表示运行时必须同时驻留两份DiT。

### 12.4 P3最低验收

分别测试 T2VA、首帧、首尾帧、单图Ref、多图Ref、视频Ref、音频Ref。同一任务不能因为统一入口“看起来很方便”就省略质量检查。任一关键能力不通过，Profile继续标实验，不影响P1/P2交付。

暂不提供 P3+Heretic直接开关。这项确需继续时必须新增明确修订、保留 P3官方encoder对照并重复各模态测试，不偷偷修改现有已验收文件。

---

## 13. 一个入口与持久化队列：V2 已提供的最小实现

### 13.1 实现范围

`scripts/queue_v2.py` 在 S0使用 SQLite WAL保存任务，`flock`确保同一队列同时最多一个 worker；每个 worker只处理一个已运行／已排队任务。配合本计划限制，**四机组一次只生成一条**。

它提供CLI，不提供已接好的Web UI、HTTP队列服务、跨主机分布式锁或自动Profile切换。所有提交必须经此S0入口，或由已有Gateway实现等价约束；**用户／其他进程直接POST后端会绕过队列**。后端仍绑定localhost，工作台必须改接统一入口，不同时保留旧直连生成方式。

未知提交将阻塞后续派发；脚本不自动重复POST、不自动从中断去噪步继续、不承诺后台守护已经启动。

### 13.2 初始化与显式激活

确保S0可执行 ffmpeg／ffprobe，然后：

```bash
cd "$HOME/h3-ring-kit-v2"
python3 scripts/queue_v2.py init
python3 scripts/queue_v2.py pause
```

默认暂停。只有完成第8.5节LIVE检查后，才能执行：

```bash
python3 scripts/queue_v2.py activate \
  --receipt "/srv/h3-ring/manifests/deploy/$DEPLOY/cluster-ready.json" \
  --live-verified
python3 scripts/queue_v2.py resume
```

`--live-verified` 是操作者对已完成现场检查的明确确认，**不是脚本自动连接四台检查**。入门第一次正式视频随后经队列验证；失败则立刻pause并定位。

### 13.3 提交、处理、查看

```bash
# 外层返回一个持久化任务ID；此命令只入队，不调用模型生成。
python3 scripts/queue_v2.py submit --profile fl2va-official \
  --request requests/p0-quality-001.json

# 前台处理一个任务。超出轮询预算不代表取消后台生成。
python3 scripts/queue_v2.py worker --poll-budget 14400

python3 scripts/queue_v2.py status
```

入队保存请求正文、Profile、素材大小与SHA256；派发前再次检查S0素材是否改变。四台素材一致性由第7.3节完成，队列不会通过SSH替你核对另外三台。

输出：`/srv/h3-ring/queue/outputs/<外层任务ID>.mp4`，旁边保存ffprobe结果。DB和events表保留后端jobID、Profile指纹、deployment、错误和时间。`completed`表示下载及技术解码通过，人工画质仍标pending。

### 13.4 批量串行怎么执行

先按当前Profile入队N条，再由现有Gateway为每次需要的处理调用一次worker；或者在S0前台执行有限次数：

```bash
# 例如已经明确入队本Profile的5条任务；任一异常退出就停止检查。
for _ in 1 2 3 4 5; do
  python3 scripts/queue_v2.py worker --poll-budget 14400 || break
done
python3 scripts/queue_v2.py status
```

不能用循环次数当作完成条数：一个worker可能仅恢复同一未完成job，可能队列暂停，或者该Profile没有排队任务。以任务状态为准。脚本不无限重试，不给每张卡单独创建一条全群队列。

队列允许存放其他Profile的queued任务，但不会自动加载那个Profile。切换后只有匹配当前Profile的任务才会派发。

### 13.5 对接现有短剧工作台

第一轮不要求重写ComfyUI或做复杂插件。现有工作台／Gateway应完成四个动作：把授权素材落到版本化assets并同步；生成受控请求JSON；调用入队入口取得任务ID；读取状态和下载结果。

执行Agent须把这些动作接入现有的进程管理／调用机制，并验证实际输入输出。V2没有假称一个通用ComfyUI节点已经存在。避免把任意用户字符串拼接为shell命令，使用参数数组或固定CLI适配器；避免让模型自己决定主机命令／停服动作。

工作台显示至少：当前Profile、等待／运行／未知状态、真实耗时、输出位置、错误，以及结果是否经过人工确认。

---

## 14. Profile 切换与回退：四台必须作为一个整体

### 14.1 切换事务

```text
停止新派发
  → 当前任务完成或明确处理中断／未知状态
  → 记录旧Profile、deployment、镜像和完整配置
  → 通过实际管理层停止四个旧rank
  → 确认全退出、统一内存释放，没有旧任务继续生成
  → 对目标Profile做四节点字节／配置检查并汇总
  → 同一个新deployment启动四个rank
  → 四台LIVE日志、组件、API与传输确认
  → 激活新队列指纹，进行一条受控测试
  → 通过后继续派发；失败则pause并回退
```

队列pause只阻止新任务派发，**不会取消正在运行的后端任务**：

```bash
python3 scripts/queue_v2.py pause
python3 scripts/queue_v2.py status
```

存在running时等待完成或按第15节处理；存在submitting／unknown时先核对。不允许仅看队列paused就直接杀机然后把任务误标成功。

### 14.2 停止示例

有Gateway／supervisor时用其实际停止入口。独立模式才可在四台分别执行：

```bash
python3 scripts/profile_node.py stop ref2va-heretic \
  --deployment-id p2-acceptance-001 --standalone-approved
```

它会检查目标容器的Profile与deployment标签，不停止无关容器。这里不是远程一键四机命令；四台均确认完成，且没有被上级服务重新拉起后才启动下一组。

### 14.3 回退顺序

P2失败优先回P1，P1暂不可用回P0；P3失败回本次进入实验前已验收的P1／P2。回退也生成新的deployment ID与四节点回执，不拿历史回执冒充本次LIVE检查。

**回退的是已知配置和资产，不是重新下载／覆盖模型。** 有queued的旧Profile任务可以保持排队；状态未明的旧任务必须先处理，不能换Profile后对着新后端去查询一个可能已不存在的旧job而假定未生成。

启动一半失败时，队列保持paused，收集四台首个错误并停止所有新rank；不要让旧模型rank与新模型rank拼成同一world。

---

## 15. 失败处理和恢复的具体做法

| 故障 | 应收集的证据 | 下一步 |
|---|---|---|
| FA/LSE失败 | GPU capability、实际torch/FA版本、首个堆栈 | 停在内核检查，修复SM121适配；不换cuDNN冒充Ring |
| 只有某rank装载失败 | 每台组件路径／sha／内存与worker首错 | 先区分字节不一致、encoder shape、装载峰值 |
| Ring初始化挂住 | 全四台NCCL日志、HCA映射、控制连接 | 回到同镜像通信测试；不连续提交视频 |
| TCP成功、IB失败 | 两种模式明确日志 | 记录计算通过/RDMA阻塞，继续修网络或插件 |
| P2理解图片异常 | tokenizer/processor、视觉塔、encoder输出信息 | 回P1同请求对照，不把不拒绝当作正确理解 |
| P3已加载但出片异常 | 完整权重名、量化后端、sigma/采样约定 | 核对任务混合与采样合同，不盲加Turbo或二次量化 |
| 出片无声或损坏 | ffprobe+完整解码+音频VAE日志 | 不标completed；修音频链路 |
| 持续swap或异常变慢 | MemAvailable、swap输入输出、各阶段耗时 | 停止新任务，降低单次输入或修驻留策略，记录变化 |

### 15.1 POST响应丢失：禁止自动再发一次

V2在发送POST前先保存 `submitting`。若请求返回不完整、超时或进程在该窗口退出，记为 `unknown`，阻止下一条生成。

先按时间窗口、请求内容和后端日志查到实际job。确认后：

```bash
python3 scripts/queue_v2.py attach \
  --id ACTUAL_OUTER_JOB_ID --backend-id VERIFIED_EXISTING_BACKEND_ID
python3 scripts/queue_v2.py worker --poll-budget 14400
```

这里只恢复GET轮询，不新POST。`attach`的ID必须来自后端证据，不能猜。

确实无法核对且决定中断整个旧服务时：pause，记录日志，通过管理层停止四rank并确认不再生成，再执行：

```bash
python3 scripts/queue_v2.py mark-interrupted \
  --id ACTUAL_OUTER_JOB_ID --all-ranks-stopped-confirmed
```

它不自动重新入队。需要重新生成时使用明确的新任务，并说明旧结果可能无法恢复，而不是声称原任务从未开始。

### 15.2 GET超时、worker退出

已保存后端ID且状态running时，下一次worker继续查询同一job。轮询预算用尽不删除ID、不取消后端。先核对当前队列active deployment仍是原来四rank。

后端已completed但下载／技术检查失败，状态failed但保留backendID。先尝试以原ID取回artifact；**不能因为GET失败而重新POST同一视频**。保持该后端仍可查询时，可使用原客户端的只恢复模式：

```bash
python3 scripts/video_client.py \
  --request requests/ACTUAL_ORIGINAL_REQUEST.json \
  --resume-id VERIFIED_EXISTING_BACKEND_ID \
  --out /srv/h3-ring/acceptance/recovered-unique-name.mp4
```

这里必须提供原请求和已核对ID，选择未存在的输出文件名，再执行完整解码。它不会自动改变原队列的failed记录；在该任务的验收说明中关联恢复结果，不掩盖原下载失败。

### 15.3 可恢复不等于去噪断点续算

中断测试的最低要求：能够停止派发、保留失败记录、退出全四rank、按冻结配置重启、完成下一条正式视频。V2不实现从某一去噪步接续，也不保证服务器重启后仍保留旧job的可查询状态。

### 15.4 数据库备份

队列数据库只放S0本地可靠文件系统，不放跨主机共享盘充当分布式调度。在pause且没有派发worker写入窗口时，使用SQLite backup接口保存一致副本，不只复制`queue.sqlite3`而漏掉WAL：

```bash
python3 - <<'PY'
import sqlite3, time
from pathlib import Path
root=Path('/srv/h3-ring/queue')
dst=root/f'backup-{time.time_ns()}.sqlite3'
with sqlite3.connect(root/'queue.sqlite3') as source, sqlite3.connect(dst) as target:
    source.backup(target)
print(dst)
PY
```

---

## 16. 验收、性能记录与最终交付

### 16.1 每个Profile独立的验收表

```csv
attempt_id,profile,model_revision,encoder_revision,hybrid_revision,image_id,deployment_id,config_fingerprint,task,asset_manifest,seed,target_duration,actual_duration,actual_width,actual_height,grid_points,actual_nfe,network_mode,encoder_s,denoise_s,vae_s,total_wall_s,min_mem_available_per_node,decode_ok,identity_ok,motion_ok,audio_ok,prompt_ok,error,notes
```

取不到的字段留空并说明，不把`N/A`当0。初次装载、量化、编译与热态任务分开记录。固定相同输入才比较Profile；不要用5点冒烟对比50点正式生成。

本文不预填此前聊天的15–30分钟推算，不承诺四机必然更快。先取得本机真实分项数据。功能、RDMA、长期使用与性能收益分别给结论。

### 16.2 技术验收与人工验收

队列媒体检查采用项目初始阈值：单视频+单音频流，H264/AAC、24fps、32kHz立体声，时长接近请求（容差0.75秒），短边与目标对齐误差≤32，音视频流时长差≤0.25秒，并完成ffmpeg全量解码。它们是本套工具的**严格初始检查值，不是所有编码容器通用规范**。官方或当前框架输出契约确有合法差异时须记录并修订测试，不悄悄放宽掩盖问题。

人工检查角色、手部、动作、参考理解、音画事件、对白和闪烁。黑画面／静止是否错误需结合prompt，不写一个“全黑必坏”的不分任务规则。

### 16.3 最终应留下的资料

```text
acceptance/<profile>-<deployment>/
  environment.md          # 真实硬件、驱动、网络、管理入口
  configs/                # 本次JSON、Profile、镜像与源码身份
  locks-and-receipts/      # 各资产固定版本及四节点字节证据
  materials/              # 素材清单、权利说明，原始素材可另行受控保存
  requests/               # 全部请求，不只成功样本
  logs/                   # 四rank日志、实际transport、内存
  outputs/                # 实际音视频与解码检查
  attempts.csv            # 全部尝试和实际耗时
  operations.md           # 已验证的启停、切换、队列与回退步骤
  acceptance.md           # 各项PASS/BLOCKED/NOT_TESTED及证据
```

### 16.4 当前交付已经测试了什么

本次工具包本地测试共28项：原8项回归+新20项。覆盖Python/shell语法、四Profile组合、TP1/U1/R4参数、Heretic只换encoder、Hybrid拒绝再次量化、四节点指纹／哈希、输入路径、rope归一、POST未知不重发、已知job恢复、pause、worker互斥、素材变更拦截。另用CPU合成的测试MP4验证ffprobe／ffmpeg检查；**不是H3生成的样片**。

未完成的现场项目：依赖在线安装／镜像构建、实际模型下载、所有GPU量化核验、FA/GB10、RoCE/RDMA、原生H3加载、四机出片、人工质量、Gateway集成。测试通过不能替代这些检查。

---

## 17. 给本地执行 Agent 的完整任务指令

> 以本V2主文档为准，保留原4台DGX Spark无高速交换机Ring协同生成目标。不要改成单机farm、两组双机或其他模型。单／双机只用于定位问题。
>
> 先只读盘点现场，列出已有资源和每个检查点的证据。保护DeepSeek／Qwen、网卡、镜像、权重和现有Gateway。复用已合格的内容，不重复下载一致文件，不绕过supervisor另起一份服务。
>
> 先让P0官方FL2VA四机正常出片，再切P1官方Ref2VA做单图、多图、视频、音频和小规模混合验证。然后只替换BF16 Heretic编码器为P2，保持H3官方tokenizer／processor、同一DiT与VAE，比较相同请求。P2目标是可选主力，但必须经真实质量和稳定性验收后提升；失败时官方回退照常可用。
>
> P3只在前面可用后实验，采用明确的Singularity完整INT8文件和官方编码器，不再次在线量化，不混入Turbo，不自动标为越狱。新增依赖建立独立镜像，固定版本并重测，保存原镜像。
>
> 每个Profile切换都pause并排空／核对旧任务、停止四rank、确认资源释放、比较四份新回执、以同一deployment启动、确认LIVE状态后再激活队列。素材在四台映射到同一容器路径且哈希一致。实际入口全群一次仅生成一条，超时先查原job，未知POST不自动重发。
>
> 出错保留首个异常、四台日志、权重与运行时身份、输入和峰值。只修复已定位的最小变量，记录patch和回归。遇到未经审查的模型布局、内核或请求schema变化不得猜字段、删除校验或静默换格式。
>
> 把新CLI适配到实际Gateway／短剧工作台；先提供可用的命令入口，UI不是首次部署前置条件。不把尚未实现的接口、远程编排或恢复写成已完成。最终交付固定操作方法、所有尝试、真实视频、Profile独立验收和回退证据，不预填速度、不隐去失败。

---

## 18. 来源与可复核入口

以下是本次核对事实与契约的入口；脚本、目录、队列、阶段顺序及测试阈值是本计划的工程设计，不冒充上游“四台Spark一键部署”。动态页面按访问时内容读取，实际下载必须固定revision；运行时继续锁定原审查SHA。

- **[S1] MiniMax官方模型卡、原始分区、输入与tokenizer要求：** https://huggingface.co/MiniMaxAI/MiniMax-H3
- **[S2] SGLang H3 cookbook：** https://docs.sglang.io/cookbook/diffusion/MiniMax/MiniMax-H3 ；固定源码：https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx
- **[S3] 原生H3 Qwen3-VL编码器、视觉塔、选层与维度约束：** https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/python/sglang/multimodal_gen/runtime/models/encoders/minimax_h3_qwen3vl.py
- **[S4] H3 PipelineConfig：Ring/FA、质量策略与offload边界：** https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/python/sglang/multimodal_gen/configs/pipeline_configs/minimax_h3.py
- **[S5] Singularity文件清单与作者说明：** https://huggingface.co/WarmBloodAban/Minimax-h3_Singularity/tree/main
- **[S6] Heretic候选及配置对照：** https://huggingface.co/llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic ；https://huggingface.co/llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic/blob/main/config.json ；https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/Ref2VA/text_encoder/config.json
- **[S7] 官方可复现请求：** https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/scripts/readme/reproducible-768p-ref2va-request.sh ；https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/scripts/readme/reproducible-768p-fl2va-request.sh
- **[S8] NVIDIA Spark组网说明：** https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html
- **[S9] NVIDIA NCCL环境变量／网络排障：** https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html ；https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/networking_troubleshooting.html
- **[S10] Hugging Face固定revision和文件下载：** https://huggingface.co/docs/huggingface_hub/guides/download

原始输入文件的SHA256及V2本地测试输出附在工具包 `BUILD_REPORT.md` 和 `test-results.txt`。它们用于追溯本次修订，不是用户设备上的验收证据。
