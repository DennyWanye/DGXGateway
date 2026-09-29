# DeepSeek-V4.1-Flash：4×DGX Spark 无高速交换机 Ring 部署手册

**版本：2026-09-17 · 四机共同运行同一个模型 · Official native 权重 · vLLM + NCCL Mesh**

> **适用对象：**已有四台 NVIDIA DGX Spark、希望不用 100/200G RDMA 交换机，让四台共同承担一个 DeepSeek-V4.1-Flash 的个人/实验室部署。本文从接线、旧环境保护、文件复用开始，直到真实推理、长上下文测试、停机回退。
>
> **验证边界：**上游公开记录了四 Spark 物理 Ring 的模型试验，但不是 NVIDIA Cluster Assistant 官方支持的四机直连配置。本交付中的隔离镜像装配、较保守的首轮参数和辅助脚本是据固定源码编写的复现方案；没有在你的机器上实测。脚本本地测试通过不等于 ARM64 镜像构建、四机 RoCE、模型加载和生成已经通过。**每个验收门都通过后才能继续，不能用“理论上能跑”替代输出和日志。**[S1][S2][S3]

## 阅读与执行顺序

```text
0–3   明确版本、接线与成本；盘点和备份旧环境
4–7   填写实际节点信息；维护窗口内改接；验证每条直连边
8–9   构建一次新镜像；分发；四机 NCCL Mesh 验证
10–11 下载/复用固定权重；经相邻直连边同步；逐台 SHA 校验
12–14 API 密钥；启动 smoke32k；真实短请求验收
15–17 逐步升到 256K；可选 1M / NVMe 前缀缓存
18–22 Gateway 接入、持久化、回退、故障定位、最终验收
附录   全部配套文件，可从本 Markdown 提取
```

**不要从四机 `start` 命令开始抄。** 本文中的 `FILL_...`、`实际用户名`、`旧目录实际路径`都不是可执行值。主机名、接口、管理 IP、磁盘空闲量必须以现场输出为准。

---

## 0. 这次装的究竟是什么

### 0.1 模型和运行方式

本次主路线：

```text
官方 DeepSeek-V4.1-Flash 原生混合精度 checkpoint
       ↓
vLLM 的指定 Spark/SM121 运行时 + 指定 Engram/attention 补丁
       ↓
TP=4，4 个节点，每台 1 个 GB10 GPU / 1 个 rank
       ↓
NCCL 2.29.7 + 指定 NCCL Mesh 插件
       ↓
四条点对点 RoCE 链路构成物理环
       ↓
节点 0 提供一个受认证的 OpenAI-compatible API
```

这里的“原生”是指使用官方发布的权重格式，不是全 BF16：固定配置包含 FP8 量化配置和 `expert_dtype=fp4`。也不是你旧双机的 EXL3 2.9bpw；不能把 EXL3 目录改名后交给本方案。[S4]

**本方案默认官方权重，不自动继承旧部署的 Abliterated 修改。** 你的旧 `mia-dsv41-ablit` 保留为独立回退环境。网络传输插件不改变模型的安全训练，也不是模型“越狱补丁”。将来增加原生权重修改版，应单独审计和验收，不把旧 EXL3 overlay 加在本方案的原生权重上。

### 0.2 必须纠正的 Ring 理解

**这不是“给所有节点打开 Linux IP forwarding，再让对角节点经另一台 Spark 转发”的方案。**

固定 Mesh 插件的活动连接路径按对端公布的地址寻找同子网直连网卡；它没有实现对非相邻节点的透明中继。可行性的关键是 **NCCL 的逻辑 Ring 与物理环的相邻关系吻合**。[S2]

因此：

- **数据面：**每条线一个独立 `/30` IPv4 子网；模型 collective 经直连邻边。
- **管理/控制面：**四台已有的共同有线管理网络；SSH、进程 rendezvous、Gloo、API 用它。
- **存储：**每台本地 NVMe 上有完整固定 checkpoint；此复现路线不使用 NFS。

你不需要让 rank 0 通过 RoCE 地址直接 ping 通对角 rank 2；你需要让 rank 0 和 rank 2 的**管理地址互通**，并让所有实际邻边分别通过 IP、RoCE、NCCL 验收。

`NCCL_ALGO=Ring` 不等于替 Linux 创建路由，也不能保证任意 all-to-all、专家并行或其他模型自动支持这张稀疏网络。本方案不启用额外 EP，不把 DeepSeek 的成功外推到 H3/其他模型。

### 0.3 不自动执行的操作

本包不会修改旧 Gateway 注册、自动删除旧模型、运行 `FORCE=1`、启动旧 `start-tp4.sh`、复用旧 SGLang `pack of4`、挂 Wi-Fi NFS、升级宿主机驱动、全局 `drop_caches`、重置 GPU 或杀掉不属于本包的容器。

`--maintenance-approved` 是要求操作者已经完成维护授权的显式开关，**不是 Gateway 锁的真实实现**。已有 Gateway 的四节点资源锁/维护模式必须由现有管理入口实际完成；仅加这个参数不会阻止 Gateway 在后台拉起其他模型。

---

## 1. 固定资源与已有资产复用

### 1.1 本文固定的版本

| 项目 | 固定身份 |
|---|---|
| Ring 源码 | `yunwei37/dgx-spark-4-ring-no-switch` @ `94047b444e9fc0c062619ad338f54c8255037984` |
| 原始 Spark 补丁 Recipe | `tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark` @ `592540c69853a8ce9285236ebfd6e54dfc83a013` |
| 官方模型 | `deepseek-ai/DeepSeek-V4.1-Flash` @ `dba1be0a40aa45a94ad051997016db3960a90277` |
| NCCL | `2.29.7`，源码 `b91894bd5b190c874d98a017f93f5daa515b65d0` |
| Mesh 插件 | `autoscriptlabs/nccl-mesh-plugin` @ `19924dcc7c571d6e260953724d394ae50bad82cf` |
| DeepSeek 基础镜像 | `ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:2f8e2a70e73541eacf8c71a8990e907fa18fcd8b2a8d02cd0d54a7c81382a13c` |
| 通信库供体镜像 | `ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:de17ff1d442fe41d461bf23c7d11213ac54afe7cc52a5f42a9489d434ec4db33` |
| 本地新镜像名 | `local/dsv41-ring:20260917`；构建后记录真正 image ID |
| 新容器名 | `dsv41-ring-tp4`；不复用旧容器名 |
| 新 API model ID | `deepseek-v4.1-flash-ring` |

固定版本来自上游试验记录及镜像发布记录，不在四台分别追随 `main`/`latest`。[S1][S5][S6]

**重要：**DeepSeek 基础镜像本身不包含所有模型服务补丁，也没有完成本方案所需的 Mesh 库装配。本包复制通信供体中的 `/opt/nccl-mesh`，再向新镜像加入固定补丁。供体原先用于其他模型的运行时，但这里只取 NCCL/Mesh 动态库，**不取那个模型的权重、loader 或服务参数**。[S5][S7]

### 1.2 你的旧文件哪些可以省下载

你的《给网页版GPT6的三机扩容资料.md》（2026-09-16 的快照，非当前实时查询）记载，旧双机已有相同官方 revision 的 Engram shard 47、48。这提供了约 203GB 可复用候选，但必须逐文件校验。

| 已有资产 | 本方案处理 |
|---|---|
| 官方同 revision 的原始 shard 47/48 | SHA 一致后复用，优先避免重复互联网下载 |
| 已下载的完整 native 官方权重 | 和本文固定 revision 的每个文件 hash 比较；相同文件复用，不同文件不冒认 |
| 旧 EXL3 主体、`mia_exl3_wo_b...` overlay | 保留回退，不用于这套原生运行时 |
| SGLang / Mia 的 packed Engram `of4` 文件 | 本路线不直接接入；磁盘读取补丁需要原始 checkpoint 布局 |
| 旧双机 EXL3 Docker 镜像 | 保留，不当作新的 Mesh DeepSeek 镜像 |
| Qwen / H3 权重与镜像 | 保留；验收时不要占用四台的推理资源 |

**不要为了“看起来一样”覆盖官方 `config.json`、索引或 tokenizer。** 文件名相同、下载大小接近、都叫 DeepSeek，不足以证明格式和内容一样。

### 1.3 容量预算

上游记录固定 checkpoint 约 **510.31GB（十进制）/约 475GiB**，88 个文件，每台各一份；下载脚本会用 HF 的固定版本元数据再次算总量。[S1]

建议每台为新环境准备 **至少 650–750GiB 的可用 NVMe 空间**，有 800GiB 更从容。这是包含权重、镜像、导出 tar、日志和缓存的工程预算，不是模型官方最低要求。复用硬链接/已有本地副本后，实际新增空间可能更少。

可选 NVMe 前缀缓存另需要每台 64GiB 文件；启用前脚本要求该文件系统至少剩余 80GiB。

4×128GB 并不是一台机器上的 512GB 连续显存。TP 会切分相应权重，但还有复制组件、KV、运行时、临时张量和操作系统。Engram 必须使用指定磁盘读取补丁，不能依靠“总内存看起来够”绕过它。[S1][S6]

---

## 2. 硬件接线：先确认物理口，不凭接口数量猜线

### 2.1 所需物料

- 四台 DGX Spark；每台一个 GB10，系统架构 `aarch64`。
- **总共四条已经确认兼容 Spark 直连的高速 DAC/AOC 线**，构成四条边。
- 现有共同有线管理网络，四台可互相 SSH/访问管理 IP。这里可以使用现有路由器/普通交换机；“无交换机”在本方案中是**不买专用高速 RoCE 交换机**，不是完全不需要管理网络。
- 可在每台本地操作的恢复手段；至少确保通过 RJ45 管理口的 SSH 不依赖将要改动的 QSFP。

若现场确实只有两条实体线，才需要再补两条。**两个逻辑接口 UP 不代表已经有两根实体线。**

NVIDIA 当前资料列出的已批准线材包含 Amphenol `NJAAKK-N911` / `NJAAKK0006` 和 Luxshare `LMTQF022-SD-R`。它们的产品标称可高于 Spark 的端口速率。不要仅凭“200G QSFP56”标题判断兼容性；优先核对实际部件号、现场链路协商和官方线材表。[S3]

### 2.2 一个物理口对应两个 Ethernet 接口

面向机器背面看，靠 RJ45 的是左 QSFP。官方的对应关系如下，仍需在实机核对是否被重命名：[S3]

| 物理口 | Linux Ethernet 接口 | RoCE 设备 |
|---|---|---|
| 左 | `enp1s0f0np0` | `rocep1s0f0` |
| 左，同一实体口的另一 PCIe 路径 | `enP2p1s0f0np0` | `roceP2p1s0f0` |
| 右 | `enp1s0f1np1` | `rocep1s0f1` |
| 右，同一实体口的另一 PCIe 路径 | `enP2p1s0f1np1` | `roceP2p1s0f1` |

**首轮每个实体口只选一个已核对的 Ethernet/RoCE 路径用于本方案的 IPv4 地址。** 剩余两个逻辑路径不要残留能误导 Mesh 子网选择的旧 IPv4。本文网络辅助脚本会先保存它们的旧状态，再清除指定未使用接口的 IPv4；不会自动猜哪两个应清除。

这是减少初次排障变量的设计，不是声称单逻辑路径一定把整机 PCIe/网络吞吐榨干。先建立可复现通信，再单独研究其他路径优化。

### 2.3 计划的 rank 顺序与接线表

以下把你提到的四个标签分配为 rank，是**新的接线设计**，不是声称已经读取了这四台的真实管理 IP：

```text
rank 0 / node0  ────────── rank 1 / node1
      │                         │
      │                         │
rank 3 / node3  ────────── rank 2 / node2

逻辑顺序：0 → 1 → 2 → 3 → 0
每台 next 接下一台 prev；prev 接上一台 next。
```

建议从背面看，将“左物理口”统一记为 `prev`，“右物理口”记为 `next`。两端不一定同名口对同名口，而是：

| 线号 | A 端 | B 端 |
|---|---|---|
| C01 | node0 / rank0 / next（右） | node1 / rank1 / prev（左） |
| C12 | node1 / rank1 / next（右） | node2 / rank2 / prev（左） |
| C23 | node2 / rank2 / next（右） | node3 / rank3 / prev（左） |
| C30 | node3 / rank3 / next（右） | node0 / rank0 / prev（左） |

接线之前给线的两端贴标签、拍照，记录原接法。禁止在 Qwen/EXL3 正工作时为“试一下”拔 QSFP。

---

## 3. 每台先盘点和备份，尚不改网络

### 3.1 创建独立目录

**在四台分别执行。** 不需要停旧模型即可创建目录、读取状态；真正的 GPU 测试和网络修改要等维护窗口。

```bash
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /srv/dsv41-ring
mkdir -p /srv/dsv41-ring/{models,cache,logs,manifests,state,backups,secrets,kv-offload}
chmod 700 /srv/dsv41-ring/secrets /srv/dsv41-ring/backups
```

解压本安装包到：

```text
~/DeepSeek-V41-4DGX-Ring-Package/
```

进入：

```bash
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
```

只有 MD 文件时，用附录 A 的提取程序还原同名目录。**先阅读脚本，再运行；不要以 root 运行整个安装包。** 只有明确标注网络修改和系统包安装的步骤需要 sudo。

### 3.2 保存只读盘点

```bash
umask 077
SNAP=/srv/dsv41-ring/backups/pre-ring-$(date +%Y%m%dT%H%M%S)
mkdir -p "$SNAP"
uname -a > "$SNAP/uname.txt"
cat /etc/os-release > "$SNAP/os-release.txt"
free -h > "$SNAP/free.txt"
df -hT > "$SNAP/df.txt"
lsblk -o NAME,SIZE,TYPE,FSTYPE,MOUNTPOINTS > "$SNAP/lsblk.txt"
ip -br link > "$SNAP/ip-link.txt"
ip -j address show > "$SNAP/ip-address.json"
ip -j -4 route show table all > "$SNAP/ip-routes-v4.json"
ip -j rule show > "$SNAP/ip-rules.json"
nvidia-smi -q > "$SNAP/nvidia-smi-q.txt"
docker ps -a --no-trunc > "$SNAP/docker-ps.txt"
docker image ls --digests --no-trunc > "$SNAP/docker-images.txt"
systemctl is-active NetworkManager systemd-networkd > "$SNAP/network-services.txt" || true
command -v nmcli >/dev/null && nmcli -f NAME,UUID,TYPE,DEVICE connection show > "$SNAP/nm-connections.txt"
command -v rdma >/dev/null && rdma link show > "$SNAP/rdma-link.txt"
command -v ibdev2netdev >/dev/null && ibdev2netdev > "$SNAP/ibdev2netdev.txt"
sudo tar -czf "$SNAP/etc-netplan.tgz" -C /etc netplan
sudo chmod 600 "$SNAP/etc-netplan.tgz"
printf '%s\n' "$SNAP"
```

这些备份可能含内部地址、主机名，netplan 还可能含凭据。**本地保存，不公开上传完整备份。** 对外诊断前脱敏。

另用你现在 Gateway 自带的管理方式，导出/记录原注册、自动重启策略、模型停止/启动方法、两机节点锁。这里不提供臆造的 `/gateway/...` API，也不假设其 systemd 单元名称。

### 3.3 核对依赖，缺什么才安装什么

```bash
command -v docker git python3 ip ping ethtool rsync ssh
command -v rdma ibv_devinfo ibdev2netdev ib_write_bw || true
uname -m
nvidia-smi
ls /dev/infiniband
```

Ubuntu 上缺少通用诊断工具时，在确认包源可用后安装：

```bash
sudo apt-get update
sudo apt-get install -y git rsync iproute2 iputils-ping ethtool \
  rdma-core ibverbs-utils perftest python3-venv ca-certificates
```

检查当前用户能否运行`docker info`。没有权限时，先由本机管理员决定使用sudo执行Docker相关步骤，还是把该用户加入docker组；docker组具有接近root的主机管理能力，不应向不可信账号开放。已批准采用docker组时可执行`sudo usermod -aG docker "$USER"`，重新登录后再测试`docker info`。不要修改`/var/run/docker.sock`为所有人可写。

**不因此重装 NVIDIA 驱动、OFED 或已有可用 Docker Toolkit。** 宿主机 `nvidia-smi` 正常还不足以证明容器 GPU 可用；后面用固定镜像验证。容器运行时缺失时，按 NVIDIA 对 Spark 的容器配置文档补齐后再继续。[S10]

### 3.4 查看真实网络映射和管理接口

```bash
ip -br -4 address
ip -br link
ibdev2netdev
rdma link show
ip route show
```

从另一台管理电脑通过拟定的 RJ45 管理 IP SSH 登录每台，再逐台执行：

```bash
ip route get 另一台Spark的实际管理IP
```

输出 `dev` 应是共同管理网络接口，不是 `prev/next` 点对点接口，也不应绕 VPN。四台的管理 IP 需稳定，不能在启动过程中变更。

**停止条件：**现在仅能通过将要重接的 QSFP 进入机器、没有可用管理网络、主机正在运行不可中断任务、真实接口和官方对应表对不上。先解决管理可达性，不改 netplan 碰运气。

---

## 4. 填配置：四台相同一份 JSON，rank 参数不同

### 4.1 初始化配置

**在节点 0 编辑，随后把检查后的相同文件传到其他三台。**

```bash
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
cp -n config/cluster.example.json config/cluster.json
chmod 600 config/cluster.json
```

用编辑器打开 `config/cluster.json`。四台的字段含义：

| 字段 | 怎么填 |
|---|---|
| `management_ip` | 该 rank 的真实、固定管理 IPv4 |
| `management_iface` | 对应该 IP 的本机网卡名称 |
| `prev_iface` | 接上一 rank 的物理口上选中的 Ethernet 路径 |
| `next_iface` | 接下一 rank 的物理口上选中的 Ethernet 路径 |
| `unused_rdma_ifaces` | 另两个明确不用于首轮的 RDMA Ethernet 路径；不是普通管理网卡 |
| `mesh_gid_index` | 后续 GID 检查确认的 RoCE v2 IPv4 GID index；`3` 只是初始候选 |
| `model_dir` | 首轮保持本文默认新目录；已有官方文件通过第10节的只读 `--reuse` 复用，不直接改成旧 EXL3/packed目录 |

通常未重命名的 Spark 可以按左 `enp1s0f0np0` / 右 `enp1s0f1np1` 作为两个选中路径，另两个 `enP2...` 作为首轮未使用路径。**必须以真实接线和 `ibdev2netdev` 对应为准再填，不自动套用。**

### 4.2 地址规划

模板使用以下新网段，避开旧 `192.168.100/101`；仍需要检查你的管理网/VPN/其他业务是否已经使用 `10.77.*`。有冲突就整体换成四个未使用的私有 `/30`，两端对应一起改。

| rank | prev 地址 | next 地址 |
|---:|---|---|
| 0 | `192.0.2.22/30` | `192.0.2.14/30` |
| 1 | `192.0.2.15/30` | `192.0.2.16/30` |
| 2 | `192.0.2.17/30` | `192.0.2.18/30` |
| 3 | `192.0.2.19/30` | `192.0.2.21/30` |

每条线只有两个端点、一个子网。**不要把四台八个数据口全放进同一个 `/24`**，也不要为每条线配置默认网关。

### 4.3 四台分别设置本终端 rank

| 当前机器 | 本终端执行 |
|---|---|
| node0（按本设计） | `export RANK=0` |
| node1 | `export RANK=1` |
| node2 | `export RANK=2` |
| node3 | `export RANK=3` |

