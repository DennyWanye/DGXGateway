# MiniMax H3 FL2VA：4×DGX Spark 原始精度、Heretic 与 Turbo 安装方案

**资料核验日期：2026-09-17**  
**目标硬件：4 台 NVIDIA DGX Spark，每台 128GB 统一内存、1 个 GB10 GPU、Linux ARM64。**  
**交付范围：安装与验收方案 + 下载、启动、通信测试、视频验收脚本。不是已经替你的机器完成安装。**

> **先读这一段。** 原始 H3 FL2VA、兼容的 BF16 Heretic 编码器以及指定 Turbo LoRA 均有公开资源；SGLang 也有 H3 的 TP 与跨节点支持。但是，本次没有找到“本方案精确软件版本 + 4 台 Spark + BF16 FL2VA + Heretic/Turbo”全部组合已经端到端通过的公开证明，也没有在你的设备上实测。下文因此是**有来源、有版本记录、有停止条件的实验性四机集成方案**，不是“官方认证的一键四机镜像”。不要删掉现有可用环境来替换它。

## 1. 先修正四机部署的关键前提

此前将“四台共 512GB”直接等同于“开 SP4 就能让全部 BF16 权重常驻”的说法不准确。

- **SP / Ulysses 分割的是序列计算，不能据此推断权重被分成四份。**
- **TP 才会对相应线性层权重和注意力头做切分。** 仍有复制的参数、VAE、激活和临时加载内存，不能按总文件大小简单除以四计算峰值。
- Spark 的 CPU/GPU 共用内存；CPU offload 不是在同一台机器上凭空多出一份 128GB。

本方案从 **TP=4、SP=1、每节点 1 个进程** 开始，目的是先建立可核对的原始精度容量基线，而不是声称 TP4 的跨机速度最快。跨机 TP 的逐层通信开销可能很大；实际速度必须测试。[S2][S3][S4]

```text
每台均保存同一套模型文件，但运行时不等于每台加载全部权重常驻。

Spark 0 / rank 0 ─┐
Spark 1 / rank 1 ─┼─ TP4：共同完成一个视频任务
Spark 2 / rank 2 ─┤  SP1：先不叠加序列并行
Spark 3 / rank 3 ─┘
        ↑
    可信集群网络
        ↑
节点 0 的私有 HTTP API ← 你的 Gateway / Agent / ComfyUI 外部调用
```

## 2. 你要安装的四个档位

“越狱”和“速度”是两条独立维度，不需要下载四份完整 H3。

| 档位 | H3 主体 / VAE | 条件编码器 | Turbo | SGLang 请求中的 steps | 用途 |
|---|---|---|---|---:|---|
| `q` | 官方原始精度 | 官方 | 不加载 | `50` | 原版质量与故障排查基线 |
| `u-q` | 同上 | Heretic BF16 | 不加载 | `50` | 较少拒绝的实验质量档 |
| `p` | 同上 | 官方 | v4 step600 EMA | `9` | 原版批量/预览性能档 |
| `u-p` | 同上 | Heretic BF16 | v4 step600 EMA | `9` | Heretic + Turbo 性能档 |

四档均先关闭额外的近似缓存，使用 `quality: "lossless"`。这个字段仅控制推理器额外优化策略，**不代表 Heretic、Turbo 的输出与原模型无损等价**。`p` 与 `u-p` 仍然保留原始主体权重精度；提速主要来自少步蒸馏，不是偷偷改成 NVFP4。[S2][S6]

特别注意两件事：

**原始精度不是“所有 tensor 强制 BF16”。** 官方 H3 有 BF16 与必要 FP32 的精度策略，本方案保留它。不要对整个 pipeline 粗暴 `.to(torch.bfloat16)`。[S1][S2]

**8-step Turbo 在当前 SGLang H3 API 中填写 `num_inference_steps: 9`。** 这里包含终止 sigma=0，实际执行 8 次去噪网络求值。普通档沿用参考值 `50`，不自行改成别的步数。[S2]

## 3. 下载资源：只装 FL2VA，不把整个仓库全部拉下来

### 3.1 原版质量档：官方 H3

