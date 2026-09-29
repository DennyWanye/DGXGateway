import { useEffect, useRef, useState, type ReactNode } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { mediaUrl } from '@/lib/api';
import { Badge } from '@/components/ui/badge';

type Any = Record<string, any>;
export type ReviewEntry = { job: Any; item: Any; take: Any; assetId: string };
export function reviewGroups(shots: Any[], jobs: Any[], takes: Record<string, Any>, episodeId: string) {
  const entries = new Map<string, ReviewEntry[]>();
  const seen = new Set<string>();
  for (const job of jobs) {
    if (job.episodeId !== episodeId) continue;
    for (const item of job.items || []) {
      const take = takes[item.takeId] || {};
      const assetId = item.assetId || take.assetId;
      if (!item.takeId || !assetId || seen.has(item.takeId)) continue;
      seen.add(item.takeId);
      const list = entries.get(item.shotId) || [];
      list.push({ job, item, take, assetId });
      entries.set(item.shotId, list);
    }
  }
  return shots.map((shot, index) => {
    const candidates = entries.get(shot.id) || [];
    // Imported takes also belong to the shot, even when there is no generation job.
    for (const original of shot.takes || []) {
      if (seen.has(original.id) || !original.assetId) continue;
      const take = takes[original.id] || { ...original, selectedTakeId: shot.selectedTakeId };
      candidates.push({ job: { episodeId }, item: { ...original, takeId: original.id, shotId: shot.id }, take, assetId: original.assetId });
    }
    const selected = candidates.find(c => (Object.hasOwn(c.take, "selectedTakeId") ? c.take.selectedTakeId : shot.selectedTakeId) === c.item.takeId);
    const pending = candidates.filter(c => (c.take.reviewStatus || c.item.reviewStatus || 'pending') === 'pending');
    return { shot, code: `S${String(index + 1).padStart(2, '0')}`, candidates, selected, pending };
  });
}

