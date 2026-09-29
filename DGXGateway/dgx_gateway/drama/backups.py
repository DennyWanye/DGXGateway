"""Project-only consistent backups. Archive schemas never become executable SQL."""
from __future__ import annotations
from contextlib import closing, contextmanager
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import sys
import tempfile
import time
import zipfile

from .contracts import DramaError
from .store import encode

MIB = 1024 * 1024
MAX_TOTAL = 2 * 1024 * MIB
MAX_ROWS = 64 * MIB
MAX_LINE = 16 * MIB
RESERVE = 128 * MIB
EXTENSIONS = {'mp4','png','jpg','webp','wav','mp3','m4a','flac','srt','ass','json'}
MIGRATIONS = Path(__file__).parent / 'migrations'
TABLES = tuple(name for p in sorted(MIGRATIONS.glob('*.sql')) for name in re.findall(r'CREATE TABLE (\w+)', p.read_text()))
SCHEMA_HASH = hashlib.sha256(b''.join(p.read_bytes() for p in sorted(MIGRATIONS.glob('*.sql')))).hexdigest()
SCHEMA_VERSION = 9
SCHEMA6_MIGRATIONS = tuple(p for p in sorted(MIGRATIONS.glob('*.sql')) if int(p.name.split('_')[0]) <= 6)
SCHEMA6_HASH = hashlib.sha256(b''.join(p.read_bytes() for p in SCHEMA6_MIGRATIONS)).hexdigest()
SCHEMA7_MIGRATIONS = tuple(p for p in sorted(MIGRATIONS.glob('*.sql')) if int(p.name.split('_')[0]) <= 7)
SCHEMA7_HASH = hashlib.sha256(b''.join(p.read_bytes() for p in SCHEMA7_MIGRATIONS)).hexdigest()
SCHEMA8_MIGRATIONS = tuple(p for p in sorted(MIGRATIONS.glob('*.sql')) if int(p.name.split('_')[0]) <= 8)
SCHEMA8_HASH = hashlib.sha256(b''.join(p.read_bytes() for p in SCHEMA8_MIGRATIONS)).hexdigest()
TERMINAL = {'SUCCEEDED','FAILED','CANCELED','INTERRUPTED'}
SECRET = re.compile(r'-----BEGIN (?:[A-Z ]*PRIVATE KEY)-----|(?:authorization\s*[:=]\s*bearer\s+\S+)|(?:[?&](?:x-amz-signature|x-goog-signature|signature|token|access_token)=)|\b(?:sk-[a-zA-Z0-9_-]{20,}|hf_[a-zA-Z0-9]{20,})', re.I)

# Authored text is preserved verbatim; recognizable credentials block packaging.
SECRET = re.compile(SECRET.pattern + r'|\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}|(?:api[_-]?key|access[_-]?token|password)\s*["\']?\s*[:=]\s*["\']?[^\s"\']{6,}', re.I)


def fail(message, code='backup_invalid', status=409):
    raise DramaError(code, message, status)


def sha(data): return hashlib.sha256(data).hexdigest()


def strict_json(raw):
    def pairs(items):
        result = {}
        for key,value in items:
            if key in result: fail('JSON包含重复字段')
            result[key]=value
        return result
    try:
        value=json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda x: fail('JSON数值无效'))
        def depth(v,n=0):
            if n>32: fail('JSON结构过深')
            if isinstance(v,dict):
                for child in v.values(): depth(child,n+1)
            elif isinstance(v,list):
                for child in v: depth(child,n+1)
        depth(value)
        return value
    except (ValueError,UnicodeError,RecursionError): fail('JSON内容不能解析')


@contextmanager
def directory_fd(path):
    # Walk from a trusted root using pinned directory descriptors. The two macOS
    # system aliases are resolved only after every other symlink was rejected.
    path=confined_directory(path).resolve()
    fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child=os.open(part,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=fd)
            os.close(fd); fd=child
        yield fd
    finally: os.close(fd)


def fsync_directory(path):
    with directory_fd(path) as fd: os.fsync(fd)


def atomic_json(path, value):
    path=Path(path); temporary=path.name+'.partial'
    with directory_fd(path.parent) as parent:
        fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC|os.O_NOFOLLOW,0o600,dir_fd=parent)
        with os.fdopen(fd,'w',encoding='utf8') as stream:
            stream.write(encode(value)); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary,path.name,src_dir_fd=parent,dst_dir_fd=parent)
        os.fsync(parent)


def no_replace(source, target):
    source=Path(source); target=Path(target); libc=ctypes.CDLL(None,use_errno=True)
    with directory_fd(source.parent) as source_fd,directory_fd(target.parent) as target_fd:
        if sys.platform=='darwin':
            fn=libc.renameatx_np; fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        elif sys.platform.startswith('linux'):
            fn=libc.renameat2; fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
        else: fail('当前平台没有验证过的原子发布接口')
        rc=fn(source_fd,os.fsencode(source.name),target_fd,os.fsencode(target.name),4 if sys.platform=='darwin' else 1)
        if rc: raise OSError(ctypes.get_errno(),'Atomic publication refused')
        os.fsync(target_fd)


