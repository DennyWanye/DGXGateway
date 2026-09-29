#!/usr/bin/env python3
"""Regenerate the Gateway's MCP reference from actual tool contracts."""
import html
import json
from pathlib import Path
import sys
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from DGXGateway.dgx_gateway.drama.business_mcp import tools_list
from DGXGateway.dgx_gateway.mcp import TOOLS

DRAMA_TOOLS = tools_list()
GATEWAY_TOOLS = TOOLS
DRAMA_COUNT = len(DRAMA_TOOLS)
GATEWAY_COUNT = len(GATEWAY_TOOLS)

def code(value):
    return '<pre><code>' + html.escape(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)) + '</code></pre>'

def catalog(items):
    return ''.join('<details id="'+t['name']+'"><summary>'+t['name']+'</summary><p>'+html.escape(t['description'])+'</p>'+code(t['inputSchema'])+'</details>' for t in items)

page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>MCP 工具使用文档 · DGX Gateway</title><link rel="icon" href="/favicon.svg"><link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/admin-guide.css"></head><body>
<header class="topbar"><div><p class="eyebrow">DGX GATEWAY · MCP</p><h1>MCP 工具使用文档</h1></div><a class="guide-home" href="/">返回控制台</a></header><main class="guide">
<p class="guide-intro">模型任务与短剧审核的完整调用入口。更新于 2026-09-23；参数参考由实际工具代码生成，运行时以 tools/list 为准。质量优先生图只走 generate_image / edit_image：新图省略 model 即 flux2-dev；锁定身份只用 edit_image；z-image-turbo 必须 preview=true，且仅在用户明确说先看看时使用。不要用 comfy_submit、脚本或 SSH 生图。</p>
<nav aria-label="文档目录"><a href="#connect">连接服务</a><a href="#review">审核与选用</a><a href="#examples">调用示例</a><a href="#batch">批量与重试</a><a href="#drama">短剧工具（''' + str(DRAMA_COUNT) + '''）</a><a href="#gateway">模型工具（''' + str(GATEWAY_COUNT) + '''）</a></nav>
<section id="connect"><h2>连接到正确的项目数据</h2>
<table><tr><th>服务</th><th>MCP 地址</th><th>用途</th></tr><tr><td>模型 Gateway</td><td><code>http://192.0.2.37:18082/mcp</code></td><td>模型启停、图片/视频/音频任务，''' + str(GATEWAY_COUNT) + ''' 个工具。</td></tr><tr><td>当前短剧工作台</td><td><code>http://127.0.0.1:18189/dgx-drama/mcp</code></td><td>当前工作台项目与审核状态，''' + str(DRAMA_COUNT) + ''' 个工具。此 loopback 地址供运行在这台 Mac 的客户端使用。</td></tr></table>
<p>这是两个 MCP 服务。<strong>模型 Gateway 的 /mcp 不包含 drama_* 工具。</strong>请为 Agent 添加第二个业务 MCP 连接。不能用 18082 的 /api/v1/drama/mcp 代替当前工作台地址：本部署没有在那里启用当前项目数据库。</p>
<p>两者使用 HTTP JSON-RPC POST，先 initialize，再 tools/list，最后 tools/call；不是旧式 GET/SSE 服务。业务连接发送 <code>Content-Type: application/json</code>、<code>X-DGX-Drama-Action: explicit-user-action</code>，协议版本 <code>2024-11-05</code>。Gateway 连接的认证与维护详见<a href="/admin-guide.html#connect">管理员指南</a>。当前 LAN 配置免登录；业务服务限本机。</p>
<p>短剧工具通常接受 <code>{projectId, arguments}</code>，仅 <code>drama_list_projects</code> 接受空对象。projectId 从列出项目取得，episodeId 从读取项目取得；不能用 Gateway job_id 代替业务候选或资产 ID。</p></section>
<section id="review"><h2>审核与选用的对应关系</h2>
<table><tr><th>工作台操作</th><th>MCP 工具</th><th>结果</th></tr>
<tr><td>快速审图、查看对比候选</td><td>drama_image_review_queue</td><td>当前分集或整个项目候选、种子、审核状态、选用状态、来源版本。放大/对比是界面显示，不写状态。</td></tr>
<tr><td>批量通过 / 拒绝图片</td><td>drama_review_images</td><td>只标记质量；notes 可不填。</td></tr>
<tr><td>对比里直接选用 / 批量选用</td><td>drama_select_entity_image / drama_select_shot_image / drama_select_images</td><td>替换对应参考槽位。实体保存为草稿；镜头画面同时批准。不启动视频。</td></tr>
<tr><td>批准人物 / 场景 / 道具版本</td><td>drama_approve_entity</td><td>明确批准指定修订；关联镜头可能需要重新复核。</td></tr>
<tr><td>镜头复核通过</td><td>drama_review_shot</td><td>解决指定失效记录；reason 可不填。</td></tr>
<tr><td>通过 / 拒绝审片</td><td>drama_review_take</td><td>notes 可不填，不替换当前选片。</td></tr>
<tr><td>通过并设为本镜头选片</td><td>drama_review_take → drama_select_take</td><td>先通过，再采用。两步分别保存结果；第二步冲突时第一步仍已保存。不会修改时间线。</td></tr>
</table><p><strong>Agent 按用户已经作出的选择执行。</strong>不能把“生成成功”“质量通过”和“用户已选用”当成同一件事。需要用户选版本时，将候选交给用户审核，不自动替用户决定。Agent 生成提示词默认使用当前 Agent，无需调用 Gateway 文本模型。</p>
<p>图片信息由 <code>drama_get_project</code> 的素材清单与候选 assetId 对照取得。查看媒体使用 <code>http://127.0.0.1:18189/dgx-drama/projects/{projectId}/assets/{assetId}/media</code>，先确认资产属于当前项目。<code>drama_get_episode</code> 返回镜头、视频候选和已保存时间线；<code>drama_get_shot</code> 返回当前制作修订和失效记录。新建或保存镜头草稿用 <code>drama_create_shot</code> / <code>drama_revise_shot</code>，不批准、不生成。</p></section>
<section id="examples"><h2>可复制的调用方式</h2><p>项目内的同一个脚本支持两个端点；带 --drama 才会调用短剧业务 MCP。以下命令只读取：</p>'''
page += code('''python3 scripts/dgx-mcp-call.py dgx_status
python3 scripts/dgx-mcp-call.py --drama --list
python3 scripts/dgx-mcp-call.py --drama drama_list_projects
python3 scripts/dgx-mcp-call.py --drama drama_image_review_queue --arguments-file /absolute/path/review-queue.json''')
page += '<p>review-queue.json 示例，替换项目和分集 ID：</p>' + code({'projectId': '替换为项目ID', 'arguments': {'scope': 'episode', 'episodeId': '替换为分集ID'}})
page += '<p>批量通过图片的参数（会写入审核状态，仅在用户确认后调用 drama_review_images）：</p>' + code({'projectId':'替换为项目ID','arguments':{'candidateIds':['候选ID一','候选ID二'],'decision':'approved','requestKey':'image-review-user-choice-001'}})
page += '<p>选用镜头画面的参数（drama_select_shot_image）；expectedRevision 使用最新查询值，不是来源版本号：</p>' + code({'projectId':'替换为项目ID','arguments':{'shotId':'镜头ID','candidateId':'候选ID','assetId':'64位素材ID','expectedRevision':6,'requestKey':'shot-image-user-choice-001'}})
page += '<p>通用 JSON-RPC 外层；parameters 列表中的 arguments 是工具输入，业务输入内部还含一层 arguments：</p>' + code({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'drama_get_project','arguments':{'projectId':'替换为项目ID','arguments':{}}}})
page += '<p>给人物申请侧面、背面或全身参考图时使用 <code>drama_request_entity_image</code>，并填写 <code>referenceRole</code>（identity_front / identity_3q / identity_back / costume_fullbody / identity_sheet）。不填写则落到主参考槽。此工具只提交候选，不批准、不选用：</p>' + code({'projectId':'替换为项目ID','arguments':{'entityRevisionId':'实体修订ID','tool':'edit_image','referenceRole':'identity_3q','requestKey':'entity-3q-user-request-001','parameters':{'prompt':'侧面','asset_ids':['64位素材ID'],'steps':28,'batch_count':2,'parallelism':1}}})
page += '<p>新建空白镜头（<code>drama_create_shot</code>）再写入内容草稿（<code>drama_revise_shot</code>）。expectedRevision 与 baseRevisionId 必须来自刚读取的 <code>drama_get_shot</code>。不要绑定未批准实体，也不要随后提交生成：</p>' + code({'projectId':'替换为项目ID','arguments':{'shotId':'镜头ID','expectedRevision':0,'baseRevisionId':'镜头当前修订ID','requestKey':'shot-draft-001','snapshot':{'title':'S01 · 示例','prompt':'身份锁定与场景。','action':'0-15秒连续动作，不切镜。','camera':'缓慢前推，一条运镜。','framing':'vertical 9:16 medium','targetEditFrames':360,'generation':{'contractVersion':'h3-ref2va-v1','profile':'heretic-ref2va-hmnsfw-v1','requestDurationSeconds':15}}}})
page += '<p>2026-09-26：修改已有镜头时，先读回完整 snapshot，保留 referenceNodes 的顺序、媒体类型与用途，以及 customReferenceRoles；未用素材槽可传 null。drama_propose_entity_revision 支持完整 references、referenceAssetId、referenceRole，修改说明时须带回需保留的参考列表，省略会清空草稿参考。以上都是保存草稿，不批准图片、不生成。</p>'
page += '''</section><section id="batch"><h2>批量、冲突和恢复</h2><ol><li>先读取审核队列与当前版本，只对用户选中的候选操作。审核队列可能包含共享素材；检查 episodeIds、scopeKind 和 targetId。</li><li>质量批审支持 1–2000 个不重复 candidateIds，整批校验、一起保存。批量选用支持 1–64 项 selections，每项填写 targetType（entity/shot）、targetId、assetId、candidateId、expectedRevision、requestKey。一个参考槽位每批只选一张。</li><li>批量选用逐项保存，返回 complete 和 results。complete=false 表示部分失败，不能把 HTTP 200 或 isError=false 当作全部成功。成功项保留；按每项 error 定位失败。同一实体的不同槽位会逐次增加实体版本，后项须使用相应预期版本，或拆分调用并重新读取。</li><li>未知结果或网络中断：用完全相同的参数和 requestKey 重试。新一轮用户决定使用新 key。批量每项各有一个 key，不要只看顶层成功。修改过参数后须使用新 key。</li><li>revision_conflict 表示内容被更新，重新读取后让用户确认是否仍要覆盖。stale_candidate 表示来源较旧，只有明确确认才传 acceptStaleCandidate=true（图片）或 acceptStale=true（视频）。工具不会自动放宽旧版本限制。</li><li>生图/视频生成仍通过任务工具提交并轮询。request accepted 不等于完成。不要因为选择参考画面就隐式提交生成，也不要因为采用视频就隐式拼接或导出。</li></ol>
<p>模型层批量参数以当前 tools/list 为准，候选总数和并行数量已经允许正整数，不沿用旧指南中的 8/2 限制。实际并行受显存和运行时影响。模型切换前先 dgx_status，服务重启与模型切换是不同动作。</p></section>'''
page += '<section id="drama"><h2>短剧业务工具 · '+str(DRAMA_COUNT)+' 个</h2><p>展开工具查看真实 inputSchema。所有写操作沿用工作台校验，并以 business_mcp 记录事件来源。</p>' + catalog(DRAMA_TOOLS) + '</section>'
page += '<section id="gateway"><h2>模型 Gateway 工具 · '+str(GATEWAY_COUNT)+' 个</h2>' + catalog(GATEWAY_TOOLS) + '</section></main></body></html>'
(ROOT/'DGXGateway/static/mcp-guide.html').write_text(page)
(ROOT/'DGXGateway/static/drama-mcp-tools.json').write_text(json.dumps({'tools':DRAMA_TOOLS}, ensure_ascii=False, indent=2)+'\n')
print('Generated MCP guide and business catalog')
