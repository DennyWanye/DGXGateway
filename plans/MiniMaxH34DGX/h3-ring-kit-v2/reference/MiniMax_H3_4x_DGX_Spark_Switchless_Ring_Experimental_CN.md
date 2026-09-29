# MiniMax H3：4×DGX Spark 无交换机物理 Ring 实验部署方案

**核验日期：2026-09-17**  
**目标硬件：4 台 DGX Spark；每台 1 个 GB10 GPU、128GB 统一内存；Linux ARM64。**  
**目标任务：一个 H3 视频由四台共同完成，不是四台分别生成四个视频。**  
**首选实验拓扑：SGLang Native / TP1 × Ulysses1 × Ring4 / encoder replicate。**

> **先明确交付边界：这是带检查点、停止条件和回退路径的实验集成方案，不是已在你的四台机器上通过的安装记录。** 已核对上游代码中的 Ring 约束、全局 GPU 参数、模型目录及请求格式；本文没有把“其他硬件完成跨节点 Ring”或“双 Spark 跑通 H3”冒充为“四 Spark 无交换机已验收”。本交付未构建 ARM64 GPU 镜像，未在 GB10 上执行 FlashAttention、RDMA、NCCL 或 H3 生成。辅助脚本的本地检查范围见最后一节。

## 0. 先看结论与执行顺序

建议沿用已经为 DeepSeek 配置好的物理接线和经过验证的网络，在**独立目录、独立镜像、独立端口**中测试 H3。不要卸载 DeepSeek、不要覆盖它的网卡配置、不要共用并修改它的 Python 环境。

本次路线是：

```text
现网只读盘点与备份
  → 独立 ARM64 镜像构建
  → 每台 GB10 的 FA packed-varlen + LSE 小测试
  → 2 台 / 4 台通信测试，明确 IB 与 Socket
  → 锁定模型 revision，按分区下载和分发
  → 单台 H3 基线
  → 相邻双台 Ring2
  → 四台 Ring4
  → 音视频完整解码与 50 步质量验收
  → 同条件测速
  → 最后才扩展 FL2VA / Ref2VA
```

**不要先花大量时间下载所有权重，再发现 FA 内核或四机通信不支持。** 下载与镜像构建也会占用内存和磁盘；正式 GPU 测试时先暂停占用资源的现有模型服务。

### 0.1 本次固定的选择

| 项目 | 本文的选择 | 需要注意 |
|---|---|---|
| 推理框架 | SGLang Native，固定源码快照 | 不是把 vLLM 双机自定义 executor 改为 4 |
| 首轮模型 | 官方 `MiniMaxAI/MiniMax-H3` 的 `FL2VA/` 原始分区 | 该分区同时用于纯文本 T2VA 和首尾帧 FL2VA |
| 四机分工 | `TP=1, Ulysses=1, Ring=4` | 这是序列并行，不是 DeepSeek TP4 |
| 编码器 | `--encoder-parallel replicate` | 跨节点 H3 Ring 不沿用默认 auto |
| Attention | 显式 `--attention-backend fa` | 必须先在 SM121 上验证对应 FA 路径与 LSE |
| 权重量化候选 | 官方原始权重加载时 `--quantization fp8` | 主要针对 DiT；不是所有组件统一变成 FP8 |
| 执行方式 | eager；禁用 torch.compile、FSDP | 首轮不叠加稀疏注意力、缓存、蒸馏加速 |
| 网络模式 | 原生 NCCL verbs 的 `IB` 实验；Socket 只作对照 | 现有方案若依赖自定义网络插件，需要单独移植验证 |
| 初始任务 | 单并发；4 秒；低分辨率冒烟后升至 768 短边 | 5 步只验证链路，质量检查使用 50 步 |

模型分区、量化范围、跨节点参数依据上游说明；**FP8 + GB10 + 4 节点 Ring 并不是上游已验收组合**。[S1][S2][S3][S4][S8]

### 0.2 对前面讨论的几个必要修正

**第一，三种 Ring 不是一件事。** 物理 Ring 表示线缆连接；H3 Ring 表示序列注意力的计算与交换方式；`NCCL_ALGO=Ring` 表示部分 collective 的算法选择。设置后者，不会自动解决前两者之间的 rank、HCA、GID 和路由映射，也不会限制所有通信只能发生在相邻机器。[S3][S6]

**第二，Ring4 不提供一个透明的 512GB 显存池。** 这条路线不启用 TP/FSDP 权重分片，编码器又使用 replicate。应按每台需要容纳本地组件和临时缓冲来测峰值，不能把模型总大小除以 4 得出单机需求。VAE、量化转换、编码与解码阶段的峰值也不一定随 Ring degree 下降。[S2][S3][S8]

**第三，不能保证现有网络配置原样通用。** DeepSeek 在另一套 NCCL 版本或自定义插件上成功，只说明那套组合完成了自己的通信。H3 新镜像必须重新通过测试。物理 Ring 与 Ring Attention 的邻接交换有一定匹配性，但这只是实验动机，不是网络已被解决的证明。[S3][S6]

**第四，不保证 FP8 无损，也不承诺四机提速倍数。** 本文选择 FP8 是为了给单节点内存留出实验空间；若运行时 FP8 内核或装载峰值不适合 GB10，应停在该检查点，而不是宣称已经得到稳定的四机质量版。[S2][S8]

---

## 1. 需要准备什么

### 1.1 硬件与接线

完整四机物理环需要四条适配的高速直连链路，每台分别连接两个邻居：

```text
           E01
     S0 ──────── S1
     │            │
 E30 │            │ E12
     │            │
     S3 ──────── S2
           E23

物理顺序与模型 rank：0 → 1 → 2 → 3 → 0
每台：1 个 GB10；本地可见 CUDA device 0。
```

DGX Spark 的两个 QSFP 物理口各支持最高 200Gb/s，使用 Ethernet/RoCE，不是把端口切换成 InfiniBand fabric 模式。一个物理口会对应两个 Linux Ethernet 接口及相应 RoCE 设备，因此不要把“四个 netdev”误认为“四根线”。[S5]

**本方案不要求购买高速交换机。** 但要有四节点都能到达的 TCP 控制/初始化地址。可以复用现有管理网络，或现有物理 Ring 上已经验证的 IP 路由；不能默认 Wi-Fi、VPN 或断开的管理口承担高速数据面。若完全没有独立管理网络，附录 B 提供的是 **TCP 控制面实验方案**，不是完整 RoCE 路由方案。

### 1.2 存储与运行资源

每台需要本地权重副本，以及镜像、构建缓存、日志、输出和临时装载空间。本文不会给一个伪精确的固定下载量；第 7 节脚本先查询当前固定 revision 的实际文件大小。

**工程规划值：新部署先预留每台约 350–500GiB 可用磁盘，再检查实际清单。** 这是包含镜像、导出包、原始权重与临时空间的保守预算，不是 NVIDIA 或 MiniMax 的硬性要求；已有可复用文件会减少新增下载量。扩展另一任务分区前重新计算空间。

不要将四台的 128GB 统一内存全部视为可供模型独占。GB10 上 CPU、GPU、系统与缓存共享物理内存；不同框架的 offload 实现也不同，不能套用独显机器“显存不够就搬到另一份大内存”的假设。[S5][S7]

### 1.3 本次不动的内容

保留原有 DeepSeek / Qwen 的权重、镜像、启动脚本、网络文件和缓存。已有 `/srv/h3`、FL2VA、Ref2VA、Heretic encoder 或 Turbo 资源也不要删除；**它们的目录是否真实存在，需以你的机器为准**。

新状态目录统一用 `/srv/h3-ring`。代码工具放在用户目录的 `~/h3-ring-kit`。首轮只跑官方 Base，不加载第三方编码器、Turbo LoRA 或稀疏注意力变体，以免同时引入多个无法定位的变量。

---

## 2. 取得本文中的脚本

配套 ZIP 已包含配置模板和脚本。**只有这份 Markdown 也可以完整还原运行所需文件**：附录 A 的每个文件都带独立标记。

先把本 Markdown 保存到当前目录，再执行以下提取命令。第二个参数必须是一个新的或空的目录；脚本不会覆盖已有文件。

```bash
python3 - './MiniMax_H3_4x_DGX_Spark_Switchless_Ring_Experimental_CN.md' "$HOME/h3-ring-kit" <<'PY'
import pathlib, re, sys
source = pathlib.Path(sys.argv[1]).read_text(encoding='utf-8')
root = pathlib.Path(sys.argv[2]).resolve()
blocks = re.findall(r'<!-- H3FILE ([^\n]+) -->\n```[^\n]*\n(.*?)\n```', source, re.S)
if not blocks:
    raise SystemExit('没有找到文件区块；请使用完整 Markdown。')
planned = []
for name, content in blocks:
    rel = pathlib.PurePosixPath(name)
    if rel.is_absolute() or '..' in rel.parts:
        raise SystemExit('拒绝不安全的文件路径。')
    dst = root / str(rel)
    if dst.exists():
        raise SystemExit(f'目标已存在，不覆盖：{dst}')
    planned.append((dst, content))
for dst, content in planned:
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(content + '\n', encoding='utf-8')
print(f'已提取 {len(planned)} 个文件到 {root}')
PY

cd "$HOME/h3-ring-kit"
chmod +x scripts/*.sh
```

运行前阅读 `scripts/run_node.sh` 和配置模板。`.env` 文件采用 shell 语法，只加载你自己审核过的文件。

---

## 3. 阶段 G0：盘点、备份、确认真实四机拓扑

### 3.1 四台分别执行只读盘点

以下多行安装命令建议在 Bash 中以 `set -Eeuo pipefail` 执行；任一检查失败就停下，不逐行越过错误继续。诊断日志中的 `|| true` 仅用于容许无匹配输出，不用于忽略安装、下载或测试失败。

```bash
set -Eeuo pipefail
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /srv/h3-ring
mkdir -p /srv/h3-ring/{audit,models,cache,logs,output,manifests,images}
AUDIT="/srv/h3-ring/audit/$(hostname)-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$AUDIT"
chmod 700 "$AUDIT"

uname -a > "$AUDIT/uname.txt"
nvidia-smi -q > "$AUDIT/nvidia-smi-q.txt"
free -h > "$AUDIT/memory.txt"
df -h > "$AUDIT/disk.txt"
ip -br addr > "$AUDIT/ip-brief.txt"
ip -details link show > "$AUDIT/links.txt"
ip -4 rule show > "$AUDIT/ip-rules.txt"
ip -4 route show table all > "$AUDIT/routes.txt"
rdma link show > "$AUDIT/rdma-links.txt"
ibdev2netdev > "$AUDIT/ibdev2netdev.txt"
docker ps -a --no-trunc > "$AUDIT/containers.txt"
docker image ls --digests --no-trunc > "$AUDIT/images.txt"
ss -lntup > "$AUDIT/listeners.txt"
sysctl net.ipv4.ip_forward > "$AUDIT/ip-forward.txt"
sysctl -a 2>/dev/null | grep -E 'net.ipv4.conf.*rp_filter' > "$AUDIT/rp-filter.txt"

# 网络配置备份只写入审计目录，不修改原文件。
sudo tar -czf "$AUDIT/netplan.tgz" -C /etc netplan
sudo chown "$(id -u):$(id -g)" "$AUDIT/netplan.tgz"
```

某个诊断命令不存在时，先检查系统是否已提供同类工具；缺少 `ibdev2netdev` 不意味着没有 RDMA。不要为了安装工具直接更换已工作的驱动/OFED。可以用 `/sys/class/infiniband`、`rdma link` 和 NVIDIA 说明辅助对应网卡。[S5]

### 3.2 保存现有 DeepSeek 的运行方式

先在 `docker ps -a` 中确认实际容器名，再替换以下变量；**不要照抄一个猜测出来的名称**。

```bash
OLD_CONTAINER=CHANGE_ME_TO_ACTUAL_DEEPSEEK_CONTAINER
[[ "$OLD_CONTAINER" != CHANGE_ME* ]] || { echo '先填写真实容器名'; exit 1; }

# inspect 可能含 API Key、Token、内部路径，只保存在本地且限制权限。
umask 077
docker inspect "$OLD_CONTAINER" > "$AUDIT/old-container-inspect.json"
docker logs --tail 3000 "$OLD_CONTAINER" > "$AUDIT/old-container-tail.log" 2>&1

docker inspect "$OLD_CONTAINER" --format '{{range .Config.Env}}{{println .}}{{end}}' \
  | grep -E '^(NCCL_|GLOO_|UCX_|FI_|LD_LIBRARY_PATH=)' \
  > "$AUDIT/old-communication-env.txt" || true
```

