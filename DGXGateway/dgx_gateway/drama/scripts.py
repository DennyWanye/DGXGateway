"""Persistent story authoring; approvals and production remain explicit human actions."""
from __future__ import annotations

import json
import time

from .contracts import DramaError, fields, identifier, revision, text
from .script_contracts import (KINDS, OUTPUT_BYTES, SCHEMA_VERSION, SOURCE_BYTES, TASK_KINDS,
                               TEMPLATE_VERSION, array, bounded_json, content_copy, fail,
                               local_id, number, obj, sha, string, unique)
from .restore import restore_guard
from .store import encode, new_id
from ..video_contracts import normalize_generation


class ScriptMixin:
    def init_scripts(self, text_adapter=None):
        self.text_adapter = text_adapter
        # A persisted intent is not evidence that a request was never dispatched.
        if not getattr(self, "_restore_mode", None):
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_text_jobs SET state='SUBMISSION_UNKNOWN',error=?,updated_at=? WHERE state NOT IN ('QUEUED','SUCCEEDED','FAILED','CANCELED','SUBMISSION_UNKNOWN','RECOVERY_REQUIRED')",
                           ("服务已重启；请按原任务核对，禁止自动重发", time.time()))

    def script_capabilities(self):
        available, reason = False, "尚未配置已验证的Gateway文本服务；可继续手写"
        if getattr(self, "_restore_mode", None):
            reason = "恢复工作区的模型任务保持隔离；可继续手写或显式查询原任务"
        elif self.text_adapter is not None:
            try:
                caps = self.text_adapter.capabilities()
                available = caps.get("generation_qualified") is True
                reason = caps.get("reason") or ("" if available else reason)
            except Exception:
                reason = "Gateway文本服务暂不可用；可继续手写"
        return {"storyEnabled": True, "textGenerationEnabled": available, "textGenerationReason": reason,
                "storyLimits": {"sourceBytes": SOURCE_BYTES, "contextCharacters": 32000, "outputBytes": OUTPUT_BYTES,
                                "jsonDepth": 32, "scenes": 64, "lines": 512, "beats": 128, "shots": 128,
                                "sourceIndexUnit": "unicode_codepoint", "schemaVersion": SCHEMA_VERSION, "maxPendingTextJobs": 8}}

    @staticmethod
    def _story_one(db, rid, kind=None, eid=None):
        row = db.execute("SELECT * FROM dr_story_revisions WHERE id=?", (identifier(rid),)).fetchone()
        if not row or (kind and row['kind'] != kind) or (eid and row['episode_id'] != eid):
            raise DramaError("story_not_found", "找不到当前分集的指定修订", 404)
        return dict(row)

    @staticmethod
    def _story_head(db, eid, kind):
        row = db.execute("SELECT * FROM dr_story_heads WHERE episode_id=? AND kind=?", (eid, kind)).fetchone()
        return {"revision": row['revision'] if row else 0,
                "currentRevisionId": row['current_revision_id'] if row else None,
                "approvedRevisionId": row['approved_revision_id'] if row else None}

    @staticmethod
    def _kind(kind, source=False):
        if kind not in (KINDS if source else KINDS[1:]):
            fail("kind", "不支持的故事阶段")

    def _source_span(self, db, eid, span, allowed, path):
        obj(span, {'sourceRevisionId', 'start', 'end', 'quoteSha256'}, path=path)
        if span['sourceRevisionId'] not in allowed:
            fail(path, "引用不属于冻结原文", "source_reference_invalid")
        row = self._story_one(db, span['sourceRevisionId'], 'source', eid)
        original = json.loads(row['content'])['text']
        number(span['start'], path+'.start', 0, len(original)-1)
        number(span['end'], path+'.end', span['start']+1, len(original))
        quote = original[span['start']:span['end']]
        if span['quoteSha256'] != sha(quote):
            fail(path, "原文字符区间与quote哈希不一致", "source_quote_mismatch")
        return quote

    def _source_refs(self, db, eid, content):
        ids = array(content['sourceRevisionIds'], 'sourceRevisionIds', 16, 1)
        for rid in ids:
            self._story_one(db, rid, 'source', eid)
        unique(ids, 'sourceRevisionIds')
        return ids

    def _entity_refs(self, db, pid, ids, *, current=False):
        array(ids, 'entityRevisionIds', 128)
        result = {}
        for rid in ids:
            r = self.store.one(db, 'dr_entity_revisions', identifier(rid))
            e = self.store.one(db, 'dr_entities', r['entity_id'])
            if e['project_id'] != pid:
                fail('entityRevisionIds', '实体不属于当前项目', 'entity_reference_invalid')
            if e['id'] in result:
                fail('entityRevisionIds', '同一实体不能引用多个修订')
            if current and e['current_revision_id'] != rid:
                fail('entityRevisionIds', '实体文字已有新修订，请重新核对', 'story_dependency_changed')
            result[e['id']] = {**e, 'revisionId': rid, 'content': json.loads(r['snapshot'])}
        return result

    def _validate_story(self, db, eid, kind, value, *, model=False, current=False):
        value = content_copy(value)
        ep = self.store.one(db, 'dr_episodes', identifier(eid))
        if kind == 'source':
            obj(value, {'title', 'text'})
            string(value['title'], 'title', 200)
            string(value['text'], 'text', OUTPUT_BYTES)
            if len(value['text'].encode('utf-8')) > SOURCE_BYTES:
                fail('text', '原文超过256KiB', 'source_limit')
            return value
        self._kind(kind)
        if value.get('schemaVersion') != SCHEMA_VERSION or type(value.get('schemaVersion')) is not int:
            fail('schemaVersion', '不支持的结构版本')
        if kind == 'storyboard':
            return self._validate_storyboard(db, eid, value, current=current)
        required = {'schemaVersion', 'sourceRevisionIds'}
        required |= {'candidates'} if kind == 'entities' else {'entityRevisionIds'}
        if kind == 'beats':
            required |= {'targetFrames', 'beats', 'endingState'}
        if kind == 'script':
            required |= {'beatRevisionId', 'scenes'}
        obj(value, required)
        source_ids = self._source_refs(db, eid, value)
        entities = self._entity_refs(db, ep['project_id'], value.get('entityRevisionIds', []), current=current)
        if kind == 'entities':
            ids = []
            for i, candidate in enumerate(array(value['candidates'], 'candidates', 128)):
                path = f'candidates[{i}]'
                obj(candidate, {'candidateId', 'kind', 'name', 'aliases', 'description', 'sourceSpans'}, path=path)
                ids.append(local_id(candidate['candidateId'], path+'.candidateId'))
                if candidate['kind'] not in ('character', 'location', 'costume', 'prop'):
                    fail(path+'.kind', '实体类型不受支持')
                string(candidate['name'], path+'.name', 160)
                string(candidate['description'], path+'.description', 8000, empty=True)
                for alias in array(candidate['aliases'], path+'.aliases', 32):
                    string(alias, path+'.aliases', 160)
                for span in array(candidate['sourceSpans'], path+'.sourceSpans', 64):
                    self._source_span(db, eid, span, source_ids, path+'.sourceSpans')
            unique(ids, 'candidates.candidateId')
        elif kind == 'beats':
            number(value['targetFrames'], 'targetFrames', 1, 86400)
            ids = []
            for i, beat in enumerate(array(value['beats'], 'beats', 128, 1)):
                path = f'beats[{i}]'
                obj(beat, {'beatId', 'title', 'description', 'sourceSpans'}, path=path)
                ids.append(local_id(beat['beatId'], path+'.beatId'))
                string(beat['title'], path+'.title', 200)
                string(beat['description'], path+'.description', 8000)
                for span in array(beat['sourceSpans'], path+'.sourceSpans', 64):
                    self._source_span(db, eid, span, source_ids, path+'.sourceSpans')
            unique(ids, 'beats.beatId')
            obj(value['endingState'], {'summary', 'unresolvedQuestions'}, path='endingState')
            string(value['endingState']['summary'], 'endingState.summary', 4000)
            for question in array(value['endingState']['unresolvedQuestions'], 'endingState.unresolvedQuestions', 64):
                string(question, 'endingState.unresolvedQuestions', 2000)
        else:
            beat = self._story_one(db, value['beatRevisionId'], 'beats', eid)
            self._require_approved(db, beat, current=current)
            beat_ids = {b['beatId'] for b in json.loads(beat['content'])['beats']}
            scene_ids, line_ids, covered_beats = [], [], set()
            for i, scene in enumerate(array(value['scenes'], 'scenes', 64, 1)):
                path = f'scenes[{i}]'
                obj(scene, {'sceneId', 'locationId', 'beatIds', 'actions', 'lines'}, path=path)
                scene_ids.append(local_id(scene['sceneId'], path+'.sceneId'))
                if not isinstance(scene['locationId'], str) or scene['locationId'] not in entities or entities[scene['locationId']]['kind'] != 'location':
                    fail(path+'.locationId', '请选择冻结列表中的地点', 'entity_reference_invalid')
                for bid in array(scene['beatIds'], path+'.beatIds', 128, 1):
                    if not isinstance(bid, str) or bid not in beat_ids:
                        fail(path+'.beatIds', '节拍来源不存在')
                    covered_beats.add(bid)
                unique(scene['beatIds'], path+'.beatIds')
                for action in array(scene['actions'], path+'.actions', 128):
                    string(action, path+'.actions', 4000)
                for j, line in enumerate(array(scene['lines'], path+'.lines', 512)):
                    lp = path+f'.lines[{j}]'
                    obj(line, {'lineId', 'speakerId', 'text', 'provenance'}, path=lp)
                    line_ids.append(local_id(line['lineId'], lp+'.lineId'))
                    if line['speakerId'] is not None and (not isinstance(line['speakerId'], str) or line['speakerId'] not in entities or entities[line['speakerId']]['kind'] != 'character'):
                        fail(lp+'.speakerId', '说话者不是冻结列表中的人物', 'entity_reference_invalid')
                    string(line['text'], lp+'.text', 2000)
                    provenance = line['provenance']
                    obj(provenance, {'kind'}, {'span'}, path=lp+'.provenance')
                    if provenance['kind'] not in ('source', 'user_authored', 'model_authored') or (model and provenance['kind'] == 'user_authored'):
                        fail(lp+'.provenance', '台词来源类型不真实', 'dialogue_provenance_invalid')
                    if provenance['kind'] == 'source':
                        if 'span' not in provenance or self._source_span(db, eid, provenance['span'], source_ids, lp+'.provenance.span') != line['text']:
                            fail(lp+'.provenance', '台词与原文引用不同', 'dialogue_text_mismatch')
                    elif 'span' in provenance:
                        fail(lp+'.provenance', '创作台词不能伪称原文引用')
            unique(scene_ids, 'scenes.sceneId')
            unique(line_ids, 'scenes.lines.lineId')
            if len(line_ids) > 512:
                fail('scenes.lines', '台词总量超过512')
            if covered_beats != beat_ids:
                fail('scenes.beatIds', '剧本遗漏已批准节拍', 'beat_coverage_missing')
        return value

    def _require_approved(self, db, row, *, current=True):
        head = self._story_head(db, row['episode_id'], row['kind'])
        if not db.execute('SELECT 1 FROM dr_story_approvals WHERE revision_id=?', (row['id'],)).fetchone():
            fail(row['kind'], '请先人工批准上游修订', 'story_not_approved')
        if current and (head['approvedRevisionId'] != row['id'] or head['currentRevisionId'] != row['id']):
            fail(row['kind'], '上游已有新修订，请先核对并批准', 'story_dependency_changed')
        if current:
            self._check_story_dependencies(db, row)

    def _validate_storyboard(self, db, eid, value, *, current=False):
        obj(value, {'schemaVersion', 'scriptRevisionId', 'shots', 'omittedLines'})
        script = self._story_one(db, value['scriptRevisionId'], 'script', eid)
        self._require_approved(db, script, current=current)
        content = json.loads(script['content'])
        scenes = {s['sceneId']: s for s in content['scenes']}
        lines = {l['lineId']: (s['sceneId'], l) for s in content['scenes'] for l in s['lines']}
        ids, usages = [], {}
        for i, shot in enumerate(array(value['shots'], 'shots', 128, 1)):
            path = f'shots[{i}]'
            obj(shot, {'candidateId', 'sceneId', 'title', 'action', 'framing', 'camera', 'prompt', 'targetEditFrames', 'generation', 'dialogue'}, path=path)
            ids.append(local_id(shot['candidateId'], path+'.candidateId'))
            if not isinstance(shot['sceneId'], str) or shot['sceneId'] not in scenes:
                fail(path+'.sceneId', '场景不存在', 'dialogue_source_missing')
            for key, maximum in [('title', 160), ('action', 4000), ('framing', 120), ('camera', 120), ('prompt', 8000)]:
                string(shot[key], path+'.'+key, maximum, empty=key != 'title')
            try:
                generation = normalize_generation(shot['generation'])
            except (ValueError, TypeError):
                fail(path+'.generation', '不属于已发布P4生成合同', 'invalid_generation')
            number(shot['targetEditFrames'], path+'.targetEditFrames', 1, generation['requestDurationSeconds'] * 24)
            for j, use in enumerate(array(shot['dialogue'], path+'.dialogue', 512)):
                lp = path+f'.dialogue[{j}]'
                obj(use, {'lineId', 'speakerId', 'text'}, {'range', 'continuityId'}, path=lp)
                if not isinstance(use['lineId'], str) or use['lineId'] not in lines or lines[use['lineId']][0] != shot['sceneId']:
                    fail(lp+'.lineId', '台词不属于指定剧本与场景', 'dialogue_source_missing')
                line = lines[use['lineId']][1]
                if use['text'] != line['text'] or use['speakerId'] != line['speakerId']:
                    fail(lp, '台词全文或说话者与已批准版本不同', 'dialogue_text_mismatch')
                if ('range' in use) != ('continuityId' in use):
                    fail(lp, '拆句必须同时声明range与continuityId')
                if 'range' in use:
                    obj(use['range'], {'start', 'end'}, path=lp+'.range')
                    number(use['range']['start'], lp+'.range.start', 0, len(line['text'])-1)
                    number(use['range']['end'], lp+'.range.end', use['range']['start']+1, len(line['text']))
                    local_id(use['continuityId'], lp+'.continuityId')
                usages.setdefault(use['lineId'], []).append(use)
        unique(ids, 'shots.candidateId')
        for lid, uses in usages.items():
            if len(uses) == 1 and 'range' not in uses[0]:
                continue
            end, continuity = 0, uses[0].get('continuityId')
            for use in uses:
                if 'range' not in use or use.get('continuityId') != continuity or use['range']['start'] != end:
                    fail('shots.dialogue', '对白重复或连续区间不相接', 'dialogue_repeated')
                end = use['range']['end']
            if end != len(lines[lid][1]['text']):
                fail('shots.dialogue', '连续区间未覆盖完整台词', 'dialogue_coverage_missing')
        omitted = []
        for row in array(value['omittedLines'], 'omittedLines', 512):
            obj(row, {'lineId', 'reason'}, path='omittedLines')
            if not isinstance(row['lineId'], str) or row['lineId'] not in lines or row['lineId'] in usages:
                fail('omittedLines', '台词不存在或已经分配')
            string(row['reason'], 'omittedLines.reason', 2000)
            omitted.append(row['lineId'])
        unique(omitted, 'omittedLines')
        if set(usages) | set(omitted) != set(lines):
            fail('shots.dialogue', '未分配台词须明确注明省略原因', 'dialogue_coverage_missing')
        return value

    def _story_dependencies(self, db, eid, value):
        return {'sourceHeadId': self._story_head(db, eid, 'source')['currentRevisionId'],
                'entityRevisionIds': value.get('entityRevisionIds', []),
                'beatRevisionId': value.get('beatRevisionId'), 'scriptRevisionId': value.get('scriptRevisionId')}

    def _check_story_dependencies(self, db, row):
        deps = json.loads(row['dependencies'])
        if row['kind'] != 'source' and deps.get('sourceHeadId') != self._story_head(db, row['episode_id'], 'source')['currentRevisionId']:
            fail('sourceRevisionIds', '原文已更新，请重新核对另存', 'story_dependency_changed')
        ep = self.store.one(db, 'dr_episodes', row['episode_id'])
        self._entity_refs(db, ep['project_id'], deps.get('entityRevisionIds', []), current=True)
        for field, kind in [('beatRevisionId', 'beats'), ('scriptRevisionId', 'script')]:
            if deps.get(field):
                upstream = self._story_one(db, deps[field], kind, row['episode_id'])
                self._require_approved(db, upstream, current=True)

    def _story_view(self, db, row):
        validation = {'valid': True, 'errors': []}
        try:
            self._check_story_dependencies(db, row)
        except DramaError as exc:
            validation = self._story_error(exc)
        return {'id': row['id'], 'kind': row['kind'], 'revision': row['revision'], 'content': json.loads(row['content']),
                'contentHash': row['content_hash'], 'createdAt': row['created_at'],
                'approved': bool(db.execute('SELECT 1 FROM dr_story_approvals WHERE revision_id=?', (row['id'],)).fetchone()),
                'needsReview': not validation['valid'], 'validation': validation}

    @staticmethod
    def _story_error(exc):
        return {'valid': False, 'errors': getattr(exc, 'errors', [{'path': 'content', 'code': exc.code, 'message': exc.message}])}

    @staticmethod
    def _draft_view(row):
        return {'id': row['id'], 'kind': row['kind'], 'revision': row['revision'], 'rawText': row['raw_text'],
                'rawHash': row['raw_hash'], 'validation': json.loads(row['validation']), 'attemptId': row['attempt_id']}

    def story_state(self, eid):
        with self.store.connect() as db:
            self.store.one(db, 'dr_episodes', identifier(eid))
            return {'episodeId': eid, 'heads': {k: self._story_head(db, eid, k) for k in KINDS},
                    'revisions': {k: [self._story_view(db, r) for r in db.execute('SELECT * FROM dr_story_revisions WHERE episode_id=? AND kind=? ORDER BY revision DESC', (eid, k))] for k in KINDS},
                    'editingDrafts': {k: [self._draft_view(r) for r in db.execute('SELECT * FROM dr_story_drafts WHERE episode_id=? AND kind=? ORDER BY revision DESC', (eid, k))] for k in KINDS[1:]},
                    'entityMappings': [dict(r) for r in db.execute('SELECT * FROM dr_entity_proposal_mappings WHERE episode_id=?', (eid,))],
                    'textJobs': [self._text_job_view(db, r) for r in db.execute('SELECT * FROM dr_text_jobs WHERE episode_id=? ORDER BY created_at DESC', (eid,))]}

    def _append_story(self, db, eid, kind, content):
        head = self._story_head(db, eid, kind)
        version = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM dr_story_revisions WHERE episode_id=? AND kind=?', (eid, kind)).fetchone()[0]
        rid = new_id()
        encoded = encode(content)
        db.execute('INSERT INTO dr_story_revisions VALUES(?,?,?,?,?,?,?,?)',
                   (rid, eid, kind, version, encoded, sha(encoded), encode(self._story_dependencies(db, eid, content)), time.time()))
        db.execute('INSERT INTO dr_story_heads VALUES(?,?,?,?,?) ON CONFLICT(episode_id,kind) DO UPDATE SET revision=excluded.revision,current_revision_id=excluded.current_revision_id',
                   (eid, kind, head['revision']+1, rid, head['approvedRevisionId']))
        return self._story_one(db, rid)

    def save_source(self, eid, body):
        fields(body, {'title', 'text', 'expectedRevision', 'requestKey'}, {'title', 'text', 'expectedRevision', 'requestKey'})
        def save(db):
            self.store.one(db, 'dr_episodes', identifier(eid))
            head = self._story_head(db, eid, 'source')
            revision(head['revision'], body['expectedRevision'])
            value = self._validate_story(db, eid, 'source', {k: body[k] for k in ('title', 'text')})
            old = self._story_one(db, head['currentRevisionId']) if head['currentRevisionId'] else None
            reused = bool(old and json.loads(old['content']) == value)
            row = old if reused else self._append_story(db, eid, 'source', value)
            result = self._story_view(db, row)
            result['textSha256'] = sha(value['text'])
            result['indexUnit'] = 'unicode_codepoint'
            return {'revision': result, 'head': self._story_head(db, eid, 'source'), 'reused': reused}
        return self.store.mutate(f'story-source:{eid}', body['requestKey'], body, save)

    def save_story_revision(self, eid, kind, body):
        self._kind(kind)
        fields(body, {'content', 'expectedRevision', 'requestKey'}, {'content', 'expectedRevision', 'requestKey'})
        def save(db):
            revision(self._story_head(db, eid, kind)['revision'], body['expectedRevision'])
            value = self._validate_story(db, eid, kind, body['content'], current=True)
            if kind == 'script':
                head = self._story_head(db, eid, kind)
                previous = json.loads(self._story_one(db, head['currentRevisionId'])['content']) if head['currentRevisionId'] else {'scenes': []}
                old_lines = {l['lineId']: l for s in previous['scenes'] for l in s['lines']}
                model_lines = []
                for draft in db.execute("SELECT raw_text,validation FROM dr_story_drafts WHERE episode_id=? AND kind='script' AND attempt_id IS NOT NULL", (eid,)):
                    if json.loads(draft['validation'])['valid']:
                        model_lines.extend(l for scene in json.loads(draft['raw_text'])['scenes'] for l in scene['lines'])
                for scene in value['scenes']:
                    for line in scene['lines']:
                        old = old_lines.get(line['lineId'])
                        known_model = any(l['lineId'] == line['lineId'] and l['text'] == line['text'] and l['speakerId'] == line['speakerId'] and l['provenance']['kind'] == 'model_authored' for l in model_lines)
                        if line['provenance']['kind'] == 'model_authored' and not known_model and (old is None or old['provenance']['kind'] != 'model_authored' or old['text'] != line['text'] or old['speakerId'] != line['speakerId']):
                            line['provenance'] = {'kind': 'user_authored'}
            row = self._append_story(db, eid, kind, value)
            return {'revision': self._story_view(db, row), 'head': self._story_head(db, eid, kind)}
        return self.store.mutate(f'story-{kind}:{eid}', body['requestKey'], body, save)

    def _validate_raw(self, db, eid, kind, raw, *, model=False):
        try:
            self._validate_story(db, eid, kind, bounded_json(raw), model=model, current=True)
            return {'valid': True, 'errors': []}
        except DramaError as exc:
            return self._story_error(exc)

    def _append_story_draft(self, db, eid, kind, raw, validation, attempt=None):
        version = db.execute('SELECT COALESCE(MAX(revision),0)+1 FROM dr_story_drafts WHERE episode_id=? AND kind=?', (eid, kind)).fetchone()[0]
        did = new_id()
        db.execute('INSERT INTO dr_story_drafts VALUES(?,?,?,?,?,?,?,?,?)', (did, eid, kind, version, raw, sha(raw), encode(validation), attempt, time.time()))
        return self._draft_view(db.execute('SELECT * FROM dr_story_drafts WHERE id=?', (did,)).fetchone())

    def save_story_draft(self, eid, kind, body):
        self._kind(kind)
        fields(body, {'rawText', 'expectedRevision', 'requestKey'}, {'rawText', 'expectedRevision', 'requestKey'})
        raw = string(body['rawText'], 'rawText', OUTPUT_BYTES, empty=True)
        if len(raw.encode('utf-8')) > OUTPUT_BYTES:
            fail('rawText', '草稿超过2MiB', 'story_output_limit')
        def save(db):
            self.store.one(db, 'dr_episodes', identifier(eid))
            actual = db.execute('SELECT COALESCE(MAX(revision),0) FROM dr_story_drafts WHERE episode_id=? AND kind=?', (eid, kind)).fetchone()[0]
            revision(actual, body['expectedRevision'])
            return {'draft': self._append_story_draft(db, eid, kind, raw, self._validate_raw(db, eid, kind, raw))}
        return self.store.mutate(f'story-draft-{kind}:{eid}', body['requestKey'], body, save)

    def approve_story_revision(self, rid, body):
        fields(body, {'expectedRevision', 'requestKey'}, {'expectedRevision', 'requestKey'})
        def save(db):
            row = self._story_one(db, rid)
            if row['kind'] not in ('beats', 'script'):
                fail('kind', '仅节拍和剧本使用此批准动作')
            head = self._story_head(db, row['episode_id'], row['kind'])
            revision(head['revision'], body['expectedRevision'])
            if head['currentRevisionId'] != rid:
                fail('revisionId', '只能批准当前修订', 'story_dependency_changed')
            self._check_story_dependencies(db, row)
            self._validate_story(db, row['episode_id'], row['kind'], json.loads(row['content']), current=True)
            affected = []
            if head['approvedRevisionId'] != rid:
                db.execute('INSERT OR IGNORE INTO dr_story_approvals VALUES(?,?,?)', (new_id(), rid, time.time()))
                db.execute('UPDATE dr_story_heads SET approved_revision_id=?,revision=revision+1 WHERE episode_id=? AND kind=?', (rid, row['episode_id'], row['kind']))
                if row['kind'] == 'script':
                    for dep in db.execute('SELECT d.*,s.id AS sid FROM dr_script_dependencies d JOIN dr_shots s ON s.current_content_revision_id=d.shot_revision_id WHERE s.episode_id=? AND d.script_revision_id<>?', (row['episode_id'], rid)).fetchall():
                        db.execute('INSERT OR IGNORE INTO dr_script_invalidations VALUES(?,?,?,?,?,NULL,NULL,?)',
                                   (new_id(), dep['shot_revision_id'], dep['script_revision_id'], rid, '已批准剧本更新，请核对镜头对白与情节', time.time()))
                        affected.append(dep['sid'])
            return {'revision': self._story_view(db, row), 'head': self._story_head(db, row['episode_id'], row['kind']), 'affectedShotIds': affected}
        return self.store.mutate(f'story-approve:{rid}', body['requestKey'], body, save)

    def confirm_entity_candidate(self, rid, body):
        fields(body, {'candidateId', 'expectedRevision', 'requestKey', 'mergeEntityId', 'expectedEntityRevision'}, {'candidateId', 'expectedRevision', 'requestKey'})
        def save(db):
            row = self._story_one(db, rid, 'entities')
            ep = self.store.one(db, 'dr_episodes', row['episode_id'])
            head = self._story_head(db, row['episode_id'], 'entities')
            revision(head['revision'], body['expectedRevision'])
            if head['currentRevisionId'] != rid:
                fail('revisionId', '候选已有新修订', 'story_dependency_changed')
            self._check_story_dependencies(db, row)
            value = self._validate_story(db, row['episode_id'], 'entities', json.loads(row['content']), current=True)
            candidate = next((c for c in value['candidates'] if c['candidateId'] == body['candidateId']), None)
            if candidate is None:
                fail('candidateId', '候选不存在')
            existing = db.execute('SELECT * FROM dr_entity_proposal_mappings WHERE episode_id=? AND candidate_id=?', (row['episode_id'], body['candidateId'])).fetchone()
            if existing:
                if existing['proposal_revision_id'] != rid or (body.get('mergeEntityId') and existing['entity_id'] != body['mergeEntityId']):
                    fail('candidateId', '此候选已经确认，修改需新候选ID或直接编辑实体', 'candidate_already_confirmed')
                return {'entity': self._entity_view(db, self.store.one(db, 'dr_entities', existing['entity_id'])), 'mapping': dict(existing)}
            if body.get('mergeEntityId'):
                entity = self.store.one(db, 'dr_entities', identifier(body['mergeEntityId']))
                if entity['project_id'] != ep['project_id'] or entity['kind'] != candidate['kind']:
                    fail('mergeEntityId', '实体类型或项目不匹配', 'entity_reference_invalid')
                revision(entity['revision'], body.get('expectedEntityRevision'))
                prior = json.loads(self.store.one(db, 'dr_entity_revisions', entity['current_revision_id'])['snapshot'])
                description = '\n'.join(v for v in (prior['description'], candidate['description']) if v)
                snapshot = self._entity_snapshot(db, ep['project_id'], entity['kind'], {**prior, 'description': description})
                eid = entity['id']
                entity_rid = self._append_entity(db, eid, snapshot)
                db.execute('UPDATE dr_entities SET revision=revision+1 WHERE id=?', (eid,))
            else:
                if 'expectedEntityRevision' in body:
                    fail('expectedEntityRevision', '创建实体不接受合并版本')
                eid = new_id()
                snapshot = self._entity_snapshot(db, ep['project_id'], candidate['kind'], {'name': candidate['name'], 'description': candidate['description']})
                db.execute('INSERT INTO dr_entities(id,project_id,kind,created_at) VALUES(?,?,?,?)', (eid, ep['project_id'], candidate['kind'], time.time()))
                entity_rid = self._append_entity(db, eid, snapshot)
            sources = {'aliases': candidate['aliases'], 'sourceSpans': candidate['sourceSpans']}
            db.execute('INSERT INTO dr_entity_proposal_mappings VALUES(?,?,?,?,?,?,?)',
                       (row['episode_id'], candidate['candidateId'], rid, eid, entity_rid, encode(sources), time.time()))
            mapping = dict(db.execute('SELECT * FROM dr_entity_proposal_mappings WHERE episode_id=? AND candidate_id=?', (row['episode_id'], candidate['candidateId'])).fetchone())
            return {'entity': self._entity_view(db, self.store.one(db, 'dr_entities', eid)), 'mapping': mapping}
        return self.store.mutate(f'story-confirm:{rid}', body['requestKey'], body, save)

    def import_storyboard(self, rid, body):
        fields(body, {'candidateIds', 'expectedRevision', 'requestKey'}, {'candidateIds', 'expectedRevision', 'requestKey'})
        ids = array(body['candidateIds'], 'candidateIds', 128, 1)
        for cid in ids:
            local_id(cid, 'candidateIds')
        unique(ids, 'candidateIds')
        def save(db):
            row = self._story_one(db, rid, 'storyboard')
            head = self._story_head(db, row['episode_id'], 'storyboard')
            revision(head['revision'], body['expectedRevision'])
            if head['currentRevisionId'] != rid:
                fail('revisionId', '分镜提案已更新', 'story_dependency_changed')
            self._check_story_dependencies(db, row)
            proposal = self._validate_story(db, row['episode_id'], 'storyboard', json.loads(row['content']), current=True)
            chosen = [s for s in proposal['shots'] if s['candidateId'] in ids]
            if len(chosen) != len(ids):
                fail('candidateIds', '选择包含不存在的候选')
            for shot in chosen:
                if db.execute('SELECT 1 FROM dr_storyboard_mappings WHERE episode_id=? AND candidate_id=?', (row['episode_id'], shot['candidateId'])).fetchone():
                    fail('candidateIds', '候选已经导入；重新拆镜须使用新的候选ID', 'candidate_already_imported')
            script = json.loads(self._story_one(db, proposal['scriptRevisionId'])['content'])
            ep = self.store.one(db, 'dr_episodes', row['episode_id'])
            entities = self._entity_refs(db, ep['project_id'], script['entityRevisionIds'], current=True)
            scenes = {s['sceneId']: s for s in script['scenes']}
            ordinal = db.execute('SELECT COALESCE(MAX(ordinal),0) FROM dr_shots WHERE episode_id=?', (row['episode_id'],)).fetchone()[0]
            shots, mappings = [], []
            for index, candidate in enumerate(chosen, 1):
                sid = new_id()
                db.execute('INSERT INTO dr_shots(id,episode_id,title,ordinal,revision,created_at) VALUES(?,?,?,?,0,?)', (sid, row['episode_id'], candidate['title'], ordinal+index, time.time()))
                scene = scenes[candidate['sceneId']]
                characters = sorted({line['speakerId'] for line in scene['lines'] if line['speakerId']})
                used = characters + [scene['locationId']]
                snapshot = self._default_snapshot({'title': candidate['title']}, {k: candidate[k] for k in ('title', 'action', 'framing', 'camera', 'prompt', 'generation', 'targetEditFrames')})
                snapshot['characterRevisionIds'] = [entities[e]['revisionId'] for e in characters]
                snapshot['locationRevisionId'] = entities[scene['locationId']]['revisionId']
                srid = self._append_shot(db, sid, snapshot, [(entities[e]['revisionId'], entities[e]['kind']) for e in used])
                db.execute('INSERT INTO dr_script_dependencies VALUES(?,?,?,?)', (srid, proposal['scriptRevisionId'], candidate['sceneId'], encode(candidate['dialogue'])))
                db.execute('INSERT INTO dr_storyboard_mappings VALUES(?,?,?,?,?,?)', (row['episode_id'], candidate['candidateId'], rid, sid, srid, time.time()))
                shots.append(self._shot_view(db, self.store.one(db, 'dr_shots', sid)))
                mappings.append({'candidateId': candidate['candidateId'], 'shotId': sid, 'shotRevisionId': srid})
            db.execute('UPDATE dr_episodes SET revision=revision+1 WHERE id=?', (row['episode_id'],))
            return {'shots': shots, 'mappings': mappings}
        return self.store.mutate(f'story-import:{rid}', body['requestKey'], body, save)

    def carry_script_dependency(self, db, sid, new_rid):
        old = db.execute('SELECT d.* FROM dr_script_dependencies d JOIN dr_shot_revisions r ON r.id=d.shot_revision_id WHERE r.shot_id=? AND r.id<>? ORDER BY r.revision DESC LIMIT 1', (sid, new_rid)).fetchone()
        if old is None:
            return
        db.execute('INSERT OR IGNORE INTO dr_script_dependencies VALUES(?,?,?,?)', (new_rid, old['script_revision_id'], old['scene_id'], old['dialogue']))
        for row in db.execute('SELECT * FROM dr_script_invalidations WHERE shot_revision_id=?', (old['shot_revision_id'],)).fetchall():
            db.execute('INSERT OR IGNORE INTO dr_script_invalidations VALUES(?,?,?,?,?,?,?,?)',
                       (new_id(), new_rid, row['old_revision_id'], row['new_revision_id'], row['reason'], row['resolved_at'], row['resolution'], row['created_at']))

    @staticmethod
    def script_dependency_state(db, rid):
        return [{'id': r['id'], 'kind': 'script', 'oldRevisionId': r['old_revision_id'], 'newRevisionId': r['new_revision_id'],
                 'reason': r['reason'], 'resolved': r['resolved_at'] is not None, 'resolution': r['resolution']}
                for r in db.execute('SELECT * FROM dr_script_invalidations WHERE shot_revision_id=? AND resolved_at IS NULL ORDER BY created_at', (rid,))]

    def script_invalidations(self, sid):
        with self.store.connect() as db:
            shot = self.store.one(db, 'dr_shots', identifier(sid))
            rid = shot['current_content_revision_id']
            dep = db.execute('SELECT * FROM dr_script_dependencies WHERE shot_revision_id=?', (rid,)).fetchone()
            return {'shotId': sid, 'revision': shot['revision'], 'dependency': dict(dep) if dep else None,
                    'invalidations': self.script_dependency_state(db, rid)}

    def review_script_invalidations(self, sid, body):
        fields(body, {'expectedRevision', 'invalidationIds', 'reason', 'requestKey'}, {'expectedRevision', 'invalidationIds', 'reason', 'requestKey'})
        ids = array(body['invalidationIds'], 'invalidationIds', 128, 1)
        for iid in ids:
            identifier(iid)
        unique(ids, 'invalidationIds')
        reason = body['reason']
        if not isinstance(reason, str) or len(reason) > 2000:
            raise DramaError('invalid_review', '复核意见须为2000字以内文本')
        def save(db):
            shot = self.store.one(db, 'dr_shots', identifier(sid))
            revision(shot['revision'], body['expectedRevision'])
            for iid in ids:
                row = db.execute('SELECT * FROM dr_script_invalidations WHERE id=?', (iid,)).fetchone()
                if not row or row['shot_revision_id'] != shot['current_content_revision_id'] or row['resolved_at'] is not None:
                    fail('invalidationIds', '失效记录不属于当前镜头或已经处理', 'wrong_invalidation')
                db.execute('UPDATE dr_script_invalidations SET resolved_at=?,resolution=? WHERE id=?', (time.time(), reason, iid))
            db.execute('UPDATE dr_shots SET revision=revision+1 WHERE id=?', (sid,))
            db.execute('INSERT INTO dr_events VALUES(?,?,?,?,?)', (new_id(), 'script-review:'+sid, 'retain_frozen_script', encode({'invalidationIds': ids, 'reason': reason}), time.time()))
            return {'shotId': sid, 'revision': shot['revision']+1, 'invalidations': self.script_dependency_state(db, shot['current_content_revision_id'])}
        return self.store.mutate(f'script-review:{sid}', body['requestKey'], body, save)

    @staticmethod
    def _text_job_row(db, jid):
        row = db.execute('SELECT * FROM dr_text_jobs WHERE id=?', (identifier(jid),)).fetchone()
        if row is None:
            raise DramaError('not_found', '找不到文本任务', 404)
        return dict(row)

    @staticmethod
    def _text_job_view(db, row):
        payload = json.loads(row['payload'])
        input_revisions = {'sourceRevisionIds': [s['revisionId'] for s in payload['sources']],
                           'entityRevisionIds': [e['revisionId'] for e in payload['entities']]}
        for field in ('beatRevisionId', 'scriptRevisionId', 'shotRevisionId', 'imageAssetId', 'actionImageAssetId', 'videoAssetId', 'audioAssetId', 'audioDescription', 'templateVersion', 'soundMode', 'dialogue'):
            if field in payload:
                input_revisions[field] = payload[field]
        attempts = []
        for a in db.execute('SELECT * FROM dr_text_attempts WHERE job_id=? ORDER BY epoch', (row['id'],)):
            attempts.append({'id': a['id'], 'epoch': a['epoch'], 'dispatchState': a['dispatch_state'],
                             'payloadHash': a['payload_hash'], 'rawText': a['raw_text'], 'rawHash': a['raw_hash'],
                             'validation': json.loads(a['validation']) if a['validation'] else None})
        view = {'id': row['id'], 'episodeId': row['episode_id'], 'kind': row['kind'], 'state': row['state'],
                'requestKey': row['request_key'], 'inputHash': row['input_hash'], 'outputStatus': row['output_status'],
                'resourceStatus': row['resource_status'], 'gatewayId': row['gateway_id'], 'currentAttemptId': row['current_attempt_id'],
                'error': row['error'], 'repairOf': row['repair_of'], 'attempts': attempts, 'inputRevisions': input_revisions}
        if row['kind'] == 'draft_video_prompt':
            view['promptInput'] = {field: payload[field] for field in ('shotRevisionId', 'brief', 'soundMode', 'dialogue') if field in payload}
            repaired = db.execute('SELECT id FROM dr_text_jobs WHERE repair_of=?', (row['id'],)).fetchone()
            view['repairJobId'] = repaired[0] if repaired else None
        if row['kind'] == 'draft_video_prompt' and row['output_status'] == 'valid_draft' and attempts:
            from .prompts import prepare_video_prompt
            try:
                view['compiledPrompt'], view['normalizationNotes'] = prepare_video_prompt(json.loads(attempts[-1]['rawText']), payload)
            except (ValueError, TypeError):
                view['compiledPrompt'] = None
        return view

    def text_job(self, jid):
        with self.store.connect() as db:
            return {'job': self._text_job_view(db, self._text_job_row(db, jid))}

    def find_text_job(self, eid, key):
        with self.store.connect() as db:
            self.store.one(db, 'dr_episodes', identifier(eid))
            row = db.execute('SELECT * FROM dr_text_jobs WHERE episode_id=? AND request_key=?', (eid, text(key, 'requestKey', 128))).fetchone()
            return {'job': self._text_job_view(db, row) if row else None}

    def _text_payload(self, db, eid, body):
        ep = self.store.one(db, 'dr_episodes', identifier(eid))
        kind = body['kind']
        if not isinstance(kind, str) or kind not in TASK_KINDS:
            fail('kind', '不支持的文本任务')
        if kind == 'draft_video_prompt':
            if body.get('sourceRevisionIds') != [] or any(k in body for k in ('entityRevisionIds','beatRevisionId','scriptRevisionId')): fail('kind', '视频提示词任务只接受当前镜头输入')
            from ..h3_prompt import VERSION
            context = self.validate_reference_inputs('shot', identifier(body.get('shotRevisionId')), db=db)
            shot = self.store.one(db, 'dr_shots', context['targetId'])
            if shot['episode_id'] != eid: fail('shotRevisionId', '镜头不属于当前分集')
            mode = body.get('soundMode', 'silent')
            if mode not in ('native', 'silent', 'reference'): fail('soundMode', '声音模式无效')
            dialogue = body.get('dialogue', '')
            if not isinstance(dialogue, str) or len(dialogue) > 500: fail('dialogue', '台词超过500字符')
            if mode == 'native' and not dialogue.strip(): fail('dialogue', '原生对白必须提供准确台词')
            snapshot = context['snapshot']
            references = {
                'imageAssetId': context.get('approvedReferenceAssetId') or '',
                'actionImageAssetId': snapshot.get('actionReferenceAssetId') or '',
                'videoAssetId': snapshot.get('videoReferenceAssetId') or '',
                'audioAssetId': snapshot.get('audioReferenceAssetId') or '',
            }
            if not any(references.values()): fail('shotRevisionId', '请先保存至少一种参考素材，纯文字镜头可直接手写提示词')
            return {'schemaVersion': 1, 'templateVersion': VERSION, 'taskKind': kind, 'episodeId': eid,
                    'sources': [], 'entities': [], 'shotRevisionId': body['shotRevisionId'],
                    **references, 'audioDescription': snapshot.get('audioDescription', ''), 'snapshot': snapshot,
                    'brief': text(body.get('brief', ''), 'brief', 3000), 'soundMode': mode, 'dialogue': dialogue,
                    'requestDurationSeconds': normalize_generation(context['snapshot'].get('generation'))['requestDurationSeconds']}
        if any(k in body for k in ('shotRevisionId','brief','soundMode','dialogue')): fail('kind', '故事任务不接受视频提示词字段')
        source_ids = self._source_refs(db, eid, body)
        entities = self._entity_refs(db, ep['project_id'], body.get('entityRevisionIds', []), current=True)
        inputs = {'schemaVersion': SCHEMA_VERSION, 'templateVersion': TEMPLATE_VERSION, 'episodeId': eid,
                  'sources': [{'revisionId': rid, **json.loads(self._story_one(db, rid, 'source', eid)['content'])} for rid in source_ids],
                  'entities': [{'entityId': k, 'revisionId': v['revisionId'], 'kind': v['kind'], 'name': v['content']['name'], 'description': v['content']['description']} for k, v in entities.items()],
                  'sourceHeadId': self._story_head(db, eid, 'source')['currentRevisionId']}
        if kind in ('draft_script', 'draft_storyboard'):
            upstream_kind = 'beats' if kind == 'draft_script' else 'script'
            field = 'beatRevisionId' if kind == 'draft_script' else 'scriptRevisionId'
            upstream = self._story_one(db, body.get(field), upstream_kind, eid)
            self._require_approved(db, upstream, current=True)
            inputs[field] = upstream['id']
            inputs[upstream_kind] = json.loads(upstream['content'])
            if set(inputs[upstream_kind].get('sourceRevisionIds', [])) != set(source_ids) or set(inputs[upstream_kind].get('entityRevisionIds', [])) != set(body.get('entityRevisionIds', [])):
                fail(field, '任务输入须与批准上游的原文和实体修订一致', 'story_dependency_changed')
        allowed_upstream = {'draft_script': 'beatRevisionId', 'draft_storyboard': 'scriptRevisionId'}.get(kind)
        for field in ('beatRevisionId', 'scriptRevisionId'):
            if field in body and field != allowed_upstream:
                fail(field, '此任务类型不接受该上游字段')
        inputs['taskKind'] = kind
        if len(encode(inputs)) > 32000:
            fail('sources', '任务上下文超过32000字符保守预算，请人工缩小原文片段', 'text_context_limit')
        return inputs

    def create_text_job(self, eid, body):
        restore_guard(self, "write")
        fields(body, {'kind', 'sourceRevisionIds', 'entityRevisionIds', 'beatRevisionId', 'scriptRevisionId', 'repairOf', 'requestKey', 'shotRevisionId', 'brief', 'soundMode', 'dialogue'}, {'kind', 'sourceRevisionIds', 'requestKey'})
        # Same-key recovery precedes current capability/admission checks.
        existing = self.find_text_job(eid, body['requestKey'])['job']
        if existing is None and not self.script_capabilities()['textGenerationEnabled']:
            raise DramaError('text_unavailable', self.script_capabilities()['textGenerationReason'], 503)
        def save(db):
            pending = db.execute("SELECT COUNT(*) FROM dr_text_jobs WHERE state NOT IN ('SUCCEEDED','FAILED','CANCELED')").fetchone()[0]
            if pending >= 8:
                raise DramaError('text_queue_limit', '最多保留8个未结束文本任务，请先核对或处理已有任务', 409)
            payload = self._text_payload(db, eid, body)
            repair = body.get('repairOf')
            if repair:
                parent = self._text_job_row(db, repair)
                if parent['episode_id'] != eid or parent['kind'] != body['kind'] or parent['repair_of']:
                    fail('repairOf', '修复链不属于同一分集或已经修复过')
                if parent['state'] in ('SUBMISSION_UNKNOWN', 'RECOVERY_REQUIRED') or parent['output_status'] != 'invalid_draft' or parent['resource_status'] not in ('released', 'not_acquired'):
                    fail('repairOf', '只能显式修复已结束且已知结果无效的任务', 'text_repair_not_allowed')
                if db.execute('SELECT 1 FROM dr_text_jobs WHERE repair_of=?', (repair,)).fetchone():
                    fail('repairOf', '每个任务最多一次结构修复', 'text_repair_limit')
                if payload != json.loads(parent['payload']):
                    fail('repairOf', '修复必须沿用原任务的全部冻结输入', 'text_repair_input_changed')
                previous = db.execute('SELECT raw_text FROM dr_text_attempts WHERE id=?', (parent['current_attempt_id'],)).fetchone()
                payload['repair'] = {'jobId': repair, 'rawText': previous['raw_text'] if previous else ''}
                if len(encode(payload)) > 32000:
                    fail('repairOf', '修复上下文超过32000字符，请手动编辑', 'text_context_limit')
            jid, now = new_id(), time.time()
            raw = encode(payload)
            db.execute('INSERT INTO dr_text_jobs(id,episode_id,kind,request_key,input_hash,payload,state,repair_of,created_at,updated_at) VALUES(?,?,?,?,?,?,?, ?,?,?)',
                       (jid, eid, body['kind'], body['requestKey'], sha(raw), raw, 'QUEUED', repair, now, now))
            return {'job': self._text_job_view(db, self._text_job_row(db, jid))}
        return self.store.mutate(f'text-jobs:{eid}', body['requestKey'], body, save)

    def dispatch_text_job(self, jid):
        restore_guard(self, "write")
        with self.store.connect(write=True) as db:
            row = self._text_job_row(db, jid)
            if row['state'] != 'QUEUED' or row['current_attempt_id']:
                return {'job': self._text_job_view(db, row)}
            if db.execute("SELECT 1 FROM dr_text_jobs WHERE id<>? AND current_attempt_id IS NOT NULL AND state NOT IN ('SUCCEEDED','FAILED','CANCELED') LIMIT 1", (jid,)).fetchone():
                db.execute('UPDATE dr_text_jobs SET error=? WHERE id=?', ('等待原文本任务结束或核查完成，尚未派发', jid))
                return {'job': self._text_job_view(db, self._text_job_row(db, jid))}
            if self.text_adapter is None:
                db.execute("UPDATE dr_text_jobs SET state='FAILED',error=? WHERE id=?", ('Gateway文本适配器不可用', jid))
                return {'job': self._text_job_view(db, self._text_job_row(db, jid))}
            aid, epoch, now = new_id(), row['epoch']+1, time.time()
            db.execute('INSERT INTO dr_text_attempts(id,job_id,epoch,payload_hash,dispatch_state,created_at,updated_at) VALUES(?,?,?,?,?,?,?)', (aid, jid, epoch, row['input_hash'], 'INTENT', now, now))
            db.execute("UPDATE dr_text_jobs SET current_attempt_id=?,epoch=?,state='SUBMITTING',updated_at=? WHERE id=?", (aid, epoch, now, jid))
        from .gateway_text_adapter import GatewayTextBusy
        try:
            receipt = self.text_adapter.submit(row['kind'], json.loads(row['payload']), 'drama-text-'+jid)
        except GatewayTextBusy as exc:
            return self._record_text_not_sent(jid, aid, epoch, exc.evidence)
        except Exception:
            with self.store.connect(write=True) as db:
                db.execute("UPDATE dr_text_jobs SET state='SUBMISSION_UNKNOWN',error=?,updated_at=? WHERE id=? AND epoch=? AND current_attempt_id=?", ('提交回执未知；请核对原任务，不要重发', time.time(), jid, epoch, aid))
            return self.text_job(jid)
        try:
            return self.record_text_receipt(jid, aid, epoch, receipt)
        except Exception:
            return self._text_local_recovery(jid, aid, epoch)

    def _record_text_not_sent(self, jid, aid, epoch, evidence):
        # Only the adapter's complete, explicit admission rejection is retryable.
        if evidence.get('status') != 409 or evidence.get('body', {}).get('error', {}).get('code') != 'TEXT_BUSY':
            raise ValueError('A complete TEXT_BUSY receipt is required')
        with self.store.connect(write=True) as db:
            row = self._text_job_row(db, jid)
            if row['epoch'] != epoch or row['current_attempt_id'] != aid or row['gateway_id']:
                return {'job': self._text_job_view(db, row)}
            db.execute("UPDATE dr_text_attempts SET dispatch_state='NOT_SENT',receipt=?,updated_at=? WHERE id=?", (encode(evidence), time.time(), aid))
            db.execute("UPDATE dr_text_jobs SET state='QUEUED',current_attempt_id=NULL,resource_status='not_acquired',error=?,updated_at=? WHERE id=?", ('Gateway明确未受理，等待资源后按原编号派发', time.time(), jid))
            return {'job': self._text_job_view(db, self._text_job_row(db, jid))}

    def _validate_text_output(self, db, row, raw):
        try:
            value = bounded_json(raw)
            payload = json.loads(row['payload'])
            kind = TASK_KINDS[row['kind']]
            if kind == 'video_prompt':
                from .prompts import prepare_video_prompt
                try: prepare_video_prompt(value, payload)
                except ValueError as exc: fail('prompt', str(exc))
                context = self.validate_reference_inputs('shot', payload['shotRevisionId'], db=db)
                current = context['snapshot']
                expected = (context['approvedReferenceAssetId'] or '', current.get('actionReferenceAssetId') or '',
                            current.get('videoReferenceAssetId') or '', current.get('audioReferenceAssetId') or '')
                actual = tuple(payload.get(field, '') for field in ('imageAssetId', 'actionImageAssetId', 'videoAssetId', 'audioAssetId'))
                if expected != actual: fail('prompt', '参考素材已变化，请重新核对')
                return {'valid': True, 'errors': []}
            # A valid result may reference only the immutable inputs actually sent.
            self._validate_story(db, row['episode_id'], kind, value, model=True, current=True)
            if kind != 'storyboard':
                if value.get('sourceRevisionIds') != [s['revisionId'] for s in payload['sources']]:
                    fail('sourceRevisionIds', '模型输出改变了冻结来源', 'source_reference_invalid')
                if kind != 'entities' and set(value.get('entityRevisionIds', [])) != {e['revisionId'] for e in payload['entities']}:
                    fail('entityRevisionIds', '模型输出改变了冻结实体', 'entity_reference_invalid')
            if kind == 'script' and value.get('beatRevisionId') != payload['beatRevisionId']:
                fail('beatRevisionId', '模型输出改变了冻结节拍')
            if kind == 'storyboard' and value.get('scriptRevisionId') != payload['scriptRevisionId']:
                fail('scriptRevisionId', '模型输出改变了冻结剧本')
            if payload['sourceHeadId'] != self._story_head(db, row['episode_id'], 'source')['currentRevisionId']:
                fail('sourceRevisionIds', '生成期间原文已变更，结果需重新校对', 'story_dependency_changed')
            self._validate_story(db, row['episode_id'], kind, value, model=True, current=True)
            return {'valid': True, 'errors': []}
        except DramaError as exc:
            return self._story_error(exc)

    def record_text_receipt(self, jid, aid, epoch, receipt):
        # Receipts are bounded before persistence; a giant/invalid envelope cannot take down recovery.
        try:
            if not isinstance(receipt, dict):
                fail('receipt', '回执必须是对象')
            raw_output = receipt.get('output_text')
            if raw_output is not None and (not isinstance(raw_output, str) or len(raw_output.encode('utf-8')) > OUTPUT_BYTES):
                fail('output_text', '上游输出超过2MiB', 'story_output_limit')
            # Gateway may include both output_json and output_text: archive one raw copy.
            keys = {'job_id', 'idempotency_key', 'task_kind', 'state', 'resource_status', 'output_sha256',
                    'model', 'contract_version', 'attempt_id', 'owner_id', 'lease_epoch', 'usage', 'error',
                    'memory_released', 'cleanup_required', 'cancel_requested'}
            receipt = content_copy({k: v for k, v in receipt.items() if k in keys})
            if len(encode(receipt).encode('utf-8')) > 64 * 1024:
                fail('receipt', '回执元数据超过上限')
            receipt['output_text'] = raw_output
        except (DramaError, UnicodeError):
            receipt = {'state': 'RECOVERY_REQUIRED', 'error': 'Gateway回执无效或超过上限'}
        with self.store.connect(write=True) as db:
            row = self._text_job_row(db, jid)
            identity_ok = (receipt.get('idempotency_key') == 'drama-text-'+jid and receipt.get('task_kind') == row['kind']
                           and isinstance(receipt.get('job_id'), str))
            if row['epoch'] != epoch or row['current_attempt_id'] != aid or not identity_ok or (row['gateway_id'] and row['gateway_id'] != receipt.get('job_id')):
                db.execute('INSERT INTO dr_text_receipt_evidence VALUES(?,?,?,?,?,?)', (new_id(), jid, aid, encode(receipt), '旧epoch或回执身份不匹配', time.time()))
                if row['epoch'] == epoch and row['current_attempt_id'] == aid:
                    db.execute("UPDATE dr_text_jobs SET state='RECOVERY_REQUIRED',error=?,updated_at=? WHERE id=?", ('回执身份不匹配，请核对原任务', time.time(), jid))
                return {'job': self._text_job_view(db, self._text_job_row(db, jid))}
            prior = db.execute('SELECT * FROM dr_text_attempts WHERE id=?', (aid,)).fetchone()
            raw = receipt.get('output_text')
            output_status = row['output_status']
            if raw is not None:
                if not isinstance(raw, str) or len(raw.encode('utf-8')) > OUTPUT_BYTES:
                    fail('output_text', '上游输出超过2MiB', 'story_output_limit')
                digest = sha(raw)
                if receipt.get('output_sha256') != digest or (prior['raw_hash'] and prior['raw_hash'] != digest):
                    db.execute('INSERT INTO dr_text_receipt_evidence VALUES(?,?,?,?,?,?)', (new_id(), jid, aid, encode(receipt), '输出hash不匹配或改变', time.time()))
                    db.execute("UPDATE dr_text_jobs SET state='RECOVERY_REQUIRED',error=? WHERE id=?", ('原始输出身份变化，待人工核对', jid))
                    return {'job': self._text_job_view(db, self._text_job_row(db, jid))}
                if prior['raw_hash'] is None:
                    # Commit the bounded original before parsing/semantic validation. A crash
                    # can lose validation progress, never the only copy of the model result.
                    db.execute('UPDATE dr_text_attempts SET raw_text=?,raw_hash=?,updated_at=? WHERE id=?', (raw, digest, time.time(), aid))
                    known_resource = receipt.get('resource_status', 'recovery_required')
                    if known_resource not in ('not_acquired', 'held', 'releasing', 'released', 'recovery_required'):
                        known_resource = 'recovery_required'
                    db.execute("UPDATE dr_text_jobs SET gateway_id=?,resource_status=?,state='VALIDATING',updated_at=? WHERE id=?", (receipt['job_id'], known_resource, time.time(), jid))
                    db.commit()
                    db.execute('BEGIN IMMEDIATE')
                    row = self._text_job_row(db, jid)
                    if row['epoch'] != epoch or row['current_attempt_id'] != aid:
                        db.execute('INSERT INTO dr_text_receipt_evidence VALUES(?,?,?,?,?,?)', (new_id(), jid, aid, encode(receipt), '归档后owner已变化', time.time()))
                        return {'job': self._text_job_view(db, row)}
                    prior = db.execute('SELECT * FROM dr_text_attempts WHERE id=?', (aid,)).fetchone()
                if prior['validation'] is None:
                    validation = self._validate_text_output(db, row, raw)
                    output_status = 'valid_draft' if validation['valid'] else 'invalid_draft'
                    db.execute('UPDATE dr_text_attempts SET validation=? WHERE id=?', (encode(validation), aid))
                    if row['kind'] != 'draft_video_prompt':
                        self._append_story_draft(db, row['episode_id'], TASK_KINDS[row['kind']], raw, validation, aid)
                else:
                    output_status = 'valid_draft' if json.loads(prior['validation'])['valid'] else 'invalid_draft'
            state = receipt.get('state')
            if row['state'] in ('SUCCEEDED', 'FAILED', 'CANCELED') and state != row['state']:
                db.execute('INSERT INTO dr_text_receipt_evidence VALUES(?,?,?,?,?,?)', (new_id(), jid, aid, encode(receipt), '迟到回执不得回退终态', time.time()))
                return {'job': self._text_job_view(db, row)}
            if state not in ('QUEUED', 'LOADING', 'SUBMITTING', 'RUNNING', 'VALIDATING', 'RELEASING', 'SUCCEEDED', 'FAILED', 'CANCELED', 'SUBMISSION_UNKNOWN', 'RECOVERY_REQUIRED'):
                state = 'RECOVERY_REQUIRED'
            resource = receipt.get('resource_status', 'recovery_required')
            if resource not in ('not_acquired', 'held', 'releasing', 'released', 'recovery_required'):
                resource = 'recovery_required'
            if state == 'SUCCEEDED' and (output_status == 'none' or resource != 'released'):
                state = 'RECOVERY_REQUIRED'
            db.execute('UPDATE dr_text_attempts SET receipt=?,dispatch_state=?,updated_at=? WHERE id=?', (encode(receipt), 'RECEIVED', time.time(), aid))
            db.execute('UPDATE dr_text_jobs SET gateway_id=?,state=?,output_status=?,resource_status=?,error=?,updated_at=? WHERE id=?',
                       (receipt['job_id'], state, output_status, resource, '结果需人工修复' if output_status == 'invalid_draft' else None, time.time(), jid))
            return {'job': self._text_job_view(db, self._text_job_row(db, jid))}

    def _text_local_recovery(self, jid, aid, epoch):
        with self.store.connect(write=True) as db:
            db.execute("UPDATE dr_text_jobs SET state='RECOVERY_REQUIRED',error=?,updated_at=? WHERE id=? AND epoch=? AND current_attempt_id=?",
                       ('本地结果处理未完成；原任务与已归档输出保留，请核对', time.time(), jid, epoch, aid))
        return self.text_job(jid)

    def reconcile_text_job(self, jid):
        restore_guard(self, "query")
        with self.store.connect() as db:
            row = self._text_job_row(db, jid)
        if not row['current_attempt_id'] or self.text_adapter is None:
            return self.text_job(jid)
        try:
            receipt = self.text_adapter.get(row['gateway_id']) if row['gateway_id'] else self.text_adapter.find('drama-text-'+jid)
        except Exception:
            return self.text_job(jid)
        if receipt is None:
            return self.text_job(jid)
        try:
            return self.record_text_receipt(jid, row['current_attempt_id'], row['epoch'], receipt)
        except Exception:
            return self._text_local_recovery(jid, row['current_attempt_id'], row['epoch'])

    def cancel_text_job(self, jid):
        restore_guard(self, "write")
        with self.store.connect(write=True) as db:
            row = self._text_job_row(db, jid)
            if row['state'] == 'QUEUED' and not row['current_attempt_id']:
                db.execute("UPDATE dr_text_jobs SET state='CANCELED',updated_at=? WHERE id=?", (time.time(), jid))
            elif row['state'] not in ('SUCCEEDED', 'FAILED', 'CANCELED'):
                db.execute('INSERT INTO dr_events VALUES(?,?,?,?,?)', (new_id(), 'text-cancel:'+jid, 'cancel_requested', '{}', time.time()))
                db.execute('UPDATE dr_text_jobs SET error=?,updated_at=? WHERE id=?', ('已记录取消请求；须等Gateway确认安全结束', time.time(), jid))
        if row['gateway_id'] and self.text_adapter is not None and row['state'] not in ('SUCCEEDED', 'FAILED', 'CANCELED'):
            try:
                receipt = self.text_adapter.cancel(row['gateway_id'])
                return self.record_text_receipt(jid, row['current_attempt_id'], row['epoch'], receipt)
            except Exception:
                pass
        return self.text_job(jid)
