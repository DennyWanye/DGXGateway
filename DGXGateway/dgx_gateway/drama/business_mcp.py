"""Project-scoped business tools. Explicit review/adoption reuse UI contracts; no raw model or publication."""
from __future__ import annotations
import json
import math
import re
from contextvars import ContextVar
from .contracts import DramaError, fields, identifier, integer, text
from .store import encode, new_id

CALL_SOURCE = ContextVar('drama_call_source', default='ui_api')
TEXT_TOOLS = {f'drama_{kind}': kind for kind in ('extract_entities', 'draft_beats', 'draft_script', 'draft_storyboard')}
DESCRIPTIONS = {
    **{name: '申请结构化文本草稿；冻结本集输入，复用工作台校验，不批准、不拍摄。' for name in TEXT_TOOLS},
    'drama_propose_entity_revision': '保存实体新草稿，必须提供当前版本；可带完整 references 保留各视角，省略会清空草稿参考。复用工作台校验，不批准形象。',
    'drama_request_keyframe': '按固定镜头修订申请参考图；须通过工作台的引用与批准校验。新图省略 model，即 flux2-dev；改已有图或锁定身份只用 edit_image。不接受原始 workflow、脚本或 SSH。成功不是选用。',
    'drama_request_entity_image': '按固定实体修订申请参考图；可指定正面、侧面或全身槽位。编辑已批准视角时必须包含该槽位原图；该视角未批准则仍须包含已批准主图。新图省略 model，即 flux2-dev；改已有图或锁定身份只用 edit_image。不批准、不选用。',
    'drama_request_video_take': '按已批准参考和固定镜头修订申请视频；复用项目暂停、预算和版本校验。',
    'drama_get_job': '只读本项目已有文本、图片、视频或导出任务；不重试。',
    'drama_record_review_suggestion': '记录审片建议，不改变人工批准、拒绝或采用状态。',
    'drama_build_rough_timeline': '从本集已采用且审片批准的候选形成粗剪草稿；不保存正式时间线或导出。',
    'drama_save_timeline': '保存本集时间线修订；不导出、不改选片。',
    'drama_create_export': '按已保存时间线创建导出任务；不自动批准。',
    'drama_import_audio': '将本机已校验音频路径或已有音频资产哈希收入当前项目；不调用音乐模型。',
    'drama_import_video': '将本机 2–15 秒 MP4 路径或已有视频资产哈希收入当前项目，供动作参考；不批准、不生成。',
    'drama_import_image': '将仓库、状态目录或项目素材目录里的 PNG/JPEG/WebP，或已有图片资产哈希，收入当前项目。不批准、不生成。/tmp 不在允许路径内。',
    'drama_create_entity': '在当前项目新建人物、场景、服装或道具草稿，可附带已绑定参考图。不批准形象。',
    'drama_bind_song': '将已绑定音频设为本集歌曲；不保存时间线。',
    'drama_create_shot': '在指定分集新建空白镜头；不填写内容、不批准、不生成。',
    'drama_revise_shot': '保存镜头内容草稿修订；不批准参考、不提交生成。动作或提示词修改改当前最完善的镜头，不为同一次修改再建镜头。',
    'drama_delete_shot': '删除指定镜头，并在项目审计里留下标题、提示词和生成次数。不批准、不选用、不写时间线、不停止模型。未结束或提交回执不明的生成会拒绝删除。',
    'drama_list_shot_audit': '只读本项目已记录的生成提示词、参考资源和镜头删除记录。不改镜头、不生成。',
    'drama_merge_project': '将来源项目全部分集、资料及历史归并到当前项目；须显式列出全部分集并核对两项目版本、画幅和暂停状态。保留原对象与生成任务编号，来源空项目保留。不生成、不取消、不批准。',
}


REVIEW_DESCRIPTIONS = {
    'drama_list_projects': '列出项目以取得 projectId；只读。',
    'drama_get_project': '读取项目、分集和素材清单；只读。',
    'drama_get_episode': '读取分集镜头、候选和已保存时间线；只读。',
    'drama_list_entities': '读取项目人物、场景、服装和道具及各版本；只读。',
    'drama_get_shot': '读取镜头当前修订、参考画面和失效项；只读。',
    'drama_image_review_queue': '读取项目或分集的抽卡候选、审核和选用状态；不代替用户选择。',
    'drama_select_images': '批量选用用户指定的图片，每项独立版本和请求键；返回逐项成功/失败，不隐式批准实体或生成视频。',
    'drama_review_images': '按用户决定批量通过或拒绝图片质量；意见选填，不改变选用图片。',
    'drama_select_entity_image': '按用户选择替换实体候选来源槽位，保留其他参考；保存草稿，不批准实体。',
    'drama_approve_entity': '按用户确认批准指定实体修订；可能使关联镜头需要重新复核。',
    'drama_select_shot_image': '按用户选择批准本镜头画面；已绑定图片可省略 candidateId。不提交视频、不改时间线。',
    'drama_review_take': '按用户决定通过或拒绝视频质量，意见选填；不改选片或时间线。',
    'drama_select_take': '按用户选择采用已通过的视频候选；不保存时间线或导出。',
    'drama_review_shot': '按用户决定复核当前镜头的指定失效记录，意见选填；不生成。',
}
DESCRIPTIONS.update(REVIEW_DESCRIPTIONS)

