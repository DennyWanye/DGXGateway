import { workspaceIds, returnToOriginalWorkspace, discardEditableDrafts, localStore as localStorage } from "@/lib/workspace";
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { Film, ListTodo, LayoutDashboard, Users, ImagePlus, Clapperboard, WandSparkles, Scissors, Plus, ArrowUpRight, ArrowLeft, ChevronRight, RefreshCw, Menu, Check, Circle, LoaderCircle, FolderOpen, BookOpen, Music, X } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Badge } from '@/components/ui/badge';
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from '@/components/ui/dialog';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Alert, AlertDescription } from '@/components/ui/alert';
import { Toaster, toast } from 'sonner';
import { ProductionPanel } from './ProductionPanel';
import { VideoPanel } from './VideoPanel';
import { QuickImageReviewPanel } from './QuickImageReviewPanel';
import { EditPanel } from './EditPanel';
import { StoryPanel } from './StoryPanel';
import { SongPanel } from './SongPanel';
import { BackupPanel } from './BackupPanel';
import { TaskPanel, taskDestination, type StudioTask } from './TaskPanel';
import { get, post, segment, mediaUrl } from '@/lib/api';
import type { WorkbenchContext } from '@/lib/types';
import { cn } from '@/lib/utils';

const NAV = [
 {id:'overview',label:'制作概览',hint:'项目进度与下一步',icon:LayoutDashboard},
 {id:'story',label:'故事与剧本',hint:'从原文到对白与镜头提案',icon:BookOpen},
 {id:'song',label:'歌曲',hint:'绑定本集主时钟',icon:Music},
 {id:'materials',label:'人物与场景',hint:'建立你的故事世界',icon:Users},
 {id:'images',label:'图片制作',hint:'生成与选择参考画面',icon:ImagePlus},
 {id:'shots',label:'镜头设计',hint:'构图、动作与时长',icon:Clapperboard},
 {id:'image-review',label:'快速审图',hint:'集中审核与批量选择图片',icon:ImagePlus},
 {id:'video',label:'生成与审片',hint:'制作视频，选出好镜头',icon:WandSparkles},
 {id:'tasks',label:'生成任务',hint:'统一查看进度、取消与结果',icon:ListTodo},
 {id:'backup',label:'备份与恢复',hint:'保存项目与独立恢复',icon:FolderOpen},
 {id:'edit',label:'剪辑与导出',hint:'将镜头连接成故事',icon:Scissors},
];
function preference() {try{return JSON.parse(localStorage.getItem('dgx.drama.last-selection.v1')||'{}')}catch{return {}}}
function dirty() {return !!document.querySelector('[data-dirty="true"],[data-production-dirty="true"]')}
export function App() {
 const [projects,setProjects]=useState<any[]>([]),[project,setProject]=useState<any>(null),[episodes,setEpisodes]=useState<any[]>([]),[episode,setEpisode]=useState<any>(null);
 const [assets,setAssets]=useState<any[]>([]),[shots,setShots]=useState<any[]>([]),[entities,setEntities]=useState<any[]>([]),[capabilities,setCapabilities]=useState<any>({});
 const [page,setPage]=useState('overview'),[params,setParams]=useState<Record<string,string>>({}),[productionMode,setProductionMode]=useState<'materials'|'shots'|'images'>('materials');
 const [busy,setBusy]=useState(true),[failure,setFailure]=useState(''),[mobile,setMobile]=useState(false),[notice,setNotice]=useState<{text:string;tone:string}|null>(null);
 const [create,setCreate]=useState<'project'|'episode'|null>(null),[name,setName]=useState(''),[preset,setPreset]=useState('landscape'),[kind,setKind]=useState('drama'),[saving,setSaving]=useState(false);
 const [canvasOpen,setCanvasOpen]=useState(false),[canvasPreset,setCanvasPreset]=useState('1920x1080');
 const [narrow,setNarrow]=useState(()=>window.matchMedia('(max-width: 767px)').matches);
 const sidebarRef=useRef<HTMLElement>(null);
 const mainRef=useRef<HTMLElement>(null);
 useLayoutEffect(()=>{if(mainRef.current)mainRef.current.scrollTop=0},[page,params,project?.id,episode?.id]);
 const [discard,setDiscard]=useState<null|(()=>void)>(null);
 const discardOrigin=useRef<{projectId:string;episodeId:string}|null>(null);
 const confirmedWorkspaceLeave=useRef(false);
 const workspaceLeaveTimer=useRef<number|null>(null);
 useEffect(()=>{const query=window.matchMedia('(max-width: 767px)');const update=()=>{setNarrow(query.matches);if(!query.matches)setMobile(false)};query.addEventListener('change',update);return()=>query.removeEventListener('change',update)},[]);
 useEffect(()=>{if(!mobile||!narrow)return;const previous=document.activeElement as HTMLElement;const panel=sidebarRef.current;const focusable=()=>Array.from(panel?.querySelectorAll<HTMLElement>('button:not(:disabled),[role=combobox]:not([disabled])')||[]).filter(e=>e.getClientRects().length);focusable()[0]?.focus();const trap=(event:KeyboardEvent)=>{if(document.querySelector('[data-slot=select-content],[data-slot=dialog-content]'))return;if(event.key==='Escape'){event.preventDefault();event.stopImmediatePropagation();setMobile(false)}if(event.key==='Tab'){const nodes=focusable(),first=nodes[0],last=nodes.at(-1);if(event.shiftKey&&document.activeElement===first){event.preventDefault();last?.focus()}else if(!event.shiftKey&&document.activeElement===last){event.preventDefault();first?.focus()}}};document.addEventListener('keydown',trap,true);return()=>{document.removeEventListener('keydown',trap,true);previous?.focus()}},[mobile,narrow]);
 const ids=useRef({projectId:'',episodeId:''}),epoch=useRef(0),saveLock=useRef(false);
 const notify=useCallback((text:string,tone:'error'|'success'|'info'='success')=>{setNotice({text,tone});if(tone==='error')toast.error(text);else if(tone==='success')toast.success(text);else toast(text)},[]);
 const remember=(pid:string,eid:string)=>{try{localStorage.setItem('dgx.drama.last-selection.v1',JSON.stringify({projectId:pid,episodeId:eid}))}catch{}};
 const load=useCallback(async(pid?:string,eid?:string)=>{
  const token=++epoch.current;setBusy(true);setFailure('');
  try{
   const [list,caps]=await Promise.all([get('/projects'),get('/capabilities')]);if(token!==epoch.current)return;
   if(!caps.enabled)throw new Error('短剧服务尚未启用。请检查候选服务配置后重试。');
   setCapabilities(caps);setProjects(list.projects||[]);
   const chosen=(list.projects||[]).find((p:any)=>p.id===pid)||(list.projects||[])[0];
   if(!chosen){ids.current={projectId:'',episodeId:''};setProject(null);setEpisode(null);setEpisodes([]);setAssets([]);setShots([]);setEntities([]);return;}
   const [data,entityData]=await Promise.all([get(`/projects/${segment(chosen.id)}`),get(`/projects/${segment(chosen.id)}/entities`)]);if(token!==epoch.current)return;
   const ep=(data.episodes||[]).find((e:any)=>e.id===eid)||(data.episodes||[])[0];
   const epData=ep?await get(`/episodes/${segment(ep.id)}`):null;if(token!==epoch.current)return;
   const origin=discardOrigin.current;
   if(origin&&(origin.projectId!==chosen.id||origin.episodeId!==(ep?.id||'')))discardEditableDrafts(origin.projectId,origin.episodeId);
   discardOrigin.current=null;
   setProject(data.project);setEpisodes(data.episodes||[]);setAssets(data.assets||[]);setEntities(entityData.entities||[]);setEpisode(epData?.episode||null);setShots(epData?.shots||[]);
   ids.current={projectId:chosen.id,episodeId:ep?.id||''};remember(chosen.id,ep?.id||'');
   return {projectId:chosen.id,episodeId:ep?.id||''};
  }catch(e:any){if(token===epoch.current){discardOrigin.current=null;setFailure(e.message);notify(e.message,'error')}}finally{if(token===epoch.current)setBusy(false)}
 },[notify]);
 const refresh=useCallback(async()=>{
  const {projectId:pid,episodeId:eid}=ids.current;if(!pid)return;
  const token=epoch.current;
  const [data,ent,ep]=await Promise.all([get(`/projects/${segment(pid)}`),get(`/projects/${segment(pid)}/entities`),eid?get(`/episodes/${segment(eid)}`):Promise.resolve(null)]);
  if(token!==epoch.current||ids.current.projectId!==pid||ids.current.episodeId!==eid)return;
  setProject(data.project);setAssets(data.assets||[]);setEpisodes(data.episodes||[]);setEntities(ent.entities||[]);if(ep){setEpisode(ep.episode);setShots(ep.shots||[])}
 },[]);
 const guard=(action:()=>void,busyMessage='正在处理当前操作，请等待回执后再切换项目或分集。')=>{if(document.querySelector('[aria-busy="true"]')){notify(busyMessage,'info');return;}if(dirty())setDiscard(()=>action);else action()};
 const leaveRestoredWorkspace=()=>guard(()=>{
  if(document.querySelector('[aria-busy="true"]')){notify('正在处理当前操作，请等待回执后再返回原工作区。','info');return;}
  document.querySelectorAll<HTMLMediaElement>('video,audio').forEach(el=>el.pause());
  confirmedWorkspaceLeave.current=true;
  if(workspaceLeaveTimer.current!==null)window.clearTimeout(workspaceLeaveTimer.current);
  workspaceLeaveTimer.current=window.setTimeout(()=>{confirmedWorkspaceLeave.current=false;discardOrigin.current=null;workspaceLeaveTimer.current=null},1000);
  try{returnToOriginalWorkspace()}catch{confirmedWorkspaceLeave.current=false;discardOrigin.current=null;notify('无法返回原工作区，当前草稿已保留。','error')}
 },'正在处理当前操作，请等待回执后再返回原工作区。');
 const navigate=useCallback((next:string,p:Record<string,string>={})=>{
  const id=next==='prepare'?'materials':next;
  if(!NAV.some(x=>x.id===id))return;
  if(id==='song' && project?.kind!=='mv')return;
  if(p.episodeId && p.episodeId!==ids.current.episodeId){
   if(document.querySelector('[aria-busy="true"]')){notify('请等待当前操作完成后再切换分集。','info');return;}
   const go=()=>{void load(ids.current.projectId,p.episodeId).then(loaded=>{
    if(!loaded || loaded.episodeId!==p.episodeId){notify('目标所属分集已不可用，请刷新核查。','error');return;}
    if(['materials','images','shots'].includes(id))setProductionMode(id as any);
    setPage(id);setParams(p);setMobile(false);setNotice(null);
   })};
   if(dirty())setDiscard(()=>go);else go();
   return;
  }
  document.querySelectorAll<HTMLMediaElement>('video,audio').forEach(el=>el.pause());
  if(['materials','images','shots'].includes(id))setProductionMode(id as any);
  setPage(id);setParams(p);setMobile(false);setNotice(null);
 },[load,notify,project?.kind]);
 const openTask=(task:StudioTask)=>{
  const destination=taskDestination(task);
  const switching=task.projectId!==ids.current.projectId||!!task.episodeId&&task.episodeId!==ids.current.episodeId;
  if(!switching){navigate(destination.page,destination.params);return;}
  guard(()=>{void load(task.projectId,task.episodeId).then(loaded=>{
   if(!loaded)return;
   if(loaded.projectId!==task.projectId||task.episodeId&&loaded.episodeId!==task.episodeId){notify('任务所属项目或分集已不可用，请刷新任务核查。','error');return;}
   navigate(destination.page,destination.params);
  })});
 };
 const openCanvas=useCallback((shotId:string)=>{
  if(workspaceIds().length){notify('恢复工作区请使用镜头表单编辑；共享 ComfyUI 画布尚未绑定恢复工作区。','error');return}
  if(window.parent===window){notify('请从 ComfyUI 内的短剧工作台打开镜头画布。','error');return}
  window.parent.postMessage({type:'dgx-studio:canvas',shotId},location.origin);
 },[notify]);
 const close=()=>{document.querySelectorAll<HTMLMediaElement>('video,audio').forEach(x=>x.pause());window.parent.postMessage({type:'dgx-studio:close'},location.origin)};
 useEffect(()=>{const escape=(event:KeyboardEvent)=>{if(event.key==='Escape'&&!event.defaultPrevented&&!document.querySelector('[role=dialog]'))close()};window.addEventListener('keydown',escape);return()=>window.removeEventListener('keydown',escape)},[]);
 useEffect(()=>{const p=preference();void load(p.projectId,p.episodeId);return()=>{++epoch.current}},[load]);
 useEffect(()=>{const listener=(event:MessageEvent)=>{if(event.source!==window.parent||event.origin!==location.origin)return;
  if(event.data?.type==='dgx-studio:canvas-result'){if(event.data.error)notify(event.data.error,'error');else close()}
  if(event.data?.type==='dgx-studio:shot-saved'){window.dispatchEvent(new CustomEvent('dgx-drama-shot-saved',{detail:{shotId:event.data.shotId}}));void refresh().catch(e=>notify(e.message,'error'));}
  if(event.data?.type==='dgx-studio:hidden')document.querySelectorAll<HTMLMediaElement>('video,audio').forEach(x=>x.pause());
 };window.addEventListener('message',listener);return()=>window.removeEventListener('message',listener)},[refresh,notify]);
 useEffect(()=>{
  const reset=()=>{confirmedWorkspaceLeave.current=false;discardOrigin.current=null;if(workspaceLeaveTimer.current!==null){window.clearTimeout(workspaceLeaveTimer.current);workspaceLeaveTimer.current=null}};
  const before=(event:BeforeUnloadEvent)=>{
   // All panels own unload guards. One explicit discard authorizes just this
   // departure, including those guards; ordinary departures remain protected.
   if(confirmedWorkspaceLeave.current){const origin=discardOrigin.current;try{if(origin)discardEditableDrafts(origin.projectId,origin.episodeId);discardOrigin.current=null;reset();event.stopImmediatePropagation();return;}catch{reset();event.preventDefault();event.returnValue='';return;}}
   if(dirty()){event.preventDefault();event.returnValue=''}
  };
  window.addEventListener('beforeunload',before,true);
  for(const type of ['input','change','reset'])window.addEventListener(type,reset,true);
  return()=>{reset();window.removeEventListener('beforeunload',before,true);for(const type of ['input','change','reset'])window.removeEventListener(type,reset,true)}
 },[]);
 const createRecord=async()=>{
  if(saveLock.current||!name.trim())return;saveLock.current=true;setSaving(true);
  try{if(create==='project'){const result=await post('/projects',{title:name.trim(),width:preset==='portrait'?1080:1344,height:preset==='portrait'?1920:768,fps:24,kind});await load(result.project.id)}
   else if(project){const result=await post(`/projects/${segment(project.id)}/episodes`,{title:name.trim()});await load(project.id,result.episode.id)}
   setCreate(null);setName('');notify('已保存。现在可以开始制作。');
  }catch(e:any){notify(e.message,'error')}finally{saveLock.current=false;setSaving(false)}
 };
 const saveCanvas=async()=>{
  if(saveLock.current||!project)return;saveLock.current=true;setSaving(true);
  try{const [width,height]=canvasPreset.split('x').map(Number);await post(`/projects/${segment(project.id)}/canvas`,{width,height,expectedRevision:project.revision});await refresh();setCanvasOpen(false);notify('项目画幅已保存。已有原始素材保持原尺寸。')}
  catch(e:any){notify(e.message,'error')}finally{saveLock.current=false;setSaving(false)}
 };
 const ctx:WorkbenchContext={project,episode,assets,shots,entities,capabilities,refresh,notify,navigate,openCanvas,params};
 const isMv=project?.kind==='mv';
 const current=NAV.find(n=>n.id===page)!;
 const countApproved=entities.filter(e=>e.approvedRevisionId).length;
 const selected=shots.filter(s=>s.selectedTakeId).length;
 const next=!episode?{label:'创建第一个分集',text:'为这个故事安排一集，再开始设计人物与画面。',action:()=>{setCreate('episode');setName('第 1 集')}}:shots.length > 0 && selected === shots.length?{label:'打开剪辑台',text:'镜头已选好。调整顺序与节奏，导出你的故事。',action:()=>navigate('edit')}:!entities.length && !shots.length?{label:'准备人物与场景',text:'从主角开始，为故事建立统一的视觉参考。',action:()=>navigate('materials')}:!shots.length?{label:'设计第一个镜头',text:'参考资料已经就位。写下构图、动作和镜头时长。',action:()=>navigate('shots')}:selected<shots.length?{label:'继续制作镜头',text:'完善参考画面，生成视频并选出满意的候选。',action:()=>navigate('shots')}:{label:'打开剪辑台',text:'镜头已选好。调整顺序与节奏，导出你的故事。',action:()=>navigate('edit')};
 const pictures=assets.filter(a=>a.kind==='image');
 return <div className="h-dvh min-h-0 flex overflow-hidden">
  {mobile&&<button className="fixed inset-0 bg-black/60 z-30 md:hidden" aria-label="关闭导航遮罩" onClick={()=>setMobile(false)}/>}
  <aside ref={sidebarRef} inert={narrow&&!mobile} role={narrow&&mobile?'dialog':undefined} aria-modal={narrow&&mobile?true:undefined} aria-label="工作台导航" className={cn('w-[224px] shrink-0 border-r border-border/70 bg-[#141518] flex flex-col fixed md:static inset-y-0 left-0 z-40 transition-transform',mobile?'translate-x-0':'-translate-x-full md:translate-x-0')}>
   <div className="h-20 px-6 flex items-center gap-3"><div className="size-9 rounded-xl bg-primary text-primary-foreground flex items-center justify-center"><Film size={20}/></div><div><div className="text-[15px] font-semibold tracking-wide">短剧工作台</div><div className="text-[10px] text-muted-foreground tracking-[.22em]">CREATIVE STUDIO</div></div><Button variant="ghost" size="icon" className="md:hidden ml-auto size-6" aria-label="收起导航" onClick={()=>setMobile(false)}><X size={14}/></Button></div>
   <div className="px-4 pb-5 space-y-3"><div className="flex items-center justify-between px-1"><Label className="text-xs text-muted-foreground">当前项目</Label><Button variant="ghost" size="icon" className="size-6" aria-label="新建项目" onClick={()=>guard(()=>{setName('');setKind('drama');setCreate('project')})}><Plus size={14}/></Button></div>
    <Select value={project?.id||''} onValueChange={id=>guard(()=>void load(id))} disabled={busy}><SelectTrigger className="w-full h-11 bg-background/40" aria-label="当前项目"><SelectValue placeholder="选择项目"/></SelectTrigger><SelectContent>{projects.map(p=><SelectItem key={p.id} value={p.id}>{p.title}</SelectItem>)}</SelectContent></Select>
    {project&&<><div className="flex items-center justify-between px-1"><Label className="text-xs text-muted-foreground">当前分集</Label><Button variant="ghost" size="icon" className="size-6" aria-label="新建分集" onClick={()=>guard(()=>{setName(`第 ${episodes.length+1} 集`);setCreate('episode')})}><Plus size={14}/></Button></div><Select value={episode?.id||''} onValueChange={id=>guard(()=>void load(project.id,id))} disabled={busy}><SelectTrigger className="w-full h-10 bg-transparent" aria-label="当前分集"><SelectValue placeholder="创建分集后开始"/></SelectTrigger><SelectContent>{episodes.map(e=><SelectItem key={e.id} value={e.id}>{e.title}</SelectItem>)}</SelectContent></Select></>}
   </div>
   <div className="px-6 text-[10px] tracking-[.18em] text-muted-foreground mb-2">制作流程</div>
   <nav className="px-3 space-y-1 flex-1 overflow-auto studio-scroll" aria-label={isMv?'MV制作步骤':'短剧制作步骤'}>{NAV.filter(item=>item.id!=='song'||isMv).map(item=><button key={item.id} aria-current={page===item.id?'page':undefined} onClick={()=>navigate(item.id)} className={cn('w-full flex items-center gap-3 px-3 py-3 rounded-lg text-left transition-colors',page===item.id?'bg-primary/10 text-primary':'text-muted-foreground hover:bg-white/4 hover:text-foreground')}><item.icon size={18} strokeWidth={1.7}/><span className="text-[13px] font-medium flex-1">{item.label}</span>{page===item.id&&<span className="size-1.5 rounded-full bg-primary"/>}</button>)}</nav>
   <div className="m-4 p-3 rounded-xl bg-background/50 border border-border/50"><div className="flex items-center gap-2 text-xs"><span className={cn('size-1.5 rounded-full',failure?'bg-destructive':'bg-emerald-400')}/>{failure?'业务服务待连接':'本地工作台'}</div><p className="text-[10px] text-muted-foreground mt-1">ComfyUI · DGX Gateway</p></div>
   <Button variant="ghost" className="justify-start mx-3 mb-4 text-muted-foreground" onClick={close}><ArrowLeft size={15}/>返回 ComfyUI 画布</Button>
  </aside>
  <div inert={narrow&&mobile} className="flex-1 min-w-0 flex flex-col">
   <header className="h-[70px] shrink-0 border-b border-border/60 px-5 md:px-8 flex items-center gap-3"><Button variant="ghost" size="icon" className="md:hidden" aria-label="展开导航" onClick={()=>setMobile(true)}><Menu size={19}/></Button><span className="hidden sm:block text-sm text-muted-foreground truncate max-w-[220px]">{project?.title||'我的项目'}</span><ChevronRight size={14} className="text-muted-foreground hidden sm:block"/><span className="text-sm font-medium">{current.label}</span><div className="ml-auto flex items-center gap-3">{project&&<Button variant="ghost" size="sm" aria-label="项目画幅设置" className="text-xs tabular-nums" onClick={()=>guard(()=>{setCanvasPreset(`${project.width}x${project.height}`);setCanvasOpen(true)})}>{project.width} × {project.height} · {project.fps} fps</Button>}<Button variant="ghost" size="icon" aria-label="刷新项目数据" disabled={busy} onClick={()=>void refresh().then(()=>notify('项目数据已更新，编辑中的草稿保留。')).catch(e=>notify(e.message,'error'))}><RefreshCw size={16} className={busy?'animate-spin':''}/></Button></div></header>
   {(workspaceIds().length>0||capabilities.restoreMode)&&<section aria-label="恢复工作区状态" className="shrink-0 border-b border-amber-300/25 bg-amber-300/10 px-5 py-3 md:px-8 flex flex-wrap items-center gap-x-6 gap-y-3">
    <div className="min-w-0 flex-1 basis-56"><p className="text-sm font-semibold text-amber-100">恢复工作区 · 模型任务已隔离</p><p className="mt-1 text-xs leading-5 text-amber-100/80">{capabilities.restoreMode==='query_only'?'已启用原任务只读核查。不会重新生成、取消原任务或接管模型。':'这是独立恢复副本。可查看、编辑与 CPU 导出；原模型任务不会自动继续。'}</p></div>
    <Button variant="outline" size="sm" className="shrink-0 border-amber-300/30 bg-transparent text-amber-100 hover:bg-amber-300/10" onClick={leaveRestoredWorkspace}><ArrowLeft size={15}/>返回原工作区</Button>
   </section>}
   <main ref={mainRef} inert={busy} aria-busy={busy} className="flex-1 overflow-auto studio-scroll px-5 py-7 md:px-8 lg:px-10" id="studio-main">
    <div className="max-w-[1440px] mx-auto">
     {notice&&<Alert className={cn('mb-5 py-3',notice.tone==='error'?'border-destructive/40 text-destructive':'border-primary/20 bg-primary/5')} role={notice.tone==='error'?'alert':'status'}><AlertDescription className="flex items-start justify-between gap-4 text-inherit text-sm"><span>{notice.text}</span><button aria-label="关闭提示" onClick={()=>setNotice(null)}><X size={15}/></button></AlertDescription></Alert>}
     {failure&&<div className="p-5 mb-5 border border-destructive/40 rounded-xl space-y-3" role="alert"><p>{failure}</p><Button variant="outline" onClick={()=>void load(ids.current.projectId,ids.current.episodeId)}>重新连接</Button></div>}
     {busy&&!project?<div className="flex items-center justify-center gap-3 h-72 text-muted-foreground"><LoaderCircle className="animate-spin" size={20}/>正在打开工作台…</div>:!project?<div className="max-w-lg mx-auto py-24 text-center"><div className="size-16 rounded-2xl bg-primary/10 text-primary mx-auto flex items-center justify-center mb-6"><Film size={28}/></div><h1 className="text-3xl font-semibold tracking-tight">让故事，从这里开始。</h1><p className="mt-4 text-muted-foreground leading-7">建立项目，准备人物与场景，再把一个个镜头连接成片。</p><Button className="mt-8" size="lg" onClick={()=>{setName('');setKind('drama');setCreate('project')}}><Plus size={17}/>创建第一个项目</Button></div>:<div key={`${project.id}:${episode?.id||''}`}>
      <section hidden={page!=='overview'} className="studio-panel space-y-8">
       <div className="flex flex-wrap justify-between items-end gap-4"><div><p className="text-xs tracking-[.15em] text-muted-foreground mb-2">你的创作空间</p><h1 className="text-3xl font-semibold tracking-tight">{project.title}</h1><p className="text-muted-foreground text-sm mt-2">{episode?.title||'从一个新的分集开始。'}</p></div><Badge variant="outline" className="py-1.5 text-muted-foreground font-normal">{shots.length} 个镜头 · {selected} 个已选片</Badge></div>
       <div className="grid lg:grid-cols-[1.1fr_1fr] rounded-2xl overflow-hidden border border-border bg-card min-h-[264px]">
        <div className="p-7 md:p-9 flex flex-col items-start justify-center"><div className="flex items-center gap-2 text-xs text-primary mb-5"><span className="size-1.5 bg-primary rounded-full"/>接着完成这一步</div><h2 className="text-2xl font-medium tracking-tight">{next.label}</h2><p className="text-muted-foreground mt-3 text-sm max-w-md leading-7">{next.text}</p><Button onClick={next.action} className="mt-7">{next.label}<ArrowUpRight size={16}/></Button></div>
        <div className="min-h-[220px] lg:min-h-0 relative bg-[#202126] overflow-hidden">{pictures.length?<><img src={mediaUrl(project.id,pictures.at(-1).id)} alt="项目最新参考画面" className="absolute inset-0 w-full h-full object-cover object-top opacity-85"/><div className="absolute inset-0 bg-gradient-to-r from-card/50 to-transparent"/><div className="absolute bottom-5 right-5 text-xs px-3 py-1 rounded-full bg-black/50 text-white backdrop-blur">项目参考画面</div></>:<div className="h-full min-h-[220px] flex flex-col items-center justify-center text-muted-foreground/40"><Clapperboard size={64} strokeWidth={.7}/><p className="text-xs mt-4 tracking-widest">下一个好故事，正在酝酿</p></div>}</div>
       </div>
       <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">{[[String(countApproved),'已批准资料',Users],[String(pictures.length),'参考图片',ImagePlus],[String(shots.length),'已设计镜头',Clapperboard],[String(selected),'已采用视频',Film]].map(([value,label,Icon]:any)=><div key={label} className="rounded-xl border border-border/70 p-5 flex justify-between items-start"><div><p className="text-xs text-muted-foreground">{label}</p><p className="text-[28px] font-medium tabular-nums mt-2">{value}</p></div><Icon size={18} className="text-muted-foreground/60" strokeWidth={1.5}/></div>)}</div>
       <div><div className="flex justify-between items-center mb-4"><h2 className="font-medium">制作路径</h2><span className="text-xs text-muted-foreground">可随时返回，继续完善</span></div><div className={cn("grid sm:grid-cols-2 gap-4",isMv?"xl:grid-cols-3":"xl:grid-cols-5")}>{[{id:'story',title:isMv?'故事与歌词':'故事与剧本',description:isMv?'整理原文与歌词，再拆成镜头':'整理原文、情节与对白，再拆成镜头',ready:false,icon:BookOpen},...(isMv?[{id:'song',title:'绑定本集歌曲',description:'选定主时钟音频，成片长度跟歌曲走',ready:!!episode?.song_asset_id,icon:Music}]:[]),{id:'materials',title:'准备故事世界',description:'为人物与场景确定外观',ready:countApproved>0,icon:Users},{id:'shots',title:'设计镜头画面',description:'安排动作、构图与节奏',ready:shots.length>0,icon:Clapperboard},{id:'video',title:'生成并选择镜头',description:'查看候选，留下满意的版本',ready:selected>0,icon:WandSparkles},{id:'edit',title:'剪辑与交付',description:'连接镜头，导出完整作品',ready:false,icon:Scissors}].map((s,i)=><button key={s.id} className="group p-5 bg-card/60 border border-border/60 hover:border-primary/50 rounded-xl text-left transition-colors" onClick={()=>navigate(s.id)}><div className="flex items-center justify-between mb-7"><s.icon size={22} strokeWidth={1.4} className="text-primary/80"/>{s.ready?<Check size={15} className="text-emerald-400"/>:<span className="text-xs text-muted-foreground">0{i+1}</span>}</div><p className="text-sm font-medium">{s.title}</p><p className="mt-2 text-xs text-muted-foreground leading-6">{s.description}</p><ArrowUpRight size={16} className="mt-4 text-muted-foreground group-hover:text-primary"/></button>)}</div></div>
      </section>
      <section hidden={page!=='tasks'} className="studio-panel"><TaskPanel ctx={ctx} active={page==='tasks'} onOpenTask={openTask}/></section>
      <section hidden={page!=='backup'} className="studio-panel"><BackupPanel ctx={ctx}/></section>
      <section hidden={page!=='story'} className="studio-panel"><StoryPanel ctx={ctx}/></section>
      <section hidden={page!=='song'} className="studio-panel"><SongPanel ctx={ctx}/></section>
      <section hidden={!['materials','shots','images'].includes(page)} className="studio-panel"><ProductionPanel ctx={ctx} mode={productionMode}/></section>
      {page==='image-review'&&<section className="studio-panel"><QuickImageReviewPanel ctx={ctx} active/></section>}
      <section hidden={page!=='video'} className="studio-panel"><VideoPanel ctx={ctx} active={page==='video'}/></section>
      <section hidden={page!=='edit'} className="studio-panel"><EditPanel ctx={ctx} active={page==='edit'}/></section>
     </div>}
    </div>
   </main>
  </div>
  <Dialog open={canvasOpen} onOpenChange={open=>{if(!saving)setCanvasOpen(open)}}><DialogContent><DialogHeader><DialogTitle>项目画幅</DialogTitle><DialogDescription>设置最终成片的比例。原始图片和视频不会被拉伸或重新生成。已有剪辑时间线的项目暂不支持直接改画幅。</DialogDescription></DialogHeader><div className="space-y-2"><Label>成片比例</Label><Select value={canvasPreset} onValueChange={setCanvasPreset} disabled={saving}><SelectTrigger aria-label="成片比例"><SelectValue/></SelectTrigger><SelectContent>{!['1920x1080','1080x1920','1080x1080','1344x768'].includes(canvasPreset)&&<SelectItem value={canvasPreset}>当前 · {canvasPreset}</SelectItem>}<SelectItem value="1920x1080">16:9 横屏 · 1920 × 1080</SelectItem><SelectItem value="1080x1920">9:16 竖屏 · 1080 × 1920</SelectItem><SelectItem value="1080x1080">1:1 方形 · 1080 × 1080</SelectItem><SelectItem value="1344x768">模型原画幅 · 1344 × 768</SelectItem></SelectContent></Select></div><DialogFooter><Button variant="outline" disabled={saving} onClick={()=>setCanvasOpen(false)}>取消</Button><Button disabled={saving} onClick={()=>void saveCanvas()}>{saving?'保存中…':'保存画幅'}</Button></DialogFooter></DialogContent></Dialog>
  <Dialog open={!!create} onOpenChange={v=>{if(!saving&&!v)setCreate(null)}}><DialogContent><DialogHeader><DialogTitle>{create==='project'?'创建新项目':'创建新分集'}</DialogTitle><DialogDescription>{create==='project'?'为你的故事起一个名字，选择最终交付画幅。':'每个分集都有独立的镜头和剪辑时间线。'}</DialogDescription></DialogHeader><form onSubmit={e=>{e.preventDefault();void createRecord()}} className="space-y-5"><div className="space-y-2"><Label htmlFor="create-name">{create==='project'?'项目名称':'分集名称'}</Label><Input id="create-name" value={name} onChange={e=>setName(e.target.value)} placeholder={create==='project'?'例如：雨夜来信':'第 1 集'} autoFocus maxLength={200}/></div>{create==='project'&&<div className="space-y-2"><Label>作品类型</Label><Select value={kind} onValueChange={setKind}><SelectTrigger className="w-full" aria-label="作品类型"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="drama">短剧</SelectItem><SelectItem value="mv">MV</SelectItem></SelectContent></Select><p className="text-xs text-muted-foreground">MV以歌曲为成片时钟；短剧仍以画面总长为准。</p></div>}{create==='project'&&<div className="space-y-2"><Label>交付画布</Label><Select value={preset} onValueChange={setPreset}><SelectTrigger className="w-full" aria-label="交付画布"><SelectValue/></SelectTrigger><SelectContent><SelectItem value="landscape">横屏 · 1344 × 768 · 24fps</SelectItem><SelectItem value="portrait">竖屏 · 1080 × 1920 · 24fps</SelectItem></SelectContent></Select><p className="text-xs text-muted-foreground">交付画幅与模型源画幅分别记录。</p></div>}<DialogFooter><Button variant="outline" type="button" disabled={saving} onClick={()=>setCreate(null)}>取消</Button><Button type="submit" disabled={saving||!name.trim()}>{saving&&<LoaderCircle size={15} className="animate-spin"/>}{saving?'正在创建…':create==='project'?'创建项目':'创建分集'}</Button></DialogFooter></form></DialogContent></Dialog>
  <Dialog open={!!discard} onOpenChange={v=>{if(!v)setDiscard(null)}}><DialogContent><DialogHeader><DialogTitle>还有未保存的修改</DialogTitle><DialogDescription>切换项目、分集或工作区会离开当前编辑内容。可返回保存，或放弃当前未保存的编辑；其他分集草稿、已提交任务及原请求回执仍保留。</DialogDescription></DialogHeader><DialogFooter><Button variant="outline" onClick={()=>setDiscard(null)}>返回继续编辑</Button><Button variant="destructive" onClick={()=>{const action=discard;discardOrigin.current={...ids.current};setDiscard(null);action?.()}}>放弃草稿并继续</Button></DialogFooter></DialogContent></Dialog>
  <Toaster theme="dark" richColors position="bottom-right" closeButton/>
 </div>;
}
