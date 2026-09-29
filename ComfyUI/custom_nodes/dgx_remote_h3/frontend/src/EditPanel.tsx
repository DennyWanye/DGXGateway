import { TimelinePreview } from "./TimelinePreview";
import { TimelineStrip, arrayMove } from './TimelineStrip';
import { sessionStore as sessionStorage } from "@/lib/workspace";
import { useCallback, useEffect, useRef, useState } from "react";
import type { WorkbenchContext } from "@/lib/types";
import { get, post, segment, requestKey, mediaUrl, upload } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  ArrowDown,
  ArrowUp,
  Check,
  Clapperboard,
  Download,
  Film,
  FolderInput,
  Loader2,
  Plus,
  RefreshCw,
  Save,
  Scissors,
  Trash2,
  Upload,
} from "lucide-react";

import {
  AudioTracksEditor,
  PostproductionPanel,
  PendingAction,
  useDurablePost,
  readDraft,
  putDraft,
  validateAudioClips,
  type AudioClip,
} from "./PostproductionPanel";

type Clip = {
  clipId: string;
  takeId: string;
  inFrame: number | string;
  frames: number | string;
  keepSourceAudio: boolean;
  fit: string;
  crop?: { x: number; y: number };
};
export function splitTimelineClip(clips: Clip[], id: string, sourceFrame: number, newId: string): Clip[] {
  const index = clips.findIndex(c => c.clipId === id), clip = clips[index];
  if (!clip || !Number.isInteger(sourceFrame) || !newId || clips.some(c => c.clipId === newId)) throw new Error("无效分割位置或片段编号");
  const start = Number(clip.inFrame), length = Number(clip.frames), left = sourceFrame - start;
  if (!Number.isInteger(start) || !Number.isInteger(length) || left <= 0 || left >= length) throw new Error("请在保留区间内部选择分割位置");
  return [...clips.slice(0, index), { ...clip, frames: left }, { ...clip, clipId: newId, inFrame: sourceFrame, frames: length - left }, ...clips.slice(index + 1)];
}
const terminal = new Set(["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"]);
const states: Record<string, string> = {
  QUEUED: "等待导出",
  RUNNING: "正在导出",
  SUCCEEDED: "已完成",
  FAILED: "导出失败",
  INTERRUPTED: "导出中断",
  CANCELED: "已取消",
};
const nameOf = (asset: any) =>
  asset?.filename || asset?.title || asset?.name || asset?.id || "未命名素材";
export const isVideoAsset = (asset: any) =>
  asset.kind === "video" ||
  String(asset.contentType || "").startsWith("video/") ||
  (!asset.kind && /\.(mp4|mov|mkv|webm)$/i.test(String(asset.filename || "")));
const errorText = (error: any) =>
  typeof error === "string" ? error : error?.message || "";