# Project lifecycle tools. These are the only business tools without a
# projectId envelope: they create the project/episode that later calls scope to.
PROJECT_DESCRIPTIONS = {
    'drama_create_project': '新建项目并返回 projectId；只建立空白项目，不生成内容。',
    'drama_create_episode': '在指定项目下新建分集并返回 episodeId；不生成内容。',
}
DESCRIPTIONS.update(PROJECT_DESCRIPTIONS)



def review_contracts(ident, key, revision):
    asset = {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}
    note = {'type': 'string', 'maxLength': 2000}
    decision = {'type': 'string', 'enum': ['approved', 'rejected']}
    ids = {'type': 'array', 'items': ident, 'minItems': 1, 'maxItems': 2000, 'uniqueItems': True}
    version = {'expectedRevision': revision, 'requestKey': key}
    select = {**version, 'candidateId': ident, 'acceptStaleCandidate': {'type': 'boolean'}}
    return {
        'drama_select_images': ({'selections': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': {
            'type': 'object', 'properties': {**select, 'targetType': {'type': 'string', 'enum': ['entity', 'shot']}, 'targetId': ident, 'assetId': asset},
            'required': ['targetType', 'targetId', 'assetId', 'candidateId', 'expectedRevision', 'requestKey'], 'additionalProperties': False}}}, ['selections']),
        'drama_list_projects': ({}, []),
        'drama_get_project': ({}, []),
        'drama_get_episode': ({'episodeId': ident}, ['episodeId']),
        'drama_list_entities': ({}, []),
        'drama_get_shot': ({'shotId': ident}, ['shotId']),
        'drama_image_review_queue': ({'episodeId': ident, 'scope': {'type': 'string', 'enum': ['episode', 'project']}}, ['scope']),
        'drama_review_images': ({'candidateIds': ids, 'decision': decision, 'notes': note, 'requestKey': key}, ['candidateIds', 'decision', 'requestKey']),
        'drama_select_entity_image': ({**select, 'entityId': ident, 'referenceAssetId': asset}, ['entityId', 'candidateId', 'referenceAssetId', 'expectedRevision', 'requestKey']),
        'drama_approve_entity': ({**version, 'entityId': ident, 'revisionId': ident}, ['entityId', 'revisionId', 'expectedRevision', 'requestKey']),
        'drama_select_shot_image': ({**select, 'shotId': ident, 'assetId': asset}, ['shotId', 'assetId', 'expectedRevision', 'requestKey']),
        'drama_review_take': ({'takeId': ident, 'decision': decision, 'notes': note, 'requestKey': key}, ['takeId', 'decision', 'requestKey']),
        'drama_select_take': ({**version, 'shotId': ident, 'takeId': ident, 'acceptStale': {'type': 'boolean'}}, ['shotId', 'takeId', 'expectedRevision', 'requestKey']),
        'drama_review_shot': ({**version, 'shotId': ident, 'invalidationIds': {**ids, 'maxItems': 64}, 'reason': note}, ['shotId', 'invalidationIds', 'expectedRevision', 'requestKey']),
    }


