import { AssetPicker } from "./AssetPicker";
import { ImageSizePicker } from './ImageSizePicker';
import { fitImageSize, imageDimensions } from '@/lib/image-size';
import { sessionStore as sessionStorage } from "@/lib/workspace";
import { useCallback, useEffect, useId, useRef, useState } from "react";
import { videoDraftPrompt, planningImagePrompt, SHOT_IMAGE_SLOTS, shotImageSlot, shotReferenceInputs, imageBatchOf, pendingImageBatchMessage, recoverableImageJob, completedImageRetries, knownImageRetries, remainingImageRetryIndices, type ReferenceInput } from "@/lib/production-draft";
import { H3ReferenceNodes } from "@/h3-references/H3ReferenceNodes";
import { nodesFromSnapshot, type CustomRole, type MediaKind, type ReferenceNode } from "@/lib/reference-nodes";
import { generationOf, ring4Generation } from "@/lib/video-contract";
import type { WorkbenchContext } from "@/lib/types";
import { isResident, RESIDENT_MESSAGE } from "@/lib/resource-status";
import { get, post, requestKey, mediaUrl, segment, upload, uploadImage, uploadReferenceAudio } from "@/lib/api";
import { inspectImageSelection, selectImageCandidate } from "@/lib/image-selection";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
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
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import { Checkbox } from "@/components/ui/checkbox";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Plus,
  ImagePlus,
  Check,
  ArrowRight,
  LoaderCircle,
  RefreshCw,
  Upload,
  Layers,
  Sparkles,
  Clapperboard,
  Trash2,
  Image as ImageIcon,
} from "lucide-react";

type Any = Record<string, any>;
const KINDS: Record<string, string> = {
  character: "人物",
  location: "场景",
  costume: "服装",
  prop: "道具",
};
const ROLES: Record<string, [string, string][]> = {
  character: [
    ["identity_front", "正面身份图"],
    ["identity_3q", "四分之三侧面"],
    ["identity_back", "背视图"],
    ["costume_fullbody", "服装全身图"],
    ["identity_sheet", "全位图"],
  ],
  location: [
    ["location_wide", "场景全景"],
    ["location_detail", "场景细节"],
    ["layout", "空间布局"],
  ],
  costume: [["costume_fullbody", "服装全身图"]],
  prop: [["prop_detail", "道具细节"]],
};
const STATES: Record<string, string> = {
  PREPARED: "已保存，等待执行",
  DISPATCHING: "正在发送任务",
  UPLOADING: "正在上传参考素材",
  SUBMITTING: "正在确认提交",
  RELEASING: "正在处理模型资源",
  RECONCILING: "正在核查任务状态",
  CANCELING: "正在取消",
  CANCEL_REQUESTED: "已请求取消 · 等待确认",
  QUEUED: "等待执行",
  SUBMITTED: "已提交",
  LOADING: "正在加载模型",
  RUNNING: "正在制作",
  COLLECTING: "归档素材",
  VALIDATING: "校验图片",
  SUCCEEDED: "制作完成",
  FAILED: "制作失败",
  CANCELED: "已取消",
  INTERRUPTED: "已中断",
  SUBMISSION_UNKNOWN: "结果待确认 · 请核查原任务",
};
const TOOLS: [string, string][] = [
  ["generate_image", "从文字生图 · 选择模型"],
  ["edit_image", "参考图编辑 · 选择模型"],
  ["compose_image_layers", "透明图层合成"],
];
const MODEL_NAMES: Record<string,string> = {"z-image-turbo":"Z-Image Turbo", "qwen-image-edit-2511":"Qwen Image Edit 2511", "llada-image-turbo":"LLaDA Image Turbo · INT8 / Q4_K_M · 4步", "flux2-dev":"FLUX.2 dev · 标准编码器", "flux2-dev-abliterated":"FLUX.2 dev · silveroxides 编码器"};
export function imageModelName(tool: string, parameters: Any = {}) { return tool === 'compose_image_layers' ? '透明图层合成 · 无生成模型' : MODEL_NAMES[parameters.model || (tool === 'edit_image' ? 'qwen-image-edit-2511' : 'z-image-turbo')] || parameters.model || '历史模型未记录'; }
const currentOf = (e: Any) =>
  e?.current ||
  e?.revisions?.find((r: Any) => r.id === e.currentRevisionId) ||
  e?.revisions?.at(-1);