- 模型：<https://huggingface.co/MiniMaxAI/MiniMax-H3>
- 官方代码：<https://github.com/MiniMax-AI/MiniMax-H3>
- 本方案下载：根目录 `model_index.json`、`FL2VA/`，以及说明和许可证。
- **不下载** `Ref2VA/`、根目录另一套 Diffusers transformer、无关示例文件。
- 服务端 `--model-path` 指向模型**根目录**，通过 `--model-variant fl2va` 选择任务，不能改成直接指向 `.../FL2VA`。[S1][S2]

FL2VA 可做纯文本生成，也可使用第一帧、最后一帧或首尾两帧。人物参考、声音参考等 Ref2VA 任务不属于本包。[S1]

### 3.2 Heretic：“越狱”档所用的完整 BF16 编码器

- 仓库：<https://huggingface.co/llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic>
- 角色：替换 H3 使用的 Qwen3-VL 条件编码器，而不是替换 H3 视频主体。
- 接入方式：`--component-paths.text_encoder <本地完整仓库目录>`。

这是 SGLang H3 文档列出的兼容架构微调编码器接入路径。原生 H3 的视觉处理与中间层特征提取仍由运行时负责；**不要把通用 Qwen 的 tokenizer、processor 或 model_index 手动覆盖到 H3 基础目录**。[S2][S5]

这里没有把它命名成“MiniMax 官方 H3 Uncensored BF16”。它是一个**社区编码器组合**。降低文本模型拒绝倾向不等于所有视频内容必定生成，也没有足够证据承诺人物一致性、动作、音频、理解力均不损失。保持原版 `q`，用相同测试集比较后再决定默认档。[S5][S13]

### 3.3 Turbo：只下载一个指定适配器

- 仓库：<https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora>
- 指定文件：`minimax_h3_turbo_v4_step600_ema.safetensors`
- 默认 LoRA scale：`1.0`。
- 本方案性能档：8 次网络求值，API steps=`9`。

不要整库下载；仓库中有其他训练产物和历史版本。作者建议 6–8 步，并提示 4 步在高速动作上更容易出现拖影。本方案不把最激进的 4 步档当作最终成片默认值。[S6]

### 3.4 可选：你原有 ComfyUI 能用的单文件 Heretic 编码器

此项**不属于四机 SGLang 必需下载**，也不等于四机分布式方案。

仓库：<https://huggingface.co/ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot>

虽然仓库名字带 `INT8-ConvRot`，作者也提供 BF16 文件：

```text
qwen3vl_32b_h3_ultra_uncensored_heretic_bf16.safetensors
文件大小：51,506,295,440 bytes
SHA256：bbcd92a732e911cfafd86960e0e26aacc6efe949e02f16a9641f201c62984860
```

这个是裁切到 H3 所需结构的 ComfyUI 文件，放入对应 H3 text encoder 目录并用兼容加载器读取；它不是完整 Transformers 仓库。**不要拿它冒充本方案的 `H3-Heretic-Encoder/` 目录。** 这样做可以避免“下载的确实是 BF16，但加载格式不对”的错误。[S13]

## 4. 四机网络和空间前提

NVIDIA 当前 Cluster Assistant 的文档给出 2–4 台 Spark 的配置路线：最多三台可走相应无交换机方案，四台走交换机拓扑。网络工具只完成互联配置，不会替你安装视频模型。自行搭四机直连拓扑是另一项网络工程，不能因为已有两机 RDMA 就认定四机成立。[S7][S8]

要求：四台之间可互通；节点 0 的 rendezvous 地址对其他节点可达；驱动、系统与连接方式符合 NVIDIA 当前支持说明。初测走 TCP 确认分布式逻辑，再验证 RDMA 数据通道。**TCP 能跑不代表生产速度足够。**

每台建议预留 **350 GiB 以上独立可用空间，500 GiB 更宽裕**，这是工程预算，不是官方精确硬盘要求。四档同时保留会有基础权重、完整 Heretic 编码器、LoRA、Docker 镜像、可能的导出 tar 和临时缓存。附带下载脚本会按实时文件元数据报告实际字节量。

目录约定：

```text
/srv/h3/
├── models/
│   ├── MiniMax-H3/           # 含根 model_index.json 与 FL2VA/
│   ├── H3-Heretic-Encoder/   # 完整 Transformers 格式
│   └── H3-Turbo-v4/          # 一个指定 LoRA
├── cache/
├── manifests/               # 模型提交号、文件清单、镜像记录
├── input/                   # 首尾帧：四台相同路径、相同内容
├── output/
└── logs/
```