重点看实际 NCCL 版本、`NCCL_NET`、`NCCL_NET_PLUGIN`、`NCCL_IB_HCA`、网络插件文件、`LD_LIBRARY_PATH`、特殊拓扑文件和挂载。**仅看到了环境变量，尚不能确认相应传输真的被使用；还要看旧运行日志。**

如果旧服务是 systemd、Compose 或其他 supervisor 管理，保存它的配置和恢复命令。后续通过相同管理方式暂停目标服务，避免 `docker stop` 后被上级自动拉起。

### 3.3 填写四机映射表

| Rank | 实际主机名 | 控制 IP | 控制 netdev | 连前一台的物理口/网卡/HCA | 连后一台的物理口/网卡/HCA |
|---|---|---|---|---|---|
| 0 | 待填写 | 待填写 | 待填写 | S3 ↔ S0 | S0 ↔ S1 |
| 1 | 待填写 | 待填写 | 待填写 | S0 ↔ S1 | S1 ↔ S2 |
| 2 | 待填写 | 待填写 | 待填写 | S1 ↔ S2 | S2 ↔ S3 |
| 3 | 待填写 | 待填写 | 待填写 | S2 ↔ S3 | S3 ↔ S0 |

对每条边，再记录双方 IP、子网、MTU、实际速率、RoCE v2 GID、每条 logical rail 的 peer。需要时使用 `ethtool <实际网卡>`、`ibv_devinfo` 和 `/sys/class/infiniband/<HCA>/ports/1/gid_attrs/`。

**G0 通过条件：** 你能说清每根线的两个端点；所有选定节点都能到达 head 的控制 IP；有明确的旧服务恢复方法。若只知道“有人跑成功了”，但自己的四机接线与网络尚未建立，先完成网络检查，不能跳到模型启动。

---

## 4. 阶段 G1：构建独立 ARM64 运行时

### 4.1 固定源码，而不是四台分别安装浮动最新版

本文检查的 SGLang 快照：

```text
e970453b433e1467e22f6d925f270563417540b5
```

这只是本次审查用的源码快照，不是“四 Spark Ring 稳定版”标签。原生 H3 的 Ring/FA 约束及配置均按该源码检查。[S3][S4][S10]

**只在 S0 构建，然后分发同一个镜像。** 若已有一个满足本次源码、ARM64、FA 与模型接口检查的独立 SGLang 镜像，可以复用，但必须记录真实版本和测试结果，不能把 DeepSeek 专用 vLLM 镜像直接当成 SGLang 镜像。

```bash
cd "$HOME/h3-ring-kit"
set -Eeuo pipefail
COMMIT=e970453b433e1467e22f6d925f270563417540b5

# 初次执行。已有源码目录时先检查，不直接覆盖。
test ! -e sglang-src || { echo 'sglang-src 已存在，请先检查'; exit 1; }
git init sglang-src
git -C sglang-src remote add origin https://github.com/sgl-project/sglang.git
git -C sglang-src fetch --depth 1 origin "$COMMIT"
git -C sglang-src checkout --detach FETCH_HEAD
test "$(git -C sglang-src rev-parse HEAD)" = "$COMMIT"
```

### 4.2 检查 CUDA 基础镜像的 ARM64 支持与 digest

附录 Dockerfile 是**本次编写的实验构建文件**，不是声称 NVIDIA/SGLang 已验收的四 Spark 成品镜像。其 CUDA 大版本与该快照的依赖保持一致，实际依赖由该快照的 `pyproject.toml` 解析。[S9][S10]

```bash
BASE_TAG=nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04

docker buildx imagetools inspect "$BASE_TAG" \
  | tee /srv/h3-ring/manifests/cuda-image-manifest.txt

# 只有 manifest 中存在 linux/arm64，才继续。
docker pull --platform linux/arm64 "$BASE_TAG"
docker image inspect "$BASE_TAG" --format '{{.Architecture}} {{json .RepoDigests}}'

# 记录并使用实际拉取到的 digest，避免后续 tag 漂移。
BASE_PIN="$(docker image inspect "$BASE_TAG" --format '{{index .RepoDigests 0}}')"
test -n "$BASE_PIN"
printf '%s\n' "$BASE_PIN" > /srv/h3-ring/manifests/base-image.txt
```

如果镜像 manifest、ARM64 包或 CUDA/驱动兼容检查失败，停止构建阶段。不要静默换为 `linux/amd64` 模拟执行，也不要未经回退准备升级四台主机驱动。

### 4.3 构建并做依赖检查

如果前面命令在新的终端执行，先从 `/srv/h3-ring/manifests/base-image.txt` 重新读取 `BASE_PIN`，不要依赖另一个终端中的变量。

```bash
cd "$HOME/h3-ring-kit"
set -Eeuo pipefail
BASE_PIN="$(cat /srv/h3-ring/manifests/base-image.txt)"

docker build --platform linux/arm64 \
  --build-arg BASE="$BASE_PIN" \
  -t local/h3-ring:e970453b433e . \
  2>&1 | tee /srv/h3-ring/logs/image-build.log

docker run --rm local/h3-ring:e970453b433e python -m pip check

docker image inspect local/h3-ring:e970453b433e \
  --format '{{.Id}}' > /srv/h3-ring/manifests/runtime-image-id.txt

docker run --rm local/h3-ring:e970453b433e cat /opt/h3-build-freeze.txt \
  > /srv/h3-ring/manifests/runtime-pip-freeze.txt
```

依赖中存在版本区间，Rust 安装器也会选择构建时的工具链。因此 **源码 SHA 不等于整个构建可被永久逐字节重现**；本轮通过“构建一次、保存镜像、所有节点使用相同 image ID”保证实验一致。镜像、freeze、构建日志及基础镜像 digest 都应保留。

若出现依赖冲突、缺少 ARM64 wheel 或源代码编译失败，把首个真实错误保存在日志中；根据该依赖的上游修复选择明确的新快照重新建镜像。**不要用 `--no-deps`、删验证器或随意混装不同 CUDA wheel，把失败伪装成安装完成。** 构建通过不等于 GPU 内核通过。

### 4.4 分发镜像和工具包

```bash
# S0 执行。
docker save local/h3-ring:e970453b433e \
  -o /srv/h3-ring/images/h3-ring-runtime.tar
sha256sum /srv/h3-ring/images/h3-ring-runtime.tar \
  > /srv/h3-ring/manifests/runtime-tar.sha256
```

将工具目录、镜像 tar 和清单通过已经可用的 SSH/IP 路径传到 S1–S3。每次把 `USER@S1_CONTROL_IP` 换成真实目标；不使用 `--delete`。

```bash
# 在 S0，逐台重复；目标目录先创建并确认权限。
rsync -av --partial --progress /srv/h3-ring/images/h3-ring-runtime.tar \
  USER@S1_CONTROL_IP:/srv/h3-ring/images/
rsync -av --exclude=sglang-src --exclude=cluster.env --exclude=node.env \
  "$HOME/h3-ring-kit/" USER@S1_CONTROL_IP:~/h3-ring-kit/
```

在每台目标机执行：

```bash
sha256sum /srv/h3-ring/images/h3-ring-runtime.tar
# 人工对照 S0 的哈希后：
docker load -i /srv/h3-ring/images/h3-ring-runtime.tar
docker image inspect local/h3-ring:e970453b433e --format '{{.Id}} {{.Architecture}}'
```

**四台 image ID 必须相同，Architecture 必须为 arm64。** 哈希一致仅验证分发一致，不代表镜像已经通过运行验收。

---

## 5. 阶段 G2：配置与不加载权重的内核验收

### 5.1 四台分别填写配置

```bash
cd "$HOME/h3-ring-kit"
test -f cluster.env || cp cluster.env.example cluster.env
test -f node.env || cp node.env.example node.env
```

`cluster.env` 中替换 head 的可达 IPv4 地址、真实 image ID 和模型宿主机路径。`node.env` 中分别填写 rank 0/1/2/3、**实际控制网卡**及该节点的 HCA。接口名不带空格。

`EXPECTED_IMAGE_ID` 应直接取 `docker image inspect ... --format '{{.Id}}'` 的完整 `sha256:...`，不是模型 SHA，也不是 registry manifest digest。

关键网络设置的含义：

| 设置 | 用途 | 常见误用 |
|---|---|---|
| `MASTER_ADDR` | 所有选中节点到达 S0 的初始化地址 | 写成各自的 `127.0.0.1` |
| `CONTROL_IFACE` | TCP bootstrap / Gloo 的本地网卡 | 写一个已 DOWN 的管理口 |
| `NCCL_HCA` | 本节点允许 NCCL verbs 使用的设备 | 把别的机器 HCA、IP 地址或物理口序号混在一起 |
| `NETWORK_MODE=ib` | 强制原生 `NCCL_NET=IB` | 误以为 IB 表示物理 InfiniBand 交换网络 |
| `NETWORK_MODE=socket` | 明确强制 TCP 对照实验 | 将它生成的视频报告为 RoCE 成功 |

启动脚本为 NCCL 的接口精确匹配加 `=`；Gloo 的接口值不加这个前缀。GID 不默认写死为 3；需要固定时必须逐 HCA 核对 RoCE v2 GID，且明确记录为什么禁用了当前版本的自动选择。[S6]

**自定义插件边界：** 该辅助启动器只实现原生 IB 与 Socket 两种模式。如果 DeepSeek 成功依赖自定义 `libnccl-net`、代理转发或特殊拓扑插件，必须在新镜像里完成该插件的 ABI、NCCL 版本、加载路径和多进程测试；只复制 `.so` 或环境变量不算完成移植。本文没有伪造一组“万能 Ring RDMA 参数”。

### 5.2 暂停占用 GPU/内存的原服务

先确认没有尚未完成的 DeepSeek 请求，记录恢复命令，再通过它的管理器暂停。普通独立 Docker 容器可使用：

```bash
# OLD_CONTAINER 必须是你在第 3 节确认的真实目标。
docker stop --time 60 "$OLD_CONTAINER"
```

四台都要检查 `nvidia-smi`、`free -h` 和进程列表。不要使用 `pkill python`、`killall`、停止全部容器或删除镜像来释放资源。

### 5.3 四台分别运行 FA 小测试

```bash
cd "$HOME/h3-ring-kit"
# 内核测试本身没有跨机通信，可先使用 socket 配置排除 RDMA 设备挂载因素。
# 将 node.env 中 NETWORK_MODE 暂时设为 socket 后：
bash scripts/run_node.sh kernel
```

测试实际调用所固定 SGLang 的 FlashAttention varlen 接口，采用原生 H3 配置中的 head 数与 head dimension，检查 FP16/BF16 输出及 LSE 与小型 FP32 参考计算是否一致。不会加载 H3 权重。[S3][S11]

**通过：** 四台都输出 `PASS_KERNEL_ONLY`，GPU/架构符合预期，输出与 LSE 有限且数值误差在脚本阈值内。

**失败即停：** 无法导入 FA、SM121 不支持、`no kernel image`、不返回 LSE、数值异常。不要改成 cuDNN 或 torch_sdpa 继续称其为同一条 H3 Ring4 路线。当前原生 H3 在 `ring_degree > 1` 时明确要求 FA。[S3]

这项检查仍不覆盖完整 H3 的所有 token shape、FP8 GEMM、VAE 内核或多节点 Ring attention；后面真实模型请求仍然必需。

### 5.4 保存本镜像的真实 CLI

```bash
bash scripts/run_node.sh help
```

在生成的日志中确认存在本文使用的参数：`--num-gpus`、`--nnodes`、`--node-rank`、`--dist-init-addr`、`--tp-size`、`--sp-degree`、`--ulysses-degree`、`--ring-degree`、`--encoder-parallel`、`--attention-backend`、`--quantization`、`--use-fsdp-inference`、`--enable-torch-compile`。

若参数缺失，先查镜像源码是否真是固定快照；不要凭别的框架的参数名修补启动命令。

---

