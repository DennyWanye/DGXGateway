# DGX 创作工具调用

> **2026-09-27 最新用户决定：生图和改图默认使用助手自带的内置 image_gen 模型，不再默认使用 DGX 四节点生图。** 此决定覆盖本文旧的“仅走 MCP”“默认 FLUX.2”等默认路由表述。下面的 DGX 生图合同仅在用户明确指定 DGX 生图时适用。内置生图不占用 DGX，可在 DGX 视频任务运行期间进行；需要入库时用 `drama_import_image`。锁定人物仍以已批准原图为参考，生成不代表批准或选用。

> **2026-09-10 图片数量新合同（Gateway已正常重启，在线schema已核验）：** 取消候选总数8、每组并行2的固定上限。`batch_count` / `parallelism` 接受正整数，`candidate_indices`接受原始非负编号列表；实际能力受内存与请求体/运行时资源约束。不得把“可输入”宣称“已验证任意数量并行”。MCP客户端可能缓存旧schema，服务更新后需重新读取tools/list或重连；历史下文的1–8/最多2说明以此修订为准。

## Agent 快速入口（2026-09-10 已实查）

### 2026-09-28：H3 受控性能诊断

`video_batch_submit` 可选 `diagnostic={trace_step,max_steps,max_seconds}`，仅单片 `h3-ring4/ref2va-official`，trace_step 为零基 -1（关闭）或预算内步骤，最多6步/3600秒。默认不启用。保持原输入和正式sigma轨迹，Gateway按四rank进度或持久化时间预算停止模型，终态CANCELED，不发布诊断半成片。停止时间另计；用于已授权性能实验，不是普通视频加速开关。原生连接器schema未刷新时可用现有MCP脚本传参数。 可选 `attention_probe=true` 只允许 trace=-1/max_steps=1，在前4个实际Ring KV chunk比较Attention分块；始终返回基线结果，探针步耗时不能当优化收益。


### 2026-09-27 MiniMax H3 提示词技能要求