在每台机器上执行；不会改动现有 ComfyUI 或其他模型目录：

```bash
sudo install -d -m 0755 -o "$(id -u)" -g "$(id -g)" /srv/h3
mkdir -p /srv/h3/{models,cache,manifests,input,output,logs}
```

不要清理旧部署，不要先卸载驱动，也不要把新依赖装进现有 ComfyUI 的 Python 环境。

## 5. 填写配置：所有未知信息都由实机输出确定

解压压缩包，进入 `MiniMax-H3-4x-DGX-Spark` 目录。在每台机器上执行：

```bash
cp cluster.env.example cluster.env
cp node.env.example node.env
```

修改 `cluster.env`：四台完全一致。

```bash
H3_ROOT=/srv/h3
IMAGE=local/h3-sglang:20260917
HEAD_IP=填写节点0的可达集群IP
DIST_PORT=20000
TEST_PORT=29500
API_PORT=30010
API_BIND=127.0.0.1
NETWORK_MODE=tcp
PROFILE=q
```

修改 `node.env`：每台 rank 不同；接口名和 HCA 根据该机器实际输出填写。

```bash
NODE_RANK=0   # 其他三台分别填 1、2、3
NET_IFACE=填写本机用于集群通信的接口
NCCL_HCA=
```

查接口：

```bash
ip -br addr
ip route get 节点0的IP
rdma link show
```

不要照抄网上的 `eth0`、`enp...`、`mlx5_0` 或固定 GID index。脚本不会自动改 IP、路由、防火墙或网络拓扑。配置文件是 shell 文件，只加载你自己检查过的内容。

## 6. 构建一份独立 ARM64 镜像，再分发

### 6.1 只在节点 0 构建

本包固定了一个已存在的 ARM64 基础镜像 tag，以及本次检查的 SGLang 源码提交；**这个二者组合的 Docker 构建与 GPU 运行仍需你实机验证**。采用隔离镜像是为了不破坏当前可用环境。[S9][S10]

```text
基础镜像：lmsysorg/sglang:nightly-dev-cu13-20260916-c9a8fba9
SGLang 提交：279339f113b79af84f27fd3ac92d0a13bd3f4cbd
```

执行：

```bash
set -o pipefail
docker build --platform linux/arm64 --progress=plain \
  -t local/h3-sglang:20260917 . \
  2>&1 | tee /srv/h3/logs/build.log

docker image inspect local/h3-sglang:20260917 \
  > /srv/h3/manifests/image-inspect.json

docker image inspect local/h3-sglang:20260917 \
  --format '{{.Id}} {{.Architecture}}'
```

架构必须为 `arm64`。如果构建阶段依赖解析/原生扩展失败，保留 `build.log` 并停在这里；不要继续下载全部模型来碰运气，也不要直接在宿主机升级 torch 补救。

这个 Dockerfile 不是依赖逐文件 hash 锁定的可重建构建；安装时依赖解析仍可能受上游影响。因此**真正保证四台一致的做法是构建一次，导出该镜像并分发**，而不是每台自行重新构建。

### 6.2 运行单节点预检

```bash
bash scripts/00-preflight.sh \
  2>&1 | tee /srv/h3/logs/preflight.log
```

预检会检查 ARM64、单个 CUDA GPU、SM121、BF16 小矩阵运算、torchaudio 动态库和关键 CLI 参数。**通过不等于 H3 已完成生成。**

### 6.3 导出给另外三台

```bash
docker save -o /srv/h3/h3-sglang-20260917.tar local/h3-sglang:20260917
sha256sum /srv/h3/h3-sglang-20260917.tar \
  > /srv/h3/manifests/image-tar.sha256
```

用已有 SSH/rsync 将 tar、这个脚本目录复制到另三台。示例中的用户名和节点名必须替换：

```bash
rsync -a --partial --info=progress2 \
  /srv/h3/h3-sglang-20260917.tar 用户@节点1:/srv/h3/

# 不要将节点0的 node.env 覆盖到其他节点，否则会出现四个 rank 0。
rsync -a --exclude=node.env ./ 用户@节点1:~/MiniMax-H3-4x-DGX-Spark/
```

节点 2、3 同理。各从节点执行：

```bash
docker load -i /srv/h3/h3-sglang-20260917.tar
docker image inspect local/h3-sglang:20260917 --format '{{.Id}} {{.Architecture}}'
# 检查本机 node.env 后，再运行：
bash scripts/00-preflight.sh
```

