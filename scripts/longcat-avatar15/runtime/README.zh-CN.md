# LongCat Avatar 1.5 单机运行适配

本目录对应执行合同（历史内部记录，公开版不包含）、评估计划（历史内部记录，公开版不包含）及原始指导（历史内部记录，公开版不包含）。只生成新入口，原始源码保持不变；无 attention fallback。

## 2026-09-09 现场验证更新

候选镜像 `sha256:a8b6aee9435f0a96b3b5aea7b09788e81ff3d675511eadefe3b8759ad6d3c1a1` 已在 head 构建。pip check PASS；真实GB10 BF16、FA dense/varlen与完整Pipeline导入PASS，证据为 `plans/longcat-avatar15/evidence/install/runtime-candidate.json`。meta_probe在同一NGC核心版本CPU执行PASS，误差0；prepare_runtime也已在固定源码运行成功。以下表格保留编写阶段状态，最新完整模型和媒体验收以执行日志（历史内部记录，公开版不包含）为准。完整模型仍待下载和受控推理，不能以小张量或toy通过代替。

## 产物及状态

| 项目 | 行为 | 已验证 / 未验证 |
|---|---|---|
| `prepare_runtime.py SOURCE_DIR` | 对固定提交所有 Python 文件及 requirements.txt 校验 SHA-256；新增 `run_avatar15_clean_speech.py`、helper、review diff | 本地纯 Python 已过；目标镜像未执行 |
| clean-speech | 删除新入口的 Separator 导入、初始化和分离函数，直接 librosa 读取原始音频，完全去掉该入口的音频删除代码；读取前后 SHA-256 比较 | 原输入保留测试已过；真实音频链未运行 |
| DMD | 任何模型/CUDA 初始化前强制 2,523,077,984 字节和固定 SHA-256；必须真实注册非空 adapter 且启用 | 缺失/大小/哈希/空注册/启用失败门的纯 Python 测试已过；真实权重未加载 |
| timeout | NCCL 24h 改 600s；外部探针 5min、加载 30min、首段 60min 仍由调度方管控 | 生成源码 AST 检查通过；未测故障超时 |
| `runtime_probe.py` | GB10/SM121、BF16 GEMM、FA dense 自/交叉注意力及不同序列长度 varlen；FP32 SDPA MATH 参考，固定 seed、finite、max_abs≤0.03、relative_RMS≤0.015；Pipeline import | 已语法验证；真实 GPU 尚未运行，不能标 PASS |
| `meta_loader.py` | 显式 `LONGCAT_META_LOAD=1` 才启用的候选 meta + assign 分片加载 | 已静态评审及语法验证；toy/完整模型尚未实测 |
| `meta_probe.py` | 目标镜像内 CPU tiny Conv3d/INT8 Linear/未量化 Linear/LayerNorm 对照；state/dtype/forward 必须完全一致，缺 index key 必须报错 | 交主代理在实际 Torch 运行 |

运行前建议权重目录和输入目录挂只读；最终运行后仍要独立核对原输入哈希。600 秒是通信超时，不能替代外部任务/取消控制。

## INT8 峰值审查

上游直接构造完整 FP32 DiT，随后把全部 QuantizedLinear 新 buffers 收集到 dict，直到所有新模块已建好才逐个替换旧 Linear。因此此阶段全 FP32 权重和所有 INT8 buffers 可以同时在内存，另外前面已加载 UMT5 和 VAE。最后又汇集全部分片 state_dict 并 copy 到模型。静态估计 FP32 DiT≈63GB + INT8≈16GB + UMT5≈23GB（十进制数量级），不包含 Python、Whisper、DMD、激活和系统余量；它并非真实峰值测量。