## 6. 阶段 G3/G4：证明网络能承担本次通信

### 6.1 先逐物理边验证

在已有正确 IP、MTU、RoCE v2 GID 配置的前提下，对 E01/E12/E23/E30 的每条选用 rail 分别测试，两端都核对设备与端口。

以下只是一个实际 perftest 命令模板；先运行本机 `ib_write_bw --help` 确认该版本选项。

```bash
# 接收端：替换本端实际 HCA 和 GID index。
ib_write_bw -d ACTUAL_HCA -i 1 -x ACTUAL_GID_INDEX \
  -F --report_gbits -D 10 -s 65536

# 发送端：替换本端实际 HCA/GID，最后一个参数是直连 peer 的实际 IP。
ib_write_bw -d ACTUAL_LOCAL_HCA -i 1 -x ACTUAL_LOCAL_GID_INDEX \
  -F --report_gbits -D 10 -s 65536 PEER_EDGE_IP
```

交换发送/接收端再测一次。首先确认连接与数据无错误；不要把某个理论端口速率写成应当达到的实际吞吐。这里默认使用的 buffer 不一定是 GPU memory，所以 **perftest 通过不等于 GPU→GPU RDMA 或 H3 通过**。[S12]

不要默认添加 `--use_cuda_dmabuf`、`NCCL_NET_GDR_LEVEL` 等“加速”选项：其支持取决于当前 perftest、驱动和内核。先以系统实际可用的路径建立基线，再单独测 GPU 通信。[S6][S12]

### 6.2 Socket 对照实验，单独留档

在参与节点 `node.env` 中明确设 `NETWORK_MODE=socket`，然后在两台相邻机器的两个终端分别执行同一个命令，rank 来自各自 `node.env`：

```bash
# S0、S1 各执行一次；两台都启动，不能只运行 head。
bash scripts/run_node.sh comm 2 ring
bash scripts/run_node.sh comm 2 collectives
```

每个 suite 都要等两台结束后再切换下一个。随后四台同时按各自 rank 执行：

```bash
bash scripts/run_node.sh comm 4 ring
bash scripts/run_node.sh comm 4 collectives
```

这样可以先检查 rank、四机 TCP 可达性、容器网络以及 launcher 是否正确。**Socket 通过只记 `PASS_TCP`，绝不能当成高性能数据面验收。**

### 6.3 原生 IB / RoCE 实验

四台切回 `NETWORK_MODE=ib`，填写各自核对过的 `NCCL_HCA`。然后按同样顺序运行：

```bash
# 先仅 S0、S1：
bash scripts/run_node.sh comm 2 ring
bash scripts/run_node.sh comm 2 collectives

# 双机都通过后，再四台：
bash scripts/run_node.sh comm 4 ring
bash scripts/run_node.sh comm 4 collectives
```

`ring` 测试两个方向的相邻 GPU send/recv；`collectives` 检查 all-reduce、all-gather 和以每个 rank 为源的 broadcast。数据量分级增长，检查收到的内容，而不仅是进程“没有报错”。脚本中的计时包含校验和同步，**不是 NCCL 标准带宽跑分**。

还可测试：

```bash
# 四台；专门检查包括 0↔2、1↔3 在内的任意 peer。
bash scripts/run_node.sh comm 4 allpairs
```

`allpairs` 是补充覆盖，不表示 H3 每一步都要求所有 peer 直接相连。若它失败，而 Ring 与本轮所需 collective 均通过，应记录“非相邻 P2P 未支持”，继续真实 H3 测试时重点观察是否调用该类通信；不能因此宣称网络具有通用四机 RDMA 能力。

### 6.4 怎样确认没有偷偷落到 TCP

每次运行日志目录包含 `launch.txt`、`stdout.log`、`nccl.<host>.<pid>.log`。检查所有 rank，不只检查 S0。

```bash
# 将 RUN_LOG_DIR 换成同一次测试在本机的实际日志目录。
grep -E 'NET/IB|NET/Socket|Using network|GID|HCA|WARN|ERROR' \
  "$RUN_LOG_DIR"/*.log
```

`NCCL_NET=IB` 用于要求原生 verbs 传输；如果其不可用，应失败，而不是自动成为一个成功的 Socket 结果。不过仅出现 `NET/IB` 初始化行，也不足以证明所有实际数据都达到了期望的 GPU Direct 或带宽。[S6]

同时记录选用高速网卡与管理口的字节计数变化：

```bash
# 将网卡名单换为本机实际接口，测试前、后各保存一次。
for IFACE in ACTUAL_FAST_IFACE_A ACTUAL_FAST_IFACE_B ACTUAL_CONTROL_IFACE; do
  echo "[$IFACE]"
  cat "/sys/class/net/$IFACE/statistics/rx_bytes"
  cat "/sys/class/net/$IFACE/statistics/tx_bytes"
done
```

RoCE offload 的流量不一定完整反映在普通 netdev 计数；还应参考网卡 RDMA/硬件计数器及日志。不能以“管理口计数少”作为单独证据。停止其他大流量任务后再测，避免把权重复制流量当成模型通信。

### 6.5 网络未通过时，应该停在哪

| 现象 | 优先判断 | 不应做什么 |
|---|---|---|
| 控制 IP 不通 | 地址、路由、接口、防火墙 | 先下载大模型来碰运气 |
| Socket 通，IB 不通 | HCA/GID、RoCE 可达性、verbs/runtime/插件差异 | 把 Socket 成果标为 RoCE |
| 每条边 RDMA 通，四机 collective 卡住 | 稀疏物理拓扑与 NCCL 建连/选路不匹配 | 认为 `ip_forward=1` 会自动修复 RDMA |
| Ring 小包通，大包超时 | MTU、丢包、拥塞、资源、buffer/注册问题 | 只靠无限增大 timeout 掩盖 |
| 旧 DeepSeek 通，新镜像不通 | NCCL、插件、挂载、环境与通信模式不一致 | 删除旧环境重新装全部东西 |
| 只有一台启动或 rank 重复 | launcher 配置 | 反复 POST 新视频任务 |

物理 Ring 中，Linux 内核转发普通 IP 包与 NIC 终止 RoCE QP 是不同问题。某个 HCA/GID 的报文能否经其他节点到达正确远端设备，需要具体路径验证，不能由 ping 推断。[S5][S6][S12]

**此处是方案最重要的工程关口。** 如果四节点所需 RDMA 通信无法通过，这份方案已定位到网络/运行时集成障碍；不是继续改一个 H3 `ring-degree` 就能解决。可以保留 TCP 功能对照，或退回相邻双 Spark 的实验，不对四机高速方案作成功声明。

---

## 7. 阶段 G5：锁定、下载、复用、分发模型

### 7.1 在 S0 建一个下载工具环境

```bash
python3 -m venv /srv/h3-ring/download-venv
/srv/h3-ring/download-venv/bin/python -m pip install --upgrade huggingface_hub
/srv/h3-ring/download-venv/bin/python -m pip freeze \
  > /srv/h3-ring/manifests/download-tools.txt
```

权重许可证、下载权限以官方模型页面为准。需要 Token 时使用 Hugging Face 的登录方式或仅在当前环境设置 Token，不写入脚本、镜像 Dockerfile 或要分发的 `.env`。[S1]

### 7.2 只制定清单，不立即下载

```bash
cd "$HOME/h3-ring-kit"
/srv/h3-ring/download-venv/bin/python scripts/model_download.py \
  --dest /srv/h3-ring/models/MiniMax-H3 \
  --lock /srv/h3-ring/manifests/fl2va-model-lock.json \
  --variant FL2VA
```

脚本在首次执行时解析 HF commit，写入固定 revision、下载分区、文件大小与清单；后续复用该 lock，不自动追踪 main。先查看 `scope_GiB`、`df -h` 和已存在的本地资源。

本次只取根 `model_index.json`、`FL2VA/*` 和少量元数据；不会把两个任务分区、所有 diffusers 格式以及额外模型全部下载一遍。SGLang 所需的原始分区不能随意替换成同名转换包。[S1]

### 7.3 复用旧文件的规则

已下载官方分区时，先核对其 revision/Hub 元数据、组件结构及实际文件。满足同一目标版本后，可以让 `MODEL_HOST_PATH` 指向已存在的只读模型根，或复制到新目录。

**不能仅根据“文件名相同、大小差不多”认定模型版本一致。** 本地 SHA256 清单用于确认分发副本一致；它本身不能把一个来源不明的旧目录认证成官方正确版本。不能把旧量化转换权重套上官方 BF16 的 lock。

示例只做普通增量复制，不删除旧文件，不制造需要修改的共享硬链接：

```bash
rsync -av --partial /ACTUAL_EXISTING_MODEL_ROOT/ \
  /srv/h3-ring/models/MiniMax-H3/
```

如果来源是 Hugging Face cache 的符号链接目录，先确认链接是否在目标机器可解析。跨机分发应复制真实内容，或复制完整 cache 布局；不要发送一堆指向原机器路径的断链。

### 7.4 下载缺失部分

```bash
/srv/h3-ring/download-venv/bin/python scripts/model_download.py \
  --dest /srv/h3-ring/models/MiniMax-H3 \
  --lock /srv/h3-ring/manifests/fl2va-model-lock.json \
  --variant FL2VA --download
```

下载完成后检查目录，必须保留官方 processor/tokenizer 与特殊 token，不能替换成普通 Qwen3-VL tokenizer。[S1]

```text
/srv/h3-ring/models/MiniMax-H3/
├── model_index.json
└── FL2VA/
    ├── model_index.json
    ├── processor/
    ├── tokenizer/
    ├── text_encoder/
    ├── transformer/
    ├── visual_vae/
    └── audio_vae/
```

### 7.5 固化文件哈希并分发

```bash
python3 scripts/manifest.py make \
  /srv/h3-ring/models/MiniMax-H3 \
  /srv/h3-ring/manifests/fl2va-files.sha256.json

# 目标目录先创建；逐台替换目标 SSH 地址。
# --copy-links 将已经确认的符号链接复制为实际内容。
rsync -av --copy-links --partial --progress --exclude=.cache \
  /srv/h3-ring/models/MiniMax-H3/ \
  USER@S1_CONTROL_IP:/srv/h3-ring/models/MiniMax-H3/

rsync -av /srv/h3-ring/manifests/fl2va-model-lock.json \
  /srv/h3-ring/manifests/fl2va-files.sha256.json \
  USER@S1_CONTROL_IP:/srv/h3-ring/manifests/
```

S1–S3 分别验证：

```bash
cd "$HOME/h3-ring-kit"
python3 scripts/manifest.py verify \
  /srv/h3-ring/models/MiniMax-H3 \
  /srv/h3-ring/manifests/fl2va-files.sha256.json
```

哈希会完整读取权重，属于 I/O 工作；不要与正式速度测试同时进行。任一节点文件不一致就停止，先修复副本，避免将文件损坏误判为分布式内核问题。

---

## 8. 阶段 G6：单机 H3 基线

### 8.1 确认初始配置

S0 的 `node.env`：`NODE_RANK=0`。`cluster.env`：`MODEL_VARIANT=fl2va`、`QUANTIZATION=fp8`。首轮不运行其他占用统一内存的大模型。

```bash
cd "$HOME/h3-ring-kit"
bash scripts/run_node.sh serve 1
```

辅助脚本会使用全套本地模型、只读挂载和 offline 标记，禁止Hugging Face 模型加载阶段补下载；这些环境变量不是整机网络隔离或防火墙规则。若模型配置仍引用缺少的外部组件，会明确失败；应补齐同一 revision 的本地依赖，而不是不记录来源地改回在线加载。

查看启动日志中模型 variant、量化应用对象、attention backend 和实际 residency/offload 设置。本文的性能基线是 speed 模式；如果运行时自动启用 offload，应单独记录该配置，不要把它和 resident 结果混在一起。**不需要为了“强行成功”同时开启 CPU offload、FSDP、编译和量化。**

### 8.2 检查 API 与参数是否被接受

在 S0 的另一个终端：

```bash
curl --fail --silent http://127.0.0.1:30021/openapi.json \
  -o /srv/h3-ring/manifests/h3-openapi.json
```