四台的 image ID 必须相同。不要把 Hugging Face Token 写入镜像。

## 7. 先验证四机通信，不先加载 100GB 级模型

### 7.1 TCP 诊断

四台 `NETWORK_MODE=tcp`，rank 分别为 0/1/2/3。在四台终端中分别启动同一个脚本：

```bash
set -o pipefail
bash scripts/02-test-node.sh 2>&1 | tee /srv/h3/logs/nccl-tcp.log
```

正确结果：四台都打印 `NCCL_ALLREDUCE_PASS rank=...`；运行时版本检查一致；所有归约结果正确。测试包含两个数据规模，但输出只是本脚本的计时，不是标准 NCCL bus bandwidth 分数。

某节点没有启动时，其余节点会等待；不是只在节点 0 执行四次。`TEST_PORT` 和 `DIST_PORT` 仅供可信集群网络使用，不暴露到公网。

### 7.2 RDMA 验证

完成实际四机交换机/RDMA 配置后，将四台 `cluster.env` 改成：

```bash
NETWORK_MODE=rdma
```

每台 `node.env` 填入实测设备，例如 NCCL 的精确匹配语法是：

```bash
# 仅示意格式，设备名不是你机器的已知配置：
NCCL_HCA='=实际HCA名字:实际端口'
```

再次在四台运行测试，日志另存为 `nccl-rdma.log`。检查 NCCL **数据传输通道**是否确实采用预期的 IB/RoCE 设备；仅看到某条 Socket 初始化日志不能简单判定 RDMA 失败，反之设置了 `NCCL_IB_DISABLE=0` 也不等于证明链路性能达标。[S11]

本包通过 Docker 暴露 `/dev/infiniband`，不使用 `--privileged`，也不挂载 Docker socket。缺设备、权限或驱动时会直接失败。没有通过网络测试前不要启动 H3。

## 8. 只在节点 0 下载模型，保存不可变 revision

### 8.1 先查看文件清单和空间

如仓库要求认证，先在当前终端设置合法 Hugging Face 凭据。不要把 Token 写到脚本或日志中。

```bash
docker run --rm --network host \
  -e HF_TOKEN -e HF_HOME=/srv/h3/cache \
  -v /srv/h3:/srv/h3 \
  -v "$PWD":/opt/h3-bundle:ro \
  --entrypoint python local/h3-sglang:20260917 \
  /opt/h3-bundle/scripts/01-download-models.py --root /srv/h3
```

这一步只访问元数据、记录模型提交 SHA 和文件清单，**不下载权重**。首次执行生成 `/srv/h3/manifests/model-lock.json`，再次执行沿用相同 revision，不会静默追随新的 `main`。

### 8.2 下载四档所需资源

```bash
docker run --rm --network host \
  -e HF_TOKEN -e HF_HOME=/srv/h3/cache \
  -v /srv/h3:/srv/h3 \
  -v "$PWD":/opt/h3-bundle:ro \
  --entrypoint python local/h3-sglang:20260917 \
  /opt/h3-bundle/scripts/01-download-models.py \
    --root /srv/h3 --download --verify-sha256
```

下载中断后重跑同一命令，不要删除已下载的数据。`--verify-sha256` 会额外读取大文件并校验仓库提供的 LFS hash；小型普通 Git 文件仍按清单与长度检查。

先只做原版验证时，可给计划和下载命令都追加 `--base-only`。以后去掉该参数补齐 Heretic 与 Turbo，不需要重新下载已锁定的基础权重。

如已有相同官方 FL2VA 文件，可以复用到上述目录；必须核对格式、revision 和内容。既有 INT8/NVFP4、pruned 或其他格式文件不能改名当成原始 BF16 使用。当前其他 LLM 的模型权重无法代替 H3 组件。

### 8.3 同步到其他三台

各目标机器先创建 `/srv/h3`，然后从节点 0 执行，替换实际用户名/地址：

```bash
rsync -a --partial --info=progress2 /srv/h3/models/ 用户@节点1:/srv/h3/models/
rsync -a /srv/h3/manifests/model-lock.json 用户@节点1:/srv/h3/manifests/
```

节点 2、3 同理。命令没有 `--delete`，不会删除目标机器其他模型。首轮用本地 NVMe 副本，不用未经测试的远端网络文件系统承担实时权重读取。

