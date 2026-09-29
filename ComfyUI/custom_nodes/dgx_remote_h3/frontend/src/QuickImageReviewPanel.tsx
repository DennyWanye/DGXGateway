import { useCallback, useEffect, useRef, useState } from 'react';
import { get, post, mediaUrl, requestKey, segment } from '@/lib/api';
import { localStore } from '@/lib/workspace';
import { selectImageCandidate } from '@/lib/image-selection';
import type { WorkbenchContext } from '@/lib/types';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';

type Item = Record<string, any>;
const slot = (i: Item) => `${i.targetType}:${i.targetId}:${i.role || 'shot'}`;
const identity = (i: Item) => i.candidateId || i.id;
const status = (i: Item) => i.selected ? '当前已选' : ({approved:'质量通过',rejected:'已退回',pending:'待审核'}[i.reviewStatus as string] || '待审核');
const pending = (i: Item) => !i.selected && (!i.reviewStatus || i.reviewStatus === 'pending');
const roleNames: Record<string,string> = {identity_front:'正面身份图',identity_3q:'四分之三侧面',identity_back:'背视图',costume_fullbody:'服装全身图',identity_sheet:'全位图',location_wide:'场景全景',location_detail:'场景细节',layout:'空间布局',prop_detail:'道具细节',shot:'本镜头画面',reference_image:'本镜头画面'};
function stored(key: string) { try { return JSON.parse(localStore.getItem(key) || '{}'); } catch { return {}; } }

function ReviewImage({ src, alt, large = false }: { src: string; alt: string; large?: boolean }) {
  const [failed, setFailed] = useState(false);
  return failed
    ? <div className={`grid place-items-center rounded-lg bg-black/40 text-sm text-muted-foreground ${large ? 'h-64' : 'h-52'}`}>图片无法加载</div>
    : <img src={src} alt={alt} onError={() => setFailed(true)} className={`w-full object-contain rounded-lg bg-black/20 ${large ? 'max-h-[50vh]' : 'h-52'}`} />;
}