核对存在 `POST /v1/videos`、任务查询与 `/content` 下载接口；确认请求中的 `task`、`conditions`、`target`、`seed`、`num_inference_steps`、`quality` 与本镜像一致。若某字段被接口放入额外参数或验证器明确拒绝，以该快照的真实 schema 为准并记录修正，不能让它被静默忽略后仍按“5 步”记账。[S2][S13]

### 8.3 提交冒烟任务

```bash
python3 scripts/video_client.py \
  --request requests/smoke.json \
  --out /srv/h3-ring/output/single-smoke.mp4
```

请求为 4 秒、480 短边、5 步，只检查编码→去噪→音视频 VAE→封装→下载链路。**这不是质量样片，也不是官方最优生成参数。** 如果低分辨率被该快照明确拒绝，保留错误，改用官方 768 短边并记录真实尺寸，不能悄悄继续沿用 480 的性能标签。

### 8.4 完整音视频解码

下面使用宿主机的 ffprobe/ffmpeg；尚未安装时，可以使用同一个运行时镜像中的工具，无需改动宿主机包。

```bash
ffprobe -v error -show_streams -show_format -of json \
  /srv/h3-ring/output/single-smoke.mp4 \
  > /srv/h3-ring/output/single-smoke.ffprobe.json

ffmpeg -v error -i /srv/h3-ring/output/single-smoke.mp4 \
  -f null -
```

宿主机没有上述工具时，等价检查方式：

```bash
docker run --rm --network none \
  --mount type=bind,src=/srv/h3-ring/output,dst=/out,readonly \
  local/h3-ring:e970453b433e \
  ffprobe -v error -show_streams -show_format -of json /out/single-smoke.mp4

docker run --rm --network none \
  --mount type=bind,src=/srv/h3-ring/output,dst=/out,readonly \
  local/h3-ring:e970453b433e \
  ffmpeg -v error -i /out/single-smoke.mp4 -f null -
```

要求同时存在视频流和音频流，完整解码没有报错，时长合理，输出不为空。播放检查并听声音；一个可以读取 header 的 MP4 并不足够。正式 H3 的目标音频规格应按官方模型定义和真实输出核验，而不是单凭后缀判断。[S1]

### 8.5 50 步质量基线

```bash
python3 scripts/video_client.py \
  --request requests/quality.json \
  --out /srv/h3-ring/output/single-quality.mp4
```

本轮请求使用 50 步和 768 短边。`quality="lossless"` 是本框架对运行时质量策略的控制，**不表示 FP8 权重数学无损**；不要改为 `quality="high"`，该选项在当前 H3 源码中对特定 H200 部署作了严格审计限制，不是一个通用“效果更好”开关。[S2][S3]

**G6 通过条件：** 真实视频和音频正常；模型组件、FP8、FA、采样步数与日志一致；没有 OOM、非有限值或持续 swap 抖动。若单机加载/生成失败，先解决单机内核或峰值内存，再扩到 Ring4。Ring4 不会自动救活一个装不下完整组件的单节点。

---

## 9. 阶段 G7：相邻双机 Ring2

先完成当前任务，再结束 S0 单机服务；确认模型进程已退出、内存已释放。不要同时启动单机和多机服务。

在 S0、S1 的两个终端中分别执行：

```bash
cd "$HOME/h3-ring-kit"
bash scripts/run_node.sh serve 2
```

两台的命令相同，`node.env` 中的 rank 不同。实际组合应为：

```text
world=2；nnodes=2；每台 local GPU=1
TP1 × Ulysses1 × Ring2
encoder-parallel=replicate
```

这不是社区双 Spark Ulysses2 示例的原样复现；它是本次四机实验的缩小版。现有公开双 Spark vLLM recipe 与它的后端、分工可能不同，不能把别人的速度直接当成这个组合的预期值。[S2][S7][S14]

在 S0 提交同样两个请求，输出文件名改为 `two-smoke.mp4`、`two-quality.mp4`。检查两个节点日志，确认 rank 0/1 都实际参与同一个作业，而非一个节点退化为独立服务。

**G7 通过条件：** 与单机相同的请求可以生成、全量解码并通过基本视听检查；两个 rank 的通信及内存日志正常。若使用 Socket，只能记为双机 TCP 功能通过；必须对 IB 模式重新验收，才能记录双机 RoCE 通过。

---

## 10. 阶段 G8：四机 Ring4

### 10.1 停止双机服务，四台各启动一次

四台 `NODE_RANK` 必须分别为 0、1、2、3，且按物理环顺序映射。四台配置相同的 image ID、模型内容、head 地址、端口与并行度。

```bash
# S0、S1、S2、S3 各执行一次：
cd "$HOME/h3-ring-kit"
bash scripts/run_node.sh serve 4
```

辅助脚本最终调用的核心参数等价于：

```bash
sglang serve \
  --model-path /models/h3 \
  --model-variant fl2va \
  --num-gpus 4 \
  --nnodes 4 \
  --node-rank YOUR_NODE_RANK \
  --dist-init-addr NODE0_CONTROL_IP:20021 \
  --tp-size 1 \
  --sp-degree 4 \
  --ulysses-degree 1 \
  --ring-degree 4 \
  --encoder-parallel replicate \
  --attention-backend fa \
  --quantization fp8 \
  --performance-mode speed \
  --enable-torch-compile false \
  --use-fsdp-inference false \
  --host 127.0.0.1 \
  --port 30021
```

`--num-gpus 4` 在这条 SGLang 多节点路径里是全局 GPU 总数；不是要求单台 Spark 拥有四个 GPU。**每台运行一次 `sglang serve` 即可，不要再给这个命令外套一个启动四进程的 torchrun。** torchrun 只用于本文独立的通信测试脚本。[S2][S4]

上游的拓扑静态约束允许该类配置，并不代表网络和 GB10 内核都已被证明正确。本轮使用 TP1/U1/R4，避免把跨主机的 Ulysses all-to-all 也同时引入。[S2][S4]

### 10.2 先冒烟，再质量，不要立即生成长片

仅在 S0 提交：

```bash
python3 scripts/video_client.py \
  --request requests/smoke.json \
  --out /srv/h3-ring/output/four-smoke.mp4

python3 scripts/video_client.py \
  --request requests/quality.json \
  --out /srv/h3-ring/output/four-quality.mp4
```

重复第 8 节的完整解码流程。确认没有只产视频不产音频、黑帧、持续静止、严重花屏、无穷值或损坏输出。

### 10.3 四机成功需要哪些证据

| 证据 | 通过标准 |
|---|---|
| 运行时与模型 | 四台相同 image ID、相同模型 SHA256 清单 |
| 并行参数 | 四个唯一 rank；world4；TP1/U1/R4；replicate；FA |
| 同一个任务 | head 的作业 ID、时间窗口与四个 worker 去噪日志对应 |
| 音视频正确性 | 完整解码、视听检查、合理时长和输出规格 |
| 通信 | 本次作业的真实 transport、所用设备与计数记录完整 |
| 内存与稳定性 | 无 OOM、无 silent worker death、无持续 swap 崩溃式变慢 |
| 重复性 | 同一配置至少连续完成 3 个正式请求 |

“每台 GPU 都很忙”不是充分证据；四个独立服务也会让四台都繁忙。“HTTP 200”也不是充分证据；它可能只是任务入队。

### 10.4 必须区分的结果名称

```text
PASS_TCP_FUNCTIONAL
    四机通过普通 TCP 完成一个视频；未证明高速 RDMA。

PASS_RDMA_FUNCTIONAL
    四机本次作业走已验证的 RDMA 路径，输出验收通过。

PASS_PERFORMANCE
    在相同条件下，预热后重复测试显示具有实际收益。
```

功能通过、传输通过、性能通过分别记录。四机可能功能正确但比双机慢；这仍是有价值的实验结果，不应“优化”标签来掩盖。

---

## 11. 性能测试：怎样判断四台值不值得用

### 11.1 比较条件保持一致

保持同一个模型 revision、量化范围、prompt、seed、short edge、duration、step 数、后端、offload/cache 状态和输出编码参数。分别测单机、双机、四机；不能把单机 50 步与四机 5 步相比较。

初次模型加载、FP8 转换、JIT/内核编译与第一次生成单独记录。正式比较使用预热后的请求，每种配置至少 3 次，记录中位数和最慢一次，不仅挑最快一次。

```text
单请求加速比 S2 = 单机相同条件耗时 / 双机相同条件耗时
单请求加速比 S4 = 单机相同条件耗时 / 四机相同条件耗时
```

也记录 encoder、denoise、VAE decode、封装/写盘及完整 API wall time。`video_client.py` 的 wall time 包括轮询和下载，不应与别人仅统计 denoise 的数字混用。

### 11.2 建议的记录表

```csv
run_id,model_revision,image_id,variant,quantization,world,tp,ulysses,ring,transport,short_edge,duration_s,steps,seed,cold_or_warm,encoder_s,denoise_s,vae_s,api_wall_s,node0_peak_memory_gib,node1_peak_memory_gib,node2_peak_memory_gib,node3_peak_memory_gib,decode_ok,visual_audio_ok,notes
```

GB10 上结合 `nvidia-smi` 与主机 `MemAvailable` 观察；某些 NVIDIA 查询字段不支持时留空或换用支持的字段，不能把 `N/A` 当成 0。

```bash
# 四台分别执行，使用实际日志路径；另开终端，Ctrl-C 结束。
nvidia-smi --query-gpu=timestamp,name,utilization.gpu,memory.used,memory.total,temperature.gpu \
  --format=csv -l 1

# 另一个终端监控主机内存及 swap。
vmstat 1
```

作为本次实验的人为保护线，可将 `MemAvailable` 持续低于约 8GiB、系统出现大量 swap、某 rank 无响应或出现 OOM killer 作为停止信号。这不是厂商规定的通用阈值，需要结合实际系统开销判断。

### 11.3 质量比较要看什么

查看人物/物体一致性、手部动作、接触关系、运动连续性、镜头运动、细节闪烁、音画事件同步与对白清晰度。提示词、seed 一样并不保证单机和 Ring 的视频逐像素一致；并行顺序变化会影响浮点累积。评价应该围绕可见质量与任务约束，而不是仅比较文件哈希。[S2]

**不预填“预计 4×”或每秒生成多少帧。** 本次没有四 Spark 实机数据。若双机最快、四机没有收益，就保留双机单任务模式；四台做两个独立双机队列是另一项吞吐设计，不是本次 Ring4 单视频加速。

---

## 12. 扩展到图片驱动与角色参考

### 12.1 FL2VA：无需换分区，先做一张首帧

T2VA 与首/尾帧 FL2VA 使用同一 `FL2VA/` 分区。基础四机通过后，再增加输入素材，不需要重新下载另一整套模型。[S1]

按已核对的原生请求结构，图像条件使用 `type=image`、`role=keyframe`、`frame_index=0`，并提供框架支持的 `uri`。例如将 `requests/quality.json` 改为新文件：

```json
{
  "task": "fl2va",
  "prompt": "保持首帧中成年角色的身份、衣着和场景。角色缓慢转头看向窗外，镜头小幅向前移动，环境声音自然。",
  "conditions": [
    {"type": "image", "uri": "https://YOUR_OWN_ASSET_HOST/hero-start.png", "role": "keyframe", "frame_index": 0}
  ],
  "target": {"short_edge": 768, "aspect_ratio": "16:9", "duration_seconds": 4},
  "seed": 17,
  "num_inference_steps": 50,
  "quality": "lossless"
}
```

上面 URI 是**必须替换的示例**，不是现成可用的图片。只使用你拥有权利且节点可以读取的素材。先对照该版本的输入解析器验证本地文件/data URI 支持，再切到完全本地素材路径；本文不假造一个未经核对的 `file://` 白名单参数。使用 HTTPS 素材时，模型推理仍在本地，但素材获取阶段存在外部网络访问，不能称为完全离线。[S13]

### 12.2 Ref2VA：新增官方分区，保持其他变量不变

```bash
cd "$HOME/h3-ring-kit"
REV="$(python3 -c 'import json; print(json.load(open("/srv/h3-ring/manifests/fl2va-model-lock.json"))["revision"])')"

# 先计划；检查新增空间后，把 --download 加到同一条命令再执行。
/srv/h3-ring/download-venv/bin/python scripts/model_download.py \
  --dest /srv/h3-ring/models/MiniMax-H3 \
  --lock /srv/h3-ring/manifests/ref2va-model-lock.json \
  --variant Ref2VA --revision "$REV"
```

