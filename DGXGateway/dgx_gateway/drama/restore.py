"""Bounded project restore, durable operation receipts and restore isolation."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import threading
import time
import uuid
import zipfile

from .backups import (MIB,MAX_TOTAL,MAX_ROWS,MAX_LINE,RESERVE,TABLES,SCHEMA_HASH,SCHEMA6_HASH,SCHEMA7_HASH,SCHEMA8_HASH,SCHEMA_VERSION,normalize_archive_rows,EXTENSIONS,encode,sha,strict_json,
                      fail,space,open_regular,confined_directory,no_replace,atomic_json,validate_rows,write_database,pack_project,sanitized,recovery_obligations,fsync_directory)
from .contracts import fields,identifier,revision,text


def inspect_archive(path, expected_hash=None):
    with open_regular(path) as stream:
        if expected_hash and hashlib.file_digest(stream,'sha256').hexdigest()!=expected_hash: fail('原项目包内容身份已变化')
        stream.seek(0)
        return _inspect_archive_stream(stream)


def _inspect_archive_stream(stream):
    with zipfile.ZipFile(stream) as archive:
        infos=archive.infolist()
        if not 1<=len(infos)<=8192: fail('归档成员数量无效')
        names=set(); total=0
        for info in infos:
            name=info.filename; mode=info.external_attr>>16
            valid=(name in {'manifest.json','project.sqlite3','capabilities.json','recovery.json'} or re.fullmatch(r'rows/dr_[a-z_]+\.jsonl',name) or re.fullmatch(r'assets/[a-f0-9]{64}\.(?:mp4|png|jpg|webp|wav|mp3|m4a|flac|srt|ass|json)',name))
            if not valid or info.orig_filename != name or name in names or info.is_dir() or info.compress_type!=zipfile.ZIP_STORED or info.flag_bits&1 or info.extra or info.compress_size!=info.file_size or (stat.S_IFMT(mode) not in (0,stat.S_IFREG)):
                fail('归档路径、类型或压缩方法不符合项目包合同')
            names.add(name); total+=info.file_size
            if info.file_size>512*MIB or total>MAX_TOTAL: fail('归档展开大小超过限制')
        if 'manifest.json' not in names: fail('缺少项目清单')
        if archive.getinfo('manifest.json').file_size>MIB: fail('清单超过1MiB')
        manifest=strict_json(archive.read('manifest.json'))
        if not isinstance(manifest,dict) or manifest.get('format')!='dgx-drama-project' or manifest.get('formatVersion')!=1:
            fail('项目包版本不兼容')
        supported = {6: SCHEMA6_HASH, 7: SCHEMA7_HASH, 8: SCHEMA8_HASH, SCHEMA_VERSION: SCHEMA_HASH}
        version = manifest.get('schemaVersion')
        if type(version) is not int or version not in supported or manifest.get('schemaFingerprint') != supported[version]:
            fail('项目包版本不兼容')
        identifier(manifest.get('projectId')); identifier(manifest.get('backupId'))
        listed=manifest.get('files')
        if not isinstance(listed,list) or len(listed)!=len(names)-1: fail('归档清单成员数量不符')
        source_tables = TABLES if version in {8, SCHEMA_VERSION} else tuple(
            table for table in TABLES if table != 'dr_reference_candidate_reviews')
        expected={f'rows/{t}.jsonl' for t in source_tables}|{'project.sqlite3','capabilities.json','recovery.json'}
        if not expected.issubset(names): fail('项目包缺少必需数据表')
        file_map={}
        for item in listed:
            if not isinstance(item,dict) or set(item)-{'path','bytes','sha256','assetId'}: fail('清单成员字段无效')
            name=item.get('path')
            if name not in names or name=='manifest.json' or name in file_map or type(item.get('bytes')) is not int or not re.fullmatch('[a-f0-9]{64}',str(item.get('sha256',''))): fail('归档清单成员无效')
            if item['bytes']!=archive.getinfo(name).file_size: fail('归档成员声明长度不符')
            file_map[name]=item
        if set(file_map)!=names-{'manifest.json'} or manifest.get('totalBytes')!=sum(f['bytes'] for f in listed): fail('归档清单总量不符')
        return manifest


def restore_archive(path,target,*,progress=lambda **kw:None,expected_hash=None):
    path=Path(path); target=Path(target)
    confined_directory(target.parent)
    # Targets are server-created unique names. Never replace even an empty directory.
    if target.exists() or target.is_symlink(): fail('恢复目标必须是尚未创建的新空工作区')
    if not expected_hash:
        with open_regular(path) as source:expected_hash=hashlib.file_digest(source,'sha256').hexdigest()
    manifest=inspect_archive(path,expected_hash)
    metadata_bytes=sum(f['bytes'] for f in manifest['files'] if f['path'].startswith('rows/'))
    space(target.parent,manifest['totalBytes']+metadata_bytes*3+20*MIB)
    partial=target.parent/('.restore-'+target.name+'.partial')
    partial.mkdir(mode=0o700)
    try:
        drama=partial/'drama'; drama.mkdir(); (drama/'assets').mkdir()
        rows={}; metadata={}; done=0; row_bytes=0
        with open_regular(path) as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=expected_hash:fail('原项目包在恢复前发生变化')
            stream.seek(0)
            with zipfile.ZipFile(stream) as archive:
                return _extract_restore_archive(archive,manifest,partial,target,progress,expected_hash)
    finally:
        if partial.exists(): shutil.rmtree(partial)


def _extract_restore_archive(archive,manifest,partial,target,progress,expected_hash):
        drama=partial/'drama';rows={};metadata={};done=0;row_bytes=0
        for item in manifest['files']:
            name=item['path']; digest=hashlib.sha256(); count=0
            output=(drama/name) if name.startswith('assets/') else partial/(name.replace('/','_'))
            with archive.open(name) as source,output.open('xb') as dest:
                os.chmod(output,0o600)
                while chunk:=source.read(MIB):
                    count+=len(chunk); done+=len(chunk)
                    if count>item['bytes'] or done>MAX_TOTAL: fail('展开大小超过声明')
                    space(target.parent,MIB); digest.update(chunk); dest.write(chunk)
                    progress(bytesDone=done,bytesTotal=manifest['totalBytes'])
                dest.flush(); os.fsync(dest.fileno())
            if count!=item['bytes'] or digest.hexdigest()!=item['sha256']: fail('归档文件hash校验失败')
            if name.startswith('rows/'):
                table=name[5:-6]
                if table not in TABLES: fail('归档包含未知数据表')
                values=[]
                with output.open('rb') as src:
                    while line:=src.readline(MAX_LINE+1):
                        if len(line)>MAX_LINE: fail('单条数据行超过16MiB')
                        row_bytes+=len(line)
                        if row_bytes>MAX_ROWS or len(values)>=100000: fail('归档数据行超过总量限制')
                        value=strict_json(line)
                        if not isinstance(value,dict): fail('数据行必须为对象')
                        values.append(value)
                rows[table]=values; output.unlink()
            elif name in ('capabilities.json','recovery.json'):
                if count>MAX_ROWS: fail('恢复元数据过大')
                metadata[name]=strict_json(output.read_bytes()); output.unlink()
            elif name=='project.sqlite3':
                # Deliberately opaque: uploaded sqlite_master never executes.
                output.unlink()
        rows = normalize_archive_rows(rows, manifest['schemaVersion'])
        validate_rows(rows,manifest['projectId'])
        rows,restore_redactions=sanitized(rows)
        expected_assets={f"assets/{r['id']}.{strict_json(r['metadata']).get('extension','mp4')}" for r in rows['dr_assets']}
        actual_assets={i['path'] for i in manifest['files'] if i['path'].startswith('assets/')}
        if expected_assets!=actual_assets: fail('素材表与归档文件集合不一致')
        for row in rows['dr_assets']:
            ext=strict_json(row['metadata']).get('extension','mp4'); item=next(x for x in manifest['files'] if x['path']==f"assets/{row['id']}.{ext}")
            if ext not in EXTENSIONS or item['sha256']!=row['id'] or item['bytes']!=row['size_bytes']: fail('素材内容身份不匹配')
        expected_tables = dict(manifest.get('tables') or {})
        if manifest['schemaVersion'] in {6, 7}:
            expected_tables['dr_reference_candidate_reviews'] = 0
        if {t:len(v) for t,v in rows.items()}!=expected_tables: fail('恢复行数与清单不符')
        database=drama/'drama.sqlite3'; write_database(database,rows)
        with closing(sqlite3.connect(database)) as db:
            old=db.execute('SELECT paused,revision FROM dr_production_state WHERE project_id=?',(manifest['projectId'],)).fetchone()
            db.execute('INSERT INTO dr_production_state(project_id,paused,revision) VALUES(?,1,1) ON CONFLICT(project_id) DO UPDATE SET paused=1',(manifest['projectId'],))
            db.commit()
        marker={'formatVersion':1,'mode':'isolated','backupId':manifest['backupId'],'backupSha256':expected_hash,'projectId':manifest['projectId'],'restoredAt':time.time(),'originalProductionState':list(old) if old else None,'restoreRedactions':restore_redactions,'recovery':{'tasks':recovery_obligations(rows),'defaultMode':'isolated'}}
        atomic_json(drama/'restore-state.json',marker)
        atomic_json(drama/'restore-provenance.json',{'backupId':manifest['backupId'],'backupSha256':expected_hash,'schemaFingerprint':SCHEMA_HASH,'sourceSchemaVersion':manifest['schemaVersion'],'sourceSchemaFingerprint':manifest['schemaFingerprint']})
        with database.open('rb') as stream: os.fsync(stream.fileno())
        fsync_directory(drama/'assets'); fsync_directory(drama); fsync_directory(partial)
        no_replace(partial,target)
        return {'projectId':manifest['projectId'],'backupId':manifest['backupId'],'backupSha256':expected_hash,'mode':'isolated','tables':manifest['tables'],'assetCount':len(expected_assets),'recovery':marker['recovery']}


def restore_guard(service,action):
    mode=getattr(service,'_restore_mode',None)
    if mode and not (mode=='query_only' and action=='query'):
        fail('恢复工作区的模型任务已隔离。请在原工作区处理生成或取消；这里仅可显式启用原任务查询。','restore_isolated',409)


class BackupMixin:
    def init_restore_guard(self):
        marker=self.root/'restore-state.json'; provenance=self.root/'restore-provenance.json'
        self._restore_mode=None
        self._restore_original_tasks=set()
        if marker.exists() or provenance.exists():
            if not marker.exists() or marker.is_symlink(): fail('恢复隔离记录缺失，拒绝启动')
            value=strict_json(marker.read_bytes())
            if value.get('mode') not in ('isolated','query_only'): fail('恢复隔离状态无效')
            self._restore_mode=value['mode']
            self._restore_original_tasks={(r['table'],r['id']) for r in value.get('recovery',{}).get('tasks',[]) if isinstance(r,dict) and r.get('table') and r.get('id')}

    def restored_original_task(self,table,oid):
        return (table,oid) in self._restore_original_tasks

    def active_export_count(self,db):
        return sum(not self.restored_original_task('dr_exports',r['id']) for r in db.execute("SELECT id FROM dr_exports WHERE state IN ('QUEUED','RUNNING')"))

    def init_backups(self):
        self._backup_root=self.root/'backups'; self._restore_root=self.root/'recovered'; self._backup_lock=threading.RLock()
        for path in (self._backup_root,self._restore_root): path.mkdir(mode=0o700,exist_ok=True); confined_directory(path)
        self._backup_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='drama-backup')
        self._restored_services={}
        for p in self._backup_root.glob('*.receipt.json'):
            value=strict_json(p.read_bytes())
            if value.get('state') in ('QUEUED','RUNNING'):
                value.update(state='INTERRUPTED',error='服务重启，本地归档操作未自动重做。'); atomic_json(p,value)

    def close_backups(self):
        self._backup_executor.shutdown(wait=True)
        for child in self._restored_services.values(): child.close()

    def _backup_receipt(self,oid):
        path=self._backup_root/(identifier(oid)+'.receipt.json')
        if not path.is_file() or path.is_symlink(): fail('找不到归档操作','not_found',404)
        return strict_json(path.read_bytes())

    def backup_operation(self,oid):
        return {'operation':self._backup_receipt(oid)}

    def project_backups(self,pid,before=None):
        self.project(pid)
        values=[]
        for p in self._backup_root.glob('*.receipt.json'):
            if p.is_symlink(): continue
            row=strict_json(p.read_bytes())
            if row.get('projectId')==pid: values.append(row)
        values.sort(key=lambda r:(r['createdAt'],r['id']),reverse=True)
        if before:
            identifier(before)
            index=next((i for i,r in enumerate(values) if r['id']==before),None)
            if index is None: fail('归档分页位置不属于当前项目','not_found',404)
            values=values[index+1:]
        page=values[:100]
        return {'operations':page,'nextCursor':page[-1]['id'] if len(values)>100 else None}

    def _operation(self,kind,payload,key,runner,project_id=None):
        key=text(key,'requestKey',128); digest=sha(encode(payload).encode()); oid=sha((kind+':'+key).encode())[:32]
        path=self._backup_root/(oid+'.receipt.json')
        with self._backup_lock:
            if path.exists():
                old=self._backup_receipt(oid)
                if old['inputHash']!=digest: fail('原请求键不能用于其他归档参数','request_conflict')
                return {'operation':old}
            value={'id':oid,'kind':kind,'requestKey':key,'inputHash':digest,'projectId':project_id,'state':'QUEUED','phase':'准备中','bytesDone':0,'bytesTotal':0,'createdAt':time.time()}
            atomic_json(path,value)
            def run():
                def progress(**kw):
                    with self._backup_lock: value.update(kw); atomic_json(path,value)
                try:
                    progress(state='RUNNING',phase='正在校验与复制')
                    result=runner(oid,progress)
                    progress(state='SUCCEEDED',phase='完成',result=result)
                except Exception as exc:
                    from .contracts import DramaError
                    progress(state='FAILED',phase='未完成',error=exc.message if isinstance(exc,DramaError) else '归档操作失败，原数据未修改。')
            self._backup_executor.submit(run)
            return {'operation':dict(value)}

    def create_backup(self,pid,body):
        fields(body,{'requestKey','expectedProjectRevision'},{'requestKey','expectedProjectRevision'})
        project=self.project(pid)['project']
        original_id=sha(('backup:'+text(body['requestKey'],'requestKey',128)).encode())[:32]
        if not (self._backup_root/(original_id+'.receipt.json')).exists(): revision(project['revision'],body['expectedProjectRevision'])
        return self._operation('backup',{'projectId':pid,**body},body['requestKey'],lambda oid,progress:pack_project(self,pid,self._backup_root/(oid+'.drama.zip'),oid,progress),pid)

    def backup_download(self,bid):
        receipt=self._backup_receipt(bid)
        if receipt['kind']!='backup' or receipt['state']!='SUCCEEDED': fail('项目包尚未成功')
        path=self._backup_root/(bid+'.drama.zip')
        with open_regular(path) as stream:
            if hashlib.file_digest(stream,'sha256').hexdigest()!=receipt['result']['sha256']: fail('项目包内容已变化')
        return path

    def upload_restore(self,stream,length,key,expected_hash=None):
        if type(length) is not int or not 0<length<=MAX_TOTAL or (expected_hash and not re.fullmatch('[a-f0-9]{64}',expected_hash)):
            fail('项目包需要明确有界长度与合法SHA256','invalid_upload',413)
        text(key,'requestKey',128); uid=sha(('upload:'+key).encode())[:32]; path=self._backup_root/(uid+'.upload.zip'); receipt=self._backup_root/(uid+'.upload.json')
        with self._backup_lock:
            prior=strict_json(receipt.read_bytes()) if receipt.exists() else None
            if prior and prior['bytes']!=length: fail('上传原请求参数已改变','request_conflict')
            if not prior: space(self._backup_root,length+16*MIB)
            partial=path.with_name(path.name+'.partial'); output=None
            try:
                if not prior: output=partial.open('xb'); os.chmod(partial,0o600)
                digest=hashlib.sha256(); remaining=length
                while remaining:
                    chunk=stream.read(min(remaining,MIB))
                    if not chunk: fail('上传未完成')
                    remaining-=len(chunk); digest.update(chunk)
                    if output: output.write(chunk); space(self._backup_root,MIB)
                actual=digest.hexdigest()
                if (expected_hash and actual!=expected_hash) or (prior and actual!=prior['sha256']): fail('上传原内容hash不匹配','request_conflict')
                if prior: return {'upload':prior}
                output.flush(); os.fsync(output.fileno()); output.close(); output=None
                manifest=inspect_archive(partial)
                if path.exists():
                    with open_regular(path) as existing:
                        if hashlib.file_digest(existing,'sha256').hexdigest()!=actual: fail('原上传文件身份发生变化')
                else: no_replace(partial,path)
                value={'id':uid,'sha256':actual,'bytes':length,'preview':{'projectId':manifest['projectId'],'snapshotKind':manifest['snapshotKind'],'tables':manifest['tables'],'assetCount':sum(f['path'].startswith('assets/') for f in manifest['files'])}}; atomic_json(receipt,value); return {'upload':value}
            finally:
                if output: output.close()
                partial.unlink(missing_ok=True)

    def create_restore(self,body):
        if getattr(self,'_restore_depth',0)>=4:fail('恢复工作区层级已到4层，请在主工作区导入项目包')
        fields(body,{'requestKey','backupId','uploadId'},{'requestKey'})
        if bool(body.get('backupId'))==bool(body.get('uploadId')): fail('请选择一个项目包来源')
        if body.get('backupId'):
            bid=identifier(body['backupId']); source=self.backup_download(bid); receipt=self._backup_receipt(bid)
            source_hash=receipt['result']['sha256']; source_bytes=receipt['result']['bytes']; expected_pid=receipt['projectId']
        else:
            uid=identifier(body['uploadId']); source=self._backup_root/(uid+'.upload.zip')
            with open_regular(self._backup_root/(uid+'.upload.json')) as stream:receipt=strict_json(stream.read(MIB+1))
            if receipt.get('id')!=uid:fail('上传回执身份不匹配')
            source_hash=receipt['sha256'];source_bytes=receipt['bytes'];expected_pid=receipt['preview']['projectId']
        if source.stat().st_size!=source_bytes:fail('原项目包长度已变化')
        manifest=inspect_archive(source,source_hash)
        if manifest['projectId']!=expected_pid:fail('原项目包项目身份已变化')
        return self._operation('restore',body,body['requestKey'],lambda oid,progress:restore_archive(source,self._restore_root/oid,progress=progress,expected_hash=source_hash),manifest['projectId'])

    def restored_service(self,rid):
        receipt=self._backup_receipt(rid)
        if receipt['kind']!='restore' or receipt['state']!='SUCCEEDED': fail('恢复工作区尚未就绪')
        with self._backup_lock:
            if rid not in self._restored_services:
                self._restored_services[rid]=type(self)(self._restore_root/rid,self.repo_root,reference_adapter=self.reference_adapter,video_adapter=self.video_adapter,text_adapter=self.text_adapter)
                self._restored_services[rid]._restore_depth=getattr(self,'_restore_depth',0)+1
            return self._restored_services[rid]

    def restore_query_mode(self,rid,body):
        fields(body,{'requestKey','mode'},{'requestKey','mode'}); text(body['requestKey'],'requestKey',128)
        if body['mode']!='query_only': fail('恢复工作区只支持显式只读核查模式')
        child=self.restored_service(rid)
        with self._backup_lock:
            marker=child.root/'restore-state.json'; value=strict_json(marker.read_bytes()); value['mode']='query_only'; atomic_json(marker,value); child._restore_mode='query_only'
        return {'mode':'query_only'}
