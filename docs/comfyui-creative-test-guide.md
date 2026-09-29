# ComfyUI 创作与取消测试

入口：<http://192.0.2.37:8188>。Mac Gateway 必须在线；DGX 模型按需加载，完成或取消后释放。

## 推荐：直接使用节点工作流

左侧点击 **工作流**，双击 **DGX-Qwen-图片编辑**：

`加载图像 → Qwen 2511 图片编辑 · DGX → 保存图像`

在加载图像节点选择/上传自己的图片，在中间节点填写提示词，然后点击 ComfyUI 顶部 **运行**。结果会直接显示在保存图像节点，并保存到本地 `ComfyUI/data/output/DGX/`。已有参考图已放入 input，无需注册素材或填写 JSON。

视频工作流是 **DGX-H3-图生视频**：

`加载图像 + 加载音频 → H3 图生视频 · DGX 自动释放`

已有图片与四秒静音音频可直接试用。音频节点输入会在发送前转换成四秒24kHz单声道参考音轨。输出直接在视频节点预览，并保存到本地 output。H3 节点调用当前已验证的单镜头配置；本轮未重跑完整真实 H3 推理。

**这两条专用工作流支持 ComfyUI 顶部原生取消按钮**：节点在等待时把取消传到原 Gateway job。图片在检查点中断后释放；H3 仍等待当前镜头结束后释放。网络请求进行中取消可能需要等该请求返回。请保持批次数量1，待资源释放后再提交下一次。

节点请求回执在 `ComfyUI/data/output/dgx-jobs/`。查询瞬时超时只重试只读查询；相同参数再次运行时会接续尚未交付的原回执，不会重新生成。成功交付后的再次运行会创建新任务。

## Qwen 版本选择与批量

左侧工作流打开 **DGX-Qwen-版本选择与批量**，连接方式仍是加载图像→Qwen节点→保存图像。

- `model_precision`：`fp8` / `bf16`，只选择2511主模型的精度。
- `encoder`：`standard` / `huihui-abliterated-bf16`，独立选择编码器。后者只对语言部分去限制，不保证画质提升。
- `batch_count`：1–8，同一参考图和提示词生成多张候选；种子为seed、seed+1……。这不是多个参考图或多个提示词的批处理。
- 顶部 ComfyUI 自带“批次数量”保持1；节点内batch_count在同一个Gateway job里共用加载和释放。全部成功后SaveImage收到整批图片；中途取消不保证已生成部分返回本地节点。

MCP `edit_image` 支持相同的 `model_precision`、`encoder`、`batch_count` 参数；旧请求默认FP8/standard/1，保持兼容。节点回执包含完整版本和数量，接续任务不会串用其他版本。

2026-09-05 部署了节点及MCP接口，镜像为 `dgx-comfyui-h3:batch-20260905`。BF16主模型与Huihui编码器仍在专用UID996下载队列中，下载并验收前只使用fp8+standard；选项存在不代表权重已经安装。下载完成后必须记录真实运行与资源释放验收。

## 可选：面板测试图片

1. 打开页面，点击右下角 **DGX 创作 · 图片 / 视频**。
2. 默认选择 **Qwen 2511 参考图编辑**。选择 PNG/JPEG/WebP，点击 **注册素材**。注册成功后 `asset_ids` 自动填入；可依次注册最多三张参考图。
3. 填写 `prompt`。默认 seed=42、steps=20。点击 **提交到 Gateway**，下方出现任务卡及 job_id。
4. 不需要继续生成时，点击任务卡的 **取消此任务**。显示取消已收到后，等待 `CANCELED` 且内存释放已确认。
5. 再次制作新图时点击 **新建请求编号**。相同编号、相同参数查询/提交仍返回原任务，不会重新生成。

可以不上传而直接复用已注册参考图，在 `asset_ids` 填入：

```json
["9e8b171820963bff7b562023ffb35b11a6d6fe5edc85ae31a65ed9cd08d97065"]
```