def confined_directory(path):
    path=Path(path).absolute()
    for part in (path,*path.parents):
        system_alias = sys.platform == 'darwin' and str(part) in {'/var','/tmp'} and str(part.resolve()) == '/private'+str(part)
        if part.is_symlink() and not system_alias: fail('目录不能包含符号链接')
    if not path.is_dir(): fail('目录不可用')
    return path


def open_regular(path):
    path=Path(path)
    with directory_fd(path.parent) as parent:
        fd=os.open(path.name,os.O_RDONLY|os.O_NOFOLLOW,dir_fd=parent)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd); fail('归档成员必须是普通文件')
    return os.fdopen(fd,'rb')


def space(path, required):
    if required<0 or required>MAX_TOTAL*2 or shutil.disk_usage(path).free < required+RESERVE:
        fail('磁盘空间不足；操作不会清理其他目录', 'backup_space', 507)


def snapshot(store, *, maximum=MAX_ROWS, cancel=lambda: False):
    result=sqlite3.connect(':memory:'); result.row_factory=sqlite3.Row
    started=time.monotonic()
    try:
        with store.connect() as source:
            page_size=source.execute('PRAGMA page_size').fetchone()[0]
            def progress(status,remaining,total):
                if cancel(): fail('备份已取消','backup_canceled')
                if total*page_size>maximum: fail('一致快照超过64MiB上限')
                if time.monotonic()-started>60: fail('一致快照超时，请稍后重新操作')
            source.backup(result,pages=256,progress=progress,sleep=.01)
        version = result.execute('PRAGMA user_version').fetchone()[0]
        if version in {6, 7, 8}:
            # Upgrade only the private snapshot, never the user's live database.
            for migration in sorted(MIGRATIONS.glob('*.sql')):
                if int(migration.name.split('_')[0]) > version: result.executescript(migration.read_text())
        if result.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
            fail('仅支持已冻结schema6/schema7/schema8/schema9')
        return result
    except BaseException:
        result.close(); raise


def fresh_schema(path):
    db=sqlite3.connect(path); db.row_factory=sqlite3.Row
    for p in sorted(MIGRATIONS.glob('*.sql')): db.executescript(p.read_text())
    db.execute('PRAGMA foreign_keys=ON')
    return db


