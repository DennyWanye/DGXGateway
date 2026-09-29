"""Business ownership relations which SQLite foreign keys cannot express."""
def validate_semantics(rows, fail):
    index={table:{row.get('id'):row for row in values if row.get('id')} for table,values in rows.items()}
    def get(table,oid):
        if oid not in index[table]:fail('归档业务引用缺失：'+table)
        return index[table][oid]
    def same(left,right,label):
        if left!=right:fail('归档业务关系不匹配：'+label)
    def shot_revision(rid,sid):same(get('dr_shot_revisions',rid)['shot_id'],sid,'镜头修订')
    def entity_revision(rid,eid):same(get('dr_entity_revisions',rid)['entity_id'],eid,'实体修订')
    def story(rid,eid,kind):
        row=get('dr_story_revisions',rid);same((row['episode_id'],row['kind']),(eid,kind),'故事修订分集/类型')
    def shot_episode(rid):return get('dr_shots',get('dr_shot_revisions',rid)['shot_id'])['episode_id']
    for row in rows['dr_shots']:
        if row['current_content_revision_id']:shot_revision(row['current_content_revision_id'],row['id'])
        if row['selected_take_id']:same(get('dr_takes',row['selected_take_id'])['shot_id'],row['id'],'采用take')
    for row in rows['dr_takes']:
        if row['source_revision_id']:shot_revision(row['source_revision_id'],row['shot_id'])
    for row in rows['dr_entities']:
        for key in ('current_revision_id','approved_revision_id'):
            if row[key]:entity_revision(row[key],row['id'])
    for table in ('dr_reference_jobs','dr_reference_candidates','dr_production_approvals'):
        for row in rows[table]:
            rid=row.get('target_revision_id',row.get('revision_id'))
            (entity_revision if row['target_type']=='entity' else shot_revision)(rid,row['target_id'])
            if table=='dr_reference_candidates':
                job=get('dr_reference_jobs',row['job_id'])
                same(tuple(row[k] for k in ('target_type','target_id','target_revision_id')),tuple(job[k] for k in ('target_type','target_id','target_revision_id')),'参考候选所属任务')
    for row in rows['dr_reference_candidate_reviews']:
        candidate = get('dr_reference_candidates', row['candidate_id'])
        same(row['project_id'], candidate['project_id'], '图片审核项目归属')
    for row in rows['dr_invalidations']:
        for key in ('old_revision_id','new_revision_id'):entity_revision(row[key],row['entity_id'])
    for row in rows['dr_video_items']:
        shot_revision(row['shot_revision_id'],row['shot_id'])
        same(get('dr_video_jobs',row['job_id'])['episode_id'],get('dr_shots',row['shot_id'])['episode_id'],'视频任务分集')
    for table in ('dr_exports','dr_subtitle_revisions'):
        for row in rows[table]:same(get('dr_timeline_revisions',row['timeline_id'])['episode_id'],row['episode_id'],'时间线分集')
    for row in rows['dr_transcriptions']:
        if row['subtitle_id']:same(get('dr_subtitle_revisions',row['subtitle_id'])['timeline_id'],row['timeline_id'],'转写字幕时间线')
    for row in rows['dr_story_heads']:
        for key in ('current_revision_id','approved_revision_id'):
            if row[key]:story(row[key],row['episode_id'],row['kind'])
    for table,kind in (('dr_entity_proposal_mappings','entities'),('dr_storyboard_mappings','storyboard')):
        for row in rows[table]:
            story(row['proposal_revision_id'],row['episode_id'],kind)
            if kind=='entities':entity_revision(row['entity_revision_id'],row['entity_id'])
            else:
                shot_revision(row['shot_revision_id'],row['shot_id'])
                same(get('dr_shots',row['shot_id'])['episode_id'],row['episode_id'],'分镜映射分集')
    for row in rows['dr_script_dependencies']:story(row['script_revision_id'],shot_episode(row['shot_revision_id']),'script')
    for row in rows['dr_script_invalidations']:
        for key in ('old_revision_id','new_revision_id'):story(row[key],shot_episode(row['shot_revision_id']),'script')
    task_kinds={'extract_entities':'entities','draft_beats':'beats','draft_script':'script','draft_storyboard':'storyboard'}
    for row in rows['dr_text_jobs']:
        if row['current_attempt_id']:same(get('dr_text_attempts',row['current_attempt_id'])['job_id'],row['id'],'文本当前attempt')
        if row['repair_of']:
            previous=get('dr_text_jobs',row['repair_of']);same((row['episode_id'],row['kind']),(previous['episode_id'],previous['kind']),'文本修复来源')
    for row in rows['dr_story_drafts']:
        if row['attempt_id']:
            job=get('dr_text_jobs',get('dr_text_attempts',row['attempt_id'])['job_id'])
            same((row['episode_id'],row['kind']),(job['episode_id'],task_kinds.get(job['kind'])),'编辑草稿attempt')
    # Receipt evidence attempt_id is an untrusted identity string, never an FK.