def tools_list():
    ident = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
    key = {'type': 'string', 'minLength': 1, 'maxLength': 128}
    revision = {'type': 'integer', 'minimum': 0}
    text_fields = {'episodeId': ident, 'sourceRevisionIds': {'type': 'array', 'items': ident, 'minItems': 1, 'maxItems': 16, 'uniqueItems': True},
                   'entityRevisionIds': {'type': 'array', 'items': ident, 'uniqueItems': True}, 'requestKey': key, 'repairOf': ident}
    contracts = {}
    for name, kind in TEXT_TOOLS.items():
        props = dict(text_fields)
        required = ['episodeId', 'sourceRevisionIds', 'requestKey']
        upstream = {'draft_script': 'beatRevisionId', 'draft_storyboard': 'scriptRevisionId'}.get(kind)
        if upstream: props[upstream] = ident; required.append(upstream)
        contracts[name] = (props, required)
    contracts.update({
        'drama_propose_entity_revision': ({'entityId': ident, 'expectedRevision': revision, 'name': {'type': 'string', 'minLength': 1}, 'description': {'type': 'string'},
            'referenceAssetId': {'type': ['string', 'null'], 'pattern': '^[a-f0-9]{64}$'},
            'referenceRole': {'type': 'string', 'minLength': 1, 'maxLength': 64},
            'references': {'type': 'array', 'maxItems': 5, 'items': {'type': 'object', 'properties': {
                'assetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}, 'role': {'type': 'string', 'minLength': 1, 'maxLength': 64}},
                'required': ['assetId', 'role'], 'additionalProperties': False}},
            'requestKey': key}, ['entityId', 'expectedRevision', 'name', 'requestKey']),
        'drama_request_keyframe': ({'shotRevisionId': ident, 'tool': {'type': 'string'}, 'parameters': {'type': 'object', 'description': '固定图片工具的参数；由服务端工具合同验证，不接受原始workflow或URL'}, 'requestKey': key}, ['shotRevisionId', 'tool', 'parameters', 'requestKey']),
        'drama_request_entity_image': ({'entityRevisionId': ident, 'tool': {'type': 'string'}, 'parameters': {'type': 'object', 'description': '固定图片工具的参数；由服务端工具合同验证，不接受原始workflow或URL'}, 'requestKey': key, 'referenceRole': {'type': 'string', 'minLength': 1, 'maxLength': 64}}, ['entityRevisionId', 'tool', 'parameters', 'requestKey']),
        'drama_request_video_take': ({'episodeId': ident, 'shots': {'type': 'array', 'minItems': 1, 'maxItems': 32,
            'items': {'type': 'object', 'properties': {'candidateCount': {'type':'integer','minimum':1,'maximum':4}, 'promptJobId': ident, 'shotId': ident, 'shotRevisionId': ident, 'audioAssetId': {'type': 'string', 'pattern': '^([a-f0-9]{64})?$', 'description': '可选的4秒H3参考WAV'}, 'actionImageAssetId': {'type': 'string', 'pattern': '^([a-f0-9]{64})?$'}, 'videoAssetId': {'type': 'string', 'pattern': '^([a-f0-9]{64})?$'}, 'audioDescription': {'type': 'string', 'maxLength': 2000}, 'seed': {'type': 'integer', 'minimum': 0, 'maximum': 2147483647}, 'prompt': {'type': 'string', 'maxLength': 16384}, 'generation': {'type': 'object', 'description': 'H3 Ring4内部路由与4–15秒时长合同'}},
                      'required': ['shotId', 'shotRevisionId', 'seed'], 'additionalProperties': False}}, 'requestKey': key}, ['episodeId', 'shots', 'requestKey']),
        'drama_get_job': ({'jobId': ident, 'kind': {'enum': ['text', 'image', 'video', 'export']}}, ['jobId', 'kind']),
        'drama_record_review_suggestion': ({'takeId': ident, 'suggestion': {'type': 'string', 'minLength': 1, 'maxLength': 4000}, 'requestKey': key}, ['takeId', 'suggestion', 'requestKey']),
        'drama_build_rough_timeline': ({'episodeId': ident}, ['episodeId']),
        'drama_save_timeline': ({'episodeId': ident, 'expectedRevision': revision, 'requestKey': key,
            'clips': {'type': 'array', 'minItems': 1, 'maxItems': 64, 'items': {'type': 'object'}},
            'audioClips': {'type': 'array', 'maxItems': 32, 'items': {'type': 'object'}}},
            ['episodeId', 'clips', 'expectedRevision', 'requestKey']),
        'drama_create_export': ({'timelineId': ident, 'requestKey': key,
            'outputs': {'type': 'array', 'items': {'type': 'string'}},
            'subtitleRevisionId': ident, 'presetVersion': {'type': 'string'}}, ['timelineId', 'requestKey']),
        'drama_import_audio': ({'path': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
            'assetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'filename': {'type': 'string', 'minLength': 1, 'maxLength': 255}, 'requestKey': key}, ['requestKey']),
        'drama_import_video': ({'path': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
            'assetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'filename': {'type': 'string', 'minLength': 1, 'maxLength': 255}, 'requestKey': key}, ['requestKey']),
        'drama_import_image': ({'path': {'type': 'string', 'minLength': 1, 'maxLength': 4096},
            'assetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'filename': {'type': 'string', 'minLength': 1, 'maxLength': 255}, 'requestKey': key}, ['requestKey']),
        'drama_create_entity': ({'kind': {'type': 'string', 'enum': ['character', 'location', 'costume', 'prop']},
            'name': {'type': 'string', 'minLength': 1, 'maxLength': 160},
            'description': {'type': 'string', 'maxLength': 8000},
            'referenceAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'referenceRole': {'type': 'string', 'minLength': 1, 'maxLength': 64},
            'references': {'type': 'array', 'maxItems': 5, 'items': {'type': 'object', 'properties': {
                'assetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'role': {'type': 'string', 'minLength': 1, 'maxLength': 64}},
                'required': ['assetId', 'role'], 'additionalProperties': False}},
            'requestKey': key}, ['kind', 'name', 'requestKey']),
        'drama_bind_song': ({'episodeId': ident, 'songAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
            'expectedRevision': revision, 'requestKey': key}, ['episodeId', 'songAssetId', 'expectedRevision', 'requestKey']),
        'drama_create_shot': ({'episodeId': ident, 'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
            'assetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'}, 'requestKey': key},
            ['episodeId', 'title', 'requestKey']),
        'drama_revise_shot': ({'shotId': ident, 'expectedRevision': revision, 'baseRevisionId': ident, 'requestKey': key,
            'snapshot': {'type': 'object', 'properties': {
                'title': {'type': 'string', 'minLength': 1, 'maxLength': 160},
                'action': {'type': 'string', 'maxLength': 4000},
                'framing': {'type': 'string', 'maxLength': 120},
                'camera': {'type': 'string', 'maxLength': 120},
                'prompt': {'type': 'string', 'maxLength': 16384},
                'referenceNodes': {'type': ['array', 'null'], 'items': {'type': 'object', 'properties': {
                    'id': {'type': 'string'}, 'media': {'enum': ['image', 'video', 'audio']},
                    'assetId': {'type': 'string', 'pattern': '^([a-f0-9]{64})?$'},
                    'role': {'enum': ['character', 'costume', 'scene', 'action', 'first', 'last', 'custom']},
                    'customRoleId': {'type': 'string'}, 'description': {'type': 'string'}},
                    'required': ['id', 'media', 'assetId', 'role', 'description'], 'additionalProperties': False}},
                'customReferenceRoles': {'type': 'array', 'items': {'type': 'object', 'properties': {
                    'id': {'type': 'string'}, 'label': {'type': 'string'}, 'token': {'type': 'string'}},
                    'required': ['id', 'label', 'token'], 'additionalProperties': False}},
                'characterRevisionIds': {'type': 'array', 'items': ident, 'maxItems': 16, 'uniqueItems': True},
                'locationRevisionId': ident,
                'costumeRevisionIds': {'type': 'array', 'items': ident, 'maxItems': 16, 'uniqueItems': True},
                'propRevisionIds': {'type': 'array', 'items': ident, 'maxItems': 16, 'uniqueItems': True},
                'approvedReferenceAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'actionReferenceAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'actionReferencePrompt': {'type': 'string', 'maxLength': 4000},
                'sceneReferenceAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'sceneReferencePrompt': {'type': 'string', 'maxLength': 4000},
                'blockingReferenceAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'blockingReferencePrompt': {'type': 'string', 'maxLength': 4000},
                'keyframeGridAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'keyframeGridPrompt': {'type': 'string', 'maxLength': 4000},
                'videoReferenceAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'audioReferenceAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'firstFrameAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'lastFrameAssetId': {'type': 'string', 'pattern': '^[a-f0-9]{64}$'},
                'audioDescription': {'type': 'string', 'maxLength': 2000},
                'referenceRole': {'type': 'string', 'enum': ['reference_image', 'first_frame']},
                'targetEditFrames': {'type': 'integer', 'minimum': 1, 'maximum': 86400},
                'generation': {'type': 'object', 'properties': {
                    'contractVersion': {'type': 'string'}, 'profile': {'type': 'string'},
                    'requestDurationSeconds': {'type': 'integer', 'minimum': 4, 'maximum': 15}},
                    'required': ['contractVersion', 'profile', 'requestDurationSeconds'], 'additionalProperties': False}},
                'required': ['title'], 'additionalProperties': False}},
            ['shotId', 'expectedRevision', 'baseRevisionId', 'snapshot', 'requestKey']),
        'drama_delete_shot': ({'shotId': ident, 'expectedRevision': revision, 'requestKey': key},
                              ['shotId', 'expectedRevision', 'requestKey']),
        'drama_list_shot_audit': ({}, []),
        'drama_merge_project': ({'sourceProjectId': ident, 'sourceExpectedRevision': revision,
            'expectedRevision': revision, 'episodeIds': {'type': 'array', 'items': ident,
            'minItems': 1, 'maxItems': 2000, 'uniqueItems': True}, 'requestKey': key},
            ['sourceProjectId', 'sourceExpectedRevision', 'expectedRevision', 'episodeIds', 'requestKey']),
    })
    contracts.update(review_contracts(ident, key, revision))
    # Read-back snapshots contain null for unused optional slots. Accept that
    # same UI snapshot rather than forcing MCP clients to drop fields.
    shot_properties = contracts['drama_revise_shot'][0]['snapshot']['properties']
    for field_name, schema in shot_properties.items():
        if field_name.endswith('AssetId') or field_name == 'locationRevisionId':
            shot_properties[field_name] = {**schema, 'type': ['string', 'null']}
    # Project-lifecycle schemas. These two entries are placeholders so the
    # generic loop below can build a well-formed envelope; tools_list()
    # replaces their inputSchema with the projectId-free forms afterwards.
    contracts['drama_create_project'] = ({'title': {'type': 'string'}, 'requestKey': key}, ['title', 'requestKey'])
    contracts['drama_create_episode'] = ({'title': {'type': 'string'}, 'requestKey': key}, ['title', 'requestKey'])
    result = [{'name': name, 'description': description, 'inputSchema': {'type': 'object', 'properties': {
        'projectId': ident, 'arguments': {'type': 'object', 'properties': contracts[name][0], 'required': contracts[name][1], 'additionalProperties': False}},
        'required': ['projectId', 'arguments'], 'additionalProperties': False}}
        for name, description in DESCRIPTIONS.items()]
    listing = next(t for t in result if t['name'] == 'drama_list_projects')
    listing['inputSchema'] = {'type': 'object', 'properties': {}, 'additionalProperties': False}
    create_project = next(t for t in result if t['name'] == 'drama_create_project')
    create_project['inputSchema'] = {'type': 'object', 'properties': {
        'arguments': {'type': 'object', 'properties': {
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 200},
            'width': {'type': 'integer', 'minimum': 64, 'maximum': 3840},
            'height': {'type': 'integer', 'minimum': 64, 'maximum': 3840},
            'fps': {'type': 'integer', 'enum': [24]},
            'kind': {'type': 'string', 'enum': ['drama', 'mv']},
            'requestKey': key},
            'required': ['title', 'requestKey'], 'additionalProperties': False}},
        'required': ['arguments'], 'additionalProperties': False}
    create_episode = next(t for t in result if t['name'] == 'drama_create_episode')
    create_episode['inputSchema'] = {'type': 'object', 'properties': {
        'projectId': ident, 'arguments': {'type': 'object', 'properties': {
            'title': {'type': 'string', 'minLength': 1, 'maxLength': 200}, 'requestKey': key},
            'required': ['title', 'requestKey'], 'additionalProperties': False}},
        'required': ['projectId', 'arguments'], 'additionalProperties': False}
    read_only = {'drama_list_projects', 'drama_get_project', 'drama_get_episode', 'drama_list_entities',
                 'drama_get_shot', 'drama_image_review_queue', 'drama_list_shot_audit'}
    shot_tools = {'drama_create_shot', 'drama_revise_shot', 'drama_delete_shot', 'drama_list_shot_audit'}
    for tool in result:
        if tool['name'] in REVIEW_DESCRIPTIONS or tool['name'] in PROJECT_DESCRIPTIONS or tool['name'] in shot_tools:
            tool['annotations'] = {'readOnlyHint': tool['name'] in read_only, 'openWorldHint': False}
    from ..mcp import TOOLS
    from copy import deepcopy
    choices = []
    for tool in TOOLS:
        if tool['name'] not in ('generate_image', 'edit_image', 'compose_image_layers'): continue
        schema = deepcopy(tool['inputSchema'])
        schema['properties'].pop('idempotency_key', None)
        schema['required'] = [k for k in schema.get('required', []) if k != 'idempotency_key']
        choices.append({'properties': {'tool': {'const': tool['name']}, 'parameters': schema}})
    for name in ('drama_request_keyframe', 'drama_request_entity_image'):
        arguments = next(t for t in result if t['name'] == name)['inputSchema']['properties']['arguments']
        arguments['properties']['tool'] = {'type': 'string', 'enum': ['generate_image', 'edit_image', 'compose_image_layers']}
        arguments['oneOf'] = choices
    return result


def _scope(service, pid, table, oid):
    with service.store.connect() as db:
        if table == 'dr_text_jobs':
            row = service._text_job_row(db, identifier(oid))
        else:
            row = service.store.one(db, table, identifier(oid))
        if table in ('dr_shot_revisions', 'dr_takes'):
            row = service.store.one(db, 'dr_shots', row['shot_id'])
        if table == 'dr_entity_revisions':
            row = service.store.one(db, 'dr_entities', row['entity_id'])
        if 'project_id' not in row:
            row = service.store.one(db, 'dr_episodes', row['episode_id'])
        if row['project_id'] != pid:
            raise DramaError('wrong_project', '对象不属于当前项目', 404)
    return oid


def call(service, name, envelope):
    if not isinstance(name, str) or name not in DESCRIPTIONS:
        raise DramaError('tool_not_allowed', '业务工具不提供此操作', 403)
    if name == 'drama_list_projects':
        fields(envelope, set(), set())
        return service.projects()
    if name == 'drama_create_project':
        fields(envelope, {'arguments'}, {'arguments'})
        body = envelope['arguments']
        if not isinstance(body, dict):
            raise DramaError('invalid_fields', 'arguments 必须是对象')
        return service.create_project(body)
    if name == 'drama_create_episode':
        fields(envelope, {'projectId', 'arguments'}, {'projectId', 'arguments'})
        body = envelope['arguments']
        if not isinstance(body, dict):
            raise DramaError('invalid_fields', 'arguments 必须是对象')
        return service.create_episode(identifier(envelope['projectId']), body)
    fields(envelope, {'projectId', 'arguments'}, {'projectId', 'arguments'})
    pid = identifier(envelope['projectId'])
    body = envelope['arguments']
    if not isinstance(body, dict):
        raise DramaError('invalid_fields', 'arguments 必须是对象')
    with service.store.connect() as db:
        service.store.one(db, 'dr_projects', pid)
    token = CALL_SOURCE.set('business_mcp')
    try:
        if name == 'drama_merge_project':
            return service.merge_project(pid, body)
        if name in REVIEW_DESCRIPTIONS:
            return call_review(service, name, pid, body)
        if name in TEXT_TOOLS:
            fields(body, {'episodeId', 'sourceRevisionIds', 'entityRevisionIds', 'beatRevisionId', 'scriptRevisionId', 'requestKey', 'repairOf'}, {'episodeId', 'sourceRevisionIds', 'requestKey'})
            eid = _scope(service, pid, 'dr_episodes', body['episodeId'])
            return service.start_text_job(eid, {**{k: v for k, v in body.items() if k != 'episodeId'}, 'kind': TEXT_TOOLS[name]})
        if name == 'drama_propose_entity_revision':
            fields(body, {'entityId', 'expectedRevision', 'name', 'description', 'referenceAssetId', 'referenceRole', 'references', 'requestKey'}, {'entityId', 'expectedRevision', 'name', 'requestKey'})
            eid = _scope(service, pid, 'dr_entities', body['entityId'])
            return service.revise_entity(eid, {k: v for k, v in body.items() if k != 'entityId'})
        if name == 'drama_request_keyframe':
            fields(body, {'shotRevisionId', 'tool', 'parameters', 'requestKey'}, {'shotRevisionId', 'tool', 'parameters', 'requestKey'})
            rid = _scope(service, pid, 'dr_shot_revisions', body['shotRevisionId'])
            return service.submit_reference_job('shot', rid, {k: v for k, v in body.items() if k != 'shotRevisionId'})
        if name == 'drama_request_entity_image':
            fields(body, {'entityRevisionId', 'tool', 'parameters', 'requestKey', 'referenceRole'}, {'entityRevisionId', 'tool', 'parameters', 'requestKey'})
            rid = _scope(service, pid, 'dr_entity_revisions', body['entityRevisionId'])
            return service.submit_reference_job('entity', rid, {k: v for k, v in body.items() if k != 'entityRevisionId'})
        if name == 'drama_request_video_take':
            fields(body, {'episodeId', 'shots', 'requestKey'}, {'episodeId', 'shots', 'requestKey'})
            eid = _scope(service, pid, 'dr_episodes', body['episodeId'])
            return service.submit_video_batch(eid, {k: v for k, v in body.items() if k != 'episodeId'})
        if name == 'drama_get_job':
            fields(body, {'jobId', 'kind'}, {'jobId', 'kind'})
            options = {'text': ('dr_text_jobs', service.text_job), 'image': ('dr_reference_jobs', service.reference_job),
                       'video': ('dr_video_jobs', service.video_job), 'export': ('dr_exports', service.export)}
            if not isinstance(body['kind'], str) or body['kind'] not in options:
                raise DramaError('invalid_job_kind', '不支持的任务类型')
            table, getter = options[body['kind']]
            return getter(_scope(service, pid, table, body['jobId']))
        if name == 'drama_record_review_suggestion':
            fields(body, {'takeId', 'suggestion', 'requestKey'}, {'takeId', 'suggestion', 'requestKey'})
            tid = _scope(service, pid, 'dr_takes', body['takeId'])
            suggestion = text(body['suggestion'], '审片建议', 4000)
            def save(db):
                import time
                result = {'id': new_id(), 'takeId': tid, 'suggestion': suggestion, 'actor': 'business_mcp', 'changesApproval': False}
                db.execute('INSERT INTO dr_events VALUES (?,?,?,?,?)', (result['id'], f'project:{pid}', 'review_suggestion', encode(result), time.time()))
                return {'suggestion': result}
            return service.store.mutate(f'review-suggestion:{pid}', body['requestKey'], body, save)
        if name == 'drama_save_timeline':
            fields(body, {'episodeId', 'clips', 'audioClips', 'expectedRevision', 'requestKey'}, {'episodeId', 'clips', 'expectedRevision', 'requestKey'})
            eid = _scope(service, pid, 'dr_episodes', body['episodeId'])
            return service.save_timeline(eid, {k: v for k, v in body.items() if k != 'episodeId'})
        if name == 'drama_create_export':
            fields(body, {'timelineId', 'requestKey', 'outputs', 'subtitleRevisionId', 'presetVersion'}, {'timelineId', 'requestKey'})
            tid = _scope(service, pid, 'dr_timeline_revisions', body['timelineId'])
            return service.create_export(tid, {k: v for k, v in body.items() if k != 'timelineId'})
        if name == 'drama_import_audio':
            fields(body, {'path', 'assetId', 'filename', 'requestKey'}, {'requestKey'})
            return service.import_audio(pid, body)
        if name == 'drama_import_video':
            fields(body, {'path', 'assetId', 'filename', 'requestKey'}, {'requestKey'})
            return service.import_video(pid, body)
        if name == 'drama_import_image':
            fields(body, {'path', 'assetId', 'filename', 'requestKey'}, {'requestKey'})
            return service.import_image(pid, body)
        if name == 'drama_create_entity':
            fields(body, {'kind', 'name', 'description', 'referenceAssetId', 'referenceRole', 'references', 'requestKey'},
                   {'kind', 'name', 'requestKey'})
            return service.create_entity(pid, body)
        if name == 'drama_bind_song':
            fields(body, {'episodeId', 'songAssetId', 'expectedRevision', 'requestKey'}, {'episodeId', 'songAssetId', 'expectedRevision', 'requestKey'})
            eid = _scope(service, pid, 'dr_episodes', body['episodeId'])
            return service.bind_song(eid, {k: v for k, v in body.items() if k != 'episodeId'})
        if name == 'drama_create_shot':
            fields(body, {'episodeId', 'title', 'assetId', 'requestKey'}, {'episodeId', 'title', 'requestKey'})
            eid = _scope(service, pid, 'dr_episodes', body['episodeId'])
            return service.create_shot(eid, {k: v for k, v in body.items() if k != 'episodeId'})
        if name == 'drama_revise_shot':
            fields(body, {'shotId', 'expectedRevision', 'baseRevisionId', 'snapshot', 'requestKey'},
                   {'shotId', 'expectedRevision', 'baseRevisionId', 'snapshot', 'requestKey'})
            sid = _scope(service, pid, 'dr_shots', body['shotId'])
            return service.revise_shot(sid, {k: v for k, v in body.items() if k != 'shotId'})
        if name == 'drama_delete_shot':
            fields(body, {'shotId', 'expectedRevision', 'requestKey'},
                   {'shotId', 'expectedRevision', 'requestKey'})
            sid = _scope(service, pid, 'dr_shots', body['shotId'])
            return service.delete_shot(sid, {k: v for k, v in body.items() if k != 'shotId'})
        if name == 'drama_list_shot_audit':
            return service.list_shot_audit(pid)
        fields(body, {'episodeId'}, {'episodeId'})
        eid = _scope(service, pid, 'dr_episodes', body['episodeId'])
        with service.store.connect() as db:
            project = service.store.one(db, 'dr_projects', pid)
            episode = service.store.one(db, 'dr_episodes', eid)
            clips = []
            for shot in db.execute('SELECT * FROM dr_shots WHERE episode_id=? ORDER BY ordinal', (eid,)):
                if not shot['selected_take_id']:
                    raise DramaError('take_not_selected', '粗剪需要每个镜头先采用候选', 409)
                take = service.store.one(db, 'dr_takes', shot['selected_take_id'])
                review = db.execute('SELECT decision FROM dr_take_reviews WHERE take_id=?', (take['id'],)).fetchone()
                if not review or review[0] != 'approved' or take['source_revision_id'] != shot['current_content_revision_id']:
                    raise DramaError('take_not_approved', '粗剪需要当前镜头修订的已批准候选', 409)
                asset = service._bound_asset(db, pid, take['asset_id'])
                metadata = json.loads(asset['metadata'])
                content = json.loads(service.store.one(db, 'dr_shot_revisions', shot['current_content_revision_id'])['snapshot'])
                frames = service._default_snapshot(shot, content)['targetEditFrames']
                if frames > math.floor(metadata['frames'] * project['fps'] / metadata['fps'] + 1e-6):
                    raise DramaError('clip_out_of_bounds', '素材不足以覆盖镜头剪辑时长', 409)
                clips.append({'clipId': 'rough-' + shot['id'], 'takeId': take['id'], 'inFrame': 0, 'frames': frames,
                              'keepSourceAudio': project['kind'] != 'mv', 'fit': 'contain'})
            if not 1 <= len(clips) <= 64:
                raise DramaError('invalid_timeline', '粗剪需要1–64个已批准镜头')
            audio = []
            if project['kind'] == 'mv' and episode.get('song_asset_id'):
                song_id, song_frames = service._song_clock(db, episode, project)
                audio = service._ensure_song_bgm([], song_id, song_frames)
        return {'draft': {'episodeId': eid, 'expectedRevision': episode['timeline_revision'], 'clips': clips, 'audioClips': audio}, 'saved': False}
    finally:
        CALL_SOURCE.reset(token)


def rpc(service, message):
    """Minimal stateless MCP JSON-RPC behind the existing authenticated route."""
    if not isinstance(message, dict) or message.get('jsonrpc') != '2.0':
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}
    rid, method, params = message.get('id'), message.get('method'), message.get('params', {})
    if method == 'initialize':
        result = {'protocolVersion': '2024-11-05', 'capabilities': {'tools': {}}, 'serverInfo': {'name': 'dgx-drama-business', 'version': '1.0'}}
    elif method == 'tools/list':
        result = {'tools': tools_list()}
    elif method == 'ping' or method == 'notifications/initialized':
        result = {}
    elif method == 'tools/call':
        try:
            fields(params, {'name', 'arguments'}, {'name', 'arguments'})
            value = call(service, params['name'], params['arguments'])
            result = {'content': [{'type': 'text', 'text': encode(value)}], 'isError': False}
        except DramaError as exc:
            result = {'content': [{'type': 'text', 'text': encode({'error': {'code': exc.code, 'message': exc.message}})}], 'isError': True}
    else:
        return {'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32601, 'message': 'Method not found'}}
    return {'jsonrpc': '2.0', 'id': rid, 'result': result}


def call_review(service, name, pid, body):
    """Narrow adapters: service methods own atomicity, CAS and idempotency."""
    ident = {'type': 'string', 'pattern': '^[a-f0-9]{32}$'}
    props, required = review_contracts(ident, {'type': 'string', 'minLength': 1, 'maxLength': 128}, {'type': 'integer', 'minimum': 0})[name]
    from ..mcp import _validate
    try:
        _validate({'properties': props, 'required': required}, body)
        _validate_review_patterns({'properties': props}, body)
    except ValueError as exc:
        raise DramaError('invalid_fields', str(exc)) from None
    if name == 'drama_select_images':
        # Structural/ownership and duplicate-slot checks precede all mutations.
        slots = set()
        with service.store.connect() as db:
            for item in body['selections']:
                candidate = service.store.one(db, 'dr_reference_candidates', identifier(item['candidateId']))
                if (candidate['project_id'] != pid or candidate['target_type'] != item['targetType'] or
                        candidate['target_id'] != item['targetId'] or candidate['asset_id'] != item['assetId']):
                    raise DramaError('wrong_candidate', '批量候选不属于指定项目或目标')
                table = 'dr_entity_revisions' if item['targetType'] == 'entity' else 'dr_shot_revisions'
                source = service.store.one(db, table, candidate['target_revision_id'])
                if item['targetType'] == 'entity':
                    from .production import ROLES
                    entity = service.store.one(db, 'dr_entities', item['targetId'])
                    job = service.store.one(db, 'dr_reference_jobs', candidate['job_id'])
                    job_role = json.loads(job['input_snapshot']).get('referenceRole')
                    role = job_role or json.loads(source['snapshot']).get('referenceRole', ROLES[entity['kind']][0])
                else:
                    # Shots have one approvedReferenceAssetId, regardless of
                    # whether its source revision called it a keyframe/scene.
                    role = 'shot_reference'
                slot = (item['targetType'], item['targetId'], role)
                if slot in slots: raise DramaError('duplicate_slot', '同一参考槽位每批只能选用一张')
                slots.add(slot)
        results = []
        for item in body['selections']:
            entity = item['targetType'] == 'entity'
            payload = {k: v for k, v in item.items() if k not in {'targetType', 'targetId', 'assetId'}}
            payload['entityId' if entity else 'shotId'] = item['targetId']
            payload['referenceAssetId' if entity else 'assetId'] = item['assetId']
            try:
                value = call_review(service, 'drama_select_entity_image' if entity else 'drama_select_shot_image', pid, payload)
                results.append({'candidateId': item['candidateId'], 'ok': True, 'result': value})
            except DramaError as exc:
                results.append({'candidateId': item['candidateId'], 'ok': False, 'error': {'code': exc.code, 'message': exc.message}})
        return {'complete': all(r['ok'] for r in results), 'results': results}

    if name == 'drama_get_project': return service.project(pid)
    if name == 'drama_list_entities': return service.entities(pid)
    if name == 'drama_get_episode': return service.episode(_scope(service, pid, 'dr_episodes', body['episodeId']))
    if name == 'drama_get_shot': return service.shot_production(_scope(service, pid, 'dr_shots', body['shotId']))
    if name == 'drama_image_review_queue':
        if body.get('episodeId'): _scope(service, pid, 'dr_episodes', body['episodeId'])
        return service.image_review_queue(pid, episode_id=body.get('episodeId'), scope=body['scope'])
    if name == 'drama_review_images': return service.review_image_candidates(pid, body)
    method, table, field = {
        'drama_select_entity_image': ('revise_entity', 'dr_entities', 'entityId'),
        'drama_approve_entity': ('approve_entity', 'dr_entities', 'entityId'),
        'drama_select_shot_image': ('select_reference', 'dr_shots', 'shotId'),
        'drama_review_take': ('review_take', 'dr_takes', 'takeId'),
        'drama_select_take': ('select_take', 'dr_shots', 'shotId'),
        'drama_review_shot': ('review_shot', 'dr_shots', 'shotId'),
    }[name]
    oid = _scope(service, pid, table, body[field])
    payload = {k: v for k, v in body.items() if k != field}
    if name == 'drama_select_entity_image':
        # The candidate branch preserves the current name/description in the
        # transaction. A constant placeholder keeps replay payloads identical.
        payload['name'] = 'candidate-selection'
    if name == 'drama_review_shot': payload.setdefault('reason', '')
    return getattr(service, method)(oid, payload)


def _validate_review_patterns(spec, value):
    # Model MCP's small validator handles types/ranges, while business object
    # identifiers and unique arrays also need their advertised schema rules.
    if 'pattern' in spec and not re.fullmatch(spec['pattern'], value):
        raise ValueError('invalid identifier')
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_review_patterns(spec.get('properties', {}).get(key, {}), item)
    if isinstance(value, list):
        if spec.get('uniqueItems') and len({encode(item) for item in value}) != len(value):
            raise ValueError('duplicate items')
        for item in value: _validate_review_patterns(spec.get('items', {}), item)