每台做完整模型校验：

```bash
python3 scripts/check_models.py --root /srv/h3 --profile u-p --sha256
```

只下载了 `--base-only` 时改成 `--profile q`。四台 model-lock SHA 必须相同。随后再跑一次四机通信测试，它会比对运行时版本、档位和锁文件摘要，但不会替代逐个权重文件的内容校验。

## 9. 启动原版质量档 `q`

确认四台 `PROFILE=q`，没有并行运行会占满统一内存的 LLM/ComfyUI 模型服务。先给 H3 独占测试条件。

在四台各启动一次：

```bash
set -o pipefail
bash scripts/03-start-node.sh 2>&1 | tee /srv/h3/logs/h3-q.log
```

脚本的关键参数是：

```bash
sglang serve \
  --model-path /srv/h3/models/MiniMax-H3 \
  --model-variant fl2va \
  --served-model-name h3-fl2va-q \
  --num-gpus 4 --nnodes 4 --node-rank 本机的0至3 \
  --dist-init-addr 节点0的IP:20000 \
  --tp-size 4 --sp-degree 1 --ulysses-degree 1 --ring-degree 1 \
  --encoder-parallel replicate \
  --performance-mode speed \
  --attention-backend torch_sdpa \
  --enable-torch-compile false \
  --warmup-mode off \
  --host 127.0.0.1 --port 30010
```

解释仅涉及本方案的选择：

- `num-gpus=4` 是全局总数，不是每台四张卡。`nnodes=4` 后每台一个工作进程。[S3]
- `encoder-parallel=replicate` 是关闭跨额外 SP 维度的自动 encoder folding；在本方案纯 TP4 中，编码器保留在既有 TP 组内，**不是因为这个单词就重新变成每台一份完整编码器**。[S4]
- 先用 `torch_sdpa` 避免直接套用其他 Blackwell 芯片的专用 attention kernel。它仍需通过真实 H3 请求验证；小矩阵成功不证明所有算子成功。[S12]
- 暂不开 compile、近似缓存、VSA、pruned、量化或 AdaLN 特殊缓存，减少变量。
- 不显式启用 CPU offload；仍要从加载日志确认实际放置策略与峰值，不能仅凭 `speed` 名称声称全部常驻。
- 模型目录运行时只读，且设置 HF offline。缺文件应修复下载/同步，不让四台服务各自上网补不同版本。

脚本启动时会打印完整参数。日志中必须确认 world size=4、TP=4、模型变体 FL2VA、正确编码器，没有预期外的量化覆盖。加载时或首帧解码时的峰值内存同样需要记录。

## 10. 实际生成并验收，不能只看 `/health`

预检和 `/health` 成功不代表真的出片。脚本特意关闭自动 warmup，避免启动时自动尝试较大分辨率；因此必须主动做下面的请求。

客户端需要 Python 3，以及 `ffmpeg` / `ffprobe`。客户端可在节点 0 的宿主机运行；宿主机缺少 ffmpeg 时用下面的镜像运行形式，无需往原有 ComfyUI Python 里装依赖。

### 10.1 4 秒、480 短边的链路测试

```bash
docker run --rm --network host \
  -v /srv/h3:/srv/h3 -v "$PWD":/opt/h3-bundle:ro \
  --entrypoint python local/h3-sglang:20260917 \
  /opt/h3-bundle/scripts/04-generate.py \
    --profile q --seconds 4 --short-edge 480 \
    --output /srv/h3/output/q-smoke.mp4
```

低分辨率只是工程诊断，不是该模型画质结论。如果这个分辨率在实机版本上被明确拒绝，不要擅自绕过 shape 校验；记录错误后按原生 768 档重做。

### 10.2 5 秒、768 短边的质量基线

```bash
docker run --rm --network host \
  -v /srv/h3:/srv/h3 -v "$PWD":/opt/h3-bundle:ro \
  --entrypoint python local/h3-sglang:20260917 \
  /opt/h3-bundle/scripts/04-generate.py \
    --profile q --seconds 5 --short-edge 768 --seed 20260917 \
    --output /srv/h3/output/q-768.mp4
```

每个请求都会提交一次异步任务、记录 job id、轮询状态、下载视频，再用 ffprobe 和 ffmpeg 验证真实音视频及完整解码。错误 JSON 不会被当成成功的 MP4。文件存在时拒绝覆盖。