以后文中的 `[每台] ... --rank "$RANK"` 均指在四台各自的管理终端执行一次。新开终端需重新设置 `RANK`；**不要在节点0上把 rank0/1/2/3 各运行一次冒充四机。**

```bash
python3 scripts/ringctl.py config --config config/cluster.json --rank "$RANK"
```

预期：没有 `FILL`，本机 rank、地址、接口符合接线表。校验失败立即改配置。配置已复制后可在四台对比：

```bash
sha256sum config/cluster.json
```

四台 SHA 必须相同。配置本身不含 API 密钥，但包含私有地址，仍不要公开。

---

## 5. 正式进入维护窗口，保护旧双机环境

**只有完成以下确认后才改线/改地址：**

1. 在现有 Gateway 停止接受新任务，等待在途任务完成。
2. 用现有管理方式停止旧 DeepSeek/Qwen/H3 GPU 服务，暂停可能自动拉起它们的控制逻辑。
3. 获取四台资源的维护/独占状态，确认其他项目不再向这些节点下发任务。
4. 四台分别执行 `nvidia-smi` 和 `docker ps`，确认没有需要保留运行的 GPU 推理进程。
5. 保存旧 QSFP 接法照片和第3节网络备份；四台管理 SSH 都保持连接。

**保留原目录、旧镜像、旧 `.env` 和 Gateway 注册，不卸载。** 这次物理改线会使旧双机高速拓扑暂时不可用；“保留回退资产”不等于“改线时旧双机生产完全无中断”。

现在按第2.3节把 C01/C12/C23/C30 接好。等待链路协商。每台查看两个选中的 Ethernet 接口：

```bash
# 把名称换成已确认的两个接口。这里只读，不改速率/FEC。
ethtool 实际prev接口
ethtool 实际next接口
```

目标是正确识别实体边、`Link detected: yes`，并协商到线材/端口支持的速率。200000Mb/s 是目标链路状态，**不是 NCCL 有效负载速度承诺**。50/100G 或 Link down 先查线、口、驱动，不能通过改显示名称掩盖。

---

## 6. 临时配置每条直连边，不修改 netplan

### 6.1 先输出修改计划

**四台分别执行：**

```bash
python3 scripts/ring_net.py --config config/cluster.json --rank "$RANK" plan \
  | tee /srv/dsv41-ring/logs/network-plan-r${RANK}.txt
```

计划会列出将保存的状态和精确接口操作：删除这些指定 RDMA 接口的旧 IPv4、给选中两个接口配置新 `/30` 和 MTU9000、清除另两个未使用路径的旧 IPv4。

脚本拒绝：管理地址不在本机、把管理口列为数据口、桥接/bond 从属接口、非 RDMA PCI 接口、DHCP 动态 IPv4、需要保留的非直连路由等复杂场景。**拒绝时不要删检查条件；先人工梳理现网。**

尤其检查：`management_iface` 没出现在待修改接口里；`prev/next` 分属左右两个实体口；要删的192.168.100/101地址确属已停机的旧数据面。

### 6.2 审核无误后应用

**四台分别执行：**

```bash
sudo python3 scripts/ring_net.py --config config/cluster.json --rank "$RANK" \
  apply --maintenance-approved
```

旧状态保存到每台：

```text
/srv/dsv41-ring/state/network-rN.before.json
```

如果该快照已经存在，脚本拒绝覆盖，避免把“已经改过的状态”伪装成最初回退点。不要直接删快照重试；查看失败到了哪一步，必要时执行第20节恢复再重做。

此步骤**不修改** netplan、NetworkManager 持久化 profile、默认路由、IP forwarding、rp_filter、防火墙。它只改指定接口的运行时状态；重启或网络管理器重新应用 profile 可能撤销这些修改。

### 6.3 相邻 IP 和 Jumbo 验收

四台都应用完成后，在四台各执行：

```bash
python3 scripts/ring_net.py --config config/cluster.json --rank "$RANK" check \
  | tee /srv/dsv41-ring/logs/network-check-r${RANK}.txt
```

预期：

```text
ADJACENT_IP_AND_JUMBO_PASS
```

这是邻边 ping 和8972字节 ICMP负载测试，不是 RDMA 验证。脚本还检查每个选中接口只有预期 IPv4、MTU9000，未使用路径无旧 IPv4。

额外检查：

```bash
ip -br -4 addr
ip -4 route show
```

例如 rank0 应看到连到 rank1 的 `192.0.2.13/30`，以及连到 rank3 的 `192.0.2.20/30`。**不需要**为 rank2 的数据地址加经 rank1 的路由；Mesh 不是路由器。

---

## 7. 验证 RoCE 设备、GID 和单边传输

### 7.1 确认 Ethernet → RDMA 设备对应

```bash
ibdev2netdev
ibv_devinfo
rdma link show
```

针对两个正在使用的 RoCE 设备，分别查看 GID 类型、值和所属 netdev。下面循环是只读：

```bash
for d in /sys/class/infiniband/*; do
  [ -d "$d/ports/1/gids" ] || continue
  for f in "$d"/ports/1/gids/*; do
    i=${f##*/}
    printf '%s gid=%s value=%s type=%s ndev=%s\n' \
      "${d##*/}" "$i" "$(cat "$f")" \
      "$(cat "$d/ports/1/gid_attrs/types/$i" 2>/dev/null)" \
      "$(cat "$d/ports/1/gid_attrs/ndevs/$i" 2>/dev/null)"
  done
done
```

需要确认与新 `/30` IPv4 对应的 **RoCE v2** 条目。模板中的 `mesh_gid_index=3` 不能盲用。两条选中边的正确 index 不一致、或出现多个不清楚的 GID 时，先检查残留地址、netdev对应、网卡重配；本简化脚本每节点只传一个 preferred index，不伪造“任何情况自动处理”。[S2]

核对完修改同一份 `cluster.json` 中对应节点的 index，并重新同步四台 JSON、检查 SHA一致。

### 7.2 每条物理边做一次 `ib_write_bw`

**先只测 C01，成功后测 C12、C23、C30。** 一次一条，避免混淆日志。两端确认同版本 `perftest`、相同命令参数。

先看本机已安装命令帮助，确认 `-d`、`-i`、`-x`、`-p`、`--report_gbits`、`-D` 参数存在：

```bash
ib_write_bw --help
```

以 C01 为例，rank0 为 server，rank1 为 client：

```bash
# rank0：把三个变量填成实际值。
RDMA_DEV=实际next对应的RoCE设备
GID_INDEX=已检查的RoCEv2索引
ib_write_bw -d "$RDMA_DEV" -i 1 -x "$GID_INDEX" -p 18515 --report_gbits -D 5
```

```bash
# rank1：这里是本机 prev 对应的设备。
RDMA_DEV=实际prev对应的RoCE设备
GID_INDEX=已检查的RoCEv2索引
ib_write_bw -d "$RDMA_DEV" -i 1 -x "$GID_INDEX" -p 18515 --report_gbits -D 5 192.0.2.14
```

对应其他三条边：

| 边 | server rank / 地址 | client rank |
|---|---|---|
| C12 | rank1 / `192.0.2.16` | rank2 |
| C23 | rank2 / `192.0.2.18` | rank3 |
| C30 | rank3 / `192.0.2.21` | rank0 |

两端设备/GID均使用各自那条边的真实值。首轮不加 `--use_cuda`，这是主机内存 RDMA 检查；固定 Mesh 插件也只公布 host-pointer 能力，不应把本试验称为 GPUDirect RDMA 零拷贝验证。[S2]

**通过条件：**四条边均完成测试，无 QP/CQ/GID 错误；速率记录下来并比较是否有明显掉队边。不要预设某个 GB/s 数字作为所有 Spark 必达指标。结果显示普通 IP 能通但 verbs失败时，停止在此处修复。

### 7.3 防火墙与管理网边界

测试端口18515、分布式29541、NCCL测试29561以及库建立连接时所用临时端口，只允许受信任的四节点管理网/直连网访问。不同本机防火墙工具的规则不能混抄。

先只读检查 `sudo nft list ruleset` / `sudo ufw status verbose`（以已安装工具为准）。由现有网络管理员在**限定接口、限定相邻地址或四个管理IP**的范围处理所需通信；不要 `ufw disable`、清空 iptables，或把四节点分布式端口映射到互联网。Mesh 建连包含 TCP 握手，不是只开放一个 UDP RoCE端口就足够。[S2]

---

## 8. 只构建一次隔离 ARM64 镜像，再分发

### 8.1 下载固定源码（节点0）

```bash
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
bash scripts/fetch_sources.sh
```

检查：

```bash
git -C upstream/ring rev-parse HEAD
git -C upstream/recipe rev-parse HEAD
git -C upstream/ring status --porcelain
git -C upstream/recipe status --porcelain
```

前两个输出应等于第1.1节 SHA；后两个为空。脚本不执行上游 `launch/`、恢复工具或主机调优脚本。你可以先阅读这几份来源：

```text
upstream/ring/docs/deepseek-v41-flash.md
upstream/ring/docs/image.md
upstream/ring/images/runtime/deepseek41/README.md
upstream/recipe/docs/RECIPE.md
upstream/recipe/patch/mounts.txt
```

### 8.2 拉取两个固定镜像（节点0）

```bash
docker pull --platform linux/arm64 \
  ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:2f8e2a70e73541eacf8c71a8990e907fa18fcd8b2a8d02cd0d54a7c81382a13c

docker pull --platform linux/arm64 \
  ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:de17ff1d442fe41d461bf23c7d11213ac54afe7cc52a5f42a9489d434ec4db33
```

缺权限、digest不存在、无ARM64清单时，保留具体错误并停止，不把tag随意换成`latest`。公开镜像通常不需要把个人 Token塞进Dockerfile；有认证要求时通过合法registry登录处理，不把凭据放进包。

### 8.3 新镜像做了哪些装配

本包 Dockerfile：

1. 继承固定 DeepSeek/FlashInfer 预构建镜像，保留其 SM121 相关内核缓存。
2. 从固定供体复制 `/opt/nccl-mesh`，显式指定 NCCL库加载路径。
3. 加入 Recipe 的七项服务修复，其中 `engram.py` 使用 Ring项目修订版，保留 `gather_engram_hashes` 的兼容处理。
4. 加入 Ring项目的 tokenizer兼容文件，以及可选本地NVMe KV connector。
5. 用 `install_patches.py`检查目标文件确实存在、解析Python语法、记录替换前后hash。
6. 不在宿主机pip安装模型运行依赖，也不复制模型权重或API密钥进镜像。

上游记录原本以 bind mount覆盖文件；本包把**相同固定文件烘焙到独立派生镜像**，是为了避免四台挂载遗漏。这个装配形式及首轮保守参数仍要实机验证，不能冒称它是上游原封不动发布的“四机稳定镜像”。[S5][S6][S7]

### 8.4 构建并记录身份（节点0）

```bash
set -o pipefail
docker build --platform linux/arm64 --progress=plain \
  -t local/dsv41-ring:20260917 . \
  2>&1 | tee /srv/dsv41-ring/logs/build.log

docker image inspect local/dsv41-ring:20260917 \
  > /srv/dsv41-ring/manifests/image-inspect.json

docker image inspect local/dsv41-ring:20260917 \
  --format '{{.Id}} {{.Architecture}}'
```

预期架构`arm64`；保存返回的完整`sha256:...`image ID。这里的`docker build`不应重新编译整套FlashInfer或NCCL；如果变成大规模编译，先检查你使用的是否是本包Dockerfile。

导出镜像内记录：

```bash
docker run --rm --entrypoint cat local/dsv41-ring:20260917 \
  /opt/dsv41-ring/patch-manifest.json \
  > /srv/dsv41-ring/manifests/patch-manifest.json

docker run --rm --entrypoint cat local/dsv41-ring:20260917 \
  /opt/dsv41-ring/transport.sha256 \
  > /srv/dsv41-ring/manifests/transport.sha256
```

### 8.5 节点0容器预检

```bash
python3 scripts/ringctl.py preflight --rank 0 --maintenance-approved \
  2>&1 | tee /srv/dsv41-ring/logs/preflight-r0.log
```

预检检查ARM64、一个CUDA GPU、SM121、BF16小矩阵计算、NCCL版本和库依赖、补丁hash、Engram兼容符号与FlashInfer配置支持。

目标输出：

```text
CONTAINER_PREFLIGHT_PASS
```

这还没有加载510GB模型，也没有证明四机已通。**不通过时先停在这里**，避免白白复制大模型。

### 8.6 导出并沿邻边复制镜像

节点0：

```bash
docker save -o /srv/dsv41-ring/dsv41-ring-image.tar local/dsv41-ring:20260917
cd /srv/dsv41-ring
sha256sum dsv41-ring-image.tar > manifests/image-tar.sha256
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
```

小的脚本/配置文件可以通过管理网安全复制。大镜像tar优先通过已经验收的相邻高速边；复制前确认目标目录已在对端创建。

下面示范**rank0→rank1**。`实际rank1用户名`需替换；`-b`绑定本机源IP，避免误走管理网。首次SSH通过受信任管理终端核对对端host key，不关闭host-key校验。

```bash
ip route get 192.0.2.15 from 192.0.2.14
# 输出 dev 必须是 rank0 的 next，不是 Wi-Fi/VPN/管理接口。

rsync -a --partial --info=progress2 \
  -e 'ssh -b 192.0.2.14' \
  /srv/dsv41-ring/dsv41-ring-image.tar \
  实际rank1用户名@192.0.2.15:/srv/dsv41-ring/

rsync -a -e 'ssh -b 192.0.2.14' \
  /srv/dsv41-ring/manifests/image-tar.sha256 \
  实际rank1用户名@192.0.2.15:/srv/dsv41-ring/manifests/
```

然后在rank1用`192.0.2.16→192.0.2.17`发给rank2；在rank2用`192.0.2.18→192.0.2.19`发给rank3。**不要从rank0直接向rank2的点对点地址rsync，也不额外添加转发路由。**

每个从节点接收完：

```bash
cd /srv/dsv41-ring
sha256sum -c manifests/image-tar.sha256
docker load -i dsv41-ring-image.tar
docker image inspect local/dsv41-ring:20260917 --format '{{.Id}} {{.Architecture}}'
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
python3 scripts/ringctl.py preflight --rank "$RANK" --maintenance-approved \
  2>&1 | tee /srv/dsv41-ring/logs/preflight-r${RANK}.log
```

四台image ID必须完全一致；不要只比tag名称。保存所有预检日志。镜像tar可在四台导入和校验完成后由操作者决定是否移到归档盘；本包不自动删除它。

---

## 9. 先过四机 NCCL Mesh，再下载缺失权重

### 9.1 查看将要运行的命令

每台：

```bash
python3 scripts/ringctl.py test --rank "$RANK" --dry-run
```

检查至少包含：

```text
NCCL_NET_PLUGIN=mesh
NCCL_NET=Mesh
NCCL_ALGO=Ring
NCCL_RUNTIME_CONNECT=1
NCCL_SOCKET_IFNAME==你的管理接口
GLOO_SOCKET_IFNAME=你的管理接口
NCCL_MESH_DISABLE_RDMA=0
```

`NCCL_SOCKET_IFNAME==接口`里的两个等号不是笔误：第一个是环境变量赋值，第二个是NCCL的精确接口匹配前缀。Socket控制面走管理网，Mesh数据连接按RoCE子网选网卡。[S2][S9]

### 9.2 四台启动测试

确保没有旧同名测试容器；有的话先保存日志、执行`stop-test`，不要覆盖。

在rank3、rank2、rank1依次执行，再在rank0执行：

```bash
python3 scripts/ringctl.py test --rank "$RANK" --maintenance-approved
```

这是每台一进程的四机测试，不是单机四GPU。

每台查看：

```bash
docker logs -f dsv41-ring-collectives
```

脚本测试all-reduce、all-gather、reduce-scatter、各root的broadcast，并逐张量验证结果，覆盖1/16/128MiB数据量。还检查PyTorch实际映射的NCCL库是否来自`/opt/nccl-mesh/lib/`。

### 9.3 通过标准

四台均出现：

```text
COLLECTIVES_PASS rank=N world_size=4 nccl=2.29.7
```

并检查日志包含Mesh加载、Ring连通信息，实际通道只连物理相邻rank。

**还要排除静默TCP fallback：**固定插件在RDMA初始化不可用时可能走TCP；仅`Using network Mesh`不是RoCE数据面的最终证明。对照GID/QP连接日志、RDMA计数和每边`ib_write_bw`结果，确认没有fallback/RDMAdisabled字样，且RoCE设备计数在collective时增长。[S2]

建议保存前后只读计数：

```bash
rdma statistic show
# 某些版本未提供统计时，记录可用的/sys/class/infiniband/.../counters和ethtool -S。
```

**不要把管理网网卡有少量流量认定为失败**，bootstrap/OOB本来就走管理网。反之，管理网承担全部大包而RoCE未动，不算验收成功。

### 9.4 结束测试，保存日志

每台：

```bash
docker logs dsv41-ring-collectives \
  > /srv/dsv41-ring/logs/collectives-r${RANK}.log 2>&1
python3 scripts/ringctl.py stop-test --rank "$RANK" --maintenance-approved
```

任何rank失败都应停止其他测试rank，保留四份日志，先修复。不要在一个rank残留旧rendezvous进程时重复启动新四机任务。

---

## 10. 固定权重下载与旧文件复用：只从互联网取一份

### 10.1 查找旧资产的实际目录

在已有旧模型的节点，先查项目自己的模型目录，不扫描整个根文件系统、不删除任何文件：

```bash
# 路径替换成你的实际模型根目录。
find 旧模型根目录 -maxdepth 4 -type f \
  \( -name 'model-00047-of-00048.safetensors' \
  -o -name 'model-00048-of-00048.safetensors' \
  -o -name 'model.safetensors.index.json' \) -print
```

记下原始官方文件的父目录。路径名`engram-slim`不意味着文件小，也不证明其格式可以任意复用；SHA才是依据。

### 10.2 获取固定HF元数据，不下载大文件

节点0使用新镜像中已有的huggingface_hub，避免污染宿主机Python。下列命令挂载脚本只读、资产根目录读写，不挂GPU。**首次保持 `root`、`model_dir` 和密钥文件的默认路径；旧文件通过额外的只读挂载加 `--reuse` 引入。若你自行改变目标路径，必须同步修改全部Docker挂载和rsync路径，禁止把大模型下载到容器临时层。**

```bash
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e HF_TOKEN -e HF_HOME=/srv/dsv41-ring/cache/hf-download \
  -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
  -v /srv/dsv41-ring:/srv/dsv41-ring \
  -v "$PWD":/work:ro -w /work \
  --entrypoint python3 local/dsv41-ring:20260917 \
  scripts/model_assets.py plan --config config/cluster.json
```

输出文件清单、字节数，并创建：

```text
/srv/dsv41-ring/manifests/model-lock.json
```

这是固定revision的逐文件清单，LFS权重SHA256、普通Git文件blobSHA1都记录。不跟随`main`，不把HF metadata模式当作已下载。

有访问权限要求时，在当前shell用合法`HF_TOKEN`传递，不写入Dockerfile、命令字面量或日志。公开模型能匿名获取时无需设置Token。

**关于“不走VPN流量”：**`--network host`不会自动绕过VPN。下载前在宿主机确认实际默认路由、split-tunnel策略和代理环境；禁止本包擅自关闭全局VPN或改路由。没有核实出口就先不下载。不要把代理账号、密码复制进日志。

### 10.3 最常规：只下载缺失文件