如果该固定 revision 没有需要的分区，使用独立目录与新 lock 规划升级，不覆盖已通过的 FL2VA 快照。

新增分区后重新生成一个**新文件名**的总目录哈希清单，再分发并验证。旧 FL2VA-only manifest 会因目录新增文件而不再匹配整个目录，不能直接拿它验收扩展后的根目录。

停掉当前服务，将四台 `cluster.env` 中的 `MODEL_VARIANT` 改为 `ref2va`，保持 TP1/U1/R4 和其他参数不变，再从一个简单参考输入开始。请求改为 `task=ref2va`，参考条件使用 `role=reference`。不同分区不保证相同内存峰值，Ref2VA 需要重新验收。[S1][S2]

首轮不要同时安装 Heretic encoder 或 Turbo。若后来对比第三方修改，应单独锁定资源、保持官方回退、重新验收质量与许可；不能把修改编码器等同于 H3 生成主体“无损换版”。

### 12.3 完全本地系统的边界

本地 H3-Base 并不自动包含官方整套托管的 Context-IR 与 Regenerate-2K 工作流。本文生成的是本地 Base 音视频；后期放大也不能直接宣称等同于官方 2K 再生成。[S1]

---

## 13. 常见故障与处理顺序

| 故障 | 首先收集 | 处理方向 |
|---|---|---|
| Docker 构建失败 | 第一处依赖/编译错误、CPU 架构、源码 SHA | 修复 ARM64 与依赖一致性；不继续下载权重 |
| FA 小测试失败 | torch/CUDA/FA 版本、GPU capability、完整堆栈 | 修复 SM121 支持与 LSE 路径；不用 cuDNN 冒充 Ring |
| FP8 首次加载失败 | 出错组件、量化类型、峰值内存、kernel 名称 | 区分装载峰值与 FP8 GEMM 不支持；只改一个变量 |
| 单台 OOM，期望 Ring4 自动解决 | 每个组件驻留、模型分片设置 | 先承认这是复制/序列并行路线；不是 4 份权重分片 |
| 多节点卡初始化 | rank、控制 IP、监听端口、四台日志 | 查通信 G3/G4，不重复提交任务 |
| 到第一步 attention 才卡 | 实际 FA 内核、NCCL P2P、不同 token shape | 小测试覆盖有限，需要真实 shape 复现 |
| 解码时 OOM | VAE dtype、分辨率、时长、其他组件驻留 | 先缩短输入/输出，核对该框架的正式 VAE/offload 选项 |
| 视频无音频 | audio VAE 文件与加载日志、输出 streams | 不把纯视频成功当完整 H3 成功 |
| 四机较慢 | encoder/denoise/VAE 分项、实际 transport、网卡字节 | 先排除 Socket、rank/HCA 选路和冷启动，再优化 |
| 客户端超时 | 已保存 job ID、server 状态 | 查询/恢复轮询；不要自动重复 POST 生成新任务 |

不建议首轮使用 FastH3/VDN/VSA/稀疏替换、跨节点 Ulysses、FSDP、编译、缓存和复杂 LoRA 叠加。有些 H3 attention 变体在源码中明确不支持 Ring>1；它们不是可随意替换的同一个后端。[S3]

---

## 14. 停止实验与恢复原环境

### 14.1 只停止本次 H3 容器

先等任务完成，或明确记录为中断。在每台机器按本次 action/rank 停止对应容器：

```bash
# S0 示例；S1/S2/S3 分别替换 r1/r2/r3。
docker stop --time 60 h3-ring-serve-r0
```

通信测试卡住时停止对应 `h3-ring-comm-rN`；内核测试容器为 `h3-ring-kernel-rN`。脚本使用 `--rm`，退出后容器会移除，但日志在宿主机 `/srv/h3-ring/logs`，模型与镜像不会删除。

确认四台都没有残留 H3 GPU 进程，再恢复旧服务。普通独立 Docker 容器可以：

```bash
docker start "$OLD_CONTAINER"
```

若旧服务由 Compose/systemd 管理，则使用第 3 节记录的对应恢复命令。随后运行一次原有 DeepSeek 的健康检查和小请求，确认旧网络及服务真的恢复。

### 14.2 网络回退

主线方案没有修改网络，无需为了 H3 回退去重写 netplan。

若你使用附录 B 的新实验地址/路由，必须按实验前记录撤销**仅本次增加**的地址和 `/32` 路由，恢复原 `ip_forward`、`rp_filter` 值，再验证旧模型。不要覆盖整份 netplan，除非确实审查了当前与备份的差异且具备本地恢复通道。

**禁止作为“清理步骤”使用：** `docker system prune -a`、删除全部 HF cache、删除旧模型目录、`ip addr flush`、清空 iptables/nftables、批量杀 Python 进程。它们会扩大故障范围，且本次实验不需要。

---

## 15. 建议保存的最终验收报告

将以下内容放入一个新的 `acceptance-日期/` 目录：硬件/驱动盘点、接线表、源码 SHA、镜像 ID/digest、pip freeze、模型 lock 与哈希、各级通信日志、启动命令、完整请求与 job ID、真实输出、ffprobe 结果、四台内存曲线及同条件测速表。

最终报告用这种写法：

```text
模型/分区：
模型 revision：
运行时 image ID：
SGLang commit：
精度与量化范围：
物理 rank 顺序：
实际 transport：
GPU/CPU 内存峰值：
1→2→4 各阶段结果：
连续正式请求数：
音视频解码与人工检查：
已知限制：
是否具备单请求速度收益：
旧 DeepSeek 回退是否验证：
```

只有证据足够后，才将结果称为“这套固定版本的四 Spark 无交换机 H3 Ring4 已在我的机器上通过”。不要将这个结论扩展成所有 H3 模型分区、所有 NCCL 版本、所有内核/量化方案都能直接运行。

---

## 来源索引

下列资料用于核对事实和接口。本文的检查顺序、目录隔离、辅助脚本、阈值、地址表与验收方法是本次工程设计，不冒充上游原文提供的“四 Spark 一键教程”。链接可能随上游更新；涉及实现的关键链接尽量固定到审查快照。

- **[S1] MiniMax 官方模型卡与原始权重结构：** <https://huggingface.co/MiniMaxAI/MiniMax-H3>
- **[S2] SGLang H3 cookbook，固定快照：** <https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/docs/cookbook/diffusion/MiniMax/MiniMax-H3.mdx>
- **[S3] H3 PipelineConfig：FA/Ring、质量审计、组件精度与变体限制：** <https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/python/sglang/multimodal_gen/configs/pipeline_configs/minimax_h3.py>
- **[S4] H3 拓扑配置器与静态约束：** <https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/docs/src/snippets/configs/MiniMaxAI/minimax-h3.jsx>
- **[S5] NVIDIA DGX Spark ConnectX-7 物理口、netdev 与 RoCE 对应说明：** <https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html>
- **[S6] NVIDIA NCCL 环境变量与传输选择：** <https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html>
- **[S7] vLLM-Omni Spark GB10 recipe；仅作已有单/双 Spark 实证与内存注意事项参照：** <https://github.com/vllm-project/vllm-omni/blob/main/recipes/MiniMaxAI/MiniMax-H3-Spark-GB10.md>
- **[S8] SGLang Diffusion 量化文档：** <https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/docs/docs/sglang-diffusion/quantization.mdx>
- **[S9] SGLang 上游 Dockerfile，CUDA 与构建依赖参照：** <https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/docker/Dockerfile>
- **[S10] 固定快照 Python 依赖：** <https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/python/pyproject.toml>
- **[S11] SGLang FA varlen/LSE 接口与 H3 head 配置：** <https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/python/sglang/multimodal_gen/runtime/layers/attention/backends/flash_attn.py>；<https://github.com/sgl-project/sglang/blob/e970453b433e1467e22f6d925f270563417540b5/python/sglang/multimodal_gen/configs/models/dits/minimax_h3.py>
- **[S12] NCCL 网络排障：** <https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/troubleshooting/networking_troubleshooting.html>
- **[S13] 官方 T2VA/FL2VA/Ref2VA 请求样例：** <https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/scripts/readme/reproducible-768p-t2va-request.sh>；<https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/scripts/readme/reproducible-768p-fl2va-request.sh>；<https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/scripts/readme/reproducible-768p-ref2va-request.sh>
- **[S14] 社区双 Spark 自定义 H3 executor；不应只改节点数字就扩到四台：** <https://github.com/joeynyc/MiniMax-H3-2x-DGX-Spark>

---

## 附录 A：全部运行文件与本地测试

以下文件已同时放入配套 ZIP。仅使用 Markdown 时，可执行第 2 节提取命令还原。**配置模板中的 `CHANGE_ME` 必须替换；工具会拒绝未填写的关键参数。**

下载脚本需要下载工具 venv 中的 `huggingface_hub`；FA 和通信脚本必须在构建完成的 GPU 镜像里运行；API 客户端及哈希工具使用宿主机 Python 标准库即可。

### 文件：`cluster.env.example`

<!-- H3FILE cluster.env.example -->
```bash
# Copy to cluster.env. Use the same image ID and model bytes on all four nodes.
H3_ROOT=/srv/h3-ring
IMAGE=local/h3-ring:e970453b433e
EXPECTED_IMAGE_ID=CHANGE_ME_TO_DOCKER_IMAGE_ID
MASTER_ADDR=CHANGE_ME_TO_NODE0_REACHABLE_IPV4
DIST_PORT=20021
TEST_PORT=29521
API_PORT=30021
API_BIND=127.0.0.1
MODEL_HOST_PATH=/srv/h3-ring/models/MiniMax-H3
MODEL_VARIANT=fl2va
QUANTIZATION=fp8
```

### 文件：`node.env.example`

<!-- H3FILE node.env.example -->
```bash
# Copy to node.env and edit separately on each node.
NODE_RANK=0
CONTROL_IFACE=CHANGE_ME_TO_ACTUAL_INTERFACE
# ib means native NCCL verbs; socket means an explicitly labeled TCP diagnostic.
# A custom transport/plugin is NOT automatically handled by this launcher.
NETWORK_MODE=ib
# Exact local HCA names (leading '='), e.g. '=rocep1s0f0:1,rocep1s0f1:1'.
# The example is NOT an assurance that these HCAs can reach every required peer.
NCCL_HCA=CHANGE_ME_TO_VERIFIED_LOCAL_HCAS
```

### 文件：`Dockerfile`

<!-- H3FILE Dockerfile -->
```dockerfile
# Experimental ARM64 build. Pin BASE to the digest you inspected before building.
ARG BASE=nvidia/cuda:13.0.3-cudnn-devel-ubuntu24.04
FROM ${BASE}
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 python3-dev python3-venv build-essential cmake ninja-build \
    pkg-config protobuf-compiler libssl-dev libffi-dev libnuma-dev \
    git ca-certificates curl ffmpeg libsndfile1 libgl1 libglib2.0-0 \
    libibverbs1 libibverbs-dev ibverbs-providers librdmacm1 librdmacm-dev \
    rdma-core perftest iproute2 ethtool libopenmpi-dev \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/h3env
ENV PATH=/opt/h3env/bin:/root/.cargo/bin:${PATH}
# Keep the downloaded installer in the image for the build audit.
RUN curl --proto '=https' --tlsv1.2 -fsSL https://sh.rustup.rs -o /tmp/rustup-init.sh \
    && sh /tmp/rustup-init.sh -y --profile minimal --default-toolchain stable \
    && rustc --version && cargo --version
COPY sglang-src /opt/sglang-src
WORKDIR /opt/sglang-src
RUN test "$(git rev-parse HEAD)" = e970453b433e1467e22f6d925f270563417540b5
RUN python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install --pre --extra-index-url https://download.pytorch.org/whl/cu130 \
       -e '/opt/sglang-src/python[diffusion]' \
    && python -m pip check \
    && python -m pip freeze --all > /opt/h3-build-freeze.txt
ENV PYTHONUNBUFFERED=1
WORKDIR /kit
CMD ["sglang", "serve", "--help"]
```

### 文件：`.dockerignore`

<!-- H3FILE .dockerignore -->
```text
*
!Dockerfile
!sglang-src/
!sglang-src/**
# Never send configuration, access tokens, model weights, or logs into the build.
```

### 文件：`scripts/run_node.sh`