如果等待超时，脚本不会假定任务已取消，也不会自动重新 POST 创建重复任务。保留 `.job.json`；复用同样请求参数并加 `--resume-id 实际job-id`，继续检查原任务。

### 10.3 首帧/首尾帧 FL2VA

把图片放到四台的同一路径，且内容相同：

```text
/srv/h3/input/first.png
/srv/h3/input/last.png
```

同步示例：

```bash
rsync -a /srv/h3/input/ 用户@节点1:/srv/h3/input/
```

在前面的生成命令追加：

```bash
--first /srv/h3/input/first.png \
--last /srv/h3/input/last.png \
--prompt 'The subject continues naturally from the opening frame to the final pose, with coherent motion and synchronized room ambience.'
```

只给 `--first` 就是首帧控制。这里是**服务端文件路径**，不是 Mac 上的图片路径，也不是文本提示词中假装带了一张图片。客户端会转换为 H3 的 `conditions`，首帧索引 `0`、尾帧索引 `-1`。[S2]

### 10.4 合格条件

生成结果至少满足：一个 H.264 视频流、一个 AAC stereo 音频流、可完整解码；检查实际 24fps、32kHz 音频、输出尺寸与时长。脚本会检查这些结构指标，**不会替你判断电影画质**。[S2]

另用肉眼/评审模型检查：首帧是否对应、人物有无漂移、手部与接触动作、相机运动、拖影、音画同步。保存请求 JSON、种子、四台日志、model-lock、image ID 和生成结果。

## 11. 切换到 Heretic 和 Turbo：一次只改一个维度

先让当前请求正常结束，再在四台**只停止本包的容器**：

```bash
docker stop h3-infer
```

它不会删除模型；不要使用全局停止所有容器、`docker system prune` 或清理模型目录的命令。

修改四台 `cluster.env` 的 `PROFILE`，再四台重新运行 `03-start-node.sh`。

### A. `PROFILE=u-q`：Heretic 质量档

基础模型不变，只增加：

```bash
--component-paths.text_encoder /srv/h3/models/H3-Heretic-Encoder
```

客户端使用 `--profile u-q`，输出换成新的文件名。保持相同提示词、种子、时长和分辨率。注意检查日志确认**真正加载的是 Heretic 编码器**，而不是仍在使用原版缓存。

### B. `PROFILE=p`：原版 Turbo 性能档

主体与原版编码器不变，增加：

```bash
--lora-path /srv/h3/models/H3-Turbo-v4 \
--lora-weight-name minimax_h3_turbo_v4_step600_ema.safetensors \
--lora-nickname h3-turbo-v4 \
--lora-scale 1.0 \
--lora-merge-mode auto
```

客户端使用 `--profile p`，它会发送 `num_inference_steps=9`、`lora_scale=1.0`。不要只把原版步数调低到 9 却忘记加载 Turbo，也不要把 `50` 误当成 Turbo 的“更高画质模式”。[S2][S6]

### C. `PROFILE=u-p`：Heretic + Turbo

同时启用上述两个覆盖。只有 `u-q` 与 `p` 分别通过后再测试此组合，便于判断质量变化来自编码器还是蒸馏。

四台必须使用相同档位。仅改变客户端 `--profile` 不会远程为服务端加载模型；本方案每次切档重启四台，避免复杂的动态 LoRA 状态混用。

## 12. 性能测试：哪些能推算，哪些不能

本方案没有可信的“4×Spark BF16 每条 5 秒视频需多少秒”的已测数值。其他机器的 H200/B300 成绩，以及社区单/双 Spark 的 FP8 成绩，不能换算成这里的保证。[S2][S14][S15]

单请求可以这样记账：

```text
总耗时 = 条件编码 + 去噪(含跨机通信) + VAE解码 + 写文件/服务开销
```

Turbo 将此处普通参考档的 49 次求值变成 8 次，调用次数比约 6.1，但这**不是整条流水线加速 6.1 倍的承诺**：条件编码和解码仍存在，每次求值的实现、通信及缓存状态也会影响实际结果。

固定同一组 6 个提示场景，分别覆盖静物细节、人物近景、双人交互、手物接触、快速运动和相机移动；每组至少两个固定种子。先跑 `q`，再对比其他档。这是本方案建议的验收样本，不是标准行业榜单。

