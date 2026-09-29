import { ReviewWorkspace } from "./ReviewWorkspace";
import { sessionStore as sessionStorage } from "@/lib/workspace";
import { useCallback, useEffect, useRef, useState } from "react";
import {
  generationOf,
  generationForReferences,
  ring4Generation,
  generationName,
  type Generation,
} from "@/lib/video-contract";
import type { WorkbenchContext } from "@/lib/types";
import { isResident, RESIDENT_MESSAGE } from "@/lib/resource-status";
import { buildShotPrompts, isCompleteRef2vaPrompt, promptAfterShotReload, videoDraftPrompt } from "@/lib/production-draft";
import { nodesFromSnapshot, numberNodes } from "@/lib/reference-nodes";
import {
  get,
  post,
  segment,
  requestKey,
  mediaUrl,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
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
  Clock3,
  Film,
  Loader2,
  Play,
  Plus,
  RefreshCw,
  ShieldCheck,
  Trash2,
  X,
} from "lucide-react";

type Draft = {
  promptJobId?: string;
  candidateCount?: number;
  generation?: Generation;
  savedGeneration?: Generation;
  productionRevision?: number;
  snapshot?: any;
  soundMode?: "silent" | "native" | "reference";
  dialogue?: string;
  shotId: string;
  shotRevisionId: string;
  title?: string;
  imageAssetId?: string;
  imageExtension?: string;
  actionImageAssetId?: string;
  actionImageExtension?: string;
  videoAssetId?: string;
  audioDescription?: string;
  approved?: boolean;
  needsReview?: boolean;
  prompt: string;
  seed: number | string;
  audioAssetId: string;
  pendingRecovered?: boolean;
};
type Pending = {
  key: string;
  payload: {
    shots: {
      generation?: Generation;
      candidateCount?: number;
      shotId: string;
      shotRevisionId: string;
      audioAssetId: string;
      actionImageAssetId?: string;
      videoAssetId?: string;
      audioDescription?: string;
      prompt: string;
      seed: number;
    }[];
  };
  persisted?: boolean;
};
const terminal = new Set(["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"]);
const labels: Record<string, string> = {
  QUEUED: "等待派发",
  BLOCKED: "等待资源或依赖",
  DISPATCHING: "正在派发",
  SUBMISSION_UNKNOWN: "提交结果待核查",
  SUBMITTED: "已提交原任务",
  RUNNING: "正在生成",
  LOADING: "正在加载模型",
  UPLOADING: "正在传入参考素材",
  RELEASING: "正在处理模型资源",
  RECONCILING: "正在核查资源状态",
  CANCELING: "正在取消并回收资源",
  COLLECTING: "归档与媒体检查",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  CANCEL_REQUESTED: "已请求取消",
  CANCELED: "已取消",
  INTERRUPTED: "已中断",
  NOT_SUBMITTED: "确定未发出",
  REQUEST_STARTED: "请求已开始",
  RECEIVED: "已收到结果",
  UNKNOWN: "结果待核查",
  OUTPUT_UNAVAILABLE: "原片暂不可用",
  INVALID: "媒体检查未通过",
};
const stateLabel = (value: string) => labels[value] || value || "等待状态";
const errorText = (error: any): string =>
  typeof error === "string" ? error : error?.message || "";
const audioAsset = (asset: any) =>
  asset.role === "h3_reference" &&
  (asset.kind === "audio" ||
    String(asset.contentType || "").startsWith("audio/"));
const nameOf = (asset: any) =>
  asset.filename || asset.title || asset.name || asset.id;
const receiptKey = (pid: string, eid: string) =>
  `dgx.drama.video.pending.v1:${pid}:${eid}`;