<!-- H3FILE scripts/run_node.sh -->
```bash
#!/usr/bin/env bash
set -Eeuo pipefail
KIT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
[[ -f "$KIT_DIR/cluster.env" && -f "$KIT_DIR/node.env" ]] || {
  echo 'Create and review cluster.env and node.env first.' >&2; exit 2;
}
# These two configuration files are executable shell syntax; only source your own reviewed files.
source "$KIT_DIR/cluster.env"
source "$KIT_DIR/node.env"
ACTION="${1:-}"
N="${2:-1}"
SUITE="${3:-ring}"
[[ "$ACTION" =~ ^(kernel|comm|serve|help)$ ]] || {
  echo 'Usage: scripts/run_node.sh kernel | help | comm {2|4} {ring|collectives|allpairs} | serve {1|2|4}' >&2; exit 2;
}
for key in H3_ROOT IMAGE EXPECTED_IMAGE_ID MASTER_ADDR CONTROL_IFACE NODE_RANK DIST_PORT TEST_PORT API_PORT API_BIND; do
  value="${!key:-}"
  [[ -n "$value" && "$value" != *CHANGE_ME* ]] || { echo "Missing setting: $key" >&2; exit 2; }
done
[[ "$NODE_RANK" =~ ^[0-3]$ && "$N" =~ ^(1|2|4)$ ]] || { echo 'Bad rank/world'; exit 2; }
for port in "$DIST_PORT" "$TEST_PORT" "$API_PORT"; do
  [[ "$port" =~ ^[0-9]+$ && "$port" -ge 1024 && "$port" -le 65535 ]] || { echo 'Bad port'; exit 2; }
done
[[ "$DIST_PORT" != "$TEST_PORT" && "$DIST_PORT" != "$API_PORT" && "$TEST_PORT" != "$API_PORT" ]] || exit 2
ip link show dev "$CONTROL_IFACE" >/dev/null
python3 - "$MASTER_ADDR" <<'PY'
import ipaddress, sys
address = ipaddress.IPv4Address(sys.argv[1])
if address.is_loopback or address.is_unspecified or address.is_multicast:
    raise SystemExit('MASTER_ADDR must be a unicast IPv4 address reachable from all selected nodes')
PY
ACTUAL_ID="$(docker image inspect "$IMAGE" --format '{{.Id}}')"
[[ "$ACTUAL_ID" == "$EXPECTED_IMAGE_ID" ]] || { echo 'Image ID mismatch; stop.' >&2; exit 2; }
ARCH="$(docker image inspect "$IMAGE" --format '{{.Architecture}}')"
[[ "$ARCH" == arm64 ]] || { echo "Expected arm64 image, got $ARCH"; exit 2; }
mkdir -p "$H3_ROOT/cache" "$H3_ROOT/logs"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-${ACTION}-n${N}-rank${NODE_RANK}-$$"
LOG_DIR="$H3_ROOT/logs/$RUN_ID"
mkdir -p "$LOG_DIR"
NAME="h3-ring-${ACTION}-r${NODE_RANK}"
if docker container inspect "$NAME" >/dev/null 2>&1; then
  echo "Container $NAME exists. Inspect it; this script will not replace it." >&2; exit 2
fi
ARGS=(--rm --name "$NAME" --gpus all --network host --ipc host --ulimit memlock=-1
      --mount "type=bind,src=$KIT_DIR,dst=/kit,readonly"
      --mount "type=bind,src=$H3_ROOT/cache,dst=/cache"
      --mount "type=bind,src=$LOG_DIR,dst=/runlogs"
      -e HOME=/cache -e HF_HOME=/cache/huggingface -e CUDA_VISIBLE_DEVICES=0
      -e PYTHONUNBUFFERED=1 -e "NCCL_SOCKET_IFNAME==$CONTROL_IFACE"
      -e "GLOO_SOCKET_IFNAME=$CONTROL_IFACE" -e NCCL_DEBUG=INFO
      -e NCCL_DEBUG_SUBSYS=INIT,NET,GRAPH,COLL
      -e NCCL_DEBUG_FILE=/runlogs/nccl.%h.%p.log
      -e TORCH_NCCL_ASYNC_ERROR_HANDLING=1)
case "${NETWORK_MODE:-}" in
  ib)
    [[ -d /dev/infiniband ]] || { echo 'No RDMA devices'; exit 2; }
    [[ "${NCCL_HCA:-}" == =* && "$NCCL_HCA" != *CHANGE_ME* ]] || { echo 'Fill exact NCCL_HCA'; exit 2; }
    ARGS+=(--device /dev/infiniband -e NCCL_NET=IB -e NCCL_IB_DISABLE=0 -e "NCCL_IB_HCA=$NCCL_HCA")
    ;;
  socket) ARGS+=(-e NCCL_NET=Socket -e NCCL_IB_DISABLE=1) ;;
  *) echo 'Supported modes: ib or socket. Port custom plugins explicitly before using them.'; exit 2 ;;
esac
case "$ACTION" in
  kernel) CMD=(python /kit/scripts/fa_probe.py) ;;
  help) CMD=(sglang serve --help) ;;
  comm)
    [[ "$N" != 1 && "$NODE_RANK" -lt "$N" ]] || { echo 'Rank outside comm world'; exit 2; }
    [[ "$SUITE" =~ ^(ring|collectives|allpairs)$ ]] || exit 2
    CMD=(torchrun --nnodes "$N" --nproc-per-node 1 --node-rank "$NODE_RANK"
         --master-addr "$MASTER_ADDR" --master-port "$TEST_PORT"
         /kit/scripts/comm_probe.py --suite "$SUITE")
    ;;
  serve)
    [[ "$NODE_RANK" -lt "$N" ]] || { echo 'Rank outside service world'; exit 2; }
    [[ -f "$MODEL_HOST_PATH/model_index.json" ]] || { echo 'Missing model root'; exit 2; }
    case "$MODEL_VARIANT" in
      fl2va) PARTITION=FL2VA ;;
      ref2va) PARTITION=Ref2VA ;;
      *) echo 'Unsupported model variant'; exit 2 ;;
    esac
    [[ -f "$MODEL_HOST_PATH/$PARTITION/model_index.json" ]] || { echo 'Missing task partition'; exit 2; }
    ARGS+=(--mount "type=bind,src=$MODEL_HOST_PATH,dst=/models/h3,readonly"
           -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1)
    CMD=(sglang serve --model-path /models/h3 --model-variant "$MODEL_VARIANT"
         --num-gpus "$N" --nnodes "$N" --node-rank "$NODE_RANK"
         --dist-init-addr "$MASTER_ADDR:$DIST_PORT"
         --tp-size 1 --sp-degree "$N" --ulysses-degree 1 --ring-degree "$N"
         --encoder-parallel replicate --attention-backend fa
         --performance-mode speed --enable-torch-compile false --use-fsdp-inference false
         --host "$API_BIND" --port "$API_PORT")
    case "${QUANTIZATION:-}" in
      fp8) CMD+=(--quantization fp8) ;;
      bf16) ;; # Experimental memory baseline only; this is not the recommended Ring4 preset.
      *) echo 'QUANTIZATION must be fp8 or bf16'; exit 2 ;;
    esac
    ;;
esac
{
  printf 'image_id=%s\nnetwork_mode=%s\n' "$ACTUAL_ID" "$NETWORK_MODE"
  printf 'command='; printf '%q ' "${CMD[@]}"; printf '\n'
} | tee "$LOG_DIR/launch.txt"
printf 'Container: %s\nLogs: %s\n' "$NAME" "$LOG_DIR"
# No privileged mode, no network rewrites, no automatic restarts, no silent Socket fallback.
docker run "${ARGS[@]}" "$IMAGE" "${CMD[@]}" 2>&1 | tee "$LOG_DIR/stdout.log"
```

### 文件：`scripts/fa_probe.py`

<!-- H3FILE scripts/fa_probe.py -->
```python
#!/usr/bin/env python3
"""Exercise H3-shaped packed attention and LSE without loading model weights."""
import json
import math
import platform


def main() -> None:
    import torch
    from sglang.kernels.ops.attention.flash_attention import flash_attn_varlen_func
    from sglang.multimodal_gen.configs.models.dits.minimax_h3 import MiniMaxH3DiTArchConfig

    if not torch.cuda.is_available():
        raise RuntimeError("CUDA is unavailable; do not download/load H3 yet")
    torch.cuda.set_device(0)
    arch = MiniMaxH3DiTArchConfig()
    heads, dim = arch.num_attention_heads, arch.attention_head_dim
    lengths = [64, 96]
    cu = torch.tensor([0, 64, 160], dtype=torch.int32, device="cuda")
    report = {"machine": platform.machine(), "torch": torch.__version__,
              "cuda": torch.version.cuda, "gpu": torch.cuda.get_device_name(0),
              "capability": torch.cuda.get_device_capability(0),
              "heads": heads, "head_dim": dim, "tests": []}
    with torch.inference_mode():
        for dtype in (torch.float16, torch.bfloat16):
            torch.manual_seed(17)
            q, k, v = [torch.randn(sum(lengths), heads, dim, device="cuda", dtype=dtype) * 0.25
                       for _ in range(3)]
            result = flash_attn_varlen_func(
                q, k, v, cu_seqlens_q=cu, cu_seqlens_k=cu,
                max_seqlen_q=max(lengths), max_seqlen_k=max(lengths),
                softmax_scale=1.0 / math.sqrt(dim), causal=False,
                return_softmax_lse=True, ver=4,
            )
            if not isinstance(result, (tuple, list)) or len(result) != 2:
                raise RuntimeError("Expected (output, LSE); API/runtime does not match this probe")
            out, lse = result
            if out.shape != q.shape or lse.shape != (heads, sum(lengths)):
                raise RuntimeError(f"Unexpected output/LSE shapes: {out.shape}, {lse.shape}")
            refs, lses, start = [], [], 0
            for length in lengths:
                end = start + length
                qs, ks, vs = [x[start:end].float().transpose(0, 1) for x in (q, k, v)]
                scores = qs @ ks.transpose(-1, -2) / math.sqrt(dim)
                refs.append((scores.softmax(-1) @ vs).transpose(0, 1))
                lses.append(scores.logsumexp(-1))
                start = end
            reference = torch.cat(refs, dim=0)
            reference_lse = torch.cat(lses, dim=1)
            torch.cuda.synchronize()
            if not (torch.isfinite(out).all() and torch.isfinite(lse).all()):
                raise RuntimeError("Non-finite attention output or LSE")
            out_error = (out.float() - reference).abs().max().item()
            lse_error = (lse.float() - reference_lse).abs().max().item()
            # Smoke-test thresholds, not a video-quality guarantee.
            if out_error > 0.03 or lse_error > 0.03:
                raise RuntimeError(f"Attention numerical check failed: out={out_error}, LSE={lse_error}")
            report["tests"].append({"dtype": str(dtype), "max_abs_error": out_error,
                                     "max_lse_error": lse_error})
    report["status"] = "PASS_KERNEL_ONLY"
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
```

### 文件：`scripts/comm_probe.py`