每档先排除冷启动影响，再重复测量同一请求；记录中位数与慢尾耗时。不要把只跑一次的最佳成绩当吞吐率。初始同时在途任务数设为 **1**。

在每台观察：

```bash
free -h
vmstat 1
nvidia-smi
```

特别看统一内存余量、持续 swap、解码瞬间峰值、网络通道以及是否有另一个 ComfyUI/LLM 服务抢占资源。`nvidia-smi` 一个“显存”数字不足以代表整台 Spark 的内存健康。

优化顺序建议：

1. 确认原版 `q` 与网络正确；不要一边排障一边改采样器。
2. 先测 `p` / `u-p` 的少步蒸馏收益。
3. 再比较其他并行布局或 2+2 分组的**实际**单条延迟/批量吞吐；本包不把未测试的多种拓扑做成“一键高速切换”。
4. 最后才讨论 FP8/INT8、近似 attention 或编译；一旦引入，单独命名并重新做质量验收，不能继续当成原始 BF16 基线。

**不要直接使用 `quality: "high"` 作为 Spark 的默认加速开关。** 当前 H3 文档对其已审计的硬件和请求形状有限定，不能把 H200 的测量当成 Spark 承诺；Turbo 也不应未经验证再叠加这一近似路径。[S2]

## 13. 与 ComfyUI / 你的短剧工作流怎么连接

本包后端是 SGLang 的 HTTP 视频任务服务，不是“四个 ComfyUI 各加载一遍就自动合卡”。

建议先把后端独立跑通，再让 Gateway / Agent / 你自行编写的 ComfyUI API 节点调用：

```text
POST /v1/videos
GET  /v1/videos/{id}
GET  /v1/videos/{id}/content
```

前端持有任务队列与 job id，不在连接掉线时盲目重发。验收阶段保持一个在途任务。已有 ComfyUI 模型加载节点仍会在它所在设备加载本地模型，不能用来调用这个四机服务。

默认 API 仅监听节点 0 的 loopback。其他电脑访问时可用 SSH 隧道：

```bash
ssh -N -L 30010:127.0.0.1:30010 用户@节点0
```

如果前端在 Docker bridge 网络中，它自己的 `127.0.0.1` 不是宿主机；需单独规划可达地址，或部署带认证的内网反向代理。不要为了省一步把无认证 API 和分布式端口暴露到公网。

## 14. 故障与明确停止条件

| 现象 | 先检查 | 不要做 |
|---|---|---|
| 镜像是 amd64 或 CUDA 不可用 | 镜像架构、NVIDIA Container Toolkit、驱动兼容性 | 用模拟架构继续跑 |
| `libtorchaudio.so` 等导入失败 | 容器内 torch/torchaudio 的版本与 ABI | 在宿主 ComfyUI 环境直接升级包 |
| `sm_121` / `no kernel image` / attention 不支持 | 哪个实际算子失败、该容器是否覆盖 GB10 | 仅因也是 Blackwell 就套 SM100/103 的内核 |
| NCCL 卡住 | 四台是否全启动、rank 唯一、head IP、接口、可信内网端口 | 认为等更久必然成功 |
| TCP 通，RDMA 不通 | 交换机/链路、HCA 端口、容器设备、NCCL 数据通道 | 固定写死别人机器的 GID index |
| 权重加载或 VAE 解码 OOM | TP 是否生效、阶段峰值、并存服务、加载时临时副本 | 把 512GB 当单机内存；疯狂加 swap 当优化 |
| Heretic 加载格式错误 | 是否完整 Transformers 仓库、组件路径是否正确 | 把 Comfy 单文件改名装进原目录 |
| Turbo 无效/结果异常 | 精确文件名、LoRA scale、全部节点档位、steps=9 | 只改 steps；或给 Turbo 随意加到 50 步 |
| `/health` 成功但首请求失败 | 实际生成/解码日志、模型与组件路径 | 宣称部署成功 |
| 服务试图联网补文件 | 缺失清单、根目录 model_index、组件资料 | 解除 offline 后让各节点各自升级 |

如果 TP4 在 GB10 上被明确报为未支持，**本方案就在那个检查点停止**。保留日志、版本、节点拓扑；需要修复运行时或改用有证据的其他部署路线，不能把“能下载权重”描述为“已经跑通”。社区单 Spark / 双 Spark FP8 实现是不同组合，不能通过把节点数字改成 4 就声称继承它们的验证。[S14][S15]

