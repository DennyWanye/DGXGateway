import { useCallback, useEffect, useRef, useState } from 'react';
import { ArrowUpRight, ListTodo, LoaderCircle, RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Badge } from '@/components/ui/badge';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog';
import { get, post, segment } from '@/lib/api';
import type { WorkbenchContext } from '@/lib/types';
import { isResident, RESIDENT_MESSAGE, type ResourceStatus } from '@/lib/resource-status';

export interface StudioTask extends ResourceStatus {
 id: string; kind: string; taskKind?: string; projectId: string; projectTitle: string; episodeId?: string;
 targetId?: string; targetName?: string; targetType?: string; targetRevisionId?: string;
 state: string; stage?: string; createdAt?: number | string; updatedAt?: number | string;
 error?: unknown; cancelPath?: string; refreshPath?: string; gatewayJobId?: string; resultCount?: number; totalCount?: number;
 queuePosition?: number; waitReason?: string; memoryReleased?: boolean; cleanupRequired?: boolean;
}
const kinds: Record<string, string> = { image: '图片', video: '视频', text: '文字编写', export: '导出', audio: '声音', subtitle: '字幕', backup: '备份' };
const states: Record<string, string> = { QUEUED: '排队中', PENDING: '等待处理', RUNNING: '处理中', LOADING: '模型启动中', SUCCEEDED: '已完成', FAILED: '失败', CANCELED: '已取消', CANCELLED: '已取消', INTERRUPTED: '已中断', UNKNOWN: '待核查', BLOCKED: '等待处理', CANCEL_REQUESTED: '取消处理中' };
const stages: Record<string, string> = { QUEUED: '等待执行', LOADING: '正在启动模型', STARTING: '正在启动模型', GENERATING: '正在生成', RUNNING: '正在处理', DOWNLOADING: '正在下载结果', ARCHIVING: '正在保存结果', RELEASING: '正在处理模型资源', CANCELING: '正在取消', CANCELLING: '正在取消', SUCCEEDED: '处理完成', FAILED: '处理失败', CANCELED: '取消完成', UNKNOWN: '正在核查原任务' };
Object.assign(states, { PREPARED: '等待执行', WAITING: '等待执行', PAUSED: '已暂停', DISPATCHING: '正在提交', SUBMISSION_UNKNOWN: '提交待核查', RECONCILING: '正在核查', RECOVERY_REQUIRED: '需要恢复', OUTPUT_UNAVAILABLE: '结果暂不可用', OUTPUT_INVALID: '结果需检查' });
Object.assign(stages, states, { WAITING: '等待资源与执行顺序', PAUSED: '项目制作已暂停', PREPARED: '已保存任务，等待执行', DISPATCHING: '正在向模型服务提交', SUBMISSION_UNKNOWN: '提交回执未确认，请核查原任务', RECONCILING: '正在核查原任务', CANCEL_REQUESTED: '已请求取消，等待服务确认停止', OUTPUT_UNAVAILABLE: '结果暂不可用，请核查原任务', RECOVERY_REQUIRED: '需要核查并恢复任务状态' });
const needsAttention = (state: string) => ['FAILED', 'UNKNOWN', 'BLOCKED', 'INTERRUPTED', 'SUBMISSION_UNKNOWN', 'RECOVERY_REQUIRED', 'OUTPUT_UNAVAILABLE', 'OUTPUT_INVALID'].includes(state.toUpperCase());
const terminal = (state: string) => ['SUCCEEDED', 'FAILED', 'CANCELED', 'CANCELLED', 'INTERRUPTED', 'BLOCKED'].includes(state.toUpperCase());
const textError = (error: unknown) => typeof error === 'string' ? error : error && typeof error === 'object' && 'message' in error ? String(error.message) : error ? JSON.stringify(error) : '';
function time(value?: number | string) {
 if (!value) return '时间未提供';
 const date = new Date(typeof value === 'number' && value < 1e12 ? value * 1000 : value);
 return Number.isNaN(date.getTime()) ? '时间未提供' : date.toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit' });
}
export function taskDestination(task: StudioTask): { page: string; params: Record<string, string> } {
 const params: Record<string, string> = { jobId: task.id };
 if (task.targetId && task.targetType === 'shot') params.shotId = task.targetId;
 if (task.kind === 'image') {
  if (task.targetType && task.targetRevisionId) params.target = `${task.targetType}:${task.targetRevisionId}`;
  if (task.targetType === 'entity' && task.targetId) params.entityId = task.targetId;
  return { page: 'images', params };
 }
 if (task.kind === 'video') return { page: 'video', params: { ...params, view: 'review' } };
 if (task.kind === 'text' && task.taskKind === 'draft_video_prompt') return { page: 'video', params: { ...params, promptJobId: task.id } };
 if (task.kind === 'text') return { page: 'story', params };
 if (task.kind === 'backup') return { page: 'backup', params };
 return { page: 'edit', params };
}
export function TaskPanel({ ctx, active, onOpenTask }: { ctx: WorkbenchContext; active: boolean; onOpenTask(task: StudioTask): void }) {
 const [scope, setScope] = useState('project'), [kind, setKind] = useState('all'), [state, setState] = useState('all');
 const [tasks, setTasks] = useState<StudioTask[]>([]), [error, setError] = useState(''), [loading, setLoading] = useState(false), [lastRead, setLastRead] = useState<number | null>(null);
 const [confirm, setConfirm] = useState<StudioTask | null>(null), [canceling, setCanceling] = useState(false), [cancelError, setCancelError] = useState('');
 const [checking, setChecking] = useState<string | null>(null);
 const checkLock = useRef(false);
 const [cancelSent, setCancelSent] = useState<Record<string, boolean>>({});
 const focusedTask = useRef('');
 const current = useRef(0), inFlight = useRef(false), cancelLock = useRef(false);
 useEffect(() => { if (active && ctx.params?.kind && kinds[ctx.params.kind]) setKind(ctx.params.kind); }, [active, ctx.params?.kind]);
 useEffect(() => {
  if (!active) { focusedTask.current = ''; return; }
  if (!ctx.params?.jobId || focusedTask.current === ctx.params.jobId) return;
  const task = tasks.find(item => item.id === ctx.params.jobId);
  const element = task && document.getElementById(`studio-task-${task.kind}-${task.id}`);
  if (task && element) { focusedTask.current = task.id; element.scrollIntoView({ block: 'center', behavior: 'smooth' }); }
 }, [active, ctx.params?.jobId, tasks, kind, loading]);
 const endpoint = scope === 'all' ? '/tasks' : `/tasks?projectId=${segment(ctx.project.id)}`;
 const refresh = useCallback(async () => {
  if (inFlight.current) return;
  const generation = current.current; inFlight.current = true; setLoading(true);
  try {
   const result = await get(endpoint);
   if (generation !== current.current) return;
   setTasks(result.tasks || []); setError(''); setLastRead(Date.now()); return true;
  } catch (e: any) { if (generation === current.current) setError(e.message || '暂时无法读取任务。'); }
  finally { if (generation === current.current) { inFlight.current = false; setLoading(false); } }
 }, [endpoint]);
 useEffect(() => {
  ++current.current; inFlight.current = false; setLoading(false); setTasks([]); setLastRead(null); setError('');
  if (!active) return;
  let stopped = false, timer: ReturnType<typeof setTimeout>;
  const poll = async () => { if (document.visibilityState !== 'hidden') await refresh(); if (!stopped) timer = setTimeout(poll, 5000); };
  void poll();
  return () => { stopped = true; clearTimeout(timer); ++current.current; inFlight.current = false; };
 }, [active, refresh]);
 const cancel = async () => {
  if (!confirm?.cancelPath || cancelLock.current) return;
  const task = confirm; cancelLock.current = true; setCanceling(true); setCancelError('');
  try {
   await post(task.cancelPath);
   setCancelSent(previous => ({ ...previous, [task.id]: true })); setConfirm(null);
   ctx.notify('已发送取消请求。任务停止与资源释放的结果将在这里更新。', 'info');
   await refresh();
  } catch (e: any) { setCancelError(e.message || '取消请求未确认，请先核查任务状态。'); }
  finally { cancelLock.current = false; setCanceling(false); }
 };
 const reconcile = async (task: StudioTask) => {
  if (!task.refreshPath || checkLock.current) return;
  checkLock.current = true; setChecking(task.id);
  try { await post(task.refreshPath); const updated = await refresh(); ctx.notify(updated ? '已核查原任务，请查看最新状态。' : '已核查原任务，列表尚未更新，请刷新核对。', 'info'); }
  catch (e: any) { ctx.notify(e.message || '核查失败，原任务仍保留。', 'error'); }
  finally { checkLock.current = false; setChecking(null); }
 };
 const filtered = tasks.filter(task => (kind === 'all' || task.kind === kind) && (state === 'all' || (state === 'active' ? !terminal(task.state) : state === 'problem' ? needsAttention(task.state) : task.state.toUpperCase() === state)));
 const activeCount = tasks.filter(task => !terminal(task.state)).length;

 return <div className="space-y-6">
  <div className="flex flex-wrap items-end justify-between gap-4"><div><p className="text-xs tracking-widest text-muted-foreground mb-2">制作进度</p><h1 className="text-2xl font-semibold">生成任务</h1><p className="mt-2 text-sm text-muted-foreground">查看图片、视频及其他制作任务，取消不需要的任务，再回到结果页继续。</p></div><Button variant="outline" disabled={loading} onClick={() => void refresh()}><RefreshCw className={loading ? 'size-4 animate-spin' : 'size-4'}/>刷新任务</Button></div>
  <div className="flex flex-wrap items-end gap-3 rounded-xl border bg-card p-4">
   <div className="space-y-2"><p className="text-xs text-muted-foreground">任务范围</p><Select value={scope} onValueChange={setScope}><SelectTrigger aria-label="任务范围" className="min-w-40"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="project">当前项目 · {ctx.project.title}</SelectItem><SelectItem value="all">全部项目</SelectItem></SelectContent></Select></div>
   <div className="space-y-2"><p className="text-xs text-muted-foreground">制作类型</p><Select value={kind} onValueChange={setKind}><SelectTrigger aria-label="制作类型" className="min-w-32"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">全部类型</SelectItem>{Object.entries(kinds).map(([value, label]) => <SelectItem key={value} value={value}>{label}</SelectItem>)}</SelectContent></Select></div>
   <div className="space-y-2"><p className="text-xs text-muted-foreground">任务状态</p><Select value={state} onValueChange={setState}><SelectTrigger aria-label="任务状态" className="min-w-36"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="all">全部状态</SelectItem><SelectItem value="active">进行中 / 待核查</SelectItem><SelectItem value="SUCCEEDED">已完成</SelectItem><SelectItem value="problem">需要处理</SelectItem><SelectItem value="CANCELED">已取消</SelectItem></SelectContent></Select></div>
   <div className="ml-auto text-xs text-muted-foreground leading-6">{tasks.length} 个任务 · {activeCount} 个进行中或待核查<br/>{lastRead ? `最近更新 ${time(lastRead)}` : loading ? '正在读取任务…' : '尚未读取'}</div>
  </div>
  {error && <Alert variant="destructive"><AlertDescription>{error} {lastRead ? '以下保留上次读取的任务，当前状态可能已变化。' : '请重新连接后刷新任务。'}</AlertDescription></Alert>}
  {!error && <p className="text-xs text-muted-foreground" role="status">任务记录保存在服务端，关闭页面后仍可查看。完成的视频还需要审片和选用。</p>}
  {!filtered.length ? <div className="rounded-xl border border-dashed py-16 text-center text-muted-foreground"><ListTodo className="mx-auto mb-4 size-8 opacity-50"/><p>{loading && !lastRead ? '正在读取任务…' : '这个范围内还没有匹配的任务'}</p><p className="mt-2 text-xs">可以调整筛选，或到图片制作、生成与审片页面提交。</p></div> : <div className="space-y-3">{filtered.map(task => {
   const status = task.state.toUpperCase(), finished = terminal(task.state), pendingCancel = cancelSent[task.id] && !finished;
   const stage = task.stage ? stages[task.stage.toUpperCase()] || '服务状态待核查' : '';
   return <article id={`studio-task-${task.kind}-${task.id}`} key={`${task.kind}:${task.id}`} aria-label={`${kinds[task.kind] || task.kind}任务 ${task.targetName || task.id}`} className={`rounded-xl border bg-card p-5 space-y-4 ${ctx.params?.jobId === task.id ? 'border-primary/60 ring-1 ring-primary/20' : ''}`}>
    <div className="flex flex-wrap items-start gap-3"><div className="min-w-0 flex-1"><div className="flex flex-wrap items-center gap-2"><Badge variant="outline">{kinds[task.kind] || task.kind}</Badge><h2 className="text-sm font-medium break-words">{task.targetName || `${kinds[task.kind] || '制作'}任务`}</h2><Badge variant={['FAILED', 'INTERRUPTED'].includes(status) ? 'destructive' : 'secondary'}>{states[status] || '状态待核查'}</Badge>{typeof task.queuePosition === 'number' && task.queuePosition > 0 && <Badge variant="outline">队列第 {task.queuePosition} 位</Badge>}</div><p className="mt-2 text-xs text-muted-foreground">{task.projectTitle} · 提交于 {time(task.createdAt)}</p></div><div className="flex flex-wrap gap-2">{task.refreshPath && <Button variant="ghost" size="sm" disabled={!!checking || canceling} onClick={() => void reconcile(task)}>{checking === task.id ? '正在核查…' : '核查原任务'}</Button>}<Button variant="outline" size="sm" onClick={() => onOpenTask(task)}>{(task.resultCount || 0) > 0 ? '查看结果' : '查看制作页'}<ArrowUpRight className="size-3.5"/></Button>{task.cancelPath && !finished && <Button variant="ghost" size="sm" disabled={pendingCancel || canceling} onClick={() => { setCancelError(''); setConfirm(task); }}>{pendingCancel ? '取消请求已发送' : '取消任务'}</Button>}</div></div>
    {task.waitReason && <p className="text-xs text-muted-foreground">{task.waitReason}</p>}
    {(stage || pendingCancel) && <p className="text-sm text-muted-foreground">{pendingCancel ? '已请求取消，等待服务确认停止。' : stage}</p>}
    {['UNKNOWN', 'SUBMISSION_UNKNOWN', 'RECOVERY_REQUIRED'].includes(status) && <p className="text-xs text-amber-300">任务结果尚未确认，请核查原任务，避免重复提交。</p>}
    {textError(task.error) && <p className="rounded-lg bg-destructive/10 p-3 text-sm text-destructive break-words">{textError(task.error)}</p>}
    {isResident(task) && <p className="text-xs text-muted-foreground">{RESIDENT_MESSAGE}。需要立即释放时，可前往 Gateway 的模型复用与内存释放面板。</p>}
    {finished && !!task.gatewayJobId && task.memoryReleased === false && !task.cleanupRequired && !isResident(task) && <p className="text-xs text-amber-300">任务已结束，仍在等待确认模型资源释放。</p>}
    {task.cleanupRequired && <p className="text-xs text-amber-300">任务仍需清理资源，尚未确认释放。</p>}
    <div className="flex flex-wrap justify-between gap-2 text-[11px] text-muted-foreground"><span className="break-all">任务 {task.id}{task.gatewayJobId && ` · Gateway ${task.gatewayJobId}`}</span><span>{task.totalCount ? `已归档 ${task.resultCount || 0} / ${task.totalCount} 个候选` : `${task.resultCount || 0} 个结果`}{task.memoryReleased === true ? ' · 模型资源已释放' : ''}</span></div>
   </article>;
  })}</div>}
  <Dialog open={!!confirm} onOpenChange={open => { if (!open && !canceling) setConfirm(null); }}><DialogContent aria-busy={canceling}><DialogHeader><DialogTitle>取消这个任务？</DialogTitle><DialogDescription>将取消“{confirm?.targetName || confirm?.id}”。已经生成的结果仍保留；正在运行的任务可能需要等待后端停止并释放资源。</DialogDescription></DialogHeader>{cancelError && <Alert variant="destructive"><AlertDescription>{cancelError}</AlertDescription></Alert>}<DialogFooter><Button variant="outline" disabled={canceling} onClick={() => setConfirm(null)}>保留任务</Button><Button variant="destructive" disabled={canceling || !!cancelError} onClick={() => void cancel()}>{canceling && <LoaderCircle className="size-4 animate-spin"/>}{canceling ? '正在发送取消…' : '确认取消任务'}</Button></DialogFooter>{cancelError && <p className="text-xs text-muted-foreground">关闭对话框后刷新任务，再决定下一步；不会自动重试取消。</p>}</DialogContent></Dialog>
 </div>;
}