export function ReviewWorkspace({ shots, jobs, takes, episodeId, projectId, target, busy, renderCandidate, onPrepare, onRedo, onEdit }: {
  shots: Any[]; jobs: Any[]; takes: Record<string, Any>; episodeId: string; projectId?: string;
  target?: Record<string, string>; busy: boolean;
  renderCandidate: (entry: ReviewEntry, title: string) => ReactNode;
  onRedo?: (entry: ReviewEntry, reason: string) => void;
  onPrepare: (shotId: string) => void; onEdit: () => void;
}) {
  const [shotId, setShotId] = useState('');
  const [candidateId, setCandidateId] = useState('');
  const [query, setQuery] = useState('');
  const [compareOpen, setCompareOpen] = useState(false);
  const [compareId, setCompareId] = useState(''), [reason, setReason] = useState('');
  const [pendingOnly, setPendingOnly] = useState(false);
  const groups = reviewGroups(shots, jobs, takes, episodeId);
  const handledTarget = useRef('');
  const displayed = useRef<{ shotId: string; takeId: string } | null>(null);
  const reviewPanel = useRef<HTMLElement>(null);
  const revealReview = () => requestAnimationFrame(() => {
    reviewPanel.current?.scrollIntoView?.({ block: 'start', behavior: 'smooth' });
  });
  useEffect(() => {
    const key = JSON.stringify([episodeId, target?.shotId, target?.takeId, target?.jobId]);
    if (handledTarget.current === key) return;
    const item = jobs.find(j => j.id === target?.jobId && j.episodeId === episodeId)?.items?.find((i: Any) => i.takeId);
    const sid = target?.shotId || item?.shotId;
    if (target?.jobId && !sid) return;
    handledTarget.current = key;
    if (sid) {
      setShotId(sid); setCandidateId(target?.takeId || item?.takeId || '');
      setPendingOnly(false); setQuery('');
      setCompareOpen(false); setCompareId(''); setReason('');
      revealReview();
    }
  }, [target?.shotId, target?.takeId, target?.jobId, jobs, episodeId]);
  const filtered = groups.filter(g => (!pendingOnly || g.pending.length > 0) && `${g.code} ${g.shot.title}`.toLowerCase().includes(query.trim().toLowerCase()));
  const selected = filtered.find(g => g.shot.id === shotId) || filtered.find(g => g.shot.id === displayed.current?.shotId) || filtered.find(g => g.pending.length) || filtered[0];
  const candidate = selected?.candidates.find(c => c.item.takeId === candidateId) || selected?.candidates.find(c => selected.shot.id === displayed.current?.shotId && c.item.takeId === displayed.current?.takeId) || selected?.pending[0] || selected?.selected || selected?.candidates[0];
  // Polling can prepend a new batch or change pending status. Keep the visible
  // take until the user navigates, rather than replacing a video mid-review.
  useEffect(() => {
    if (selected && candidate) displayed.current = { shotId: selected.shot.id, takeId: candidate.item.takeId };
  }, [selected?.shot.id, candidate?.item.takeId]);
  const choose = (id: string) => { displayed.current = null; setShotId(id); setCandidateId(''); setCompareId(''); setCompareOpen(false); setReason(''); revealReview(); };
  const comparison = selected?.candidates.find(c => c.item.takeId === compareId && c.item.takeId !== candidate?.item.takeId) || selected?.candidates.find(c => c.item.takeId !== candidate?.item.takeId);
  const comparing = compareOpen && !!candidate && !!comparison;
  useEffect(() => {
    if (comparing && comparison && compareId !== comparison.item.takeId) setCompareId(comparison.item.takeId);
  }, [comparing, comparison?.item.takeId, compareId]);
  const next = selected && [...groups.slice(groups.indexOf(selected) + 1), ...groups.slice(0, groups.indexOf(selected))].find(g => g.pending.length > 0);
  return <div className="grid gap-4 lg:grid-cols-[240px_minmax(0,1fr)]" aria-label="按镜头审片">
    <aside className="space-y-3 rounded-xl border bg-card p-3">
      <div className="flex items-center justify-between"><span className="font-medium">本集镜头</span><Badge variant="secondary">{groups.filter(g => g.selected).length}/{groups.length} 已采用</Badge></div>
      <Input aria-label="搜索镜头" placeholder="搜索 S03 或镜头名称" value={query} onChange={e => setQuery(e.target.value)} />
      <Button variant={pendingOnly ? 'default' : 'outline'} size="sm" aria-pressed={pendingOnly} onClick={() => setPendingOnly(!pendingOnly)}>只看待审（{groups.reduce((n, g) => n + g.pending.length, 0)}）</Button>
      <nav aria-label="审片镜头列表" className="max-h-[60vh] space-y-1 overflow-y-auto">
        {filtered.map(g => <button type="button" disabled={busy} key={g.shot.id} aria-current={selected?.shot.id === g.shot.id ? 'true' : undefined} onClick={() => choose(g.shot.id)} className={`w-full rounded-lg border p-3 text-left transition ${selected?.shot.id === g.shot.id ? 'border-primary/50 bg-primary/10' : 'border-transparent hover:bg-muted'}`}>
          <span className="block text-sm font-medium">{g.code} · {g.shot.title}</span>
          <span className="mt-1 block text-xs text-muted-foreground">{g.candidates.length} 个候选 · {g.selected ? '已采用' : g.pending.length ? '待审片' : g.candidates.length ? '待选用' : '尚无视频'}</span>
        </button>)}
      </nav>
      {!filtered.length && <p className="py-4 text-sm text-muted-foreground">没有符合条件的镜头。可清空搜索或关闭待审筛选。</p>}
    </aside>
    <section ref={reviewPanel} className="min-w-0 scroll-mt-24 space-y-4 rounded-xl border bg-card p-4" aria-label="当前镜头审片">
      {selected ? <>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div><h3 className="font-semibold">{selected.code} · {selected.shot.title}</h3><p className="mt-1 text-xs text-muted-foreground">所有批次的候选集中在这里；审片意见对应当前视频。</p></div>
          <Button variant="outline" size="sm" disabled={busy || !next} onClick={() => { setQuery(''); if (next) choose(next.shot.id); }}>下一待审镜头</Button>
        </div>
        {!!selected.candidates.length && <div className="flex flex-wrap gap-2" role="group" aria-label="选择视频候选">{selected.candidates.map((c, i) => <Button key={c.item.takeId} size="sm" variant={candidate?.item.takeId === c.item.takeId ? 'default' : 'outline'} disabled={busy} aria-pressed={candidate?.item.takeId === c.item.takeId} onClick={() => { setShotId(selected.shot.id); setCandidateId(c.item.takeId); }}>候选 {i + 1}{selected.selected?.item.takeId === c.item.takeId ? ' · 已采用' : ` · ${({ approved: '已通过', rejected: '已拒绝', pending: '待审' } as Record<string, string>)[c.take.reviewStatus || c.item.reviewStatus || 'pending'] || '待审'}`}</Button>)}</div>}
        {candidate && selected.candidates.length > 1 && <details open={compareOpen} onToggle={e=>setCompareOpen(e.currentTarget.open)} className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">并排对比两个候选</summary><div className="mt-3 flex flex-wrap gap-2">{selected.candidates.filter(c=>c.item.takeId!==candidate.item.takeId).map(c=><Button size="sm" variant={compareId===c.item.takeId?'default':'outline'} key={c.item.takeId} onClick={()=>setCompareId(c.item.takeId)}>对比候选 {selected.candidates.indexOf(c)+1}</Button>)}</div>{comparing && <div className="mt-3 grid gap-3 md:grid-cols-2">{[candidate, comparison].filter(Boolean).map(c=><figure key={c!.item.takeId}><video controls preload="metadata" className="aspect-video w-full rounded bg-black" src={mediaUrl(projectId || '', c!.assetId)} aria-label={`对比候选 ${selected.candidates.indexOf(c!)+1}`} onPlay={e=>{for(const v of e.currentTarget.parentElement?.parentElement?.querySelectorAll('video') || []) if(v!==e.currentTarget)v.pause();}} /><figcaption className="mt-2 text-xs">候选 {selected.candidates.indexOf(c!)+1} · 种子 {c!.item.inputSnapshot?.seed ?? c!.item.seed ?? '历史未记录'} <Button size="sm" variant="ghost" onClick={()=>{setCandidateId(c!.item.takeId);setCompareId('');setCompareOpen(false);}}>定位此候选审片</Button></figcaption></figure>)}</div>}</details>}
        {candidate ? (comparing ? <p className="text-xs text-muted-foreground">对比后点击“定位此候选审片”，再填写意见并采用。</p> : renderCandidate(candidate, selected.shot.title)) : <div className="py-12 text-center text-sm text-muted-foreground">这个镜头还没有可播放的视频。<Button className="mx-auto mt-4 block" variant="outline" disabled={busy} onClick={() => onPrepare(selected.shot.id)}>准备此镜头</Button></div>}
        {candidate && onRedo && <details className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">质量不满意，准备新的候选</summary><p className="mt-2 text-xs text-muted-foreground">保留现有结果，带入当前镜头参考与原提示词；填写具体问题后，到准备页核对并提交。这不是重试失败任务。</p><Input className="my-2" aria-label="重新生成要改进什么" placeholder="例如：保留衣服，拳头接触前不要增加撞击声" value={reason} onChange={e=>setReason(e.target.value)} /><Button size="sm" variant="outline" disabled={busy || !reason.trim()} onClick={()=>onRedo(candidate,reason)}>带着改进要求准备生成</Button></details>}
        {selected.selected && <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-3 text-sm"><span>已有选片，可以加入剪辑。</span><Button variant="outline" onClick={onEdit}>去剪辑与导出</Button></div>}
      </> : <p className="py-12 text-center text-muted-foreground">选择一个镜头开始审片。</p>}
    </section>
  </div>;
}