<!-- H3FILE scripts/comm_probe.py -->
```python
#!/usr/bin/env python3
"""One GPU per node. Test neighbor P2P, collectives, or optional all-pairs P2P."""
import argparse
import datetime
import json
import os
import time


def main() -> None:
    import torch
    import torch.distributed as dist

    p = argparse.ArgumentParser()
    p.add_argument("--suite", choices=["ring", "collectives", "allpairs"], required=True)
    p.add_argument("--timeout", type=int, default=180)
    a = p.parse_args()
    rank, world = int(os.environ["RANK"]), int(os.environ["WORLD_SIZE"])
    if world not in (2, 4):
        raise ValueError("This probe is designed for two or four one-GPU nodes")
    torch.cuda.set_device(0)
    dist.init_process_group("nccl", timeout=datetime.timedelta(seconds=a.timeout))
    try:
        # All ranks enter the first communicator operation.
        dist.barrier(device_ids=[0])
        for nbytes in (4096, 1048576, 16777216, 67108864):
            count = nbytes // 4
            tx = torch.full((count,), rank + 1, dtype=torch.float32, device="cuda")
            rx = torch.zeros_like(tx)
            dist.barrier(device_ids=[0])
            torch.cuda.synchronize()
            t0 = time.perf_counter()
            if a.suite == "ring":
                for direction in (1, -1):
                    send_to = (rank + direction) % world
                    recv_from = (rank - direction) % world
                    rx.zero_()
                    reqs = dist.batch_isend_irecv([
                        dist.P2POp(dist.isend, tx, send_to),
                        dist.P2POp(dist.irecv, rx, recv_from),
                    ])
                    for req in reqs:
                        req.wait()
                    torch.cuda.synchronize()
                    if not torch.all(rx == recv_from + 1).item():
                        raise RuntimeError(f"Neighbor data mismatch at rank {rank}")
            elif a.suite == "collectives":
                reduced = tx.clone()
                dist.all_reduce(reduced)
                expected = world * (world + 1) / 2
                if not torch.all(reduced == expected).item():
                    raise RuntimeError("all_reduce data mismatch")
                gathered = [torch.empty_like(tx) for _ in range(world)]
                dist.all_gather(gathered, tx)
                for source, tensor in enumerate(gathered):
                    if not torch.all(tensor == source + 1).item():
                        raise RuntimeError("all_gather data mismatch")
                # Every rank is a broadcast source at least once.
                for source in range(world):
                    rx.fill_(source + 1 if rank == source else 0)
                    dist.broadcast(rx, src=source)
                    if not torch.all(rx == source + 1).item():
                        raise RuntimeError("broadcast data mismatch")
                del reduced, gathered
            else:
                # offset=2 in world=4 specifically checks the opposite ranks.
                for offset in range(1, world):
                    send_to, recv_from = (rank + offset) % world, (rank - offset) % world
                    rx.zero_()
                    reqs = dist.batch_isend_irecv([
                        dist.P2POp(dist.isend, tx, send_to),
                        dist.P2POp(dist.irecv, rx, recv_from),
                    ])
                    for req in reqs:
                        req.wait()
                    torch.cuda.synchronize()
                    if not torch.all(rx == recv_from + 1).item():
                        raise RuntimeError("all-pairs data mismatch")
            torch.cuda.synchronize()
            print(json.dumps({"rank": rank, "world": world, "suite": a.suite,
                              "bytes_per_tensor": nbytes,
                              "seconds": round(time.perf_counter() - t0, 6),
                              "status": "PASS"}), flush=True)
            del tx, rx
        dist.barrier(device_ids=[0])
    finally:
        dist.destroy_process_group()


if __name__ == "__main__":
    main()
```

### 文件：`scripts/model_download.py`

<!-- H3FILE scripts/model_download.py -->
```python
#!/usr/bin/env python3
"""Plan first; pin a Hub commit; download only the requested original partition."""
import argparse
import datetime
import fnmatch
import json
import re
from pathlib import Path

REPO = "MiniMaxAI/MiniMax-H3"


def main() -> None:
    from huggingface_hub import HfApi, snapshot_download
    p = argparse.ArgumentParser()
    p.add_argument("--dest", required=True, type=Path)
    p.add_argument("--lock", required=True, type=Path)
    p.add_argument("--variant", choices=["FL2VA", "Ref2VA"], default="FL2VA")
    p.add_argument("--revision", help="Optional exact Hub commit, not a mutable branch")
    p.add_argument("--download", action="store_true")
    a = p.parse_args()
    if a.revision and not re.fullmatch(r"[0-9a-fA-F]{40}", a.revision):
        p.error("--revision must be an exact 40-character commit")
    patterns = ["model_index.json", f"{a.variant}/*", "LICENSE*", "README.md", ".gitattributes"]
    api = HfApi()
    if a.lock.exists():
        lock = json.loads(a.lock.read_text())
        if lock["repo"] != REPO or lock["variant"] != a.variant:
            raise RuntimeError("Existing lock belongs to a different model/variant")
        revision = lock["revision"]
        if a.revision and revision != a.revision:
            raise RuntimeError("Do not overwrite an existing revision lock")
        if lock["allow_patterns"] != patterns:
            raise RuntimeError("Existing download scope differs; use a separate reviewed lock")
    else:
        info = api.model_info(REPO, revision=a.revision or "main", files_metadata=True)
        revision = info.sha
        selected = [f for f in info.siblings if any(fnmatch.fnmatchcase(f.rfilename, pat) for pat in patterns)]
        names = {f.rfilename for f in selected}
        if "model_index.json" not in names:
            raise RuntimeError("Repository layout changed: missing public model_index.json")
        for component in ("transformer", "text_encoder", "visual_vae", "audio_vae", "tokenizer", "processor"):
            if not any(n.startswith(f"{a.variant}/{component}/") for n in names):
                raise RuntimeError(f"Missing required component: {component}")
        if any(f.size is None for f in selected):
            raise RuntimeError("Hub did not return all file sizes; cannot establish a complete plan")
        lock = {"repo": REPO, "revision": revision, "variant": a.variant,
                "created_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                "allow_patterns": patterns, "total_bytes": sum(f.size for f in selected),
                "files": [{"path": f.rfilename, "size": f.size} for f in selected]}
        a.lock.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive create prevents silent replacement of another plan.
        with a.lock.open("x", encoding="utf-8") as f:
            json.dump(lock, f, ensure_ascii=False, indent=2)
    print(json.dumps({"revision": revision, "variant": a.variant,
                      "files": len(lock["files"]), "scope_GiB": round(lock["total_bytes"] / 2**30, 2),
                      "dest": str(a.dest.resolve()), "download": a.download}, ensure_ascii=False, indent=2))
    if not a.download:
        return
    a.dest.mkdir(parents=True, exist_ok=True)
    snapshot_download(REPO, revision=revision, allow_patterns=patterns,
                      local_dir=str(a.dest), max_workers=4)
    for item in lock["files"]:
        path = a.dest / item["path"]
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise RuntimeError(f"Downloaded file missing or size mismatch: {item['path']}")
    print("PASS_DOWNLOAD_SIZE_CHECK; run manifest.py for your cross-node SHA256 inventory")


if __name__ == "__main__":
    main()
```

### 文件：`scripts/manifest.py`

<!-- H3FILE scripts/manifest.py -->
```python
#!/usr/bin/env python3
"""Hash a read-only model snapshot; verify identical bytes on the other nodes."""
import argparse
import hashlib
import json
from pathlib import Path


def digest(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(16 * 1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["make", "verify"])
    p.add_argument("root", type=Path)
    p.add_argument("manifest", type=Path)
    a = p.parse_args()
    root = a.root.resolve()
    if not root.is_dir():
        raise RuntimeError("Model root is missing")
    if a.manifest.resolve().is_relative_to(root):
        raise RuntimeError("Store the manifest outside the model directory")
    paths = sorted(p for p in root.rglob("*") if p.is_file() and ".cache" not in p.relative_to(root).parts)
    if not paths:
        raise RuntimeError("Refusing an empty model manifest")
    actual = {str(p.relative_to(root)): {"bytes": p.stat().st_size, "sha256": digest(p)} for p in paths}
    if a.mode == "make":
        a.manifest.parent.mkdir(parents=True, exist_ok=True)
        with a.manifest.open("x", encoding="utf-8") as f:
            json.dump(actual, f, indent=2)
    else:
        expected = json.loads(a.manifest.read_text())
        if actual != expected:
            keys = set(actual) | set(expected)
            bad = sorted(k for k in keys if actual.get(k) != expected.get(k))
            raise RuntimeError(f"Manifest mismatch ({len(bad)} files): {bad[:20]}")
    print(json.dumps({"status": "PASS", "mode": a.mode, "files": len(actual)}))


if __name__ == "__main__":
    main()
```

### 文件：`scripts/video_client.py`

<!-- H3FILE scripts/video_client.py -->
```python
#!/usr/bin/env python3
"""Local H3 job client. Never automatically retries a job-creating POST."""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def request_json(url: str, payload=None) -> dict:
    data = None if payload is None else json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            value = json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read(8192).decode(errors="replace")
        raise RuntimeError(f"HTTP {e.code}: {detail}") from e
    if not isinstance(value, dict):
        raise RuntimeError("Expected an API JSON object")
    return value


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://127.0.0.1:30021")
    p.add_argument("--request", required=True, type=Path)
    p.add_argument("--out", required=True, type=Path)
    p.add_argument("--timeout", type=int, default=7200, help="Client polling budget, not an ETA")
    p.add_argument("--resume-id", help="Poll a known existing job instead of submitting a new job")
    a = p.parse_args()
    if a.timeout <= 0:
        p.error("--timeout must be positive")
    payload = json.loads(a.request.read_text())
    if not isinstance(payload, dict):
        p.error("Request must be a JSON object")
    if a.out.exists() or a.out.with_suffix(a.out.suffix + ".part").exists():
        p.error("Output/partial file already exists; select a new output path")
    a.out.parent.mkdir(parents=True, exist_ok=True)
    prefix = str(a.out)
    Path(prefix + ".request.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    base = a.base.rstrip("/")
    started = time.monotonic()
    job = {"id": a.resume_id} if a.resume_id else request_json(base + "/v1/videos", payload)
    Path(prefix + ".submitted.json").write_text(json.dumps(job, ensure_ascii=False, indent=2))
    job_id = job.get("id")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("API did not return a job id; inspect submitted.json; do not retry POST blindly")
    url = base + "/v1/videos/" + urllib.parse.quote(job_id, safe="")
    print(f"job_id={job_id}", flush=True)
    previous = None
    while True:
        if time.monotonic() - started > a.timeout:
            raise TimeoutError(f"Client deadline reached; job {job_id} may still run. Resume by id, not POST.")
        state = request_json(url)
        Path(prefix + ".status.json").write_text(json.dumps(state, ensure_ascii=False, indent=2))
        status = state.get("status")
        if status != previous:
            print(f"status={status}", flush=True)
            previous = status
        if status == "completed":
            break
        if status in {"failed", "cancelled", "canceled", "expired"}:
            raise RuntimeError(f"Job ended with {status}: {json.dumps(state, ensure_ascii=False)}")
        if status not in {"queued", "pending", "running", "in_progress", "processing"}:
            raise RuntimeError(f"Unknown job status {status!r}; inspect server schema instead of polling forever")
        time.sleep(2)
    part = a.out.with_suffix(a.out.suffix + ".part")
    with urllib.request.urlopen(url + "/content", timeout=600) as r:
        content_type = r.headers.get("Content-Type", "").lower()
        if "json" in content_type or "text/" in content_type:
            raise RuntimeError(f"Expected video content, got {content_type}")
        with part.open("xb") as f:
            while chunk := r.read(1024 * 1024):
                f.write(chunk)
    if part.stat().st_size == 0:
        raise RuntimeError("Empty media output")
    part.rename(a.out)
    result = {"job_id": job_id, "wall_seconds": round(time.monotonic() - started, 3),
              "bytes": a.out.stat().st_size, "output": str(a.out),
              "resumed": bool(a.resume_id), "media_decode_verified": False}
    Path(prefix + ".result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
```

### 文件：`requests/smoke.json`

<!-- H3FILE requests/smoke.json -->
```json
{
  "task": "t2va",
  "prompt": "A single continuous cinematic shot in a quiet woodworking workshop. An adult craftsperson slowly places a wooden cup on a table. The camera gently moves forward. Soft daylight, realistic hand motion, natural room ambience and a clearly audible gentle wooden tap. No music, no subtitles.",
  "conditions": [],
  "target": {"short_edge": 480, "aspect_ratio": "16:9", "duration_seconds": 4},
  "seed": 17,
  "num_inference_steps": 5,
  "quality": "lossless"
}
```

### 文件：`requests/quality.json`

<!-- H3FILE requests/quality.json -->
```json
{
  "task": "t2va",
  "prompt": "A single continuous cinematic shot in a quiet woodworking workshop. An adult craftsperson slowly places a wooden cup on a table. The camera gently moves forward. Soft daylight, realistic hand motion, natural room ambience and a clearly audible gentle wooden tap. No music, no subtitles.",
  "conditions": [],
  "target": {"short_edge": 768, "aspect_ratio": "16:9", "duration_seconds": 4},
  "seed": 17,
  "num_inference_steps": 50,
  "quality": "lossless"
}
```

### 文件：`tests/test_local.py`