```bash
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e HF_TOKEN -e HF_HOME=/srv/dsv41-ring/cache/hf-download \
  -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
  -v /srv/dsv41-ring:/srv/dsv41-ring \
  -v "$PWD":/work:ro -w /work \
  --entrypoint python3 local/dsv41-ring:20260917 \
  scripts/model_assets.py download --config config/cluster.json --rank 0
```

已存在且hash正确的文件跳过；文件存在但hash错误时停止，**不会自动覆盖**。HF下载中断后重跑同命令可利用其本地下载缓存；不要清空`.cache/huggingface`后重来。[S8]

### 10.4 复用旧官方Engram，避免重复下载约203GB

假设你已经核对原始官方Engram父目录，先在节点0设置实际值：

```bash
OLD_ENGRAM=/实际旧Engram父目录
# 必须是真实绝对路径；此行不是你的机器已知路径。
```

将旧目录**只读**挂进下载容器：

```bash
docker run --rm --network host --user "$(id -u):$(id -g)" \
  -e HF_TOKEN -e HF_HOME=/srv/dsv41-ring/cache/hf-download \
  -e HF_HUB_OFFLINE=0 -e TRANSFORMERS_OFFLINE=0 \
  -v /srv/dsv41-ring:/srv/dsv41-ring \
  -v "$OLD_ENGRAM":/old-engram:ro \
  -v "$PWD":/work:ro -w /work \
  --entrypoint python3 local/dsv41-ring:20260917 \
  scripts/model_assets.py download --config config/cluster.json --rank 0 \
  --reuse /old-engram --reuse-method copy
```

脚本先hash旧候选，完全一致才复制；`copy`使用`cp --reflink=auto`，文件系统支持CoW时可节省额外空间，不支持时实际复制。源只读、不修改旧资产。

已有完整native目录时可再加一个只读挂载与第二个`--reuse`：

```text
-v /实际已有native目录:/old-native:ro
--reuse /old-engram --reuse /old-native
```

**不要把“metadata显示有候选”当成“已经省下这些流量”。** 看实际下载日志中的`reused verified source`。

### 10.5 硬链接是可选，不默认使用

`--reuse-method hardlink`可以在同一文件系统、允许跨挂载的条件下节省空间。但两个路径共享同一inode：以后修改或chmod任一链接会影响另一份。

只有明确把源和新模型都作为**不可修改资产**，且文件系统/容器挂载允许硬链接时才使用。跨文件系统或只读挂载限制导致EXDEV/EPERM时，回到copy/reflink；不要为省空间把整个旧目录改成可写并运行模型补丁。**本包不会在硬链接文件上覆盖或量化。**

### 10.6 完成节点0全量校验

`download`结尾已全量校验。也可单独执行：

```bash
python3 scripts/model_assets.py verify --config config/cluster.json --rank 0
```

预期：

```text
MODEL_HASH_PASS ...
```

生成节点0校验戳：`manifests/model-verified-r0.json`。这是本地文件hash验收记录，**不是模型推理正确性证明**。

---

## 11. 沿相邻高速边复制到另外三台

### 11.1 不采用NFS，也不假设对角直达

这条复现路线的Engram读取来自各节点本地NVMe。它不是旧手册的`dsv41-nfs`共享数据布局，因此不需要修改旧`.env.tp4`的`NFS_SERVER_IPS/NFS_CLIENTS`或运行`pack of4`。[S1]

节点0先发rank1；rank1完整收到并验证后发rank2；rank2再发rank3。也可以由rank0同时发两个相邻节点，但首轮为减少I/O和排障变量，使用顺序链式复制。**文件复制的顺序链不是模型拓扑改成三边链；C30物理线仍必须保留。**

### 11.2 rank0 → rank1

在rank0：

```bash
ip route get 192.0.2.15 from 192.0.2.14

rsync -a --partial --info=progress2 \
  --exclude='.cache/' \
  -e 'ssh -b 192.0.2.14' \
  /srv/dsv41-ring/models/DeepSeek-V4.1-Flash/ \
  实际rank1用户名@192.0.2.15:/srv/dsv41-ring/models/DeepSeek-V4.1-Flash/

rsync -a -e 'ssh -b 192.0.2.14' \
  /srv/dsv41-ring/manifests/model-lock.json \
  实际rank1用户名@192.0.2.15:/srv/dsv41-ring/manifests/
```

本示例假定默认新模型路径。`cluster.json`改了`model_dir`就同步替换rsync两端路径，并确保四台一致。

不使用`--delete`、`--inplace`或覆盖旧部署目录。若新目标目录已有hash错误/来自其他模型的文件，先人工检查并移出这个新目录；不要在硬链接旧权重的目录盲目rsync覆盖。

rank1校验：

```bash
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
python3 scripts/model_assets.py verify --rank 1
```

### 11.3 rank1 → rank2

在rank1：

```bash
ip route get 192.0.2.17 from 192.0.2.16
rsync -a --partial --info=progress2 --exclude='.cache/' \
  -e 'ssh -b 192.0.2.16' \
  /srv/dsv41-ring/models/DeepSeek-V4.1-Flash/ \
  实际rank2用户名@192.0.2.17:/srv/dsv41-ring/models/DeepSeek-V4.1-Flash/
rsync -a -e 'ssh -b 192.0.2.16' \
  /srv/dsv41-ring/manifests/model-lock.json \
  实际rank2用户名@192.0.2.17:/srv/dsv41-ring/manifests/
```

rank2：

```bash
python3 scripts/model_assets.py verify --rank 2
```

### 11.4 rank2 → rank3

在rank2：

```bash
ip route get 192.0.2.19 from 192.0.2.18
rsync -a --partial --info=progress2 --exclude='.cache/' \
  -e 'ssh -b 192.0.2.18' \
  /srv/dsv41-ring/models/DeepSeek-V4.1-Flash/ \
  实际rank3用户名@192.0.2.19:/srv/dsv41-ring/models/DeepSeek-V4.1-Flash/
rsync -a -e 'ssh -b 192.0.2.18' \
  /srv/dsv41-ring/manifests/model-lock.json \
  实际rank3用户名@192.0.2.19:/srv/dsv41-ring/manifests/
```

rank3：

```bash
python3 scripts/model_assets.py verify --rank 3
```

**每台都要自己的全量SHA验收戳，不复制节点0的`model-verified-r0.json`当作其他机器校验。** 启动脚本还会快速检查manifest、文件大小和mtime是否与本机验收戳一致；这不是防恶意篡改替代品，文件改过就重新全量hash。

### 11.5 完成后复查四台空闲资源

```bash
free -h
df -hT /srv/dsv41-ring
nvidia-smi
python3 scripts/ring_net.py --rank "$RANK" check
```

复制期间如果NetworkManager恢复了旧地址，重新做第6–9节的必要验收；不能认为之前一次通过之后网络永远正确。

---

## 12. API密钥与资源保护

### 12.1 节点0生成随机密钥

```bash
umask 077
python3 - <<'PY'
from pathlib import Path
import secrets
p = Path('/srv/dsv41-ring/secrets/api.env')
with p.open('x') as f:
    f.write('VLLM_API_KEY=' + secrets.token_hex(32) + '\n')
p.chmod(0o600)
print('Created API env file; secret intentionally not printed.')
PY
```

已存在就不覆盖。用受信任SSH安全复制同一小文件到另三台的相同路径，权限都设600。不要把文件放进Git、源码包或公开日志。Docker由本机管理员可inspect环境，这不是防本机root的密钥保险箱。

API只绑定rank0的`127.0.0.1:8891`。默认不会暴露在全部网卡或公网。跨电脑访问用第14.4节SSH隧道。

### 12.2 显式检查内存与其他进程

每台：

```bash
free -h
nvidia-smi --query-compute-apps=pid,process_name --format=csv,noheader
docker ps --format '{{.Names}} {{.Status}}'
```

服务启动脚本要求`MemAvailable≥100GiB`，并拒绝已有GPU计算进程。这是本包保守门槛，不是NVIDIA最低系统要求。不达标先查哪个已授权任务占用；不要自动`killall python`、卸载旧模型文件或清空全局page cache。

### 12.3 前台内存守护

四台各开一个管理终端：

```bash
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
python3 scripts/memory_watch.py --floor-gib 6 --interval 2 --consecutive 3 \
  --stop-on-low-memory \
  | tee /srv/dsv41-ring/logs/memory-r${RANK}.jsonl
```

该脚本在连续低于6GiB时，仅停止带本包owner label的**本节点**`dsv41-ring-tp4`，不杀其他进程。其他rank需要你立即按第20节停止。

6GiB是额外安全余量选择，会使一些上游“最低只剩3–4GiB”的1M配置提前退出。**宁可承认当前配置未通过你的安全门槛，也不要删除保护把主机拖死。** 前台采样来不及阻止所有突发OOM；Docker112GiB限制在统一内存/CUDA环境中也不能当作绝对主机保护。[S1]

---

## 13. 第一阶段：32K单请求跑通

### 13.1 为什么不直接复制1M性能参数

先证明四台能共同加载并返回正确文本，再打开DSpark、CUDA graphs、更大KV和多模态。`smoke32k`是本方案的工程排障档，不是性能榜单配置：

| 参数 | smoke32k |
|---|---:|
| TP / 节点数 | 4 / 4 |
| max model len | 32768 |
| max num seqs | 1 |
| max batched tokens | 512 |
| GPU memory utilization | 0.78 |
| CUDA graphs | 关闭，eager |
| DSpark | 关闭 |
| 视觉 | 关闭，language-model-only |
| Engram | 指定磁盘读取补丁 |
| NVMe KV前缀缓存 | 关闭 |

它可能明显慢于上游50tok/s级结果；这个阶段考察正确性、内存和网络，而不是最终速度。

### 13.2 先看dry-run

每台：

```bash
python3 scripts/ringctl.py start --rank "$RANK" --stage smoke32k --dry-run
```

检查挂载的是新官方目录、服务镜像相同、rank不同、master为rank0管理IP。命令显示密钥文件路径，不显示密钥内容。

### 13.3 worker先启动，再启动head

**rank3终端：**

```bash
python3 scripts/ringctl.py start --rank 3 --stage smoke32k --maintenance-approved
```

**rank2终端：**

```bash
python3 scripts/ringctl.py start --rank 2 --stage smoke32k --maintenance-approved
```

**rank1终端：**

```bash
python3 scripts/ringctl.py start --rank 1 --stage smoke32k --maintenance-approved
```

**rank0终端：**

```bash
python3 scripts/ringctl.py start --rank 0 --stage smoke32k --maintenance-approved
```

四台必须在合理时间内依次启动，不要让第一个worker孤等几十分钟。启动后每台：

```bash
docker logs -f dsv41-ring-tp4
```

### 13.4 观察哪些日志

依次确认：

```text
固定vLLM运行时 / 模型type正确
→ NCCL加载Mesh，而不是旧IB/Socket数据实现
→ Ring communicator建立
→ 模型权重加载
→ Engram DISK路径启用，而不是把全表常驻主存
→ 四个rank均完成初始化
→ rank0 API ready
```

源试验的加载耗时不能直接作为你的机器SLA。脚本设置较长engine-ready timeout以允许首次初始化，但**持续0进展、JIT导致内存急降、某rank退出**都不是“再等一会必好”。保存每台错误，不只看head一份日志。

若日志显示要在线拉HF权重，说明本地模型或路径不完整；本包已开启offline，不应该以后台大下载掩盖本地缺失。

### 13.5 就绪与模型ID验证

只在rank0：

```bash
python3 scripts/api_probe.py models \
  --output /srv/dsv41-ring/logs/models-smoke32k.json
```

模型列表应包含：

```text
deepseek-v4.1-flash-ring
```

这一步证明API有响应，不是生成已通过；继续下一节。

---

## 14. 真实短请求、工具调用和28K输入验收

### 14.1 三个短请求

rank0：

```bash
python3 scripts/api_probe.py smoke \
  --output /srv/dsv41-ring/logs/smoke32k-api.json
```

脚本逐次测试：17×19严格答案、固定中文字符串、非空Python源代码文本。**不会执行生成代码**，代码项仅是文本/乱码检查，不代表语义单元测试通过。

脚本显式使用`reasoning_effort: "none"`避免短输出预算全耗在thinking而误以为模型不回应。未来编程Agent需要思考时再按固定tokenizer支持的字段启用。[S1][S7]

### 14.2 工具调用解析

```bash
python3 scripts/api_probe.py tools \
  --output /srv/dsv41-ring/logs/smoke32k-tools.json
```

这检查一个强制命名函数调用及其JSON参数，**不真正执行外部工具**。HTTP400、parser导入错误或非JSON参数都需定位，不能靠宽松文本正则把错误当作成功。

### 14.3 实际长输入小步验收

```bash
python3 scripts/api_probe.py needle \
  --target-tokens 28000 --depth 0.5 --timeout 1800 \
  --output /srv/dsv41-ring/logs/needle-28k-middle.json
```

脚本通过本地服务`/tokenize`测量普通prompt token数，调整填充长度，插入随机32位hex口令，再读取生成响应的`usage.prompt_tokens`核对实际输入量和严格答案。

`/tokenize`在该运行实例不可用/格式不符时，脚本会失败。应核对固定vLLM的tokenize路由，或用同版本原生tokenizer替代计数；**不要把字符数或估算token当成实测输入量**。测试脚本与此HTTP接口在交付环境中只做过模拟服务验证，真实实例要现场通过。

留出至少4096token供聊天模板、输出和边界变化；32K阶段不要填满32768个纯prompt token。

### 14.4 从你的电脑访问

在你的Mac/Windows管理电脑建立SSH隧道：

```bash
ssh -N -L 8891:127.0.0.1:8891 实际用户名@rank0实际管理IP
```

客户端配置：

```text
Base URL: http://127.0.0.1:8891/v1
Model:    deepseek-v4.1-flash-ring
API Key:  secrets/api.env里生成的密钥值
```

不要把`VLLM_API_KEY=`前缀也填进客户端。客户端若在Docker内部，它的127.0.0.1是容器自己，必须另行明确隧道/认证代理的可达地址；不要用“绑定0.0.0.0并关防火墙”替代网络规划。

### 14.5 通过后保存第一份运行基线

每台：

```bash
docker logs dsv41-ring-tp4 \
  > /srv/dsv41-ring/logs/model-smoke32k-r${RANK}.log 2>&1
python3 scripts/ringctl.py status --rank "$RANK"
```

保存四台imageID、模型manifestSHA、网络配置SHA、API输出、实际prompttokens、内存最低值。**不要收集完整docker inspect给外部人员**，其中可能含API Key；只导出需要的字段并脱敏。

---

## 15. 第二阶段：256K + DSpark5 + CUDA graphs

### 15.1 必须先全部停旧阶段

先停止head接新请求、等待在途请求结束，保存日志。四台分别：

```bash
python3 scripts/ringctl.py stop --rank "$RANK" --maintenance-approved
```

然后四台确认没有本包残留容器、没有其他GPU推理进程。一个worker连上还存活的旧head会造成非常难排查的混合启动；**不要只停head后立即替换worker**。[S6]

### 15.2 text256k参数

| 参数 | text256k |
|---|---:|
| 上下文 | 262144 |
| 实际调度seq上限 | 2 |
| chunked prefill token批量 | 2048 |
| gmu | 0.80 |
| DSpark draft tokens | 5 |
| 自适应验证 | 关闭 |
| graphs | FULL_AND_PIECEWISE |
| capture sizes | `[5,6,10,12]` |
| 视觉 / NVMe KV tier | 均关闭 |

此档是保守增量配置，不是上游1M/8并发性能记录原样复刻。`k=5`时目标/草稿批次的捕获尺寸有对应关系；**不能单改k、并发或图尺寸，然后沿用旧“已经验证”的说法**。已知SM120稀疏attention的padded speculative batch问题是上游禁用自适应验证并用精确尺寸的重要原因。[S6]

### 15.3 启动与重测

按rank3→2→1→0顺序，每台执行：

```bash
python3 scripts/ringctl.py start --rank "$RANK" --stage text256k --maintenance-approved
```

API ready后rank0：

```bash
python3 scripts/api_probe.py smoke --output /srv/dsv41-ring/logs/text256k-smoke.json
python3 scripts/api_probe.py tools --output /srv/dsv41-ring/logs/text256k-tools.json
python3 scripts/api_probe.py needle --target-tokens 120000 --depth 0.5 --timeout 3600 \
  --output /srv/dsv41-ring/logs/text256k-needle-120k.json
```

120K通过且四台内存可控，再做：

```bash
python3 scripts/api_probe.py needle --target-tokens 240000 --depth 0.1 --timeout 3600 \
  --output /srv/dsv41-ring/logs/text256k-needle-240k-d01.json
python3 scripts/api_probe.py needle --target-tokens 240000 --depth 0.9 --timeout 3600 \
  --output /srv/dsv41-ring/logs/text256k-needle-240k-d09.json
```

每次只发一个长请求；确认完成后发下一个，不在长上下文初验同时压并发。

### 15.4 两路并发与速度记录

短请求先以两路并发试验，但日志中调度seq上限是2，不把排队的第3/4个HTTP连接算成真实4路推理。

可以在rank0两个终端同时运行`api_probe.py smoke`，输出到不同文件。记录全体任务墙钟耗时、completion tokens、错误率和内存。

**不要把 `completion_tokens / 整个HTTP墙钟` 命名为纯decode tok/s。** 它混入prefill和TTFT；纯decode需要流式首token/末token计时或服务端指标。同样，prefix cache命中后的TTFT不能冒充冷prefill速度。

### 15.5 要开启视觉时

四台全部停止，再以`--stage vision256k`启动。此档保持256K与两路，取消language-model-only。先单张小图、单并发验证，再增加图片；不要将“config有vision_config”当作视觉推理已通过。

由于用户本次核心任务是文本DeepSeek四机，视觉不是安装成功的必要门槛。保留文本基线可降低首次复现复杂度。

---

## 16. 第三阶段：1M是单独的容量/稳定性实验

### 16.1 与公开试验的关系

上游Ring记录使用官方checkpoint、gmu0.83、1,048,576上下文、8192批量、DSpark5与对应graphs，并有接近993K输入的needle记录；同时记录了gmu0.88造成主机内存问题的失败。[S1]

这些是他人特定机群/版本/负载的结果，不是你的四台在所有请求下的保证。尤其低剩余主存、多个长prompt、首次JIT和视觉profiling可能使主机无法管理。

**保留本包6GiB低内存保护；被保护停止就记为该安全预算下未通过。** 不建议以牺牲管理可恢复性为代价追1M。

### 16.2 启动research1m

先保存256K日志、停四台全部旧阶段。按worker-first，在每台：

```bash
python3 scripts/ringctl.py start --rank "$RANK" --stage research1m \
  --maintenance-approved --accept-1m-risk
```

此档：

```text
max_model_len = 1048576
gmu = 0.83
max_num_batched_tokens = 8192
DSpark = 5
graphs = 上游记录的固定列表
max_num_seqs = 不显式指定，遵循该运行时默认
vision = 保留
NVMe KV tier = 此阶段仍不开
```

不显式指定seqs是为了贴近固定Ring仓库当前profile；**绝不意味着允许你未经测试提交无限并发。** 外层请求队列先严格限制1个在途任务。

### 16.3 长输入递增

先重测短请求，再逐次测试240K→480K→900K。每次都检查四台内存、I/O、超时。示例：

```bash
python3 scripts/api_probe.py needle --target-tokens 480000 --depth 0.5 --timeout 7200 \
  --output /srv/dsv41-ring/logs/research1m-480k.json

# 前一条完整成功、没有低内存/节点退出后才执行：
python3 scripts/api_probe.py needle --target-tokens 900000 --depth 0.5 --timeout 7200 \
  --output /srv/dsv41-ring/logs/research1m-900k.json
```