export function replaceSelectedCandidate(
  current: Record<string, { targetId: string; slot: string; assetId: string | null }>,
  candidateId: string,
  targetId: string,
  slot: string,
  assetId: string | null,
) {
  return {
    ...Object.fromEntries(Object.entries(current).filter(([, value]) => value.targetId !== targetId || value.slot !== slot)),
    [candidateId]: { targetId, slot, assetId },
  };
}
const nameOf = (e: Any) => e?.name || e?.title || e?.filename || "未命名";
const path = (kind: string, id: string) => `/${kind}/${segment(id)}`;
const integer = (
  value: any,
  title: string,
  min = 1,
  max = Number.MAX_SAFE_INTEGER,
) => {
  const n = Number(value);
  if (
    String(value).trim() === "" ||
    !Number.isSafeInteger(n) ||
    n < min ||
    n > max
  )
    throw new Error(`${title}须为 ${min}–${max} 的整数。`);
  return n;
};
export function compositionParameters(background: string, layers: Any[]) {
  if (!background || !layers.length || layers.length > 16)
    throw new Error("请选择背景并添加 1–16 个图层。");
  return {
    background_asset_id: background,
    layers: layers.map((l) => {
      if (!l.asset_id || !l.name?.trim() || l.name.trim().length > 80)
        throw new Error("每个图层需要图片和 1–80 字名称。");
      return {
        name: l.name.trim(),
        asset_id: l.asset_id,
        x: integer(l.x, "水平偏移", -4096, 4096),
        y: integer(l.y, "垂直偏移", -4096, 4096),
        width: integer(l.width, "宽度", 1, 4096),
        height: integer(l.height, "高度", 1, 4096),
        opacity: integer(l.opacity, "不透明度", 1, 100),
        flip_x: l.flip_x === true,
      };
    }),
  };
}
function positiveCount(value: unknown, label: string): number {
  const n = Number(value);
  if (typeof value === 'boolean' || value == null || String(value).trim() === '' || !Number.isSafeInteger(n) || n < 1) throw new Error(`${label}须为正整数。`);
  return n;
}
export function imageParameters(tool: string, form: Any) {
  if (tool === "compose_image_layers")
    return compositionParameters(form.background, form.layers);
  if (!form.prompt?.trim()) throw new Error("请填写画面要求。");
  const seed = integer(form.seed, "种子", 0);
  const count = positiveCount(form.count ?? 1, "候选数量");
  const parallelism = Math.min(count, positiveCount(form.parallelism ?? 2, "每批并行数量"));
  if (tool === "generate_image") {
    const model = form.model || "z-image-turbo";
    const { width, height } = imageDimensions(form.width, form.height, model);
    const refs = (form.refs || []).filter(Boolean);
    if (refs.length && model !== "flux2-dev")
      throw new Error("多参考图生图只能用 FLUX.2 dev。");
    if (refs.length > 8) throw new Error("FLUX.2 dev 最多 8 张参考图。");
    return { prompt: form.prompt, width, height, seed, batch_count: count, parallelism,
      ...(model === "z-image-turbo" ? {} : { model }),
      ...(refs.length ? { asset_ids: refs } : {}) };
  }
  if (!form.refs?.[0]) throw new Error("请选择输入图 1。");
  if (form.editModel === "llada-image-turbo") {
    if (form.refs.filter(Boolean).length !== 1)
      throw new Error("LLaDA 只接收一张完整参考画面；多张人物和场景参考请使用 Qwen。");
    const { width, height } = imageDimensions(form.width, form.height, "llada-image-turbo");
    return { model: "llada-image-turbo", prompt: form.prompt, asset_ids: [form.refs[0]],
      width, height, seed, steps: 4, batch_count: count, parallelism };
  }
  if (form.editModel === "flux2-dev") {
    const ids = form.refs.filter(Boolean);
    if (ids.length > 8) throw new Error("FLUX.2 dev 最多 8 张参考图。");
    return { model: "flux2-dev", prompt: form.prompt, asset_ids: ids, seed, batch_count: count, parallelism };
  }
  return {
    prompt: form.prompt,
    asset_ids: form.refs.filter(Boolean),
    seed,
    steps: integer(form.steps, "采样步数", 20, 40),
    batch_count: count,
    parallelism,
    encoder: form.encoder,
    model_precision: form.precision,
  };
}
function Choice({
  label,
  value,
  options,
  onChange,
  disabled = false,
}: {
  label: string;
  value: string;
  options: [string, string][];
  onChange: (v: string) => void;
  disabled?: boolean;
}) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      <Select
        value={value || "__none"}
        onValueChange={(v) => onChange(v === "__none" ? "" : v)}
        disabled={disabled}
      >
        <SelectTrigger aria-label={label} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {options.map(([v, n]) => (
            <SelectItem key={v || "__none"} value={v || "__none"}>
              {n}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
function Field({
  label,
  value,
  onChange,
  multiline = false,
  type = "text",
  disabled = false,
}: {
  label: string;
  value: any;
  onChange: (v: string) => void;
  multiline?: boolean;
  type?: string;
  disabled?: boolean;
}) {
  const id = useId();
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      {multiline ? (
        <Textarea
          id={id}
          disabled={disabled}
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
          className="min-h-28 resize-y"
        />
      ) : (
        <Input
          id={id}
          disabled={disabled}
          type={type}
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}
function Picture({
  ctx,
  id,
  className = "",
  alt = "参考图片",
}: {
  ctx: WorkbenchContext;
  id: string;
  className?: string;
  alt?: string;
}) {
  return id ? (
    <img
      className={`rounded-lg object-contain bg-black/15 ${className}`}
      src={mediaUrl(ctx.project.id, id)}
      alt={alt}
      loading="lazy"
    />
  ) : (
    <div
      className={`grid place-items-center rounded-lg bg-muted text-muted-foreground ${className}`}
    >
      <ImageIcon className="size-8 opacity-40" />
    </div>
  );
}

function ReferencePreview({
  ctx,
  id,
  caption,
}: {
  ctx: WorkbenchContext;
  id?: string;
  caption: string;
}) {
  if (!id) return null;
  return (
    <figure className="overflow-hidden rounded-lg border bg-black/40">
      <img
        src={mediaUrl(ctx.project.id, id)}
        alt={caption}
        className="mx-auto block h-72 w-full object-contain"
      />
      <figcaption className="border-t px-3 py-2 text-xs text-muted-foreground">{caption}</figcaption>
    </figure>
  );
}
const blankEntity = () => ({
  name: "",
  description: "",
  references: [],
  referenceRole: "",
  referenceAssetId: null,
});
const blankLayer = (n = 1) => ({
  name: `图层 ${n}`,
  asset_id: "",
  x: 0,
  y: 0,
  width: 512,
  height: 512,
  opacity: 100,
  flip_x: false,
});

type ImagePending = {
  version: 1;
  projectId: string;
  key: string;
  target: string;
  body: Any;
  form: Any;
  jobId?: string;
};
const pendingImageKey = (pid: string) => `dgx-drama-image-pending:${pid}`;
export function readImagePending(pid: string): ImagePending | null {
  try {
    const p = JSON.parse(
      sessionStorage.getItem(pendingImageKey(pid)) || "null",
    );
    if (
      p?.version !== 1 ||
      p.projectId !== pid ||
      typeof p.key !== "string" ||
      !p.key.startsWith("drama-ui-") ||
      !/^(entity|shot):[^:]+$/.test(p.target) ||
      !TOOLS.some(([tool]) => tool === p.body?.tool) ||
      !p.body.parameters ||
      !p.form
    )
      return null;
    return p;
  } catch {
    return null;
  }
}
const saveImagePending = (p: ImagePending) => {
  try {
    sessionStorage.setItem(pendingImageKey(p.projectId), JSON.stringify(p));
    return true;
  } catch {
    return false;
  }
};
const clearImagePending = (pid: string) => {
  try {
    sessionStorage.removeItem(pendingImageKey(pid));
  } catch {
    /* The live lock remains until the request is resolved. */
  }
};

export function ProductionPanel({
  ctx,
  mode,
}: {
  ctx: WorkbenchContext;
  mode: "materials" | "shots" | "images";
}) {
  const [kind, setKind] = useState("character"),
    [entityId, setEntityId] = useState(""),
    [revisionId, setRevisionId] = useState("");
  const [entityBaseRevision, setEntityBaseRevision] = useState<number | null>(
    null,
  );
  const [entityDraft, setEntityDraft] = useState<Any>(blankEntity),
    [entityDirty, setEntityDirty] = useState(false),
    [materialTab, setMaterialTab] = useState("entities");
  const [shotId, setShotId] = useState(""),
    [production, setProduction] = useState<Any | null>(null),
    [shotDraft, setShotDraft] = useState<Any>({}),
    [shotDirty, setShotDirty] = useState(false),
    [newTitle, setNewTitle] = useState(""),
    [showNewShot, setShowNewShot] = useState(false),
    [selectedReference, setSelectedReference] = useState("");
  const [shotExternalUpdate, setShotExternalUpdate] = useState(false);
  const [shotVideoFile, setShotVideoFile] = useState<File | undefined>();
  const [shotAudioFile, setShotAudioFile] = useState<File | undefined>();
  const observedShotSync = useRef("");
  const shotSync = JSON.stringify({
    shotId,
    shot: ctx.shots.find((s: Any) => s.id === shotId),
    dependencies: ctx.entities.map((e: Any) => [
      e.id,
      e.revision,
      e.approvedRevisionId,
    ]),
  });
  const [reviewIds, setReviewIds] = useState<string[]>([]),
    [reviewReason, setReviewReason] = useState("");
  const [jobs, setJobs] = useState<Any[]>([]),
    [library, setLibrary] = useState<Any[]>([]),
    [source, setSource] = useState(""),
    [file, setFile] = useState<File | null>(null);
  const [showAllImageJobs, setShowAllImageJobs] = useState(false);
  const [target, setTarget] = useState(""),
    [tool, setTool] = useState("generate_image"),
    [gatewayId, setGatewayId] = useState("");
  const [imageForm, setImageForm] = useState<Any>({
    prompt: "",
    promptZh: "",
    ...fitImageSize(ctx.project?.width || 1344, ctx.project?.height || 768),
    seed: 42,
    refs: ["", "", ""],
    steps: 28,
    count: 1, parallelism: 2,
    encoder: "standard",
    precision: "fp8",
    background: "",
    layers: [blankLayer()],
  });
  const [imageScope, setImageScope] = useState("");
  const [imagePreparing, setImagePreparing] = useState(false);
  const [inputInfo, setInputInfo] = useState<{ inputs: ReferenceInput[]; issues: string[] }>({ inputs: [], issues: [] });
  const imageFieldEdits = useRef<Record<string, number>>({});
  const imageLastTargetKey = `dgx-drama-image-last-target:${ctx.project?.id || ""}:${ctx.episode?.id || ""}`;
  const imageDraftKey = (value: string) => `dgx-drama-image-draft:${ctx.project?.id || ""}:${ctx.episode?.id || ""}:${value}`;
  const [imageDirty, setImageDirty] = useState(false),
    [busy, setBusy] = useState(false),
    [receipt, setReceipt] = useState(""),
    [uncertain, setUncertain] = useState(false),
    [trackedJob, setTrackedJob] = useState("");
  const [compare, setCompare] = useState<Record<string, string[]>>({});
  const [gallery, setGallery] = useState<{ jobId: string; ids: string[] } | null>(null);
  const [selectedCandidates, setSelectedCandidates] = useState<Record<string, { targetId: string; slot: string; assetId: string | null }>>({});
  const [selectionInspectionError, setSelectionInspectionError] = useState("");
  useEffect(() => {
    const job = jobs.find((value: Any) => value.id === gallery?.jobId);
    if (!job || !gallery) return;
    let live = true;
    void Promise.allSettled((job.candidates || []).filter((candidate: Any) => gallery.ids.includes(candidate.id))
      .map(async (candidate: Any) => ({ candidate, inspection: await inspectImageSelection(job, candidate) })))
      .then(settled => {
        if (!live) return;
        const failed = settled.some(result => result.status === "rejected");
        const entries = settled.filter((result): result is PromiseFulfilledResult<{ candidate: Any; inspection: Any }> => result.status === "fulfilled").map(result => result.value);
        if (failed) {
          setSelectionInspectionError("无法核查当前选用状态。请刷新候选任务后再选择，避免依据旧状态操作。");
          setSelectedCandidates(current => Object.fromEntries(Object.entries(current).filter(([, value]) => value.targetId !== job.targetId)));
          return;
        }
        setSelectionInspectionError("");
        setSelectedCandidates(current => {
          const imageSlot = shotImageSlot(ctx.params?.purpose);
          const slots = new Set(entries.map(({ inspection }) => `${inspection.targetId}:${inspection.targetType === "entity" ? inspection.sourceRole : imageSlot?.slot || "shot-reference"}`));
          const next = Object.fromEntries(Object.entries(current).filter(([, value]) => !slots.has(`${value.targetId}:${value.slot}`))) as Record<string, { targetId: string; slot: string; assetId: string | null }>;
          for (const { candidate, inspection } of entries) {
            const slot = inspection.targetType === "entity" ? inspection.sourceRole : imageSlot?.slot || "shot-reference";
            const assetId = inspection.targetType === "entity"
              ? currentOf(inspection.target)?.references?.find((reference: Any) => reference.role === slot)?.assetId || null
              : imageSlot ? inspection.target.current?.snapshot?.[imageSlot.assetKey] || null : inspection.target.current?.snapshot?.approvedReferenceAssetId || null;
            next[`server:${inspection.targetId}:${slot}`] = { targetId: inspection.targetId, slot, assetId };
          }
          return next;
        });
      });
    return () => { live = false; };
  }, [gallery, jobs, ctx.entities, ctx.shots, ctx.params?.purpose]);
  const [retrySelection, setRetrySelection] = useState<Record<string, number[]>>({});
  const [retryIntents, setRetryIntents] = useState<Record<string, Any>>({});
  const retryStoreKey = (pid: string) => `dgx-drama-image-retries:${pid}`;
  const persistRetries = (pid: string, value: Record<string, Any>) => sessionStorage.setItem(retryStoreKey(pid), JSON.stringify(value));
  useEffect(() => {
    setCompare({}); setGallery(null); setRetrySelection({});
    try { const saved = JSON.parse(sessionStorage.getItem(retryStoreKey(ctx.project?.id || "")) || "{}");
      setRetryIntents(Object.fromEntries(Object.entries(saved).filter(([id, raw]) => { const v = raw as Any; return v?.parentId === id && typeof v.key === "string" && v.key.startsWith("drama-ui-") && Array.isArray(v.indices) && v.indices.length > 0 && v.indices.every((i: any) => Number.isInteger(i) && i >= 0); })));
    } catch { setRetryIntents({}); }
  }, [ctx.project?.id]);
  const viewedJobId = ctx.params?.jobId || "";
  const targetType = target.split(':')[0], targetRevision = target.split(':')[1];
  const targetOwner = (targetType==='shot' ? ctx.shots : ctx.entities).find((o:Any)=>o.currentRevisionId===targetRevision) || (targetType==='shot' ? ctx.shots : ctx.entities).find((o:Any)=>o.id===jobs.find(j=>j.targetRevisionId===targetRevision)?.targetId);
  const currentTarget = targetOwner ? `${targetType}:${targetOwner.currentRevisionId}` : '';
  const visibleImageJobs = jobs.filter(j=>showAllImageJobs || !target || j.id===viewedJobId || j.targetId===targetOwner?.id || j.targetRevisionId===targetRevision);

  const scrolledViewedJob = useRef("");
  useEffect(() => {
    if (mode !== "images" || !viewedJobId) { scrolledViewedJob.current = ""; return; }
    const key = `${ctx.project?.id}/${ctx.episode?.id}/${viewedJobId}`;
    if (scrolledViewedJob.current === key) return;
    const card = document.getElementById(`production-job-${viewedJobId}`);
    if (!card || card.closest("[hidden]")) return;
    card.scrollIntoView?.({ block: "start", behavior: "smooth" });
    scrolledViewedJob.current = key;
  }, [mode, viewedJobId, ctx.params, jobs, ctx.project?.id, ctx.episode?.id]);
  const [pendingImage, setPendingImage] = useState<ImagePending | null>(null);
  const [confirmation, setConfirmation] = useState<{
    text: string;
    action: () => void;
  } | null>(null);
  const busyRef = useRef(false),
    projectEpoch = useRef(0),
    loadEpoch = useRef(0),
    disposed = useRef(false),
    slowTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shotAuxDirty =
    selectedReference !==
      (production?.current?.snapshot?.approvedReferenceAssetId || "") ||
    reviewIds.length > 0 ||
    reviewReason.length > 0;
  const unresolvedReviewIds = new Set(
    (production?.invalidations || [])
      .filter(
        (i: Any) =>
          !(
            i.resolved ||
            i.reviewedAt ||
            i.resolvedAt ||
            i.status === "RESOLVED"
          ),
      )
      .map((i: Any) => i.id),
  );
  const reviewSelectionValid =
    reviewIds.length > 0 &&
    reviewIds.every((id) => unresolvedReviewIds.has(id));
  const scriptReviewIds = reviewIds.filter((id) =>
    production?.invalidations?.some(
      (i: Any) => i.id === id && i.kind === "script",
    ),
  );
  const entityReviewIds = reviewIds.filter((id) =>
    production?.invalidations?.some(
      (i: Any) => i.id === id && i.kind !== "script",
    ),
  );
  const hasScriptInvalidation = (production?.invalidations || []).some(
    (i: Any) => i.kind === "script" && unresolvedReviewIds.has(i.id),
  );
  const reviewStorage = `dgx.drama.review.pending:${ctx.project?.id || ""}/${ctx.episode?.id || ""}`;
  const readReviewPending = () => {
    try {
      return JSON.parse(sessionStorage.getItem(reviewStorage) || "null");
    } catch {
      return null;
    }
  };
  const [pendingReview, setPendingReview] = useState<Any | null>(
    readReviewPending,
  );
  const shotHasUnsaved = shotDirty || shotAuxDirty || !!pendingReview;
  const dirty = entityDirty || shotHasUnsaved || imageDirty;
  const ask = (text: string, action: () => void) =>
    setConfirmation({ text, action });
  const protect = (hasDirty: boolean, action: () => void) =>
    hasDirty
      ? ask("当前有未保存的修改。切换后会放弃这些修改，确定继续吗？", action)
      : action();
  const entity = ctx.entities.find((e: Any) => e.id === entityId);
  const revision =
    entity?.revisions?.find((r: Any) => r.id === revisionId) ||
    currentOf(entity);
  const fetchJobs = useCallback(async () => {
    if (!ctx.project?.id) return;
    const epoch = projectEpoch.current;
    const data = await get(
      `${path("projects", ctx.project.id)}/reference-jobs`,
    );
    if (!disposed.current && epoch === projectEpoch.current)
      setJobs(data.jobs || []);
  }, [ctx.project?.id]);
  useEffect(() => {
    disposed.current = false;
    return () => {
      disposed.current = true;
      if (slowTimer.current) clearTimeout(slowTimer.current);
    };
  }, []);
  const targetShotListed = !target.startsWith("shot:") || ctx.shots.some(s => `shot:${s.currentRevisionId}` === target);
  useEffect(() => {
    if (!target || !ctx.project?.id) return;
    let live = true;
    const scope = imageDraftKey(target), edits = { ...imageFieldEdits.current };
    const pending = readImagePending(ctx.project.id);
    let saved: Any | null = null;
    try { saved = JSON.parse(sessionStorage.getItem(scope) || "null"); } catch { /* Invalid draft is ignored. */ }
    const valid = saved?.version === 1 && saved.target === target && saved.form && TOOLS.some(([key]) => key === saved.tool)
      && (saved.dirty || saved.form.prompt || saved.trackedJob || saved.receipt);
    const fixed = pending?.target === target ? pending : null;
    setImageScope("");
    setInputInfo({ inputs: [], issues: [] });
    setTrackedJob(fixed?.jobId || (valid ? saved!.trackedJob || "" : ""));
    setReceipt(fixed ? "已恢复原请求身份，请核查原任务。" : valid ? saved!.receipt || "" : "");
    if (fixed || valid) {
      setImageForm(structuredClone(fixed?.form || saved!.form));
      setTool(fixed?.body.tool || saved!.tool);
      setImageDirty(valid ? Boolean(saved!.dirty) : false);
    } else {
      const imageSlot = shotImageSlot(ctx.params?.purpose);
      setImageForm({ prompt: "", promptZh: "", ...fitImageSize(imageSlot ? 1344 : (ctx.project.width || 1344), imageSlot ? 768 : (ctx.project.height || 768)),
        seed: 42, refs: ["", "", ""], steps: 28, count: imageSlot ? 2 : 1, parallelism: 2, encoder: "standard", precision: "fp8", background: "", layers: [blankLayer()] });
      setTool("generate_image");
      setImageDirty(false);
    }
    const finish = (snapshot: Any, inputs: ReferenceInput[], issues: string[], entityPrompt = "") => {
      if (!live) return;
      setInputInfo({ inputs, issues });
      if (!fixed && !valid) {
        const untouched = (key: string) => imageFieldEdits.current[key] === edits[key];
        const imageSlot = shotImageSlot(ctx.params?.purpose);
        const prompt = entityPrompt || planningImagePrompt(ctx.params?.purpose, snapshot, ctx.entities);
        setImageForm(form => ({ ...form,
          ...(untouched("prompt") ? { prompt } : {}),
          ...(untouched("refs") ? { refs: inputs.length <= 3 ? [...inputs.map(r => r.assetId), "", "", ""].slice(0, 3) : ["", "", ""] } : {}),
          ...(imageSlot ? fitImageSize(1344, 768) : {}),
          ...(imageSlot ? { count: 2 } : {}),
        }));
        if (untouched("tool")) setTool(target.startsWith("shot:") && inputs.length ? "edit_image" : "generate_image");
      }
      setImageScope(scope);
      setImagePreparing(false);
    };
    const [kind, rid] = target.split(":");
    if (kind === "entity") {
      const entity = ctx.entities.find(e => e.revisions?.some((r: Any) => r.id === rid));
      const revision = entity?.revisions?.find((r: Any) => r.id === rid);
      const approved = entity?.revisions?.find((r: Any) => r.id === entity.approvedRevisionId);
      const role = ROLES[entity?.kind]?.[0]?.[0];
      const assetId = approved?.references?.find((r: Any) => r.role === role)?.assetId || approved?.referenceAssetId;
      finish({}, assetId ? [{ assetId, name: approved.name, revisionId: approved.id, revision: approved.revision, kind: entity.kind }] : [], [], [revision?.name, revision?.description].filter(Boolean).join("。"));
    } else {
      const shot = ctx.shots.find(s => s.currentRevisionId === rid);
      if (!shot) { finish({}, [], ["目标修订已不在当前镜头列表，请重新选择制作目标。"]); }
      else {
        setImagePreparing(true);
        get(`${path("shots", shot.id)}/production`).then(p => {
          const revision = p.currentRevisionId === rid ? p.current : p.history?.find((r: Any) => r.id === rid);
          const snapshot = revision?.snapshot || {};
          const info = shotReferenceInputs(snapshot, ctx.entities, revision?.approved === true);
          info.inputs = info.inputs.map(r => r.kind === "shot" ? { ...r, revisionId: revision.id, revision: revision.revision } : r);
          if (!revision?.snapshot) info.issues.push("镜头内容尚未读取完整，请重新选择目标。");
          if (p.currentRevisionId !== rid) info.issues.push("镜头内容已经更新，请重新选择最新目标修订。");
          if ((p.invalidations || []).some((i: Any) => !(i.resolved || i.reviewedAt || i.resolvedAt || i.status === "RESOLVED"))) info.issues.push("关联资料或剧本已更新，请返回镜头复核后再制作。");
          finish(snapshot, info.inputs, info.issues);
        }).catch((e: Error) => finish({}, [], [`镜头资料读取失败：${e.message}。请重新选择目标重试。`]));
      }
    }
    return () => { live = false; };
  }, [target, ctx.project?.id, ctx.episode?.id, targetShotListed, ctx.params?.purpose]);
  useEffect(() => {
    if (!target || imageScope !== imageDraftKey(target)) return;
    try {
      sessionStorage.setItem(imageLastTargetKey, target);
      sessionStorage.setItem(imageScope, JSON.stringify({ version: 1, target, tool, form: imageForm, dirty: imageDirty, trackedJob, receipt }));
    } catch { /* Pending request persistence is mandatory at submit; draft storage is best effort. */ }
  }, [imageScope, target, tool, imageForm, imageDirty, trackedJob, receipt]);
  useEffect(() => {
    const before = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty]);
  useEffect(() => {
    projectEpoch.current++;
    loadEpoch.current++;
    setEntityId("");
    setRevisionId("");
    setEntityDraft(blankEntity());
    setEntityDirty(false);
    setShotId("");
    setProduction(null);
    setShotDraft({});
    setShotDirty(false);
    setShotVideoFile(undefined);
    setShotAudioFile(undefined);
    setSelectedReference("");
    setReviewIds([]);
    setReviewReason("");
    setImageDirty(false);
    setPendingReview(readReviewPending());
    setJobs([]);
    setImageScope("");
    let lastTarget = "";
    try { lastTarget = sessionStorage.getItem(imageLastTargetKey) || ""; } catch { /* Defaults to no target. */ }
    setTarget(ctx.params?.target || lastTarget);
    setTrackedJob("");
    const pending = readImagePending(ctx.project?.id || "");
    setPendingImage(pending);
    setUncertain(Boolean(pending));
    setReceipt(
      pending
        ? `已恢复原请求身份，请核查结果，勿重复生成。请求键：${pending.key}`
        : "",
    );
    if (pending) {
      setTarget(pending.target);
      setTool(pending.body.tool);
      setTrackedJob(pending.jobId || "");
    }
    setImageForm(
      pending?.form || {
        prompt: "",
        promptZh: "",
        ...fitImageSize(ctx.project?.width || 1344, ctx.project?.height || 768),
        seed: 42,
        refs: ["", "", ""],
        steps: 28,
        count: 1, parallelism: 2,
        encoder: "standard",
        precision: "fp8",
        background: "",
        layers: [blankLayer()],
      },
    );
  }, [ctx.project?.id, ctx.episode?.id]);
  useEffect(() => {
    let live = true;
    if (!ctx.project?.id) return;
    get(`${path("projects", ctx.project.id)}/image-library`)
      .then((v: Any) => {
        if (live) setLibrary(v.sources || []);
      })
      .catch((e: Error) => ctx.notify(e.message, "error"));
    return () => {
      live = false;
    };
  }, [ctx.project?.id]);
  useEffect(() => {
    let live = true;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        await fetchJobs();
      } catch (e) {
        if (live) setReceipt(`任务查询暂不可用：${(e as Error).message}`);
      } finally {
        if (live) timer = setTimeout(poll, 5000);
      }
    };
    void poll();
    return () => {
      live = false;
      clearTimeout(timer);
    };
  }, [fetchJobs]);
  const run = async (action: () => Promise<void>) => {
    if (busyRef.current) return;
    busyRef.current = true;
    setBusy(true);
    try {
      await action();
    } catch (e) {
      ctx.notify((e as Error).message, "error");
    } finally {
      busyRef.current = false;
      if (!disposed.current) setBusy(false);
    }
  };
  const editEntity = (key: string, value: any) => {
    setEntityDraft((d) => ({ ...d, [key]: value }));
    setEntityDirty(true);
  };
  const editShot = (key: string, value: any) => {
    setShotDraft((d) => ({ ...d, [key]: value }));
    setShotDirty(true);
  };
  const editImage = (key: string, value: any) => {
    imageFieldEdits.current[key] = (imageFieldEdits.current[key] || 0) + 1;
    setImageForm((d) => ({ ...d, [key]: value }));
    setImageDirty(true);
  };
  const selectEntity = (id: string, rid = "") => {
    const e = ctx.entities.find((v: Any) => v.id === id),
      r = e?.revisions?.find((v: Any) => v.id === rid) || currentOf(e);
    setEntityId(id);
    setEntityBaseRevision(e?.revision ?? null);
    setRevisionId(r?.id || "");
    setKind(e?.kind || kind);
    setEntityDraft(
      r ? { ...r, references: [...(r.references || [])] } : blankEntity(),
    );
    setEntityDirty(false);
  };
  const loadShot = async (
    id: string,
    keep: { reference?: boolean; review?: boolean } = {},
  ) => {
    const epoch = ++loadEpoch.current;
    setShotId(id);
    setProduction(null);
    if (!id) return;
    const p = await get(`${path("shots", id)}/production`);
    if (epoch !== loadEpoch.current || disposed.current) return;
    setProduction(p);
    setShotDraft({ ...p.current?.snapshot });
    if (!keep.reference)
      setSelectedReference(p.current?.snapshot?.approvedReferenceAssetId || "");
    setShotDirty(false);
    setShotExternalUpdate(false);
    if (!keep.review) {
      setReviewIds([]);
      setReviewReason("");
    }
  };
  const sendReview = async (intent: Any) => {
    sessionStorage.setItem(reviewStorage, JSON.stringify(intent));
    setPendingReview(intent);
    try {
      await post(intent.path, intent.body, intent.key);
      await ctx.refresh();
      await loadShot(intent.shotId, { reference: true, review: true });
      setReviewIds(intent.remainingIds);
      setReviewReason(intent.remainingIds.length ? intent.reason : "");
      sessionStorage.removeItem(reviewStorage);
      setPendingReview(null);
      ctx.notify("所选旧依赖的复核原因已记录；其他组需要分别确认。", "success");
    } catch (e: any) {
      if ([400, 401, 403, 404, 409, 422].includes(e?.status)) {
        sessionStorage.removeItem(reviewStorage);
        setPendingReview(null);
      }
      throw e;
    }
  };
  const submitReview = (script: boolean) => {
    const ids = script ? scriptReviewIds : entityReviewIds;
    return run(() =>
      sendReview({
        path: `${path("shots", shotId)}/${script ? "script-review" : "review"}`,
        shotId,
        key: requestKey(),
        body: {
          expectedRevision: production.revision,
          invalidationIds: ids,
          reason: reviewReason.trim(),
        },
        remainingIds: reviewIds.filter((id) => !ids.includes(id)),
        reason: reviewReason,
      }),
    );
  };
  useEffect(() => {
    if (mode === "shots" && !shotId && ctx.shots.length)
      void run(() => loadShot(ctx.params?.shotId || ctx.shots[0].id));
  }, [mode, ctx.shots]);
  useEffect(() => {
    if (
      !readImagePending(ctx.project?.id || "") &&
      ctx.params?.target &&
      ctx.params.target !== target
    )
      setTarget(ctx.params.target);
    if (ctx.params?.entityId && ctx.params.entityId !== entityId)
      protect(entityDirty, () => selectEntity(ctx.params.entityId));
    if (ctx.params?.shotId && ctx.params.shotId !== shotId)
      protect(
        shotHasUnsaved,
        () => void run(() => loadShot(ctx.params.shotId)),
      );
  }, [ctx.params]);
  useEffect(() => {
    const saved = (e: Event) => {
      const id = (e as CustomEvent).detail?.shotId;
      if (id !== shotId) return;
      if (shotHasUnsaved)
        ctx.notify(
          "画布已保存新修订；请先保存或核查当前未保存表单，再刷新。",
          "info",
        );
      else
        void run(async () => {
          await ctx.refresh();
          await loadShot(id);
        });
    };
    window.addEventListener("dgx-drama-shot-saved", saved);
    return () => window.removeEventListener("dgx-drama-shot-saved", saved);
  }, [shotId, shotHasUnsaved]);
  useEffect(() => {
    if (!shotId || !production) {
      observedShotSync.current = shotSync;
      return;
    }
    if (observedShotSync.current === shotSync || busy) return;
    observedShotSync.current = shotSync;
    if (shotHasUnsaved) {
      setShotExternalUpdate(true);
      ctx.notify(
        "镜头或关联资料已更新。未保存的输入已保留，请核查后重新读取镜头详情。",
        "info",
      );
      return;
    }
    void run(() => loadShot(shotId));
  }, [shotSync, production, shotHasUnsaved, busy]);
  const refreshEntityResult = async (result: Any) => {
    const e = result.entity;
    await ctx.refresh();
    if (e) {
      const r = currentOf(e);
      setEntityId(e.id);
      setEntityBaseRevision(e.revision);
      setRevisionId(r.id);
      setKind(e.kind);
      setEntityDraft({ ...r });
    }
    setEntityDirty(false);
  };
  const saveEntity = () =>
    run(async () => {
      if (!entityDraft.name?.trim()) throw new Error("请填写资料名称。");
      const role = entityDraft.referenceRole || ROLES[kind][0][0];
      const refs = entityDraft.references || [];
      const body = {
        name: entityDraft.name.trim(),
        description: entityDraft.description || "",
        references: refs,
        referenceRole: role,
        referenceAssetId:
          refs.find((r: Any) => r.role === role)?.assetId || null,
      };
      const result = entity
        ? await post(`${path("entities", entity.id)}/revisions`, {
            ...body,
            expectedRevision: entityBaseRevision,
          })
        : await post(`${path("projects", ctx.project.id)}/entities`, {
            ...body,
            kind,
          });
      await refreshEntityResult(result);
      ctx.notify("资料草稿已保存。选好参考图后确认此参考版本。", "success");
    });
  const saveShot = () =>
    run(async () => {
      if (!shotDraft.title?.trim()) throw new Error("请填写镜头名称。");
      await post(`${path("shots", shotId)}/revisions`, {
        expectedRevision: production?.revision,
        baseRevisionId: production?.currentRevisionId,
        snapshot: {
          ...shotDraft,
          title: shotDraft.title.trim(),
          generation: {
            ...ring4Generation,
            requestDurationSeconds: generationOf(shotDraft.generation).requestDurationSeconds,
          },
        },
      });
      setShotDirty(false);
      await ctx.refresh();
      await loadShot(shotId, { reference: true, review: true });
      ctx.notify("镜头已保存为新修订。下一步制作或批准参考画面。", "success");
    });
  const makeImage = (value: string, purpose = "primary") => {
    setTarget(value);
    ctx.navigate("images", { target: value, purpose });
  };
  const selectCandidate = async (job: Any, candidate: Any, fromCompare = false) => {
    const inspection = await inspectImageSelection(job, candidate);
    const apply = async () => {
      const imageSlot = job.targetType === "shot" ? shotImageSlot(ctx.params?.purpose) : null;
      const saved = imageSlot
        ? { targetId: job.targetId, targetType: "shot", sourceRole: imageSlot.slot, result: await post(`${path("shots", job.targetId)}/${imageSlot.path}`, { assetId: candidate.assetId, candidateId: candidate.id, expectedRevision: inspection.expectedRevision, acceptStaleCandidate: inspection.stale }) }
        : await selectImageCandidate(job, candidate, {
            acceptStale: inspection.stale,
            expectedRevision: inspection.expectedRevision,
          });
      const slot = job.targetType === "entity" ? saved.sourceRole : imageSlot?.slot || "shot-reference";
      const assetId = saved.targetType === "entity"
        ? currentOf(saved.result.entity)?.references?.find((reference: Any) => reference.role === slot)?.assetId
        : imageSlot ? saved.result.current?.snapshot?.[imageSlot.assetKey] : saved.result.current?.snapshot?.approvedReferenceAssetId;
      setSelectedCandidates(current => replaceSelectedCandidate(current, `server:${saved.targetId}:${slot}`, saved.targetId, slot, assetId || null));
      await ctx.refresh();
      if (job.targetType === "entity") {
        setMaterialTab("entities");
        if (!fromCompare) ctx.navigate("materials", { entityId: job.targetId });
        ctx.notify("已选为新参考草稿。请检查后点击确认此参考版本。", "success");
      } else {
        if (!fromCompare) ctx.navigate(imageSlot ? "shots" : "video", { shotId: job.targetId });
        ctx.notify(imageSlot
          ? (imageSlot.sendsToH3 ? `${imageSlot.label}已加入镜头修订；它只用于约束姿势和身体力学。` : `${imageSlot.label}已加入镜头修订；只给戏台看，不会送入 H3。`)
          : "本镜头画面已确认；视频仍需由你在下一页单独提交。", "success");
      }
      return saved;
    };
    if (inspection.stale) {
      ask(job.targetType === "entity"
        ? "此图片来自旧版本。仍要将它保存为当前资料的新参考草稿吗？"
        : "此图片来自镜头旧版本。仍要确认为当前镜头的新参考画面吗？",
      () => void run(async () => { await apply(); }));
      return;
    }
    await apply();
  };
  const candidateIsSelected = (job: Any, candidate: Any) => {
    const imageSlot = job.targetType === "shot" ? shotImageSlot(ctx.params?.purpose) : null;
    const slot = job.targetType === "entity"
      ? (job.referenceRole || ctx.entities.find((value: Any) => value.id === job.targetId)?.revisions?.find((revision: Any) => revision.id === (candidate.targetRevisionId || job.targetRevisionId))?.referenceRole || "identity_front")
      : imageSlot?.slot || "shot-reference";
    const cached = Object.entries(selectedCandidates).find(([, value]) => value.targetId === job.targetId && value.slot === slot);
    if (cached) return cached[1].assetId === candidate.assetId;
    if (job.targetType === "entity") {
      const entity = ctx.entities.find((value: Any) => value.id === job.targetId);
      const source = entity?.revisions?.find((revision: Any) => revision.id === (candidate.targetRevisionId || job.targetRevisionId));
      const current = currentOf(entity);
      const role = job.referenceRole || source?.referenceRole || ROLES[entity?.kind]?.[0]?.[0];
      return Boolean(role && current?.references?.some((reference: Any) => reference.role === role && reference.assetId === candidate.assetId));
    }
    return job.targetType === "shot" && job.targetId === shotId && (imageSlot ? production?.current?.snapshot?.[imageSlot.assetKey] : production?.current?.snapshot?.approvedReferenceAssetId) === candidate.assetId;
  };
  const sendFrozenImage = async (pending: ImagePending) => {
    const [type, rid] = pending.target.split(":");
    setReceipt("正在确认原请求，请稍候。无需重复点击。");
    slowTimer.current = setTimeout(
      () => setReceipt("服务仍在确认任务，请勿重复提交。"),
      15000,
    );
    try {
      const result = await post(
        `${path(type === "entity" ? "entity-revisions" : "shot-revisions", rid)}/reference-jobs`,
        pending.body,
        pending.key,
      );
      if (!result.job?.id) {
        setUncertain(true);
        setReceipt(
          `提交结果尚未确认。请核查任务列表，勿重复生成。请求键：${pending.key}`,
        );
        return;
      }
      setJobs((v) => [result.job, ...v.filter((j) => j.id !== result.job.id)]);
      setTrackedJob(result.job.id);
      setImageDirty(false);
      const unknown = result.job.state === "SUBMISSION_UNKNOWN";
      setUncertain(unknown);
      if (unknown) {
        const next = { ...pending, jobId: result.job.id };
        saveImagePending(next);
        setPendingImage(next);
      } else {
        clearImagePending(pending.projectId);
        setPendingImage(null);
      }
      setReceipt("任务已接收并保存，可在任务中心查看进度或取消。完成后在下方选图；需要新一组时请单独选择“再生成一组”。");
      ctx.notify("图片任务回执已确认，进度将在此处更新。", "success");
    } catch (e) {
      const error = e as Error & { status?: number };
      if (
        !error.status ||
        error.status >= 500 ||
        [408, 429].includes(error.status)
      ) {
        setUncertain(true);
        setReceipt(`结果待确认。先核查原任务，勿重复提交。${error.message}`);
      } else {
        clearImagePending(pending.projectId);
        setPendingImage(null);
        setUncertain(false);
        setReceipt(error.message);
      }
      throw e;
    } finally {
      if (slowTimer.current) clearTimeout(slowTimer.current);
    }
  };
  const submitImage = () =>
    run(async () => {
      if (uncertain || pendingImage || trackedJob || imagePreparing) return;
      if (inputInfo.issues.length) throw new Error(inputInfo.issues.join(" "));
      if (tool === "edit_image" && inputInfo.inputs.some(r => !imageForm.refs.includes(r.assetId))) throw new Error("请包含全部已确认参考图；超过三张时先返回镜头调整关联或确认完整合成参考画面。");
      if (!target) throw new Error("请选择制作目标。");
      const parameters: Any = imageParameters(tool, imageForm);
      if (tool === "compose_image_layers") {
        const ids = [parameters.background_asset_id, ...parameters.layers.map((layer: Any) => layer.asset_id)];
        if (inputInfo.inputs.some(r => !ids.includes(r.assetId))) throw new Error("合成必须包含全部关联参考图，请逐一添加图层，不能省略关联人物。");
      }
      const pending: ImagePending = {
        version: 1,
        projectId: ctx.project.id,
        target,
        key: requestKey(),
        body: { tool, parameters, ...(target.startsWith("entity:") && (imageForm.referenceRole || ROLES[targetOwner?.kind]?.[0]?.[0])
          ? { referenceRole: imageForm.referenceRole || ROLES[targetOwner?.kind][0][0] }
          : {}) },
        form: structuredClone(imageForm),
      };
      if (!saveImagePending(pending))
        throw new Error(
          "浏览器未能暂存请求身份。请检查存储设置后再提交，当前尚未发送制作任务。",
        );
      setPendingImage(pending);
      await sendFrozenImage(pending);
    });
  const recoverImages = (job: Any) => run(async () => {
    if (!recoverableImageJob(job)) throw new Error("当前任务不符合已生成图片恢复条件，请先核查原任务。");
    const epoch = projectEpoch.current;
    const result = await post(`${path("reference-jobs", job.id)}/recover`, {});
    if (!result.job?.id) throw new Error("恢复回执缺少任务编号，请核查原任务；不会重新生成图片。");
    if (epoch !== projectEpoch.current || disposed.current) return;
    setJobs(current => current.map(row => row.id === result.job.id ? result.job : row));
    ctx.notify("已核查已生成图片的归档状态。此操作仅恢复已有图片，不重新生成。", "success");
  });
  const retryImages = (job: Any) => run(async () => {
    const pid = ctx.project.id, epoch = projectEpoch.current;
    let intent = retryIntents[job.id];
    if (!intent || intent.jobId) {
      const indices = retrySelection[job.id] || [], allowed = remainingImageRetryIndices(job, intent, jobs);
      if (!indices.length || indices.some(index => !allowed.includes(index))) throw new Error("只能重拍已确认失败或取消的候选；请先核查原任务及资源归还。");
      intent = { parentId: job.id, indices: [...indices], key: requestKey(), history: completedImageRetries(intent).map(attempt => ({ key: attempt.key, indices: attempt.indices, jobId: attempt.jobId })) };
      const saved = { ...retryIntents, [job.id]: intent };
      try { persistRetries(pid, saved); } catch { throw new Error("无法保存重拍请求身份，尚未提交。请检查浏览器存储。"); }
      setRetryIntents(saved);
    }
    try {
      const result = await post(`${path("reference-jobs", job.id)}/retry`, { candidateIndices: intent.indices }, intent.key);
      if (!result.job?.id) throw new Error("重拍回执缺少任务编号，请按原请求键核查。");
      const saved = { ...retryIntents }; delete saved[job.id];
      persistRetries(pid, saved);
      if (epoch !== projectEpoch.current || disposed.current) return;
      setRetryIntents(saved);
      setRetrySelection(current => ({ ...current, [job.id]: [] }));
      setJobs(current => [{ ...result.job, retryOf: result.job.retryOf || job.id, parameters: { ...result.job.parameters, candidate_indices: result.job.parameters?.candidate_indices || intent.indices } }, ...current.filter(row => row.id !== result.job.id)]);
      ctx.notify("所选失败候选已创建重拍任务，成功图片仍保留。可在任务中心查看新任务。", "success");
    } catch (error: any) {
      if (error.status >= 400 && error.status < 500 && ![408, 429].includes(error.status)) {
        const saved = { ...retryIntents }, history = completedImageRetries(intent);
        const previous = history.at(-1);
        if (previous) saved[job.id] = { ...previous, parentId: job.id, history: history.slice(0, -1) }; else delete saved[job.id];
        persistRetries(pid, saved);
        if (epoch === projectEpoch.current && !disposed.current) setRetryIntents(saved);
      }
      throw new Error(`重拍结果需核查：${error.message}。原请求身份已保留时，请用原键核查，勿重复生成。`);
    }
  });
  const deps = (k: string, selected: string[]) =>
    ctx.entities
      .filter((e: Any) => e.kind === k)
      .flatMap((e: Any) =>
        (e.revisions || [])
          .filter(
            (r: Any) =>
              r.id === e.approvedRevisionId || selected.includes(r.id),
          )
          .map((r: Any) => ({ ...r, approved: r.id === e.approvedRevisionId, hasApprovedVersion: Boolean(e.approvedRevisionId) })),
      );
  const shotGeneration = {
    ...ring4Generation,
    requestDurationSeconds: generationOf(shotDraft.generation).requestDurationSeconds,
  };
  const renderDependency = (k: string, field: string) => (
    <div className="space-y-3">
      <Label>{KINDS[k]}关联</Label>
      <div className="grid gap-2 sm:grid-cols-2">
        {deps(k, shotDraft[field] || []).map((r: Any) => (
          <label
            key={r.id}
            className="flex cursor-pointer items-center gap-3 rounded-lg border p-3"
          >
            <Checkbox
              checked={(shotDraft[field] || []).includes(r.id)}
              onCheckedChange={(v) =>
                editShot(
                  field,
                  v
                    ? [...(shotDraft[field] || []), r.id]
                    : (shotDraft[field] || []).filter(
                        (id: string) => id !== r.id,
                      ),
                )
              }
            />
            <Picture ctx={ctx} id={r.referenceAssetId} className="size-12" />
            <span className="text-sm">
              {r.name}
              <span className="block text-xs text-muted-foreground">
                v{r.revision} · {r.approved ? "已批准" : r.hasApprovedVersion ? "旧依赖，请复核" : "参考形象尚未批准"}
              </span>
            </span>
          </label>
        ))}
      </div>
      {!deps(k, shotDraft[field] || []).length && (
        <p className="text-xs text-muted-foreground">
          尚无已批准的{KINDS[k]}资料。
        </p>
      )}
    </div>
  );
  return (
    <section
      className="space-y-6"
      aria-busy={busy}
      data-production-dirty={dirty ? "true" : "false"}
      aria-label={
        mode === "shots"
          ? "镜头工作区"
          : mode === "images"
            ? "图片制作工作区"
            : "人物与场景工作区"
      }
    >
      {pendingReview && (
        <Card>
          <CardContent className="space-y-3 pt-6">
            <p role="status">
              复核请求尚未确认。原镜头和请求键已保存，核查前不能提交新的复核。
            </p>
            <p className="break-all text-xs text-muted-foreground">
              {pendingReview.shotId} · {pendingReview.key}
            </p>
            <Button
              disabled={busy}
              onClick={() => void run(() => sendReview(pendingReview))}
            >
              按原请求键核查复核结果
            </Button>
          </CardContent>
        </Card>
      )}
      <fieldset
        disabled={busy || !!pendingReview}
        className="min-w-0 space-y-6 border-0 p-0 m-0"
      >
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <p className="mb-1 text-xs uppercase tracking-[0.2em] text-muted-foreground">
              {mode === "shots"
                ? "Shot design"
                : mode === "images"
                  ? "Image studio"
                  : "Creative library"}
            </p>
            <h2 className="text-2xl font-semibold tracking-tight">
              {mode === "shots"
                ? "把故事变成画面"
                : mode === "images"
                  ? "制作参考画面"
                  : "故事从这里开始"}
            </h2>
            <p className="mt-2 text-sm text-muted-foreground">
              {mode === "shots"
                ? "写清动作即可生成视频；也可添加参考图来约束画面。"
                : mode === "images"
                  ? "选择目标、描述画面。制作进度与候选图片会留在这里。"
                  : "为人物和场景建立可信的视觉档案。每一次修订都可追溯。"}
            </p>
          </div>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() =>
              void run(async () => {
                await ctx.refresh();
                await fetchJobs();
                ctx.notify("列表已刷新，未保存的表单内容已保留。", "success");
              })
            }
          >
            <RefreshCw className="size-4" />
            刷新列表
          </Button>
        </div>
        {mode === "materials" && (
          <>
            <Tabs value={materialTab} onValueChange={setMaterialTab}>
              <TabsList>
                <TabsTrigger value="entities">人物与场景</TabsTrigger>
                <TabsTrigger value="library">项目图片库</TabsTrigger>
              </TabsList>
              <TabsContent value={materialTab} className="space-y-5">
                {materialTab === "entities" ? (
                  <div className="grid gap-5 xl:grid-cols-[260px_minmax(0,1fr)]">
                    <aside className="space-y-4">
                      <Choice
                        label="资料类型"
                        value={kind}
                        options={Object.entries(KINDS)}
                        onChange={(v) =>
                          protect(entityDirty, () => {
                            setKind(v);
                            setEntityId("");
                            setRevisionId("");
                            setEntityDraft(blankEntity());
                            setEntityDirty(false);
                          })
                        }
                      />
                      <Button
                        className="w-full"
                        variant="outline"
                        onClick={() =>
                          protect(entityDirty, () => selectEntity(""))
                        }
                      >
                        <Plus className="size-4" />
                        新建{KINDS[kind]}
                      </Button>
                      <div className="space-y-2">
                        {ctx.entities
                          .filter((e: Any) => e.kind === kind)
                          .map((e: Any) => {
                            const r = currentOf(e);
                            return (
                              <button
                                type="button"
                                aria-label={`${r.name} · v${r.revision}`}
                                key={e.id}
                                className={`flex w-full items-center gap-3 rounded-xl border p-3 text-left transition hover:bg-accent ${entityId === e.id ? "border-primary bg-primary/5" : ""}`}
                                onClick={() =>
                                  protect(entityDirty, () => selectEntity(e.id))
                                }
                              >
                                <Picture
                                  ctx={ctx}
                                  id={r.referenceAssetId}
                                  className="size-14 shrink-0"
                                />
                                <span className="min-w-0">
                                  <span className="block truncate text-sm font-medium">
                                    {r.name}
                                  </span>
                                  <span className="mt-1 block text-xs text-muted-foreground">
                                    v{r.revision} ·{" "}
                                    {e.approvedRevisionId === r.id
                                      ? "已批准"
                                      : "草稿"}
                                  </span>
                                </span>
                              </button>
                            );
                          })}
                      </div>
                    </aside>
                    <Card>
                      <CardHeader>
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <CardTitle>
                            {entity ? nameOf(revision) : `新建${KINDS[kind]}`}
                          </CardTitle>
                          <Badge
                            variant={
                              entity?.approvedRevisionId === revision?.id &&
                              revision
                                ? "default"
                                : "secondary"
                            }
                          >
                            {entityDirty
                              ? "有未保存修改"
                              : entity?.approvedRevisionId === revision?.id &&
                                  revision
                                ? "已批准"
                                : "草稿"}
                          </Badge>
                        </div>
                      </CardHeader>
                      <CardContent className="space-y-6">
                        {entity && (
                          <Choice
                            label="查看修订历史"
                            value={revisionId || revision?.id}
                            options={entity.revisions.map((r: Any) => [
                              r.id,
                              `v${r.revision} · ${r.name}${r.id === entity.approvedRevisionId ? " · 当前批准" : ""}`,
                            ])}
                            onChange={(v) =>
                              protect(entityDirty, () =>
                                selectEntity(entityId, v),
                              )
                            }
                          />
                        )}
                        <Field
                          label={`${KINDS[kind]}名称`}
                          value={entityDraft.name}
                          onChange={(v) => editEntity("name", v)}
                        />
                        <Field
                          label="形象、状态与制作说明"
                          multiline
                          value={entityDraft.description}
                          onChange={(v) => editEntity("description", v)}
                        />
                        <div className="grid gap-4 md:grid-cols-2">
                          {ROLES[kind].map(([role, label], i) => (
                            <AssetPicker
                              key={role}
                              ctx={ctx}
                              label={`${label}${i === 0 ? " · 批准必需" : " · 可选"}`}
                              value={
                                entityDraft.references?.find(
                                  (r: Any) => r.role === role,
                                )?.assetId ||
                                (entityDraft.referenceRole === role
                                  ? entityDraft.referenceAssetId
                                  : "") ||
                                ""
                              }
                              onChange={(id) =>
                                editEntity("references", [
                                  ...(entityDraft.references || []).filter(
                                    (r: Any) => r.role !== role,
                                  ),
                                  ...(id ? [{ role, assetId: id }] : []),
                                ])
                              }
                            />
                          ))}
                        </div>
                        <Choice
                          label="主参考用途"
                          options={ROLES[kind]}
                          value={entityDraft.referenceRole || ROLES[kind][0][0]}
                          onChange={(v) => editEntity("referenceRole", v)}
                        />
                        <div className="rounded-lg bg-muted/60 p-4 text-sm text-muted-foreground">
                          已有参考图的版本可以立即确认。全位图也算。确认后镜头才能引用这版形象；旧版本始终保留。
                        </div>
                        <div className="flex flex-wrap gap-2">
                          <Button
                            onClick={saveEntity}
                            disabled={busy || (!!entity && !entityDirty)}
                          >
                            {busy ? (
                              <LoaderCircle className="size-4 animate-spin" />
                            ) : (
                              <Check className="size-4" />
                            )}
                            {entity ? "保存为新修订" : `创建${KINDS[kind]}草稿`}
                          </Button>
                          {entity && (
                            <>
                              <Button
                                variant="outline"
                                disabled={busy || entityDirty}
                                onClick={() =>
                                  makeImage(
                                    `entity:${entity.currentRevisionId}`,
                                  )
                                }
                              >
                                <Sparkles className="size-4" />
                                制作参考图
                              </Button>
                              <Button
                                variant="secondary"
                                disabled={
                                  busy ||
                                  entityDirty ||
                                  !revision ||
                                  entity.approvedRevisionId === revision.id ||
                                  !(revision.references || []).length
                                }
                                onClick={() =>
                                  void run(async () => {
                                    const result = await post(
                                      `${path("entities", entity.id)}/approval`,
                                      {
                                        revisionId: revision.id,
                                        expectedRevision: entity.revision,
                                      },
                                    );
                                    if (result.entity)
                                      setEntityBaseRevision(
                                        result.entity.revision,
                                      );
                                    await ctx.refresh();
                                    ctx.notify(
                                      `此版本已批准。${result.affectedShotIds?.length ? `${result.affectedShotIds.length} 个关联镜头需复核，请到制作镜头更新依赖。` : "可以用于镜头制作。"}`,
                                      "success",
                                    );
                                  })
                                }
                              >
                                {entity.approvedRevisionId === revision?.id
                                  ? "此版本已批准"
                                  : "确认此参考版本"}
                              </Button>
                            </>
                          )}
                        </div>
                        {entity && (
                          <details className="text-xs text-muted-foreground">
                            <summary className="cursor-pointer">
                              资料编号与版本记录
                            </summary>
                            <p className="mt-2 break-all">
                              {entity.id} · 当前 v{currentOf(entity)?.revision}{" "}
                              · 批准版本 {entity.approvedRevisionId || "暂无"}
                            </p>
                          </details>
                        )}
                      </CardContent>
                    </Card>
                  </div>
                ) : (
                  <>
                    <Card>
                      <CardHeader>
                        <CardTitle>添加参考图片</CardTitle>
                      </CardHeader>
                      <CardContent className="grid gap-5 md:grid-cols-2">
                        <div className="space-y-3">
                          <Choice
                            label="固定图片素材库"
                            value={source}
                            options={[
                              ["", "选择源图片"],
                              ...library.map((s: Any): [string, string] => [
                                s.sourceId,
                                s.title,
                              ]),
                            ]}
                            onChange={setSource}
                          />
                          <Button
                            variant="outline"
                            disabled={busy || !source}
                            onClick={() =>
                              void run(async () => {
                                await post(
                                  `${path("projects", ctx.project.id)}/image-imports`,
                                  { sourceId: source },
                                );
                                await ctx.refresh();
                                ctx.notify("图片已导入并归档。", "success");
                              })
                            }
                          >
                            <ImagePlus className="size-4" />
                            导入所选图片
                          </Button>
                        </div>
                        <div className="space-y-3">
                          <Label htmlFor="production-upload">
                            上传图片 · PNG / JPEG / WebP · 最大 8 MiB
                          </Label>
                          <Input
                            id="production-upload"
                            type="file"
                            accept="image/png,image/jpeg,image/webp"
                            onChange={(e) =>
                              setFile(e.target.files?.[0] || null)
                            }
                          />
                          <Button
                            variant="outline"
                            disabled={busy || !file}
                            onClick={() =>
                              void run(async () => {
                                await uploadImage(ctx.project.id, file);
                                await ctx.refresh();
                                setFile(null);
                                ctx.notify("图片已校验并加入项目。", "success");
                              })
                            }
                          >
                            <Upload className="size-4" />
                            上传图片
                          </Button>
                        </div>
                      </CardContent>
                    </Card>
                    <div className="grid grid-cols-2 gap-4 lg:grid-cols-3">
                      {ctx.assets
                        .filter(
                          (a: Any) =>
                            a.kind === "image" ||
                            a.contentType?.startsWith("image/"),
                        )
                        .map((a: Any) => (
                          <Card key={a.id} className="overflow-hidden">
                            <Picture
                              ctx={ctx}
                              id={a.id}
                              className="aspect-video w-full rounded-none"
                              alt={nameOf(a)}
                            />
                            <CardContent className="space-y-2 pt-4">
                              <p className="truncate text-sm font-medium">
                                {nameOf(a)}
                              </p>
                              <p className="text-xs text-muted-foreground">
                                {a.width} × {a.height}
                              </p>
                              <details className="text-xs text-muted-foreground">
                                <summary className="cursor-pointer">
                                  素材编号
                                </summary>
                                <p className="break-all pt-2">{a.id}</p>
                              </details>
                            </CardContent>
                          </Card>
                        ))}
                    </div>
                  </>
                )}
              </TabsContent>
            </Tabs>
          </>
        )}
        {mode === "shots" &&
          (!ctx.episode ? (
            <Alert>
              <AlertDescription>
                先在概览创建或选择分集，再开始镜头制作。
              </AlertDescription>
            </Alert>
          ) : (
            <>
              <div className="flex flex-wrap items-end gap-3">
                <div className="min-w-56 flex-1">
                  <Choice
                    label="制作镜头"
                    value={shotId}
                    options={[
                      ["", "选择镜头"],
                      ...ctx.shots.map((s: Any): [string, string] => [
                        s.id,
                        `${s.title}${s.needsReview ? " · 需复核" : ""}`,
                      ]),
                    ]}
                    onChange={(v) =>
                      protect(shotHasUnsaved, () => void run(() => loadShot(v)))
                    }
                  />
                </div>
                <Button variant="outline" onClick={() => setShowNewShot(true)}>
                  <Plus className="size-4" />
                  新建镜头
                </Button>
              </div>
              {shotExternalUpdate && (
                <Alert>
                  <AlertDescription>
                    <p>
                      镜头或关联资料已有更新，当前未保存的输入已保留。重新读取前会请你确认是否放弃这些修改。
                    </p>
                    <Button
                      className="mt-3"
                      size="sm"
                      variant="outline"
                      disabled={busy}
                      onClick={() =>
                        protect(
                          shotHasUnsaved,
                          () =>
                            void run(async () => {
                              await loadShot(shotId);
                              ctx.notify("已重新读取最新镜头详情。", "success");
                            }),
                        )
                      }
                    >
                      重新读取镜头详情
                    </Button>
                  </AlertDescription>
                </Alert>
              )}
              {production ? (
                <>
                  <Card>
                    <CardHeader>
                      <div className="flex flex-wrap items-center justify-between gap-2">
                        <CardTitle>{shotDraft.title || "镜头内容"}</CardTitle>
                        <Badge variant="secondary">
                          v{production.current?.revision}
                          {shotHasUnsaved ? " · 有未保存修改" : " · 已保存"}
                        </Badge>
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-5">
                      <Field
                        label="镜头名称"
                        value={shotDraft.title}
                        onChange={(v) => editShot("title", v)}
                      />
                      {renderDependency("character", "characterRevisionIds")}
                      <Choice
                        label="场景批准版本"
                        value={shotDraft.locationRevisionId || ""}
                        options={[
                          ["", "无场景依赖"],
                          ...deps(
                            "location",
                            [shotDraft.locationRevisionId].filter(Boolean),
                          ).map((r: Any): [string, string] => [
                            r.id,
                            `${r.name} · v${r.revision} · ${r.approved ? "批准" : r.hasApprovedVersion ? "旧依赖" : "参考形象尚未批准"}`,
                          ]),
                        ]}
                        onChange={(v) =>
                          editShot("locationRevisionId", v || null)
                        }
                      />
                      {shotDraft.locationRevisionId && (
                        <Picture
                          ctx={ctx}
                          id={
                            deps("location", [
                              shotDraft.locationRevisionId,
                            ]).find(
                              (r: Any) => r.id === shotDraft.locationRevisionId,
                            )?.referenceAssetId
                          }
                          className="max-h-48 w-full"
                        />
                      )}
                      <Field
                        label="视频生成提示词"
                        multiline
                        value={shotDraft.prompt}
                        onChange={(v) => editShot("prompt", v)}
                      />
                      <Card>
                        <CardHeader>
                          <CardTitle className="text-base">
                            视频生成设置
                          </CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-4">
                          <div className="grid gap-4 sm:grid-cols-2">
                            <div className="space-y-2">
                              <Label>生成模型</Label>
                              <div className="rounded-md border bg-muted/30 px-3 py-2 text-sm font-medium">H3 Ring4 · 自适应多模态</div>
                              <p className="text-xs text-muted-foreground">文字、主参考图、动作参考图、参考视频、参考音频均可选。</p>
                            </div>
                            <div className="space-y-2">
                              <Label htmlFor="shot-generation-duration">请求生成时长（秒）</Label>
                              <Input
                                id="shot-generation-duration"
                                aria-label="请求生成时长"
                                aria-describedby="shot-generation-duration-help"
                                type="number"
                                inputMode="numeric"
                                min={4}
                                max={15}
                                step={1}
                                value={shotGeneration.requestDurationSeconds}
                                onChange={(event) => {
                                  const seconds = Number(event.target.value);
                                  if (Number.isInteger(seconds) && seconds >= 4 && seconds <= 15) {
                                    editShot("generation", {
                                      ...ring4Generation,
                                      requestDurationSeconds: seconds,
                                    });
                                  }
                                }}
                              />
                              <p id="shot-generation-duration-help" className="text-xs text-muted-foreground">
                                可输入 4–15 秒整数。
                              </p>
                            </div>
                          </div>
                          <p className="text-sm text-muted-foreground">
                            请求生成 {shotGeneration.requestDurationSeconds}{" "}
                            秒原片。参考音频仍须严格 4 秒。系统会依据本镜头是否带参考素材自动选择内部路线。
                          </p>
                          <p className="text-sm text-muted-foreground">4–15 秒提交合同已开放；15 秒真实成片已跑通，但生成耗时会明显长于 4 秒。</p>
                        </CardContent>
                      </Card>
                      <Card>
                        <CardHeader>
                          <CardTitle className="text-base">H3 参考素材 · 全部可选</CardTitle>
                        </CardHeader>
                        <CardContent className="space-y-5">
                          <H3ReferenceNodes
                            nodes={nodesFromSnapshot(shotDraft)}
                            customRoles={Array.isArray(shotDraft.customReferenceRoles) ? shotDraft.customReferenceRoles : []}
                            assets={ctx.assets.map((asset: Any) => ({
                              id: asset.id,
                              name: nameOf(asset),
                              kind: (asset.kind === "video" || String(asset.contentType || "").startsWith("video/") ? "video" : asset.kind === "audio" || String(asset.contentType || "").startsWith("audio/") || asset.role === "h3_reference" ? "audio" : "image") as MediaKind,
                            }))}
                            instruction={shotDraft.prompt || ""}
                            previewUrl={(assetId) => mediaUrl(ctx.project.id, assetId)}
                            onChange={(nodes: ReferenceNode[]) => { setShotDraft((current: Any) => ({ ...current, referenceNodes: nodes })); setShotDirty(true); }}
                            onCreateRole={(role: CustomRole) => { setShotDraft((current: Any) => ({ ...current, customReferenceRoles: [...(current.customReferenceRoles || []), role] })); setShotDirty(true); }}
                            onUpload={async (file, media) => {
                              const result = media === "video" ? await upload(ctx.project.id, file) : media === "audio" ? await uploadReferenceAudio(ctx.project.id, file) : await uploadImage(ctx.project.id, file);
                              if (!result.asset?.id) throw new Error("上传回执缺少素材编号。");
                              await ctx.refresh();
                              return { id: result.asset.id, name: result.asset.filename || file.name };
                            }}
                          />
                        </CardContent>
                      </Card>
                      <details className="rounded-lg border p-4">
                        <summary className="cursor-pointer text-sm font-medium">
                          服装与道具关联 · 可选
                        </summary>
                        <div className="mt-4 space-y-5">
                          {renderDependency("costume", "costumeRevisionIds")}
                          {renderDependency("prop", "propRevisionIds")}
                        </div>
                      </details>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          disabled={busy || !shotDirty}
                          onClick={saveShot}
                        >
                          <Check className="size-4" />
                          保存镜头新修订
                        </Button>
                        <Button
                          disabled={busy || shotDirty}
                          variant="outline"
                          onClick={() =>
                            makeImage(`shot:${production.currentRevisionId}`)
                          }
                        >
                          <Sparkles className="size-4" />
                          制作镜头参考图
                        </Button>
                        <Button
                          disabled={busy || shotDirty}
                          variant="ghost"
                          onClick={() => ctx.openCanvas(shotId)}
                        >
                          <Clapperboard className="size-4" />在 ComfyUI 画布编辑
                        </Button>
                      </div>
                    </CardContent>
                  </Card>
                  <Card>
                    <CardHeader>
                      <CardTitle>确认镜头参考画面</CardTitle>
                    </CardHeader>
                    <CardContent className="space-y-4">
                      {shotDraft.approvedReferenceAssetId && (
                        <>
                          <Picture
                            ctx={ctx}
                            id={shotDraft.approvedReferenceAssetId}
                            className="max-h-80 w-full"
                          />
                          <Badge>
                            <Check className="mr-1 size-3" />
                            本镜头已选画面 · 可更换画面
                          </Badge>
                        </>
                      )}
                      <AssetPicker
                        ctx={ctx}
                        label="选择参考画面"
                        value={selectedReference}
                        onChange={setSelectedReference}
                      />
                      <p className="text-xs text-muted-foreground">确认表示后续生成可以使用这张图作为镜头参考；不会自动生成视频，也不会加入成片时间线。</p>
                      <div className="flex flex-wrap gap-2">
                        <Button
                          variant="secondary"
                          disabled={
                            busy ||
                            shotDirty ||
                            !selectedReference ||
                            hasScriptInvalidation
                          }
                          onClick={() =>
                            void run(async () => {
                              await post(
                                `${path("shots", shotId)}/reference-selection`,
                                {
                                  expectedRevision: production.revision,
                                  assetId: selectedReference,
                                },
                              );
                              await ctx.refresh();
                              await loadShot(shotId, { review: true });
                              ctx.notify(
                                "参考画面已确认，下一步生成视频。",
                                "success",
                              );
                            })
                          }
                        >
                          确认参考画面
                        </Button>
                        {production.currentRevisionId && (
                          <Button
                            variant="outline"
                            disabled={shotDirty || hasScriptInvalidation}
                            onClick={() => ctx.navigate("video", { shotId, prompt: videoDraftPrompt(shotDraft, ctx.entities) })}
                          >
                            下一步：H3 Ring4 生成
                            <ArrowRight className="size-4" />
                          </Button>
                        )}
                      </div>
                    </CardContent>
                  </Card>
                  {(production.invalidations?.length > 0 ||
                    reviewIds.length > 0 ||
                    reviewReason.length > 0) && (
                    <Card>
                      <CardHeader>
                        <CardTitle>
                          {unresolvedReviewIds.size
                            ? "关联资料有更新，需要复核"
                            : reviewIds.length || reviewReason
                              ? "保留的复核草稿"
                              : "依赖复核记录"}
                        </CardTitle>
                      </CardHeader>
                      <CardContent className="space-y-4">
                        <p className="text-sm text-muted-foreground">
                          建议在上方关联中选择新的批准版本并保存。如果保留旧版本，请逐项勾选并写明原因。
                        </p>
                        {hasScriptInvalidation && (
                          <div className="space-y-2 rounded-lg border border-amber-500/30 bg-amber-500/5 p-3 text-sm">
                            <p>
                              批准的剧本已更新。参考画面批准和视频生成已暂停；请先核对原有对白及分镜。保留旧绑定需要单独说明原因，不会自动改写台词。
                            </p>
                            <Button
                              variant="outline"
                              onClick={() => ctx.navigate("story")}
                            >
                              查看故事与剧本
                            </Button>
                          </div>
                        )}
                        {!unresolvedReviewIds.size &&
                          (reviewIds.length > 0 || reviewReason.length > 0) && (
                            <p
                              role="status"
                              className="rounded-lg bg-muted/50 p-3 text-sm text-muted-foreground"
                            >
                              当前镜头版本没有待复核项。之前填写的原因和勾选已保留供你查看，不会提交到新版本；确认不再需要后可清除复核草稿。
                            </p>
                          )}
                        {reviewIds.length > 0 && !reviewSelectionValid && (
                          <p className="text-sm text-muted-foreground">
                            之前勾选的 {reviewIds.length}{" "}
                            项包含当前版本已不存在或已完成的复核项，暂不能提交。请清除旧草稿后重新选择需要复核的项目。
                          </p>
                        )}
                        {(production.invalidations || []).map((i: Any) => {
                          const resolved = Boolean(
                            i.resolved ||
                            i.reviewedAt ||
                            i.resolvedAt ||
                            i.status === "RESOLVED",
                          );
                          const e = ctx.entities.find(
                            (e: Any) => e.id === i.entityId,
                          );
                          return (
                            <label
                              key={i.id}
                              className="flex items-start gap-3 rounded-lg border p-3"
                            >
                              <Checkbox
                                checked={resolved || reviewIds.includes(i.id)}
                                disabled={resolved}
                                onCheckedChange={(v) =>
                                  setReviewIds((ids) =>
                                    v
                                      ? [...ids, i.id]
                                      : ids.filter((id) => id !== i.id),
                                  )
                                }
                              />
                              <span className="min-w-0 text-sm">
                                {i.kind === "script"
                                  ? "剧本与对白"
                                  : nameOf(currentOf(e))}{" "}
                                · {i.reason || i.message || i.id}
                                {resolved ? " · 已复核" : ""}
                                <span className="mt-1 block break-all text-xs text-muted-foreground">
                                  原版本 {i.oldRevisionId || "未知"} →
                                  新批准版本 {i.newRevisionId || "未知"}
                                </span>
                              </span>
                            </label>
                          );
                        })}
                        <Field
                          label="接受旧依赖的原因"
                          multiline
                          value={reviewReason}
                          onChange={setReviewReason}
                        />
                        <p className="-mt-2 text-xs text-muted-foreground">选填，最多 2000 字。</p>
                        <div className="flex flex-wrap gap-2">
                          <Button
                            variant="outline"
                            disabled={
                              busy ||
                              shotDirty ||
                              !reviewSelectionValid ||
                              !entityReviewIds.length
                            }
                            onClick={() => void submitReview(false)}
                          >
                            确认接受所选旧依赖
                          </Button>
                          {(production.invalidations || []).some(
                            (i: Any) => i.kind === "script",
                          ) && (
                            <Button
                              variant="outline"
                              disabled={
                                busy ||
                                shotDirty ||
                                !reviewSelectionValid ||
                                !scriptReviewIds.length
                              }
                              onClick={() => void submitReview(true)}
                            >
                              确认保留所选旧剧本对白
                            </Button>
                          )}
                        </div>
                        {(reviewIds.length > 0 || reviewReason.length > 0) && (
                          <Button
                            variant="ghost"
                            disabled={busy}
                            onClick={() => {
                              setReviewIds([]);
                              setReviewReason("");
                            }}
                          >
                            清除复核草稿
                          </Button>
                        )}
                      </CardContent>
                    </Card>
                  )}
                  <details className="rounded-lg border p-4 text-sm">
                    <summary className="cursor-pointer">
                      内容历史 · {production.history?.length || 0} 个版本
                    </summary>
                    <div className="mt-3 space-y-2 text-muted-foreground">
                      {production.history?.map((h: Any) => (
                        <p key={h.id} className="break-all">
                          v{h.revision} · {h.snapshot?.title} · {h.id}
                          {h.approved ? " · 已批准" : ""}
                        </p>
                      ))}
                    </div>
                  </details>
                </>
              ) : (
                <div className="rounded-xl border border-dashed py-16 text-center">
                  <Clapperboard className="mx-auto mb-3 size-8 text-muted-foreground" />
                  <p className="text-sm text-muted-foreground">
                    选择镜头，或新建第一个镜头。
                  </p>
                </div>
              )}
            </>
          ))}
        {mode === "images" && (
          <>
            <Card>
              <CardHeader>
                <CardTitle>01 · 选择目标与制作方式</CardTitle>
              </CardHeader>
              <CardContent className="space-y-5">
                <Choice
                  label="制作目标（固定当前内容版本）"
                  value={target}
                  options={[
                    ["", "选择人物、场景或镜头"],
                    ...ctx.entities.map((e: Any): [string, string] => [
                      `entity:${e.currentRevisionId}`,
                      `${KINDS[e.kind]} · ${nameOf(currentOf(e))} · v${currentOf(e)?.revision}`,
                    ]),
                    ...ctx.shots
                      .filter((s: Any) => s.currentRevisionId)
                      .map((s: Any): [string, string] => [
                        `shot:${s.currentRevisionId}`,
                        `镜头 · ${s.title}`,
                      ]),
                  ]}
                  onChange={(v) => {
                    setTarget(v);
                    const [kind, rid] = v.split(":");
                    if (kind === "entity") {
                      const entity = ctx.entities.find((row: Any) => row.currentRevisionId === rid);
                      const roles = ROLES[entity?.kind] || [];
                      if (!roles.some(([role]) => role === imageForm.referenceRole)) {
                        editImage("referenceRole", roles[0]?.[0] || "");
                      }
                    }
                  }}
                />
                {target.startsWith("entity:") && targetOwner && ROLES[targetOwner.kind] && (
                  <Choice
                    label="参考用途"
                    value={imageForm.referenceRole || ROLES[targetOwner.kind][0][0]}
                    options={ROLES[targetOwner.kind]}
                    onChange={(v) => editImage("referenceRole", v)}
                  />
                )}
                {target && <div className="rounded-lg border bg-muted/20 p-3 text-sm"><p className="font-medium">当前目标：{targetOwner ? nameOf(currentOf(targetOwner) || targetOwner) : '旧内容版本，请选择当前目标'}</p><p className="text-xs text-muted-foreground">参考图片确认后才能用于视频；制作完成、质量通过和已采用会分别保留。</p>{currentTarget && currentTarget!==target && <Button size="sm" className="mt-2" variant="outline" onClick={()=>protect(imageDirty,()=>setTarget(currentTarget))}>同步到当前版本</Button>}</div>}
                <Choice
                  label="图片工具"
                  value={tool}
                  options={TOOLS}
                  onChange={(v) => {
                    imageFieldEdits.current.tool = (imageFieldEdits.current.tool || 0) + 1;
                    setTool(v);
                    setImageDirty(true);
                  }}
                />
                <details className="rounded-lg border p-3"><summary className="cursor-pointer text-sm font-medium">模型能力与版本说明</summary><div className="mt-3 space-y-2 text-xs text-muted-foreground"><p>Z-Image Turbo：文生图；Qwen Image Edit 2511：1–3 张参考编辑，保留原有编码器与精度选择。</p><p>LLaDA Image Turbo：INT8 主模型 / Q4_K_M 编码器，4 步；文生图与单张完整画面编辑。已实测双候选；复杂双人动作仍需挑选。</p><p>FLUX.2 dev：标准与 silveroxides 编码器为两个独立选项；当前工作台只接入文生图。</p><p>LLaDA Image Base：下载暂停，未就绪；独立于 Turbo，本页不会启动下载或冒用 Turbo。</p><p>输入参考数量和输出候选数量是两回事。当前 LLaDA 适配器接收一张画面；多参考板的效果还需单独测试。</p></div></details>
                {inputInfo.inputs.length > 0 && <div className="rounded-lg border p-3 text-sm space-y-2">
                  <p className="font-medium">本次关联参考 · {inputInfo.inputs.length} 张</p>
                  {tool === "generate_image" && <p className="text-muted-foreground">当前为文字生图，不会读取这些参考图片。要保持人物形象，请选择“参考图编辑 · 选择模型”。</p>}
                  {inputInfo.inputs.map((r, index) => <p key={r.assetId} className="text-muted-foreground">{index + 1}. {r.name}{r.revision !== "" ? ` · v${r.revision}` : ""}</p>)}
                </div>}
                {tool === "edit_image" && imageForm.editModel === "llada-image-turbo" &&
                  (inputInfo.inputs.length > 1 || imageForm.refs.filter(Boolean).length > 1) &&
                  <Alert variant="destructive"><AlertDescription>当前包含多张参考，LLaDA 只支持一张完整画面。请选择 Qwen 多图组合；已有的已确认身份参考不能省略。<Button className="mt-2" size="sm" variant="outline" disabled={busy} onClick={()=>editImage("editModel","qwen-image-edit-2511")}>改用 Qwen 组合当前参考</Button></AlertDescription></Alert>}
                {(inputInfo.issues.length > 0 || (tool === "edit_image" && inputInfo.inputs.length > 3)) && <Alert variant="destructive"><AlertDescription>
                  {inputInfo.issues.map(issue => <p key={issue}>{issue}</p>)}
                  {tool === "edit_image" && imageForm.editModel !== "flux2-dev" && inputInfo.inputs.length > 3 && <p>当前镜头需要 {inputInfo.inputs.length} 张参考，Qwen 最多接受 3 张，尚未截取或省略任何人物。请返回镜头调整关联，或先确认一张包含全部人物与场景的完整合成参考画面，再来制作。也可以切换“透明图层合成”编排全部参考图层，或改用 FLUX.2 dev（最多 8 张）。</p>}
                  <Button variant="outline" size="sm" className="mt-2" onClick={() => ctx.navigate("shots", { shotId: ctx.shots.find(s => `shot:${s.currentRevisionId}` === target)?.id || "" })}>返回镜头核查参考</Button>
                </AlertDescription></Alert>}
                <p className="text-xs leading-relaxed text-muted-foreground">
                  制作绑定所选内容版本。已带入该版本的画面说明与参考图，你可以继续修改。确认图片后才能作为后续视频的参考。
                </p>
              </CardContent>
            </Card>
            <Card>
              <CardHeader>
                <CardTitle>
                  02 ·{" "}
                  {tool === "compose_image_layers"
                    ? "编排图层"
                    : "描述你想看到的画面"}
                </CardTitle>
              </CardHeader>
              <CardContent className="space-y-5">
                {tool !== "compose_image_layers" ? (
                  <>
                    <Field
                      label="中文说明（给你看，不发给模型）"
                      multiline
                      value={imageForm.promptZh || ""}
                      onChange={(v) => editImage("promptZh", v)}
                    />
                    <Field
                      label={
                        tool === "generate_image" ? "英文提示词（实际发给模型）" : "英文编辑要求（实际发给模型）"
                      }
                      multiline
                      value={imageForm.prompt}
                      onChange={(v) => editImage("prompt", v)}
                    />
                    <p className="text-xs text-muted-foreground">生成时只发送英文这一份。中文说明留在这页，方便你核对服装和构图。</p>
                    {tool === "generate_image" ? (
                      <>
                        <Choice label="生图模型" value={imageForm.model || "z-image-turbo"}
                          options={[["z-image-turbo", "Z-Image Turbo"], ["llada-image-turbo", "LLaDA Image Turbo · 4 步"],
                            ["flux2-dev", "FLUX.2 dev · 标准编码器"], ["flux2-dev-abliterated", "FLUX.2 dev · silveroxides 去限制编码器"]]}
                          onChange={v => {editImage("model", v); const size=fitImageSize(imageForm.width,imageForm.height,v);editImage("width",size.width);editImage("height",size.height)}} />
                        {(imageForm.model || "z-image-turbo") === "flux2-dev" && <div className="grid gap-4 lg:grid-cols-4">
                          {[0, 1, 2, 3, 4, 5, 6, 7].map((i) => (
                            <AssetPicker key={i} ctx={ctx} label={`参考图 ${i + 1}${i === 0 ? " · 可选" : ""}`} value={imageForm.refs[i] || ""}
                              onChange={(v) => { const next = [...imageForm.refs]; while (next.length <= i) next.push(""); next[i] = v; editImage("refs", next); }} />
                          ))}
                        </div>}
                        <ImageSizePicker width={imageForm.width} height={imageForm.height} model={imageForm.model || "z-image-turbo"} project={ctx.project}
                          onChange={(w,h)=>{editImage("width",w);editImage("height",h)}} />
                        <Field label="种子" type="number" value={imageForm.seed} onChange={v=>editImage("seed",v)} />
                      </>
                    ) : (
                      <>
                        <Choice label="参考图编辑模型" value={imageForm.editModel || "qwen-image-edit-2511"}
                          options={[["qwen-image-edit-2511", "Qwen Image Edit 2511 · 多图组合"], ["flux2-dev", "FLUX.2 dev · 参考图编辑 · 最多 8 张"], ["llada-image-turbo", "LLaDA Image Turbo · 单图编辑 · 4 步"]]}
                          onChange={v => {editImage("editModel", v); if(v === "llada-image-turbo"){const size=fitImageSize(imageForm.width,imageForm.height,v);editImage("width",size.width);editImage("height",size.height)}}} />
                        {imageForm.editModel === "flux2-dev" ? <Alert><AlertDescription>
                          FLUX.2 dev 用参考潜变量改图。第 1 张决定输出尺寸，最多再加 7 张参考。不使用 Qwen 的编码器、精度和步数。
                        </AlertDescription></Alert> : imageForm.editModel === "llada-image-turbo" ? <Alert><AlertDescription>
                          基于一张完整画面修改人物动作、构图或场景，原生参考图编辑，固定 4 步。仅保留一个输入图；不会自动丢弃已有参考。
                          若镜头关联多张已确认人物或场景图，请先用 Qwen 组合并确认完整镜头参考，再用 LLaDA 继续编辑。输出画幅建议与参考图一致。
                        </AlertDescription></Alert> : <Alert>
                          <AlertDescription>
                            输入图 1 决定画面比例。合成双人镜头时：第 1
                            张选横屏场景，第 2、3
                            张选人物参考；在编辑要求中写清各图用途。
                          </AlertDescription>
                        </Alert>}
                        <div className="grid gap-4 lg:grid-cols-3">
                          {[...Array(imageForm.editModel === "flux2-dev" ? 8 : 3).keys()].filter(i => imageForm.editModel !== "llada-image-turbo" || i === 0 || imageForm.refs[i]).map((i) => (
                            <AssetPicker
                              key={i}
                              ctx={ctx}
                              label={imageForm.editModel === "llada-image-turbo" ? (i === 0 ? "输入图 1 · 完整参考画面" : `输入图 ${i + 1} · LLaDA 不支持，请移除或改用 Qwen`) : `输入图 ${i + 1}${i === 0 ? " · 必选，决定画幅" : " · 可选"}`}
                              value={imageForm.refs[i] || ""}
                              onChange={(v) => {
                                const next = [...imageForm.refs];
                                while (next.length <= i) next.push("");
                                next[i] = v;
                                editImage("refs", next);
                              }}
                            />
                          ))}
                        </div>
                        <div className="grid gap-4 sm:grid-cols-3">
                          <Field
                            label="种子"
                            type="number"
                            value={imageForm.seed}
                            onChange={(v) => editImage("seed", v)}
                          />
                          {imageForm.editModel === "llada-image-turbo" || imageForm.editModel === "flux2-dev" ? null : <Field
                            label="采样步数（20–40）"
                            type="number"
                            value={imageForm.steps}
                            onChange={(v) => editImage("steps", v)}
                          />}

                        </div>
                        {imageForm.editModel === "llada-image-turbo" && <ImageSizePicker width={imageForm.width} height={imageForm.height} model="llada-image-turbo" project={ctx.project}
                          onChange={(w,h)=>{editImage("width",w);editImage("height",h)}} />}
                        {imageForm.editModel !== "llada-image-turbo" && imageForm.editModel !== "flux2-dev" && <details className="rounded-lg border p-4">
                          <summary className="cursor-pointer text-sm font-medium">
                            模型高级设置
                          </summary>
                          <div className="mt-4 grid gap-4 sm:grid-cols-2">
                            <Choice
                              label="文本编码器"
                              value={imageForm.encoder}
                              options={[
                                ["standard", "标准"],
                                ["huihui-abliterated-bf16", "Huihui BF16"],
                              ]}
                              onChange={(v) => editImage("encoder", v)}
                            />
                            <Choice
                              label="模型精度"
                              value={imageForm.precision}
                              options={[
                                ["fp8", "FP8"],
                                ["bf16", "BF16"],
                              ]}
                              onChange={(v) => editImage("precision", v)}
                            />
                          </div>
                        </details>}
                      </>
                    )}
                  </>
                ) : (
                  <>
                    <Alert>
                      <AlertDescription>
                        图层须是含真实透明区域的 PNG / WebP。先在 DGX
                        创作面板抠图或导入透明素材；背景可以不透明。
                      </AlertDescription>
                    </Alert>
                    <AssetPicker
                      ctx={ctx}
                      label="背景图片"
                      value={imageForm.background}
                      onChange={(v) => editImage("background", v)}
                    />
                    {imageForm.layers.map((layer: Any, i: number) => {
                      const edit = (key: string, value: any) =>
                        editImage(
                          "layers",
                          imageForm.layers.map((l: Any, n: number) =>
                            n === i ? { ...l, [key]: value } : l,
                          ),
                        );
                      return (
                        <div
                          key={i}
                          className="space-y-4 rounded-xl border p-4"
                        >
                          <div className="flex items-center justify-between">
                            <span className="text-sm font-medium">
                              图层 {i + 1}
                            </span>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() =>
                                editImage(
                                  "layers",
                                  imageForm.layers.filter(
                                    (_: Any, n: number) => n !== i,
                                  ),
                                )
                              }
                            >
                              <Trash2 className="size-4" />
                              删除图层
                            </Button>
                          </div>
                          <Field
                            label={`图层 ${i + 1} 名称`}
                            value={layer.name}
                            onChange={(v) => edit("name", v)}
                          />
                          <AssetPicker
                            ctx={ctx}
                            label={`图层 ${i + 1} 图片`}
                            value={layer.asset_id}
                            onChange={(v) => edit("asset_id", v)}
                          />
                          <div className="grid gap-3 sm:grid-cols-3">
                            {[
                              ["x", "水平偏移"],
                              ["y", "垂直偏移"],
                              ["width", "宽度"],
                              ["height", "高度"],
                              ["opacity", "不透明度 %"],
                            ].map(([key, label]) => (
                              <Field
                                key={key}
                                label={`图层 ${i + 1} ${label}`}
                                type="number"
                                value={layer[key]}
                                onChange={(v) => edit(key, v)}
                              />
                            ))}
                            <label className="flex items-center gap-2 pt-5 text-sm">
                              <Checkbox
                                checked={layer.flip_x}
                                onCheckedChange={(v) =>
                                  edit("flip_x", v === true)
                                }
                              />
                              水平翻转
                            </label>
                          </div>
                        </div>
                      );
                    })}
                    <Button
                      variant="outline"
                      disabled={imageForm.layers.length >= 16}
                      onClick={() =>
                        editImage("layers", [
                          ...imageForm.layers,
                          blankLayer(imageForm.layers.length + 1),
                        ])
                      }
                    >
                      <Layers className="size-4" />
                      增加图层 · {imageForm.layers.length}/16
                    </Button>
                  </>
                )}
                {tool === "compose_image_layers" ? <p className="text-sm text-muted-foreground">确定性图层合成 · 1 张结果，相同参数不会生成随机候选。</p> : <div className="space-y-3 rounded-lg border p-4">
                  <div className="grid gap-4 sm:grid-cols-2">
                    <Field label="候选数量" type="number" value={imageForm.count ?? 1} onChange={v => editImage("count", v)} />
                    <Field label="每批并行数量" type="number" value={imageForm.parallelism ?? 2} onChange={v => editImage("parallelism", v)} />
                  </div>
                  <p className="text-xs text-muted-foreground">总候选分批制作，每批最多 {Math.min(Number(imageForm.count || 1), Number(imageForm.parallelism || 2))} 张；实际并行以任务观测为准。种子按原候选序号逐张递增，已完成批次会先显示。并行数越大，内存需求越高；内存不足时任务可能失败，可降低并行数后重试失败候选。</p>
                </div>}
                {ctx.capabilities?.referenceGenerationEnabled === false && (
                  <Alert>
                    <AlertDescription>
                      图片制作适配器尚未启用。仍可上传图片，或导入已有 Gateway
                      任务。
                    </AlertDescription>
                  </Alert>
                )}
                <div className="space-y-3 rounded-xl bg-muted/50 p-4">
                  <p className="text-sm">本次调用：{imageModelName(tool,{model:tool==='edit_image' ? imageForm.editModel : imageForm.model})} · {(tool==='generate_image' ? (imageForm.model === 'flux2-dev' ? imageForm.refs.filter(Boolean).length : 0) : imageForm.refs.filter(Boolean).length)} 张输入参考 · {imageForm.count || 1} 个输出候选</p>
                  <Button
                    disabled={
                      busy || imagePreparing || inputInfo.issues.length > 0 ||
                      (tool === "edit_image" && (inputInfo.inputs.length > (imageForm.editModel === "llada-image-turbo" ? 1 : imageForm.editModel === "flux2-dev" ? 8 : 3) || (imageForm.editModel === "llada-image-turbo" && imageForm.refs.filter(Boolean).length !== 1) || (imageForm.editModel === "flux2-dev" && imageForm.refs.filter(Boolean).length > 8))) ||
                      uncertain || !!trackedJob ||
                      !target ||
                      ctx.capabilities?.referenceGenerationEnabled === false
                    }
                    onClick={submitImage}
                  >
                    {busy ? (
                      <LoaderCircle className="size-4 animate-spin" />
                    ) : (
                      <Sparkles className="size-4" />
                    )}
                    {busy
                      ? "正在提交…"
                      : uncertain
                        ? "结果待确认 · 请先核查"
                        : imagePreparing ? "正在带入镜头资料…" : trackedJob ? "本组已提交" : "提交图片制作"}
                  </Button>
                  <p
                    role="status"
                    aria-live="polite"
                    className="text-sm text-muted-foreground"
                  >
                    {receipt ||
                      "提交后会立即显示回执。完成后，请到下方挑选图片。"}
                  </p>
                  {viewedJobId && trackedJob && viewedJobId !== trackedJob && <p className="text-xs text-muted-foreground">
                    上方保留当前草稿最近一次提交的回执。你从任务中心打开的是任务 {viewedJobId}，其候选已在下方单独标记。
                  </p>}
                  {trackedJob && (
                    <div className="flex flex-wrap items-center gap-3">
                      <Badge
                        variant={
                          jobs.find((j) => j.id === trackedJob)?.state ===
                          "FAILED"
                            ? "destructive"
                            : "secondary"
                        }
                      >
                        {STATES[jobs.find((j) => j.id === trackedJob)?.state] ||
                          jobs.find((j) => j.id === trackedJob)?.state || "已取得任务回执"}
                      </Badge>
                      <Button variant="outline" size="sm" onClick={() => ctx.navigate("tasks", { kind: "image", jobId: trackedJob })}>查看原任务</Button>
                      {!uncertain && <Button variant="secondary" size="sm" disabled={busy} onClick={() => {
                        setTrackedJob("");
                        setReceipt("已准备新一组。可调整要求或种子，再点击提交图片制作；原任务会继续保留在任务中心。");
                        setImageDirty(true);
                      }}>再生成一组</Button>}
                      <Button
                        variant="link"
                        size="sm"
                        onClick={() =>
                          document
                            .getElementById(`production-job-${trackedJob}`)
                            ?.scrollIntoView({
                              block: "start",
                              behavior: "smooth",
                            })
                        }
                      >
                        查看任务与图片
                        <ArrowRight className="size-4" />
                      </Button>
                    </div>
                  )}
                  {uncertain && pendingImage && (
                    <div className="space-y-2 rounded-lg border border-amber-500/30 p-3">
                      <p className="break-all text-xs text-muted-foreground">
                        原请求 {pendingImage.key} · 固定目标{" "}
                        {pendingImage.target}
                      </p>
                      <p className="text-xs text-muted-foreground">
                        按原请求键恢复：已接收的任务只取回回执；首次未送达时会提交这份固定输入。不会自动发送。
                      </p>
                      <Button
                        variant="outline"
                        disabled={busy}
                        onClick={() =>
                          void run(() => sendFrozenImage(pendingImage))
                        }
                      >
                        按原请求键确认提交结果
                      </Button>
                    </div>
                  )}
                  {uncertain && (
                    <p className="text-xs text-muted-foreground">
                      为避免重复生成，本次提交已锁定。核查下方原任务；没有任务编号时，保留请求键交由管理员核对。
                    </p>
                  )}
                </div>
                <details className="rounded-lg border p-4">
                  <summary className="cursor-pointer text-sm font-medium">
                    导入已有 Gateway 图片任务
                  </summary>
                  <div className="mt-4 space-y-3">
                    <p className="text-xs text-muted-foreground">
                      绑定上方选定目标与工具，只查询和归档已有任务，不重新生成。
                    </p>
                    <Field
                      label="已有 Gateway job_id"
                      value={gatewayId}
                      onChange={setGatewayId}
                    />
                    <Button
                      variant="outline"
                      disabled={busy || !target || !gatewayId.trim()}
                      onClick={() =>
                        void run(async () => {
                          const [targetType, targetRevisionId] =
                            target.split(":");
                          const result = await post(
                            `${path("projects", ctx.project.id)}/reference-imports`,
                            {
                              targetType,
                              targetRevisionId,
                              gatewayJobId: gatewayId.trim(),
                              tool,
                            },
                          );
                          if (result.job) {
                            setJobs((v) => [
                              result.job,
                              ...v.filter((j) => j.id !== result.job.id),
                            ]);
                            setTrackedJob(result.job.id);
                          }
                          ctx.notify(
                            "已登记原任务，后续只查询和归档原结果。",
                            "success",
                          );
                        })
                      }
                    >
                      导入并核查已有任务
                    </Button>
                  </div>
                </details>
              </CardContent>
            </Card>
            <div className="space-y-4">
              <div className="flex items-center justify-between">
                <h3 className="text-lg font-semibold">
                  03 · 制作任务与候选图片
                </h3>
                <Badge variant="outline">显示 {visibleImageJobs.length} / {jobs.length} 个任务</Badge>
              </div>
              {viewedJobId && !jobs.some(job => job.id === viewedJobId) && <p role="status" className="text-sm text-muted-foreground">正在定位任务 {viewedJobId}。列表加载后会自动定位候选；若未显示，请刷新任务列表核查。</p>}
              {jobs.length === 0 && (
                <div className="rounded-xl border border-dashed py-12 text-center text-sm text-muted-foreground">
                  还没有图片任务。提交后，进度和图片会出现在这里。
                </div>
              )}
              {target && <Button size="sm" variant="outline" onClick={()=>setShowAllImageJobs(!showAllImageJobs)}>{showAllImageJobs ? "只看当前目标的任务" : "显示全部目标的任务"}</Button>}
              {jobs.length > 0 && visibleImageJobs.length === 0 && <p className="text-sm text-muted-foreground">当前目标还没有制作任务。可提交制作，或查看其他目标的历史任务。</p>}
              {visibleImageJobs.map((job) => {
                const batch = imageBatchOf(job), chosen = compare[job.id] || [];
                const retryIntent = retryIntents[job.id], retryable = remainingImageRetryIndices(job, retryIntent, jobs), retryAttempts = knownImageRetries(job, jobs, retryIntent);
                const retryPending = !!retryIntent && !retryIntent.jobId;
                const terminal = [
                  "SUCCEEDED",
                  "FAILED",
                  "CANCELED",
                  "INTERRUPTED",
                ].includes(job.state);
                const owner =
                  job.targetType === "entity"
                    ? ctx.entities.find((e: Any) => e.id === job.targetId)
                    : ctx.shots.find((s: Any) => s.id === job.targetId);
                return (
                  <Card
                    key={job.id}
                    id={`production-job-${job.id}`}
                    role="region"
                    aria-label={`图片任务 ${job.id}`}
                    data-viewed={job.id === viewedJobId ? "true" : undefined}
                    className={`scroll-mt-4 ${job.id === viewedJobId ? "ring-2 ring-primary border-primary" : ""}`}
                  >
                    <CardHeader>
                      {job.id === viewedJobId && <p className="break-all text-sm font-medium text-primary">正在查看任务 {job.id} · {(job.candidates || []).length} 张候选</p>}
                      <div className="flex flex-wrap items-center justify-between gap-3">
                        <div>
                          <CardTitle className="text-base">
                            {job.targetType === "entity"
                              ? nameOf(currentOf(owner))
                              : nameOf(owner)}
                          </CardTitle>
                          <p className="mt-1 text-xs text-muted-foreground">
                            {job.imageBatch?.modelName || imageModelName(job.tool, job.parameters)}
                          </p>
                        </div>
                        <Badge
                          variant={
                            job.state === "FAILED" ? "destructive" : "secondary"
                          }
                        >
                          {STATES[job.state] || job.state}
                        </Badge>
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-4">
                      {isResident(job) && <p className="text-xs text-muted-foreground">{RESIDENT_MESSAGE}。</p>}
                      {job.tool === "compose_image_layers" ? <p className="text-sm text-muted-foreground">确定性合成 · 单张结果</p> : batch ? <div className="space-y-2 rounded-lg bg-muted/40 p-3">
                        <p className="text-sm">候选总数 {batch.candidateCount} · 请求每批上限 {batch.parallelLimit} 张 · {typeof batch.observedParallelism === "number" && batch.observedParallelism > 0 ? `已观测有效并行 ${batch.observedParallelism} 张` : "实际并行尚未观测"}</p>
                        <div className="flex flex-wrap gap-2">{batch.candidates.map((item: Any) => <Badge key={item.index} variant={item.state === "FAILED" ? "destructive" : "outline"}>候选 {Number(item.index) + 1} · 种子 {(item.seedText ?? item.seed) == null ? "未记录" : String(item.seedText ?? item.seed)} · {STATES[item.state] || (item.state === "UNKNOWN" ? "结果未知，请核查" : item.state)}</Badge>)}</div>
                      </div> : <p className="text-xs text-muted-foreground">{["SUCCEEDED", "FAILED", "CANCELED", "INTERRUPTED"].includes(job.state) ? "历史任务未记录实际并行信息，原候选仍可查看。" : pendingImageBatchMessage(job)}</p>}
                      {job.error && (
                        <Alert variant="destructive">
                          <AlertDescription>
                            {typeof job.error === "string"
                              ? job.error
                              : job.error.message}
                          </AlertDescription>
                        </Alert>
                      )}
                      <div className="grid gap-4 sm:grid-cols-2">
                        {(job.candidates || []).map((candidate: Any) => (
                          <div
                            key={candidate.id}
                            className="space-y-3 rounded-xl border p-3"
                          >
                            <button type="button" className="block w-full rounded-lg focus-visible:outline-2 focus-visible:outline-primary" aria-label={`放大候选 ${candidate.index == null ? (job.candidates || []).indexOf(candidate) + 1 : Number(candidate.index) + 1}`} onClick={() => setGallery({ jobId: job.id, ids: [candidate.id] })}>
                              <Picture ctx={ctx} id={candidate.assetId} className="max-h-96 min-h-32 w-full" alt="图片候选" />
                            </button>
                            <p className="text-xs text-muted-foreground">候选 {candidate.index == null ? (job.candidates || []).indexOf(candidate) + 1 : Number(candidate.index) + 1} · 种子 {(candidate.seedText ?? candidate.seed) == null ? "历史未记录" : String(candidate.seedText ?? candidate.seed)} · {candidate.state ? STATES[candidate.state] || candidate.state : "已归档"}</p>
                            <label className="flex items-center gap-2 text-sm"><Checkbox aria-label={`对比候选 ${candidate.index == null ? (job.candidates || []).indexOf(candidate) + 1 : Number(candidate.index) + 1}`} checked={chosen.includes(candidate.id)} disabled={!chosen.includes(candidate.id) && chosen.length >= 4} onCheckedChange={checked => setCompare(current => ({ ...current, [job.id]: checked ? [...chosen, candidate.id] : chosen.filter(id => id !== candidate.id) }))} />加入对比（最多 4 张）</label>
                            <p className="text-xs text-muted-foreground">
                              {candidate.stale
                                ? "来自旧版本 · 选择前需确认"
                                : "候选已归档 · 等待选择"}
                            </p>
                            <Button
                              variant="secondary"
                              disabled={busy}
                              onClick={() =>
                                protect(
                                  entityDirty || shotHasUnsaved,
                                  () => void run(() => selectCandidate(job, candidate)),
                                )
                              }
                            >
                              {candidateIsSelected(job, candidate)
                                ? "当前已选"
                                : job.targetType === "entity"
                                  ? "选为资料参考草稿"
                                  : "选用这张，准备生成视频"}
                              <ArrowRight className="size-4" />
                            </Button>
                          </div>
                        ))}
                      </div>
                      <div className="flex flex-wrap gap-2">
                        <Button size="sm" variant="secondary" disabled={chosen.length < 2} onClick={() => setGallery({ jobId: job.id, ids: chosen })}>对比已选图片（{chosen.length}/4）</Button>
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={busy}
                          onClick={() =>
                            void run(async () => {
                              const result = await post(
                                `${path("reference-jobs", job.id)}/refresh`,
                              );
                              if (result.job) {
                                setJobs((v) =>
                                  v.map((j) =>
                                    j.id === job.id ? result.job : j,
                                  ),
                                );
                                if (
                                  job.id === trackedJob &&
                                  result.job.state !== "SUBMISSION_UNKNOWN"
                                ) {
                                  setUncertain(false);
                                  clearImagePending(ctx.project.id);
                                  setPendingImage(null);
                                }
                              }
                              await ctx.refresh();
                              ctx.notify(
                                "已核查原任务，未重新生成。",
                                "success",
                              );
                            })
                          }
                        >
                          <RefreshCw className="size-3.5" />
                          核查原任务并归档
                        </Button>
                        <Button
                          size="sm"
                          variant="ghost"
                          disabled={busy || terminal}
                          onClick={() =>
                            void run(async () => {
                              const result = await post(
                                `${path("reference-jobs", job.id)}/cancel`,
                              );
                              if (result.job)
                                setJobs((v) =>
                                  v.map((j) =>
                                    j.id === job.id ? result.job : j,
                                  ),
                                );
                              ctx.notify(
                                "取消意图已提交。请继续核查至任务终态。",
                                "info",
                              );
                            })
                          }
                        >
                          取消原任务
                        </Button>
                      </div>
                      {recoverableImageJob(job) && <div className="space-y-2 rounded-lg border p-3"><p className="text-sm">图片已生成，但下载归档未完成。仅重新读取已保存图片，不重新生成。</p><Button size="sm" variant="outline" disabled={busy} onClick={() => recoverImages(job)}>恢复已生成图片</Button></div>}
                      {(retryable.length > 0 || retryIntent || retryAttempts.length > 0) && <div className="space-y-3 rounded-lg border p-3">
                        <p className="text-sm font-medium">仅重拍明确失败或取消的候选</p>
                        <p className="text-xs text-muted-foreground">成功与结果未知的候选不会重拍。保留原提示词、参考与种子，以新任务关联原结果。</p>
                        {retryable.map(index => <label key={index} className="mr-4 inline-flex items-center gap-2 text-sm"><Checkbox aria-label={`重拍候选 ${index + 1}`} disabled={busy || retryPending} checked={(retryPending ? retryIntent.indices : retrySelection[job.id] || []).includes(index)} onCheckedChange={checked => setRetrySelection(current => ({ ...current, [job.id]: checked ? [...(current[job.id] || []), index] : (current[job.id] || []).filter(i => i !== index) }))} />候选 {index + 1}</label>)}
                        {retryAttempts.map(attempt => <Button key={attempt.jobId} size="sm" variant="outline" onClick={() => ctx.navigate("tasks", { kind: "image", jobId: attempt.jobId })}>查看重拍任务 · 候选 {attempt.indices.map((index: number) => index + 1).join("、")}</Button>)}
                        {(retryable.length > 0 || retryPending) && <Button size="sm" variant="outline" disabled={busy || (!retryPending && !(retrySelection[job.id] || []).length)} onClick={() => retryImages(job)}>{retryPending ? "按原请求键核查重拍结果" : "重拍所选失败候选"}</Button>}
                        {retryPending && <p className="break-all text-xs text-muted-foreground">待确认原请求 {retryIntent.key}</p>}
                      </div>}
                      <details className="text-xs text-muted-foreground">
                        <summary className="cursor-pointer">
                          任务编号、固定版本与参数
                        </summary>
                        <div className="mt-2 space-y-1 break-all">
                          <p>任务 {job.id}</p>
                          <p>固定修订 {job.targetRevisionId}</p>
                          <p>Gateway {job.gatewayJobId || "尚无回执"}</p>
                          <pre className="whitespace-pre-wrap">
                            {JSON.stringify(job.parameters || {}, null, 2)}
                          </pre>
                          {job.candidates?.map((c: Any) => (
                            <p key={c.id}>
                              候选 {c.id} · 素材 {c.assetId}
                            </p>
                          ))}
                        </div>
                      </details>
                    </CardContent>
                  </Card>
                );
              })}
            </div>
          </>
        )}
        <Dialog open={!!gallery} onOpenChange={open => { if (!open) setGallery(null); }}>
          <DialogContent className="max-h-[90vh] overflow-auto sm:max-w-[95vw]">
            <DialogHeader><DialogTitle>{gallery && gallery.ids.length > 1 ? "候选图片对比" : "查看候选大图"}</DialogTitle><DialogDescription>可直接选用其中一张。资料只保存参考草稿，仍需明确批准；镜头选用的是本镜头画面，不会改动共享场景参考，也不会自动提交视频。{selectionInspectionError ? ` ${selectionInspectionError}` : ""}</DialogDescription></DialogHeader>
            <div className={`grid gap-4 ${gallery && gallery.ids.length > 1 ? "md:grid-cols-2" : ""}`}>
              {(jobs.find(job => job.id === gallery?.jobId)?.candidates || []).filter((candidate: Any) => gallery?.ids.includes(candidate.id)).map((candidate: Any) => { const job = jobs.find(row => row.id === gallery?.jobId); return <figure key={candidate.id} className="min-w-0 space-y-2"><Picture ctx={ctx} id={candidate.assetId} className="max-h-[70vh] w-full" alt={`候选 ${candidate.index == null ? (job?.candidates || []).indexOf(candidate) + 1 : Number(candidate.index) + 1} 大图`} /><figcaption className="text-sm">候选 {candidate.index == null ? (job?.candidates || []).indexOf(candidate) + 1 : Number(candidate.index) + 1} · 种子 {(candidate.seedText ?? candidate.seed) == null ? "历史未记录" : String(candidate.seedText ?? candidate.seed)}</figcaption>{job && candidateIsSelected(job, candidate) ? <Badge><Check className="mr-1 size-3" />当前已选</Badge> : <Button variant="secondary" disabled={busy || !job} onClick={() => protect(entityDirty || shotHasUnsaved, () => void run(() => selectCandidate(job, candidate, true)))}>{job?.targetType === "shot" ? "选用这张，准备生成视频" : "选为资料参考草稿"}<ArrowRight className="size-4" /></Button>}</figure>; })}
            </div>
            <DialogFooter><Button variant="outline" onClick={() => setGallery(null)}>关闭图片查看</Button></DialogFooter>
          </DialogContent>
        </Dialog>
        <Dialog open={showNewShot} onOpenChange={setShowNewShot}>
          <DialogContent>
            <DialogHeader>
              <DialogTitle>新建镜头</DialogTitle>
              <DialogDescription>
                先给镜头起名，创建后填写人物、动作与构图。
              </DialogDescription>
            </DialogHeader>
            <Field label="新镜头名称" value={newTitle} onChange={setNewTitle} />
            <DialogFooter>
              <Button variant="outline" onClick={() => setShowNewShot(false)}>
                取消
              </Button>
              <Button
                disabled={busy || !newTitle.trim()}
                onClick={() =>
                  protect(
                    shotHasUnsaved,
                    () =>
                      void run(async () => {
                        const result = await post(
                          `${path("episodes", ctx.episode.id)}/shots`,
                          { title: newTitle.trim() },
                        );
                        setShowNewShot(false);
                        setNewTitle("");
                        await ctx.refresh();
                        await loadShot(result.shot.id);
                        ctx.notify(
                          "镜头已创建。现在填写内容与参考。",
                          "success",
                        );
                      }),
                  )
                }
              >
                创建镜头
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
        <Dialog
          open={!!confirmation}
          onOpenChange={(open) => {
            if (!open) setConfirmation(null);
          }}
        >
          <DialogContent>
            <DialogHeader>
              <DialogTitle>确认这次操作</DialogTitle>
              <DialogDescription>{confirmation?.text}</DialogDescription>
            </DialogHeader>
            <DialogFooter>
              <Button variant="outline" onClick={() => setConfirmation(null)}>
                返回继续编辑
              </Button>
              <Button
                onClick={() => {
                  const action = confirmation?.action;
                  setConfirmation(null);
                  action?.();
                }}
              >
                确认继续
              </Button>
            </DialogFooter>
          </DialogContent>
        </Dialog>
      </fieldset>
    </section>
  );
}
