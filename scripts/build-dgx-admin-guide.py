from pathlib import Path
import html,json
from DGXGateway.dgx_gateway.mcp import TOOLS
root=Path('DGXGateway/static')
parts=[]
def section(id,title,body):parts.append(f'<section id="{id}"><h2>{title}</h2>{body}</section>')
def code(text):return '<pre><code>'+html.escape(text)+'</code></pre>'
section('start','1. 先了解这三个入口','''<p>DGX Gateway 是两台 DGX 的统一管理入口：查看资源、切换模型、提交创作任务、取消和恢复任务、保存运行记录。你拥有管理员操作权限。两台机器组成一个计算集群，模型任务由 Gateway 统一协调。</p>
<table><tr><th>入口</th><th>当前地址</th><th>用途</th></tr>
<tr><td>Gateway 管理页面</td><td><a href="/">192.0.2.37:18082</a></td><td>看状态、模型切换、连接资料</td></tr>
<tr><td>MCP 工具服务</td><td><code>http://192.0.2.37:18082/mcp</code></td><td>供 AI 助手、MCP 客户端或脚本管理集群、生成媒体</td></tr>
<tr><td>ComfyUI</td><td><a href="http://192.0.2.37:8188/">192.0.2.37:8188</a></td><td>用节点工作流测试图片和视频</td></tr></table>
<p>语言模型的 OpenAI 兼容推理接口、H3 推理接口属于另一类地址。要调用正在运行的模型，请使用管理首页“当前本地模型”的连接信息。<strong>不要把 /mcp 填进 OpenAI base_url。</strong></p>
<p>Gateway 目前运行在 Mac，DGX 负责计算；AgentOS NAS 保存日志备份。Mac 关机、休眠或 Gateway 停止时，MCP 和后台记录/备份也会暂停。</p>''')
section('connect','2. 连接 MCP：从只读检查开始','''<p>在支持 HTTP MCP 的客户端中填写以下连接信息。各客户端的配置文件格式不同，按字段映射即可：</p>
<table><tr><th>字段</th><th>填写内容</th></tr><tr><td>名称</td><td>DGX Gateway</td></tr><tr><td>传输方式</td><td>HTTP / Streamable HTTP（支持 JSON 响应）</td></tr><tr><td>URL</td><td><code>http://192.0.2.37:18082/mcp</code></td></tr><tr><td>当前局域网认证</td><td>免登录，无需填写 API Key</td></tr></table>
<p>本服务使用 JSON-RPC POST，支持 initialize、tools/list、tools/call；通知返回 202。<strong>GET /mcp 返回 405 属于正常行为</strong>，它不是旧式 SSE 订阅地址。支持的协议版本：2024-11-05、2025-03-26、2025-06-18、2025-11-25。</p>
<p>当前配置为局域网免登录管理。能够访问服务的局域网客户端具有管理操作能力；请让自动化调用遵循下面的状态检查流程。浏览器 Origin 仍需匹配配置。将来关闭免登录后，应按管理会话认证规则接入，不能假设普通 Bearer token 已受支持。</p>
<h3>用终端验证连接</h3><p>以下三条只初始化、列出工具、查看状态，不启动模型。</p>'''+code('''curl -sS http://192.0.2.37:18082/mcp \\
  -H 'Content-Type: application/json' \\
  -H 'Accept: application/json, text/event-stream' \\
  -H 'MCP-Protocol-Version: 2025-11-25' \\
  --data '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25","capabilities":{},"clientInfo":{"name":"admin-check","version":"1.0"}}}'

curl -sS http://192.0.2.37:18082/mcp \\
  -H 'Content-Type: application/json' \\
  --data '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'

curl -sS http://192.0.2.37:18082/mcp \\
  -H 'Content-Type: application/json' \\
  --data '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"dgx_status","arguments":{}}}'
''')+'''<p>连接后请以 tools/list 返回的参数规则为准。下面的工具参考也来自当前服务源码，更新日期见页首。</p>''')
section('workflow','3. 调用顺序与结果判断','''<ol><li>调用 <code>dgx_status</code>，确认两台机器可达、当前模型、<code>current_operation</code> 和锁状态。</li><li>提交需要的操作，保存返回的 <code>operation_id</code> 或 <code>job_id</code>。同一个逻辑请求重试时复用原来的 <code>idempotency_key</code>；新任务使用新 key（8–128 字符）。不要因页面没反应就重复创建任务。</li><li>切换/停止模型用 <code>dgx_operation</code> 查询；图片用 <code>comfy_job</code>；视频用 <code>video_job</code>；抠图/合成用 <code>media_job</code>。建议每 5–10 秒查询一次，长期任务可放慢。</li><li>看到完成状态后再读取返回的作品地址。对于带资源释放的图片/视频任务，还要核对 <code>memory_released</code> 和 <code>cleanup_required</code>。</li></ol>
<p><strong>HTTP 200 不代表生成成功。</strong> JSON-RPC 可能包含 error，工具结果也可能有 <code>result.isError=true</code>。返回 job_id 只表示已接收任务；采样 100% 后还可能有解码、编码和释放资源步骤。</p>
<table><tr><th>常见状态</th><th>含义与操作</th></tr><tr><td>QUEUED / RUNNING</td><td>排队或处理中，继续查原任务</td></tr><tr><td>SUCCEEDED</td><td>任务成功；核对产物及资源释放字段</td></tr><tr><td>FAILED / INTERRUPTED</td><td>失败或被打断；先读错误和已有产物，再决定恢复</td></tr><tr><td>CANCELED</td><td>取消流程已结束；仍需确认资源释放</td></tr><tr><td>RECOVERY_REQUIRED</td><td>模型切换留下需要恢复的状态，不能继续随意切换</td></tr><tr><td>UNKNOWN / PARTIAL</td><td>观测不完整或两机不一致，先排查连接和服务</td></tr></table>
<p>管理员也应通过 Gateway 启停模型。直接 SSH 启动另一套模型可能绕开资源独占和任务账本。工具不保证强行打断忙碌中的生成。</p>''')
section('examples','4. 可复制的常用调用','''<p>图片生成和Qwen编辑支持候选总数1–8、每批并行上限1–2；4张按2+2处理。任务记录区分请求上限与实测有效并行，逐图保留原候选编号和种子。失败子集通过 retry_image_candidates 显式重拍；UNKNOWN只核查原任务，缺失下载用 comfy_reconcile 恢复，不重新生成。</p><p>以下示例使用 Mac 项目自带脚本。切换和生成示例会实际占用资源，先完成上节状态检查。将占位 ID 替换为上一次返回的值。</p>'''+code('''cd /Users/operator/projects/DGXSpark

# 只读：状态和图片模型目录
python3 scripts/dgx-mcp-call.py dgx_status
python3 scripts/dgx-mcp-call.py comfy_catalog

# 切换 H3 基础配置，保存返回的 operation_id
python3 scripts/dgx-mcp-call.py dgx_switch_model '{"model":"minimax-h3","profile":"heretic-ref2va-v1","idempotency_key":"admin-h3-start-001"}'
python3 scripts/dgx-mcp-call.py dgx_operation '{"operation_id":"替换为返回的操作ID"}'

# 停止当前模型并释放资源，继续查询返回的 operation_id
python3 scripts/dgx-mcp-call.py dgx_stop '{"idempotency_key":"admin-stop-001"}'
''')+'''<h3>参考图编辑与批量图片</h3><p>图片先注册一次，后续用 asset_id 重复引用。编辑支持 1–3 张参考图、FP8/BF16、两种编码器和每次 1–8 张候选图；batch_count 表示顺序生成候选，不是多份模型同时运行。图像步骤范围 20–40。FP8 较省内存，BF16 较占资源；不承诺每次 BF16 都有明显画质提升。</p>'''+code('''# 替换为自己的图片路径，输出中取 asset_id
python3 scripts/dgx-mcp-call.py comfy_register_image --image-file /absolute/path/reference.png

python3 scripts/dgx-mcp-call.py edit_image '{"prompt":"保留人物外貌，将背景改为雨后的街道","asset_ids":["替换为注册返回的asset_id"],"model_precision":"fp8","encoder":"standard","batch_count":2,"steps":20,"seed":42,"idempotency_key":"admin-image-edit-001"}'
python3 scripts/dgx-mcp-call.py comfy_job '{"job_id":"替换为返回的任务ID"}'
''')+'''<p>图片注册支持 PNG/JPEG/WebP，最大 8 MiB。<code>generate_image</code> 是另一条文生图工具，不是 Qwen 编辑工具。高级 <code>comfy_submit</code> 接收 API 格式 workflow 对象；不要直接提交 ComfyUI 画布导出的界面 JSON。</p>
<h3>H3 视频</h3><p><strong>当前 MCP generate_video / video_batch_submit 使用基础 heretic-ref2va-v1，单镜头固定 4 秒。</strong> 批次支持 1–32 个镜头，顺序执行。ComfyUI 第二套 4–15 秒控件属于另一条路径；15 秒此前发生超时和主节点 OOM，尚不能作为稳定可用能力。</p>'''+code('''# 音频要求：恰好4秒、单声道、PCM16、24kHz的WAV，最大256KiB
python3 scripts/dgx-mcp-call.py video_register_audio --audio-file /absolute/path/reference.wav

python3 scripts/dgx-mcp-call.py generate_video '{"name":"雨中街道","prompt":"人物穿雨衣走向街角咖啡馆，自然雨声","image_asset_id":"替换为图片asset_id","audio_asset_id":"替换为音频audio_asset_id","seed":42,"idempotency_key":"admin-video-001"}'
python3 scripts/dgx-mcp-call.py video_job '{"job_id":"替换为返回的任务ID"}'
''')+'''<p>高级工具的字段、必填项和范围请展开下面的工具参考。不要把 generate_video 的工具返回当成同步 MP4 文件。</p>''')
section('cancel','5. 取消与故障恢复','''<table><tr><th>要做什么</th><th>调用</th><th>注意</th></tr>
<tr><td>取消图片任务</td><td>comfy_cancel(job_id)</td><td>在可中断位置处理，再保存已有结果和释放；不是点下立即释放</td></tr>
<tr><td>取消视频批次</td><td>video_cancel(job_id)</td><td>当前镜头可能继续完成，后续镜头跳过，再释放双机</td></tr>
<tr><td>取消模型切换</td><td>dgx_cancel_operation(operation_id)</td><td>在安全边界处理，继续查操作结果</td></tr>
<tr><td>图片/视频中断恢复</td><td>comfy_reconcile / video_reconcile</td><td>按 job_id 恢复清理并保留已有产物，不是自动重跑按钮</td></tr>
<tr><td>切换失败后恢复停止</td><td>dgx_recover_stop(operation_id)</td><td>仅用于对应 RECOVERY_REQUIRED 操作及其有效恢复上下文</td></tr></table>
<p>ComfyUI 原生“取消”与这些持久任务工具不是同一套接口，不能互换 job_id / prompt_id / operation_id。</p>
<h3>管理员排障顺序</h3><ol><li>记录发生时间、模型/profile、任务 ID、参数和错误码。</li><li>查询原任务与 Gateway 状态，再看双机实际内存、容器状态和日志。</li><li>遇到 504，检查各层等待时限；不要假设远端计算已经停止。</li><li>遇到 OOM，查看主/工作节点内核故障；Docker 容器 OOMKilled=false 也不能排除内部子进程被系统杀掉。</li><li>确认原任务和资源状态后才恢复或重试。停止 Gateway 会中断其管理流程，并不能当作停止 DGX 计算的方法。</li></ol>
<p>已知限制：H3 曾出现首页 ACTIVE 但实际 API 已退出的情况；遇到无法调用时，需要结合实际容器状态和任务结果判断。不要仅凭 ACTIVE 或 GPU 利用率 0% 判定服务健康、内存已释放。</p>''')
section('records','6. 运行记录、NAS 与硬盘保护','''<p>记录独立于浏览器运行，趋势图显示最近一小时，并在重新进入页面时补载后台历史。横轴显示本地具体时间，缺失时段留空。历史约每15秒采集，页面实时约5秒更新；隐藏页面暂停轮询。</p>
<table><tr><th>频率</th><th>内容</th></tr><tr><td>15 秒</td><td>双机在线、统一内存、GPU 利用率/温度/功耗、CPU load、磁盘余量、网络链路、模型/任务阶段</td></tr><tr><td>60 秒</td><td>详细内存/CPU/IO计数、进程、容器状态、GPU进程、OOM和NVIDIA等内核故障</td></tr><tr><td>5 分钟或4 MiB</td><td>内存缓冲批量写入本地 SSD；新故障提前保存</td></tr><tr><td>每天08:00（北京时间）</td><td>压缩前一 UTC 日记录，备份 AgentOS NAS 并校验 SHA-256</td></tr><tr><td>每月初</td><td>清理上上个月及更早的 NAS 归档，保留本月与上个月两个自然月</td></tr></table>
<p>NAS 断连每小时重试，磁盘未挂载时拒绝回退写入系统盘。NAS 日志仅在每日备份/月度清理时集中读写；正常网页轮询不会逐条写访问日志。本地至少保留 7 天，只有取得校验回执的文件才会到期清理；小型清理审计记录保留。</p>
<p>目前空闲样本估算双机合计每天原始约 120 MB、压缩约 30 MB，忙时会增加。记录不含模型权重、生成媒体、提示词和密钥。突然断电或强杀进程最多可能丢失约 5 分钟未落盘缓存；采样无法保证捕获所有瞬时峰值，内核读取超限会标记截断。</p>
<ul><li><a href="/api/v1/recording/status">查看当前记录与备份状态（JSON）</a></li><li><a href="/api/v1/recording/recent">查看最近持久记录样本（JSON）</a></li><li>本地目录：<code>~/.local/state/dgx-cluster-gateway/recordings/</code></li><li>AgentOS NAS：<code>/srv/nas/@family/DGXGateway-Records/</code>，使用现有免密 SSH。</li></ul>
<p><code>WAITING_NEXT_DAY</code> 表示等待下一次日备份；不表示 NAS 故障。检查 last_sample_at、last_flush_at、备份回执和 nas_retention。机器休眠或 Gateway 停止后会在恢复运行时补传/补清理。</p>''')
section('admin','7. 管理员日常维护','''<p>日常只需检查双机在线、任务有无失败、日志是否持续更新、NAS 最近回执。生成任务由 Gateway 接管时，不需要手动提前加载模型。主动手动切换模型后，它可能保持驻留，闲置可通过 dgx_stop 释放。</p>'''+code('''# 在当前 Mac 项目目录操作
cd /Users/operator/projects/DGXSpark
DGXGateway/manage.sh status
DGXGateway/manage.sh logs 50

# 维护重启：先确认任务空闲。脚本会检查管理状态。
DGXGateway/manage.sh stop
DGXGateway/manage.sh start
''')+'''<p>配置文件：<code>~/.config/dgx-cluster-gateway/gateway.toml</code>。修改局域网地址后，客户端 URL、Origin 允许列表及依赖配置需同步调整。不要直接删除 Gateway 状态目录来清理报错，那里保存任务账本和恢复信息。</p>
<p>主 DGX：<code>dgx-head / 192.0.2.30</code>；工作 DGX：<code>dgx-worker / 192.0.2.31</code>；AgentOS NAS：<code>192.0.2.29</code>。这属于当前部署地址，不是可直接套用到其他环境的默认配置。</p>
<p>自动化管理员可遵循：先查状态 → 提交一次 → 保存 ID → 查询至结束 → 核对产物及释放 → 失败先恢复。日志归档不是模型、媒体或整机的全量备份。</p>''')
labels={'dgx_status':'查看双机和模型状态','dgx_switch_model':'启动或切换模型','dgx_stop':'停止模型并释放资源','dgx_operation':'查询切换/停止进度','dgx_cancel_operation':'取消切换操作','dgx_recover_stop':'恢复失败的模型切换','comfy_catalog':'查询图片运行目录','comfy_register_image':'注册参考图片','generate_image':'文字生成图片','edit_image':'Qwen参考图编辑/批量候选','comfy_submit':'提交高级API工作流','comfy_job':'查询图片任务','comfy_cancel':'取消图片任务','comfy_reconcile':'恢复图片归档与资源','retry_image_candidates':'仅重拍已失败图片候选','video_register_audio':'注册4秒参考音频','generate_video':'生成单个4秒镜头','video_batch_submit':'提交顺序镜头批次','video_job':'查询视频任务','video_cancel':'取消视频批次','video_reconcile':'恢复视频任务清理','prepare_image_cutout':'本地去背景','compose_image_layers':'本地图层合成','media_job':'查询去背景/合成任务','personaplex_session':'读取语音会话连接信息'}
reference='<p>以下 '+str(len(TOOLS))+' 个工具和 JSON Schema 按当前源码生成。带“只读”的工具不发起生成或切换；其余工具可能注册素材、创建任务或改变资源状态。</p>'
for t in TOOLS:
 reference+='<details><summary><code>'+t['name']+'</code> · '+labels.get(t['name'],t['name'])+(' · 只读' if t['annotations']['readOnlyHint'] else '')+'</summary>'+code(json.dumps(t['inputSchema'],ensure_ascii=False,indent=2))+'</details>'
section('tools','8. 全部 MCP 工具与参数',reference)
head='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>管理员使用指南 · DGX Gateway</title><link rel="icon" href="/favicon.svg"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/admin-guide.css"></head><body><header class="topbar"><div><p class="eyebrow">DGX GATEWAY · ADMIN GUIDE</p><h1>管理员使用指南</h1></div><a class="guide-home" href="/">返回控制台</a></header><main class="guide"><p class="guide-intro">连接 MCP、管理模型和任务、查看历史与备份。按当前部署整理，更新于 2026-09-08。</p><nav aria-label="文档目录">'''
for id,title in [('start','入口说明'),('connect','连接 MCP'),('workflow','调用流程'),('examples','常用示例'),('cancel','取消与恢复'),('records','日志和NAS'),('admin','管理维护'),('tools','工具参考')]:head+=f'<a href="#{id}">{title}</a>'
(root/'admin-guide.html').write_text(head+'</nav>'+''.join(parts)+'</main></body></html>')
(root/'mcp-tools.json').write_text(json.dumps({'updated_at':'2026-09-08','tools':TOOLS},ensure_ascii=False,indent=2)+'\n')