/** Choosing candidates is explicit; merely opening or checking a card never writes. */
export function QuickImageReviewPanel({ ctx, active }: {ctx:WorkbenchContext;active:boolean}) {
  const pid = ctx.project?.id || '', eid = ctx.episode?.id || '';
  const baseKey = `dgx.drama.quick-images:${pid}/${eid}`;
  const [scope,setScope] = useState<'episode'|'project'>(() => stored(baseKey).scope || 'episode');
  const [filter,setFilter] = useState(() => stored(baseKey).filter || 'pending');
  const [focus,setFocus] = useState(() => stored(baseKey).focus || '');
  const [checked,setChecked] = useState<string[]>(() => stored(baseKey).checked || []);
  const [items,setItems] = useState<Item[]>([]), [busy,setBusy] = useState(false), [loading,setLoading] = useState(false);
  const [error,setError] = useState(''), [message,setMessage] = useState(''), [notes,setNotes] = useState('');
  const [results,setResults] = useState<Record<string,string>>({}), [gallery,setGallery] = useState<Item[] | null>(null);
  const [staleConfirm,setStaleConfirm] = useState<Item[] | null>(null);
  const [saved, setSaved] = useState<{label: string; id: string}[]>([]);
  const lock = useRef(false), epoch = useRef(0), alive = useRef(true), focusRef = useRef(focus);
  const actionKeys = useRef<Record<string,string>>({});
  useEffect(() => { alive.current=true; return () => { alive.current=false; ++epoch.current; }; }, []);
  useEffect(() => { focusRef.current=focus; }, [focus]);
  useEffect(() => { localStore.setItem(baseKey,JSON.stringify({scope,filter,focus,checked})); },[baseKey,scope,filter,focus,checked]);
  const read = useCallback(async () => {
    if (!pid) return;
    const token=++epoch.current; setLoading(true);
    try {
      const data=await get(`/projects/${segment(pid)}/image-review-queue?scope=${scope}&episodeId=${segment(eid)}`);
      if (!alive.current || token!==epoch.current) return;
      const next=data.items || []; setItems(next); setError('');
      setChecked(ids => ids.filter(id => next.some((i:Item) => identity(i)===id && !i.selected)));
      if (!next.some((i:Item) => slot(i)===focusRef.current)) { const nextFocus=next.length?slot(next.find(pending)||next[0]):''; focusRef.current=nextFocus;setFocus(nextFocus); }
    } catch(e:any) { if(alive.current && token===epoch.current) setError(e.message); }
    finally { if(alive.current && token===epoch.current) setLoading(false); }
  },[pid,eid,scope]);
  useEffect(() => { if(active) void read(); return () => { ++epoch.current; }; },[active,read]);
  useEffect(() => { if(!active) return; const timer=setInterval(() => {if(!lock.current) void read();},15000); return () => clearInterval(timer); },[active,read]);
  useEffect(() => {
    const focused = items.find(i => slot(i) === focus && i.targetType === 'shot');
    if (!pid || !focused) { setSaved([]); return; }
    let cancel = false;
    get(`/shots/${segment(focused.targetId)}/production`).then(data => {
      if (cancel) return;
      const snap = data.current?.snapshot || {};
      setSaved([
        ['已保存 · 场景', snap.sceneReferenceAssetId],
        ['已保存 · 动作', snap.actionReferenceAssetId],
        ['已保存 · 九宫格', snap.keyframeGridAssetId],
      ].flatMap(([label, id]) => id ? [{label, id}] : []));
    }).catch(() => { if (!cancel) setSaved([]); });
    return () => { cancel = true; };
  }, [pid, focus, items]);
  const groups = new Map<string,Item[]>();
  for(const i of items) {const key=slot(i); groups.set(key,[...(groups.get(key)||[]),i]);}
  const visibleGroups=[...groups].filter(([key,list]) => key===focus || filter==='all' || list.some(pending));
  const current=groups.get(focus) || [];
  const selected=items.filter(i => checked.includes(identity(i)));
  const ordered = (i: Item) => items.filter(x => slot(x) === slot(i)).slice().sort((a, b) =>
    (a.createdAt || 0) - (b.createdAt || 0) || String(identity(a)).localeCompare(String(identity(b))));
  const candidateNumber=(i:Item) => ordered(i).findIndex(x => identity(x) === identity(i)) + 1;
  const chooseFocus=(key:string) => {setFocus(key);setNotes('');setError('');};
  const nextPending=() => {const keys=[...groups.keys()]; const start=keys.indexOf(focus); const next=[...keys.slice(start+1),...keys.slice(0,start+1)].find(key=>key!==focus && groups.get(key)!.some(pending)); if(next) chooseFocus(next); else setMessage('没有其他待审目标；可以切换“全部结果”回看。');};
  const keyFor=(value:string) => actionKeys.current[value] || (actionKeys.current[value]=requestKey());

  const review=async(list:Item[],decision:'approved'|'rejected') => {
    if(lock.current || !list.length || notes.trim().length>2000) return;
    lock.current=true;setBusy(true);setError('');setMessage('');
    const ids=list.map(identity), text=notes.trim();
    const signature=JSON.stringify([ids,decision,text]);
    try {
      await post(`/projects/${segment(pid)}/image-reviews`,{candidateIds:ids,decision,notes:text},keyFor(signature));
      delete actionKeys.current[signature];
      if(!alive.current)return;
      setMessage(`已${decision==='approved'?'通过质量审核':'退回'} ${ids.length} 张；正式参考图未改变。`);
      setNotes('');setChecked(v=>v.filter(id=>!ids.includes(id)));
      await read();
    } catch(e:any) {if(alive.current)setError(e.message);}
    finally {lock.current=false;if(alive.current)setBusy(false);}
  };
  const adopt=async(list:Item[],acceptStale=false) => {
    if(lock.current)return;
    list=list.filter(i=>!items.find(x=>identity(x)===identity(i))?.selected);
    if(!list.length){setMessage('这些图片已经选用，无需重复保存。');return;}
    const slots=list.map(slot);
    if(new Set(slots).size!==slots.length){setError('同一参考用途只能选用一张。请取消同组中多余的勾选，再批量选用。');return;}
    if(!acceptStale && list.some(i=>i.stale&&!i.selected)){setStaleConfirm(list);return;}
    lock.current=true;setBusy(true);setError('');setMessage('');setStaleConfirm(null);
    const revisions=new Map<string,number>(),failedTargets=new Set<string>(), advancedTargets=new Set<string>(); let successes=0; const failed:string[]=[];
    // Freeze the page revisions before any write; only our own successful write
    // may advance the base when another slot belongs to the same entity.
    for(const i of list) revisions.set(`${i.targetType}:${i.targetId}`,i.currentRevision);
    try {
      for(const i of list) {
        if(!alive.current)break;
        const id=identity(i),target=`${i.targetType}:${i.targetId}`;
        try {
          if(failedTargets.has(target))throw new Error('同一资料的上一项未成功，请刷新核查后再处理。');
          const expectedRevision=revisions.get(target);
          if(!Number.isInteger(expectedRevision))throw new Error('目标版本缺失，请刷新后重试。');
          // A successful write by this batch may make a previously current
          // candidate old. CAS still rejects every external revision change.
          const acceptOwnAdvance=advancedTargets.has(target) && !i.stale && i.targetRevisionId===i.currentRevisionId;
          const confirmedStale=acceptStale || acceptOwnAdvance;
          const result=await selectImageCandidate(i,{...i,id}, {expectedRevision,acceptStale:confirmedStale,requestKey:keyFor(JSON.stringify([id,expectedRevision,confirmedStale]))});
          if(!alive.current)break;
          if(Number.isInteger(result.revision)) { revisions.set(target,result.revision); if(result.revision!==expectedRevision)advancedTargets.add(target); }
          ++successes; setResults(r=>({...r,[id]:i.targetType==='entity'?'已保存为资料参考草稿':'已选为本镜头画面'}));
          setChecked(v=>v.filter(x=>x!==id));
        }catch(e:any){failedTargets.add(target);failed.push(`${i.targetName} / 候选${candidateNumber(i)}：${e.message}`);setResults(r=>({...r,[id]:`未成功：${e.message}`}));}
      }
      if(!alive.current)return;
      await read();await ctx.refresh();
      setMessage(`已选用 ${successes} 张${failed.length?`，${failed.length} 张未成功（保留勾选）`:''}。${successes?'已选资料可前往资料页确认版本；已选镜头画面可用于准备视频。':'请核查未成功项后重试。'}`);
      if(failed.length)setError(failed.join('\n'));
    }catch(e:any){if(alive.current)setError(`保存后的状态刷新失败，请刷新核查：${e.message}`);}
    finally{lock.current=false;if(alive.current)setBusy(false);}
  };
  const saveToSlot=async(i:Item, kind:'scene'|'action') => {
    if(lock.current || i.targetType!=='shot') return;
    lock.current=true; setBusy(true); setError(''); setMessage('');
    const label=kind==='scene'?'场景':'动作';
    try {
      await post(`/shots/${segment(i.targetId)}/${kind}-reference-selection`, {
        expectedRevision: i.currentRevision, assetId: i.assetId,
      }, keyFor(JSON.stringify(['slot', kind, identity(i), i.currentRevision])));
      if(!alive.current) return;
      setResults(r=>({...r,[identity(i)]:`已存到${label}槽，没有改镜头主参考`}));
      setMessage(`已存到${label}槽。镜头主参考和人脸没有改。`);
      await ctx.refresh();
    } catch(e:any) { if(alive.current) setError(e.message); }
    finally { lock.current=false; if(alive.current) setBusy(false); }
  };
  const card=(i:Item,large=false) => {
    const latest=items.find(x=>identity(x)===identity(i)) || i, id=identity(i);
    const repeated=items.filter(x=>x.assetId&&x.assetId===i.assetId).length>1;
    return <article key={id} className="rounded-xl border bg-card p-3 space-y-3" aria-label={`${i.targetName} 候选 ${candidateNumber(i)}`}>
      <button className="block w-full" aria-label={`放大 ${i.targetName} 候选 ${candidateNumber(i)}`} onClick={()=>setGallery([latest])}><ReviewImage src={mediaUrl(pid,i.assetId)} alt={`${i.targetName} 候选 ${candidateNumber(i)}`} large={large}/></button>
      <div className="flex flex-wrap items-center justify-between gap-2"><strong className="text-sm">候选 {candidateNumber(i)}</strong><Badge variant={latest.selected?'default':'outline'}>{status(latest)}</Badge></div>
      <p className="text-xs text-muted-foreground">{i.model || '历史模型未记录'} · 种子 {i.seedText ?? i.seed ?? '未记录'}<br/>批次 {i.jobId?.slice(0,8)} · {i.createdAt?new Date(i.createdAt*1000).toLocaleString():''}</p>
      {i.promptExcerpt&&<p className="text-xs text-muted-foreground">{i.promptExcerpt}</p>}
      {repeated&&<p className="text-xs text-amber-300">和本组另一张是同一张图</p>}
      {latest.stale&&!latest.selected&&<p className="text-xs text-amber-300">来自旧版本，选用前需要确认</p>}
      <label className="flex items-center gap-2 text-sm"><Checkbox checked={checked.includes(id)} disabled={busy||latest.selected} aria-label={`勾选 ${i.targetName} 候选 ${candidateNumber(i)} ${i.jobId?.slice(0,8)}`} onCheckedChange={v=>setChecked(ids=>v?[...new Set([...ids,id])]:ids.filter(x=>x!==id))}/>加入批量操作</label>
      {i.targetType==='shot'&&<div className="flex flex-wrap gap-2"><Button variant="secondary" size="sm" disabled={busy} onClick={()=>void saveToSlot(latest,'scene')}>存到场景</Button><Button variant="secondary" size="sm" disabled={busy} onClick={()=>void saveToSlot(latest,'action')}>存到动作</Button></div>}
      <Button className="w-full" size="sm" disabled={busy||latest.selected} onClick={()=>void adopt([latest])}>{latest.selected?'当前已选':i.targetType==='shot'?'选用为本镜头主参考':'选为资料参考草稿'}</Button>
      {i.targetType==='shot'&&<p className="text-xs text-muted-foreground">选用为主参考会替换人脸画面。场景和动作用上面的按钮，不会改人脸。</p>}
      <div className="flex flex-wrap gap-2"><Button variant="outline" size="sm" disabled={busy} onClick={()=>void review([latest],'approved')}>质量通过</Button><Button variant="outline" size="sm" disabled={busy} onClick={()=>void review([latest],'rejected')}>退回</Button></div>
      {results[id]&&<p role="status" className="text-xs">{results[id]}</p>}
    </article>;
  };
  return <div className="space-y-5" aria-busy={busy}>
    <header className="flex flex-wrap justify-between gap-3"><div><h2 className="text-2xl font-semibold">快速审图</h2><p className="mt-2 text-sm text-muted-foreground">集中查看抽卡结果。勾选后批量处理；质量通过与正式选用分别保存。</p></div><Button variant="outline" disabled={busy||loading} onClick={()=>void read()}>{loading?'刷新中…':'刷新候选'}</Button></header>
    <div className="flex flex-wrap gap-4 items-center"><label className="text-sm">审核范围 <select aria-label="审核范围" className="rounded-md border bg-background p-2" disabled={busy} value={scope} onChange={e=>{setScope(e.target.value as any);setChecked([]);setFocus('');focusRef.current='';setItems([]);setNotes('');setGallery(null);setStaleConfirm(null);setResults({});setMessage('');}}><option value="episode">当前分集</option><option value="project">当前项目全部分集</option></select></label><label className="text-sm">显示 <select aria-label="审核筛选" className="rounded-md border bg-background p-2" value={filter} onChange={e=>setFilter(e.target.value)}><option value="pending">待审目标</option><option value="all">全部结果</option></select></label><span className="text-sm">{items.filter(i=>!pending(i)).length} / {items.length} 张已处理 · {items.filter(pending).length} 张待审</span></div>
    <section className="rounded-xl border bg-card p-4 space-y-3" aria-label="批量图片审核">
      <div className="flex flex-wrap items-center gap-2"><strong className="text-sm">已勾选 {selected.length} 张</strong><Button variant="outline" size="sm" disabled={busy||!current.length} onClick={()=>setChecked(ids=>[...new Set([...ids,...current.filter(pending).map(identity)])])}>勾选当前目标待审图</Button><Button variant="ghost" size="sm" disabled={busy||!checked.length} onClick={()=>setChecked([])}>清空勾选</Button></div>
      <label className="block text-sm">审图意见 <span className="text-muted-foreground">（选填，用于本次质量审核）</span><Textarea aria-label="审图意见" className="mt-2" value={notes} maxLength={2000} disabled={busy} onChange={e=>setNotes(e.target.value)} placeholder="可以直接通过或退回，也可以记录需要调整的地方。"/></label>
      <div className="flex flex-wrap gap-2"><Button disabled={busy||!selected.length} onClick={()=>void adopt(selected)}>批量选用勾选图片（{selected.length}）</Button><Button variant="secondary" disabled={busy||!selected.length} onClick={()=>void review(selected,'approved')}>批量质量通过</Button><Button variant="outline" disabled={busy||!selected.length} onClick={()=>void review(selected,'rejected')}>批量退回</Button></div>
      <p className="text-xs text-muted-foreground">同一参考用途只能选用一张。人物与场景选用后保存为草稿。镜头主参考会替换人脸画面；场景和动作请用卡片上的单独按钮。</p>
    </section>
    {error&&<p role="alert" className="whitespace-pre-wrap rounded-lg border border-destructive p-3 text-sm text-destructive">{error}</p>}{message&&<p role="status" className="rounded-lg border p-3 text-sm">{message}</p>}
    {!items.length&&!loading&&<p className="py-12 text-center text-muted-foreground">当前范围暂无图片候选。</p>}
    <div className="grid gap-5 md:grid-cols-[190px_minmax(0,1fr)]">
      <nav className="space-y-2 max-h-[35vh] md:max-h-[65vh] overflow-auto" aria-label="审图目标列表">{visibleGroups.map(([key,list])=><button key={key} disabled={busy} className={`w-full text-left border rounded-lg p-3 ${key===focus?'border-primary bg-primary/10':'bg-card'}`} onClick={()=>chooseFocus(key)}><strong className="block text-sm">{list[0].targetName}</strong><span className="text-xs text-muted-foreground">{roleNames[list[0].role||'shot']||list[0].role} · {list.filter(pending).length} 待审 / {list.length} 张</span></button>)}</nav>
      <section className="space-y-4 min-w-0" aria-label="当前审图目标">
        {current.length>0&&<><div className="flex flex-wrap items-start justify-between gap-3"><div><h3 className="font-semibold">{current[0].targetName} · {roleNames[current[0].role||'shot']||current[0].role}</h3><p className="text-xs text-muted-foreground mt-1">{current.filter(pending).length} 张待审 · {current.length-current.filter(pending).length} 张已处理，都在下面</p></div><div className="flex flex-wrap gap-2"><Button variant="outline" size="sm" disabled={busy} onClick={nextPending}>跳过 / 下一待审</Button><Button variant="outline" size="sm" onClick={()=>setGallery(current)}>对比当前目标图片</Button><Button size="sm" variant="secondary" disabled={busy} onClick={()=>ctx.navigate(current[0].targetType==='entity'?'materials':'shots',current[0].targetType==='entity'?{entityId:current[0].targetId}:{shotId:current[0].targetId,episodeId:current[0].episodeIds?.[0]||eid})}>查看目标与参考版本</Button></div></div>
        {saved.length>0&&<div className="space-y-2"><h4 className="text-sm font-medium">已入库参考，不是抽卡候选</h4><div className="grid gap-3 sm:grid-cols-3">{saved.map(row=><figure key={row.label} className="overflow-hidden rounded-lg border"><img src={mediaUrl(pid,row.id)} alt={row.label} className="h-40 w-full object-contain bg-black/20"/><figcaption className="px-2 py-1 text-xs text-muted-foreground">{row.label}</figcaption></figure>)}</div></div>}
        <div className="grid gap-4" style={{gridTemplateColumns:"repeat(auto-fit,minmax(min(100%,260px),1fr))"}}>{current.map(i=>card(i))}</div></>}
      </section>
    </div>
    <Dialog open={!!gallery} onOpenChange={v=>{if(!v&&!busy)setGallery(null);}}><DialogContent className="sm:max-w-6xl max-h-[90vh] overflow-auto"><DialogHeader><DialogTitle>候选图片对比</DialogTitle><DialogDescription>可在这里直接选用。保存成功后显示当前已选；资料参考仍需确认版本。</DialogDescription></DialogHeader><div className="grid gap-4 sm:grid-cols-2">{gallery?.map(i=>card(i,true))}</div><DialogFooter><Button variant="outline" disabled={busy} onClick={()=>setGallery(null)}>关闭对比</Button></DialogFooter></DialogContent></Dialog>
    <Dialog open={!!staleConfirm} onOpenChange={v=>{if(!v)setStaleConfirm(null);}}><DialogContent><DialogHeader><DialogTitle>选用旧版本候选？</DialogTitle><DialogDescription>以下图片来自旧内容版本，将保存为当前目标的新参考。其他参考用途保持不变。</DialogDescription></DialogHeader><ul className="text-sm list-disc pl-5">{staleConfirm?.filter(i=>i.stale&&!i.selected).map(i=><li key={identity(i)}>{i.targetName} · 候选 {candidateNumber(i)}</li>)}</ul><DialogFooter><Button variant="outline" onClick={()=>setStaleConfirm(null)}>返回检查</Button><Button onClick={()=>void adopt(staleConfirm||[],true)}>确认选用旧版本</Button></DialogFooter></DialogContent></Dialog>
  </div>;
}