<!-- H3FILE tests/test_local.py -->
```python
"""CPU-only tests: files, HTTP contract handling, and dry-run launch argument assembly."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KIT = Path(__file__).resolve().parents[1]


class LocalTests(unittest.TestCase):
    def test_shell_syntax(self):
        subprocess.run(["bash", "-n", str(KIT / "scripts/run_node.sh")], check=True)

    def test_python_syntax(self):
        for path in (KIT / "scripts").glob("*.py"):
            compile(path.read_text(), str(path), "exec")

    def test_manifest_detects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "model"
            root.mkdir()
            (root / "weights.bin").write_bytes(b"abcd")
            manifest = Path(tmp) / "manifest.json"
            command = [sys.executable, str(KIT / "scripts/manifest.py")]
            subprocess.run(command + ["make", str(root), str(manifest)], check=True, capture_output=True)
            subprocess.run(command + ["verify", str(root), str(manifest)], check=True, capture_output=True)
            (root / "weights.bin").write_bytes(b"abce")
            result = subprocess.run(command + ["verify", str(root), str(manifest)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b"Manifest mismatch", result.stderr)

    def run_mock_video(self, status):
        posts = []
        content = b"Mock transport bytes; deliberately not a real encoded video."

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, data, content_type="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                posts.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                self.reply(b'{"id":"job-local-1", "status":"queued"}')

            def do_GET(self):
                if self.path.endswith("/content"):
                    self.reply(content, "video/mp4")
                else:
                    self.reply(json.dumps({"id": "job-local-1", "status": status}).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "result.mp4"
                result = subprocess.run([
                    sys.executable, str(KIT / "scripts/video_client.py"),
                    "--base", f"http://127.0.0.1:{server.server_port}",
                    "--request", str(KIT / "requests/smoke.json"),
                    "--out", str(output), "--timeout", "10",
                ], capture_output=True, text=True, timeout=20)
                self.assertEqual(len(posts), 1, "Must not retry job-creating POST")
                if status == "completed":
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(output.read_bytes(), content)
                    metadata = json.loads(Path(str(output) + ".result.json").read_text())
                    self.assertFalse(metadata["media_decode_verified"])
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertFalse(output.exists())
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_mock_http_success(self):
        self.run_mock_video("completed")

    def test_mock_http_failure(self):
        self.run_mock_video("failed")

    def test_mock_http_unknown_state(self):
        self.run_mock_video("unrecognized_future_status")

    def test_ring_peer_mapping(self):
        for world in (2, 4):
            for offset in range(1, world):
                for sender in range(world):
                    receiver = (sender + offset) % world
                    self.assertEqual((receiver - offset) % world, sender)

    def test_socket_launch_is_labeled_and_global_world_is_four(self):
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            kit = work / "kit"
            (kit / "scripts").mkdir(parents=True)
            shutil.copy2(KIT / "scripts/run_node.sh", kit / "scripts/run_node.sh")
            root = work / "state"
            model = root / "models"
            (model / "FL2VA").mkdir(parents=True)
            (model / "model_index.json").write_text("{}")
            (model / "FL2VA/model_index.json").write_text("{}")
            (kit / "cluster.env").write_text(
                f"H3_ROOT={root}\nIMAGE=local/test\nEXPECTED_IMAGE_ID=sha256:test\n"
                "MASTER_ADDR=192.0.2.5\nDIST_PORT=20021\nTEST_PORT=29521\n"
                f"API_PORT=30021\nAPI_BIND=127.0.0.1\nMODEL_HOST_PATH={model}\n"
                "MODEL_VARIANT=fl2va\nQUANTIZATION=fp8\n"
            )
            (kit / "node.env").write_text("NODE_RANK=0\nCONTROL_IFACE=test0\nNETWORK_MODE=socket\n")
            fake = work / "fakebin"
            fake.mkdir()
            (fake / "ip").write_text("#!/bin/sh\nexit 0\n")
            (fake / "docker").write_text("""#!/bin/sh
if [ "$1" = image ]; then
  case "$*" in *Architecture*) echo arm64;; *) echo sha256:test;; esac
elif [ "$1" = container ]; then
  exit 1
elif [ "$1" = run ]; then
  printf '%s\\n' "$@"
else
  exit 9
fi
""")
            for path in fake.iterdir():
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"])
            result = subprocess.run(["bash", str(kit / "scripts/run_node.sh"), "serve", "4"],
                                    env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--num-gpus\n4\n", result.stdout)
            self.assertIn("--ring-degree\n4\n", result.stdout)
            self.assertIn("NCCL_NET=Socket", result.stdout)
            self.assertIn("NCCL_SOCKET_IFNAME==test0", result.stdout)
            self.assertIn("--encoder-parallel\nreplicate", result.stdout)
            self.assertNotIn("--privileged", result.stdout)


if __name__ == "__main__":
    unittest.main()
```

---

## 附录 B：没有独立管理网络时的 TCP 控制面实验

**本附录默认不执行。** 已有工作的 DeepSeek Ring 网络时，优先复用其控制面，而不是添加第二套路由。以下设计只用于隔离实验，证明四节点普通 IP/TCP 可达；**它不构成 RoCE 多跳成功方案，也不改变本文的 RDMA 验收条件。**

前提：有本机控制台或可靠恢复路径；确认 `192.0.2.4/24` 没有被现有网络/VPN 使用；四根线已核对；每条边首轮只选定一条明确的 logical rail，另一条不自动加入。不要把不同子网强行放进一个环形 Linux bridge，造成二层环路。

### B.1 仅作示例的地址表

约定每台 `RIGHT` 连下一台的 `LEFT`：S0→S1→S2→S3→S0。

| 节点 | RIGHT 地址 | LEFT 地址 | RIGHT 直连 peer/GW | 该节点控制地址 |
|---|---|---|---|---|
| S0 | 192.0.2.5/30 | 192.0.2.8/30 | 192.0.2.9 | 192.0.2.5 |
| S1 | 192.0.2.10/30 | 192.0.2.9/30 | 192.0.2.11 | 192.0.2.10 |
| S2 | 192.0.2.12/30 | 192.0.2.11/30 | 192.0.2.6 | 192.0.2.12 |
| S3 | 192.0.2.7/30 | 192.0.2.6/30 | 192.0.2.8 | 192.0.2.7 |

**示例按每个选定 netdev 只有表内一个实验 IPv4 地址设计。** 若接口已有多个地址，必须核对 NCCL 实际选中了哪个地址；`NCCL_SOCKET_IFNAME` 选择网卡，并不按本表自动选择其中某个 IP。不要为了匹配示例删除现网地址，应优先跳过本附录。

这是为简单验证设计的单向 IP 转发环，控制地址间最多绕行三个边；不是最短路径优化，也没有断线后的自动路由收敛。它不用额外交换机，但经过中间节点的 TCP 会使用主机转发资源。

### B.2 每台在本地控制台填写自己的变量

下面以 S0 为例。S1/S2/S3 必须根据上表改变地址与 gateway。

```bash
RIGHT=CHANGE_ME_TO_ACTUAL_RIGHT_NETDEV
LEFT=CHANGE_ME_TO_ACTUAL_LEFT_NETDEV
RIGHT_ADDR=192.0.2.5/30
LEFT_ADDR=192.0.2.8/30
MY_CONTROL=192.0.2.5
NEXT_GW=192.0.2.9

# 先人工确认：变量正确、子网未占用、当前实验接口没有其他必须保留的业务。
# 不要把这段放进无人监督的远程自动化。
[[ "$RIGHT" != CHANGE_ME* && "$LEFT" != CHANGE_ME* && "$RIGHT" != "$LEFT" ]] \
  || { echo '先填写实际接口'; exit 1; }
ip -4 addr show
ip -4 route show table all
```

先记录以下 sysctl 的旧值，写入审计目录，并保存当前接口 MTU。确认没有冲突后再继续。

```bash
sysctl net.ipv4.ip_forward
sysctl net.ipv4.conf.all.rp_filter
sysctl "net.ipv4.conf.$RIGHT.rp_filter"
sysctl "net.ipv4.conf.$LEFT.rp_filter"

sudo ip link set dev "$RIGHT" up
sudo ip link set dev "$LEFT" up
sudo ip addr add "$RIGHT_ADDR" dev "$RIGHT"
sudo ip addr add "$LEFT_ADDR" dev "$LEFT"

sudo sysctl -w net.ipv4.ip_forward=1
# 本测试存在非对称路径，使用 loose 检查；实验后恢复原值。
sudo sysctl -w net.ipv4.conf.all.rp_filter=2
sudo sysctl -w "net.ipv4.conf.$RIGHT.rp_filter=2"
sudo sysctl -w "net.ipv4.conf.$LEFT.rp_filter=2"

for DST in 192.0.2.5 192.0.2.10 192.0.2.12 192.0.2.7; do
  [[ "$DST" == "$MY_CONTROL" ]] && continue
  sudo ip route add "$DST/32" via "$NEXT_GW" dev "$RIGHT"
done
```

使用 `add` 而不是覆盖已有路由的 `replace`；发现地址或路由已经存在时应停下检查。MTU 首轮保持双方一致的已有值，不把 9000 当成所有环境默认。

转发策略由当前防火墙决定。只在必要的实验接口与实验网段之间允许所需的控制/测试流量；NCCL Socket 除 rendezvous 端口外可能使用动态连接，不要误以为开放一个 `29521` 就覆盖全部通信。**不禁用整机防火墙，不放通公网，不添加 NAT。**

### B.3 验证与配置

四台配置完后，每台执行：

```bash
for DST in 192.0.2.5 192.0.2.10 192.0.2.12 192.0.2.7; do
  ping -c 3 "$DST"
  ip route get "$DST"
done
```

在实验 `node.env` 中将 `CONTROL_IFACE` 设为该节点 `RIGHT` 的真实接口名，`NETWORK_MODE=socket`；`MASTER_ADDR=192.0.2.5`。然后回到第 6.2 节做四机 TCP 测试。

**这里验证的只是 TCP 可达性。** 特别是目的 IP 所属 HCA 与实际到达端口不一致时，不能假定原生 RDMA 会由 Linux 帮忙完成转接。要升级为 RoCE 结论，仍需通过第 6.3–6.4 节及真实 H3 作业验收。

### B.4 撤销本次临时设置

按本节点仍然正确的变量，仅删除实验时新增的内容：

```bash
for DST in 192.0.2.5 192.0.2.10 192.0.2.12 192.0.2.7; do
  [[ "$DST" == "$MY_CONTROL" ]] && continue
  sudo ip route del "$DST/32" via "$NEXT_GW" dev "$RIGHT"
done
sudo ip addr del "$RIGHT_ADDR" dev "$RIGHT"
sudo ip addr del "$LEFT_ADDR" dev "$LEFT"

# 下面不要猜数值；从实验前审计记录中读取并恢复。
# sudo sysctl -w net.ipv4.ip_forward=原值
# sudo sysctl -w net.ipv4.conf.all.rp_filter=原值
# sudo sysctl -w net.ipv4.conf.实际RIGHT.rp_filter=原值
# sudo sysctl -w net.ipv4.conf.实际LEFT.rp_filter=原值
```

这些是临时运行态设置，本文没有写入永久 netplan；正式固化网络前应独立审查和验收，不直接把本附录当生产网络方案。

---

## 附录 C：本交付完成的检查与未完成的实机测试

本交付在本地 CPU 环境实际完成 **8 项测试，全部通过**：Python 语法、Bash 语法、文件哈希变化检测、模拟 HTTP 成功下载、模拟任务失败、未知状态拒绝处理、Ring peer 配对逻辑，以及使用模拟 Docker 命令检查四机启动参数和明确的 Socket 标记。

```bash
cd "$HOME/h3-ring-kit"
python3 -m unittest discover -s tests -v
```

HTTP 测试使用刻意标明的模拟字节，**不是 H3 生成的视频，也不是音视频解码通过的证据**。Docker 测试使用命令替身，不启动真实容器。

尚未在本交付环境验证：ARM64 镜像构建和依赖求解、CUDA/SM121 FA 内核、FP8 执行与装载峰值、模型实际下载、四机 RDMA、四机 H3 生成、真实音视频质量和速度。这些由正文 G1–G8 的检查点逐项验收；不把本地脚本测试结论外推到它们。

**最终原则：保留旧环境；先小测试再大模型；每次只改变一个变量；失败留证据；功能、传输、质量和速度分别验收。**