def validate_response_schema(rows, scope, response, fail):
    """Allow only the frozen mutation envelope and typed object identities."""
    name=scope.split(':')[0]
    envelopes={
        'projects':'project','episodes':'episode','order':'episode','project-canvas':'project',
        'project-merge':'projectId recordedSourceProjectId episodeIds revision counts sourceRetained',
        'upload':'asset','image-upload':'asset','image-import':'asset','video-import':'asset','audio':'asset','audio-upload':'asset','audio-import':'asset',
        'entities':'entity','entity-revisions':'entity','entity-approval':'entity affectedShotIds',
        'production-state':'revision paused reservedShots reservedSeconds maxReservedShots',
        'reference-submit':'jobId','reference-import':'jobId','image-reviews':'reviews','video-batches':'job','text-jobs':'job',
        'shots':'shot','selection':'shot','take-review':'takeId reviewStatus',
        'timeline':'timeline','song':'episode','subtitles':'subtitle','subtitle-approve':'subtitleId approved',
        'exports':'export','export-retry':'exportId','export-approve':'exportId review',
        'transcriptions':'transcription','shot-content':'shotId revision currentRevisionId current',
        'shot-reference':'shotId revision currentRevisionId current','shot-review':'shotId revision invalidations',
        'script-review':'shotId revision invalidations','story-approve':'revision head affectedShotIds',
        'story-confirm':'entity mapping','story-import':'shots mappings','review-suggestion':'suggestion',
    }
    for kind in ('source','entities','beats','script','storyboard'):
        envelopes['story-'+kind]='revision head reused'
        envelopes['story-draft-'+kind]='draft'
    if name not in envelopes or not isinstance(response,dict) or set(response)-set(envelopes[name].split()):
        fail('幂等响应字段尚未纳入scope合同：'+name)
    ids={table:{r.get('id') for r in values} for table,values in rows.items()}
    jobs={'text-jobs':ids['dr_text_jobs'],'video-batches':ids['dr_video_jobs'],'reference-submit':ids['dr_reference_jobs'],'reference-import':ids['dr_reference_jobs']}
    objects={'project':ids['dr_projects'],'episode':ids['dr_episodes'],'shot':ids['dr_shots'],'take':ids['dr_takes'],'asset':ids['dr_assets'],'entity':ids['dr_entities'],'timeline':ids['dr_timeline_revisions'],'subtitle':ids['dr_subtitle_revisions'],'export':ids['dr_exports'],'transcription':ids['dr_transcriptions'],'draft':ids['dr_story_drafts'],'attempt':ids['dr_text_attempts'],'job':jobs.get(name,set()),'current':ids['dr_shot_revisions']|ids['dr_entity_revisions'],'revision':ids['dr_story_revisions']|ids['dr_entity_revisions']|ids['dr_shot_revisions']}
    objects.update(videoItem=ids['dr_video_items'], referenceCandidate=ids['dr_reference_candidates'])
    lists={'jobs':'job','shots':'shot','takes':'take','assets':'asset','entities':'entity','attempts':'attempt','history':'revision','revisions':'revision','items':'videoItem','candidates':'referenceCandidate','reviews':'referenceCandidate'}
    scalars={'jobId':jobs.get(name,set()),'shotId':ids['dr_shots'],'takeId':ids['dr_takes'],'exportId':ids['dr_exports'],'subtitleId':ids['dr_subtitle_revisions'],'currentRevisionId':ids['dr_shot_revisions']|ids['dr_entity_revisions']|ids['dr_story_revisions'],'approvedRevisionId':ids['dr_entity_revisions']|ids['dr_story_revisions']}
    list_ids={'affectedShotIds':ids['dr_shots'],'shotIds':ids['dr_shots'],'shotRevisionIds':ids['dr_shot_revisions'],'takeIds':ids['dr_takes'],'jobIds':jobs.get(name,set())}
    job_fields=set('id projectId episodeId kind state requestKey inputHash outputStatus resourceStatus gatewayId currentAttemptId error repairOf attempts inputRevisions gatewayJobId dispatchKey memoryReleased resourceSettled cleanupRequired cancelRequested reservedShots reservedSeconds createdAt updatedAt items targetType targetId targetRevisionId tool parameters inputSnapshot candidates canContinue stale'.split())
    job_fields.update({'promptInput', 'repairJobId', 'compiledPrompt', 'normalizationNotes'})
    scalars.update(repairJobId=ids['dr_text_jobs'], repairOf=ids['dr_text_jobs'])
    attempt_fields=set('id epoch dispatchState payloadHash rawText rawHash validation'.split())
    def walk(value,key=''):
        if key in scalars and value is not None and value not in scalars[key]:fail('幂等响应技术身份不属于原scope')
        if key in list_ids and (not isinstance(value,list) or any(x not in list_ids[key] for x in value)):fail('幂等响应列表越出原scope')
        if isinstance(value,dict):
            if key in objects and value.get('id') is not None and value['id'] not in objects[key]:fail('幂等响应对象身份不属于原scope')
            if key=='job' and set(value)-job_fields:fail('任务响应出现未冻结字段')
            if key=='attempt' and set(value)-attempt_fields:fail('attempt响应出现未冻结字段')
            for k,child in value.items():
                # Authored documents/strings are preserved, not mistaken for API envelopes.
                if k not in {'content','snapshot','rawText','raw_text','inputSnapshot','parameters'}:walk(child,k)
        elif isinstance(value,list):
            for child in value:walk(child,lists.get(key,''))
    walk(response)
