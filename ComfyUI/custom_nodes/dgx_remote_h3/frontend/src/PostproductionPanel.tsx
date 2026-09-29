import { sessionStore as sessionStorage } from "@/lib/workspace";
import { useEffect, useRef, useState } from "react";
import type { WorkbenchContext } from "@/lib/types";
import {
  get,
  post,
  requestKey,
  segment,
  mediaUrl,
  uploadAudio,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import {
  Card,
  CardHeader,
  CardTitle,
  CardContent,
  CardDescription,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Plus,
  Trash2,
  Save,
  Scissors,
  Play,
  RefreshCw,
  Upload,
  Check,
  Loader2,
} from "lucide-react";

export type AudioClip = {
  audioClipId: string;
  assetId: string;
  track: string;
  startFrame: number | string;
  sourceInFrame: number | string;
  frames: number | string;
  gainDb: number | string;
  fadeInFrames: number | string;
  fadeOutFrames: number | string;
};
export type SubtitleSegment = {
  startFrame: number | string;
  endFrame: number | string;
  text: string;
};
const tracks: Record<string, string> = {
  voiceover: "对白 / 旁白",
  ambience: "环境声",
  bgm: "背景音乐",
};
const names = (a: any) => a?.filename || a?.title || a?.id || "未选择";
const message = (error: any) => error?.message || String(error);
const knownRejection = (error: any) =>
  [400, 401, 403, 404, 409, 413, 415, 422].includes(error?.status) ||
  (error?.status === 503 &&
    ["ASR_WORKER_MISSING", "ASR_MODEL_MISSING"].includes(error?.code));
export function frame(value: number | string, label: string, min = 0) {
  const n = Number(value);
  if (String(value).trim() === "" || !Number.isSafeInteger(n) || n < min)
    throw new Error(`${label}须为不小于 ${min} 的整数帧。`);
  return n;
}
export function validateAudioClips(
  clips: AudioClip[],
  total: number,
  assets: any[],
  fps = 24,
) {
  if (clips.length > 32) throw new Error("最多可放置 32 段声音。");
  const ids = new Set();
  return clips.map((clip, index) => {
    const label = `声音 ${index + 1}`;
    if (!clip.audioClipId || ids.has(clip.audioClipId))
      throw new Error(`${label}编号重复。`);
    ids.add(clip.audioClipId);
    if (!tracks[clip.track]) throw new Error(`${label}请选择声轨。`);
    const asset = assets.find(
      (a) => a.id === clip.assetId && a.kind === "audio",
    );
    if (!asset) throw new Error(`${label}请先选择项目音频素材。`);
    const startFrame = frame(clip.startFrame, `${label}时间线起点`),
      sourceInFrame = frame(clip.sourceInFrame, `${label}源入点`),
      frames = frame(clip.frames, `${label}长度`, 1);
    const fadeInFrames = frame(clip.fadeInFrames, `${label}淡入`),
      fadeOutFrames = frame(clip.fadeOutFrames, `${label}淡出`),
      gainDb = Number(clip.gainDb);
    if (
      String(clip.gainDb).trim() === "" ||
      !Number.isFinite(gainDb) ||
      gainDb < -60 ||
      gainDb > 12
    )
      throw new Error(`${label}音量须在 -60 至 12 dB。`);
    if (startFrame + frames > total)
      throw new Error(`${label}超出完整时间线长度。`);
    const seconds = Number(
      asset.duration ?? asset.durationSeconds ?? asset.metadata?.duration,
    );
    if (
      Number.isFinite(seconds) &&
      sourceInFrame + frames > Math.floor(seconds * fps + 0.001)
    )
      throw new Error(`${label}超出音频源片长度。`);
    if (fadeInFrames + fadeOutFrames > frames)
      throw new Error(`${label}淡入与淡出总长度超过声音长度。`);
    return {
      ...clip,
      startFrame,
      sourceInFrame,
      frames,
      fadeInFrames,
      fadeOutFrames,
      gainDb,
    };
  });
}
export function validateSubtitles(segments: SubtitleSegment[], total: number) {
  let previous = 0;
  return segments.map((s, index) => {
    const startFrame = frame(s.startFrame, `字幕 ${index + 1} 入点`),
      endFrame = frame(s.endFrame, `字幕 ${index + 1} 出点`, 1);
    if (endFrame <= startFrame || endFrame > total || startFrame < previous)
      throw new Error(
        `字幕 ${index + 1} 时间须顺序排列、不重叠且位于当前时间线内。`,
      );
    if (!s.text.trim() || s.text.length > 1000)
      throw new Error(`字幕 ${index + 1} 文本须为 1–1000 字符。`);
    previous = endFrame;
    return { startFrame, endFrame, text: s.text };
  });
}
export function splitSubtitle(s: SubtitleSegment): SubtitleSegment[] {
  const start = frame(s.startFrame, "字幕入点"),
    end = frame(s.endFrame, "字幕出点");
  if (end - start < 2) throw new Error("至少两帧的字幕才能拆分。");
  const middle = Math.floor((start + end) / 2),
    split = Math.ceil(s.text.length / 2);
  return [
    { startFrame: start, endFrame: middle, text: s.text.slice(0, split) },
    { startFrame: middle, endFrame: end, text: s.text.slice(split) },
  ];
}

type Intent = { path: string; payload: any; key: string; kind: string };
export function readDraft(key: string): any {
  try {
    return JSON.parse(sessionStorage.getItem(key) || "null");
  } catch {
    return null;
  }
}
export function putDraft(key: string, value: any) {
  sessionStorage.setItem(key, JSON.stringify(value));
}
export function useDurablePost(
  scope: string,
  channel: string,
  onResult: (value: any, kind: string) => Promise<void> | void,
  notify: WorkbenchContext["notify"],
) {
  const storage = `dgx.drama.p5.pending:${scope}:${channel}`;
  const [pending, setPending] = useState<Intent | null>(() =>
      readDraft(storage),
    ),
    [busy, setBusy] = useState(false);
  const lock = useRef(false),
    currentScope = useRef(scope);
  currentScope.current = scope;
  useEffect(() => {
    setPending(readDraft(storage));
  }, [storage]);
  const send = async (intent: Intent) => {
    if (lock.current) return;
    if (
      !intent ||
      typeof intent.path !== "string" ||
      !intent.path.startsWith("/") ||
      typeof intent.key !== "string" ||
      !intent.key.startsWith("drama-ui-")
    )
      throw new Error("原提交身份无效，请核查保存记录。");
    lock.current = true;
    setBusy(true);
    try {
      putDraft(storage, intent);
      setPending(intent);
      const result = await post(intent.path, intent.payload, intent.key);
      if (currentScope.current !== scope) return;
      await onResult(result, intent.kind);
      sessionStorage.removeItem(storage);
      setPending(null);
    } catch (error) {
      if (knownRejection(error)) {
        sessionStorage.removeItem(storage);
        setPending(null);
      }
      notify(
        `${message(error)}${knownRejection(error) ? "" : " 原请求已保留，请按原键核查，勿创建新请求。"}`,
        "error",
      );
    } finally {
      lock.current = false;
      if (currentScope.current === scope) setBusy(false);
    }
  };
  return {
    pending,
    busy,
    execute: (path: string, payload: any, kind: string) =>
      pending
        ? Promise.resolve()
        : send({ path, payload, key: requestKey(), kind }),
    recover: () => (pending ? send(pending) : Promise.resolve()),
  };
}
export function PendingAction({
  pending,
  busy,
  recover,
}: {
  pending: any;
  busy: boolean;
  recover: () => any;
}) {
  return pending ? (
    <div
      role="alert"
      className="space-y-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-4"
    >
      <p className="text-sm">
        {busy
          ? "正在等待保存回执…"
          : "原提交结果尚未确认。已冻结原始参数，请核查原请求。"}
      </p>
      <p className="break-all text-xs text-muted-foreground">{pending.key}</p>
      <Button disabled={busy} variant="outline" onClick={recover}>
        按原请求键核查结果
      </Button>
    </div>
  ) : null;
}

export function AudioTracksEditor({
  mediaActive = true,
  ctx,
  clips,
  onChange,
  totalFrames,
  fps,
  disabled,
  onStatus,
}: {
  mediaActive?: boolean;
  ctx: WorkbenchContext;
  clips: AudioClip[];
  onChange: (clips: AudioClip[]) => void;
  totalFrames: number;
  fps: number;
  disabled: boolean;
  onStatus: (dirty: boolean, busy: boolean) => void;
}) {
  const audio = ctx.assets.filter((a: any) => a.kind === "audio"),
    pid = ctx.project.id;
  const [file, setFile] = useState<File>(),
    [uploading, setUploading] = useState(false),
    [uploadPending, setUploadPending] = useState<any>(() =>
      readDraft(`dgx.drama.audio.upload:${pid}`),
    );
  const [openAudioId, setOpenAudioId] = useState("");
  const key = `dgx.drama.audio.upload:${pid}`;
  useEffect(() => {
    onStatus(!!file || !!uploadPending, uploading);
  }, [file, uploadPending, uploading, onStatus]);
  useEffect(() => {
    setUploadPending(readDraft(key));
    setFile(undefined);
  }, [key]);
  const uploadFile = async () => {
    if (!file || uploading) return;
    setUploading(true);
    try {
      if (
        !file.size ||
        file.size > 256 * 1024 * 1024 ||
        !/\.(wav|mp3|m4a|flac)$/i.test(file.name)
      )
        throw new Error(
          "请选择非空、最大 256 MiB 的 WAV、MP3、M4A 或 FLAC 音频。",
        );
      if (uploadPending && uploadPending.filename !== file.name)
        throw new Error("请重新选择原文件名的音频，以保持请求身份。");
      const digest = Array.from(
        new Uint8Array(
          await crypto.subtle.digest("SHA-256", await file.arrayBuffer()),
        ),
        (b) => b.toString(16).padStart(2, "0"),
      ).join("");
      if (uploadPending && uploadPending.digest !== digest)
        throw new Error("请选择原提交的同一音频文件，以保持请求身份。");
      const intent = uploadPending || {
        key: requestKey(),
        digest,
        filename: file.name,
      };
      putDraft(key, intent);
      setUploadPending(intent);
      await uploadAudio(pid, file, intent.key);
      sessionStorage.removeItem(key);
      setUploadPending(null);
      setFile(undefined);
      await ctx.refresh();
      ctx.notify("声音素材已保存，可加入独立声轨。", "success");
    } catch (error) {
      if (knownRejection(error)) {
        sessionStorage.removeItem(key);
        setUploadPending(null);
      }
      ctx.notify(message(error), "error");
    } finally {
      setUploading(false);
    }
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle>声音</CardTitle>
        <CardDescription>
          对白、环境声与配乐可跨越多个镜头；与原声一起随时间线保存。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <fieldset disabled={disabled || uploading} className="space-y-4">
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-48 flex-1">
              <Label htmlFor="post-audio-file">导入声音（最大 256 MiB）</Label>
              <Input
                id="post-audio-file"
                type="file"
                accept=".wav,.mp3,.m4a,.flac"
                onChange={(e) => setFile(e.target.files?.[0])}
              />
            </div>
            <Button variant="outline" disabled={!file} onClick={uploadFile}>
              <Upload className="mr-2 size-4" />
              {uploading
                ? "正在上传…"
                : uploadPending
                  ? "按原键重新核查上传"
                  : "上传声音"}
            </Button>
          </div>
          {uploadPending && (
            <p role="alert" className="text-sm text-amber-700">
              原上传 {uploadPending.filename}{" "}
              尚需核查。重新选择原文件后按原键确认，或刷新素材库检查。请求{" "}
              {uploadPending.key}
            </p>
          )}
          {clips.map((clip, index) => {
            const asset = audio.find((a: any) => a.id === clip.assetId);
            const change = (patch: Partial<AudioClip>) =>
              onChange(
                clips.map((c, i) => (i === index ? { ...c, ...patch } : c)),
              );
            return (
              <details key={clip.audioClipId} open={openAudioId === clip.audioClipId} onToggle={e => { if (e.currentTarget.open) setOpenAudioId(clip.audioClipId); }} className="space-y-4 rounded-xl border p-4">
                <summary className="cursor-pointer text-sm font-medium">声音 {index + 1} · {tracks[clip.track] || clip.track} · {(Number(clip.startFrame)/fps).toFixed(2)}–{((Number(clip.startFrame)+Number(clip.frames))/fps).toFixed(2)} 秒 · {names(asset)}</summary>
                <div className="flex items-center justify-between">
                  <h4 className="text-sm font-semibold">
                    声音 {index + 1} · {names(asset)}
                  </h4>
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`移除声音 ${index + 1}`}
                    onClick={() =>
                      onChange(clips.filter((_, i) => i !== index))
                    }
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
                <div className="grid gap-3 sm:grid-cols-2">
                  <NativeChoice
                    label={`声音 ${index + 1} 素材`}
                    value={clip.assetId}
                    onChange={(assetId) => change({ assetId })}
                    options={audio.map((a: any) => [a.id, names(a)])}
                  />
                  <NativeChoice
                    label={`声音 ${index + 1} 声轨`}
                    value={clip.track}
                    onChange={(track) => change({ track })}
                    options={Object.entries(tracks)}
                  />
                </div>
                {asset && mediaActive && openAudioId === clip.audioClipId && (
                  <audio
                    controls
                    preload="none"
                    src={mediaUrl(pid, asset.id)}
                    aria-label={`声音 ${index + 1} 试听`}
                    className="w-full"
                  />
                )}
                <div className="grid grid-cols-2 gap-3 lg:grid-cols-3">
                  {(
                    [
                      ["startFrame", "时间线起点"],
                      ["sourceInFrame", "音频源入点"],
                      ["frames", "保留长度"],
                      ["gainDb", "音量 dB"],
                      ["fadeInFrames", "淡入"],
                      ["fadeOutFrames", "淡出"],
                    ] as const
                  ).map(([field, label]) => (
                    <div key={field} className="space-y-1">
                      <Label htmlFor={`${clip.audioClipId}-${field}`}>
                        {label}
                        {field !== "gainDb" && "（帧）"}
                      </Label>
                      <Input
                        id={`${clip.audioClipId}-${field}`}
                        aria-label={`声音 ${index + 1} ${label}`}
                        type="number"
                        value={clip[field]}
                        onChange={(e) => change({ [field]: e.target.value })}
                      />
                    </div>
                  ))}
                </div>
                <p className="text-xs text-muted-foreground">
                  时间线 {(Number(clip.startFrame) / fps).toFixed(2)}–
                  {(
                    (Number(clip.startFrame) + Number(clip.frames)) /
                    fps
                  ).toFixed(2)}{" "}
                  秒；整片 {(totalFrames / fps).toFixed(2)} 秒。
                </p>
              </details>
            );
          })}
          <Button
            variant="outline"
            disabled={clips.length >= 32 || !audio.length}
            onClick={() => { const id = requestKey(); setOpenAudioId(id);
              onChange([
                ...clips,
                {
                  audioClipId: id,
                  assetId: audio[0].id,
                  track: "voiceover",
                  startFrame: 0,
                  sourceInFrame: 0,
                  frames: Math.max(
                    1,
                    Math.min(
                      totalFrames,
                      Math.floor(
                        Number(audio[0].duration || totalFrames / fps) * fps,
                      ),
                    ),
                  ),
                  gainDb: 0,
                  fadeInFrames: 0,
                  fadeOutFrames: 0,
                },
              ]); }
            }
          >
            <Plus className="mr-2 size-4" />
            添加声音片段
          </Button>
          {!audio.length && (
            <p className="text-sm text-muted-foreground">
              先上传声音，再选择要使用的声轨。
            </p>
          )}
        </fieldset>
      </CardContent>
    </Card>
  );
}
function NativeChoice({
  label,
  value,
  options,
  onChange,
  disabled,
}: {
  label: string;
  value: string;
  options: string[][];
  onChange: (value: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-1">
      <Label>{label}</Label>
      <select
        aria-label={label}
        className="h-9 w-full rounded-md border bg-background px-3 text-sm"
        disabled={disabled}
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">请选择</option>
        {options.map(([id, name]) => (
          <option key={id} value={id}>
            {name}
          </option>
        ))}
      </select>
    </div>
  );
}

export function PostproductionPanel({
  mediaActive = true,
  onPreviewSubtitles,
  ctx,
  timeline,
  timelineDirty,
  disabled,
  exports,
  onExport,
  onStatus,
}: {
  mediaActive?: boolean;
  onPreviewSubtitles?: (segments: SubtitleSegment[]) => void;
  ctx: WorkbenchContext;
  timeline: any;
  timelineDirty: boolean;
  disabled: boolean;
  exports: any[];
  onExport: (item: any) => void;
  onStatus: (dirty: boolean, busy: boolean) => void;
}) {
  const pid = ctx.project.id,
    tid = timeline?.id || "",
    scope = `${pid}/${ctx.episode?.id || ""}`,
    storage = `dgx.drama.subtitle.draft:${scope}`;
  const stored = readDraft(storage);
  const [draftScope, setDraftScope] = useState(scope);
  const [segments, setSegments] = useState<SubtitleSegment[]>(
      stored?.segments || [],
    ),
    [dirty, setDirty] = useState(!!stored),
    [baseTimelineId, setBaseTimelineId] = useState(stored?.timelineId || tid);
  const [baseSubtitleRevision, setBaseSubtitleRevision] = useState(
    stored?.baseSubtitleRevision ?? 0,
  );
  const [subtitles, setSubtitles] = useState<any[]>([]),
    [transcriptions, setTranscriptions] = useState<any[]>([]),
    [selectedSubtitle, setSelectedSubtitle] = useState(""),
    [outputs, setOutputs] = useState(["master", "proxy", "report"]);
  const [loadedTimelineId, setLoadedTimelineId] = useState("");
  const restoredSubtitle = useRef("");
  const [reviewNotes, setReviewNotes] = useState<Record<string, string>>(
      stored?.reviewNotes || {},
    ),
    [confirm, setConfirm] = useState<{
      title: string;
      action: () => void;
    } | null>(null),
    [error, setError] = useState("");
  const player = useRef<HTMLVideoElement>(null),
    stopAt = useRef<number | null>(null),
    root = useRef<HTMLDivElement>(null),
    current = useRef({ scope, tid });
  current.current = { scope, tid };
  const fps = timeline?.fps || 24,
    total = (timeline?.clips || []).reduce(
      (sum: number, c: any) => sum + c.frames,
      0,
    );
  useEffect(() => { onPreviewSubtitles?.(segments); }, [segments, onPreviewSubtitles]);
  const latest = subtitles[0],
    selected = subtitles.find((s) => s.id === selectedSubtitle);
  const preview =
    exports
      .filter((e) => e.timelineId === tid && e.state === "SUCCEEDED")
      .flatMap(
        (e) =>
          e.outputs ||
          (e.assetId ? [{ role: "master", assetId: e.assetId }] : []),
      )
      .find((o) => o.role === "proxy") ||
    exports
      .filter((e) => e.timelineId === tid && e.state === "SUCCEEDED")
      .flatMap(
        (e) =>
          e.outputs ||
          (e.assetId ? [{ role: "master", assetId: e.assetId }] : []),
      )
      .find((o) => o.role === "master");
  const refresh = async () => {
    if (!tid) return;
    const results = await Promise.all([
      get(`/timeline-revisions/${segment(tid)}/subtitles`),
      get(`/timeline-revisions/${segment(tid)}/transcriptions`),
    ]);
    if (current.current.scope !== scope || current.current.tid !== tid) return;
    setSubtitles(results[0].subtitles || []);
    setTranscriptions(results[1].transcriptions || []);
    setLoadedTimelineId(tid);
  };
  useEffect(() => {
    const restorationKey = `${scope}/${tid}`;
    if (draftScope !== scope || loadedTimelineId !== tid || !tid || restoredSubtitle.current === restorationKey) return;
    restoredSubtitle.current = restorationKey;
    // Restore saved work on entry, while preserving any local or in-flight edits.
    if (dirty || segments.length || selectedSubtitle || readDraft(storage)?.segments?.length) return;
    const saved = subtitles.find(s => s.approved && !s.needsReview && s.sourceTimelineRevisionId === tid);
    if (!saved) return;
    setSelectedSubtitle(saved.id);
    setSegments(saved.segments.map((s: SubtitleSegment) => ({ ...s })));
    setBaseTimelineId(tid);
    setBaseSubtitleRevision(saved.revision);
  }, [scope, tid, draftScope, loadedTimelineId, subtitles, dirty, segments.length, selectedSubtitle, storage]);
  const applyResult = async (result: any, kind: string) => {
    if (kind === "subtitles") {
      if (!result.subtitle?.id) throw new Error("字幕保存回执缺少修订编号。");
      setDirty(false);
      setBaseSubtitleRevision(result.subtitle.revision);
      setBaseTimelineId(tid);
      setSelectedSubtitle(result.subtitle.id);
      sessionStorage.removeItem(storage);
      ctx.notify("字幕新修订已保存，校对后可单独批准。", "success");
    }
    if (result.export?.id) onExport(result.export);
    if (result.transcription?.id)
      setTranscriptions((value) => [
        result.transcription,
        ...value.filter((j) => j.id !== result.transcription.id),
      ]);
    if (kind.startsWith("approve-export:")) {
      const id = kind.split(":")[1];
      if (result.review) {
        const original = exports.find((e) => e.id === id);
        if (original) onExport({ ...original, review: result.review });
      }
      setReviewNotes((notes) => {
        const next = { ...notes };
        delete next[id];
        return next;
      });
    }
    await refresh();
  };
  const action = useDurablePost(
    scope,
    "postproduction",
    applyResult,
    ctx.notify,
  );
  const locked = disabled || action.busy || !!action.pending;
  const hasDraft =
    dirty || Object.values(reviewNotes).some((note) => !!note.trim());
  useEffect(() => {
    onStatus(hasDraft || !!action.pending, action.busy || !!action.pending);
  }, [hasDraft, !!action.pending, action.busy, onStatus]);
  useEffect(() => {
    const value = readDraft(storage);
    setDraftScope(scope);
    setSegments(value?.segments || []);
    setDirty(!!value?.dirty);
    setBaseSubtitleRevision(value?.baseSubtitleRevision ?? 0);
    setReviewNotes(value?.reviewNotes || {});
    setBaseTimelineId(value?.timelineId || tid);
    setSelectedSubtitle("");
    setSubtitles([]);
    setTranscriptions([]);
    setError("");
  }, [scope]);
  useEffect(() => {
    if (draftScope !== scope) return;
    if (hasDraft)
      putDraft(storage, {
        segments,
        dirty,
        timelineId: baseTimelineId,
        baseSubtitleRevision,
        reviewNotes,
      });
    else sessionStorage.removeItem(storage);
  }, [
    storage,
    scope,
    draftScope,
    segments,
    dirty,
    baseTimelineId,
    baseSubtitleRevision,
    reviewNotes,
    hasDraft,
  ]);
  useEffect(() => {
    setSelectedSubtitle("");
    refresh().catch((e) =>
      ctx.notify(`字幕记录读取失败：${message(e)}`, "error"),
    );
  }, [tid]);
  useEffect(() => {
    let polling = false;
    const timer = setInterval(async () => {
      if (
        polling ||
        action.busy ||
        !root.current?.checkVisibility() ||
        document.hidden ||
        !transcriptions.some((j) => ["QUEUED", "RUNNING"].includes(j.state))
      )
        return;
      polling = true;
      try {
        await refresh();
      } catch (e) {
        setError(`转写状态读取失败：${message(e)}`);
      } finally {
        polling = false;
      }
    }, 4000);
    return () => clearInterval(timer);
  }, [tid, transcriptions, action.busy]);
  const edit = (value: SubtitleSegment[]) => {
    setSegments(value);
    if (!dirty) setBaseSubtitleRevision(latest?.revision || 0);
    setDirty(true);
    if (!baseTimelineId) setBaseTimelineId(tid);
    setError("");
  };
  const load = (value: SubtitleSegment[], sourceTimeline = tid) => {
    const apply = () => {
      setSegments(value.map((s) => ({ ...s })));
      setBaseTimelineId(sourceTimeline);
      setBaseSubtitleRevision(latest?.revision || 0);
      setDirty(true);
      setError("");
    };
    if (dirty)
      setConfirm({ title: "用选定内容替换当前字幕草稿？", action: apply });
    else apply();
  };
  const attempt = async (fn: () => any) => {
    try {
      setError("");
      await fn();
    } catch (e) {
      setError(message(e));
      ctx.notify(message(e), "error");
    }
  };
  const saveSubtitles = () =>
    attempt(() => {
      if (!tid || timelineDirty)
        throw new Error("请先保存时间线，再保存其字幕。");
      if (baseTimelineId && baseTimelineId !== tid)
        throw new Error(
          "时间线已变化，请确认将保留草稿用于当前版本并重新校对时间。",
        );
      return action.execute(
        `/timeline-revisions/${segment(tid)}/subtitle-revisions`,
        {
          expectedRevision: baseSubtitleRevision,
          segments: validateSubtitles(segments, total),
        },
        "subtitles",
      );
    });
  const transcriptNames: Record<string, string> = {
    QUEUED: "等待识别",
    RUNNING: "正在识别",
    SUCCEEDED: "已生成待校对字幕",
    NEEDS_REVIEW: "结果已保留，需人工载入",
    FAILED: "识别失败",
    INTERRUPTED: "识别中断",
  };
  return (
    <div ref={root} className="space-y-6" aria-label="声音之后：字幕与交付">
      <PendingAction {...action} />
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {timelineDirty && (
        <p className="rounded-lg border p-3 text-sm text-muted-foreground">
          时间线有未保存修改。请先保存视频与声音，再进行字幕、识别和导出。
        </p>
      )}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap justify-between gap-3">
            <div>
              <CardTitle>字幕校对</CardTitle>
              <CardDescription>
                编辑时间与文字，另存修订，再批准用于交付。识别结果始终需要人工校对。
              </CardDescription>
            </div>
            <Badge variant="outline">
              {dirty
                ? "有未保存字幕"
                : latest
                  ? `最新修订 ${latest.revision}`
                  : "尚无字幕修订"}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <fieldset disabled={locked} className="space-y-5">
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                disabled={!tid || timelineDirty || !ctx.capabilities?.asrReady}
                onClick={() =>
                  action.execute(
                    `/timeline-revisions/${segment(tid)}/transcriptions`,
                    {},
                    "transcribe",
                  )
                }
              >
                从对白声轨识别字幕
              </Button>
              <Button
                variant="ghost"
                disabled={!tid}
                onClick={() => attempt(refresh)}
              >
                <RefreshCw className="mr-2 size-4" />
                刷新字幕与转写记录
              </Button>
            </div>
            {!ctx.capabilities?.asrReady && (
              <p className="text-xs text-muted-foreground">
                本地语音识别尚未配置；可以直接手工添加、保存和批准字幕。
              </p>
            )}
            {transcriptions.map((job) => (
              <div
                key={job.id}
                className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3"
              >
                <div>
                  <p className="text-sm">
                    {transcriptNames[job.state] || job.state}
                  </p>
                  {job.error && (
                    <p className="text-xs text-muted-foreground">
                      {message(job.error)}
                    </p>
                  )}
                </div>
                {job.state === "NEEDS_REVIEW" &&
                  job.result?.segments?.length > 0 && (
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => load(job.result.segments)}
                    >
                      载入识别结果到草稿
                    </Button>
                  )}
                {(job.result?.rawText || job.result?.validationError) && (
                  <details className="w-full text-sm">
                    <summary className="cursor-pointer text-muted-foreground">
                      原始识别结果与校验详情
                    </summary>
                    <div className="mt-3 space-y-3">
                      {job.result.validationError && (
                        <p role="alert" className="text-sm text-destructive">
                          时间或格式校验未通过：
                          {String(job.result.validationError)}
                        </p>
                      )}
                      {job.result.requiresCorrection && (
                        <p className="text-xs text-muted-foreground">
                          原始结果已保留。请根据原文手工添加和校对字幕，再另存修订；空结果不会替换现有字幕草稿。
                        </p>
                      )}
                      <Textarea
                        aria-label="原始识别结果（只读）"
                        readOnly
                        rows={8}
                        value={String(job.result.rawText || "")}
                      />
                      <p className="break-all text-xs text-muted-foreground">
                        原始结果校验值：
                        {String(job.result.rawSha256 || "未提供")}
                      </p>
                    </div>
                  </details>
                )}
              </div>
            ))}
            {subtitles.length > 0 && (
              <div className="flex flex-wrap items-end gap-2">
                <div className="min-w-48 flex-1">
                  <NativeChoice
                    label="字幕修订记录"
                    value={selectedSubtitle}
                    onChange={setSelectedSubtitle}
                    options={subtitles.map((s) => [
                      s.id,
                      `修订 ${s.revision} · ${s.needsReview ? "需重新校对" : s.approved ? "已批准" : "待批准"}`,
                    ])}
                  />
                </div>
                <Button
                  variant="outline"
                  disabled={!selected}
                  onClick={() =>
                    load(
                      selected.segments,
                      selected.sourceTimelineRevisionId || tid,
                    )
                  }
                >
                  载入选定字幕修订
                </Button>
                <Button
                  variant="outline"
                  disabled={
                    !selected ||
                    selected.id !== latest?.id ||
                    selected.approved ||
                    selected.needsReview ||
                    dirty ||
                    timelineDirty
                  }
                  onClick={() =>
                    action.execute(
                      `/subtitle-revisions/${segment(selected.id)}/approve`,
                      { timelineId: tid },
                      "approve-subtitles",
                    )
                  }
                >
                  批准选定字幕修订
                </Button>
              </div>
            )}
            {dirty &&
              baseTimelineId === tid &&
              latest &&
              latest.revision !== baseSubtitleRevision && (
                <div role="alert" className="rounded-lg border p-3 text-sm">
                  服务端出现新的字幕修订，当前文字草稿已保留。
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() =>
                      setConfirm({
                        title:
                          "确认以当前字幕修订为基线另存这份人工草稿？请先比较并合并需要保留的文字。",
                        action: () => setBaseSubtitleRevision(latest.revision),
                      })
                    }
                  >
                    确认合并后的保存基线
                  </Button>
                </div>
              )}
            {baseTimelineId && baseTimelineId !== tid && dirty && (
              <div
                role="alert"
                className="rounded-lg border border-amber-500/50 p-3 text-sm"
              >
                字幕草稿来自另一时间线，文字仍然保留。
                <Button
                  variant="outline"
                  size="sm"
                  className="ml-2"
                  disabled={timelineDirty}
                  onClick={() =>
                    setConfirm({
                      title:
                        "确认将保留的字幕草稿用于当前时间线？请重新校对每条时间。",
                      action: () => {
                        setBaseTimelineId(tid);
                        setBaseSubtitleRevision(latest?.revision || 0);
                      },
                    })
                  }
                >
                  确认用于当前时间线
                </Button>
              </div>
            )}
            {preview ? (
              <div className="space-y-2">
                {mediaActive && <video
                  ref={player}
                  key={preview.assetId}
                  src={mediaUrl(pid, preview.assetId)}
                  controls
                  preload="metadata"
                  className="aspect-video w-full max-w-3xl rounded-lg bg-black"
                  aria-label="字幕区间成片预览"
                  onTimeUpdate={(e) => {
                    if (
                      stopAt.current !== null &&
                      e.currentTarget.currentTime >= stopAt.current
                    ) {
                      e.currentTarget.pause();
                      stopAt.current = null;
                    }
                  }}
                />}
                <p className="text-xs text-muted-foreground">
                  当前时间线已渲染的{preview.role === "proxy" ? "代理" : "主片"}
                  ，包含已保存的裁切与混音。
                </p>
              </div>
            ) : (
              <p className="text-xs text-muted-foreground">
                先导出当前时间线的代理片，即可按字幕区间试听与播放；源片不能代表整片混音。
              </p>
            )}
            <div className="space-y-3">
              {segments.map((s, index) => (
                <article
                  className="space-y-3 rounded-xl border p-4"
                  key={index}
                >
                  <div className="grid grid-cols-2 gap-3">
                    <div>
                      <Label htmlFor={`subtitle-${index}-start`}>
                        入点（帧）
                      </Label>
                      <Input
                        id={`subtitle-${index}-start`}
                        aria-label={`字幕 ${index + 1} 入点`}
                        type="number"
                        value={s.startFrame}
                        onChange={(e) =>
                          edit(
                            segments.map((v, i) =>
                              i === index
                                ? { ...v, startFrame: e.target.value }
                                : v,
                            ),
                          )
                        }
                      />
                    </div>
                    <div>
                      <Label htmlFor={`subtitle-${index}-end`}>
                        出点（帧）
                      </Label>
                      <Input
                        id={`subtitle-${index}-end`}
                        aria-label={`字幕 ${index + 1} 出点`}
                        type="number"
                        value={s.endFrame}
                        onChange={(e) =>
                          edit(
                            segments.map((v, i) =>
                              i === index
                                ? { ...v, endFrame: e.target.value }
                                : v,
                            ),
                          )
                        }
                      />
                    </div>
                  </div>
                  <p className="text-xs text-muted-foreground">
                    {(Number(s.startFrame) / fps).toFixed(3)}–
                    {(Number(s.endFrame) / fps).toFixed(3)} 秒
                  </p>
                  <Textarea
                    aria-label={`字幕 ${index + 1} 文字`}
                    value={s.text}
                    onChange={(e) =>
                      edit(
                        segments.map((v, i) =>
                          i === index ? { ...v, text: e.target.value } : v,
                        ),
                      )
                    }
                  />
                  <div className="flex flex-wrap gap-1">
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={!preview}
                      onClick={() =>
                        attempt(() => {
                          validateSubtitles([s], total);
                          if (player.current) {
                            player.current.currentTime =
                              Number(s.startFrame) / fps;
                            stopAt.current = Number(s.endFrame) / fps;
                            return player.current.play();
                          }
                        })
                      }
                    >
                      <Play className="mr-1 size-3" />
                      播放区间
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      onClick={() =>
                        attempt(() =>
                          edit([
                            ...segments.slice(0, index),
                            ...splitSubtitle(s),
                            ...segments.slice(index + 1),
                          ]),
                        )
                      }
                    >
                      <Scissors className="mr-1 size-3" />
                      拆分字幕 {index + 1}
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={index === segments.length - 1}
                      onClick={() =>
                        edit([
                          ...segments.slice(0, index),
                          {
                            startFrame: s.startFrame,
                            endFrame: segments[index + 1].endFrame,
                            text: `${s.text} ${segments[index + 1].text}`.trim(),
                          },
                          ...segments.slice(index + 2),
                        ])
                      }
                    >
                      与下一条合并
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      aria-label={`删除字幕 ${index + 1}`}
                      onClick={() =>
                        edit(segments.filter((_, i) => i !== index))
                      }
                    >
                      <Trash2 className="size-3" />
                    </Button>
                  </div>
                </article>
              ))}
            </div>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                onClick={() => {
                  const start = segments.length
                    ? Number(segments[segments.length - 1].endFrame)
                    : 0;
                  edit([
                    ...segments,
                    {
                      startFrame: start,
                      endFrame: Math.min(total || start + 48, start + 48),
                      text: "",
                    },
                  ]);
                }}
              >
                <Plus className="mr-2 size-4" />
                添加字幕
              </Button>
              <Button
                disabled={!tid || timelineDirty || !dirty}
                onClick={saveSubtitles}
              >
                <Save className="mr-2 size-4" />
                保存字幕新修订
              </Button>
              <Button
                variant="ghost"
                disabled={!dirty}
                onClick={() =>
                  setConfirm({
                    title: "清除尚未保存的字幕草稿？",
                    action: () => {
                      setSegments([]);
                      setDirty(false);
                      setBaseTimelineId(tid);
                    },
                  })
                }
              >
                清除字幕草稿
              </Button>
            </div>
          </fieldset>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>交付与全片审片</CardTitle>
          <CardDescription>
            选择需要的文件。技术导出完成后，播放全片并填写审片意见，再明确批准。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <fieldset
            disabled={locked || timelineDirty || !tid}
            className="space-y-4"
          >
            <div className="flex flex-wrap gap-4">
              {Object.entries({
                master: "高清主片",
                proxy: "轻量代理",
                burned: "字幕烧录片",
                srt: "SRT 字幕",
                ass: "ASS 字幕",
                report: "验收报告",
              }).map(([id, label]) => (
                <label className="flex items-center gap-2 text-sm" key={id}>
                  <Checkbox
                    checked={outputs.includes(id)}
                    onCheckedChange={(yes) =>
                      setOutputs((current) =>
                        yes
                          ? [...current, id]
                          : current.filter((v) => v !== id),
                      )
                    }
                  />
                  {label}
                </label>
              ))}
            </div>
            <p className="text-xs text-muted-foreground">
              烧录片与字幕文件使用上方选定且已批准的字幕修订。
            </p>
            <Button
              data-export-primary
              disabled={!outputs.length}
              onClick={() =>
                attempt(() => {
                  if (
                    outputs.some((o) => ["burned", "srt", "ass"].includes(o)) &&
                    (!selected?.approved ||
                      selected?.needsReview ||
                      selected.id !== latest?.id)
                  )
                    throw new Error("请先选择并批准当前最新字幕修订。");
                  return action.execute(
                    `/timeline-revisions/${segment(tid)}/exports`,
                    {
                      outputs,
                      subtitleRevisionId: outputs.some((o) =>
                        ["burned", "srt", "ass"].includes(o),
                      )
                        ? selected.id
                        : null,
                      presetVersion: "drama-export-v2",
                    },
                    "export",
                  );
                })
              }
            >
              提交选定交付文件
            </Button>
          </fieldset>
          {exports.map((item) => (
            <DeliveryCard
              mediaActive={mediaActive}
              key={item.id}
              item={item}
              pid={pid}
              note={reviewNotes[item.id] || ""}
              onNote={(note) =>
                setReviewNotes((value) => ({ ...value, [item.id]: note }))
              }
              disabled={locked}
              retry={() =>
                action.execute(
                  `/exports/${segment(item.id)}/retry`,
                  {},
                  "export",
                )
              }
              approve={() =>
                action.execute(
                  `/exports/${segment(item.id)}/approve`,
                  { note: reviewNotes[item.id] },
                  `approve-export:${item.id}`,
                )
              }
            />
          ))}
        </CardContent>
      </Card>
      <Dialog
        open={!!confirm}
        onOpenChange={(open) => {
          if (!open) setConfirm(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{confirm?.title}</DialogTitle>
            <DialogDescription>
              已保存的字幕、影片与识别结果继续保留；本次操作仅改变当前编辑草稿。
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirm(null)}>
              返回继续编辑
            </Button>
            <Button
              onClick={() => {
                confirm?.action();
                setConfirm(null);
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
function DeliveryCard({
  mediaActive,
  item,
  pid,
  note,
  onNote,
  disabled,
  retry,
  approve,
}: {
  mediaActive: boolean;
  item: any;
  pid: string;
  note: string;
  onNote: (value: string) => void;
  disabled: boolean;
  retry: () => any;
  approve: () => any;
}) {
  const result =
      item.outputs ||
      (item.assetId ? [{ role: "master", assetId: item.assetId }] : []),
    videoOutputs = result.filter((o: any) =>
      ["master", "proxy", "burned"].includes(o.role),
    );
  const [role, setRole] = useState(""),
    [mediaError, setMediaError] = useState(false);
  const active =
      videoOutputs.find((o: any) => o.role === role) || videoOutputs[0],
    approved = item.review?.status === "approved";
  const labels: Record<string, string> = {
    master: "高清主片",
    proxy: "轻量代理",
    burned: "字幕烧录片",
    srt: "SRT 字幕",
    ass: "ASS 字幕",
    report: "验收报告",
  };
  return (
    <article className="space-y-4 rounded-xl border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="text-sm font-semibold">
          交付任务 · {item.id.slice(0, 8)}
        </h4>
        <Badge variant={approved ? "default" : "secondary"}>
          {approved
            ? "全片已批准"
            : item.state === "SUCCEEDED"
              ? "导出完成 · 可审片"
              : (
                  {
                    QUEUED: "等待导出",
                    RUNNING: "正在导出",
                    FAILED: "导出失败",
                    INTERRUPTED: "导出中断",
                  } as any
                )[item.state] || item.state}
        </Badge>
      </div>
      {active && (
        <>
          <NativeChoice
            label={`交付 ${item.id} 播放版本`}
            value={active.role}
            onChange={setRole}
            options={videoOutputs.map((o: any) => [o.role, labels[o.role]])}
          />
          {mediaActive && <video
            key={active.assetId}
            src={mediaUrl(pid, active.assetId)}
            controls
            preload="none"
            aria-label="交付成片预览"
            className="aspect-video w-full max-w-3xl rounded-lg bg-black"
            onError={() => setMediaError(true)}
          />}
          {mediaError && (
            <p role="alert" className="text-sm text-destructive">
              影片无法播放，请核查归档素材。
            </p>
          )}
        </>
      )}
      <div className="flex flex-wrap gap-4 text-sm">
        {result.map((o: any) => (
          <a
            key={o.role}
            href={mediaUrl(pid, o.assetId)}
            download
            className="text-primary hover:underline"
          >
            下载{labels[o.role] || o.role}
          </a>
        ))}
      </div>
      {item.error && (
        <p role="alert" className="text-sm text-destructive">
          {message(item.error)}
        </p>
      )}
      {["FAILED", "INTERRUPTED"].includes(item.state) && (
        <Button variant="outline" disabled={disabled} onClick={retry}>
          按原完整配方重新导出
        </Button>
      )}
      {item.state === "SUCCEEDED" && (
        <div className="space-y-2">
          <Label htmlFor={`delivery-note-${item.id}`}>全片审片意见</Label>
          <Textarea
            id={`delivery-note-${item.id}`}
            value={note}
            disabled={disabled || approved}
            onChange={(e) => onNote(e.target.value)}
            placeholder="检查全片画面、声音、字幕与结尾，记录结论。"
          />
          <Button
            disabled={disabled || approved || !note.trim()}
            onClick={approve}
          >
            <Check className="mr-2 size-4" />
            {approved ? "全片已批准" : "批准本次完整交付"}
          </Button>
          {approved && (
            <p className="text-sm text-muted-foreground">{item.review.note}</p>
          )}
        </div>
      )}
      <details>
        <summary className="cursor-pointer text-xs text-muted-foreground">
          冻结版本与交付配方
        </summary>
        <p className="break-all text-xs text-muted-foreground">
          时间线 {item.timelineId} · {item.recipe?.presetVersion || "旧版配方"}{" "}
          ·{" "}
          {(item.recipe?.outputs || result.map((o: any) => o.role))
            .map((o: string) => labels[o] || o)
            .join("、")}
        </p>
      </details>
    </article>
  );
}