候选构造使用 `torch.device('meta')`，包含参数与 QuantizedLinear buffers；RoPE 构造只有普通配置和空字典，频率在第一次 forward 才计算，故不需要补全已计算的频率常量。候选遇非持久 buffers 或直接未注册的 meta Tensor 属性即失败，不使用可能留下未初始化状态的 `to_empty()`。`weight_map` 必须与模型 state 全等，每一 shard header 的 key/shape/dtype 都核对，禁止重复/逃逸路径；逐片 `load_state_dict(strict=False, assign=True)` 后检查无剩余 meta。权重 INT8 保持 int8，scale 保持 float32，其余按上游最终状态转 BF16。只有分片赋值使用 strict=False，整个闭包仍严格。

toy PASS 是候选进入真实受控加载的必要条件，不是完整 Avatar 验收。真实模型还必须确认 indexed keys/shapes/dtypes 闭包、内存峰值、DMD forward 兼容和完整视频。Whisper 上游默认 FP32，本适配未擅自改变其 dtype；DMD 自身 FP32 构造峰值也仍存在。激活、VAE、连续 KV 不是这个加载补丁解决的范围。

## 最小依赖建议

先盘点 NGC 25.11 实际包和 `pip check`，优先复用原有 torch/torchvision/Triton/FlashAttention/CUDA/NCCL/NumPy/SymPy。原 NGC 包清单冻结为 constraints，pip dry-run/report 后检查不可变核心，再在派生容器中安装应用缺口。不要整份安装上游 requirements；不要把 requirements_avatar.txt 的 `libsndfile1==0.0.1` 当系统 libsndfile 包。

| 依赖组 | 上游源码需要 | 安装建议 |
|---|---|---|
| 文本/扩散 | transformers、diffusers、safetensors、tokenizers/HF hub、sentencepiece、ftfy、regex、einops、accelerate | 已有兼容版本先 import。若缺失，上游候选 transformers 4.41.0 / diffusers 0.35.1；实际 resolved 版本在镜像中验证后锁定，不宣称组合已验收 |
| 音频 | librosa、soundfile、soxr、scipy、pyloudnorm；librosa 的 numba/scikit-learn/audioread 传递闭包 | 缺包候选 librosa 0.11.0、soundfile 0.13.1、soxr 0.5.0.post1、pyloudnorm 0.1.1，服从核心约束和 ARM64 wheel 可用性 |
| 视频 | Pillow、imageio、imageio-ffmpeg、tqdm；系统 ffmpeg/ffprobe 能运行 | 缺包候选 imageio 2.37.0、imageio-ffmpeg 0.6.0；实际 ffmpeg 测试与路径留证 |
| 日志等 | loguru、psutil | 仅缺失再安装 |
| 暂不需要 | audio-separator、onnx/onnxruntime、Kim_Vocal ONNX、streamlit、pyarrow、OpenAI SDK、tritonserverclient、FlashAttention3、xformers | 本次单人物 clean-speech 路径不使用；不全量引入 |

NGC 已提供的 FlashAttention 应先运行本目录 probe，成功则无须编译另一版。FA probe 只是小张量 API/数值门，并不覆盖 BSA、完整 Avatar 注意力张量、双机通信或生成质量。

## 实际调用（由主代理取得 GPU 运行锁后执行）

```bash
python /runtime/prepare_runtime.py /workspace/LongCat-Video
python /runtime/meta_probe.py --source-dir /workspace/LongCat-Video
python /runtime/runtime_probe.py --source-dir /workspace/LongCat-Video --output /evidence/runtime-probe.json
```

`prepare_runtime` 重复执行要求生成内容完全一致；源码身份漂移或已有不同版本生成文件会拒绝覆盖。升级适配应由操作者审查并给新的干净源码目录，不自动删除用户已有文件。

## 自审结论

本地三项纯 Python 测试 PASS，涵盖多个错误分支；py_compile PASS。未运行任何远端命令、未安装软件、未使用 GPU。主要剩余风险是目标镜像 dependency import、GB10 FA 二进制、meta 候选完整模型状态与实际内存；这些不能用本地测试替代。独立审查由主代理安排。