export function videoPrompt(draft: Draft) {
  const stored = draft.prompt.trim();
  if (draft.promptJobId || isCompleteRef2vaPrompt(stored)) return stored;
  const visual = stored;
  const referenced = Boolean(draft.imageAssetId || draft.actionImageAssetId || draft.videoAssetId || draft.audioAssetId);
  const soundMode = draft.soundMode || (draft.audioAssetId ? "reference" : "silent");
  if (soundMode === "native" && !draft.dialogue?.trim())
    throw new Error("原生对白请填写要说的台词。");
  if (!stored && !draft.audioDescription?.trim() && soundMode !== "native") return "";
  const sound = soundMode === "native"
    ? `Generate synchronized native dialogue and preserve the exact Chinese line: <d>[Chinese] ${draft.dialogue!.trim()}</d>. Do not add any other speech.`
    : soundMode === "reference"
      ? `Use <Audio 1> as an audio reference. ${draft.audioDescription?.trim() || "Follow its rhythm and sound texture without inventing dialogue."}`
      : "No dialogue, narration, speech, or human voice.";
  const music = soundMode === "reference"
    ? "Follow only the music or rhythm explicitly present in <Audio 1>; do not add unrelated music."
    : "No non-diegetic music unless explicitly requested in the director instruction.";
  if (!referenced) return [
    `integrated_multimodal_description: ${visual}`,
    `overall_soundscape: ${sound}`,
    `non_diegetic_music: ${music}`,
  ].join("\n\n");
  const subjects = [
    draft.imageAssetId ? "<Picture 1> is the primary identity, appearance, wardrobe, and composition reference." : "",
    draft.actionImageAssetId ? `<Picture ${draft.imageAssetId ? 2 : 1}> is the pose and body-mechanics reference only; preserve identity from the primary reference when present.` : "",
    draft.videoAssetId ? "<Video 1> is the motion cadence, body mechanics, and camera-path reference; do not copy unrelated identity or background." : "",
    draft.audioAssetId ? "<Audio 1> is the rhythm and sound-texture reference." : "",
  ].filter(Boolean).join(" ");
  return [
    `subject_definitions: ${subjects}`,
    `summary: Create one coherent H3 Ring4 shot from the supplied optional references and the director instruction.`,
    `retention_analysis: Retain subject identity and wardrobe from the primary picture. Retain only pose/body mechanics from the action picture, only motion/camera cadence from the video, and only the requested sound properties from the audio.`,
    `detailed_description: ${visual}`,
    `overall_soundscape: ${sound}`,
    `non_diegetic_music: ${music}`,
  ].join("\n\n");
}
export function generationChanged(draft: Draft) {
  return !!draft.savedGeneration && generationOf(draft.generation).requestDurationSeconds !== generationOf(draft.savedGeneration).requestDurationSeconds;
}
const hasReference = (draft: Draft) => Boolean(draft.imageAssetId || draft.actionImageAssetId || draft.videoAssetId || draft.audioAssetId);
function h3Outbound(draft: Draft) {
  const snapshot = draft.snapshot || {};
  const numbered = numberNodes(nodesFromSnapshot(snapshot));
  const pictures = numbered.filter((item) => item.label === "Picture");
  const videos = numbered.filter((item) => item.label === "Video");
  if (!videos.length && draft.videoAssetId) {
    videos.push({ id: "draft-video", media: "video", assetId: draft.videoAssetId, role: "action", description: "", label: "Video", index: 1 });
  }
  if (!pictures.length) {
    if (draft.imageAssetId) pictures.push({ id: "draft-image", media: "image", assetId: draft.imageAssetId, role: "character", description: "", label: "Picture", index: 1 });
    if (draft.actionImageAssetId) pictures.push({ id: "draft-action", media: "image", assetId: draft.actionImageAssetId, role: "action", description: "", label: "Picture", index: pictures.length + 1 });
  }
  const saved = String(snapshot.prompt || "").trim();
  const usesSaved = !draft.promptJobId && isCompleteRef2vaPrompt(saved);
  let prompt = saved;
  try {
    prompt = usesSaved ? saved : videoPrompt(draft);
  } catch (error) {
    prompt = error instanceof Error ? error.message : "提示词还不能提交";
  }
  return { prompt, pictures, videos, unsaved: usesSaved && draft.prompt.trim() !== saved };
}
export function buildVideoBatch(drafts: Draft[]) {
  if (!Array.isArray(drafts) || drafts.length < 1 || drafts.length > 32)
    throw new Error("每批次请选择 1–32 个镜头。");
  if (drafts.some(d => !Number.isInteger(d.candidateCount ?? 1) || (d.candidateCount ?? 1) < 1 || (d.candidateCount ?? 1) > 4)) throw new Error("每个镜头请选择1至4个候选。");
  if (drafts.reduce((n, d) => n + (d.candidateCount ?? 1), 0) > 32) throw new Error("每批最多生成32个视频，请减少镜头或候选数量。");
  const seen = new Set<string>();
  return {
    shots: drafts.map((draft, index) => {
      if (
        !draft.shotId ||
        !draft.shotRevisionId
      )
        throw new Error(
          `镜头 ${index + 1} 尚未保存内容版本，请先保存镜头。`,
        );
      if (draft.imageAssetId && !draft.approved) throw new Error(`镜头 ${index + 1} 已选主参考图尚未批准，请先确认或移除。`);
      if (draft.needsReview)
        throw new Error(
          `镜头 ${index + 1} 的人物或场景已更新，请先复核并重新读取镜头版本。`,
        );
      if (seen.has(draft.shotId))
        throw new Error("同一批次不能重复加入同一个镜头。");
      seen.add(draft.shotId);
      if (generationChanged(draft)) throw new Error(`镜头 ${index + 1} 生成设置尚未保存并确认参考画面。`);
      const prompt = videoPrompt(draft);
      if ((draft.soundMode || (draft.audioAssetId ? "reference" : "silent")) === "reference" && !draft.audioAssetId)
        throw new Error(`镜头 ${index + 1} 选择了参考声音，但尚未选择已验证音频。`);
      if (typeof draft.prompt !== "string")
        throw new Error(`镜头 ${index + 1} 的视频提示词须为文字，可留空。`);
      if (new TextEncoder().encode(prompt).length > 16384)
        throw new Error(`镜头 ${index + 1} 组合提示词不得超过 16384 UTF-8 字节。`);
      if (
        draft.imageAssetId && draft.imageExtension &&
        !["png", "jpg"].includes(draft.imageExtension)
      )
        throw new Error(`镜头 ${index + 1} 的参考图片必须为 PNG 或 JPEG。`);
      if (draft.actionImageAssetId && draft.actionImageExtension && !["png", "jpg"].includes(draft.actionImageExtension))
        throw new Error(`镜头 ${index + 1} 的动作参考图必须为 PNG 或 JPEG。`);
      const seed = Number(draft.seed);
      if (
        String(draft.seed).trim() === "" ||
        !Number.isSafeInteger(seed) ||
        seed < 0 ||
        seed > 2147483647
      )
        throw new Error(`镜头 ${index + 1} 种子须为 0–2147483647 的整数。`);
      return {
        ...(!hasReference(draft) || draft.generation
          ? { generation: generationForReferences(draft.generation, hasReference(draft)) }
          : {}),
        ...(draft.promptJobId ? { promptJobId: draft.promptJobId } : {}),
        ...((draft.candidateCount ?? 1) > 1 ? { candidateCount: draft.candidateCount } : {}),
        shotId: draft.shotId,
        shotRevisionId: draft.shotRevisionId,
        audioAssetId: draft.audioAssetId,
        actionImageAssetId: draft.actionImageAssetId || "",
        videoAssetId: draft.videoAssetId || "",
        audioDescription: draft.audioDescription?.trim() || "",
        prompt,
        seed,
      };
    }),
  };
}
export function readPending(pid: string, eid: string): Pending | null {
  try {
    const value = JSON.parse(
      sessionStorage.getItem(receiptKey(pid, eid)) || "null",
    );
    if (
      value?.version !== 1 ||
      value.projectId !== pid ||
      value.episodeId !== eid ||
      typeof value.key !== "string" ||
      !value.key.startsWith("drama-ui-") ||
      !Array.isArray(value.payload?.shots) ||
      !value.payload.shots.length ||
      value.payload.shots.length > 32 ||
      value.payload.shots.some(
        (shot: any) =>
          !shot ||
          typeof shot.shotId !== "string" ||
          typeof shot.shotRevisionId !== "string" ||
          typeof shot.audioAssetId !== "string" ||
          typeof shot.prompt !== "string" ||
          !Number.isSafeInteger(shot.seed),
      )
    )
      return null;
    for (const shot of value.payload.shots) generationOf(shot.generation);
    return { key: value.key, payload: value.payload, persisted: true };
  } catch {
    return null;
  }
}
export function savePending(
  pid: string,
  eid: string,
  pending: Pending,
): boolean {
  try {
    sessionStorage.setItem(
      receiptKey(pid, eid),
      JSON.stringify({
        version: 1,
        projectId: pid,
        episodeId: eid,
        key: pending.key,
        payload: pending.payload,
      }),
    );
    return true;
  } catch {
    return false;
  }
}
export function explicitRejection(error: any): boolean {
  return (
    error?.status >= 400 &&
    error.status < 500 &&
    ![408, 429].includes(error.status)
  );
}
function clearPending(pid: string, eid: string) {
  try {
    sessionStorage.removeItem(receiptKey(pid, eid));
  } catch {
    /* Optional session storage. */
  }
}
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
function TaskState({ state }: { state: string }) {
  return (
    <Badge
      variant={
        ["FAILED", "SUBMISSION_UNKNOWN", "INTERRUPTED", "UNKNOWN"].includes(
          state,
        )
          ? "destructive"
          : state === "SUCCEEDED"
            ? "default"
            : "secondary"
      }
    >
      {stateLabel(state)}
    </Badge>
  );
}