## 15. 许可与“完全本地”的边界

本包是本地 H3 Base FL2VA，不调用官方 Context-IR 或 Regenerate-2K。它不是官方完整云端 2K 工作流；后期本地放大也不等于官方 2K 再生成。[S1]

模型使用权以各权重仓库许可证为准，不由推理代码的开源许可证代替。MiniMax Community License 包含地域、使用和分发条件；部署、对外服务或商业使用前，应核对当前文本。Heretic / Turbo 修改不会自动解除这些义务。本方案仅面向合法、获授权的内容和素材使用。[S16]

## 16. 本交付实际验证了什么

- 已核对模型、编码器、LoRA 的公开来源和相关运行时文档。
- 已在本地环境检查本包 Python/Shell 语法、请求构造、下载范围和错误处理等测试。
- HTTP 客户端使用模拟服务验证，不代表真实模型生成通过。
- **未在此环境中构建 ARM64 GPU 镜像；未在四台 Spark 上执行 NCCL、加载 H3 或进行生成测速。**

本包测试命令：

```bash
python3 -m unittest discover -s tests -v
for f in scripts/*.sh; do bash -n "$f"; done
```

正式验收需要的证据：四台同镜像同模型清单、四机通信成功、`q` 的真实 768 输出、四档相同场景的 A/B 结果、峰值内存和耗时记录。证据齐全后，再把通过的版本固化为你自己的稳定部署。

## 17. 来源索引

以下为本次核验入口；上游会更新，本包通过模型 revision 锁与镜像分发来避免四台各自漂移。文档中的策略选择与测试脚本是本次工程设计，不冒充上游逐条原样提供的“四 Spark 官方教程”。

- [S1] MiniMax 官方模型卡及原始代码：<https://huggingface.co/MiniMaxAI/MiniMax-H3>；<https://github.com/MiniMax-AI/MiniMax-H3>
- [S2] SGLang H3 cookbook：<https://docs.sglang.io/cookbook/diffusion/MiniMax/MiniMax-H3>
- [S3] SGLang 并行轴和跨机参数：<https://github.com/sgl-project/sglang/blob/279339f113b79af84f27fd3ac92d0a13bd3f4cbd/docs/docs/sglang-diffusion/parallelism.mdx>；<https://docs.sglang.io/docs/sglang-diffusion/ring_sp_performance>
- [S4] Encoder parallelism：<https://github.com/sgl-project/sglang/blob/279339f113b79af84f27fd3ac92d0a13bd3f4cbd/docs/docs/sglang-diffusion/encoder_parallel.mdx>
- [S5] Heretic 完整 BF16 编码器：<https://huggingface.co/llmfan46/Qwen3-VL-32B-Instruct-ultra-uncensored-heretic>
- [S6] Larry Turbo 作者说明：<https://huggingface.co/larryvrh/MiniMax-H3-Turbo-Lora>
- [S7] NVIDIA Cluster Assistant：<https://docs.nvidia.com/sync/latest/cluster-assistant.html>
- [S8] DGX Spark 当前发布说明：<https://docs.nvidia.com/dgx/dgx-spark/release-notes.html>
- [S9] SGLang 安装指南：<https://docs.sglang.io/docs/sglang-diffusion/installation>
- [S10] 官方 Docker tags / ARM64 清单：<https://hub.docker.com/r/lmsysorg/sglang/tags>
- [S11] NVIDIA NCCL 环境变量：<https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/env.html>
- [S12] SGLang attention 与 CLI：<https://docs.sglang.io/docs/sglang-diffusion/attention_backends>；<https://docs.sglang.io/docs/sglang-diffusion/api/cli>
- [S13] H3 专用 Heretic 编码器作者说明及 BF16 校验值：<https://huggingface.co/ethanfel/Qwen3-VL-32B-Ultra-Heretic-H3-ComfyUI-INT8-ConvRot>
- [S14] 社区单 Spark 实现，不能视为本包的四机 BF16 证明：<https://github.com/joeynyc/MiniMax-H3-DGX-Spark>
- [S15] 社区双 Spark 实现，同样不是四机证明：<https://github.com/joeynyc/MiniMax-H3-2x-DGX-Spark>
- [S16] MiniMax H3 许可证：<https://huggingface.co/MiniMaxAI/MiniMax-H3/blob/main/LICENSE>