900K尚未成功，不做“1M已验收”结论。还需不同needle位置、冷/热cache、真实代码/RAG样本和反复请求，不能靠一次随机口令召回宣称百万上下文质量无损。

最终报告必须同时记录：配置窗口、实际`usage.prompt_tokens`、答案是否准确、prefill/总时间、各rank最低内存和是否触发保护。

---

## 17. 可选：每节点64GiB NVMe前缀缓存

**不是必装项。** 前面全部通过后，才考虑`research1m-nvme`。

它缓存的是KV前缀，不是替代Engram文件，也不是把模型权重额外量化。固定Ring项目的`dsv41_kv_nvme.py`为每台worker读写本机NVMe；该模型存在不应缓存的compressor ring，所以不能随手换成通用filesystem `OffloadingConnector`。[S1][S7]

### 17.1 前置检查

每台：

```bash
df -hT /srv/dsv41-ring/kv-offload
```

确认真实本地NVMe文件系统，而不是NFS、FUSE网络盘或USB慢盘。至少80GiB剩余，开启后会预分配64GiB。prefix KV含请求派生数据，按敏感业务数据管理，不共享给其他用户。

### 17.2 全部停机后启用

按worker-first在每台：

```bash
python3 scripts/ringctl.py start --rank "$RANK" --stage research1m-nvme \
  --maintenance-approved --accept-1m-risk
```

脚本启用`--enable-cumem-allocator`及指定自定义connector配置，不直接启用generic filesystem tier。

### 17.3 如何确认真的有效

先重复短请求正确性与内存验收。对固定相同前缀分别测冷访问、重复访问、GPU prefix cache失效后的访问，结合NVMe读写计数、服务端cache指标和greedy输出一致性。

**仅仅重复同一请求变快可能只是GPU prefix cache命中，不能证明NVMe tier在工作。** 本包不擅自调用未知版本的cache-reset管理接口；要复现源记录的GPU-cache-reset试验，先核对固定运行时支持的接口和认证方式。未完成该项验证，记录为“tier已启动，收益未核实”。

---

## 18. 接入你的Gateway：新注册，不替换旧身份

在独立API通过后再接Gateway。建议新增逻辑注册名：

```text
dsv41-ring-tp4
```

而不是把旧`mia-dsv41-ablit`改名。记录区别：

| 项目 | 旧环境 | 本文新环境 |
|---|---|---|
| 权重 | Mia EXL3 Abliterated | 官方原生混合精度 |
| 节点 | 两机 | 四机一个实例 |
| 网络 | 原双机直连 | Mesh物理Ring |
| 引擎 | 旧EXL3定制镜像 | 新固定vLLM派生镜像 |
| 资源锁 | 原两机 | 四台都必须独占 |
| NFS / packed Engram | 以旧计划为准 | 本地原始checkpoint，不使用旧pack |

Gateway适配必须有以下行为：四节点启动前统一检查；按worker-first启动；只以head真实API探针认定ready；任一rank失败令整个实例不健康并停止其余rank；停止/切换前排空在途请求；保留旧环境回退入口。

本包不接管你尚未公开的Gateway代码。手动测试时保持Gateway维护状态，不让后台自动重启争抢GPU。需要从Gateway宿主机访问loopback API时，要明确它是否就是rank0；跨机用认证代理/隧道设计，不虚构一个现成地址。

---

## 19. 网络持久化与重启策略

### 19.1 首轮故意不自动写netplan

理由：你已有Qwen/EXL3的数据面配置和回退地址，现场renderer、NetworkManager profile、优先级、DHCP/静态配置都可能不同。直接新增一个包含同名接口的netplan文件会和已有文件合并，未必等于替换；尤其可能重新带回旧IPv4，干扰Mesh。

因此本交付完整覆盖**临时配置→真实推理验收→安全恢复**。只有选定Ring作为长期拓扑后，再完成一次有审计的持久化变更；这不是启动模型前必须冒险修改的条件。

### 19.2 暂不持久化时怎么重启

先经Gateway排空并停止四个rank，再正常重启节点。重启后以`ip -br -4 addr`查看真实状态，不假设临时地址还在。

如果确认已恢复旧profile，且第6节`network-rN.before.json`仍是最初快照：保留该快照到归档，人工确认旧状态与它一致后，重新走`plan→apply→check→collectives`。不要为了绕过快照冲突直接覆盖文件。

**不创建开机自动拉模型的systemd服务**，因为它可能绕过现有Gateway四节点锁、在其他rank未就绪时抢先启动。

### 19.3 确定长期使用Ring后的持久化变更清单

由实际维护者执行：

1. 确认当前renderer及管理口profile；再备份`/etc/netplan`和对应NetworkManager配置。
2. 在专用维护窗口处理四个RDMA逻辑接口的所有旧IPv4来源，保留管理接口完全不动。
3. 只为选中的prev/next设置新地址与MTU9000；未使用的逻辑路径不留下旧IPv4；不配置默认网关/转发。
4. 运行`sudo netplan generate`检查合并结果。必须人工确认没有误改管理网络，再通过本地可恢复终端运行`sudo netplan try --timeout 120`。
5. 管理SSH、每条边Jumbo/RoCE、四rankcollectives全部复测后才确认变更。
6. 保存最终配置hash、运行时IP/GID映射、恢复旧配置的方法；重启四台后再验收一次。

若使用直接的NetworkManager profile，应使用对应profile的审核流程，不再叠加另一套netplan配置。没有确认renderer和profile前，不提供“通吃所有机器”的写文件命令。

---

## 20. 失败回退：软件状态和物理线都要恢复

### 20.1 正常停止新服务

Gateway/客户端停止新请求，等待在途结束。在rank0停止head后，依次停止另外三台；每台：

```bash
cd "$HOME/DeepSeek-V41-4DGX-Ring-Package"
docker logs dsv41-ring-tp4 \
  > /srv/dsv41-ring/logs/final-model-r${RANK}-$(date +%Y%m%dT%H%M%S).log 2>&1
python3 scripts/ringctl.py stop --rank "$RANK" --maintenance-approved
```

测试容器如有残留：

```bash
python3 scripts/ringctl.py stop-test --rank "$RANK" --maintenance-approved
```

`stop`仅处理带本包label的固定名称；不执行`docker rm -f $(docker ps -aq)`、`pkill vllm`等广泛清理。

### 20.2 恢复临时网络状态

确认新任务全停后，在四台分别：

```bash
sudo python3 scripts/ring_net.py --config config/cluster.json --rank "$RANK" \
  restore --maintenance-approved
```

它恢复最初快照中指定接口的IPv4、MTU和admin up/down状态，快照不会被悄悄丢弃，而是改为带恢复时间的归档文件。

**不替代netplan备份恢复**：本包首轮没有写netplan；若你后来另做了持久化变更，应按第19节自己的变更记录先恢复对应profile。

### 20.3 恢复实体线与旧Gateway

按第3/5节原接线照片接回两机拓扑；确认旧`192.168.100/101`地址与对应实体边一致，重新做旧两机ping/Jumbo/旧环境健康检查。

然后通过原Gateway正式入口恢复旧注册/自动管理/节点锁，重新启动原`mia-dsv41-ablit`或Qwen回退，做一次真实短请求。**只恢复IP、不把线接回去，旧双机不一定能运行。**

### 20.4 绝不为了回退清理大资产

新模型、新镜像、日志都可以暂时留在独立目录。没有必要删除476GiB权重或重下旧模型。后续清理以你明确批准为准；本包不执行`docker system prune -a`、清HF缓存或删除旧模型。

---

## 21. 故障速查与停止条件

| 现象 | 优先检查 | 不要做 |
|---|---|---|
| 两个Linux接口UP但另一条边不通 | 是否其实是同一个实体QSFP的双PCIe路径；核对左右口表 | 用接口数量推断线数量 |
| `No local NIC found on same subnet...` | rank顺序、相邻子网、未使用路径残留IP、逻辑Ring边 | 给对角节点随意加forward路由并声称插件支持relay |
| `libnccl-net-mesh.so`找不到 | 新派生镜像是否包含`/opt/nccl-mesh`，LD_LIBRARY_PATH | 只换NCCL_NET名字或退回Socket冒称RoCE成功 |
| PyTorch实际加载另一版NCCL | `/proc/self/maps`、VLLM_NCCL_SO_PATH、imageID | 四台混用不同NCCL或只比较pip版本 |
| ping通但verbs失败 | GID类型/索引、设备对应、固件/驱动、邻边防火墙 | 跳过RDMA验收直接加载模型 |
| Mesh日志成功但吞吐极低 | 是否TCP fallback、接口计数、磁盘I/O、GPU时钟 | 仅凭“200G Link Up”承诺实际25GB/s |
| 持续GID table changed | NetworkManager/profile重配、地址重复、接口抖动 | 推理中继续改地址/MTU |
| `gather_engram_hashes`缺失 | 是否用了Ring修订的engram.py而非原Recipe旧文件 | 随意添加pass函数绕过未知Engram DP |
| 权重加载爆内存 | Engram DISK补丁、原始模型格式、是否旧模型仍在运行 | 提高gmu到0.88、全局drop_caches、关闭保护 |
| 首次JIT导致主机急剧吃内存 | 是否隐藏了镜像预编译cache，FlashInfer版本/环境是否漂移 | 四台同时不限并行编译 |
| 模型启动了但API model ID不对 | 新服务是否真由本包容器提供、端口是否冲突 | 用旧Gateway的健康状态当新模型通过 |
| 请求返回content空、reasoning非空 | 请求是否启用thinking、max_tokens是否太低 | 判定为网络损坏或直接调大所有并发 |
| DSpark/graphs阶段卡0tok/s | 先回退smoke32k；核对k和精确capture sizes、自适应验证关闭 | 任意修改graph padding继续压测 |
| 长上下文报KV不足 | 实际每rankKV容量、视觉profiling、上下文与并发组合 | 用max_position_embeddings当内存保证 |
| 某一rank退出 | 保存全部日志，停止其他rank，重新统一启动 | 让其他三rank长期挂着等或只重启一个rank |
| 部分文件已有但hash不符 | 是否混入不同revision、EXL3或补丁权重；备份后处理新目录 | 静默覆盖硬链接旧资产 |

**应立即停止模型试验的情况：**主机SSH开始失联、多个rank内存快速下降、GPU/驱动Xid、RoCE传输错误、权重hash不符、逻辑Ring连接非相邻节点、任一rank使用错误镜像或旧rendezvous。

要提交诊断时，提供脱敏的四份日志、版本和hash、接口/GID对应、真实接线表、实际请求token数。不要公开API Key、完整`.env`、HF_TOKEN或含凭据的netplan备份。

---

## 22. 最终验收表

将此表复制到你的执行记录，填写“通过/未测/失败”，不要空口填“完成”。

| 检查项 | 证据 | 状态 |
|---|---|---|
| 4条实体线和rank顺序一致 | 线两端标签/接线照片 | 待填 |
| 两个物理口与四个Ethernet路径映射正确 | `ibdev2netdev`与现场核对 | 待填 |
| 管理面四台全可达 | 四台SSH/管理IP记录 | 待填 |
| 每边IP/Jumbo/verbs通过 | 4条边测试日志 | 待填 |
| 四rankMesh collectives正确且未fallback | 4份COLLECTIVES_PASS+传输证据 | 待填 |
| 四台同imageID、同库hash、同patch清单 | 镜像/transport/patch记录 | 待填 |
| 每台完整固定模型hash通过 | 各rank自己的校验戳 | 待填 |
| 没有从Wi-Fi/NFS读取推理权重/Engram | 本地挂载、路径、I/O记录 | 待填 |
| 32K真实请求通过 | 数学、中文、28Kneedle、工具解析 | 待填 |
| 256K真实输入通过 | 120K/240K实际prompttokens及答案 | 待填 |
| 256K两路并发通过 | 两路执行、错误率、内存日志 | 待填 |
| 视觉测试 | 可选，单图真实响应 | 未测 |
| 1M容量/质量与内存 | 可选，实际输入与各rank最低内存 | 未测 |
| NVMe KV tier正确性/收益 | 可选，真实读盘及一致性对照 | 未测 |
| Gateway四机独占与失败联动 | 人工检查与切换记录 | 待填 |
| 旧双机回退可恢复 | 原接线、原配置、真实回退请求 | 待填 |

**本交付自身的测试：**37项本地测试通过，涵盖配置约束、Ring边匹配、参数生成、保护条件、hash/硬链接行为和模拟HTTP客户端；另做Python编译及Shell语法检查。未构建ARM64镜像，未执行真实GPU/RDMA测试，未下载或加载510GB模型，也未取得你机器上的token/s数据。

上游性能数值的用途是说明该路线已有公开试验依据，不是承诺。最终稳定版本应是你填完整上述证据、能够重复启动和回退的那一份。

---

## 来源索引（固定版本优先）

文中的工程门槛、目录、客户端和辅助脚本为本交付设计；以下材料用于核实模型、传输边界、镜像与上游补丁，不冒充作者逐条给出本包所有命令。

- **[S1]** Ring项目DeepSeek试验与配置记录：<https://github.com/yunwei37/dgx-spark-4-ring-no-switch/blob/94047b444e9fc0c062619ad338f54c8255037984/docs/deepseek-v41-flash.md>
- **[S2]** NCCL Mesh固定README：<https://github.com/autoscriptlabs/nccl-mesh-plugin/blob/19924dcc7c571d6e260953724d394ae50bad82cf/README.md>
- **[S3]** NVIDIA Spark物理口/PCIe/接口对应与集群支持说明：<https://docs.nvidia.com/dgx/dgx-spark/spark-clustering.html>
- **[S4]** 官方模型固定配置：<https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/blob/dba1be0a40aa45a94ad051997016db3960a90277/config.json>；固定文件目录：<https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash/tree/dba1be0a40aa45a94ad051997016db3960a90277>
- **[S5]** 固定镜像发布与装配记录：<https://github.com/yunwei37/dgx-spark-4-ring-no-switch/blob/94047b444e9fc0c062619ad338f54c8255037984/docs/image.md>；DeepSeek Dockerfile：<https://github.com/yunwei37/dgx-spark-4-ring-no-switch/blob/94047b444e9fc0c062619ad338f54c8255037984/Dockerfile.deepseek-v41-flash>；供体Dockerfile：<https://github.com/yunwei37/dgx-spark-4-ring-no-switch/blob/94047b444e9fc0c062619ad338f54c8255037984/Dockerfile>
- **[S6]** 原始Spark Recipe与逐项修复：<https://github.com/tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark/blob/592540c69853a8ce9285236ebfd6e54dfc83a013/docs/RECIPE.md>；实际launch源码（供阅读，不按其硬编码主机操作执行）：<https://github.com/tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark/blob/592540c69853a8ce9285236ebfd6e54dfc83a013/launch/dsv41-tp4.sh>
- **[S7]** Ring运行时文件及用途：<https://github.com/yunwei37/dgx-spark-4-ring-no-switch/blob/94047b444e9fc0c062619ad338f54c8255037984/images/runtime/deepseek41/README.md>；独立DeepSeek profile：<https://github.com/yunwei37/dgx-spark-4-ring-no-switch/blob/94047b444e9fc0c062619ad338f54c8255037984/profiles/deepseek-v41-flash-tp4.sh>
- **[S8]** Hugging Face官方下载/固定revision说明：<https://huggingface.co/docs/huggingface_hub/guides/download>
- **[S9]** NVIDIA NCCL环境变量说明：<https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html>。该页面可能随最新版本变化，本包仍固定2.29.7，不任意套新变量。
- **[S10]** NVIDIA Spark容器运行时：<https://docs.nvidia.com/dgx/dgx-spark/nvidia-container-runtime-for-docker.html>。

所有模型、代码、镜像与通信库仍受各自许可证约束；本包未更改其许可。只部署你有权使用的模型，API仅向获授权的用户开放。


---

## 附录 A：仅保存本 Markdown 时，如何还原全部配套文件

ZIP 已包含全部文件，使用 ZIP 时不需要执行本附录。只有单个 MD 时，先把原始下载文件保留为以下文件名，不通过会改写代码块/空格的编辑器再次导出：

```text
DeepSeek-V41-Flash-4DGX-Switchless-Ring-Installation.zh-CN.md
```

在它所在目录，将下面 Python 内容保存为 `extract_ring_bundle.py`，执行 `python3 extract_ring_bundle.py`。提取程序校验每个文件的 SHA256，拒绝覆盖已有目标，不自动联网、不执行安装。

```python
from pathlib import Path
import hashlib
import re
import shutil

source = Path("DeepSeek-V41-Flash-4DGX-Switchless-Ring-Installation.zh-CN.md")
out = Path.home() / "DeepSeek-V41-4DGX-Ring-Package"
text = source.read_text(encoding="utf-8")
pattern = re.compile(
    r"<!-- BEGIN_BUNDLE_FILE ([^ ]+) ([0-9a-f]{64}) -->\n"
    r"~~~~[^\n]*\n(.*?)\n~~~~\n<!-- END_BUNDLE_FILE -->", re.S)
items = pattern.findall(text)
if not items:
    raise SystemExit("没有找到完整的内嵌文件块，请使用原始下载的MD")
# Validate all entries before writing any of them.
for name, expected, body in items:
    rel = Path(name)
    if rel.is_absolute() or ".." in rel.parts:
        raise SystemExit("拒绝不安全路径: " + name)
    data = (body + "\n").encode("utf-8")
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit("内嵌文件校验失败: " + name)
    dst = out / rel
    if not dst.resolve().is_relative_to(out.resolve()):
        raise SystemExit("拒绝符号链接越界: " + name)
    if dst.exists():
        raise SystemExit("目标已存在，不覆盖: " + str(dst))
for name, expected, body in items:
    dst = out / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_bytes((body + "\n").encode("utf-8"))
    if dst.suffix == ".sh":
        dst.chmod(0o755)
shutil.copyfile(source, out / "INSTALLATION.zh-CN.md")
print(f"已提取 {len(items)} 个文件到 {out}；尚未安装或修改任何主机配置。")
```

得到 `~/DeepSeek-V41-4DGX-Ring-Package/` 后，从正文第3节开始；不要直接运行附录里的单独启动代码跳过配置和验收。

### 配套文件说明

| 文件 | 功能 |
|---|---|
| `Dockerfile` | 固定双镜像来源，装配 Mesh 与 DeepSeek 补丁 |
| `config/cluster.example.json` | 四机信息与相邻边模板 |
| `scripts/fetch_sources.sh` | 只拉固定源码，不运行上游主机脚本 |
| `scripts/ring_net.py` | 指定接口的临时 IPv4/MTU 计划、应用、验收与恢复 |
| `scripts/ringctl.py` | 本节点容器预检、通信测试、分阶段启动/停止 |
| `scripts/model_assets.py` | 固定模型清单、可验证复用、下载与逐节点校验 |
| `scripts/collective_test.py` | 真正上机后执行四 rank collective 正确性测试 |
| `scripts/preflight_container.py` | 真正上机后执行容器/GPU/库与补丁检查 |
| `scripts/serve.py` / `common.py` | 参数验证与固定阶段启动参数 |
| `scripts/api_probe.py` | 认证 API、工具格式、实际 token 数 needle 检查 |
| `scripts/memory_watch.py` | 显式启用的前台低内存保护，不接管其他服务 |
| `scripts/install_patches.py` | 只在新镜像构建时安装指定补丁 |
| `tests/test_bundle.py` | 本地、不依赖 GPU 的单元/模拟 HTTP 测试 |
| `EXECUTION_RECORD.md` | 实机执行记录模板 |