export function editableFrames(
  value: string,
  unit: string,
  fps: number,
): number | string {
  if (!value.trim()) return "";
  const number = Number(value);
  return Number.isFinite(number)
    ? unit === "seconds"
      ? Math.round(number * fps)
      : number
    : value;
}
export function validateClips(clips: Clip[], requireCrop = false, keepSourceDefault = true) {
  const integer = (value: string | number, label: string, min = 0) => {
    const number = Number(value);
    if (
      String(value).trim() === "" ||
      !Number.isSafeInteger(number) ||
      number < min
    )
      throw new Error(`${label}须为不小于 ${min} 的整数帧。`);
    return number;
  };
  return clips.map((clip, index) => {
    if (
      clip.fit === "cover" &&
      requireCrop &&
      (!clip.crop ||
        !Number.isFinite(clip.crop.x) ||
        !Number.isFinite(clip.crop.y) ||
        clip.crop.x < 0 ||
        clip.crop.x > 1 ||
        clip.crop.y < 0 ||
        clip.crop.y > 1)
    )
      throw new Error(`片段 ${index + 1}请选择明确的裁切位置，再保存。`);
    return {
      clipId: clip.clipId,
      takeId: clip.takeId,
      inFrame: integer(clip.inFrame, `片段 ${index + 1} 源入点`),
      frames: integer(clip.frames, `片段 ${index + 1} 保留长度`, 1),
      keepSourceAudio: clip.keepSourceAudio ?? keepSourceDefault,
      fit: clip.fit || "contain",
      ...(clip.fit === "cover" && clip.crop ? { crop: clip.crop } : {}),
    };
  });
}
export function retryTimelineId(item: any): string {
  if (!["FAILED", "INTERRUPTED"].includes(item.state))
    throw new Error("仅失败或中断的导出可以重新提交，请刷新核查。");
  if (typeof item.timelineId !== "string" || !item.timelineId.trim())
    throw new Error("缺少原任务的冻结时间线编号，不能用当前版本代替。");
  return item.timelineId;
}
const defaultClip = (takeId: string, keepSourceAudio = true): Clip => ({
  clipId: requestKey(),
  takeId,
  inFrame: 0,
  frames: 96,
  keepSourceAudio,
  fit: "contain",
});
function Choice({
  value,
  onChange,
  options,
  label,
  disabled,
}: {
  value: string;
  onChange: (value: string) => void;
  options: { id: string; title: string }[];
  label: string;
  disabled?: boolean;
}) {
  return (
    <Select
      value={value || "__empty"}
      onValueChange={(v) => onChange(v === "__empty" ? "" : v)}
      disabled={disabled}
    >
      <SelectTrigger aria-label={label} className="w-full">
        <SelectValue placeholder={label} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="__empty">{label}</SelectItem>
        {options.map((option) => (
          <SelectItem key={option.id} value={option.id}>
            {option.title}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

export function EditPanel({ ctx, active = true }: { ctx: WorkbenchContext; active?: boolean }) {
  const pid = ctx.project?.id || "",
    eid = ctx.episode?.id || "",
    scope = `${pid}/${eid}`;
  const root = useRef<HTMLDivElement>(null),
    scopeRef = useRef(scope),
    busyRef = useRef(false),
    dirtyRef = useRef(false),
    sequence = useRef(0);
  scopeRef.current = scope;
  const [ownBusy, setBusy] = useState(false),
    [busyText, setBusyText] = useState(""),
    [dirty, setDirty] = useState(false);
  const [postDirty, setPostDirty] = useState(false),
    [postBusy, setPostBusy] = useState(false),
    [audioBusy, setAudioBusy] = useState(false),
    [audioDirty, setAudioDirty] = useState(false);
  const [audioClips, setAudioClips] = useState<AudioClip[]>([]),
    [draftScope, setDraftScope] = useState(scope);
  const postStatus = useCallback((dirty: boolean, busy: boolean) => {
    setPostDirty(dirty);
    setPostBusy(busy);
  }, []);
  const audioStatus = useCallback((dirty: boolean, busy: boolean) => {
    setAudioDirty(dirty);
    setAudioBusy(busy);
  }, []);
  const [timeline, setTimeline] = useState<any>(null),
    [clips, setClips] = useState<Clip[]>([]),
    [exports, setExports] = useState<any[]>([]);
  const [selectedClipId, setSelectedClipId] = useState("");
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [previewAssetId, setPreviewAssetId] = useState("");
  const selectedClip = clips.find(c => c.clipId === selectedClipId) || clips[0];
  const [history, setHistory] = useState<{ past: Clip[][]; future: Clip[][] }>({ past: [], future: [] });
  const trimGesture = useRef<{ before: Clip[]; recorded: boolean } | null>(null);
  const changeClips = (next: Clip[]) => {
    const gesture = trimGesture.current;
    if (!gesture || !gesture.recorded) setHistory(h => ({ past: [...h.past.slice(-49), gesture?.before || clips], future: [] }));
    if (gesture) gesture.recorded = true;
    setClips(next); markDirty(true);
  };
  const travel = (direction: "past" | "future") => {
    const source = history[direction]; if (!source.length || busy) return;
    const previous = source[source.length - 1];
    setHistory(direction === "past" ? { past: source.slice(0, -1), future: [...history.future, clips] } : { past: [...history.past, clips], future: source.slice(0, -1) });
    setClips(previous);
    markDirty(!timeline || JSON.stringify(previous) !== JSON.stringify(timeline.clips || []) || JSON.stringify(audioClips) !== JSON.stringify(timeline.audioClips || []));
  };
  const [unit, setUnit] = useState("seconds"),
    [takeId, setTakeId] = useState(""),
    [library, setLibrary] = useState<any[]>([]),
    [sourceId, setSourceId] = useState("");
  const [videoFile, setVideoFile] = useState<File | undefined>(),
    [importNames, setImportNames] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<{
    title: string;
    description: string;
    action: () => Promise<void> | void;
  } | null>(null);
  const shots = ctx.shots || [],
    assets = ctx.assets || [],
    fps = timeline?.fps || ctx.project?.fps || 24;
  const takes = shots.flatMap((shot: any) =>
    (shot.takes || []).map((take: any) => ({
      ...take,
      shotId: shot.id,
      shotTitle: shot.title,
      selected: shot.selectedTakeId === take.id,
    })),
  );
  const adopted = takes.filter((take: any) => take.selected);
  const totalFrames = clips.reduce(
    (sum, clip) => sum + (Number(clip.frames) || 0),
    0,
  );
  const isMv = ctx.project?.kind === "mv";
  const keepSourceDefault = !isMv;
  const songAsset =
    isMv && ctx.episode?.song_asset_id
      ? assets.find(
          (a: any) =>
            a.id === ctx.episode.song_asset_id && a.kind === "audio",
        )
      : null;
  const songFrames =
    songAsset && Number.isFinite(Number(songAsset.duration))
      ? Math.floor(Number(songAsset.duration) * fps)
      : null;
  const clockFrames = isMv && songFrames != null ? songFrames : totalFrames;
  const displayAudio =
    isMv &&
    ctx.episode?.song_asset_id &&
    songFrames != null &&
    !audioClips.some(
      (clip) =>
        clip.assetId === ctx.episode.song_asset_id && clip.track === "bgm",
    )
      ? [
          {
            audioClipId: "song-bgm",
            assetId: ctx.episode.song_asset_id,
            track: "bgm",
            startFrame: 0,
            sourceInFrame: 0,
            frames: songFrames,
            gainDb: 0,
            fadeInFrames: 0,
            fadeOutFrames: 0,
          },
          ...audioClips,
        ]
      : audioClips;
  const postEnabled = !!ctx.capabilities?.postproductionEnabled;
  const [previewSubtitles, setPreviewSubtitles] = useState<any[]>([]);
  const timelineAction = useDurablePost(
    scope,
    "timeline",
    async (result, kind) => {
      if (kind === "timeline") {
        if (!result.timeline?.id)
          throw new Error("保存回执缺少时间线编号，请按原键核查。");
        setTimeline(result.timeline);
        setClips(result.timeline.clips.map((clip: any) => ({ ...clip })));
        setAudioClips(result.timeline.audioClips || []);
        markDirty(false);
        sessionStorage.removeItem(`dgx.drama.edit.draft:${scope}`);
        await ctx.refresh();
        ctx.notify("时间线与声音已保存。", "success");
      } else if (result.export?.id) {
        setExports((current) => [
          result.export,
          ...current.filter((item) => item.id !== result.export.id),
        ]);
        ctx.notify("导出任务已保存。", "success");
      } else throw new Error("回执缺少导出编号，请按原键核查。");
    },
    ctx.notify,
  );
  const busy =
    ownBusy ||
    postBusy ||
    audioBusy ||
    timelineAction.busy ||
    !!timelineAction.pending;
  const markDirty = (value: boolean) => {
    dirtyRef.current = value;
    setDirty(value);
  };
  const run = async (text: string, action: () => Promise<void> | void) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setBusyText(text);
    try {
      await action();
    } catch (error) {
      if (scopeRef.current === scope) ctx.notify(errorText(error), "error");
    } finally {
      busyRef.current = false;
      if (scopeRef.current === scope) {
        setBusy(false);
        setBusyText("");
      }
    }
  };
  const readEpisode = useCallback(async () => {
    if (!eid) return;
    const request = ++sequence.current,
      result = await get(`/episodes/${segment(eid)}`);
    if (scopeRef.current !== scope || request !== sequence.current) return;
    setExports(result.exports || []);
    if (!dirtyRef.current) {
      setHistory({ past: [], future: [] });
      setTimeline(result.timeline || null);
      setClips(
        (result.timeline?.clips || []).map((clip: any) => ({ ...clip })),
      );
      setAudioClips(result.timeline?.audioClips || []);
    }
  }, [eid, scope]);
  useEffect(() => {
    ++sequence.current;
    markDirty(false);
    setTimeline(null);
    setHistory({ past: [], future: [] });
    setSelectedClipId("");
    setClips([]);
    setExports([]);
    setTakeId("");
    setVideoFile(undefined);
    setImportNames({});
    setConfirm(null);
    setBusy(false);
    setSourceId("");
    setLibrary([]);
    const saved = readDraft(`dgx.drama.edit.draft:${scope}`);
    setDraftScope(scope);
    setAudioClips(saved?.audioClips || []);
    if (saved) {
      setTimeline(saved.timeline);
      setClips(saved.clips || []);
      markDirty(true);
    }

    if (!pid) return;
    readEpisode().catch((error) => {
      if (scopeRef.current === scope)
        ctx.notify(`时间线读取失败：${errorText(error)}`, "error");
    });
    get("/library")
      .then((result) => {
        if (scopeRef.current === scope) setLibrary(result.sources || []);
      })
      .catch((error) => {
        if (scopeRef.current === scope)
          ctx.notify(`影片库读取失败：${errorText(error)}`, "error");
      });
    return () => {
      ++sequence.current;
    };
  }, [scope, readEpisode]);
  useEffect(() => {
    let polling = false;
    const timer = window.setInterval(async () => {
      if (
        busyRef.current ||
        polling ||
        !root.current?.checkVisibility() ||
        document.hidden
      )
        return;
      const pending = exports.filter((item) => !terminal.has(item.state));
      if (!pending.length) return;
      polling = true;
      try {
        const results = await Promise.allSettled(
          pending.map((item) => get(`/exports/${segment(item.id)}`)),
        );
        if (scopeRef.current !== scope) return;
        const updates = new Map(
          results.flatMap((result) =>
            result.status === "fulfilled" && result.value.export?.id
              ? [[result.value.export.id, result.value.export] as const]
              : [],
          ),
        );
        setExports((current) =>
          current.map((item) => updates.get(item.id) || item),
        );
      } finally {
        polling = false;
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [exports, scope]);
  useEffect(() => {
    if (!dirty && !postDirty && !audioDirty && !timelineAction.pending) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty, postDirty, audioDirty, !!timelineAction.pending]);
  useEffect(() => {
    if (dirty && draftScope === scope)
      putDraft(`dgx.drama.edit.draft:${scope}`, {
        timeline,
        clips,
        audioClips,
      });
    else if (!dirty && timeline && draftScope === scope)
      sessionStorage.removeItem(`dgx.drama.edit.draft:${scope}`);
  }, [scope, draftScope, dirty, timeline, clips, audioClips]);
  const editClip = (id: string, patch: Partial<Clip>) => changeClips(clips.map(c => c.clipId === id ? { ...c, ...patch } : c));
  const moveClip = (index: number, delta: number) => changeClips(arrayMove(clips, index, index + delta));
  const buildTimeline = () => {
    const action = () => {
      changeClips(
        shots
          .filter((shot: any) => shot.selectedTakeId)
          .map((shot: any) => defaultClip(shot.selectedTakeId, keepSourceDefault)),
      );
      markDirty(true);
      ctx.notify("时间线草稿已建立，检查片段长度后保存。", "success");
    };
    if (clips.length)
      setConfirm({
        title: "重新建立时间线草稿？",
        description:
          "当前片段排列和裁切会被替换为各镜头的已采用候选。已保存版本继续保留。",
        action,
      });
    else action();
  };
  const save = () =>
    run("正在保存时间线…", async () => {
      const validated = validateClips(clips, postEnabled, keepSourceDefault);
      if (isMv) {
        if (songFrames == null)
          throw new Error("MV须先绑定本集歌曲，再保存时间线。");
        if (totalFrames !== songFrames)
          throw new Error(
            `画面总帧必须等于歌曲长度（${songFrames}帧）；请补黑场或定格，不会自动补帧`,
          );
      }
      const body = {
        expectedRevision: timeline?.revision || 0,
        clips: validated,
        ...(postEnabled
          ? {
              audioClips: validateAudioClips(
                displayAudio,
                clockFrames,
                assets,
                fps,
              ),
            }
          : {}),
      };
      await timelineAction.execute(
        `/episodes/${segment(eid)}/timeline-revisions`,
        body,
        "timeline",
      );
    });
  const exportTimeline = async (id: string) =>
    timelineAction.execute(
      `/timeline-revisions/${segment(id)}/exports`,
      postEnabled
        ? {
            outputs: ["master"],
            subtitleRevisionId: null,
            presetVersion: "drama-export-v2",
          }
        : {},
      "export",
    );
  const adoptTake = (shot: any, take: any) => {
    const action = async () => {
      await post(`/shots/${segment(shot.id)}/selection`, {
        takeId: take.id,
        expectedRevision: shot.revision,
        ...(take.stale ? { acceptStale: true } : {}),
      });
      if (scopeRef.current !== scope) return;
      await ctx.refresh();
      ctx.notify(
        "候选已采用。当前时间线仍保留原片段，需要时请重新建立或追加替换。",
        "success",
      );
    };
    if (take.stale)
      setConfirm({
        title: "采用旧修订的视频？",
        description:
          "这段视频来自镜头旧版本，将替换当前采用候选。时间线不会自动替换。",
        action,
      });
    else void run("正在采用候选…", action);
  };
  const reorderShot = (index: number, delta: number) =>
    run("正在保存镜头顺序…", async () => {
      const ids = shots.map((shot: any) => shot.id);
      [ids[index], ids[index + delta]] = [ids[index + delta], ids[index]];
      await post(`/episodes/${segment(eid)}/shot-order`, {
        shotIds: ids,
        expectedRevision: ctx.episode.revision,
      });
      if (scopeRef.current !== scope) return;
      await ctx.refresh();
      ctx.notify("镜头顺序已保存，当前时间线顺序保留。", "success");
    });
  const currentMaster = exports.filter(item => item.timelineId === timeline?.id && item.state === "SUCCEEDED")
    .sort((a, b) => Number(b.createdAt || 0) - Number(a.createdAt || 0))
    .flatMap(item => item.outputs || (item.assetId ? [{ role: "master", assetId: item.assetId }] : []))
    .find(output => output.role === "master" && typeof output.assetId === "string" && output.assetId.length > 0);
  if (!pid)
    return (
      <Card>
        <CardContent className="p-10 text-center text-muted-foreground">
          先选择短剧项目，再开始剪辑。
        </CardContent>
      </Card>
    );
  return (
    <div
      ref={root}
      aria-busy={busy}
      data-panel="edit"
      data-dirty={dirty || postDirty || audioDirty || !!timelineAction.pending}
      className="space-y-6"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-[.2em] text-primary">
            EDIT & EXPORT
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight">
            把故事剪成一部短片
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            排列已采用的镜头，调整入点和长度，保存后导出。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() =>
            run("正在刷新时间线与导出…", async () => {
              await readEpisode();
              await ctx.refresh();
              ctx.notify("数据已刷新，未保存的时间线编辑保留。", "success");
            })
          }
        >
          <RefreshCw className="mr-2 size-4" />
          刷新状态
        </Button>
      </div>
      <div className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-3 rounded-xl border bg-background/95 p-3 shadow-sm backdrop-blur" aria-label="保存与下载成片">
        <div className="text-sm"><span className="font-medium">{dirty ? "有未保存修改" : timeline ? `已保存 · 版本 ${timeline.revision}` : "尚未建立时间线"}</span><p className="text-xs text-muted-foreground">{dirty ? "先保存，再导出最新修改。" : "导出完成后，直接在这里下载视频。"}</p></div>
        <div className="flex flex-wrap gap-2">

              <Button
                variant="outline"
                disabled={busy || !clips.length || !eid}
                onClick={save}
              >
                <Save className="mr-2 size-4" />
                保存时间线
              </Button>
              <Button
                disabled={busy || !timeline || dirty}
                onClick={() =>
                  postEnabled ? root.current?.querySelector<HTMLButtonElement>("[data-export-primary]")?.click() : run("正在提交导出任务…", () => exportTimeline(timeline.id))
                }
              >
                <Download className="mr-2 size-4" />
                {currentMaster ? "重新导出已保存版本" : "导出已保存版本"}
              </Button>
            
          {currentMaster && <Button asChild><a href={mediaUrl(pid, currentMaster.assetId)} download><Download className="mr-2 size-4" />下载视频 · 已保存版本</a></Button>}
        </div>
      </div>
      {busy && (
        <div
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 rounded-xl border border-primary/20 bg-primary/5 p-3 text-sm"
        >
          <Loader2 className="size-4 animate-spin" />
          {busyText || "正在处理声音/字幕操作；未确认请求请使用原键核查。"}
        </div>
      )}
      <PendingAction {...timelineAction} />
      {dirty && (
        <Button
          variant="ghost"
          disabled={busy}
          onClick={() =>
            setConfirm({
              title: "重新读取已保存时间线？",
              description:
                "将清除当前视频与声音的未保存编辑，并读取服务端最新版本。字幕草稿继续保留。",
              action: async () => {
                markDirty(false);
                sessionStorage.removeItem(`dgx.drama.edit.draft:${scope}`);
                await readEpisode();
              },
            })
          }
        >
          重新读取已保存时间线
        </Button>
      )}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap justify-between gap-3">
            <div>
              <CardTitle className="text-base">时间线</CardTitle>
              <CardDescription className="mt-2">
                {isMv && songFrames != null
                  ? `${clips.length} 个片段 · 歌曲时钟 ${songFrames} 帧 · ${(songFrames / fps).toFixed(3)} 秒 · 画面 ${totalFrames} 帧 · ${fps} fps`
                  : `${clips.length} 个片段 · ${totalFrames} 帧 · ${(totalFrames / fps).toFixed(3)} 秒 · ${fps} fps`}
              </CardDescription>
            </div>
            <Badge variant={dirty ? "outline" : "secondary"}>
              {dirty
                ? "有未保存的修改"
                : timeline
                  ? `已保存版本 ${timeline.revision}`
                  : "尚未保存"}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="flex flex-col gap-3 xl:flex-row xl:items-center">
            <Button
              variant="outline"
              disabled={busy || !adopted.length || !eid}
              onClick={buildTimeline}
            >
              <Clapperboard className="mr-2 size-4" />
              按镜头顺序建立时间线
            </Button>
            <div className="flex min-w-0 flex-1 gap-2">
              <div className="min-w-0 flex-1">
                <Choice
                  value={takeId}
                  onChange={setTakeId}
                  options={adopted.map((take: any) => ({
                    id: take.id,
                    title: take.shotTitle,
                  }))}
                  label="选择已采用候选"
                  disabled={busy}
                />
              </div>
              <Button
                variant="secondary"
                disabled={busy || !takeId || !eid}
                onClick={() => {
                  changeClips([...clips, defaultClip(takeId, keepSourceDefault)]);
                  markDirty(true);
                }}
              >
                <Plus className="mr-1 size-4" />
                追加
              </Button>
            </div>
            <Select value={unit} onValueChange={setUnit} disabled={busy}>
              <SelectTrigger className="w-full xl:w-36" aria-label="时间单位">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="seconds">按秒编辑</SelectItem>
                <SelectItem value="frames">按帧编辑</SelectItem>
              </SelectContent>
            </Select>
          </div>
          {!!clips.length && <>
            <TimelinePreview pid={pid} clips={clips} takes={takes} assets={assets} audioClips={displayAudio} subtitles={previewSubtitles} fps={fps} width={ctx.project?.width || 1344} height={ctx.project?.height || 768} active={active} />
            <TimelineStrip clips={clips} titles={Object.fromEntries(takes.map((t: any) => [t.id, t.shotTitle]))} fps={fps} selectedId={selectedClip?.clipId || ""} disabled={busy} onSelect={setSelectedClipId} onReorder={(from, to) => changeClips(arrayMove(clips, from, to))} />
            <div className="flex gap-2"><Button variant="outline" size="sm" disabled={busy || !history.past.length} onClick={() => travel("past")}>撤销片段修改</Button><Button variant="outline" size="sm" disabled={busy || !history.future.length} onClick={() => travel("future")}>重做片段修改</Button></div>
          </>}
          {!clips.length && (
            <div className="rounded-xl border border-dashed px-5 py-12 text-center">
              <Scissors className="mx-auto mb-4 size-9 text-muted-foreground/40" />
              <p className="font-medium">给故事一个节奏</p>
              <p className="mt-2 text-sm text-muted-foreground">
                {adopted.length
                  ? "点击「按镜头顺序建立时间线」，从已采用的视频开始剪辑。"
                  : "先到「生成与审片」批准并采用视频；也可以在下方导入已有影片。"}
              </p>
              {!adopted.length && (
                <Button
                  variant="outline"
                  className="mt-4"
                  onClick={() => ctx.navigate("video", { view: "review" })}
                >
                  去审片与选片
                </Button>
              )}
            </div>
          )}
          {clips.map((clip, index) => clip.clipId === selectedClip?.clipId && (
            <ClipEditor
              active={active}
              outputWidth={ctx.project?.width || 1344}
              outputHeight={ctx.project?.height || 768}
              key={clip.clipId}
              clip={clip}
              index={index}
              count={clips.length}
              take={takes.find((take: any) => take.id === clip.takeId)}
              pid={pid}
              fps={fps}
              unit={unit}
              busy={busy}
              fitModes={ctx.capabilities?.fitModes || ["contain"]}
              keepSourceDefault={keepSourceDefault}
              onTrimStart={() => { trimGesture.current = { before: clips, recorded: false }; }}
              onTrimEnd={() => { trimGesture.current = null; }}
              onChange={(patch) => editClip(clip.clipId, patch)}
              move={(delta) => moveClip(index, delta)}
              split={(sourceFrame) => {
                const next = splitTimelineClip(clips, clip.clipId, sourceFrame, requestKey());
                changeClips(next);
              }}
              remove={() => {
                changeClips(clips.filter((item) => item.clipId !== clip.clipId));
                markDirty(true);
              }}
            />
          ))}
          <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
            <p className="text-xs text-muted-foreground">
              按秒输入会对齐最近一帧。导出使用已保存版本。
            </p>

          </div>
        </CardContent>
      </Card>
      {postEnabled && (
        <AudioTracksEditor
          mediaActive={active}
          ctx={ctx}
          clips={displayAudio}
          onChange={(value) => {
            setAudioClips(value);
            markDirty(true);
          }}
          totalFrames={clockFrames}
          fps={fps}
          disabled={busy}
          onStatus={audioStatus}
        />
      )}
      {postEnabled && (
        <PostproductionPanel
          mediaActive={active}
          onPreviewSubtitles={setPreviewSubtitles}
          ctx={ctx}
          timeline={timeline}
          timelineDirty={dirty}
          disabled={
            ownBusy ||
            timelineAction.busy ||
            !!timelineAction.pending ||
            audioBusy
          }
          exports={exports}
          onExport={(item) =>
            setExports((current) => [
              item,
              ...current.filter((v) => v.id !== item.id),
            ])
          }
          onStatus={postStatus}
        />
      )}
      {!postEnabled && (
        <section aria-label="导出任务与成片" className="space-y-4">
          <div className="flex items-center gap-2">
            <h3 className="font-semibold">导出任务与成片</h3>
            <Badge variant="secondary">{exports.length}</Badge>
          </div>
          {!exports.length && (
            <p className="rounded-xl border border-dashed p-6 text-sm text-muted-foreground">
              保存时间线后点击「导出已保存版本」，成片会显示在这里。
            </p>
          )}
          {exports.map((item) => (
            <ExportCard
              mediaActive={active}
              key={item.id}
              item={item}
              pid={pid}
              episodeTitle={ctx.episode?.title}
              busy={busy}
              retry={() =>
                run("正在按原版本重新导出…", () =>
                  timelineAction.execute(
                    `/exports/${segment(item.id)}/retry`,
                    {},
                    "export",
                  ),
                )
              }
            />
          ))}
        </section>
      )}
      <details className="rounded-xl border bg-card p-5">
        <summary className="cursor-pointer text-sm font-semibold">
          镜头顺序与已有选片 · {shots.length} 个镜头
        </summary>
        <div className="mt-5 space-y-4">
          {!shots.length && (
            <p className="text-sm text-muted-foreground">本集还没有镜头。</p>
          )}
          {shots.map((shot: any, index: number) => (
            <div key={shot.id} className="space-y-3 rounded-lg border p-4">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <span className="text-sm font-medium">
                    {index + 1}. {shot.title}
                  </span>
                  <div className="mt-1 flex gap-2">
                    <Badge variant="outline">修订 {shot.revision}</Badge>
                    <Badge variant="secondary">
                      {shot.selectedTakeId ? "已有采用候选" : "尚未采用"}
                    </Badge>
                    {shot.needsReview && (
                      <Badge variant="destructive">制作依赖需复核</Badge>
                    )}
                  </div>
                </div>
                <div className="flex gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`上移镜头 ${index + 1}`}
                    disabled={busy || index === 0}
                    onClick={() => reorderShot(index, -1)}
                  >
                    <ArrowUp className="size-4" />
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label={`下移镜头 ${index + 1}`}
                    disabled={busy || index === shots.length - 1}
                    onClick={() => reorderShot(index, 1)}
                  >
                    <ArrowDown className="size-4" />
                  </Button>
                </div>
              </div>
              {(shot.takes || []).map((take: any, takeIndex: number) => {
                const needsReview =
                  take.source === "h3_video" &&
                  take.reviewStatus !== "approved";
                return (
                  <div
                    key={take.id}
                    className="flex flex-wrap items-center justify-between gap-3 border-t pt-3"
                  >
                    <a
                      href={mediaUrl(pid, take.assetId)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="min-w-0 break-all text-sm text-primary hover:underline"
                    >
                      候选 {takeIndex + 1} ·{" "}
                      {nameOf(assets.find((a: any) => a.id === take.assetId))}
                      {take.stale ? " · 旧修订" : ""} ↗
                    </a>
                    <Button
                      variant={
                        shot.selectedTakeId === take.id
                          ? "secondary"
                          : "outline"
                      }
                      size="sm"
                      disabled={
                        busy || shot.selectedTakeId === take.id || needsReview
                      }
                      onClick={() => adoptTake(shot, take)}
                    >
                      {shot.selectedTakeId === take.id ? (
                        <>
                          <Check className="mr-1 size-3" />
                          已采用
                        </>
                      ) : needsReview ? (
                        take.reviewStatus === "rejected" ? (
                          "人工已拒绝"
                        ) : (
                          "需先审片批准"
                        )
                      ) : (
                        "采用此候选"
                      )}
                    </Button>
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      </details>
      <details open={libraryOpen} onToggle={event => setLibraryOpen(event.currentTarget.open)} className="rounded-xl border bg-card p-5">
        <summary className="cursor-pointer text-sm font-semibold">
          已有视频素材 · 导入 / 上传
        </summary>
        <div className="mt-5 space-y-5">
          <p className="text-sm text-muted-foreground">
            已有视频也可以加入短剧。文件完成检查后归档，并可复用于多个镜头。
          </p>
          <div className="grid gap-5 md:grid-cols-2">
            <div className="space-y-3">
              <Label>已配置的参考影片库</Label>
              <Choice
                value={sourceId}
                onChange={setSourceId}
                options={library.map((source) => ({
                  id: source.sourceId,
                  title: source.title,
                }))}
                label="选择源视频"
                disabled={busy || !library.length}
              />
              <Button
                variant="outline"
                disabled={busy || !sourceId}
                onClick={() =>
                  run("正在导入并检查影片…", async () => {
                    await post(`/projects/${segment(pid)}/imports`, {
                      sourceId,
                    });
                    if (scopeRef.current !== scope) return;
                    await ctx.refresh();
                    ctx.notify("影片已验证并加入项目素材。", "success");
                  })
                }
              >
                <FolderInput className="mr-2 size-4" />
                导入影片
              </Button>
              {!library.length && (
                <p className="text-xs text-muted-foreground">
                  尚未配置影片库，可使用文件上传。
                </p>
              )}
            </div>
            <div className="space-y-3">
              <Label htmlFor="edit-video-upload">
                上传视频（最大 512 MiB）
              </Label>
              <Input
                id="edit-video-upload"
                type="file"
                accept="video/*,.mp4,.mov,.mkv,.webm"
                disabled={busy}
                onChange={(event) => setVideoFile(event.target.files?.[0])}
              />
              <Button
                variant="outline"
                disabled={busy || !videoFile}
                onClick={() =>
                  run("正在上传并检查视频…", async () => {
                    await upload(pid, videoFile);
                    if (scopeRef.current !== scope) return;
                    await ctx.refresh();
                    ctx.notify("视频已归档，可用它建立镜头。", "success");
                  })
                }
              >
                <Upload className="mr-2 size-4" />
                上传并检查
              </Button>
            </div>
          </div>
          <div className="grid gap-4 xl:grid-cols-2">
            {assets.filter(isVideoAsset).map((asset: any) => (
              <Card key={asset.id}>
                <CardContent className="space-y-3 p-4">
                  <div className="break-all text-sm font-medium">
                    {nameOf(asset)}
                  </div>
                  <details open={previewAssetId === asset.id} onToggle={event => { if (event.currentTarget.open) setPreviewAssetId(asset.id); else setPreviewAssetId(current => current === asset.id ? '' : current); }}>
                    <summary className="cursor-pointer text-xs text-muted-foreground">
                      预览素材
                    </summary>
                    <div className="mt-3">
                      {active && libraryOpen && previewAssetId === asset.id && <MediaPreview
                        pid={pid}
                        assetId={asset.id}
                        label={nameOf(asset)}
                      />}
                    </div>
                  </details>
                  <Label htmlFor={`import-name-${asset.id}`}>新镜头名称</Label>
                  <Input
                    id={`import-name-${asset.id}`}
                    value={
                      importNames[asset.id] ??
                      `镜头 ${String(shots.length + 1).padStart(2, "0")}`
                    }
                    disabled={busy}
                    onChange={(event) =>
                      setImportNames((current) => ({
                        ...current,
                        [asset.id]: event.target.value,
                      }))
                    }
                  />
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={busy || !eid}
                    onClick={() =>
                      run("正在建立镜头…", async () => {
                        const title = (
                          importNames[asset.id] ??
                          `镜头 ${String(shots.length + 1).padStart(2, "0")}`
                        ).trim();
                        if (!title) throw new Error("请填写镜头名称。");
                        await post(`/episodes/${segment(eid)}/shots`, {
                          title,
                          assetId: asset.id,
                        });
                        if (scopeRef.current !== scope) return;
                        await ctx.refresh();
                        ctx.notify(
                          "镜头和候选已建立，请在「镜头顺序与已有选片」中采用候选。",
                          "success",
                        );
                      })
                    }
                  >
                    <Plus className="mr-1 size-3" />
                    以此素材建镜头
                  </Button>
                  <details>
                    <summary className="cursor-pointer text-xs text-muted-foreground">
                      素材编号
                    </summary>
                    <p className="mt-1 break-all text-xs text-muted-foreground">
                      {asset.id}
                    </p>
                  </details>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      </details>
      <Dialog
        open={!!confirm}
        onOpenChange={(open) => {
          if (!open && !busy) setConfirm(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{confirm?.title}</DialogTitle>
            <DialogDescription>{confirm?.description}</DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setConfirm(null)}
            >
              取消
            </Button>
            <Button
              disabled={busy}
              onClick={() => {
                const action = confirm?.action;
                setConfirm(null);
                if (action) void run("正在保存操作…", action);
              }}
            >
              确认继续
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function ClipEditor({
  active,
  split,
  outputWidth,
  outputHeight,
  clip,
  index,
  count,
  take,
  pid,
  fps,
  unit,
  busy,
  fitModes,
  onChange,
  onTrimStart,
  onTrimEnd,
  move,
  remove,
  keepSourceDefault = true,
}: {
  active: boolean;
  split: (frame: number) => void;
  outputWidth: number;
  outputHeight: number;
  clip: Clip;
  index: number;
  count: number;
  take: any;
  pid: string;
  fps: number;
  unit: string;
  busy: boolean;
  fitModes: string[];
  keepSourceDefault?: boolean;
  onChange: (patch: Partial<Clip>) => void;
  onTrimStart: () => void;
  onTrimEnd: () => void;
  move: (delta: number) => void;
  remove: () => void;
}) {
  const player = useRef<HTMLVideoElement>(null),
    [inputs, setInputs] = useState<Record<string, string>>({}),
    [mediaError, setMediaError] = useState(false);
  const [sourceFrame, setSourceFrame] = useState(0);
  const [mediaReady, setMediaReady] = useState(false);
  const splitReady = sourceFrame > Number(clip.inFrame) && sourceFrame < Number(clip.inFrame) + Number(clip.frames);
  useEffect(() => {
    const video = player.current;
    return () => { if (video?.hasAttribute('src')) { video.pause(); video.removeAttribute('src'); video.load(); } };
  }, [active, take?.assetId]);
  useEffect(() => setInputs({}), [unit]);
  const formatted = (field: "inFrame" | "frames") =>
    clip[field] === ""
      ? ""
      : unit === "seconds"
        ? String(Number((Number(clip[field]) / fps).toFixed(6)))
        : String(clip[field]);
  return (
    <article
      id={`clip-${clip.clipId}`}
      className="overflow-hidden rounded-xl border"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 border-b px-4 py-3">
        <h3 className="text-sm font-semibold">
          <span className="mr-2 text-muted-foreground">
            {String(index + 1).padStart(2, "0")}
          </span>
          {take?.shotTitle || "候选不可用"}
        </h3>
        <div className="flex gap-1">
          <Button
            size="sm"
            variant="ghost"
            aria-label={`上移片段 ${index + 1}`}
            disabled={busy || index === 0}
            onClick={() => move(-1)}
          >
            <ArrowUp className="size-4" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label={`下移片段 ${index + 1}`}
            disabled={busy || index === count - 1}
            onClick={() => move(1)}
          >
            <ArrowDown className="size-4" />
          </Button>
          <Button
            size="sm"
            variant="ghost"
            aria-label={`移除片段 ${index + 1}`}
            disabled={busy}
            onClick={remove}
          >
            <Trash2 className="size-4" />
          </Button>
        </div>
      </div>
      <div className="grid gap-4 p-4 md:grid-cols-[minmax(180px,1fr)_2fr]">
        <div className="space-y-2">
          {take?.assetId && active ? (
            <video
              ref={player}
              className="w-full rounded-lg bg-black"
              style={{
                aspectRatio: `${outputWidth}/${outputHeight}`,
                objectFit: clip.fit === "cover" ? "cover" : "contain",
                objectPosition: `${(clip.crop?.x ?? 0.5) * 100}% ${(clip.crop?.y ?? 0.5) * 100}%`,
              }}
              controls
              preload="none"
              src={mediaUrl(pid, take.assetId)}
              aria-label={`片段 ${index + 1} 源视频预览`}
              onError={() => setMediaError(true)}
              onLoadedMetadata={e => { setMediaReady(true); e.currentTarget.currentTime = Number(clip.inFrame) / fps; setSourceFrame(Number(clip.inFrame)); }}
              onTimeUpdate={e => setSourceFrame(Math.round(e.currentTarget.currentTime * fps))}
            />
          ) : (
            <div className="flex aspect-video items-center justify-center rounded-lg bg-muted text-xs text-muted-foreground">
              源候选不可用，请检查选片
            </div>
          )}
          {mediaReady && <div className="space-y-2 rounded-lg border p-3" aria-label="拖动裁切范围">
            <label className="block text-xs">入点 · {(Number(clip.inFrame)/fps).toFixed(2)} 秒<input type="range" className="block w-full accent-primary" onPointerDown={onTrimStart} onPointerUp={onTrimEnd} onPointerCancel={onTrimEnd} onBlur={onTrimEnd} aria-label="拖动源入点" min={0} max={Math.max(0, Number(clip.inFrame)+Number(clip.frames)-1)} step={1} value={Number(clip.inFrame)} disabled={busy} onChange={e => { const value=Number(e.target.value), end=Number(clip.inFrame)+Number(clip.frames); onChange({inFrame:value,frames:end-value}); if(player.current) player.current.currentTime=value/fps; }} /></label>
            <label className="block text-xs">出点 · {((Number(clip.inFrame)+Number(clip.frames))/fps).toFixed(2)} 秒<input type="range" className="block w-full accent-primary" onPointerDown={onTrimStart} onPointerUp={onTrimEnd} onPointerCancel={onTrimEnd} onBlur={onTrimEnd} aria-label="拖动源出点" min={Number(clip.inFrame)+1} max={Math.max(Number(clip.inFrame)+Number(clip.frames), Math.floor((player.current?.duration || 0)*fps))} step={1} value={Number(clip.inFrame)+Number(clip.frames)} disabled={busy} onChange={e => {const end=Number(e.target.value);onChange({frames:end-Number(clip.inFrame)});if(player.current) player.current.currentTime=(end-1)/fps;}} /></label>
          </div>}
          {mediaError && (
            <p className="text-xs text-destructive">
              视频无法播放，请核查素材或下载检查。
            </p>
          )}
          <Button
            variant="ghost"
            size="sm"
            disabled={!take?.assetId}
            onClick={() => {
              if (player.current) {
                player.current.currentTime = (Number(clip.inFrame) || 0) / fps;
                void player.current.play().catch(() => setMediaError(true));
              }
            }}
          >
            <Film className="mr-1 size-3" />
            从入点预览源片
          </Button>
          <label className="block space-y-1 text-xs text-muted-foreground">
            <span>分割位置 · 源片第 {sourceFrame} 帧</span>
            <input type="range" className="w-full accent-primary" aria-label={`片段 ${index + 1} 分割位置`} min={Number(clip.inFrame) || 0} max={Math.max(Number(clip.inFrame) || 0, (Number(clip.inFrame) || 0) + (Number(clip.frames) || 1) - 1)} step={1} value={Math.max(Number(clip.inFrame) || 0, Math.min(sourceFrame, (Number(clip.inFrame) || 0) + (Number(clip.frames) || 1) - 1))} disabled={busy || !mediaReady} onChange={e => { const frame = Number(e.target.value); setSourceFrame(frame); if (player.current) { player.current.pause(); player.current.currentTime = frame / fps; } }} />
          </label>
          <Button variant="outline" size="sm" disabled={busy || !splitReady} onClick={() => split(sourceFrame)}>在播放位置分割</Button>
          <p className="text-xs text-muted-foreground">播放并暂停在保留区间内，即可分割为两段；两段总时长保持不变。</p>
        </div>
        <div className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            {(["inFrame", "frames"] as const).map((field) => (
              <div className="space-y-2" key={field}>
                <Label htmlFor={`${clip.clipId}-${field}`}>
                  {field === "inFrame" ? "源入点" : "保留长度"}（
                  {unit === "seconds" ? "秒" : "帧"}）
                </Label>
                <Input
                  id={`${clip.clipId}-${field}`}
                  aria-label={`片段 ${index + 1} ${field === "inFrame" ? "源入点" : "保留长度"}`}
                  type="number"
                  min={
                    field === "inFrame" ? 0 : unit === "seconds" ? 1 / fps : 1
                  }
                  step={unit === "seconds" ? "any" : 1}
                  value={inputs[field] ?? formatted(field)}
                  disabled={busy}
                  onChange={(event) => {
                    setInputs((current) => ({
                      ...current,
                      [field]: event.target.value,
                    }));
                    onChange({
                      [field]: editableFrames(event.target.value, unit, fps),
                    });
                  }}
                  onBlur={() =>
                    setInputs((current) => {
                      const next = { ...current };
                      delete next[field];
                      return next;
                    })
                  }
                />
              </div>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            保留源片第 {Number(clip.inFrame) || 0} 帧起的{" "}
            {Number(clip.frames) || 0} 帧；完整原片继续保留。
          </p>
          <div className="flex items-center gap-2">
            <Checkbox
              id={`${clip.clipId}-audio`}
              checked={clip.keepSourceAudio ?? keepSourceDefault}
              disabled={busy}
              onCheckedChange={(value) =>
                onChange({ keepSourceAudio: value === true })
              }
            />
            <Label htmlFor={`${clip.clipId}-audio`}>保留原声</Label>
          </div>
          <Choice
            value={clip.fit || "contain"}
            onChange={(value) =>
              onChange({ fit: value || "contain", crop: undefined })
            }
            options={[
              { id: "contain", title: "完整画面（留边）" },
              { id: "cover", title: "填满画幅（选择裁切位置）" },
            ].filter((option) => fitModes.includes(option.id))}
            label={`片段 ${index + 1} 画幅适配`}
            disabled={busy}
          />
          {clip.fit === "cover" && (
            <div className="space-y-2">
              <p className="text-xs text-muted-foreground">
                选择保留画面的位置。源片窗口仅展示裁切示意，导出代理片才是实际渲染预览。
              </p>
              <div
                className="grid w-40 grid-cols-3 gap-1"
                aria-label={`片段 ${index + 1} 裁切位置`}
              >
                {[0, 0.5, 1].flatMap((y, yi) =>
                  [0, 0.5, 1].map((x, xi) => (
                    <Button
                      key={`${x}-${y}`}
                      size="sm"
                      variant={
                        clip.crop?.x === x && clip.crop?.y === y
                          ? "default"
                          : "outline"
                      }
                      disabled={busy}
                      aria-label={`片段 ${index + 1} 裁切${["上", "中", "下"][yi]}${["左", "中", "右"][xi]}`}
                      onClick={() => onChange({ crop: { x, y } })}
                    >
                      {["上", "中", "下"][yi]}
                      {["左", "中", "右"][xi]}
                    </Button>
                  )),
                )}
              </div>
              {!clip.crop && (
                <p role="alert" className="text-xs text-amber-700">
                  尚未指定裁切位置，保存前请选择。
                </p>
              )}
            </div>
          )}
        </div>
      </div>
    </article>
  );
}
function MediaPreview({
  pid,
  assetId,
  label,
}: {
  pid: string;
  assetId: string;
  label: string;
}) {
  const [error, setError] = useState(false);
  return (
    <div className="space-y-2">
      <video
        className="aspect-video w-full rounded-lg bg-black"
        controls
        preload="none"
        src={mediaUrl(pid, assetId)}
        aria-label={label}
        onError={() => setError(true)}
      />
      {error && (
        <p role="alert" className="text-xs text-destructive">
          影片无法播放：请核查素材或下载检查。
        </p>
      )}
    </div>
  );
}
function ExportCard({
  mediaActive,
  item,
  pid,
  episodeTitle,
  busy,
  retry,
}: {
  mediaActive: boolean;
  item: any;
  pid: string;
  episodeTitle: string;
  busy: boolean;
  retry: () => void;
}) {
  const canRetry = ["FAILED", "INTERRUPTED"].includes(item.state),
    validId = typeof item.timelineId === "string" && !!item.timelineId.trim();
  return (
    <Card>
      <CardHeader>
        <div className="flex flex-wrap justify-between gap-2">
          <CardTitle className="text-base">
            成片 · {episodeTitle || "当前分集"}
          </CardTitle>
          <Badge
            variant={
              canRetry
                ? "destructive"
                : item.state === "SUCCEEDED"
                  ? "default"
                  : "secondary"
            }
          >
            {states[item.state] || item.state}
          </Badge>
        </div>
      </CardHeader>
      <CardContent className="space-y-4">
        {item.assetId && (
          <div className="max-w-3xl space-y-3">
            {mediaActive && <MediaPreview
              key={item.assetId}
              pid={pid}
              assetId={item.assetId}
              label="导出影片预览"
            />}
            <div className="flex flex-wrap gap-4 text-sm">
              <a
                className="text-primary hover:underline"
                href={mediaUrl(pid, item.assetId)}
                target="_blank"
                rel="noopener noreferrer"
              >
                打开 / 下载成片 ↗
              </a>
              <a
                className="text-primary hover:underline"
                href={mediaUrl(pid, item.assetId)}
                download={`drama-${item.id}.mp4`}
              >
                保存文件
              </a>
            </div>
          </div>
        )}
        {errorText(item.error) && (
          <p role="alert" className="text-sm text-destructive">
            {errorText(item.error)}
          </p>
        )}
        <details open={canRetry}>
          <summary className="cursor-pointer text-xs text-muted-foreground">
            导出记录与版本
          </summary>
          <p className="mt-2 break-all text-xs text-muted-foreground">
            导出 {item.id}
            <br />
            {validId
              ? `冻结时间线 ${item.timelineId}`
              : "缺少冻结时间线编号，无法按原版本重新导出。"}
          </p>
        </details>
        {canRetry && (
          <Button
            variant="outline"
            size="sm"
            disabled={busy || !validId}
            onClick={retry}
          >
            <RefreshCw className="mr-2 size-3" />
            按原版本重新导出
          </Button>
        )}
      </CardContent>
    </Card>
  );
}