用户明确指定：以后编写、改写或优化 MiniMax H3 提示词，必须先使用
[`h3-prompt-writing`](https://github.com/MiniMax-AI/MiniMax-H3/tree/main/skills/h3-prompt-writing)，
并读取其对应的 `references/base-en.txt` 或 `references/ref-en.txt`。
项目 Ring4 技能和经验摘要只能作为补充，不能代替该技能原文。
此要求不改变 Ref2VA 可选输入、四节点运行或业务 MCP 写入规则。

### 2026-09-26 Ref2VA 可选输入修复

用户明确新工作固定使用 Ref2VA。工作台 `drama_request_video_take` 和底层 `video_batch_submit` 的文字、图片、视频、音频可省略或传空；没有参考也不自动选择 T2VA。业务层将历史镜头 profile 按原时长归一到 Ref2VA，不改镜头内容修订，历史已提交任务保持原冻结路由。`generate_video` 的文字、图、音频也可省略（仍为4秒入口）。

全空输入不伪造素材或提示词，运行时用 tokenizer EOS 作为空上下文。Ref2VA 必须加载 `ref2va-optional-inputs-v1` 补丁；原始上游不接受空参考。纯文字提示词可保留三字段原文，不因执行 Ref2VA 强制改写或添加 `<Picture>`。旧 `10eros-fl2va-v1` 仍只供显式历史 T2VA 调用；与当前分区不兼容时提前拒绝，不提交到错误分区。功能、部署及真实模型证据见本轮检查点（历史内部记录，公开版不包含）。下文9月11日“无图必须T2VA”的说明是历史。

后续 agent 不必重新找调用方法。**日常 Gateway 操作优先用已连接的原生 MCP；命令行只作为缺少连接、大媒体上传或开发诊断的备用。**

### 缺工具时：扩展还是新建（2026-09-23）

2026-09-27 项目归并工具：`drama_merge_project` 将来源项目的全部分集、人物场景、素材绑定、候选及任务历史归到目标项目。外层 `projectId` 是目标，arguments 必填 `sourceProjectId`、`sourceExpectedRevision`、目标 `expectedRevision`、来源全部 `episodeIds` 和 `requestKey`。两项目类型/画幅/帧率/暂停状态必须一致，历史请求键冲突拒绝且事务回滚。保留对象与 Gateway 任务编号，使用生成/收片锁协调在途图片视频，来源空项目和原素材访问保留；不启动、取消、批准或重拍。文本/导出/转写忙时拒绝。用户只移动部分分集时不得以此工具擅自归并整个项目。

2026-09-26 光影资源编辑补齐：`drama_revise_shot.snapshot` 已公布 `referenceNodes`（按顺序的图片/视频/音频节点）与 `customReferenceRoles`，接受读回快照中的空素材槽 `null`，提示词上限与服务一致为16384字符。先 `drama_get_shot` 读完整快照，只修改所需字段再保存，避免丢失参考顺序和用途。`drama_propose_entity_revision` 支持 `references`、`referenceAssetId`、`referenceRole`；改人物/场景说明时须原样带回要保留的参考列表，省略会保存无参考草稿。这两项都复用既有工作台 service，不批准图片、不选择候选、不生成。

`drama_request_entity_image` 指定 `referenceRole` 编辑一个已批准视角时，输入必须包含**该槽位在已批准实体修订中的原图**，无需强塞正面主图，避免不同姿势互相干扰。该槽位还没有已批准原图时，继续要求已批准主图；未批准草稿图不能替代身份约束。提交和实际派发两次核对，排队期间批准参考变化会阻止派发。历史排队任务保留冻结的原校验规则。它仍只申请候选，不批准、不选用。

做镜头、实体、候选、时间线、生成和同类创作时，先 `python3 scripts/dgx-mcp-call.py --drama --list`，或读本文工具表。没有对应工具时，按下面补产品工具，不要写本地脚本直连数据库、ffmpeg 或模型。

| 缺的是什么 | 怎么做 |
|---|---|
| 同一对象、同一次用户决定里少了字段或读回 | 扩展现有工具。镜头草稿缺字段就改 `drama_revise_shot` 的 snapshot，不另开一个「更新提示词」工具。 |
| 另一次用户决定，或另一个对象 | 新建工具。批准、选用、申请生成、写入时间线不能并进 `drama_create_shot` / `drama_revise_shot`。 |
| 工作台 HTTP 已经有这个合同 | 在 MCP 里包现有 service 方法，不要第二套写入。 |

注册点是 [DGXGateway/dgx_gateway/drama/business_mcp.py](../DGXGateway/dgx_gateway/drama/business_mcp.py)：`DESCRIPTIONS`、`tools_list()` 的 schema，以及同文件的调用分发。输入保持 `{projectId, arguments}`，只有 `drama_list_projects` 和 `drama_create_project` 例外。写操作带幂等 `requestKey`。先读再写。`X-DGX-Drama-Action: explicit-user-action` 不是用户授权。

补完后做三件让下一个 Agent 不用再读实现：在 [tests/test_dgx_drama_business_mcp.py](../tests/test_dgx_drama_business_mcp.py) 加一条合同测试并跑过；运行 `python3 scripts/build-dgx-mcp-guide.py` 重新生成网页目录；在本节补一行工具名、用途和「不做什么」。模型启停仍只走 Gateway MCP，不走 SSH。审核、选用、批准和写入时间线仍要用户明确决定。

动作或提示词修改用 `drama_revise_shot` 改当前最完善的那一个镜头，不要为同一次修改再建一个镜头。删除、选用、写入时间线和批准仍是分开的决定。

- `drama_delete_shot`：删除指定镜头，并在项目审计里留下标题、提示词和生成次数。不批准、不选用、不写时间线、不停止模型。未结束或提交回执不明的生成会拒绝删除。
- `drama_list_shot_audit`：只读本项目已记录的生成提示词、参考资源和镜头删除记录。不改镜头、不生成。
- `drama_import_video`：把仓库、状态目录或项目素材目录里的 2–15 秒 MP4，或已有视频资产哈希，绑进当前项目作动作参考。调用 `drama_import_video`，arguments 里给绝对 `path` 或 64 位 `assetId`，再加 `requestKey`。不批准、不生成、不写时间线。`/tmp` 不在允许路径内。
- `drama_import_image`：把同一允许路径里的 PNG/JPEG/WebP，或已有图片资产哈希，收入当前项目。调用 `drama_import_image`，arguments 里给绝对 `path` 或 64 位 `assetId`，再加 `requestKey`。不批准、不生成、不写时间线。`/tmp` 不在允许路径内。
- `drama_create_entity`：在当前项目新建人物、场景、服装或道具草稿。arguments 给 `kind`、`name`、`requestKey`，参考图放 `references`（`assetId` + `role`）。不批准形象，也不能因此把未批准修订绑进镜头。
- `drama_select_shot_image`：已绑定的身份图可以不带 `candidateId`。这仍是批准画面，不是申请视频，也不是选用成片。
- `edit_image` / `generate_image`：2026-09-24 接上 FLUX.2 dev 的改图和多参考图生图。改图传 `model=flux2-dev` 和 1–8 个 `asset_ids`，不传宽高、编码器或非 20 的步数。多参考图生图在 `generate_image` 上加 `asset_ids`，画布仍用宽高。不批准、不选用。

### 2026-09-23：质量优先生图，且只能走 MCP

用户确认这是后续生图的用法。质量优先，不按“总是用最强的写实模型”一刀切。

| 要做什么 | 怎么调用 | 不要做什么 |
|---|---|---|
| 新图，画面里没有已锁定人物 | `generate_image`，省略 `model`。服务端保存为 `flux2-dev`。 | 不要先用快图打草稿，不要选 `flux2-dev-abliterated` 或 `llada-image-turbo`。这两个会被拒绝。 |
| 喵宝或任何已锁定身份要改姿势、服装、暴露 | 默认 `edit_image`，模型 `qwen-image-edit-2511`，参考图必须包含已批准原图。一次只改一件事，两张候选，等用户选。脸不像就丢掉，不送视频。用户明确要 FLUX.2 时，`edit_image` 的 `model=flux2-dev`，1–8 张参考，第 1 张决定输出尺寸。 | 不要在用户没点名 FLUX.2 时用 `generate_image` 重画锁定身份。 |
| 改一张已经有的图 | `edit_image`。默认 Qwen；用户要 FLUX.2 改图或多参考图生图时用 `flux2-dev`。多参考图生图是 `generate_image` 加 `asset_ids`，画布仍用请求的宽高。 | 不要重新生成一张新图来代替修改，除非用户要的就是多参考图生图。 |
| 用户明确说“先随便看看” | `generate_image`，`model=z-image-turbo` 且 `preview=true`。 | 没说这句话就不要用快图。预览图不保留、不送视频。 |

视频任务还在跑时不要生图。任务完成不是选用，也不是批准。后续 Agent 只能通过 Gateway MCP 的 `generate_image` / `edit_image`，或包住它们的 `drama_request_keyframe` / `drama_request_entity_image` 来生图。`comfy_submit` 遇到生图工作流会拒绝；不要用本地脚本、SSH 或手写 Comfy 图绕过。

### 当前会话有 `dgx_gateway` 工具时

工具名形如 `mcp__dgx_gateway__dgx_status`。若工具延迟提供，先在工具目录按 `dgx_gateway` 搜索，不要因此直接 SSH 启动模型。在支持 functions.exec 的会话可这样发现并调用：

```javascript
text(ALL_TOOLS.filter(t => t.name.startsWith('mcp__dgx_gateway__')).map(t => ({name: t.name, description: t.description})));
// 确认此工具存在后，只读查询：
const result = await tools.mcp__dgx_gateway__dgx_status({});
text(result);
```

当前原生连接已列出32个工具，`dgx_status` 与 `text_capabilities` 已实际成功调用。工具参数以当前schema为准，不能照旧示例假设支持新模型或时长。

| 目的 | 正常调用链 |
|---|---|
| 看当前模型、占用及切换结果 | `dgx_status`；有 operation 时用 `dgx_operation` |
| 已获授权的模型切换/停止 | 先 `dgx_status`，再 `dgx_switch_model` / `dgx_stop`，按原 operation ID 查询终态 |
| 图片制作 | 只走 `generate_image` / `edit_image` → `comfy_job`。新图省略 model 即 `flux2-dev`；改已有图或锁定身份只用 `edit_image`。不要用 `comfy_submit`、脚本或 SSH 生图 |
| 视频制作 | `video_batch_submit` / `generate_video` → `video_job` |
| 查原任务或取消 | `*_job_find` / `*_job`；按类型 `comfy_cancel` / `video_cancel` / `text_cancel` |
| Agent 自己写提示词 | Agent读取镜头资料、看参考图并填写工作台；不要调用 `text_submit` 代写，除非用户明确选DGX文本编写 |
| 音乐制作 | 当前没有独立 `generate_music` 工具；使用经验证的 Music 3 Comfy 工作流，经 `comfy_submit` 提交，再查 `comfy_job` |

先检查占用，不能为本任务擅自中断别的任务。重试沿用原任务/幂等键，结果不明先核查，不创建重复生成。请求受理不等于制作成功；按任务返回检查产物与资源释放/驻留状态。只读查询不需要先启动模型。

### 原生连接缺失时的备用

现有脚本仍然走 MCP `tools/call`，不是绕过 Gateway。工作目录为仓库根目录：

```bash
python3 scripts/dgx-mcp-call.py dgx_status
python3 scripts/dgx-mcp-call.py text_capabilities
# 对已授权的复杂请求，把JSON写入文件再传入，勿在shell拼接大段提示词/base64。
# python3 scripts/dgx-mcp-call.py TOOL --arguments-file /absolute/path/request.json
```

HTTP MCP 地址：`http://192.0.2.37:18082/mcp`。`initialize` / `tools/list` / `tools/call` 的原始JSON-RPC示例在本文下方。不要将DGX节点SSH地址当成Mac Gateway地址，也不要改变认证设置来迁就客户端。

### 2026-09-11：短剧审核 MCP 已补齐（FU11–14）

Gateway 首页新增 [MCP 工具文档](http://192.0.2.37:18082/mcp-guide.html)。
模型 Gateway 仍为32个工具；当前工作台业务服务为24个工具，新增14个：

- 读取：`drama_list_projects`（空参数）、`drama_get_project`、`drama_get_episode`、`drama_list_entities`、`drama_get_shot`、`drama_image_review_queue`。
- 审图与选用：`drama_review_images`、`drama_select_images`、`drama_select_entity_image`、`drama_select_shot_image`、`drama_approve_entity`。
- 审片与复核：`drama_review_take`、`drama_select_take`、`drama_review_shot`。

除项目列表外，工具输入均为 `{projectId, arguments}`。意见选填，省略即可。
`drama_select_images` 最多64项，各项独立requestKey与expectedRevision，同一来源槽位只选一张。
先检查整体 `isError`，再检查 `complete` 与每个 `results[].ok`；批量选用可能部分成功。
成功项重试必须保持相同参数和key；参数改变后用新key。工具不会自动确认旧版本或自动生成。
实体选用保存草稿，实体批准是单独动作；视频质量通过后才能采用生成视频，采用不改时间线。
按用户明确决定执行审核/采用，不能以程序拿到候选为由替用户决定。

```bash
python3 scripts/dgx-mcp-call.py --drama --list
python3 scripts/dgx-mcp-call.py --drama drama_list_projects
python3 scripts/dgx-mcp-call.py --drama drama_image_review_queue --arguments-file /absolute/path/queue.json
```

当前原生连接器只有模型工具；业务工具走独立工作台MCP，不能冒称已经原生挂载。
网页文档与 `DGXGateway/static/drama-mcp-tools.json` 由 `python3 scripts/build-dgx-mcp-guide.py` 生成；修改工具schema后重新生成并运行业务MCP测试。
执行记录：审核MCP补齐检查点（历史内部记录，公开版不包含）。

### 2026-09-21：镜头草稿 MCP（create / revise）

业务 MCP 补了两个与工作台 HTTP 同合同的草稿工具，方便 Agent 把分镜写进项目而不绕开产品路径：

- `drama_create_shot`：在指定分集新建空白镜头；幂等 `requestKey`；不填写内容、不批准、不生成。
- `drama_revise_shot`：按 `expectedRevision` + `baseRevisionId` 保存镜头内容草稿。`snapshot` 字段与工作台一致。绑定人物/场景必须是当前已批准实体修订，否则 `dependency_not_approved`。写入参考资产只是草稿指针，不批准画面，也不提交视频。

输入仍是 `{projectId, arguments}`。不要用这两步代替 `drama_select_shot_image` / `drama_request_video_take`。修改 schema 后重新生成 MCP 文档。

### 短剧业务 MCP 与底层 Gateway MCP 不同

2026-09-10实际检查：

- 上述 `/mcp` 返回32个底层Gateway工具，当前会话已原生接入。
- 当前本地工作台 `http://127.0.0.1:18189/dgx-drama/mcp` 的 `tools/list` 成功，业务工具接受 `{projectId, arguments}`，经工作台原有校验。HTTP POST使用JSON及 `X-DGX-Drama-Action: explicit-user-action`。其实际参数先读schema，不能将此头视为越过用户授权的凭据。
- LAN Gateway 的 `/api/v1/drama/mcp` 此次返回404；不要因为源码有路由就假定这个部署已开放。
- 当前会话未原生挂载 `drama_*` 工具。业务工具已包含上述读取、审核、采用操作，以及既有实体草稿、参考图/视频申请、审片建议、粗剪草稿；本次不扩展其他编辑器操作。不得将底层提交成功冒充短剧项目已归档或已采用。

### MiniMax Music 3 在哪里

ComfyUI 头节点进程的生命周期 ID 是 `comfyui`，不再把图片占用登记成 `minimax-music3`。`minimax-music3` 只作为历史别名：旧操作/驻留记录仍可读，`dgx_switch_model minimax-music3` 会规范成 `comfyui`。这个共享进程同时承载图片和音乐工作流；启动它并不等于 Music 3 权重已加载。音乐仍通过 `comfy_submit` 提交 Music 3 图，任务详情用加载器名称显示 MiniMax Music 3。

实际状态2026-09-10：`minimax-music3=STOPPED`；当前 `qwen38-flash-next=HEALTHY` / `256K`，不是音乐运行中。本次未启动或测试音乐，STOPPED不证明音乐生成已重新验收。

音乐工作流文件：`workflows/audio_minimax_music_3_bf16.json`、`workflows/MiniMax_Music_3_灯还亮着_V1.json`。这些是已有工作流资料，不应未经核对直接当成可提交API格式。适配器：`DGXGateway/dgx_gateway/adapters/music3.py`；实际加载文件以工作流和在线catalog为准，不以文件标题猜精度。


Gateway 页面：[DGX 控制台](http://192.0.2.37:18082/)。MCP 地址：`http://192.0.2.37:18082/mcp`，使用 HTTP POST JSON-RPC；没有 SSE 长连接。

当前配置 `gateway.lan_no_auth = true`：局域网 Agent 不需要管理令牌、Cookie 或登录。已有单一 authority、集群租约、互斥和恢复机制继续有效。启动、停止、切换都通过 Gateway，不通过 SSH 启动模型。

## 工具

| 工具 | 用途 |
|---|---|
| `dgx_status` | 查询模型和当前 operation，不启动模型 |
| `dgx_switch_model` | 异步启动或切换，传 model、可选 profile、idempotency_key |
| `dgx_stop` | 异步停止当前模型，传 idempotency_key |
| `dgx_operation` | 用 operation_id 轮询生命周期结果 |
| `dgx_cancel_operation` | 请求在安全边界取消 operation |
| `dgx_recover_stop` | 对 RECOVERY_REQUIRED operation 使用保留租约恢复到全部停止 |
| `comfy_register_image` | 上传PNG/JPEG/WebP（base64，上限8MiB），返回内容哈希asset_id，不启动模型 |
| `prepare_image_cutout` | 在Mac使用固定U2Net移除背景，异步返回透明PNG资产，不占DGX显存 |
| `compose_image_layers` | 用背景和1–16个透明资产按像素坐标确定性合成，不占DGX显存 |
| `media_job` | 查询本地背景移除/分层合成任务及输出asset_id |
| `edit_image` | 改已有图。默认 Qwen Image Edit 2511。`model=flux2-dev` 时用参考潜变量改图，1–8 张，第 1 张决定尺寸，固定 20 步。成功不是选用。 |
| `generate_image` | 质量优先文生图。省略 model 即 flux2-dev。`asset_ids` 只给 flux2-dev，最多 8 张参考，画布仍是请求的宽高。锁定身份不要用它重画，除非用户明确要 FLUX.2。z-image-turbo 仅在 preview=true 且用户明确说先看看时可用。 |
| `comfy_submit` | 提交已保存的非生图工作流（如 Music 3）。生图工作流会被拒绝，改走 generate_image / edit_image。 |
| `comfy_cancel` | 持久请求图片取消；定向中断原 prompt，安全收尾并释放，查询 comfy_job |
| `video_cancel` | 请求取消视频批次；Ring4 会对当前远端 job 发一次 DELETE，并由 Gateway 生命周期控制停止四节点运行时后释放资源；旧双机批次仍按安全镜头边界取消 |
| `comfy_job` | 用 job_id 查询任务、产物及 memory_released |
| `video_register_audio` | 注册严格4秒、24kHz、单声道PCM16 WAV参考音频，不启动模型 |
| `generate_video` | 提交单个4秒H3 Ref2VA镜头，自动加载、保存MP4并停模 |
| `video_batch_submit` | 提交1–32个有序镜头；一次加载H3、串行生成、批次末统一释放 |
| `video_job` | 查询视频批次及逐镜头状态、产物URL和释放状态 |
| `video_reconcile` | 核查中断批次；Ring4按原ID恢复持续跟踪并收集MP4，不重新提交推理 |
| `comfy_reconcile` | 恢复结果不明或重启中断的任务；查询原结果，不重发生成 |
| `comfy_catalog` | 读取在线 Comfy loader 和模型列表，不加载权重 |
| `personaplex_session` | 返回原生语音会话连接信息；不启动模型或代替音频流 |

以实际 `tools/list` 返回的输入 schema 为准。提交返回不等于生成或模型启动完成。

## 图片：调用完释放内存

Agent 正常顺序是 `dgx_status → generate_image → comfy_job`。高级工作流使用 `comfy_submit`，不必先手动加载模型。任务会通过 Gateway 启动 Comfy，运行工作流，将媒体复制到 Mac 持久目录，再停止 Comfy 进程并验证停止。只有 `state=SUCCEEDED` 且 `memory_released=true` 才表示完整完成。

产物地址在 job 的 `artifacts[].url`，Mac 文件在 `artifacts[].path`。共享 ComfyUI 运行环境的生命周期 ID 是 `comfyui`（端口 8188）；`minimax-music3` 只是历史别名。它不是只允许音乐模型，也不会因为切换到此 ID 就自动加载 Music3 权重。手动检查节点可切换至 `comfyui`，检查后执行 `dgx_stop`。

当前质量默认是 `generate_image` 省略 model，即 `flux2-dev`。`ComfyUI/workflows/dgx-z-image-turbo-api.json` 只保留给用户明确要求的快图预览，不是 Agent 的默认生图路径。模型文件就绪和实际图像验收以部署日志及真实 job 结果为准，catalog 在线只证明 Comfy 接口可达。

硬件执行槽位串行；同时提交多个任务可能返回 busy，不表示多个模型能同时推理。创作任务从加载到复制产物、停止期间占有 Gateway；其他切换不得打断它。发现已有外部 Comfy 工作时保留外部任务。

## 视频：单镜头与长篇批次

Agent 先注册参考图和四秒参考音频，再调用 `generate_video`。参考图沿用 `comfy_register_image` 返回的 `asset_id`；音频使用 `video_register_audio`。服务固定调用已资格验证的 `heretic-ref2va-v1`、1344×768、24fps、4秒契约，Agent 不需要拼 multipart、读取H3令牌或自行开关模型。

多个镜头使用 `video_batch_submit`。每个 shot 只包含 `name`、`prompt`、`image_asset_id`、`audio_asset_id` 和 `seed`。批次最多32条，一次冷加载H3，依次生成并持久化每条MP4，最后统一停模。用 `video_job` 轮询；只有批次 `state=SUCCEEDED` 且 `memory_released=true` 才完整结束。每条镜头也有独立 `state` 和 `artifact`，因此长片可按场或每32镜头切批次。

当前 `execution_mode=serial_single_engine`。一次提交多条可减少重复冷加载并支持无人值守队列，但不会让同一双节点H3引擎并行计算。未来只有新增且资格验证通过的独立执行器后，调度器才可安全并行；工具契约无需改变。

请求中断后先查原 `video_job`。不要更换 idempotency_key 重发。`video_reconcile` 保留已完成MP4；Ring4原任务仍在运行时恢复后台持续查询，沿用原 `ring_job_id`，不会重新提交或重启模型。查询临时失败只重试GET；连续5次失败保留原身份并回到 `SUBMISSION_UNKNOWN`。其他执行器沿用各自的安全回收规则，不猜测或重发结果不明的当前镜头。

本机音频文件可直接注册：

```bash
python3 scripts/dgx-mcp-call.py video_register_audio --audio-file /absolute/path/reference-4s.wav
```

无对白镜头可以复用已经注册的严格四秒静音资产；有对白或动作声的镜头应先制作各自四秒音轨，再注册其内容哈希。最终声音仍在剪辑阶段使用独立高质量音轨，不把模型参考音频当作完成混音。

## 多角色可控合成

当整图编辑无法同时保持多人身份时，先对每个已批准的单人图调用 `prepare_image_cutout`，用 `media_job` 取得透明PNG的 `output_asset.asset_id`。再调用 `compose_image_layers`，明确给出每层的唯一名称、asset_id、x/y、width/height及1–100的不透明度；可选的 `flip_x: true` 会水平翻转该层。数组顺序就是从后到前的叠放顺序，背景画布尺寸保持不变。

背景移除固定使用 `hyperframes@0.8.29` 的 `u2net_human_seg`，权重缓存于Mac；不会启动或占用DGX模型。合成是确定性像素操作，拒绝没有透明通道的前景，并把结果重新注册为可复用asset_id。五人镜头应先用这一工具锁定数量、站位和遮挡，再把合成图作为Qwen/H3参考，不让扩散模型重新决定角色数。

相同 idempotency_key 和相同 workflow 返回同一个 job；换内容必须使用新 key。POST 超时或连接中断后不要换 key 盲目重发；先查 `comfy_job`，需要时调用 `comfy_reconcile`。恢复会核对队列/history 并收集原结果，随后释放模型。仍有队列或生命周期操作时等待完成再恢复。

## 参考图编辑

先调用 `comfy_register_image(filename, image_base64)`，图片只保存到 Mac，返回内容哈希 `asset_id`。然后调用 `edit_image(prompt, asset_ids, idempotency_key)`；第一张图决定输出比例，最多三张，默认40步。Gateway 自动上传已校验输入到 Comfy，再运行 Qwen Image Edit2511，输出和停止证明仍通过 `comfy_job` 查询。注册只是文件校验，不代表图像质量或模型编辑成功。

高级 `comfy_submit` 可携带 `input_assets`；工作流中的 `LoadImage.image` 使用注册结果的 `comfy_filename`。同一键必须绑定同一工作流和同一输入列表。大体积base64请求用 `scripts/dgx-mcp-call.py TOOL --arguments-file request.json`，避免shell参数长度限制。MCP请求体上限16MiB。

本机文件也可直接注册，无需先写包含 base64 的临时 JSON：

```bash
python3 scripts/dgx-mcp-call.py comfy_register_image --image-file /absolute/path/reference.png
```

可复用 API 图保存在 `ComfyUI/workflows/dgx-qwen-image-edit-2511-api.json`（其中 LoadImage 文件名需要先通过注册与上传准备）。

默认编辑模型：`qwen_image_edit_2511_fp8mixed.safetensors`、`qwen_2.5_vl_7b_fp8_scaled.safetensors`、`qwen_image_vae.safetensors`。参考官方原生工作流（shift3.1、CFGNorm1、CFG4、Euler/simple）；实机验证结果见 journal，不将官方能力介绍当成本地质量验收。

## PersonaPlex：按需语音会话

1. `dgx_switch_model`：`model="personaplex"`，提供唯一 idempotency_key。
2. 轮询 `dgx_operation`，确认成功释放生命周期租约后查询 `personaplex_session`。
3. 使用 `http://192.0.2.30:8998` 上的 `/api/chat` 原生 Opus WebSocket。只允许一个语音会话；其他会话返回 409。
4. 结束会话后调用 `dgx_stop` 并轮询完成，才释放模型内存。仅关闭麦克风不会卸载权重。

浏览器麦克风需要安全上下文。通过已有 SSH LAN 连接转发，打开 localhost：

```bash
ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 8998:127.0.0.1:8998 operator@192.0.2.30
```

随后打开 [PersonaPlex 浏览器界面](http://localhost:8998)。隧道不会启动额外模型。协议和重复音频验收见 [PersonaPlex 操作说明](personaplex-gateway.md)。这是原生全双工语音接口，不宣称 OpenAI Realtime API 兼容或已经通过中文配音质量验收。

## 最小 MCP 调用示例

```bash
curl -sS http://192.0.2.37:18082/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"lan-agent","version":"1"}}}'
curl -sS http://192.0.2.37:18082/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
curl -sS http://192.0.2.37:18082/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"dgx_status","arguments":{}}}'
```

## 控制面维护

`manage.sh stop/start` 的维护 fence 同时检查生命周期 operation 和持久创作任务。仍在运行或需要 reconciliation 的创作任务会阻止维护；拒绝维护不会额外封锁该任务之后的正常停止。跨进程 admission lock 将新任务 QUEUED 落盘与维护检查串行化，防止检查为空后另一个任务突然被接收。

模型权重、生成媒体和 creative-jobs JSON 不包含在现有 SQLite/config export 包中。备份创作成果时另行保存 state_dir 的 `creative-jobs`、`creative-artifacts`、`creative-assets`。

首次升级的旧 Comfy 进程可能还没有 `/dgx/drain`，严格停止会拒绝 404，不能降级成单次空队列检查。确认旧实例无人提交、当前队列空闲后，由 Gateway 的显式 `dgx_recover_stop` 使用失败 operation 的保留租约完成一次停止，再通过 Gateway 启动加载新 custom node。此迁移使用已有恢复路径，不增加常态绕过规则。

补丁恢复时，如果旧 Gateway 尚在运行而创作任务已经 FAILED/INTERRUPTED 且 cleanup_required，可使用 `DGXGateway/manage.sh recovery-restart` 重载控制面。此命令拒绝任何仍在 LOADING/RUNNING/RELEASING/RECONCILING 等状态的任务或活跃生命周期操作，保留原 operation、租约及创作记录。重载后先 `dgx_recover_stop` 处理原生命周期恢复，再 `comfy_reconcile`。如果 Comfy 已停止，reconcile 通过 Gateway 获取新的全部停止证明，核验 Mac 已保存的成功产物，不请求已关闭的 Comfy HTTP，也不重复生成。

若 lifecycle 线程在申请租约前异常退出，记录停在 REQUESTED/PREFLIGHT，且从未创建该 operation 的 token，可运行 `DGXGateway/manage.sh recover-preflight-stop OPERATION_ID`。该命令要求记录至少 30 秒未变化，按原 operation ID 申请正常集群租约并采集全部停止证明，成功后冻结 FAILED_SAFE_STOPPED 并释放。它不调用模型 start/stop/retire，不修改运行中的模型；存在已有租约、记录仍在变化、模型未停止时拒绝。正常任务与持有租约的恢复继续使用已有 Gateway/MCP 路径。

## ComfyUI 共用入口与取消

当前24个工具也通过 Mac ComfyUI 的 DGX 创作面板及 DGXCreativeTool 节点提供。两端使用同一个 Gateway、素材ID、任务ID和幂等键；原生 Comfy 队列与 Gateway 持久任务不同。完整测试步骤和取消边界见 [ComfyUI 创作测试](comfyui-creative-test-guide.md)。取消意图独立保存为任务目录的 `<job_id>.cancel`，生成线程不能覆盖；备份应保留这些文件。


### 图片任务完成后的驻留释放（2026-09-10 实查）

当任务SUCCEEDED但模型由creative_jobs保留驻留时，dgx_stop可能提示先释放Gateway-owned retained model。这不是直接停进程的理由。先读取Gateway `GET /api/v1/snapshot`，确认residency为IDLE、can_release=true且无活跃生成；从响应取得准确owner_job_id。当前原生MCP没有驻留释放工具时，可使用同一Gateway正常HTTP接口 `POST /api/v1/residency/release`，JSON为 `{"owner_job_id":"刚查询到的ID"}`。不得硬编码旧owner，之后用dgx_status和comfy_job核验NONE/UNLOCKED及memory_released。2026-09-10本次MV角色图已实际验证此路径，无需重启Gateway或SSH停进程。


### 工作台无图文生视频（2026-09-11）

`video_batch_submit` 的 `profile="10eros-fl2va-v1"` 为T2VA；每条shot必须提供 `image_asset_id=""`、`audio_asset_id=""`，以及name/prompt/seed，可带request_duration_seconds=4和contract_version="h3-ref2va-v1"。实际请求t2va、8步、1344×768、24fps；不上传假图、不生成静音参考。批次仍串行，统一模型资源管理。不能把有图Ref2VA混进文字批次。旧generate_video工具仍为有图模式。当前已完成代码、自动测试、在线契约及页面验证，新工作台真实GPU成片尚未验收；不要把安装配置或模拟测试当作成片证据。