自动化 Chrome 文件上传需要浏览器扩展允许访问文件 URL；这与用户手动文件选择不同。

## H3 视频

选择 **H3 单镜头**，注册参考图片及严格 4 秒 / 24kHz / 单声道 PCM16 WAV 后填写提示词和镜头名。上传后对应 asset_id 自动填入。

测试音频：`creative-projects/wissenland-hunt/audio/reference-silence-4s.wav`。
也可复用参考音频 ID：`c6c498ab5ae712929a7fcca892582c45d2530c0c68efc94ff39deeadf50fbb92`。

**H3 多镜头批次**的 shots 使用 MCP 相同数组；可参考 `creative-projects/wissenland-hunt/prompts/h3-process-two-shot-v1.json` 内的 shots。

取消 H3 会持久记录意图，等待当前加载或当前镜头结束，跳过剩余镜头，保留已完成 MP4，再释放双机。当前没有已验证的双机推理中途硬中断；一个镜头可能需要十多分钟，不能当作秒停。

产物可在任务卡播放或打开保存。需要查看之前 MCP 创建的任务，在下方填写 job_id、选择 comfy/video/media，点击 **查询并加入列表**。

已完成的视频验证批次：`3f8e958b79e84520aba040f9f9b8616d`（video）。

## 节点图入口

左侧 **工作流** 打开 `dgx-creative-tools`。该图默认调用只读 `dgx_status`，可以安全点击 ComfyUI 的运行验证连接。

`DGXCreativeTool` 节点的 `tool` 和 `arguments_json` 接受与 MCP 完全相同的名字与参数。生成工具返回 job_id，不阻塞本地节点队列等待 GPU；生成状态及取消使用创作面板的任务卡。节点提交的任务会自动加入同浏览器的任务列表。

**通用 DGXCreativeTool 节点已经返回、或者从面板提交的任务，不能用原生队列取消代替任务卡取消。新专用 Qwen/H3 节点在等待期间支持原生取消。** 旧的直接 H3 节点保持原行为；需要持久任务、自动释放和取消时使用新的创作入口。

全部 24 个 Gateway 工具都能通过面板和通用节点调用。高级工作流、图层和批次仍使用 JSON 参数；不是每个工具都有单独的专用可视化节点。

## 状态与恢复

- 取消接口立即返回 `cancel_requested=true`，不代表已经停止。
- 图片：只对匹配 prompt_id + client_id 的队列任务中断；不会使用不区分任务的全局中断。
- 加载、上传、保存文件期间等待当前操作安全结束；采样在 Comfy 检查点中断。取消与生成完成同时发生时，可能保留已完成产物。
- `CANCELED + memory_released=true` 才表示取消收尾完成。若任务尚未加载任何模型，release_reason 会说明未分配模型资源。
- `INTERRUPTED + cleanup_required=true` 表示仍需恢复，不能当作已释放。使用原 job_id 调用 comfy_reconcile 或 video_reconcile；若生命周期需要恢复先处理原 operation。
- 关闭面板、刷新浏览器、停止聊天不取消服务端工作。浏览器保存请求编号与任务列表，Gateway 保存权威任务记录及独立 `.cancel` 文件。
- 取消是幂等的；已完成任务的取消请求不会删除其产物，也不会重启模型。

## 本次验收

Qwen 从真实 ComfyUI 面板提交并取消：`ac4cc087fe114a678dfb89313d6ad307`。
采样节点 KSampler 记录 execution_interrupted，取消意图至中断约 0.94 秒；约18秒后 Gateway 确认停止模型并释放。节点图 dgx_status 运行成功。

H3 取消的“保留当前完成镜头、取消后续镜头、最后释放”经过自动化测试；本次未新增耗时的真实 H3 取消推理验收。此前两镜头正常生成与释放已有实机证据。

## 部署与回滚

Gateway 服务从当前仓库运行，空闲时使用 `DGXGateway/manage.sh stop` / `start` 更新；活动任务或待恢复任务会阻止停服。