## 附录 B：内嵌文件原文

以下文件是本包的完整内容；上游第三方源码和镜像不内嵌，在指定步骤从固定来源获取。SHA256用于检查提取完整性，不等于对远程运行效果的保证。

### 文件：`.dockerignore`

<!-- BEGIN_BUNDLE_FILE .dockerignore 417c59519af802f49960917f8704c62331c375e37fc7f0567123381da95d6b2a -->
~~~~text
**/.git
**/__pycache__
config/cluster.json
config/*.env
*.tar
*.zip
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`.gitignore`

<!-- BEGIN_BUNDLE_FILE .gitignore c1082f2d6c1224d628fdab763b819d5d1c737ddcfa903f6c1aba8586f19968e7 -->
~~~~text
config/cluster.json
config/*.env
upstream/
__pycache__/
*.tar
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`Dockerfile`

<!-- BEGIN_BUNDLE_FILE Dockerfile 32aac3c76810307c274aad378a484452f5dccba78c03790ec4f9b3cd6f55169f -->
~~~~dockerfile
# Only immutable images. No model weights and no credentials are copied.
# The DeepSeek runtime alone DOES NOT supply NCCL Mesh; copy the pinned donor libraries.
ARG MESH_IMAGE=ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:de17ff1d442fe41d461bf23c7d11213ac54afe7cc52a5f42a9489d434ec4db33
ARG DEEPSEEK_IMAGE=ghcr.io/yunwei37/dgx-spark-4-ring-no-switch@sha256:2f8e2a70e73541eacf8c71a8990e907fa18fcd8b2a8d02cd0d54a7c81382a13c
FROM ${MESH_IMAGE} AS mesh
FROM ${DEEPSEEK_IMAGE}
USER root
COPY --from=mesh /opt/nccl-mesh /opt/nccl-mesh
COPY upstream/recipe/patch/ /opt/dsv41-sources/recipe/patch/
COPY upstream/recipe/LICENSE /opt/dsv41-sources/recipe/LICENSE
COPY upstream/ring/images/runtime/deepseek41/ /opt/dsv41-sources/ring/images/runtime/deepseek41/
COPY upstream/ring/LICENSE /opt/dsv41-sources/ring/LICENSE
COPY upstream/ring/THIRD_PARTY_NOTICES.md /opt/dsv41-sources/ring/THIRD_PARTY_NOTICES.md
COPY scripts/ /opt/dsv41-ring/scripts/
RUN test -f /opt/nccl-mesh/lib/libnccl.so.2.29.7 \
 && test -e /opt/nccl-mesh/lib/libnccl-net-mesh.so \
 && python3 /opt/dsv41-ring/scripts/install_patches.py \
 && python3 -m compileall -q /opt/dsv41-ring/scripts \
 && sha256sum /opt/nccl-mesh/lib/libnccl.so.2.29.7 /opt/nccl-mesh/lib/libnccl-net.so > /opt/dsv41-ring/transport.sha256
ENV LD_LIBRARY_PATH=/opt/nccl-mesh/lib:/usr/local/cuda/lib64:${LD_LIBRARY_PATH}
ENV VLLM_NCCL_SO_PATH=/opt/nccl-mesh/lib/libnccl.so.2
ENV PYTHONPATH=/opt/dsv41-ring/runtime
LABEL local.dsv41-ring.image="step-by-step-20260917" \
      local.dsv41-ring.source="94047b444e9fc0c062619ad338f54c8255037984" \
      local.dsv41-ring.recipe="592540c69853a8ce9285236ebfd6e54dfc83a013"
ENTRYPOINT ["python3", "/opt/dsv41-ring/scripts/serve.py"]
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`EXECUTION_RECORD.md`

<!-- BEGIN_BUNDLE_FILE EXECUTION_RECORD.md bf4f856280958b190f7cb6b3efe5932bc9bdc59fe24e4e4d77a9f66202b29ddc -->
~~~~markdown
# 四机Ring现场执行记录

日期：
操作者：
维护窗口与Gateway四节点锁取得方式：
旧环境可恢复的启动/停止入口：
原始网络备份路径（四台）：
原接线照片位置：

## 物理端口

| rank | 标签/主机 | 管理IP/接口 | prev实体口/Ethernet/RoCE/GID | next实体口/Ethernet/RoCE/GID | 未用逻辑接口 |
|---|---|---|---|---|---|
|0|node0|||||
|1|node1|||||
|2|node2|||||
|3|node3|||||

每条线的部件号/速率/两端口：

## 软件与资产

四台driver/CUDA可见版本：
新image ID：
transport.sha256：
patch-manifest SHA：
cluster.json SHA：
model-lock.json SHA：
四台model-verified-rN.json：
实际互联网下载量/复用量：
实际模型本地路径与可用磁盘：

## 验收

|阶段|每rank最低MemAvailable|实际prompt tokens|结果/输出路径|失败与原因|
|---|---|---|---|---|
|四边Jumbo/RoCE||||
|四rank collectives||||
|smoke32k||||
|28K needle||||
|text256k短请求||||
|120K/240K needle||||
|256K两路||||
|vision256k（可选）||||
|research1m（可选）||||
|NVMe KV tier（可选）||||

## 传输与性能口径

Mesh库已加载：
确认非TCP fallback的日志/设备计数：
GPU/主机温度或时钟异常：
冷prefill/缓存命中分别如何测试：
速度为纯decode、端到端还是aggregate：

## 停机和回退

四个新rank全部停止：
临时IPv4/MTU恢复：
原物理接线恢复：
旧Gateway资源/注册恢复：
旧模型真实请求通过：
新增资产保留/清理决定：

不要将API Key、HF_TOKEN、完整环境变量或未脱敏配置粘入此表。
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`LOCAL_TEST_RESULTS.txt`

<!-- BEGIN_BUNDLE_FILE LOCAL_TEST_RESULTS.txt f98ba9150b448731c751bac58d356b16c97fffda35d60bd74d6f1764a6f8c01c -->
~~~~text
test_authenticated_chat (test_bundle.APIClientTests.test_authenticated_chat) ... ok
test_http_failure_surfaces (test_bundle.APIClientTests.test_http_failure_surfaces) ... ok
test_missing_api_key (test_bundle.APIClientTests.test_missing_api_key) ... ok
test_needle_budget_fits_mock_tokenizer (test_bundle.APIClientTests.test_needle_budget_fits_mock_tokenizer) ... ok
test_git_blob_sha1 (test_bundle.AssetTests.test_git_blob_sha1) ... ok
test_manifest_path_traversal (test_bundle.AssetTests.test_manifest_path_traversal) ... ok
test_reuse_hardlink_unchanged (test_bundle.AssetTests.test_reuse_hardlink_unchanged) ... ok
test_reuse_never_overwrites (test_bundle.AssetTests.test_reuse_never_overwrites) ... ok
test_sha256 (test_bundle.AssetTests.test_sha256) ... ok
test_wrong_existing_file_not_accepted (test_bundle.AssetTests.test_wrong_existing_file_not_accepted) ... ok
test_api_public_bind_rejected (test_bundle.ConfigTests.test_api_public_bind_rejected) ... ok
test_broken_cable_subnet (test_bundle.ConfigTests.test_broken_cable_subnet) ... ok
test_data_management_same_iface (test_bundle.ConfigTests.test_data_management_same_iface) ... ok
test_duplicate_management (test_bundle.ConfigTests.test_duplicate_management) ... ok
test_duplicate_ports (test_bundle.ConfigTests.test_duplicate_ports) ... ok
test_duplicate_rank (test_bundle.ConfigTests.test_duplicate_rank) ... ok
test_interface_shell_injection (test_bundle.ConfigTests.test_interface_shell_injection) ... ok
test_network_address_not_host (test_bundle.ConfigTests.test_network_address_not_host) ... ok
test_path_volume_injection (test_bundle.ConfigTests.test_path_volume_injection) ... ok
test_placeholder (test_bundle.ConfigTests.test_placeholder) ... ok
test_three_nodes (test_bundle.ConfigTests.test_three_nodes) ... ok
test_valid (test_bundle.ConfigTests.test_valid) ... ok
test_all_profiles_bounded_gmu (test_bundle.LaunchTests.test_all_profiles_bounded_gmu) ... ok
test_graphs_exact_two_sequences (test_bundle.LaunchTests.test_graphs_exact_two_sequences) ... ok
test_head_is_not_headless (test_bundle.LaunchTests.test_head_is_not_headless) ... ok
test_key_permissions (test_bundle.LaunchTests.test_key_permissions) ... ok
test_mesh_and_socket_separate (test_bundle.LaunchTests.test_mesh_and_socket_separate) ... ok
test_native_ring_research_keeps_runtime_sequences (test_bundle.LaunchTests.test_native_ring_research_keeps_runtime_sequences) ... ok
test_no_privileged_or_docker_socket (test_bundle.LaunchTests.test_no_privileged_or_docker_socket) ... ok
test_nvme_only_opt_in (test_bundle.LaunchTests.test_nvme_only_opt_in) ... ok
test_preserve_flashinfer_baked_cache (test_bundle.LaunchTests.test_preserve_flashinfer_baked_cache) ... ok
test_secrets_not_rendered (test_bundle.LaunchTests.test_secrets_not_rendered) ... ok
test_smoke_has_eager_no_spec (test_bundle.LaunchTests.test_smoke_has_eager_no_spec) ... ok
test_stop_rejects_unowned (test_bundle.LaunchTests.test_stop_rejects_unowned) ... ok
test_worker_headless (test_bundle.LaunchTests.test_worker_headless) ... ok
test_scoped_only (test_bundle.NetworkPlanTests.test_scoped_only) ... ok
test_unused_ipv4_removed_not_assigned (test_bundle.NetworkPlanTests.test_unused_ipv4_removed_not_assigned) ... ok

----------------------------------------------------------------------
Ran 37 tests in 0.529s

OK
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`README.md`

<!-- BEGIN_BUNDLE_FILE README.md 7472c1858f4b37ce26dfe468e62b4d04657146cecced7fea044cf0d1eebf7de7 -->
~~~~markdown
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
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`config/cluster.example.json`

<!-- BEGIN_BUNDLE_FILE config/cluster.example.json 2f855b25ae89a51daeba325c79326b9500785afdbccf283d1562309f836c5a04 -->
~~~~json
{
  "root": "/srv/dsv41-ring",
  "model_dir": "/srv/dsv41-ring/models/DeepSeek-V4.1-Flash",
  "image": "local/dsv41-ring:20260917",
  "api_key_file": "/srv/dsv41-ring/secrets/api.env",
  "api_bind": "127.0.0.1",
  "api_port": 8891,
  "master_port": 29541,
  "test_port": 29561,
  "nodes": [
    {
      "rank": 0,
      "label": "node0",
      "management_ip": "FILL_NODE_0_MANAGEMENT_IP",
      "management_iface": "FILL_MGMT",
      "prev_iface": "FILL_PREV",
      "prev_cidr": "192.0.2.22/30",
      "next_iface": "FILL_NEXT",
      "next_cidr": "192.0.2.14/30",
      "unused_rdma_ifaces": [
        "FILL_UNUSED1",
        "FILL_UNUSED2"
      ],
      "mesh_gid_index": 3
    },
    {
      "rank": 1,
      "label": "node1",
      "management_ip": "FILL_NODE_1_MANAGEMENT_IP",
      "management_iface": "FILL_MGMT",
      "prev_iface": "FILL_PREV",
      "prev_cidr": "192.0.2.15/30",
      "next_iface": "FILL_NEXT",
      "next_cidr": "192.0.2.16/30",
      "unused_rdma_ifaces": [
        "FILL_UNUSED1",
        "FILL_UNUSED2"
      ],
      "mesh_gid_index": 3
    },
    {
      "rank": 2,
      "label": "node2",
      "management_ip": "FILL_NODE_2_MANAGEMENT_IP",
      "management_iface": "FILL_MGMT",
      "prev_iface": "FILL_PREV",
      "prev_cidr": "192.0.2.17/30",
      "next_iface": "FILL_NEXT",
      "next_cidr": "192.0.2.18/30",
      "unused_rdma_ifaces": [
        "FILL_UNUSED1",
        "FILL_UNUSED2"
      ],
      "mesh_gid_index": 3
    },
    {
      "rank": 3,
      "label": "node3",
      "management_ip": "FILL_NODE_3_MANAGEMENT_IP",
      "management_iface": "FILL_MGMT",
      "prev_iface": "FILL_PREV",
      "prev_cidr": "192.0.2.19/30",
      "next_iface": "FILL_NEXT",
      "next_cidr": "192.0.2.21/30",
      "unused_rdma_ifaces": [
        "FILL_UNUSED1",
        "FILL_UNUSED2"
      ],
      "mesh_gid_index": 3
    }
  ]
}
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/api_probe.py`

<!-- BEGIN_BUNDLE_FILE scripts/api_probe.py c9b1947a0e38dfbddb9afd6e085fcb06b47a35a8bf02cedb769b67538553ab4d -->
~~~~python
#!/usr/bin/env python3
"""Authenticated API smoke and real-token-count needle probes; never executes generated code/tools."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request

MODEL = 'deepseek-v4.1-flash-ring'

def key_from_file(path: str) -> str:
    lines = Path(path).read_text().splitlines()
    values = [x.split('=', 1)[1] for x in lines if x.startswith('VLLM_API_KEY=')]
    if len(values) != 1 or not values[0]:
        raise ValueError('Missing/duplicate VLLM_API_KEY in env file')
    return values[0]

def request_json(base: str, key: str, path: str, payload: dict | None = None, timeout: int = 600) -> dict:
    req = urllib.request.Request(base.rstrip('/') + path,
          data=json.dumps(payload, ensure_ascii=False).encode() if payload is not None else None,
          headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as exc:
        body = exc.read(2000).decode(errors='replace')
        raise RuntimeError(f'HTTP {exc.code}: {body}') from exc

def chat(base: str, key: str, text: str, *, max_tokens=512, timeout=600, extra=None) -> tuple[dict, float]:
    body = {'model': MODEL, 'messages': [{'role': 'user', 'content': text}],
            'temperature': 0, 'max_tokens': max_tokens, 'reasoning_effort': 'none'}
    if extra:
        body.update(extra)
    t = time.perf_counter()
    result = request_json(base, key, '/v1/chat/completions', body, timeout)
    return result, time.perf_counter() - t

def answer(result: dict) -> str:
    return result['choices'][0]['message'].get('content') or ''

def tokenize(base: str, key: str, text: str) -> int:
    obj = request_json(base, key, '/tokenize', {'model': MODEL, 'prompt': text, 'add_special_tokens': False})
    count = obj.get('count')
    if not isinstance(count, int):
        if isinstance(obj.get('tokens'), list):
            count = len(obj['tokens'])
        else:
            raise RuntimeError('/tokenize returned no usable token count; stop, do not claim a long-context pass')
    return count

def make_needle_text(base: str, key: str, target: int, depth: float, secret: str) -> tuple[str, int]:
    prefix = 'Read this archive. A single record gives the passphrase for project ORBIT.\n'
    filler = 'Ordinary archive record: the sample is stored in cabinet seven.\n'
    needle = f'\nPROJECT ORBIT PASSPHRASE = {secret}\n'
    suffix = '\nReturn only the exact PROJECT ORBIT PASSPHRASE. Do not include punctuation or explanations.'
    def build(n: int):
        left = int(n * depth)
        return prefix + filler * left + needle + filler * (n-left) + suffix
    lo, hi = 0, max(1, target // 8)
    # Tokenizer-only calls. No model generation during budget fitting.
    while tokenize(base, key, build(hi)) < target:
        hi *= 2
        if hi > 2000000:
            raise RuntimeError('Abnormal tokenizer response')
    best, best_count = build(0), tokenize(base, key, build(0))
    while lo <= hi:
        mid = (lo + hi) // 2
        txt = build(mid)
        n = tokenize(base, key, txt)
        if n <= target:
            best, best_count, lo = txt, n, mid + 1
        else:
            hi = mid - 1
    return best, best_count

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['models', 'smoke', 'tools', 'needle'])
    p.add_argument('--base-url', default='http://127.0.0.1:8891')
    p.add_argument('--key-file', default='/srv/dsv41-ring/secrets/api.env')
    p.add_argument('--output', default='api-probe-result.json')
    p.add_argument('--target-tokens', type=int, default=28000, help='Plain prompt tokens; leave >=4096 room below server context')
    p.add_argument('--depth', type=float, default=0.5)
    p.add_argument('--timeout', type=int, default=1800)
    a = p.parse_args()
    u = urllib.parse.urlparse(a.base_url)
    if u.scheme not in ('http','https') or not u.hostname:
        p.error('Invalid base URL')
    key = key_from_file(a.key_file)
    out = {'mode': a.mode, 'model': MODEL, 'results': []}
    if a.mode == 'models':
        out['response'] = request_json(a.base_url, key, '/v1/models')
        if not any(x.get('id') == MODEL for x in out['response'].get('data', [])):
            raise RuntimeError('Expected model ID missing')
    elif a.mode == 'smoke':
        cases = [('arithmetic', 'Compute 17 * 19. Reply only with the integer.', '323'),
                 ('literal', '只输出这三个汉字，不要输出其他内容：环形网', '环形网'),
                 ('code', 'Write a Python function is_even(n: int) -> bool. Return only source code.', None)]
        for name, text, expected in cases:
            r, elapsed = chat(a.base_url, key, text, timeout=a.timeout)
            content = answer(r).strip()
            passed = content == expected if expected else bool(content) and '\ufffd' not in content
            out['results'].append({'case': name, 'pass': passed, 'wall_seconds': elapsed,
                                   'response': r, 'note': 'Code case checks nonempty text, NOT code correctness'})
            if not passed:
                Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
                raise RuntimeError(f'Smoke mismatch: {name}; result saved. Never execute returned code automatically')
    elif a.mode == 'tools':
        tool = {'type': 'function', 'function': {'name': 'lookup_test_record', 'description': 'Look up a test record.',
                'parameters': {'type': 'object', 'properties': {'record_id': {'type': 'string'}},
                               'required': ['record_id']}}}
        r, elapsed = chat(a.base_url, key, 'Use lookup_test_record with record_id RING_TEST_17.', timeout=a.timeout,
                          extra={'tools': [tool], 'tool_choice': {'type': 'function','function': {'name': 'lookup_test_record'}}})
        calls = r['choices'][0]['message'].get('tool_calls') or []
        passed = len(calls) >= 1 and calls[0]['function']['name'] == 'lookup_test_record' and \
                 json.loads(calls[0]['function']['arguments']).get('record_id') == 'RING_TEST_17'
        out['results'].append({'pass': passed, 'wall_seconds': elapsed, 'response': r,
                               'note': 'Only validates call parsing. No external function was executed.'})
        if not passed:
            Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
            raise RuntimeError('Tool-call schema mismatch')
    else:
        if not 1000 <= a.target_tokens <= 1044000 or not 0.05 <= a.depth <= 0.95:
            p.error('Use target_tokens=1000..1044000, depth=0.05..0.95; leave context headroom')
        secret = secrets.token_hex(16)
        text, plain_tokens = make_needle_text(a.base_url, key, a.target_tokens, a.depth, secret)
        r, elapsed = chat(a.base_url, key, text, max_tokens=128, timeout=a.timeout)
        passed = answer(r).strip() == secret
        actual = r.get('usage', {}).get('prompt_tokens')
        out['results'].append({'pass': passed, 'needle': secret, 'depth': a.depth,
                               'plain_prompt_tokens': plain_tokens, 'actual_prompt_tokens': actual,
                               'wall_seconds': elapsed, 'response': r})
        if not isinstance(actual, int) or actual < plain_tokens * 0.95 or not passed:
            Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
            raise RuntimeError('Needle answer/token-count verification failed; no long-context pass claimed')
    Path(a.output).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print('API_PROBE_PASS (limited to the explicit cases above; not a general quality benchmark)')

if __name__ == '__main__':
    main()
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/collective_test.py`

<!-- BEGIN_BUNDLE_FILE scripts/collective_test.py 21679ae4bbf8456eb02319a8f58703032f12dadc1510ff0d79e3d7e5f9d411d4 -->
~~~~python
"""Small deterministic four-rank CUDA collectives, no model load."""
import datetime
import json
import os
import time
from pathlib import Path

def main():
    import torch
    import torch.distributed as dist
    if os.environ.get('NCCL_NET')!='Mesh' or os.environ.get('NCCL_ALGO')!='Ring':
        raise RuntimeError('Mesh/Ring must be explicitly selected')
    torch.cuda.set_device(0)
    if torch.cuda.get_device_capability(0)!=(12,1):raise RuntimeError('Expected GB10 SM121')
    rank=int(os.environ['RANK']);world=int(os.environ['WORLD_SIZE'])
    if world!=4:raise RuntimeError('Expected WORLD_SIZE=4')
    dist.init_process_group('nccl',timeout=datetime.timedelta(seconds=180),device_id=torch.device('cuda',0))
    if torch.cuda.nccl.version()!=(2,29,7):raise RuntimeError(f'Unexpected loaded NCCL: {torch.cuda.nccl.version()}')
    maps=Path('/proc/self/maps').read_text()
    paths=sorted({x.split()[-1] for x in maps.splitlines() if '/libnccl.so' in x})
    if not paths or any('/opt/nccl-mesh/lib/' not in x for x in paths):
        raise RuntimeError(f'Wrong/duplicate NCCL mapping: {paths}')
    for mib in (1,16,128):
        count=mib*1024*1024//4
        x=torch.full((count,),float(rank+1),device='cuda')
        dist.all_reduce(x);torch.cuda.synchronize()
        assert torch.all(x==10).item(), 'all_reduce corruption'
        inp=torch.full((count,),float(rank),device='cuda')
        out=torch.empty((count*world,),device='cuda')
        dist.all_gather_into_tensor(out,inp);torch.cuda.synchronize()
        for r in range(world):assert torch.all(out[r*count:(r+1)*count]==r).item(),'all_gather corruption'
        inp2=torch.cat([torch.full((count,),float(r+rank),device='cuda') for r in range(world)])
        out2=torch.empty((count,),device='cuda')
        dist.reduce_scatter_tensor(out2,inp2);torch.cuda.synchronize()
        assert torch.all(out2==(4*rank+6)).item(),'reduce_scatter corruption'
        for src in range(world):
            x.fill_(src+11 if rank==src else 0)
            dist.broadcast(x,src);torch.cuda.synchronize()
            assert torch.all(x==src+11).item(),'broadcast corruption'
        elapsed=[]
        for _ in range(5):
            x.fill_(rank+1);torch.cuda.synchronize();t=time.perf_counter()
            dist.all_reduce(x);torch.cuda.synchronize();elapsed.append(time.perf_counter()-t)
            assert torch.all(x==10).item()
        record={'rank':rank,'MiB':mib,'all_reduce_seconds_mean':sum(elapsed)/len(elapsed),'correct':True}
        print(json.dumps(record),flush=True)
        del x,inp,out,inp2,out2
    dist.barrier();torch.cuda.synchronize()
    print(f'COLLECTIVES_PASS rank={rank} world_size={world} nccl=2.29.7',flush=True)
    dist.destroy_process_group()

if __name__=='__main__':main()
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/common.py`

<!-- BEGIN_BUNDLE_FILE scripts/common.py 41182cd68be96202d5143ccbb3cdee92fb445c02739b65e9076fcf45f726c643 -->
~~~~python
"""Shared validation; no host modifications on import."""
from __future__ import annotations
import ipaddress
import json
import re
from pathlib import Path

RING_COMMIT = '94047b444e9fc0c062619ad338f54c8255037984'
RECIPE_COMMIT = '592540c69853a8ce9285236ebfd6e54dfc83a013'
MODEL_REPO = 'deepseek-ai/DeepSeek-V4.1-Flash'
MODEL_REV = 'dba1be0a40aa45a94ad051997016db3960a90277'
MESH_COMMIT = '19924dcc7c571d6e260953724d394ae50bad82cf'
NCCL_COMMIT = 'b91894bd5b190c874d98a017f93f5daa515b65d0'
CONTAINER = 'dsv41-ring-tp4'
TEST_CONTAINER = 'dsv41-ring-collectives'
PROJECT_LABEL = 'local.dsv41-ring.owner=step-by-step-20260917'
STAGES = {
    'smoke32k': dict(maxlen=32768, gmu='0.78', seqs=1, batch=512, eager=True, spec=False, vision=False, nvme=False),
    'text256k': dict(maxlen=262144, gmu='0.80', seqs=2, batch=2048, eager=False, spec=True, vision=False, nvme=False),
    'vision256k': dict(maxlen=262144, gmu='0.80', seqs=2, batch=2048, eager=False, spec=True, vision=True, nvme=False),
    'research1m': dict(maxlen=1048576, gmu='0.83', seqs=None, batch=8192, eager=False, spec=True, vision=True, nvme=False),
    'research1m-nvme': dict(maxlen=1048576, gmu='0.83', seqs=None, batch=8192, eager=False, spec=True, vision=True, nvme=True),
}

def iface_name(s: str) -> str:
    if not isinstance(s, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,15}', s) or 'FILL' in s or s == 'lo':
        raise ValueError(f'Invalid/unfilled interface: {s!r}')
    return s

def abs_path(s: str) -> str:
    if not isinstance(s, str) or not s.startswith('/') or s == '/' or any(c in s for c in '\n\r\0,:') or '..' in Path(s).parts:
        raise ValueError(f'Unsafe absolute path: {s!r}')
    return s

def load_config(path: str | Path) -> dict:
    c = json.loads(Path(path).read_text())
    for key in ('root', 'model_dir', 'api_key_file'):
        abs_path(c[key])
    if not isinstance(c['image'], str) or not c['image'] or any(x.isspace() for x in c['image']):
        raise ValueError('Invalid image name')
    for key in ('api_port', 'master_port', 'test_port'):
        if not isinstance(c[key], int) or not 1024 <= c[key] <= 65535:
            raise ValueError(f'Invalid {key}')
    if len({c[k] for k in ('api_port','master_port','test_port')}) != 3:
        raise ValueError('API/master/test ports must differ')
    # The service is local by default. Remote access uses an authenticated SSH tunnel.
    if c.get('api_bind') != '127.0.0.1':
        raise ValueError('This bundle requires api_bind=127.0.0.1; use the documented SSH tunnel')
    nodes = c['nodes']
    if len(nodes) != 4 or [n['rank'] for n in nodes] != [0,1,2,3]:
        raise ValueError('Exactly four nodes, in rank order 0,1,2,3, are required')
    mgmt_ips = []
    for n in nodes:
        mgmt = ipaddress.IPv4Address(n['management_ip'])
        if mgmt.is_unspecified or mgmt.is_loopback or mgmt.is_multicast:
            raise ValueError('Invalid management IPv4')
        mgmt_ips.append(mgmt)
        iface_name(n['management_iface'])
        data = [n['prev_iface'], n['next_iface']] + n.get('unused_rdma_ifaces', [])
        for x in data:
            iface_name(x)
        if len(set(data)) != len(data) or n['management_iface'] in data:
            raise ValueError('Management and RDMA interfaces must be distinct; no duplicated interfaces')
        for side in ('prev','next'):
            addr = ipaddress.IPv4Interface(n[f'{side}_cidr'])
            if addr.network.prefixlen != 30 or addr.ip not in list(addr.network.hosts()):
                raise ValueError('Use host addresses in /30 link networks')
            if addr.ip == mgmt:
                raise ValueError('Management IP equals RDMA IP')
        if not isinstance(n['mesh_gid_index'], int) or not 0 <= n['mesh_gid_index'] <= 255:
            raise ValueError('mesh_gid_index must be an inspected integer')
    if len(set(mgmt_ips)) != 4:
        raise ValueError('Management IPs must be unique')
    networks = []
    for i, n in enumerate(nodes):
        a = ipaddress.IPv4Interface(n['next_cidr'])
        b = ipaddress.IPv4Interface(nodes[(i+1)%4]['prev_cidr'])
        if a.network != b.network or a.ip == b.ip:
            raise ValueError(f'Cable {i}->{(i+1)%4} endpoints must share one subnet and use distinct addresses')
        networks.append(a.network)
    if len(set(networks)) != 4:
        raise ValueError('Each cable needs its own subnet')
    if any(m in net for m in mgmt_ips for net in networks):
        raise ValueError('Management IP falls in an RDMA subnet')
    return c

def graph_sizes(k: int, seqs: int | None) -> list[int]:
    if seqs is None:
        return [5,6,10,12,15,18,20,24,25,30,35,36,40,42,48]
    return sorted({k*i for i in range(1,seqs+1)} | {(k+1)*i for i in range(1,seqs+1)})

def model_args(stage: str, rank: int, master: str, port: int, master_port: int) -> list[str]:
    s = STAGES[stage]
    args = ['vllm','serve','/models/DeepSeek-V4.1-Flash',
            '--served-model-name','deepseek-v4.1-flash-ring',
            '--host','127.0.0.1','--port',str(port),
            '--tokenizer-mode','deepseek_v41','--tensor-parallel-size','4',
            '--gpu-memory-utilization',s['gmu'],'--max-model-len',str(s['maxlen']),
            '--max-num-batched-tokens',str(s['batch']),'--block-size','128',
            '--engram-config','{"cpu_offload":false}',
            '--tool-call-parser','deepseek_v41','--enable-auto-tool-choice',
            '--reasoning-parser','deepseek_v41',
            '--distributed-executor-backend','mp','--nnodes','4','--node-rank',str(rank),
            '--master-addr',master,'--master-port',str(master_port)]
    if s['seqs'] is not None:
        args += ['--max-num-seqs',str(s['seqs'])]
    if not s['vision']:
        args += ['--language-model-only']
    if s['eager']:
        args += ['--enforce-eager']
    else:
        args += ['--compilation-config', json.dumps({'cudagraph_mode':'FULL_AND_PIECEWISE',
                                                   'cudagraph_capture_sizes':graph_sizes(5,s['seqs'])},separators=(',',':'))]
    if s['spec']:
        args += ['--speculative-config', json.dumps({'method':'dspark','num_speculative_tokens':5,
              'draft_sample_method':'probabilistic','rejection_sample_method':'block',
              'enable_adaptive_verification':False},separators=(',',':'))]
    if s['nvme']:
        args += ['--enable-cumem-allocator','--kv-transfer-config',json.dumps({
            'kv_connector':'CacheableGroupsOffloadingConnector','kv_connector_module_path':'dsv41_kv_nvme',
            'kv_role':'kv_both','kv_connector_extra_config':{'spec_name':'NVMeOffloadingSpec',
            'spec_module_path':'dsv41_kv_nvme','nvme_dir':'/kv-offload','nvme_bytes':68719476736,
            'offload_prompt_only':False}},separators=(',',':'))]
    if rank:
        args += ['--headless']
    return args
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/fetch_sources.sh`

<!-- BEGIN_BUNDLE_FILE scripts/fetch_sources.sh 9b18f2002d9f952940f8a8da3b0d04722e40919f6b1b78ba7ee3725183861d32 -->
~~~~bash
#!/usr/bin/env bash
set -euo pipefail
BASE=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$BASE/upstream"
checkout() {
  local url=$1 dir=$2 sha=$3
  if [[ ! -d "$dir/.git" ]]; then
    [[ ! -e "$dir" ]] || { echo "Refuse non-git destination: $dir" >&2; exit 1; }
    git init "$dir"
    git -C "$dir" remote add origin "$url"
  fi
  [[ $(git -C "$dir" remote get-url origin) == "$url" ]] || { echo 'Unexpected remote' >&2; exit 1; }
  [[ -z $(git -C "$dir" status --porcelain) ]] || { echo "Dirty checkout: $dir" >&2; exit 1; }
  if ! git -C "$dir" cat-file -e "$sha^{commit}" 2>/dev/null; then
    git -C "$dir" fetch --depth 1 origin "$sha"
  fi
  git -C "$dir" checkout --detach "$sha"
  [[ $(git -C "$dir" rev-parse HEAD) == "$sha" ]]
}
checkout https://github.com/yunwei37/dgx-spark-4-ring-no-switch.git "$BASE/upstream/ring" 94047b444e9fc0c062619ad338f54c8255037984
checkout https://github.com/tonyd2wild/DeepSeek-V4.1-Flash-vLLM-DGX-Spark.git "$BASE/upstream/recipe" 592540c69853a8ce9285236ebfd6e54dfc83a013
printf '%s\n' 'SOURCES_PINNED: do not run the upstream host launch/cleanup scripts.'
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/install_patches.py`

<!-- BEGIN_BUNDLE_FILE scripts/install_patches.py 891ab1f370c1a148755e25e6e9c6d2c7c823089c7fd7816c71045aaaee3c58b1 -->
~~~~python
"""Image-build-only installer; writes inside the new image, never the host."""
import ast
import hashlib
import importlib.util
import json
import shutil
from pathlib import Path

PATCHES = {
    'engram.py':'models/deepseek_v4_1/common/engram.py',
    'model_state.py':'models/deepseek_v4_1/nvidia/model_state.py',
    'weight_utils.py':'model_executor/model_loader/weight_utils.py',
    'attention.py':'models/deepseek_v4_1/attention.py',
    'flashinfer_sparse.py':'models/deepseek_v4_1/nvidia/flashinfer_sparse.py',
    'sparse_swa.py':'v1/attention/backends/mla/sparse_swa.py',
    'sparse_attn_indexer.py':'model_executor/layers/sparse_attn_indexer.py',
}

def main():
    spec = importlib.util.find_spec('vllm')
    if not spec or not spec.origin:
        raise RuntimeError('vLLM package missing')
    site = Path(spec.origin).parent
    src = Path('/opt/dsv41-sources')
    jobs = []
    for filename, target in PATCHES.items():
        source = src / ('ring/images/runtime/deepseek41' if filename=='engram.py' else 'recipe/patch') / filename
        jobs.append((source,site/target))
    jobs.append((src/'ring/images/runtime/deepseek41/deepseek_v41_tokenizer.py',site/'tokenizers/deepseek_v41.py'))
    record=[]
    for source,target in jobs:
        if not source.is_file() or not target.is_file():
            raise RuntimeError(f'Missing patch/source target: {source} -> {target}')
        text=source.read_text(); ast.parse(text)
        if source.name=='engram.py' and 'def gather_engram_hashes' not in text:
            raise RuntimeError('Missing day-0 Engram compatibility helper')
        record.append({'source':str(source),'target':str(target),
            'original_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
            'installed_sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
    for source,target in jobs:
        shutil.copyfile(source,target)
    connector=src/'ring/images/runtime/deepseek41/dsv41_kv_nvme.py'
    ast.parse(connector.read_text())
    out=Path('/opt/dsv41-ring/runtime');out.mkdir(parents=True,exist_ok=True)
    shutil.copyfile(connector,out/connector.name)
    record.append({'source':str(connector),'target':str(out/connector.name),
                  'installed_sha256':hashlib.sha256(connector.read_bytes()).hexdigest()})
    Path('/opt/dsv41-ring/patch-manifest.json').write_text(json.dumps(record,indent=2)+'\n')
    print(f'PATCHES_INSTALLED: {len(jobs)} replacements + one optional connector; vLLM={site}')

if __name__=='__main__':main()
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/memory_watch.py`

<!-- BEGIN_BUNDLE_FILE scripts/memory_watch.py 3bcafe604776ed303c64f7166370e22a8b8b192ca62110f0128728ac3aea8ab7 -->
~~~~python
#!/usr/bin/env python3
"""Foreground, opt-in host-memory guard. It can stop only this bundle's labelled model container."""
import argparse
import json
import time
from datetime import datetime, timezone
from ringctl import mem_available_gib, inspect_container, stop_owned, LABEL_KEY, LABEL_VALUE
from common import CONTAINER

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--floor-gib', type=float, default=6.0)
    p.add_argument('--interval', type=float, default=2.0)
    p.add_argument('--consecutive', type=int, default=3)
    p.add_argument('--stop-on-low-memory', action='store_true')
    a = p.parse_args()
    if not (2 <= a.floor_gib <= 32 and 1 <= a.interval <= 60 and 1 <= a.consecutive <= 30):
        p.error('Invalid guard parameters')
    lows = 0
    try:
        while True:
            m = mem_available_gib()
            lows = lows + 1 if m < a.floor_gib else 0
            print(json.dumps({'time': datetime.now(timezone.utc).isoformat(), 'MemAvailable_GiB': round(m, 3),
                              'low_samples': lows, 'stop_enabled': a.stop_on_low_memory}), flush=True)
            if lows >= a.consecutive:
                print('LOW_MEMORY: record all rank logs; this is not proof that CUDA allocations are fully bounded', flush=True)
                if a.stop_on_low_memory:
                    info = inspect_container(CONTAINER)
                    if info and info.get('Config', {}).get('Labels', {}).get(LABEL_KEY) == LABEL_VALUE:
                        stop_owned(CONTAINER)
                        print('Stopped ONLY this node’s labelled dsv41-ring container. Stop the other ranks explicitly.')
                    else:
                        print('No owned model container to stop; unrelated processes unchanged')
                    return
            time.sleep(a.interval)
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/model_assets.py`

<!-- BEGIN_BUNDLE_FILE scripts/model_assets.py 59ccdc39bf4836798fb088312b1639bd26f69866bd62ea6038968f2321c27893 -->
~~~~python
#!/usr/bin/env python3
"""Pinned HF download and SHA verification. Never edits/requantizes old model files."""
from __future__ import annotations
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from common import MODEL_REPO, MODEL_REV, load_config

CHUNK = 8 * 1024**2

def file_digest(path: Path, algorithm: str, git_blob: bool = False) -> str:
    h = hashlib.new(algorithm)
    if git_blob:
        h.update(f'blob {path.stat().st_size}\0'.encode())
    with path.open('rb') as f:
        while block := f.read(CHUNK):
            h.update(block)
    return h.hexdigest()

def match_file(path: Path, item: dict) -> bool:
    if not path.is_file() or path.stat().st_size != item['size']:
        return False
    if item.get('sha256'):
        return file_digest(path, 'sha256') == item['sha256']
    return file_digest(path, 'sha1', git_blob=True) == item['blob_id']

def valid_relative(name: str) -> bool:
    return bool(name) and not name.startswith('/') and '..' not in Path(name).parts and '\\' not in name

def validate_manifest(m: dict) -> None:
    if (m.get('repository'), m.get('revision')) != (MODEL_REPO, MODEL_REV):
        raise ValueError('Manifest is not the pinned official DeepSeek V4.1 Flash checkpoint')
    names = []
    for f in m['files']:
        if not valid_relative(f['path']) or not isinstance(f['size'], int) or f['size'] < 0:
            raise ValueError('Invalid manifest file entry')
        sha = f.get('sha256')
        blob = f.get('blob_id')
        expected, length = (sha, 64) if sha else (blob, 40)
        if not isinstance(expected, str) or len(expected) != length or any(c not in '0123456789abcdef' for c in expected):
            raise ValueError(f"No verifiable hash for {f['path']}")
        names.append(f['path'])
    if len(set(names)) != len(names):
        raise ValueError('Duplicate manifest paths')
    for required in ['config.json', 'model.safetensors.index.json',
                     'model-00047-of-00048.safetensors', 'model-00048-of-00048.safetensors']:
        if required not in names:
            raise ValueError(f'Incomplete manifest: {required}')

def get_manifest(path: Path, create: bool) -> dict:
    if path.exists():
        m = json.loads(path.read_text())
        validate_manifest(m)
        return m
    if not create:
        raise RuntimeError(f'Manifest missing: {path}; run plan on rank 0 and copy the manifest first')
    # Import only for online metadata/download. Offline verification uses stdlib only.
    from huggingface_hub import HfApi
    info = HfApi().model_info(MODEL_REPO, revision=MODEL_REV, files_metadata=True)
    if info.sha != MODEL_REV:
        raise RuntimeError('HF returned a different commit; refusing to follow main')
    rows = []
    for s in info.siblings:
        lfs = s.lfs
        sha = (lfs.get('sha256') if isinstance(lfs, dict) else getattr(lfs, 'sha256', None)) if lfs else None
        rows.append({'path': s.rfilename, 'size': s.size, 'sha256': sha, 'blob_id': s.blob_id})
    m = {'repository': MODEL_REPO, 'revision': MODEL_REV, 'files': sorted(rows, key=lambda f: f['path'])}
    validate_manifest(m)
    path.parent.mkdir(parents=True, exist_ok=True)
    # No overwrite of a concurrently created lock.
    with path.open('x') as f:
        json.dump(m, f, ensure_ascii=False, indent=2)
    return m

def check_stamp(stamp: Path, model: Path, manifest: Path) -> None:
    s = json.loads(stamp.read_text())
    if s.get('revision') != MODEL_REV or s.get('model_dir') != str(model.resolve()):
        raise RuntimeError('Verification stamp belongs to another model path or revision')
    if s['manifest_sha256'] != file_digest(manifest, 'sha256'):
        raise RuntimeError('Manifest changed after verification; re-run full verify')
    for item in s['files']:
        p = model / item['path']
        st = p.stat()
        if (st.st_size, st.st_mtime_ns) != (item['size'], item['mtime_ns']):
            raise RuntimeError(f'File changed after verification: {p}; re-run full verify')
    # This is a quick operational check, not a tamper-proof substitute for SHA hashing.

def verify_all(m: dict, model: Path, manifest: Path, stamp: Path) -> None:
    rows = []
    for i, item in enumerate(m['files'], 1):
        p = model / item['path']
        print(f"[{i}/{len(m['files'])}] SHA verify {item['path']}", flush=True)
        if not match_file(p, item):
            raise RuntimeError(f'Bad/missing model file: {p}; no automatic replacement or deletion')
        st = p.stat()
        rows.append({'path': item['path'], 'size': st.st_size, 'mtime_ns': st.st_mtime_ns})
    config = json.loads((model / 'config.json').read_text())
    if config.get('model_type') != 'deepseek_v41' or config.get('quantization_config', {}).get('expert_dtype') != 'fp4':
        raise RuntimeError('Unexpected model architecture or quantization; do not substitute EXL3')
    out = {'repository': MODEL_REPO, 'revision': MODEL_REV, 'model_dir': str(model.resolve()),
           'manifest_sha256': file_digest(manifest, 'sha256'), 'files': rows}
    stamp.parent.mkdir(parents=True, exist_ok=True)
    temp = stamp.with_suffix('.tmp')
    temp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    os.replace(temp, stamp)
    print(f'MODEL_HASH_PASS files={len(rows)} stamp={stamp}')

def reuse_one(src: Path, dst: Path, method: str) -> None:
    if dst.exists():
        raise RuntimeError(f'Destination unexpectedly exists: {dst}')
    dst.parent.mkdir(parents=True, exist_ok=True)
    if method == 'hardlink':
        # Explicit operator choice. Never chmod or mutate either link afterwards.
        os.link(src.resolve(), dst)
    else:
        tmp = dst.with_name(dst.name + '.reuse-in-progress')
        if tmp.exists():
            raise RuntimeError(f'Old incomplete copy requires inspection: {tmp}')
        subprocess.run(['cp', '--reflink=auto', '--sparse=always', '--', str(src), str(tmp)], check=True)
        os.replace(tmp, dst)

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['plan', 'download', 'verify'])
    p.add_argument('--config', default='config/cluster.json')
    p.add_argument('--rank', type=int, choices=range(4), default=0)
    p.add_argument('--reuse', action='append', default=[], help='Existing source directory; may be repeated')
    p.add_argument('--reuse-method', choices=['copy', 'hardlink'], default='copy')
    a = p.parse_args()
    c = load_config(a.config)
    root, model = Path(c['root']), Path(c['model_dir'])
    manifest = root / 'manifests/model-lock.json'
    m = get_manifest(manifest, create=a.action == 'plan')
    total = sum(f['size'] for f in m['files'])
    print(f'Pinned {MODEL_REPO}@{MODEL_REV}\nFiles={len(m["files"])} bytes={total:,} GB={total/1e9:.2f} GiB={total/1024**3:.2f}')
    if a.action == 'plan':
        for f in m['files']:
            existing = model / f['path']
            hints = [str(Path(d) / f['path']) for d in a.reuse if (Path(d)/f['path']).is_file()]
            status = 'exists (NOT YET HASHED)' if existing.is_file() else 'missing'
            print(f"{f['size']:>14,}  {f['path']}  {status}" + (f'  reuse candidates={hints}' if hints else ''))
        print('PLAN ONLY: no weights downloaded. Candidate filenames do not establish compatibility.')
        return
    if a.action == 'verify':
        verify_all(m, model, manifest, root / 'manifests' / f'model-verified-r{a.rank}.json')
        return
    model.mkdir(parents=True, exist_ok=True)
    from huggingface_hub import hf_hub_download
    for i, f in enumerate(m['files'], 1):
        dst = model / f['path']
        print(f"[{i}/{len(m['files'])}] {f['path']}", flush=True)
        if dst.exists():
            if not match_file(dst, f):
                raise RuntimeError(f'Destination has wrong hash: {dst}. Inspect/move aside manually; never overwrite old hardlinked assets')
            print('  verified existing; skipped')
            continue
        reused = False
        for d in a.reuse:
            src = Path(d) / f['path']
            if match_file(src, f):
                if a.reuse_method == 'copy' and shutil.disk_usage(model).free < f['size'] + 40 * 1024**3:
                    raise RuntimeError('Insufficient conservative copy budget (file +40 GiB). Review storage/reflink/hardlink choice')
                reuse_one(src, dst, a.reuse_method)
                print(f'  reused verified source: {src} ({a.reuse_method})')
                reused = True
                break
        if not reused:
            # Budget per file, not an assurance the entire remaining download will fit.
            if shutil.disk_usage(model).free < f['size'] + 40 * 1024**3:
                raise RuntimeError('Insufficient free space for next file +40 GiB safety margin; do not delete old deployment')
            hf_hub_download(MODEL_REPO, filename=f['path'], revision=MODEL_REV, local_dir=str(model))
        if not match_file(dst, f):
            raise RuntimeError(f'Post-copy/download hash mismatch: {dst}')
    verify_all(m, model, manifest, root / 'manifests' / f'model-verified-r{a.rank}.json')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, KeyError, subprocess.CalledProcessError) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        sys.exit(2)
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/preflight_container.py`

<!-- BEGIN_BUNDLE_FILE scripts/preflight_container.py dadcd83fabccd565d9c63335c25c11d27664870f3b525fe023c8f49544876154 -->
~~~~python
"""GPU/container probe; no model load, no downloads."""
import ctypes
import hashlib
import importlib.metadata
import json
import platform
import subprocess
from pathlib import Path

def main():
    import torch
    import flashinfer
    assert platform.machine() in ('aarch64','arm64'), platform.machine()
    assert torch.cuda.is_available() and torch.cuda.device_count()==1
    assert torch.cuda.get_device_capability(0)==(12,1)
    x=torch.ones((128,128),device='cuda',dtype=torch.bfloat16)
    assert (x@x)[0,0].item()==128
    torch.cuda.synchronize()
    nccl=ctypes.CDLL('/opt/nccl-mesh/lib/libnccl.so.2');ver=ctypes.c_int()
    assert nccl.ncclGetVersion(ctypes.byref(ver))==0 and ver.value==22907,ver.value
    ctypes.CDLL('/opt/nccl-mesh/lib/libnccl-net-mesh.so')
    patches=json.loads(Path('/opt/dsv41-ring/patch-manifest.json').read_text())
    for item in patches:
        assert hashlib.sha256(Path(item['target']).read_bytes()).hexdigest()==item['installed_sha256'],item['target']
    import vllm.models.deepseek_v4_1.common.engram as engram
    assert callable(engram.gather_engram_hashes)
    from flashinfer.mla import supported_sparse_mla_sm120_configs
    assert supported_sparse_mla_sm120_configs()['dsv4'].supports_decode(num_heads=16,topk=1152)
    print(json.dumps({'platform':platform.machine(),'torch':torch.__version__,'cuda':torch.version.cuda,
        'vllm':importlib.metadata.version('vllm'),'flashinfer':flashinfer.__version__,
        'nccl':ver.value,'patch_count':len(patches)},indent=2))
    for so in ('libnccl.so.2.29.7','libnccl-net.so'):
        lib=Path('/opt/nccl-mesh/lib')/so
        out=subprocess.check_output(['ldd',str(lib)],text=True)
        if 'not found' in out:raise RuntimeError(out)
        print(out)
    print('CONTAINER_PREFLIGHT_PASS (not a full-model inference pass)')

if __name__=='__main__':main()
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/ring_net.py`

<!-- BEGIN_BUNDLE_FILE scripts/ring_net.py bbc5b2a520c406cd3a8d9b3e0cd9b15c74d89b98145aee503338db12bb6bf355 -->
~~~~python
"""Temporary, scoped RDMA IPv4 configuration. No netplan/routing/sysctl changes.
Default action is a dry plan. Review every interface before --apply.
"""
from __future__ import annotations
import argparse
import datetime
import ipaddress
import json
import os
import shlex
import subprocess
from pathlib import Path
from common import load_config

def run(cmd: list[str], capture=False):
    if capture:return subprocess.check_output(cmd,text=True)
    print('+ '+shlex.join(cmd),flush=True)
    subprocess.run(cmd,check=True)

def net_state(dev):
    return json.loads(run(['ip','-j','address','show','dev',dev],True))[0]

def ipv4(state):
    return [a for a in state.get('addr_info',[]) if a['family']=='inet']

def addrstr(a):return f"{a['local']}/{a['prefixlen']}"

def build_plan(c,n,states):
    desired={n['prev_iface']:n['prev_cidr'],n['next_iface']:n['next_cidr']}
    cmds=[]
    for dev,s in states.items():
        for a in ipv4(s):cmds.append(['ip','address','del',addrstr(a),'dev',dev])
        if dev in desired:
            cmds += [['ip','link','set','dev',dev,'mtu','9000'],
                     ['ip','link','set','dev',dev,'up'],
                     ['ip','address','add',desired[dev],'dev',dev]]
    return cmds

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--config',default='config/cluster.json')
    p.add_argument('--rank',type=int,choices=range(4),required=True)
    p.add_argument('action',choices=['plan','apply','restore','check'])
    p.add_argument('--maintenance-approved',action='store_true')
    a=p.parse_args();c=load_config(a.config);n=c['nodes'][a.rank]
    touched=[n['prev_iface'],n['next_iface']]+n.get('unused_rdma_ifaces',[])
    mg=net_state(n['management_iface'])
    if n['management_ip'] not in [x['local'] for x in ipv4(mg)]:
        raise SystemExit('Configured management IP is not local: wrong rank or config')
    states={d:net_state(d) for d in touched}
    statefile=Path(c['root'])/'state'/f'network-r{a.rank}.before.json'
    if a.action=='restore':
        if not a.maintenance_approved or os.geteuid()!=0:raise SystemExit('Restore requires root and maintenance approval')
        saved=json.loads(statefile.read_text())
        if saved['management_ip']!=n['management_ip'] or set(saved['interfaces'])!=set(touched):
            raise SystemExit('Snapshot/config mismatch; restore manually from the saved inventory')
        for dev,old in saved['interfaces'].items():
            for x in ipv4(states[dev]):run(['ip','address','del',addrstr(x),'dev',dev])
            run(['ip','link','set','dev',dev,'mtu',str(old['mtu'])])
            for x in ipv4(old):
                cmd=['ip','address','add',addrstr(x),'dev',dev]
                if x.get('broadcast'):cmd+=['broadcast',x['broadcast']]
                if x.get('scope'):cmd+=['scope',x['scope']]
                if x.get('label') and x['label']!=dev:cmd+=['label',x['label']]
                if x.get('noprefixroute'):cmd+=['noprefixroute']
                run(cmd)
            run(['ip','link','set','dev',dev,'up' if 'UP' in old.get('flags',[]) else 'down'])
        out=statefile.with_name(statefile.name+'.restored-'+datetime.datetime.now().strftime('%Y%m%dT%H%M%S'))
        statefile.rename(out)
        print('RESTORED named-interface IPv4/MTU/admin flags. Reconnect original cables and verify old services separately.')
        return
    for dev,s in states.items():
        if s.get('master'):
            raise SystemExit(f'{dev} is enslaved to a bridge/bond; no automatic change')
        if not (Path('/sys/class/net')/dev/'device'/'infiniband').exists():
            raise SystemExit(f'{dev} is not an inspected RDMA PCI interface')
        for x in ipv4(s):
            if x.get('dynamic') or x.get('valid_life_time') not in (None,4294967295,'forever'):
                raise SystemExit(f'{dev} has DHCP/dynamic IPv4; resolve the network-manager profile first')
        routes=json.loads(run(['ip','-j','-4','route','show','table','all','dev',dev],True))
        for r in routes:
            if r.get('gateway') or (r.get('type','unicast')=='unicast' and r.get('protocol')!='kernel'):
                raise SystemExit(f'{dev} carries non-connected routes; review manually before using this helper')
    if a.action=='check':
        for side in ('prev','next'):
            dev=n[f'{side}_iface'];want=n[f'{side}_cidr']
            got=[addrstr(x) for x in ipv4(states[dev])]
            if got!=[want] or states[dev]['mtu']!=9000:
                raise SystemExit(f'{dev}: expected only {want}, MTU9000; actual {got}, MTU={states[dev]["mtu"]}')
            peer=c['nodes'][(a.rank-1)%4]['next_cidr'] if side=='prev' else c['nodes'][(a.rank+1)%4]['prev_cidr']
            peer=str(ipaddress.IPv4Interface(peer).ip)
            run(['ping','-I',dev,'-c','3','-W','3',peer])
            run(['ping','-I',dev,'-M','do','-s','8972','-c','3','-W','3',peer])
        for dev in n.get('unused_rdma_ifaces',[]):
            if ipv4(states[dev]):raise SystemExit(f'Unused RDMA interface still has IPv4: {dev}')
        print('ADJACENT_IP_AND_JUMBO_PASS (not proof of RDMA/NCCL)')
        return
    # Refuse overlap with any IPv4 subnet on any untouched interface.
    all_states=json.loads(run(['ip','-j','-4','address','show'],True))
    targets=[ipaddress.ip_interface(n[f'{s}_cidr']).network for s in ('prev','next')]
    for other in all_states:
        if other['ifname'] in touched:continue
        for x in ipv4(other):
            net=ipaddress.ip_interface(addrstr(x)).network
            if any(t.overlaps(net) for t in targets):
                raise SystemExit(f'RDMA subnet overlaps {other["ifname"]}: {net}')
    commands=build_plan(c,n,states)
    if a.action=='plan':
        print(json.dumps({'snapshot_to':str(statefile),'interfaces_before':states},indent=2))
        for cmd in commands:print(shlex.join(['sudo']+cmd))
        print('PLAN ONLY. Review physical ports and stop old GPU/data-plane work through the Gateway before apply.')
        return
    if not a.maintenance_approved or os.geteuid()!=0:
        raise SystemExit('Apply requires root and --maintenance-approved')
    statefile.parent.mkdir(parents=True,exist_ok=True)
    payload={'rank':a.rank,'management_ip':n['management_ip'],'interfaces':states,'timestamp':datetime.datetime.now().isoformat()}
    fd=os.open(statefile,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as f:json.dump(payload,f,indent=2)
    for cmd in commands:run(cmd)
    print('TEMPORARY_LINK_CONFIG_APPLIED. No netplan/default-route/sysctl/firewall change. Reboot may undo this.')

if __name__=='__main__':main()
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/ringctl.py`

<!-- BEGIN_BUNDLE_FILE scripts/ringctl.py 20fc7d73a940d08f2b7b904ad7ba2f30f0eb05ed3c62efa531a096d69abf5374 -->
~~~~python
#!/usr/bin/env python3
"""Per-node Docker launcher. Does not SSH, modify networking, or stop other workloads."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import shlex
import stat
import subprocess
import sys
import time
from common import CONTAINER, TEST_CONTAINER, PROJECT_LABEL, STAGES, load_config

LABEL_KEY, LABEL_VALUE = PROJECT_LABEL.split('=', 1)

def run(cmd: list[str], *, capture: bool = False) -> str:
    p = subprocess.run(cmd, check=True, text=True, stdout=subprocess.PIPE if capture else None)
    return p.stdout.strip() if capture else ''

def inspect_container(name: str) -> dict | None:
    p = subprocess.run(['docker', 'container', 'inspect', name], text=True, capture_output=True)
    if p.returncode:
        # A daemon/permission error is not proof that the name is free.
        run(['docker', 'info'], capture=True)
        if 'No such' not in p.stderr:
            raise RuntimeError(p.stderr.strip())
        return None
    return json.loads(p.stdout)[0]

def stop_owned(name: str) -> None:
    info = inspect_container(name)
    if info is None:
        print(f'Already absent: {name}')
        return
    if info.get('Config', {}).get('Labels', {}).get(LABEL_KEY) != LABEL_VALUE:
        raise RuntimeError(f'Refusing to stop {name}: ownership label mismatch')
    if info.get('State', {}).get('Running'):
        run(['docker', 'stop', '--time', '30', name])
    run(['docker', 'rm', name])

def check_local(n: dict) -> None:
    info = json.loads(run(['ip', '-j', '-4', 'addr', 'show', 'dev', n['management_iface']], capture=True))
    addrs = {a['local'] for x in info for a in x.get('addr_info', [])}
    if n['management_ip'] not in addrs:
        raise RuntimeError('Wrong node/rank or management interface: configured management IP not present locally')
    if not Path('/dev/infiniband').is_dir():
        raise RuntimeError('/dev/infiniband missing: inspect the existing RDMA driver first')

def check_key(path: str) -> None:
    p = Path(path)
    if p.is_symlink() or not p.is_file():
        raise RuntimeError('API env file must be a regular file, not a symlink')
    if stat.S_IMODE(p.stat().st_mode) & 0o077:
        raise RuntimeError('API env file must not be readable/writable by group/other; chmod 600')
    lines = [x for x in p.read_text().splitlines() if x and not x.startswith('#')]
    if len(lines) != 1 or not lines[0].startswith('VLLM_API_KEY=') or len(lines[0].split('=', 1)[1]) < 32:
        raise RuntimeError('API env must contain only VLLM_API_KEY=<at least 32 characters>')
    value = lines[0].split('=', 1)[1]
    if any(x.isspace() for x in value) or any(x in value for x in '\"\''):
        raise RuntimeError('Use an unquoted random hex API key in the Docker env file')

def mem_available_gib() -> float:
    for line in Path('/proc/meminfo').read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) / 1024**2
    raise RuntimeError('MemAvailable unavailable')

def docker_command(c: dict, rank: int, action: str, stage: str) -> list[str]:
    n = c['nodes'][rank]
    root = c['root']
    args = ['docker', 'run', '--pull', 'never', '--network', 'host', '--ipc', 'host',
            '--gpus', 'all', '--device', '/dev/infiniband:/dev/infiniband',
            '--ulimit', 'memlock=-1:-1', '--ulimit', 'stack=67108864', '--cap-add', 'IPC_LOCK',
            '--label', PROJECT_LABEL]
    if action == 'preflight':
        args += ['--rm']
    else:
        args += ['-d', '--restart', 'no', '--name', TEST_CONTAINER if action == 'test' else CONTAINER]
    env = {
        'LD_LIBRARY_PATH': '/opt/nccl-mesh/lib:/usr/local/cuda/lib64',
        'VLLM_NCCL_SO_PATH': '/opt/nccl-mesh/lib/libnccl.so.2',
        'PYTHONPATH': '/opt/dsv41-ring/runtime',
        'NCCL_NET_PLUGIN': 'mesh', 'NCCL_NET': 'Mesh', 'NCCL_ALGO': 'Ring',
        'NCCL_RUNTIME_CONNECT': '1', 'NCCL_SOCKET_IFNAME': '=' + n['management_iface'],
        'NCCL_SOCKET_FAMILY': 'AF_INET', 'GLOO_SOCKET_IFNAME': n['management_iface'],
        'VLLM_HOST_IP': n['management_ip'], 'TP_SOCKET_IFNAME': n['management_iface'],
        'MN_IF_NAME': n['management_iface'],
        'NCCL_MESH_GID_INDEX': str(n['mesh_gid_index']), 'NCCL_MESH_DISABLE_RDMA': '0',
        'NCCL_MESH_DEBUG': '1', 'NCCL_DEBUG': 'INFO', 'NCCL_DEBUG_SUBSYS': 'INIT,NET,GRAPH',
        'NCCL_NVLS_ENABLE': '0', 'NCCL_IGNORE_CPU_AFFINITY': '1',
        'TORCH_NCCL_ASYNC_ERROR_HANDLING': '1',
        'HF_HUB_OFFLINE': '1', 'TRANSFORMERS_OFFLINE': '1',
        'VLLM_USE_BREAKABLE_CUDAGRAPH': '1', 'VLLM_USE_RUST_FRONTEND': '0',
        'VLLM_HAS_FLASHINFER_CUBIN': '1', 'VLLM_USE_FLASHINFER_SAMPLER': '0',
        'TORCH_CUDA_ARCH_LIST': '12.1a', 'FLASHINFER_CUDA_ARCH_LIST': '12.1a',
        'FLASHINFER_DISABLE_VERSION_CHECK': '1', 'MAX_JOBS': '2', 'FLASHINFER_NVCC_THREADS': '1',
        'DSV41_ENGRAM_DISK': '1', 'DSV41_ENGRAM_DISK_THREADS': '32', 'DSV41_ENGRAM_DISK_CHUNK': '16',
        'VLLM_ENGINE_READY_TIMEOUT_S': '3600', 'PYTORCH_CUDA_ALLOC_CONF': 'expandable_segments:True',
    }
    if action == 'start':
        args += ['--memory', '112g', '--memory-swap', '112g', '--oom-score-adj', '500',
                 '--env-file', c['api_key_file'],
                 '-v', f"{c['model_dir']}:/models/DeepSeek-V4.1-Flash:ro",
                 '-v', f'{root}/cache:/cache']
        env.update({'HF_HOME': '/cache/huggingface', 'VLLM_CACHE_ROOT': f'/cache/vllm-{stage}',
                    'TILELANG_CACHE_DIR': '/cache/tilelang', 'TRITON_CACHE_DIR': '/cache/triton'})
        if STAGES[stage]['nvme']:
            args += ['-v', f'{root}/kv-offload:/kv-offload']
    for key, val in env.items():
        args += ['-e', f'{key}={val}']
    if action == 'preflight':
        args += ['--entrypoint', 'python3', c['image'], '/opt/dsv41-ring/scripts/preflight_container.py']
    elif action == 'test':
        args += ['--entrypoint', 'torchrun', c['image'], '--nnodes=4', '--nproc-per-node=1',
                 f'--node-rank={rank}', f"--master-addr={c['nodes'][0]['management_ip']}",
                 f"--master-port={c['test_port']}", '/opt/dsv41-ring/scripts/collective_test.py']
    else:
        args += [c['image'], '--stage', stage, '--rank', str(rank), '--master', c['nodes'][0]['management_ip'],
                 '--port', str(c['api_port']), '--master-port', str(c['master_port'])]
    return args

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('action', choices=['config', 'preflight', 'test', 'start', 'stop', 'stop-test', 'status'])
    p.add_argument('--config', default='config/cluster.json')
    p.add_argument('--rank', type=int, choices=range(4), required=True)
    p.add_argument('--stage', choices=STAGES, default='smoke32k')
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--maintenance-approved', action='store_true')
    p.add_argument('--accept-1m-risk', action='store_true')
    a = p.parse_args()
    c = load_config(a.config)
    n = c['nodes'][a.rank]
    if a.action == 'config':
        print(json.dumps({'node': n, 'root': c['root'], 'model_dir': c['model_dir'],
                          'stage': STAGES[a.stage]}, ensure_ascii=False, indent=2))
        return
    if a.action in {'preflight', 'test', 'start'}:
        cmd = docker_command(c, a.rank, a.action, a.stage)
        if a.dry_run:
            print(shlex.join(cmd))
            return
    check_local(n)
    if a.action == 'status':
        for name in (CONTAINER, TEST_CONTAINER):
            info = inspect_container(name)
            print(name, json.dumps(info.get('State', {}) if info else {'absent': True}))
        return
    if a.action in {'stop', 'stop-test'}:
        if not a.maintenance_approved:
            raise RuntimeError('Explicit --maintenance-approved is required to stop this deployment')
        stop_owned(CONTAINER if a.action == 'stop' else TEST_CONTAINER)
        return
    if not a.maintenance_approved:
        raise RuntimeError('First obtain the existing Gateway maintenance/exclusive-node lock; then pass --maintenance-approved')
    run(['docker', 'image', 'inspect', c['image']], capture=True)
    if a.action in {'test', 'start'}:
        name = TEST_CONTAINER if a.action == 'test' else CONTAINER
        if inspect_container(name) is not None:
            raise RuntimeError(f'{name} already exists. Preserve logs and stop it explicitly before a new run')
        # Refuse simultaneous communication tests and model runs from this package.
        other = CONTAINER if a.action == 'test' else TEST_CONTAINER
        oi = inspect_container(other)
        if oi and oi.get('State', {}).get('Running'):
            raise RuntimeError(f'{other} is running; finish and stop it first')
    if a.action == 'start':
        if a.stage.startswith('research1m') and not a.accept_1m_risk:
            raise RuntimeError('1M is a separately admitted experiment; --accept-1m-risk is required')
        check_key(c['api_key_file'])
        if mem_available_gib() < 100:
            raise RuntimeError('MemAvailable <100 GiB. Stop here; do not drop caches or kill unrelated processes automatically')
        procs = run(['nvidia-smi', '--query-compute-apps=pid,process_name', '--format=csv,noheader'], capture=True)
        if procs.strip():
            raise RuntimeError(f'Existing GPU compute processes; do not bypass Gateway:\n{procs}')
        for f in ('config.json', 'model.safetensors.index.json', 'model-00047-of-00048.safetensors',
                  'model-00048-of-00048.safetensors'):
            if not (Path(c['model_dir']) / f).is_file():
                raise RuntimeError(f'Model file missing: {f}; complete full hash verification first')
        for d in ('cache', 'logs', 'kv-offload', 'manifests'):
            (Path(c['root']) / d).mkdir(parents=True, exist_ok=True)
        stamp = Path(c['root']) / 'manifests' / f'model-verified-r{a.rank}.json'
        if not stamp.is_file():
            raise RuntimeError(f'Run model_assets.py verify --rank {a.rank} first; missing {stamp}')
        from model_assets import check_stamp
        check_stamp(stamp, Path(c['model_dir']), Path(c['root']) / 'manifests' / 'model-lock.json')
        if STAGES[a.stage]['nvme']:
            import shutil
            if shutil.disk_usage(Path(c['root']) / 'kv-offload').free < 80 * 1024**3:
                raise RuntimeError('NVMe tier needs >=80 GiB free on its filesystem before allocating 64 GiB')
    run(cmd)
    if a.action != 'preflight':
        print(f'Launched rank {a.rank}; this is NOT a readiness or inference pass. Inspect all four logs.')

if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print(f'STOP: {exc}', file=sys.stderr)
        sys.exit(2)
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`scripts/serve.py`

<!-- BEGIN_BUNDLE_FILE scripts/serve.py 0ea30fa79af14f252d6778201dd10b90a349ddfa922b399e2fede2ea9c2fbadc -->
~~~~python
"""Invoked inside the new container. Does not download or mutate models."""
import argparse
import json
import os
from pathlib import Path
from common import STAGES, model_args

def main():
    p=argparse.ArgumentParser()
    p.add_argument('--stage',choices=STAGES,required=True)
    p.add_argument('--rank',type=int,choices=range(4),required=True)
    p.add_argument('--master',required=True)
    p.add_argument('--port',type=int,required=True)
    p.add_argument('--master-port',type=int,required=True)
    p.add_argument('--dry-run',action='store_true')
    a=p.parse_args()
    if not os.environ.get('VLLM_API_KEY'):
        raise SystemExit('VLLM_API_KEY is required')
    model=Path('/models/DeepSeek-V4.1-Flash')
    config=json.loads((model/'config.json').read_text())
    if config.get('model_type')!='deepseek_v41' or config.get('quantization_config',{}).get('expert_dtype')!='fp4':
        raise SystemExit('Expected official native FP4/FP8 DeepSeek V4.1, not EXL3')
    for k in ('model.safetensors.index.json','model-00047-of-00048.safetensors','model-00048-of-00048.safetensors'):
        if not (model/k).is_file():raise SystemExit(f'Missing {k}')
    cmd=model_args(a.stage,a.rank,a.master,a.port,a.master_port)
    print(json.dumps({'stage':a.stage,'rank':a.rank,'argv':cmd},ensure_ascii=False),flush=True)
    if not a.dry_run:os.execvp(cmd[0],cmd)

if __name__=='__main__':main()
~~~~
<!-- END_BUNDLE_FILE -->

### 文件：`tests/test_bundle.py`

<!-- BEGIN_BUNDLE_FILE tests/test_bundle.py 3f38d19ae3b2fed09a8e18fe1c0a76fa3d26bd35adcd07fdc08bd219460f5782 -->
~~~~python
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE / 'scripts'))
import common
import model_assets as assets
import ringctl
import ring_net
import api_probe


def configured():
    c = json.loads((BASE / 'config/cluster.example.json').read_text())
    for i, n in enumerate(c['nodes']):
        n.update(management_ip=f'192.168.50.{20+i}', management_iface='mgmt0',
                 prev_iface='rdma0', next_iface='rdma1', unused_rdma_ifaces=['rdma2','rdma3'])
    return c

class ConfigTests(unittest.TestCase):
    def validate(self, c):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d)/'c.json'; f.write_text(json.dumps(c)); return common.load_config(f)
    def test_valid(self): self.assertEqual(len(self.validate(configured())['nodes']),4)
    def test_placeholder(self):
        with self.assertRaises((ValueError,KeyError)): self.validate(json.loads((BASE/'config/cluster.example.json').read_text()))
    def test_duplicate_rank(self):
        c=configured(); c['nodes'][2]['rank']=1
        with self.assertRaises(ValueError): self.validate(c)
    def test_three_nodes(self):
        c=configured(); c['nodes'].pop()
        with self.assertRaises(ValueError): self.validate(c)
    def test_duplicate_management(self):
        c=configured();c['nodes'][1]['management_ip']=c['nodes'][0]['management_ip']
        with self.assertRaises(ValueError):self.validate(c)
    def test_data_management_same_iface(self):
        c=configured();c['nodes'][0]['prev_iface']='mgmt0'
        with self.assertRaises(ValueError):self.validate(c)
    def test_broken_cable_subnet(self):
        c=configured();c['nodes'][1]['prev_cidr']='192.0.2.23/30'
        with self.assertRaises(ValueError):self.validate(c)
    def test_network_address_not_host(self):
        c=configured();c['nodes'][0]['next_cidr']='192.0.2.13/30'
        with self.assertRaises(ValueError):self.validate(c)
    def test_api_public_bind_rejected(self):
        c=configured();c['api_bind']='0.0.0.0'
        with self.assertRaises(ValueError):self.validate(c)
    def test_duplicate_ports(self):
        c=configured();c['master_port']=c['api_port']
        with self.assertRaises(ValueError):self.validate(c)
    def test_interface_shell_injection(self):
        with self.assertRaises(ValueError):common.iface_name('eth0;reboot')
    def test_path_volume_injection(self):
        with self.assertRaises(ValueError):common.abs_path('/data:/host')

class LaunchTests(unittest.TestCase):
    def test_smoke_has_eager_no_spec(self):
        a=common.model_args('smoke32k',0,'192.0.2.43',8891,29541)
        self.assertIn('--enforce-eager',a);self.assertNotIn('--speculative-config',a)
    def test_head_is_not_headless(self):
        self.assertNotIn('--headless',common.model_args('text256k',0,'x',8891,29541))
    def test_worker_headless(self):
        self.assertIn('--headless',common.model_args('text256k',3,'x',8891,29541))
    def test_graphs_exact_two_sequences(self):self.assertEqual(common.graph_sizes(5,2),[5,6,10,12])
    def test_native_ring_research_keeps_runtime_sequences(self):
        self.assertNotIn('--max-num-seqs',common.model_args('research1m',0,'x',8891,29541))
    def test_nvme_only_opt_in(self):
        self.assertNotIn('--kv-transfer-config',common.model_args('research1m',0,'x',8891,29541))
        self.assertIn('--kv-transfer-config',common.model_args('research1m-nvme',0,'x',8891,29541))
    def test_all_profiles_bounded_gmu(self):self.assertTrue(all(float(s['gmu'])<=.83 for s in common.STAGES.values()))
    def test_mesh_and_socket_separate(self):
        a=ringctl.docker_command(configured(),0,'start','smoke32k')
        self.assertIn('NCCL_NET=Mesh',a);self.assertIn('NCCL_SOCKET_IFNAME==mgmt0',a)
        self.assertNotIn('NCCL_NET=IB',a)
    def test_no_privileged_or_docker_socket(self):
        a=ringctl.docker_command(configured(),0,'start','text256k')
        self.assertNotIn('--privileged',a);self.assertNotIn('/var/run/docker.sock',' '.join(a))
    def test_secrets_not_rendered(self):
        a=ringctl.docker_command(configured(),0,'start','smoke32k')
        self.assertIn('--env-file',a);self.assertFalse(any(x.startswith('VLLM_API_KEY=') for x in a))
    def test_preserve_flashinfer_baked_cache(self):
        self.assertNotIn('/root/.cache/flashinfer',' '.join(ringctl.docker_command(configured(),0,'start','smoke32k')))
    def test_stop_rejects_unowned(self):
        with patch.object(ringctl,'inspect_container',return_value={'Config':{'Labels':{}}}):
            with self.assertRaises(RuntimeError):ringctl.stop_owned('dsv41-ring-tp4')
    def test_key_permissions(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'key';p.write_text('VLLM_API_KEY='+'a'*64+'\n');p.chmod(0o644)
            with self.assertRaises(RuntimeError):ringctl.check_key(str(p))
            p.chmod(0o600);ringctl.check_key(str(p))

class NetworkPlanTests(unittest.TestCase):
    def test_scoped_only(self):
        c=configured();n=c['nodes'][0]
        states={d:{'mtu':1500,'addr_info':[{'family':'inet','local':'192.0.2.33','prefixlen':30}]} for d in ['rdma0','rdma1']}
        a=ring_net.build_plan(c,n,states);text=json.dumps(a)
        self.assertNotIn('mgmt0',text);self.assertNotIn('netplan',text);self.assertNotIn('route',text)
        self.assertNotIn('flush',text);self.assertIn('9000',text)
    def test_unused_ipv4_removed_not_assigned(self):
        n=configured()['nodes'][0];states={'rdma2':{'addr_info':[{'family':'inet','local':'192.0.2.35','prefixlen':30}]}}
        a=ring_net.build_plan(configured(),n,states)
        self.assertEqual(a,[['ip','address','del','192.0.2.35/30','dev','rdma2']])

class AssetTests(unittest.TestCase):
    def test_sha256(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'f';p.write_bytes(b'abc')
            self.assertEqual(assets.file_digest(p,'sha256'),hashlib.sha256(b'abc').hexdigest())
    def test_git_blob_sha1(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'f';p.write_bytes(b'abc')
            self.assertEqual(assets.file_digest(p,'sha1',True),hashlib.sha1(b'blob 3\0abc').hexdigest())
    def test_wrong_existing_file_not_accepted(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'f';p.write_bytes(b'abc')
            self.assertFalse(assets.match_file(p,{'size':3,'sha256':'0'*64}))
    def test_reuse_hardlink_unchanged(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source';q=Path(d)/'new';p.write_bytes(b'old')
            assets.reuse_one(p,q,'hardlink')
            self.assertEqual(p.stat().st_ino,q.stat().st_ino);self.assertEqual(p.read_bytes(),b'old')
    def test_reuse_never_overwrites(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'source';q=Path(d)/'new';p.write_bytes(b'old');q.write_bytes(b'keep')
            with self.assertRaises(RuntimeError):assets.reuse_one(p,q,'hardlink')
            self.assertEqual(q.read_bytes(),b'keep')
    def test_manifest_path_traversal(self):self.assertFalse(assets.valid_relative('../config.json'))

class MockAPIHandler(BaseHTTPRequestHandler):
    def log_message(self,*a):pass
    def do_POST(self):
        obj=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        if self.headers.get('Authorization')!='Bearer test-key':
            self.send_response(401);self.end_headers();return
        if self.path=='/tokenize':result={'count':len(obj['prompt'].split())}
        else:result={'choices':[{'message':{'content':'323'}}],'usage':{'prompt_tokens':10,'completion_tokens':1}}
        body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)

class APIClientTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),MockAPIHandler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.url=f'http://127.0.0.1:{cls.server.server_port}'
    @classmethod
    def tearDownClass(cls):cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def test_authenticated_chat(self):
        r,t=api_probe.chat(self.url,'test-key','17*19');self.assertEqual(api_probe.answer(r),'323');self.assertGreaterEqual(t,0)
    def test_http_failure_surfaces(self):
        with self.assertRaises(RuntimeError):api_probe.chat(self.url,'wrong','hi')
    def test_needle_budget_fits_mock_tokenizer(self):
        text,count=api_probe.make_needle_text(self.url,'test-key',1000,.5,'abc123')
        self.assertLessEqual(count,1000);self.assertGreater(count,950);self.assertEqual(text.count('abc123'),1)
    def test_missing_api_key(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'key';p.write_text('NOT_KEY=x')
            with self.assertRaises(ValueError):api_probe.key_from_file(str(p))

if __name__=='__main__':unittest.main()
~~~~
<!-- END_BUNDLE_FILE -->