def schema_columns(db):
    names={r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    if names!=set(TABLES): fail('数据库表结构不在冻结合同中')
    return {table:[r[1] for r in db.execute(f'PRAGMA table_info({table})')] for table in TABLES}


def normalize_archive_rows(rows, version):
    """Accept exact schema6/schema7/schema8 rows, then add trusted empty MV metadata."""
    if version == SCHEMA_VERSION:
        return rows
    if version not in {6, 7, 8}:
        fail('项目包数据版本不兼容')
    legacy_migrations = {6: SCHEMA6_MIGRATIONS, 7: SCHEMA7_MIGRATIONS, 8: SCHEMA8_MIGRATIONS}[version]
    legacy_tables = tuple(name for p in legacy_migrations for name in re.findall(r'CREATE TABLE (\w+)', p.read_text()))
    if set(rows) != set(legacy_tables):
        fail('项目包数据版本不兼容')
    with closing(sqlite3.connect(':memory:')) as legacy:
        for migration in legacy_migrations: legacy.executescript(migration.read_text())
        columns = {table:[r[1] for r in legacy.execute(f'PRAGMA table_info({table})')] for table in legacy_tables}
    for table, values in rows.items():
        if not isinstance(values, list) or any(not isinstance(row, dict) or set(row) != set(columns[table]) for row in values):
            fail(f'schema{version}归档列结构无效：' + table)
    rows = strict_json(encode(rows))
    if version in {6, 7}:
        rows['dr_reference_candidate_reviews'] = []
        for row in rows['dr_video_jobs']:
            settled = row['memory_released'] == 1 and row['state'] in TERMINAL and row['cleanup_required'] == 0
            row['resource_settled'] = int(settled)
            row['resource_status'] = 'released' if settled else 'unsettled'
    for row in rows['dr_projects']:
        row.setdefault('kind', 'drama')
    for row in rows['dr_episodes']:
        row.setdefault('song_asset_id', None)
    return rows


def projection(db,pid):
    with closing(sqlite3.connect(':memory:')) as trusted:
        for p in sorted(MIGRATIONS.glob('*.sql')): trusted.executescript(p.read_text())
        if schema_columns(db)!=schema_columns(trusted): fail('数据库列结构不在冻结合同中')
    out={table:[] for table in TABLES}
    def take(table,column,ids):
        out[table]=[dict(row) for row in db.execute(f'SELECT * FROM {table}') if row[column] in ids]
        if len(out[table])>100000: fail('单表超过行数上限')
        return {r['id'] for r in out[table] if 'id' in r}
    P={pid}; take('dr_projects','id',P)
    if len(out['dr_projects'])!=1: fail('找不到需要备份的项目','not_found',404)
    E=take('dr_episodes','project_id',P); S=take('dr_shots','episode_id',E); SR=take('dr_shot_revisions','shot_id',S)
    T=take('dr_takes','shot_id',S); TL=take('dr_timeline_revisions','episode_id',E); X=take('dr_exports','project_id',P)
    N=take('dr_entities','project_id',P); NR=take('dr_entity_revisions','entity_id',N)
    take('dr_asset_bindings','project_id',P); A={r['asset_id'] for r in out['dr_asset_bindings']}; take('dr_assets','id',A)
    take('dr_entity_reference_bindings','revision_id',NR); take('dr_dependencies','shot_revision_id',SR); take('dr_invalidations','shot_revision_id',SR)
    out['dr_production_approvals']=[dict(r) for r in db.execute('SELECT * FROM dr_production_approvals') if (r['target_type']=='entity' and r['target_id'] in N) or (r['target_type']=='shot' and r['target_id'] in S)]
    RJ=take('dr_reference_jobs','project_id',P); take('dr_reference_candidates','project_id',P); take('dr_reference_candidate_reviews','project_id',P); take('dr_production_state','project_id',P)
    VJ=take('dr_video_jobs','project_id',P); take('dr_video_items','job_id',VJ); take('dr_take_reviews','take_id',T)
    out['dr_video_continuations']=[dict(r) for r in db.execute('SELECT * FROM dr_video_continuations') if r['source_job_id'] in VJ or r['continuation_job_id'] in VJ]
    SUB=take('dr_subtitle_revisions','episode_id',E); take('dr_subtitle_approvals','subtitle_id',SUB); take('dr_export_recipes','export_id',X)
    ASR=take('dr_transcriptions','timeline_id',TL); take('dr_transcription_details','job_id',ASR)
    ST=take('dr_story_revisions','episode_id',E); take('dr_story_heads','episode_id',E); take('dr_story_approvals','revision_id',ST); take('dr_story_drafts','episode_id',E)
    take('dr_entity_proposal_mappings','episode_id',E); take('dr_storyboard_mappings','episode_id',E); take('dr_script_dependencies','shot_revision_id',SR); take('dr_script_invalidations','shot_revision_id',SR)
    TJ=take('dr_text_jobs','episode_id',E); take('dr_text_attempts','job_id',TJ); take('dr_text_receipt_evidence','job_id',TJ)
    scope_sets={}
    for names,ids in [
        ('episodes upload audio audio-upload audio-import image-upload image-import video-import project-canvas project-merge entities production-state reference-submit reference-import image-reviews review-suggestion project fixture',P),
        ('shots order timeline song video-batches subtitles story-source story-entities story-beats story-script story-storyboard story-draft-entities story-draft-beats story-draft-script story-draft-storyboard text-jobs',E),
        ('selection shot-content shot-reference shot-review script-review',S),('entity-revisions entity-approval',N),('take-review',T),('exports transcriptions',TL),('export-retry export-approve',X),('subtitle-approve',SUB),('story-approve story-confirm story-import',ST),('transcription',ASR),('text-cancel',TJ)]:
        for name in names.split(): scope_sets[name]=ids
    def owned(scope):
        parts=scope.split(':'); return len(parts)==2 and parts[1] in scope_sets.get(parts[0],set())
    owned_ids=set().union(*scope_sets.values())
    def reject_unknown_owned(scope):
        parts=scope.split(':')
        if len(parts)==2 and parts[1] in owned_ids and parts[0] not in scope_sets:
            fail('已归属项目的scope尚未纳入归档合同')
    for row in db.execute('SELECT * FROM dr_requests'):
        r=dict(row); reject_unknown_owned(r['scope'])
        if owned(r['scope']) or (r['scope']=='projects' and strict_json(r['response']).get('project',{}).get('id')==pid): out['dr_requests'].append(r)
    keys={(r['scope'],r['request_key']) for r in out['dr_requests']}
    for row in db.execute('SELECT * FROM dr_events'):
        r=dict(row); reject_unknown_owned(r['scope']); payload=strict_json(r['payload'])
        if owned(r['scope']) or (r['scope']=='projects' and (r['scope'],payload.get('requestKey')) in keys): out['dr_events'].append(r)
    validate_rows(out,pid)
    return out


def validate_review_fixture(rows, event, pid):
    """Preserve the historical QA event without accepting arbitrary fixture data."""
    payload = strict_json(event['payload'])
    fields = set('fixture created projectId episodeId shotId currentJobId staleJobId source assetId currentTakeId staleTakeId gpuGenerated mediaSeconds note'.split())
    if event['scope'] != 'fixture:' + pid or event['kind'] != 'ui_review_fixture':
        fail('历史审片fixture事件scope或类型无效')
    if not isinstance(payload, dict) or set(payload) != fields:
        fail('历史审片fixture事件字段未纳入合同')
    if (payload['fixture'] != 'shadcn-review-fixture-v1' or payload['source'] != 'ui_review_fixture'
            or type(payload['created']) is not bool or payload['gpuGenerated'] is not False
            or type(payload['mediaSeconds']) is not int or payload['mediaSeconds'] != 8
            or not isinstance(payload['note'], str)):
        fail('历史审片fixture版本或来源不符合合同')
    index = {table: {row.get('id'): row for row in values if row.get('id')} for table, values in rows.items()}
    def get(table, field):
        oid = payload[field]
        if not isinstance(oid, str) or oid not in index[table]:
            fail('历史审片fixture引用不属于项目：' + field)
        return index[table][oid]
    project = get('dr_projects', 'projectId')
    episode = get('dr_episodes', 'episodeId')
    shot = get('dr_shots', 'shotId')
    asset = get('dr_assets', 'assetId')
    if project['id'] != pid or episode['project_id'] != pid or shot['episode_id'] != episode['id']:
        fail('历史审片fixture项目/分集/镜头关系不匹配')
    meta = strict_json(asset['metadata'])
    if meta.get('kind') != 'video' or meta.get('frames') != 192 or meta.get('fps') != 24:
        fail('历史审片fixture必须引用原8秒视频')
    if payload['currentJobId'] == payload['staleJobId'] or payload['currentTakeId'] == payload['staleTakeId']:
        fail('历史审片fixture两份候选身份不能重复')
    revisions = []
    for role in ('current', 'stale'):
        job = get('dr_video_jobs', role + 'JobId')
        take = get('dr_takes', role + 'TakeId')
        items = [row for row in rows['dr_video_items'] if row['job_id'] == job['id']]
        if (job['project_id'] != pid or job['episode_id'] != episode['id']
                or job['gateway_job_id'] is not None or job['dispatch_key'] != 'fixture-no-gpu-' + job['id']
                or job['state'] != 'SUCCEEDED' or job['reserved_shots'] != 0 or job['memory_released'] != 1
                or job['cleanup_required'] != 0 or len(items) != 1):
            fail('历史审片fixture任务归属或非GPU来源不匹配')
        item = items[0]
        if (take['shot_id'] != shot['id'] or take['asset_id'] != asset['id'] or take['source'] != payload['source']
                or item['shot_id'] != shot['id'] or item['take_id'] != take['id']
                or item['shot_revision_id'] != take['source_revision_id'] or item['output_hash'] != asset['id']
                or item['ordinal'] != 0 or item['state'] != 'SUCCEEDED' or item['submission_phase'] != 'RECEIVED'):
            fail('历史审片fixture候选与任务/素材关系不匹配')
        inputs = strict_json(item['input_snapshot'])
        if (inputs.get('source') != payload['source'] or inputs.get('fixture') != payload['fixture']
                or inputs.get('mediaSourceAssetId') != asset['id']):
            fail('历史审片fixture输入来源不匹配')
        revisions.append(take['source_revision_id'])
    # "current" and "stale" describe creation time, not today's mutable head.
    if revisions[0] == revisions[1]:
        fail('历史审片fixture必须保留两份不同修订')


def validate_rows(rows,pid):
    if set(rows)!=set(TABLES) or len(rows['dr_projects'])!=1 or rows['dr_projects'][0]['id']!=pid: fail('归档必须仅包含指定项目')
    with closing(sqlite3.connect(':memory:')) as schema:
        for migration in sorted(MIGRATIONS.glob('*.sql')):schema.executescript(migration.read_text())
        for table,values in rows.items():
            definitions=list(schema.execute(f'PRAGMA table_info({table})'))
            for row in values:
                if not isinstance(row,dict) or set(row)!={c[1] for c in definitions}:fail('归档列结构无效：'+table)
                for _,name,kind,required,_,primary in definitions:
                    value=row[name]
                    if value is None:
                        if required or primary:fail('归档必填字段缺失：'+table+'.'+name)
                        continue
                    valid=(kind=='TEXT' and isinstance(value,str)) or (kind=='INTEGER' and type(value) is int) or (kind=='REAL' and type(value) in (int,float) and math.isfinite(value))
                    if not valid:fail('归档字段类型无效：'+table+'.'+name)
                    if isinstance(value,str) and len(value.encode('utf8'))>MAX_LINE:fail('归档字段过长：'+table+'.'+name)
                    if kind=='INTEGER' and not -(2**63)<=value<2**63:fail('归档整数越界：'+table+'.'+name)
                    if name in {'revision','timeline_revision','epoch','ordinal','reserved_shots','base_revision'} and value<0:fail('归档版本或计数不能为负数')
                    if name in {'paused','memory_released','cleanup_required','cancel_requested','resource_settled'} and value not in (0,1):fail('归档布尔状态必须为0或1')
    project=rows['dr_projects'][0]
    from .contracts import integer, text
    width=integer(project['width'],'width',64,3840);height=integer(project['height'],'height',64,3840);integer(project['fps'],'fps',24,24);integer(project['revision'],'revision',1,2**31-1);text(project['title'],'项目名')
    if width%2 or height%2 or width*height>3840*2160:fail('恢复项目画布必须为偶数且不超过4K像素数')
    ids={t:{r.get('id') for r in values} for t,values in rows.items()}
    assets=ids['dr_assets']; all_ids=set().union(*ids.values())- {None}
    owned_projects={pid}; episodes=ids['dr_episodes']; shots=ids['dr_shots']; entityids=ids['dr_entities']; storyids=ids['dr_story_revisions']
    for table,values in rows.items():
        if len(values)>100000: fail('单表超过行数限制')
        for row in values:
            if table == 'dr_assets' and (type(row['size_bytes']) is not int or not 0 < row['size_bytes'] <= 512*MIB or not re.fullmatch('[a-f0-9]{64}',row['id'])): fail('素材大小或hash身份无效')
            if 'project_id' in row and row['project_id']!=pid: fail(f'{table}项目归属不匹配')
            if 'episode_id' in row and row['episode_id'] not in episodes: fail(f'{table}分集归属不匹配')
            if table == 'dr_video_jobs':
                if row['resource_status'] not in {'unsettled','resident','released'}:
                    fail('视频资源责任状态不在归档合同中')
                if row['resource_settled'] and (row['state'] not in TERMINAL or row['cleanup_required']
                        or row['resource_status'] != ('released' if row['memory_released'] else 'resident')):
                    fail('视频资源责任移交状态与任务事实不符')
            if table in {'dr_reference_jobs','dr_reference_candidates','dr_production_approvals'}:
                kind=row['target_type']; targets=entityids if kind=='entity' else shots if kind=='shot' else set()
                revisions=ids['dr_entity_revisions'] if kind=='entity' else ids['dr_shot_revisions']
                if row['target_id'] not in targets or row.get('target_revision_id',row.get('revision_id')) not in revisions: fail(f'{table}目标不属于当前项目')
            if table=='dr_story_drafts' and row.get('attempt_id') and row['attempt_id'] not in ids['dr_text_attempts']: fail('编辑草稿的attempt归属无效')
            if table=='dr_text_jobs' and row.get('current_attempt_id') and row['current_attempt_id'] not in ids['dr_text_attempts']: fail('文本当前attempt不在项目内')
            for field in ('snapshot','content','dependencies','parameters','input_snapshot','sources','dialogue','recipe','outputs','response','payload'):
                if field in row and row[field] is not None:
                    value=strict_json(row[field])
                    def refs(v,key=''):
                        asset_keys={'assetId','imageAssetId','audioAssetId','referenceAssetId','approvedReferenceAssetId','actionReferenceAssetId','sceneReferenceAssetId','blockingReferenceAssetId','keyframeGridAssetId','videoReferenceAssetId','audioReferenceAssetId','firstFrameAssetId','lastFrameAssetId','backgroundAssetId','asset_id','background_asset_id','image_asset_id','action_image_asset_id','video_asset_id','audio_asset_id'}
                        object_keys={'promptJobId','project_id','episode_id','shot_id','source_revision_id','locationRevisionId','speakerId','projectId','episodeId','shotId','shotRevisionId','takeId','timelineRevisionId','timelineId','subtitleRevisionId','scriptRevisionId','beatRevisionId','entityId','entityRevisionId','sourceRevisionId','exportId','subtitleId','transcriptionId','candidateAssetId','jobId','attemptId','currentAttemptId','targetId','targetRevisionId'}
                        plural={'sourceRevisionIds','entityRevisionIds','characterRevisionIds','costumeRevisionIds','propRevisionIds','assetIds','asset_ids','shotIds','shotRevisionIds','affectedShotIds','takeIds','jobIds','revisionIds','episodeIds','projectIds'}
                        if key in asset_keys and v and v not in assets: fail(f'{table}.{field}引用缺少项目素材绑定')
                        if key in object_keys and v and v not in all_ids: fail(f'{table}.{field}引用越出项目')
                        if key in plural and isinstance(v,list) and any(x not in all_ids for x in v): fail(f'{table}.{field}引用越出项目')
                        if isinstance(v,dict):
                            response_objects={'project','episode','shot','take','entity','asset','timeline','subtitle','export','revision','job','transcription','draft','attempt','current','head'}
                            if field=='response' and key in response_objects and v.get('id') and v['id'] not in all_ids: fail(f'{table}.{field}对象身份越出项目')
                            for k,x in v.items(): refs(x,k)
                        elif isinstance(v,list):
                            for x in v: refs(x,{'jobs':'job','transcriptions':'transcription','shots':'shot','takes':'take','entities':'entity','assets':'asset','revisions':'revision','attempts':'attempt','history':'revision','editingDrafts':'draft'}.get(key,''))
                    refs(value)
    from .backup_semantics import validate_semantics, validate_response_schema
    validate_semantics(rows,fail)
    scope_sets={}
    for names,table in [('episodes upload audio audio-upload audio-import image-upload image-import video-import project-canvas project-merge entities production-state reference-submit reference-import image-reviews review-suggestion project fixture','dr_projects'),('shots order timeline song video-batches subtitles story-source story-entities story-beats story-script story-storyboard story-draft-entities story-draft-beats story-draft-script story-draft-storyboard text-jobs','dr_episodes'),('selection shot-content shot-reference shot-review script-review','dr_shots'),('entity-revisions entity-approval','dr_entities'),('take-review','dr_takes'),('exports transcriptions','dr_timeline_revisions'),('export-retry export-approve','dr_exports'),('subtitle-approve','dr_subtitle_revisions'),('story-approve story-confirm story-import','dr_story_revisions'),('transcription','dr_transcriptions'),('text-cancel','dr_text_jobs')]:
        for name in names.split(): scope_sets[name]=ids[table]
    def scope_ok(scope):
        parts=scope.split(':'); return len(parts)==2 and parts[1] in scope_sets.get(parts[0],set())
    keys=set()
    for row in rows['dr_requests']:
        response=strict_json(row['response'])
        validate_response_schema(rows,row['scope'],response,fail)
        if not scope_ok(row['scope']) and not (row['scope']=='projects' and strict_json(row['response']).get('project',{}).get('id')==pid): fail('幂等记录不属于项目')
        keys.add((row['scope'],row['request_key']))
    for row in rows['dr_events']:
        payload=strict_json(row['payload'])
        if not scope_ok(row['scope']) and not (row['scope']=='projects' and (row['scope'],payload.get('requestKey')) in keys): fail('事件不属于项目')
        if row['scope'].startswith('fixture:') or row['kind']=='ui_review_fixture':
            validate_review_fixture(rows,row,pid)
        elif row['kind'] not in {'mutation','asr_provenance','retain_frozen_script','cancel_requested','review_suggestion','video_generation','shot_deleted','project_merged'}: fail('事件类型尚未纳入归档合同')
        if row['kind']=='mutation' and payload.get('actor','ui_api') not in {'ui_api','business_mcp'}: fail('事件actor尚未纳入归档合同')
        if row['kind'] in {'video_generation','shot_deleted','project_merged'} and payload.get('actor') not in {'ui_api','business_mcp'}: fail('事件actor尚未纳入归档合同')
    # Re-run exact scope selection on imported data; SQL FKs alone cannot prove scope ownership.
    with closing(sqlite3.connect(':memory:')) as db:
        for p in sorted(MIGRATIONS.glob('*.sql')): db.executescript(p.read_text())
        columns=schema_columns(db)
        db.execute('PRAGMA foreign_keys=ON'); db.execute('BEGIN'); db.execute('PRAGMA defer_foreign_keys=ON')
        for table in TABLES:
            for row in rows[table]:
                if set(row)!=set(columns[table]): fail('归档列结构无效')
                db.execute(f'INSERT INTO {table} ({",".join(columns[table])}) VALUES ({",".join("?" for _ in columns[table])})',[row[c] for c in columns[table]])
        if db.execute('PRAGMA foreign_key_check').fetchone(): fail('归档包含断开的引用')
        from .scripts import ScriptMixin
        from .store import DramaStore
        validator=ScriptMixin();validator.store=DramaStore
        db.row_factory=sqlite3.Row
        for row in rows['dr_story_revisions']:
            content=strict_json(row['content'])
            if sha(encode(content).encode())!=row['content_hash']:fail('故事内容hash不一致')
            validator._validate_story(db,row['episode_id'],row['kind'],content,current=False)
        for table in ('dr_story_drafts','dr_text_attempts'):
            for row in rows[table]:
                if row.get('raw_text') is not None and row.get('raw_hash')!=sha(row['raw_text'].encode()):fail('原始草稿hash不一致')
        db.rollback()


def sanitized(rows):
    rows=strict_json(encode(rows)); changes=[]
    technical={'kind','extension','contentType','width','height','fps','frames','duration','durationSeconds','sampleRate','channels','codec','videoCodec','audioCodec','hasAudio','hasVideo','sampleCount','audioSamples','startTime','outputRole','timeMappingHash','totalFrames','decodedFrames','sizeBytes','decodedSamples48k','role','dialogueContract','subtitleFontFamily','subtitleFontSha256'}
    for row in rows['dr_assets']:
        meta=strict_json(row['metadata']); ext=meta.get('extension','mp4')
        if ext not in EXTENSIONS: fail('素材扩展类型不受支持')
        filename=row['id']+'.'+ext; metadata=encode({k:v for k,v in meta.items() if k in technical})
        if row['filename']!=filename:changes.append({'table':'dr_assets','id':row['id'],'field':'filename'})
        if set(meta)-technical:changes.append({'table':'dr_assets','id':row['id'],'field':'metadata','removedFieldCount':len(set(meta)-technical)})
        row['filename']=filename; row['metadata']=metadata
    asset_views={r['id']:{'id':r['id'],'filename':r['filename'],'sizeBytes':r['size_bytes'],**strict_json(r['metadata'])} for r in rows['dr_assets']}
    def scrub_views(value):
        if isinstance(value,dict):
            if value.get('id') in asset_views and ('filename' in value or 'sizeBytes' in value): return asset_views[value['id']]
            return {key:scrub_views(child) for key,child in value.items()}
        if isinstance(value,list): return [scrub_views(child) for child in value]
        return value
    for row in rows['dr_requests']:
        cleaned=encode(scrub_views(strict_json(row['response'])))
        if cleaned!=row['response']:changes.append({'table':'dr_requests','scope':row['scope'],'requestKey':row['request_key'],'field':'response.assetViews'})
        row['response']=cleaned
    for row in rows['dr_export_recipes']:
        recipe=strict_json(row['recipe'])
        if '_runtime' in recipe:
            recipe['_runtime']={k:{'sha256':v['sha256'],'path':'RESTORE_BINDING_REQUIRED'} for k,v in recipe['_runtime'].items()}; row['recipe']=encode(recipe); changes.append({'table':'dr_export_recipes','id':row['export_id'],'field':'_runtime.path'})
    for row in rows['dr_text_receipt_evidence']:
        receipt=row['receipt']; existing=strict_json(receipt)
        if not (isinstance(existing,dict) and existing.get('redacted') is True and set(existing)=={'redacted','originalSha256','reason'} and re.fullmatch('[a-f0-9]{64}',str(existing.get('originalSha256','')))):
            row['receipt']=encode({'redacted':True,'originalSha256':sha(receipt.encode()),'reason':'错身份回执仅保留技术证据摘要'}); changes.append({'table':'dr_text_receipt_evidence','id':row['id'],'field':'receipt'})
    for table,values in rows.items():
        for row in values:
            for key,value in row.items():
                if isinstance(value,str) and SECRET.search(value): fail(f'{table}.{key}包含凭据结构，不能放入项目包','backup_credentials')
    return rows,changes


def write_database(path,rows):
    db=fresh_schema(path)
    try:
        columns=schema_columns(db); db.execute('BEGIN'); db.execute('PRAGMA defer_foreign_keys=ON')
        for table in TABLES:
            for row in rows[table]: db.execute(f'INSERT INTO {table} ({",".join(columns[table])}) VALUES ({",".join("?" for _ in columns[table])})',[row[k] for k in columns[table]])
        if db.execute('PRAGMA foreign_key_check').fetchone(): fail('项目数据库引用不完整')
        db.commit()
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok': fail('项目数据库完整性检查失败')
        db.execute('PRAGMA journal_mode=DELETE')
    finally: db.close()


def recovery_obligations(rows):
    obligations=[]
    text_cancels={r["scope"].split(":",1)[1] for r in rows["dr_events"] if r["kind"]=="cancel_requested" and r["scope"].startswith("text-cancel:")}
    assets={r['id'] for r in rows['dr_assets']}
    for table in ('dr_reference_jobs','dr_video_jobs','dr_text_jobs','dr_exports','dr_transcriptions'):
        for row in rows[table]:
            reasons=[]
            if row['state'] not in TERMINAL: reasons.append('非终态任务')
            if row.get('cleanup_required'):reasons.append('资源清理尚未完成')
            if row.get('resource_status') in {'held','releasing','recovery_required'}:reasons.append('文本资源尚未释放')
            if table=='dr_video_jobs':
                settled = row.get('resource_settled') == 1 and row['state'] in TERMINAL and not row.get('cleanup_required')
                if row.get('gateway_job_id') and not row.get('memory_released') and not settled:reasons.append('视频显存释放尚未确认')
                items=[r for r in rows['dr_video_items'] if r['job_id']==row['id']]
                if any(i['state'] in {'COLLECTING','OUTPUT_UNAVAILABLE','SUBMISSION_UNKNOWN'} or (i['state']=='SUCCEEDED' and not i.get('take_id')) for i in items):reasons.append('视频产物仍待核查或归档')
            if table=='dr_reference_jobs' and row['state']=='SUCCEEDED' and not any(r['job_id']==row['id'] for r in rows['dr_reference_candidates']):reasons.append('参考任务尚无已归档候选')
            if table=='dr_exports' and row['state']=='SUCCEEDED':
                recipe=next((r for r in rows['dr_export_recipes'] if r['export_id']==row['id']),None)
                if recipe:
                    requested=set(strict_json(recipe['recipe']).get('outputs',[]))
                    outputs=strict_json(recipe['outputs'])
                    present={r.get('role') for r in outputs if r.get('assetId') in assets}
                    if requested-present:reasons.append('导出仍缺少配方要求的产物')
                elif row.get('asset_id') not in assets:reasons.append('导出主片尚未归档')
            if table=='dr_text_jobs' and row['state']=='SUCCEEDED' and not row.get('current_attempt_id'):reasons.append('文本任务缺少原attempt')
            if reasons:
                obligations.append({'table':table,'id':row['id'],'state':row['state'],'gatewayId':row.get('gateway_job_id',row.get('gateway_id')),'dispatchKey':row.get('dispatch_key',row.get('request_key')),'cancelRequested':bool(row.get('cancel_requested')) or row['state']=='CANCEL_REQUESTED' or row['id'] in text_cancels,'cleanupRequired':bool(row.get('cleanup_required')),'memoryReleased':bool(row.get('memory_released')) if table=='dr_video_jobs' else None,'reasons':reasons})
    return obligations


def pack_project(service,pid,destination,bid,progress=lambda **kw:None):
    destination=Path(destination); confined_directory(destination.parent)
    snap=snapshot(service.store)
    try: rows=projection(snap,pid)
    finally: snap.close()
    rows,changes=sanitized(rows)
    obligations=recovery_obligations(rows)
    encoded={}
    total_rows=0
    for table in TABLES:
        lines=[]
        for row in sorted(rows[table],key=encode):
            line=(encode(row)+'\n').encode()
            if len(line)>MAX_LINE: fail('单条归档记录超过16MiB')
            total_rows+=len(line)
            if total_rows>MAX_ROWS: fail('归档元数据超过64MiB')
            lines.append(line)
        encoded[f'rows/{table}.jsonl']=b''.join(lines)
    total_assets=sum(r['size_bytes'] for r in rows['dr_assets'])
    if total_assets+total_rows*2>MAX_TOTAL: fail('项目超过2GiB包上限')
    space(destination.parent,total_assets+total_rows*3+16*MIB)
    manifest={'format':'dgx-drama-project','formatVersion':1,'schemaVersion':SCHEMA_VERSION,'schemaFingerprint':SCHEMA_HASH,'backupId':bid,'projectId':pid,'createdAt':time.time(),'snapshotKind':'recovery_required' if obligations else 'stable','files':[],'tables':{t:len(rows[t]) for t in TABLES},'redactions':changes}
    with tempfile.TemporaryDirectory(prefix='backup-own-',dir=destination.parent) as directory:
        database=Path(directory)/'project.sqlite3'; write_database(database,rows)
        if database.stat().st_size>MAX_ROWS: fail('项目数据库超过64MiB上限')
        encoded['recovery.json']=encode({'tasks':obligations,'defaultMode':'isolated'}).encode()
        from ..video_contracts import video_capabilities
        from .script_contracts import TEMPLATE_VERSION
        encoded['capabilities.json']=encode({'schemaVersion':SCHEMA_VERSION,'schemaFingerprint':SCHEMA_HASH,'videoCapabilities':video_capabilities(),'storyTemplateVersion':TEMPLATE_VERSION,'sourceCodeHashes':{p.name:sha(p.read_bytes()) for p in sorted(Path(__file__).parent.glob('*.py'))},'generationContracts':sorted({encode(strict_json(r['input_snapshot']).get('generation',{})) for r in rows['dr_video_items']})}).encode()
        partial=destination.with_name(destination.name+'.partial')
        try:
            with zipfile.ZipFile(partial,'x',compression=zipfile.ZIP_STORED,allowZip64=False) as z:
                os.chmod(partial,0o600)
                def add_bytes(name,data):
                    z.writestr(name,data); manifest['files'].append({'path':name,'bytes':len(data),'sha256':sha(data)})
                for name,data in encoded.items(): add_bytes(name,data)
                add_bytes('project.sqlite3',database.read_bytes())
                done=0
                for row in rows['dr_assets']:
                    if not re.fullmatch('[a-f0-9]{64}',row['id']): fail('素材hash身份无效')
                    ext=strict_json(row['metadata']).get('extension','mp4'); name=f"assets/{row['id']}.{ext}"
                    asset_path=service.root/name
                    with directory_fd(asset_path.parent) as asset_parent, os.fdopen(os.open(asset_path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=asset_parent),'rb') as source,z.open(name,'w') as output:
                        before=os.fstat(source.fileno()); digest=hashlib.sha256(); length=0
                        if not stat.S_ISREG(before.st_mode): fail('归档成员必须是普通文件')
                        while chunk:=source.read(MIB):
                            length+=len(chunk); done+=len(chunk)
                            if length>512*MIB or length>row['size_bytes']: fail('素材大小发生变化')
                            space(destination.parent,MIB); digest.update(chunk); output.write(chunk); progress(bytesDone=done,bytesTotal=total_assets)
                        after=os.fstat(source.fileno())
                        # Valid old FD bytes are insufficient if its directory
                        # entry was replaced while this archive was reading it.
                        current=os.stat(asset_path.name,dir_fd=asset_parent,follow_symlinks=False)
                        def identity(info): return (info.st_dev,info.st_ino,info.st_mode,info.st_size,info.st_mtime_ns,info.st_ctime_ns)
                        if identity(before)!=identity(after) or identity(before)!=identity(current) or length!=row['size_bytes'] or digest.hexdigest()!=row['id']: fail('素材hash或文件身份校验失败')
                    manifest['files'].append({'path':name,'bytes':length,'sha256':digest.hexdigest(),'assetId':row['id']})
                manifest['totalBytes']=sum(f['bytes'] for f in manifest['files'])
                if len(manifest['files'])+1>8192: fail('归档成员过多')
                raw=encode(manifest).encode()
                if len(raw)>MIB: fail('归档清单过大')
                z.writestr('manifest.json',raw)
            with partial.open('rb') as stream: os.fsync(stream.fileno()); digest=hashlib.file_digest(stream,'sha256').hexdigest()
            no_replace(partial,destination)
            return {'manifest':manifest,'sha256':digest,'bytes':destination.stat().st_size}
        finally: partial.unlink(missing_ok=True)