ComfyUI 使用 `Dockerfile.creative` 基于既有合格镜像增加创作扩展，不改 ComfyUI 或依赖版本。当前 image 为 `dgx-comfyui-h3:native-20260905`，由 `ComfyUI/.env` 的 DGX_COMFYUI_IMAGE 选择；旧 `dgx-comfyui-h3:0.1.0` 保留。重建扩展后应通过同一 compose 项目更新，先确认本地队列及 Gateway 空闲。

DGX head 的取消路由位于原 dgx_gateway_admission 扩展；原文件保留为 `__init__.py.pre-cancel-20260905`。不需要改变 H3 runtime 或双机配置。


## 工作流新名称（2026-09-05）

工作流目录按模型配置编号整理，旧文件备份在ComfyUI/data/workflow-backups中。浏览器已打开的旧标签不会随文件改名；保留其中未保存的编辑，后续从左侧打开新名称。

| 工作流 | 实际配置与操作 |
|---|---|
| H3-0-三套配置切换与状态 | 管理工具，不生成视频 |
| H3-1-heretic-ref2va-v1-参考图视频（自动释放） | 固定heretic-ref2va-v1，自动加载、生成、释放 |
| H3-2-heretic-ref2va-hmnsfw-v1-参考图视频 | 已预选hmnsfw配置；先用H3-0切换，再运行；仍为旧直接节点的生命周期行为 |
| H3-3-10eros-fl2va-v1-首帧图视频 | 先用H3-0切换10eros，再运行 |
| H3-3-10eros-fl2va-v1-文字生成视频 | 同上，输入文字 |
| Qwen-2511-版本选择与批量（推荐） | 节点内选FP8/BF16及编码器，batch_count选择数量 |
| Qwen-2511-FP8-标准编码器-单图 | 固定FP8、standard |
| Qwen-2511-FP8-Huihui编码器-单图 | 固定FP8、Huihui BF16编码器 |
| 工具-MCP调用与状态查询 | 通用MCP入口 |

此次只整理工作流及预选配置，没有切换模型或提交新生成任务。第二、第三套直接节点不具备第一套持久Gateway任务的全部自动释放与取消语义，不应混淆。


### H3-2 参数扩展（2026-09-05）

仅第二套工作流改用DGXRemoteMiniMaxH3HMNSFWControls：时长4–15整数秒，FPS固定24，并在Seed同区显示说明。模型卡官方4–15秒/24FPS；已安装pipeline_minimax_h3.py拒绝非24fps，time_request按17n+5向上对齐，因此实际成片可能略长于目标（<1秒）。分辨率1344×768、采样20步、参考音频4秒不变。未改变H3-1/H3-3原参数范围。

ComfyUI已部署image dgx-comfyui-h3:duration-20260905，新节点超时10900秒；远端/etc/minimax-h3/request-policy-v2.json仅hmnsfw配置允许最大15秒，上游等待10800秒。对安装版gateway作精确转发超时补丁，持有control.lock非阻塞排他锁更新，仅重启minimax-h3-gateway.service，未重启模型rank。首次策略文件权限错误导致健康失败，事务已自动回滚；二次保留原权限/属主后部署和鉴权health通过。旧网关和策略备份/var/lib/minimax-h3/controls-backup-20260905。源gateway代码相同补丁已回写，Gateway测试20通过，Comfy64项62通过2跳过。

证据evidence/h3-duration-controls/acceptance.json。4秒已有旧节点真实生成证据；本次只做参数/HTTP策略/界面验证，未新跑15秒，不得宣称双机15秒性能和内存已实测。输入长度可选不代表所有长度/内容已保证。用户当前旧浏览器缓存缺少新节点，另打开?h3-duration-ready=20260905重新加载节点。旧视图先保存再备份至ComfyUI/data/workflow-backups/before-duration-controls，迁移时保留提示词、seed及参考图音频。