export function VideoPanel({ ctx, active = true }: { ctx: WorkbenchContext; active?: boolean }) {
  const pid = ctx.project?.id || "",
    eid = ctx.episode?.id || "",
    scope = `${pid}/${eid}`;
  const root = useRef<HTMLDivElement>(null),
    scopeRef = useRef(scope),
    busyRef = useRef(false),
    requestSeq = useRef(0);
  scopeRef.current = scope;
  const [view, setView] = useState("generate"),
    [busy, setBusy] = useState(false),
    [busyText, setBusyText] = useState("");
  const [draftScope, setDraftScope] = useState("");
  const [drafts, setDrafts] = useState<Draft[]>([]),
    [pending, setPending] = useState<Pending | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const [jobs, setJobs] = useState<any[]>([]),
    [production, setProduction] = useState<any>(null),
    [takes, setTakes] = useState<Record<string, any>>({});
  const [audioId, setAudioId] = useState(""),
    [shotId, setShotId] = useState("");
  const [notes, setNotes] = useState<Record<string, string>>({});
  const [confirm, setConfirm] = useState<{
    title: string;
    description: string;
    action: () => Promise<void> | void;
  } | null>(null);
  const dirty =
    drafts.length > 0 || !!pending || Object.values(notes).some(Boolean);
  const assets = ctx.assets || [],
    shots = ctx.shots || [];
  const updatePending = (value: Pending | null) => {
    pendingRef.current = value;
    setPending(value);
  };
  const run = async (text: string, action: () => Promise<void> | void) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    setBusyText(text);
    const startedScope = scope;
    try {
      await action();
    } catch (error) {
      if (scopeRef.current === startedScope)
        ctx.notify(errorText(error), "error");
    } finally {
      busyRef.current = false;
      if (scopeRef.current === startedScope) {
        setBusy(false);
        setBusyText("");
      }
    }
  };
  const refreshRead = useCallback(async () => {
    if (!pid) return;
    const token = ++requestSeq.current;
    const [state, result] = await Promise.all([
      get(`/projects/${segment(pid)}/production-state`),
      get(`/projects/${segment(pid)}/video-jobs`),
    ]);
    const nextJobs = result.jobs || [];
    const episodeIds = [
      ...new Set<string>(
        [
          eid,
          ...nextJobs
            .filter((j: any) =>
              (j.items || []).some((item: any) => item.takeId),
            )
            .map((j: any) => j.episodeId),
        ].filter(Boolean),
      ),
    ];
    const episodes = await Promise.all(
      episodeIds.map((id) => get(`/episodes/${segment(id)}`)),
    );
    if (scopeRef.current !== scope || token !== requestSeq.current) return;
    const nextTakes: Record<string, any> = {};
    for (const episode of episodes)
      for (const shot of episode.shots || [])
        for (const take of shot.takes || [])
          nextTakes[take.id] = {
            ...take,
            shotId: shot.id,
            shotTitle: shot.title,
            selectedTakeId: shot.selectedTakeId,
            currentRevisionId: shot.currentRevisionId,
          };
    setProduction(state);
    setJobs(nextJobs);
    setTakes(nextTakes);
  }, [pid, eid, scope]);
  useEffect(() => {
    ++requestSeq.current;
    let savedDrafts: Draft[] = [];
    try {
      const saved = JSON.parse(sessionStorage.getItem(`dgx.drama.video.drafts.v1:${scope}`) || "null");
      if (saved?.scope === scope && Array.isArray(saved.drafts) && saved.drafts.length <= 32 && saved.drafts.every((d: Draft) => d && typeof d.shotId === "string" && typeof d.shotRevisionId === "string" && typeof d.prompt === "string")) {
        saved.drafts.forEach((d: Draft) => generationOf(d.generation));
        savedDrafts = saved.drafts;
      }
    } catch { /* Invalid local drafts never replace server state. */ }
    setDrafts(savedDrafts);
    setDraftScope(scope);
    setNotes({});
    setJobs([]);
    setTakes({});
    setProduction(null);
    setAudioId("");
    setShotId("");
    setView("generate");
    setConfirm(null);
    setBusy(false);
    const recovered = pid && eid ? readPending(pid, eid) : null;
    updatePending(recovered);
    if (recovered)
      setDrafts(
        recovered.payload.shots.map((shot) => ({
          ...shot,
          title:
            ctx.shots?.find((item: any) => item.id === shot.shotId)?.title ||
            shot.shotId,
          pendingRecovered: true,
        })),
      );
    refreshRead().catch((error) => {
      if (scopeRef.current === scope)
        ctx.notify(`视频状态读取失败：${errorText(error)}`, "error");
    });
    let polling = false;
    const timer = window.setInterval(async () => {
      if (
        busyRef.current ||
        polling ||
        !root.current?.checkVisibility() ||
        document.hidden
      )
        return;
      polling = true;
      try {
        await refreshRead();
      } catch {
        /* Explicit refresh reports connection errors; transient poll errors retain last state. */
      } finally {
        polling = false;
      }
    }, 4000);
    return () => {
      clearInterval(timer);
      ++requestSeq.current;
    };
  }, [scope, refreshRead]);
  useEffect(() => {
    if (draftScope !== scope || !pid || !eid) return;
    try {
      sessionStorage.setItem(`dgx.drama.video.drafts.v1:${scope}`, JSON.stringify({ scope, drafts }));
    } catch { /* The visible draft and existing unload guard remain available. */ }
  }, [drafts, draftScope, scope, pid, eid]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  useEffect(() => {
    if (ctx.params?.view === "review") setView("review");
    else if (ctx.params?.shotId) setView("generate");
  }, [ctx.params?.view, ctx.params?.shotId]);
  const changeView = (value: string) => {
    root.current
      ?.querySelectorAll<HTMLMediaElement>("video,audio")
      .forEach((media) => media.pause());
    setView(value);
  };
  const readDraft = async (sid: string, previous?: Draft): Promise<Draft> => {
    const result = await get(`/shots/${segment(sid)}/production`),
      snapshot = result.current?.snapshot;
    if (!snapshot || !result.currentRevisionId)
      throw new Error("镜头还没有内容版本，请先到制作镜头保存。");
    const duration = generationOf(snapshot.generation).requestDurationSeconds;
    return {
      generation: { ...ring4Generation, requestDurationSeconds: duration },
      savedGeneration: { ...ring4Generation, requestDurationSeconds: duration },
      productionRevision: result.revision,
      snapshot,
      soundMode: previous?.soundMode || (snapshot.audioReferenceAssetId ? "reference" : "silent"),
      dialogue: previous?.dialogue || "",
      shotId: sid,
      shotRevisionId: result.currentRevisionId,
      title: snapshot.title,
      imageAssetId: snapshot.approvedReferenceAssetId,
      imageExtension: assets.find(
        (asset: any) => asset.id === snapshot.approvedReferenceAssetId,
      )?.extension,
      actionImageAssetId: snapshot.actionReferenceAssetId || "",
      actionImageExtension: assets.find((asset: any) => asset.id === snapshot.actionReferenceAssetId)?.extension,
      videoAssetId: snapshot.videoReferenceAssetId || "",
      approved: !!(
        result.current.approved && snapshot.approvedReferenceAssetId
      ),
      needsReview: (result.invalidations || []).some(
        (item: any) => !item.resolved,
      ),
      prompt: promptAfterShotReload(previous, result.currentRevisionId, videoDraftPrompt(snapshot, ctx.entities)),
      seed: previous?.seed ?? 42,
      audioAssetId: previous?.audioAssetId || snapshot.audioReferenceAssetId || audioId,
      audioDescription: previous?.audioDescription ?? snapshot.audioDescription ?? "",
    };
  };
  const loadedNavigation = useRef("");
  const activeRef = useRef(active);
  activeRef.current = active;
  useEffect(() => {
    if (!active) { loadedNavigation.current = ""; return; }
    if (draftScope !== scope) return;
    if (ctx.params?.view === "review") return;
    const sid = ctx.params?.shotId;
    const key = `${scope}/${sid || ""}/${ctx.params?.prompt || ""}`;
    if (!sid || loadedNavigation.current === key || pendingRef.current || busyRef.current) return;
    loadedNavigation.current = key;
    const previous = drafts.find((draft) => draft.shotId === sid);
    run("正在带入当前镜头…", async () => {
      const next = await readDraft(sid, previous);
      if (scopeRef.current !== scope || !activeRef.current || pendingRef.current) return;
      if (!previous && ctx.params?.prompt && !isCompleteRef2vaPrompt(next.snapshot?.prompt)) next.prompt = ctx.params.prompt;
      if (previous && generationChanged(previous)) next.generation = previous.generation;
      setDrafts((current) => current.some((draft) => draft.shotId === sid)
        ? current.map((draft) => draft.shotId === sid ? next : draft)
        : [...current, next]);
    });
  }, [active, draftScope, scope, ctx.params?.shotId, ctx.params?.prompt, ctx.params?.view, busy]);
  const draftRevisionKey = drafts.map((draft) => `${draft.shotId}:${draft.shotRevisionId}`).join("|");
  useEffect(() => {
    if (!active || draftScope !== scope || pending || !draftRevisionKey) return;
    let cancelled = false;
    (async () => {
      const replacements = [];
      for (const draft of drafts) {
        try {
          const result = await get(`/shots/${segment(draft.shotId)}/production`);
          const snapshot = result.current?.snapshot;
          if (!snapshot || result.currentRevisionId === draft.shotRevisionId) continue;
          replacements.push({
            shotId: draft.shotId,
            shotRevisionId: result.currentRevisionId,
            productionRevision: result.revision,
            snapshot,
            title: snapshot.title,
            prompt: videoDraftPrompt(snapshot, ctx.entities),
            videoAssetId: snapshot.videoReferenceAssetId || "",
          });
        } catch { /* A shot that cannot be read keeps its current draft. */ }
      }
      if (cancelled || !replacements.length) return;
      setDrafts((current) => current.map((draft) => {
        const next = replacements.find((item) => item.shotId === draft.shotId);
        if (!next || draft.shotRevisionId === next.shotRevisionId) return draft;
        return { ...draft, ...next };
      }));
    })();
    return () => { cancelled = true; };
  }, [active, draftScope, scope, pending, draftRevisionKey]);
  const saveGeneration = (draft: Draft) => run("正在保存生成设置并确认参考画面…", async () => {
    if (!draft.snapshot || (draft.imageAssetId && !draft.approved) || draft.needsReview)
      throw new Error("请先确认镜头参考画面与人物场景依赖。");
    const revised = await post(`/shots/${segment(draft.shotId)}/revisions`, {
      expectedRevision: draft.productionRevision,
      baseRevisionId: draft.shotRevisionId,
      snapshot: { ...draft.snapshot, generation: { ...ring4Generation, requestDurationSeconds: generationOf(draft.generation).requestDurationSeconds } },
    });
    try {
      if (draft.imageAssetId) await post(`/shots/${segment(draft.shotId)}/reference-selection`, {
        expectedRevision: revised.revision,
        assetId: draft.imageAssetId,
      });
    } catch (error) {
      let reloaded = false;
      if (scopeRef.current === scope) editDraft(draft.shotId, { approved: false });
      try {
        const next = await readDraft(draft.shotId, draft);
        if (scopeRef.current === scope) editDraft(draft.shotId, next);
        reloaded = true;
      } catch { /* Preserve the known successful revision write even if read-back also fails. */ }
      throw new Error(`生成设置已保存为新修订，参考画面确认回执未完成：${errorText(error)}。${reloaded ? "已重新读取实际状态" : "暂时无法读取最新状态"}，请到制作镜头核查参考画面后再提交。`);
    }
    const next = await readDraft(draft.shotId, draft);
    if (scopeRef.current !== scope) return;
    editDraft(draft.shotId, next);
    await ctx.refresh();
    ctx.notify(draft.imageAssetId ? "H3 Ring4 时长已保存，当前主参考图继续沿用。" : "H3 Ring4 时长已保存。", "success");
  });
  const addShot = () =>
    run("正在读取镜头版本…", async () => {
      if (pendingRef.current)
        throw new Error("原提交结果待核查，不能修改冻结批次。");
      if (!shotId) throw new Error("请选择一个镜头。");
      if (drafts.length >= 32) throw new Error("每批次最多 32 个镜头。");
      if (drafts.some((d) => d.shotId === shotId))
        throw new Error("这个镜头已经加入批次。");
      const next = await readDraft(shotId);
      if (scopeRef.current === scope) {
        setDrafts((current) => [...current, next]);
        setShotId("");
      }
    });
  const editDraft = (id: string, patch: Partial<Draft>) =>
    setDrafts((current) =>
      current.map((d) => (d.shotId === id ? { ...d, ...patch } : d)),
    );
  const moveDraft = (index: number, delta: number) =>
    setDrafts((current) => {
      const next = [...current];
      [next[index], next[index + delta]] = [next[index + delta], next[index]];
      return next;
    });
  const submit = () =>
    run(
      pendingRef.current ? "正在用原请求身份核查…" : "正在提交视频批次…",
      async () => {
        let frozen = pendingRef.current;
        if (!frozen) {
          frozen = { key: requestKey(), payload: buildVideoBatch(drafts) };
          frozen.persisted = savePending(pid, eid, frozen);
          if (!frozen.persisted) throw new Error("浏览器未能保存视频请求身份，尚未提交视频。请检查浏览器存储后重试。");
          updatePending(frozen);
        }
        ++requestSeq.current;
        let result;
        try {
          result = await post(
            `/episodes/${segment(eid)}/video-batches`,
            frozen.payload,
            frozen.key,
          );
        } catch (error) {
          if (explicitRejection(error)) {
            clearPending(pid, eid);
            if (scopeRef.current === scope) updatePending(null);
          }
          throw error;
        }
        if (!result.job?.id)
          throw new Error(
            "回执缺少任务编号。请先只读刷新核查；输入和原请求身份已保留。",
          );
        clearPending(pid, eid);
        if (scopeRef.current !== scope) return;
        updatePending(null);
        setDrafts([]);
        setJobs((current) => [
          result.job,
          ...current.filter((j) => j.id !== result.job.id),
        ]);
        changeView("review");
        ctx.notify("视频批次已保存，关闭工作台后任务仍会继续。", "success");
        await refreshRead();
      },
    );
  const jobAction = (job: any, action: string) =>
    run("正在处理原任务…", async () => {
      if (
        action === "continue" &&
        (!job.canContinue ||
          !(job.items || []).some(
            (item: any) => item.submissionPhase === "NOT_SUBMITTED",
          ))
      )
        throw new Error("仅服务端确认未发出的镜头可以续拍；请先核查原任务。");
      ++requestSeq.current;
      const result = await post(`/video-jobs/${segment(job.id)}/${action}`);
      if (scopeRef.current !== scope) return;
      if (!result.job?.id)
        throw new Error("回执缺少任务编号，请只读刷新核查，勿重复操作。");
      const messages: Record<string, string> = {
        refresh: "已核查原任务，未重新生成。",
        cancel: "已记录取消意图，请继续核查任务与资源状态。",
        release: "已显式请求释放本任务资源，请核查实际归还状态。",
        continue: "仅确定未发出的镜头进入新续拍任务；未知结果仍保留在原任务。",
      };
      ctx.notify(messages[action], "success");
      await refreshRead();
    });
  const review = (item: any, decision: string) =>
    run("正在保存审片意见…", async () => {
      const text = (notes[item.takeId] || "").trim();
      if (text.length > 2000)
        throw new Error("审片意见不能超过 2000 字。");
      await post(`/takes/${segment(item.takeId)}/review`, {
        decision,
        notes: text,
      });
      if (scopeRef.current !== scope) return;
      setNotes((current) => ({ ...current, [item.takeId]: "" }));
      await refreshRead();
      await ctx.refresh();
      ctx.notify(
        decision === "approved"
          ? "审片已通过。下一步可「设为本镜头选片」；不会自动加入时间线。"
          : "已记录拒绝意见。当前采用视频与已有时间线保持原样。",
        "success",
      );
    });
  const selectReviewed = async (job: any, item: any) => {
      const [episode, prod] = await Promise.all([
        get(`/episodes/${segment(job.episodeId)}`),
        get(`/shots/${segment(item.shotId)}/production`),
      ]);
      if (scopeRef.current !== scope) return;
      const shot = (episode.shots || []).find((s: any) => s.id === item.shotId),
        take = shot?.takes?.find((t: any) => t.id === item.takeId);
      if (!take || take.reviewStatus !== "approved")
        throw new Error("候选尚未通过人工审片，请先批准并刷新核查。");
      const stale = !!(
        take.stale || item.shotRevisionId !== prod.currentRevisionId
      );
      const commit = async () => {
        await post(`/shots/${segment(item.shotId)}/selection`, {
          takeId: item.takeId,
          expectedRevision: prod.revision,
          ...(stale ? { acceptStale: true } : {}),
        });
        if (scopeRef.current !== scope) return;
        await refreshRead();
        await ctx.refresh();
        ctx.notify(
          "已设为本镜头选片。下一步到「剪辑与导出」添加或替换时间线片段。",
          "success",
        );
      };
      if (stale)
        setConfirm({
          title: "采用旧修订的视频？",
          description:
            "这段视频来自镜头旧版本。采用后会替换当前选片；已经保存的时间线保持原样。",
          action: commit,
        });
      else await commit();
    };
  const adopt = (job: any, item: any) => run("正在核对候选版本…", () => selectReviewed(job, item));
  const reviewAndSelect = (job: any, item: any) => run("正在通过审片并设置本镜头选片…", async () => {
    const text = (notes[item.takeId] || "").trim();
    if (text.length > 2000) throw new Error("审片意见不能超过 2000 字。");
    await post(`/takes/${segment(item.takeId)}/review`, { decision: "approved", notes: text });
    if (scopeRef.current !== scope) return;
    setNotes((current) => ({ ...current, [item.takeId]: "" }));
    try {
      await selectReviewed(job, item);
    } catch (error) {
      await refreshRead().catch(() => {});
      throw new Error(`审片已通过，但本镜头选片尚未确认成功：${errorText(error)}。请刷新核查后使用「设为本镜头选片」，无需重复审片。`);
    }
  });
  if (!pid)
    return (
      <Card>
        <CardContent className="p-10 text-center text-muted-foreground">
          先选择一个短剧项目，再制作视频。
        </CardContent>
      </Card>
    );
  return (
    <div
      ref={root}
      aria-busy={busy}
      data-panel="video"
      data-dirty={dirty}
      className="space-y-6"
    >
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="text-xs font-medium uppercase tracking-[.2em] text-primary">
            GENERATE & REVIEW
          </p>
          <h2 className="mt-2 text-2xl font-semibold tracking-tight">
            让镜头动起来
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            准备好画面与音频，生成后逐条审片，再选入成片。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() =>
            run("正在刷新任务…", async () => {
              await refreshRead();
              await ctx.refresh();
              ctx.notify("任务状态已更新，编辑草稿保留。", "success");
            })
          }
        >
          <RefreshCw className="mr-2 size-4" />
          刷新状态
        </Button>
      </div>
      {busy && (
        <div
          role="status"
          aria-live="polite"
          className="flex items-center gap-2 rounded-xl border border-primary/20 bg-primary/5 p-3 text-sm"
        >
          <Loader2 className="size-4 animate-spin" />
          {busyText}
        </div>
      )}
      <Tabs value={view} onValueChange={changeView}>
        <TabsList>
          <TabsTrigger value="generate">
            <Film className="mr-2 size-4" />
            准备生成{drafts.length > 0 ? ` · ${drafts.length}` : ""}
          </TabsTrigger>
          <TabsTrigger value="review">
            <ShieldCheck className="mr-2 size-4" />
            镜头审片{ctx.shots?.length > 0 ? ` · ${ctx.shots.length}` : ""}
          </TabsTrigger>
        </TabsList>
        <TabsContent
          forceMount
          value="generate"
          className="mt-5 space-y-5 data-[state=inactive]:hidden"
        >
          {pending && (
            <Card className="border-amber-500/40 bg-amber-500/5">
              <CardHeader>
                <CardTitle className="text-base">原提交结果仍需确认</CardTitle>
                <CardDescription>
                  输入已经冻结。请使用下方「按原请求键确认提交结果」，页面不会自动重新发送。
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <p className="break-all font-mono text-xs">{pending.key}</p>
                <p>
                  {pending.persisted
                    ? "本标签页已保存核查身份，刷新后可恢复。"
                    : "浏览器暂存不可用，请先核查任务再关闭页面。"}
                </p>
              </CardContent>
            </Card>
          )}
          <Card>
            <CardHeader>
              <CardTitle className="text-base">生成队列</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {eid ? (
                <div className="flex flex-col gap-2 sm:flex-row">
                  <div className="min-w-0 flex-1">
                    <Choice
                      value={shotId}
                      onChange={setShotId}
                      options={shots.map((s: any) => ({
                        id: s.id,
                        title: `${s.title}${s.needsReview ? " · 需复核" : ""}`,
                      }))}
                      label="选择本集镜头"
                      disabled={busy || !!pending}
                    />
                  </div>
                  <Button
                    disabled={
                      busy || !!pending || !shotId || drafts.length >= 32
                    }
                    onClick={addShot}
                  >
                    <Plus className="mr-2 size-4" />
                    加入批次
                  </Button>
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">请先选择分集。</p>
              )}
              {!drafts.length && (
                <div className="rounded-xl border border-dashed py-10 text-center">
                  <Film className="mx-auto mb-3 size-8 text-muted-foreground/50" />
                  <p className="text-sm font-medium">从一个镜头开始</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    先在上方选镜头，加入后再核对提示词。
                  </p>
                </div>
              )}
              {drafts.map((draft, index) => {
                const sent = h3Outbound(draft);
                const primary = sent.pictures[0] || sent.videos[0];
                return (
                <Card key={draft.shotId} className="overflow-hidden">
                  <div className="flex items-center gap-3 px-4 py-3">
                    {primary ? (
                      primary.label === "Video" ? (
                        <video className="h-14 w-24 shrink-0 rounded-md bg-black object-cover" muted preload="metadata" src={mediaUrl(pid, primary.assetId)} aria-label={`${draft.title || "镜头"} 主参考`} />
                      ) : (
                        <img className="h-14 w-24 shrink-0 rounded-md bg-black object-cover" src={mediaUrl(pid, primary.assetId)} alt={`${draft.title || "镜头"} 主参考`} />
                      )
                    ) : (
                      <span className="flex h-14 w-24 shrink-0 items-center justify-center rounded-md bg-muted text-xs text-muted-foreground">无参考</span>
                    )}
                    <div className="min-w-0 flex-1">
                      <h3 className="truncate text-sm font-semibold">{index + 1}. {draft.title || "未命名镜头"}</h3>
                    </div>
                    {(draft.pendingRecovered || draft.needsReview || (draft.imageAssetId && !draft.approved)) && (
                      <Badge variant="destructive">
                        {draft.pendingRecovered ? "待核查" : draft.needsReview ? "需复核" : "参考图待确认"}
                      </Badge>
                    )}
                    <div className="flex shrink-0 gap-1">
                      <Button size="sm" variant="ghost" aria-label={`上移镜头 ${index + 1}`} disabled={busy || !!pending || index === 0} onClick={() => moveDraft(index, -1)}><ArrowUp className="size-4" /></Button>
                      <Button size="sm" variant="ghost" aria-label={`下移镜头 ${index + 1}`} disabled={busy || !!pending || index === drafts.length - 1} onClick={() => moveDraft(index, 1)}><ArrowDown className="size-4" /></Button>
                      <Button size="sm" variant="ghost" disabled={busy || !!pending} onClick={() => setDrafts((current) => current.filter((d) => d.shotId !== draft.shotId))}><Trash2 className="mr-1 size-3" />移出</Button>
                    </div>
                  </div>
                  <details className="border-t px-4 py-3">
                    <summary className="cursor-pointer text-sm font-medium">查看将发给 H3 的内容</summary>
                    <div className="mt-4 space-y-4">
                      <div>
                        <p className="text-xs font-medium text-muted-foreground">提示词</p>
                        <pre className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-md border bg-muted/40 p-3 text-xs">{sent.prompt}</pre>
                        {sent.unsaved && <p className="mt-1 text-xs text-amber-600">页面上的提示词还没存回镜头，发出去的是镜头里已保存的稿。</p>}
                      </div>
                      {sent.pictures.length > 0 && (
                        <div className="grid gap-3 sm:grid-cols-2">
                          {sent.pictures.map((item) => (
                            <figure key={`p-${item.index}`} className="space-y-1">
                              <img className="aspect-video w-full rounded-lg bg-black object-contain" src={mediaUrl(pid, item.assetId)} alt={`${draft.title || "镜头"} Picture ${item.index}`} />
                              <figcaption className="text-xs text-muted-foreground">Picture {item.index}{item.description ? ` · ${item.description}` : ""}</figcaption>
                            </figure>
                          ))}
                        </div>
                      )}
                      {sent.videos.length > 0 && (
                        <div className="grid gap-3 sm:grid-cols-2">
                          {sent.videos.map((item) => (
                            <figure key={`v-${item.index}`} className="space-y-1">
                              <video className="aspect-video w-full rounded-lg bg-black object-contain" controls preload="metadata" src={mediaUrl(pid, item.assetId)} aria-label={`${draft.title || "镜头"} Video ${item.index}`} />
                              <figcaption className="text-xs text-muted-foreground">Video {item.index}{item.description ? ` · ${item.description}` : ""}</figcaption>
                            </figure>
                          ))}
                        </div>
                      )}
                      {!sent.pictures.length && !sent.videos.length && <p className="text-xs text-muted-foreground">没有图片或视频会发给 H3。</p>}
                      <div className="grid gap-3 sm:grid-cols-3">
                        <div className="space-y-1">
                          <Label htmlFor={`generation-duration-${draft.shotId}`}>时长（秒）</Label>
                          <Input id={`generation-duration-${draft.shotId}`} aria-label="生成视频时长" type="number" inputMode="numeric" min={4} max={15} step={1} value={generationOf(draft.generation).requestDurationSeconds} disabled={busy || !!pending} onChange={(event) => {
                            const seconds = Number(event.target.value);
                            if (Number.isInteger(seconds) && seconds >= 4 && seconds <= 15) editDraft(draft.shotId, { generation: { ...generationOf(draft.generation), requestDurationSeconds: seconds } });
                          }} />
                        </div>
                        <div className="space-y-1">
                          <Label>候选</Label>
                          <Select value={String(draft.candidateCount ?? 1)} disabled={busy || !!pending} onValueChange={v => editDraft(draft.shotId, { candidateCount: Number(v) })}>
                            <SelectTrigger aria-label={`镜头 ${index + 1} 视频候选数量`}><SelectValue /></SelectTrigger>
                            <SelectContent>{[1,2,3,4].map(n => <SelectItem key={n} value={String(n)}>{n}</SelectItem>)}</SelectContent>
                          </Select>
                        </div>
                        <div className="space-y-1">
                          <Label htmlFor={`seed-${draft.shotId}`}>种子</Label>
                          <Input id={`seed-${draft.shotId}`} aria-label="种子（0–2147483647）" type="number" min={0} max={2147483647} step={1} value={draft.seed} disabled={busy || !!pending} onChange={(event) => editDraft(draft.shotId, { seed: event.target.value })} />
                        </div>
                      </div>
                      {generationChanged(draft) && <Button size="sm" variant="secondary" disabled={busy || !!pending || (!!draft.imageAssetId && !draft.approved) || draft.needsReview} onClick={() => saveGeneration(draft)}>保存时长</Button>}
                      <Button variant="outline" size="sm" disabled={busy || !!pending} onClick={() => setConfirm({
                        title: "读取镜头的最新版本？",
                        description: "参考和时长会换成镜头当前版本。已改的种子会保留。",
                        action: async () => {
                          const next = await readDraft(draft.shotId, draft);
                          if (scopeRef.current === scope) editDraft(draft.shotId, next);
                        },
                      })}>
                        <RefreshCw className="mr-2 size-3" />读取镜头最新版本
                      </Button>
                    </div>
                  </details>
                </Card>
                );
              })}
              <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-4">
                <p className="text-sm text-muted-foreground">
                  {drafts.length} 个镜头 · {drafts.reduce((n, d) => n + (d.candidateCount ?? 1), 0)} 个视频候选 · 合计请求{" "}
                  {drafts.reduce(
                    (sum, d) =>
                      sum + generationOf(d.generation).requestDurationSeconds * (d.candidateCount ?? 1),
                    0,
                  )}{" "}
                  秒{drafts.length ? " · 尚未确认提交" : ""}
                </p>
                <div className="flex gap-2">
                  <Button
                    variant="ghost"
                    disabled={busy || !!pending || !drafts.length}
                    onClick={() =>
                      setConfirm({
                        title: "清空批次草稿？",
                        description:
                          "只清空尚未提交的镜头；已提交任务继续运行。",
                        action: () => setDrafts([]),
                      })
                    }
                  >
                    清空草稿
                  </Button>
                  <Button
                    disabled={
                      busy ||
                      !eid ||
                      (!pending &&
                        (!drafts.length ||
                          !production ||
                          production.paused ||
                          ctx.capabilities?.videoBatchEnabled === false))
                    }
                    onClick={submit}
                  >
                    {busy ? (
                      <Loader2 className="mr-2 size-4 animate-spin" />
                    ) : (
                      <Play className="mr-2 size-4" />
                    )}
                    {pending ? "按原请求键确认提交结果" : "提交当前批次"}
                  </Button>
                </div>
              </div>
              {ctx.capabilities?.videoBatchEnabled === false && (
                <p className="text-sm text-amber-500">
                  视频批次服务尚未启用。可以查看已有任务和审片。
                </p>
              )}
            </CardContent>
          </Card>
        </TabsContent>
        <TabsContent
          forceMount
          value="review"
          className="mt-5 space-y-4 data-[state=inactive]:hidden"
        >
          {active && view === "review" && <ReviewWorkspace key={scope} shots={shots} jobs={jobs} takes={takes} episodeId={eid} projectId={pid} target={ctx.params} busy={busy}
            onRedo={(entry, reason) => run("正在准备新的候选…", async () => {
              if (pendingRef.current) throw new Error("原提交仍需核查，请先处理原任务。");
              const next = await readDraft(entry.item.shotId);
              const old = entry.item;
              next.prompt = `${old.prompt || next.prompt}\n本次重点改进：${reason}`;
              next.promptJobId = undefined;
              next.seed = (Number(old.seed ?? next.seed) + 1) % 2147483648;
              if (scopeRef.current !== scope) return;
              setDrafts(current => [...current.filter(d=>d.shotId!==next.shotId),next]); setView("generate");
              ctx.notify("已准备新候选草稿，请核对提示词后提交。原视频和选片保持保留。", "success");
            })}
            onPrepare={(shotId) => ctx.navigate("video", { shotId })} onEdit={() => ctx.navigate("edit")}
            renderCandidate={({ job, item, take, assetId }, title) => <ReviewCandidate
              key={`${item.takeId}/${assetId}`} pid={pid} assetId={assetId} title={title} item={item}
              notes={notes[item.takeId] || ""} setNotes={(value) => setNotes(current => ({ ...current, [item.takeId]: value }))}
              status={take.reviewStatus || item.reviewStatus || "pending"} adopted={take.selectedTakeId === item.takeId}
              stale={!!take.stale} busy={busy} review={decision => review(item, decision)}
              reviewAndSelect={() => reviewAndSelect(job, item)} adopt={() => adopt(job, item)} />}
          />}
          {!jobs.length && (
            <Card>
              <CardContent className="py-14 text-center">
                <Film className="mx-auto mb-4 size-10 text-muted-foreground/40" />
                <p className="font-medium">还没有视频批次</p>
                <p className="mt-2 text-sm text-muted-foreground">
                  完成准备生成后，这里会显示任务进度和可审看的视频。
                </p>
                <Button
                  className="mt-4"
                  variant="outline"
                  onClick={() => changeView("generate")}
                >
                  准备第一个镜头
                </Button>
              </CardContent>
            </Card>
          )}
          <details className="rounded-xl border p-4" open={!jobs.some(job => (job.items || []).some((item: any) => item.takeId))}>
            <summary className="cursor-pointer text-sm text-muted-foreground">生成批次与恢复工具 · {jobs.length} 个记录</summary>
            <div className="mt-4 space-y-3">
          {jobs.map((job) => (
            <Card key={job.id}>
              <CardHeader>
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <CardTitle className="text-base">
                    {job.items?.length || 0} 个镜头 ·{" "}
                    {job.createdAt
                      ? new Date(job.createdAt * 1000).toLocaleString("zh-CN", {
                          month: "numeric",
                          day: "numeric",
                          hour: "2-digit",
                          minute: "2-digit",
                        })
                      : "视频批次"}
                  </CardTitle>
                  <TaskState state={job.state} />
                </div>
                <CardDescription>
                  {job.episodeId === eid ? "当前分集" : "其他分集"} ·{" "}
                  {isResident(job)
                    ? RESIDENT_MESSAGE
                    : job.memoryReleased === true
                    ? "资源已归还或未占用"
                    : job.state === "CANCELED" &&
                        !job.gatewayJobId &&
                        !job.cleanupRequired &&
                        job.items?.length > 0 &&
                        job.items.every((item: any) => item.submissionPhase === "NOT_SUBMITTED")
                      ? "排队时取消 · 未占用模型资源"
                      : "尚未确认资源归还"}
                  {job.cleanupRequired ? " · 需要清理资源" : ""}
                  {job.cancelRequested ? " · 已记录取消意图" : ""}
                </CardDescription>
                {errorText(job.error) && (
                  <p role="alert" className="text-sm text-destructive">
                    {errorText(job.error)}
                  </p>
                )}
              </CardHeader>
              <CardContent className="space-y-4">
                {(job.items || []).map((item: any) => {
                  const take = takes[item.takeId],
                    assetId = item.assetId || take?.assetId,
                    adopted = take?.selectedTakeId === item.takeId,
                    status =
                      take?.reviewStatus || item.reviewStatus || "pending";
                  const title =
                    take?.shotTitle ||
                    shots.find((s: any) => s.id === item.shotId)?.title ||
                    item.shotId;
                  return (
                    <article
                      key={item.id}
                      className="space-y-3 rounded-xl border p-4"
                    >
                      <div className="flex flex-wrap justify-between gap-2">
                        <h3 className="text-sm font-semibold">
                          {shots.some((s: any) => s.id === item.shotId) ? `S${String(shots.findIndex((s: any) => s.id === item.shotId) + 1).padStart(2, "0")} · ` : ""}{title}
                        </h3>
                        <TaskState
                          state={
                            job.state === "CANCELED" &&
                            item.state === "QUEUED" &&
                            item.submissionPhase === "NOT_SUBMITTED"
                              ? "CANCELED"
                              : item.state
                          }
                        />
                      </div>
                      <p className="text-xs text-muted-foreground">
                        {generationName(item.generation)} · 请求{" "}
                        {generationOf(item.generation).requestDurationSeconds}{" "}
                        秒 · 提交阶段：{stateLabel(item.submissionPhase)}
                        {item.submissionPhase === "NOT_SUBMITTED"
                          ? " · 仅此阶段允许续拍"
                          : ""}
                      </p>
                      {errorText(item.error) && (
                        <p role="alert" className="text-sm text-destructive">
                          {errorText(item.error)}
                        </p>
                      )}
                      {item.takeId && assetId ? <Button size="sm" variant="outline" onClick={() => ctx.navigate("video", { view: "review", shotId: item.shotId, takeId: item.takeId, jobId: job.id })}>定位此视频审片</Button> : null}
                      <details>
                        <summary className="cursor-pointer text-xs text-muted-foreground">
                          生成参数与版本
                        </summary>
                        <p className="mt-2 break-all text-xs text-muted-foreground">
                          固定修订 {item.shotRevisionId} · 种子 {item.seed} ·
                          参考音频 {item.audioAssetId}
                        </p>
                      </details>
                    </article>
                  );
                })}
                <details className="border-t pt-3">
                  <summary className="cursor-pointer text-xs text-muted-foreground">
                    任务记录与恢复工具
                  </summary>
                  <div className="mt-3 space-y-3">
                    <p className="break-all text-xs text-muted-foreground">
                      批次 {job.id}
                      <br />原 Gateway 任务 {job.gatewayJobId || "尚无回执"}
                      <br />
                      派发键 {job.dispatchKey || "待确认"}
                    </p>
                    <div className="flex flex-wrap gap-2">
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={busy}
                        onClick={() => jobAction(job, "refresh")}
                      >
                        核查原任务
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={
                          busy || terminal.has(job.state) || job.cancelRequested
                        }
                        onClick={() => jobAction(job, "cancel")}
                      >
                        取消原批次
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={
                          busy ||
                          job.memoryReleased === true ||
                          !job.cleanupRequired ||
                          !job.gatewayJobId
                        }
                        onClick={() => jobAction(job, "release")}
                      >
                        显式释放本任务资源
                      </Button>
                      <Button
                        variant="outline"
                        size="sm"
                        disabled={
                          busy ||
                          production?.paused ||
                          !job.canContinue ||
                          !(job.items || []).some(
                            (item: any) =>
                              item.submissionPhase === "NOT_SUBMITTED",
                          )
                        }
                        onClick={() => jobAction(job, "continue")}
                      >
                        只续拍确定未发出的镜头
                      </Button>
                    </div>
                  </div>
                </details>
              </CardContent>
            </Card>
          ))}
            </div>
          </details>
        </TabsContent>
      </Tabs>
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

function ReviewCandidate({
  pid,
  assetId,
  title,
  item,
  notes,
  setNotes,
  status,
  adopted,
  stale,
  busy,
  review,
  reviewAndSelect,
  adopt,
}: {
  pid: string;
  assetId: string;
  title: string;
  item: any;
  notes: string;
  setNotes: (value: string) => void;
  status: string;
  adopted: boolean;
  stale: boolean;
  busy: boolean;
  review: (decision: string) => void;
  reviewAndSelect: () => void;
  adopt: () => void;
}) {
  const video = useRef<HTMLVideoElement>(null),
    [mediaError, setMediaError] = useState(false),
    [playbackVersion, setPlaybackVersion] = useState(0),
    [actualDuration, setActualDuration] = useState<number | null>(null);
  useEffect(() => {
    const element = video.current;
    return () => { if (element?.hasAttribute("src")) { element.pause(); element.removeAttribute("src"); element.load(); } };
  }, [assetId, playbackVersion]);
  const reloadPlayback = () => {
    const previous = video.current;
    if (previous) {
      previous.pause();
      previous.removeAttribute("src");
      previous.load();
    }
    setMediaError(false);
    setActualDuration(null);
    setPlaybackVersion((version) => version + 1);
  };
  return (
    <div className="grid gap-4 2xl:grid-cols-[minmax(0,1.4fr)_minmax(240px,1fr)]">
      <div className="space-y-2">
        <video
          key={`${assetId}:${playbackVersion}`}
          ref={video}
          className="aspect-video w-full rounded-lg bg-black"
          controls
          preload="none"
          src={mediaUrl(pid, assetId)}
          aria-label={`${title} 视频候选`}
          onLoadedMetadata={(event) => {
            const duration = event.currentTarget.duration;
            if (Number.isFinite(duration)) setActualDuration(duration);
          }}
          onError={() => setMediaError(true)}
        />
        {actualDuration !== null && (
          <p className="text-xs text-muted-foreground">
            实际视频 {actualDuration.toFixed(2)} 秒 · 请求{" "}
            {generationOf(item.generation).requestDurationSeconds}{" "}
            秒；剪辑长度另行设置。
          </p>
        )}
        {mediaError && (
          <p role="alert" className="text-xs text-destructive">
            视频无法播放，请核查素材是否存在或下载检查。
          </p>
        )}
        <div className="flex flex-wrap items-center gap-2">
          <Button type="button" variant="outline" size="sm" onClick={reloadPlayback}>
            重新加载视频
          </Button>
          <span className="text-xs text-muted-foreground">黑屏或卡住时使用，不会重新生成。</span>
        </div>
        {playbackVersion > 0 && (
          <p role="status" className="text-xs text-muted-foreground">
            播放器已重置，请点击播放。审片意见已保留。
          </p>
        )}
        <a
          className="text-xs text-primary hover:underline"
          href={mediaUrl(pid, assetId)}
          target="_blank"
          rel="noopener noreferrer"
        >
          打开 / 保存候选视频 ↗
        </a>
      </div>
      <div className="space-y-3">
        <div className="flex flex-wrap gap-2">
          <Badge variant={status === "rejected" ? "destructive" : "secondary"}>
            {status === "approved"
              ? "审片已通过"
              : status === "rejected"
                ? "人工已拒绝"
                : "待人工审片"}
          </Badge>
          {adopted && <Badge>本镜头已选片</Badge>}
          {stale && <Badge variant="outline">旧修订</Badge>}
        </div>
        <Label htmlFor={`review-${item.takeId}`}>审片意见 <span aria-hidden="true">（选填）</span></Label>
        <Textarea
          id={`review-${item.takeId}`}
          aria-label="审片意见"
          rows={4}
          value={notes}
          maxLength={2000}
          placeholder="例如：00:02.100 人物外观稳定，动作连贯，可采用。通过和拒绝都可留空。"
          onChange={(event) => setNotes(event.target.value)}
          disabled={busy}
        />
        <div className="flex items-center justify-between">
          <Button
            variant="ghost"
            size="sm"
            disabled={busy}
            onClick={() => {
              const time = video.current?.currentTime || 0;
              const stamp = `${String(Math.floor(time / 60)).padStart(2, "0")}:${(time % 60).toFixed(3).padStart(6, "0")}`;
              setNotes(
                `${notes}${notes ? "\n" : ""}[${stamp}] `.slice(0, 2000),
              );
            }}
          >
            <Clock3 className="mr-1 size-3" />
            记录当前时间点
          </Button>
          <span className="text-xs text-muted-foreground">
            {notes.length}/2000
          </span>
        </div>
        <div className="flex flex-wrap gap-2">
          {!adopted && status !== "approved" && <Button size="sm" disabled={busy} onClick={reviewAndSelect}>
            <Check className="mr-1 size-3" />通过并设为本镜头选片
          </Button>}
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => review("approved")}
          >
            <Check className="mr-1 size-3" />
            通过审片
          </Button>
          <Button
            size="sm"
            variant="outline"
            disabled={busy}
            onClick={() => review("rejected")}
          >
            <X className="mr-1 size-3" />
            审片拒绝
          </Button>
          <Button
            size="sm"
            disabled={busy || status !== "approved" || adopted}
            onClick={adopt}
          >
            {adopted ? "本镜头已选片" : "设为本镜头选片"}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground">
          通过审片表示质量合格；设为本镜头选片表示选定此候选。选片后还需到剪辑页添加到时间线。拒绝意见与历史记录会保留。
        </p>
      </div>
    </div>
  );
}
