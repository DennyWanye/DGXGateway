import { sessionStore as sessionStorage } from "@/lib/workspace";
import { useEffect, useRef, useState } from "react";
import type { WorkbenchContext } from "@/lib/types";
import { get, post, requestKey, segment } from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
  CardDescription,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  BookOpen,
  Check,
  ChevronRight,
  FileText,
  Plus,
  RefreshCw,
  Save,
  Trash2,
  WandSparkles,
} from "lucide-react";
import {
  StoryForm,
  SourceEvidence,
  defaultStoryContent,
  validateStoryContent,
  type StoryKind,
} from "./StoryForms";

const TEXT_JOB_STATES: Record<string, string> = {
    QUEUED: "等待处理",
    WAITING_RESOURCE: "等待资源",
    LOADING: "正在加载模型",
    SUBMITTING: "正在提交",
    RUNNING: "正在生成",
    VALIDATING: "正在检查结构",
    RELEASING: "正在释放资源",
    SUCCEEDED: "候选已归档",
    FAILED: "任务失败",
    CANCELED: "已取消",
    SUBMISSION_UNKNOWN: "提交结果待核对",
    RECOVERY_REQUIRED: "资源需核对",
  };
const ACTIVE_TEXT_STATES = ["QUEUED", "WAITING_RESOURCE", "LOADING", "SUBMITTING", "RUNNING", "VALIDATING", "RELEASING"];

type Draft = {
  content: any;
  expectedRevision: number;
  dirty: boolean;
  expectedDraftRevision?: number;
  retained?: boolean;
};
type Intent = {
  path: string;
  payload: any;
  key: string;
  kind: string;
  stage: StoryKind;
};
const stages: { id: StoryKind; title: string; hint: string }[] = [
  { id: "source", title: "原始故事", hint: "保存故事原文，保留创作来源" },
  { id: "entities", title: "人物与场景", hint: "整理候选，再确认为资料草稿" },
  { id: "beats", title: "情节节拍", hint: "安排转折与结尾，人工批准节拍" },
  { id: "script", title: "剧本台词", hint: "把节拍写成场景与对白" },
  { id: "storyboard", title: "镜头提案", hint: "绑定对白，选出要制作的镜头" },
];
export const STORY_TASK_KINDS: Record<string, string> = {
  entities: "extract_entities",
  beats: "draft_beats",
  script: "draft_script",
  storyboard: "draft_storyboard",
};
export function storyGenerationPayload(
  stage: StoryKind,
  content: any,
  story: any,
) {
  const kind = STORY_TASK_KINDS[stage];
  if (!kind) throw new Error("原文由你保存，不提交生成任务。");
  let inputs = content;
  let upstream: Record<string, string> = {};
  if (stage === "script" || stage === "storyboard") {
    const upstreamKind = stage === "script" ? "beats" : "script";
    const key = stage === "script" ? "beatRevisionId" : "scriptRevisionId";
    const revision = (story.revisions?.[upstreamKind] || []).find(
      (r: any) => r.id === content[key],
    );
    if (!revision?.approved || revision.needsReview)
      throw new Error(
        `请先选择已批准且来源有效的${stage === "script" ? "节拍" : "剧本"}。`,
      );
    inputs = revision.content;
    upstream = { [key]: revision.id };
  }
  if (
    !Array.isArray(inputs.sourceRevisionIds) ||
    !inputs.sourceRevisionIds.length
  )
    throw new Error("请先选择已保存的原文修订。");
  return {
    kind,
    sourceRevisionIds: [...inputs.sourceRevisionIds],
    entityRevisionIds: [...(inputs.entityRevisionIds || [])],
    ...upstream,
  };
}
const emptyStory = {
  heads: {},
  revisions: {},
  editingDrafts: {},
  entityMappings: [],
  textJobs: [],
};
const readLocal = (key: string) => {
  try {
    return JSON.parse(sessionStorage.getItem(key) || "null");
  } catch {
    return null;
  }
};
const errorText = (e: any) => e?.message || String(e);
export { sourceSlice, sourceSpan, parseSourceFile } from "./StorySource";
import { parseSourceFile } from "./StorySource";

export function StoryPanel({ ctx }: { ctx: WorkbenchContext }) {
  const eid = ctx.episode?.id || "",
    scope = `${ctx.project?.id || ""}/${eid}`,
    storage = `dgx.drama.story.draft:${scope}`,
    pendingKey = `dgx.drama.story.pending:${scope}`;
  const cached = readLocal(storage),
    [active, setActive] = useState<StoryKind>(cached?.active || "source");
  const [story, setStory] = useState<any>(emptyStory),
    [drafts, setDrafts] = useState<Record<string, Draft>>(cached?.drafts || {}),
    [aux, setAux] = useState<any>(cached?.aux || { selections: [], merge: {} }),
    [draftScope, setDraftScope] = useState(scope);
  const [pending, setPending] = useState<Intent | null>(() =>
      readLocal(pendingKey),
    ),
    [busy, setBusy] = useState(false),
    [errors, setErrors] = useState<any[]>([]),
    [historyId, setHistoryId] = useState(""),
    [confirm, setConfirm] = useState<{
      title: string;
      action: () => void;
    } | null>(null);
  const root = useRef<HTMLDivElement>(null),
    lock = useRef(false),
    currentScope = useRef(scope),
    readSerial = useRef(0);
  currentScope.current = scope;
  const draft = drafts[active] || {
    content: defaultStoryContent(active, story, ctx.entities),
    expectedRevision: 0,
    dirty: false,
  };
  const head = story.heads[active] || {},
    revisions = story.revisions[active] || [],
    currentRevision = revisions.find(
      (r: any) => r.id === head.currentRevisionId,
    );
  const activeTextJobs = (story.textJobs || []).filter((job: any) =>
    (!job.episodeId || job.episodeId === eid) &&
    (ACTIVE_TEXT_STATES.includes(job.state) || job.resourceStatus === "held"),
  );
  const dirty =
    Object.values(drafts).some((d) => d.dirty) || !!aux.dirty || !!pending;
  const refresh = async () => {
    if (!eid || !ctx.capabilities?.storyEnabled) return;
    const serial = ++readSerial.current,
      value = await get(`/episodes/${segment(eid)}/story`);
    if (currentScope.current !== scope || serial !== readSerial.current) return;
    setStory(value);
    setDrafts((old) =>
      Object.fromEntries(
        stages.map(({ id }) => {
          const saved = (value.revisions?.[id] || []).find(
            (r: any) => r.id === value.heads?.[id]?.currentRevisionId,
          );
          return [
            id,
            old[id]?.dirty || old[id]?.retained
              ? old[id]
              : {
                  content:
                    saved?.content ||
                    defaultStoryContent(id, value, ctx.entities),
                  expectedRevision: value.heads?.[id]?.revision || 0,
                  expectedDraftRevision:
                    value.editingDrafts?.[id]?.[0]?.revision || 0,
                  dirty: false,
                },
          ];
        }),
      ),
    );
  };
  useEffect(() => {
    const data = readLocal(storage);
    setDraftScope(scope);
    setActive(data?.active || "source");
    setDrafts(data?.drafts || {});
    setAux(data?.aux || { selections: [], merge: {} });
    setPending(readLocal(pendingKey));
    setStory(emptyStory);
    setErrors([]);
    setHistoryId("");
    ++readSerial.current;
    refresh().catch((e) =>
      ctx.notify(`故事读取失败：${errorText(e)}`, "error"),
    );
  }, [scope, !!ctx.capabilities?.storyEnabled]);
  useEffect(() => {
    if (draftScope !== scope) return;
    try {
      sessionStorage.setItem(storage, JSON.stringify({ active, drafts, aux }));
    } catch {
      ctx.notify("本地草稿空间不足，请先保存修订，暂不要关闭页面。", "error");
    }
  }, [active, drafts, aux, scope, draftScope]);
  useEffect(() => {
    if (!dirty) return;
    const guard = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", guard);
    return () => window.removeEventListener("beforeunload", guard);
  }, [dirty]);
  useEffect(() => {
    let polling = false;
    const timer = setInterval(async () => {
      if (
        polling ||
        lock.current ||
        document.hidden ||
        !(
          root.current?.checkVisibility?.() ??
          !root.current?.closest("[hidden]")
        ) ||
        !(story.textJobs || []).some((j: any) =>
          [
            "QUEUED",
            "WAITING_RESOURCE",
            "LOADING",
            "SUBMITTING",
            "RUNNING",
            "VALIDATING",
            "RELEASING",
          ].includes(j.state),
        )
      )
        return;
      polling = true;
      try {
        await refresh();
      } catch (e) {
        ctx.notify(`文本任务读取失败：${errorText(e)}`, "error");
      } finally {
        polling = false;
      }
    }, 5000);
    return () => clearInterval(timer);
  }, [scope, story.textJobs]);
  const edit = (content: any) => {
    setDrafts((old) => ({
      ...old,
      [active]: { ...draft, content, dirty: true },
    }));
    setErrors([]);
  };
  const send = async (intent: Intent) => {
    if (lock.current || (intent.kind === "generate" && activeTextJobs.length)) return;
    lock.current = true;
    setBusy(true);
    setErrors([]);
    try {
      sessionStorage.setItem(pendingKey, JSON.stringify(intent));
      setPending(intent);
      const result = await post(intent.path, intent.payload, intent.key);
      if (currentScope.current !== scope) return;
      if (
        (["generate", "cancel", "reconcile"].includes(intent.kind) &&
          !result.job?.id) ||
        (intent.kind === "approve" && !result.revision?.id) ||
        (intent.kind === "entity" && !result.entity?.id) ||
        (intent.kind === "import" && !Array.isArray(result.shots))
      )
        throw new Error("操作回执缺少结果身份，请保留原请求键核查。");
      if (intent.kind === "generate") {
        setStory((old: any) => ({ ...old, textJobs: [result.job, ...(old.textJobs || []).filter((job: any) => job.id !== result.job.id)] }));
        ctx.notify(`候选生成任务已提交 · ${result.job.id}。${TEXT_JOB_STATES[result.job.state] || "等待 Gateway 处理"}；可在当前生成进度核查原任务。`, "success");
      }
      if (intent.kind === "save") {
        if (!result.revision?.id)
          throw new Error("保存回执缺少修订编号，请按原键核查。");
        setDrafts((old) => ({
          ...old,
          [intent.stage]: {
            content: result.revision.content,
            expectedRevision: result.head?.revision ?? result.revision.revision,
            dirty: false,
          },
        }));
        ctx.notify("新修订已保存。批准和导入需要单独操作。", "success");
      }
      if (intent.kind === "approve") {
        await ctx.refresh();
        ctx.notify("当前修订已批准，可以继续下一阶段。", "success");
      }
      if (intent.kind === "draft") {
        if (!result.draft?.id) throw new Error("编辑草稿回执缺少编号。");
        setDrafts((old) => ({
          ...old,
          [intent.stage]: {
            ...old[intent.stage],
            dirty: false,
            expectedDraftRevision: result.draft.revision,
            retained: true,
          },
        }));
        setErrors(result.draft.validation?.errors || []);
        ctx.notify("编辑草稿已保存；校验通过后再另存正式修订。", "success");
      }
      if (intent.kind === "import" || intent.kind === "entity") {
        setAux((old: any) => ({
          ...old,
          selections: intent.kind === "import" ? [] : old.selections,
        }));
        await ctx.refresh();
        ctx.notify(
          intent.kind === "import"
            ? "所选镜头已导入为未批准草稿，请到镜头设计继续。"
            : "实体已确认为资料文字草稿，参考形象仍需另行制作和批准。",
          "success",
        );
      }
      sessionStorage.removeItem(pendingKey);
      setPending(null);
      await refresh();
    } catch (e: any) {
      if ([400, 401, 403, 404, 409, 413, 422].includes(e?.status)) {
        sessionStorage.removeItem(pendingKey);
        setPending(null);
      }
      setErrors(
        e?.errors ||
          e?.details?.errors || [{ path: "", message: errorText(e) }],
      );
      ctx.notify(errorText(e), "error");
    } finally {
      lock.current = false;
      if (currentScope.current === scope) setBusy(false);
    }
  };
  const write = (path: string, payload: any, kind: string) => {
    if (!pending)
      return send({ path, payload, kind, stage: active, key: requestKey() });
  };
  const check = () => {
    const problems = validateStoryContent(
      active,
      draft.content,
      story,
      ctx.entities,
    );
    setErrors(problems);
    return !problems.length;
  };
  const save = () => {
    if (!check()) return;
    return write(
      `/episodes/${segment(eid)}/${active === "source" ? "source" : active}-revisions`,
      active === "source"
        ? { ...draft.content, expectedRevision: draft.expectedRevision }
        : { content: draft.content, expectedRevision: draft.expectedRevision },
      "save",
    );
  };
  const load = (content: any) => {
    const listFields = new Set([
      "candidates",
      "beats",
      "scenes",
      "lines",
      "shots",
      "dialogue",
      "sourceSpans",
      "sourceRevisionIds",
      "entityRevisionIds",
      "actions",
      "aliases",
      "beatIds",
      "omittedLines",
      "unresolvedQuestions",
    ]);
    const complexLists = new Set([
      "candidates",
      "beats",
      "scenes",
      "lines",
      "shots",
      "dialogue",
      "sourceSpans",
      "omittedLines",
    ]);
    const malformed = (value: any): boolean =>
      value &&
      typeof value === "object" &&
      Object.entries(value).some(
        ([key, v]) =>
          (listFields.has(key) && !Array.isArray(v)) ||
          (complexLists.has(key) &&
            Array.isArray(v) &&
            v.some(
              (item) =>
                !item || typeof item !== "object" || Array.isArray(item),
            )) ||
          (Array.isArray(v)
            ? v.some((item) => malformed(item))
            : v && typeof v === "object" && malformed(v)),
      );
    if (malformed(content)) {
      setErrors([
        {
          path: "",
          message:
            "候选列表结构不完整，无法安全载入表单。请参照保留的原文新建本阶段草稿；原始结果不会丢失。",
        },
      ]);
      return;
    }
    if (!content || typeof content !== "object" || Array.isArray(content)) {
      setErrors([
        {
          path: "",
          message: "候选不是可编辑的结构对象，请保留原文并手工整理。",
        },
      ]);
      return;
    }
    const apply = () => {
      setDrafts((old) => ({
        ...old,
        [active]: {
          content: structuredClone(content),
          expectedRevision: head.revision || 0,
          expectedDraftRevision:
            story.editingDrafts?.[active]?.[0]?.revision || 0,
          dirty: true,
        },
      }));
      setErrors([]);
    };
    if (draft.dirty)
      setConfirm({
        title: "用选定内容替换当前阶段的未保存草稿？",
        action: apply,
      });
    else apply();
  };
  const focusError = (path: string) => {
    const normalized = path.replace(/^\$\.?/, "").replace(/\[(\d+)\]/g, ".$1");
    const element = [
      ...(root.current?.querySelectorAll<HTMLElement>("[data-story-path]") ||
        []),
    ].find((e) => e.dataset.storyPath === normalized);
    element?.scrollIntoView({ behavior: "smooth", block: "center" });
    element?.focus();
  };
  const locked = busy || !!pending;
  if (!eid)
    return (
      <Card>
        <CardContent className="p-8 text-muted-foreground">
          请先创建或选择分集，再整理这个故事。
        </CardContent>
      </Card>
    );
  if (!ctx.capabilities?.storyEnabled)
    return (
      <Card>
        <CardContent className="p-8 text-muted-foreground">
          故事与剧本服务尚未启用。现有资料、镜头和剪辑功能继续可用。
        </CardContent>
      </Card>
    );
  return (
    <div
      ref={root}
      data-panel="story"
      data-dirty={dirty}
      aria-busy={busy}
      className="space-y-6"
    >
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="mb-2 text-xs uppercase tracking-[.2em] text-primary">
            Story development
          </p>
          <h2 className="text-2xl font-semibold tracking-tight">
            先把故事讲清楚
          </h2>
          <p className="mt-2 text-sm text-muted-foreground">
            从原文到镜头，逐步编辑、保存和确认。模型只提供候选。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() =>
            refresh().catch((e) => ctx.notify(errorText(e), "error"))
          }
        >
          <RefreshCw className="mr-2 size-4" />
          刷新故事记录
        </Button>
      </div>
      <div
        className="grid grid-cols-2 gap-2 lg:grid-cols-5"
        aria-label="故事创作阶段"
      >
        {stages.map((s, i) => (
          <Button
            key={s.id}
            variant={active === s.id ? "default" : "outline"}
            className="h-auto justify-start gap-2 py-3"
            disabled={busy}
            onClick={() => {
              setActive(s.id);
              setHistoryId("");
              setErrors([]);
            }}
          >
            <span className="opacity-60">0{i + 1}</span>
            {s.title}
            {drafts[s.id]?.dirty && <span aria-label="未保存">•</span>}
          </Button>
        ))}
      </div>
      {aux.dirty && (
        <div className="flex flex-wrap items-center justify-between gap-3 rounded-lg border p-3 text-sm">
          <p className="text-muted-foreground">
            引用区间、合并目标或镜头勾选草稿已保留。保存正文不会清除这些辅助选择。
          </p>
          <Button
            variant="ghost"
            disabled={locked}
            onClick={() =>
              setConfirm({
                title: "清除所有辅助选择草稿？",
                action: () => setAux({ selections: [], merge: {} }),
              })
            }
          >
            清除辅助选择草稿
          </Button>
        </div>
      )}
      {pending && (
        <div
          role="alert"
          className="space-y-2 rounded-lg border border-amber-500/40 p-4"
        >
          <p className="text-sm">
            {busy
              ? "正在提交并等待回执…"
              : "原提交结果尚未确认。参数已冻结，重开页面不会自动提交。"}
          </p>
          <p className="break-all text-xs text-muted-foreground">
            原请求 {pending.key}
          </p>
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => send(pending)}
          >
            按原请求键核查故事操作
          </Button>
        </div>
      )}
      {errors.length > 0 && (
        <div
          role="alert"
          className="space-y-1 rounded-lg border border-destructive/40 p-4 text-sm"
        >
          <p className="font-medium">请先处理这些内容</p>
          {errors.map((e, i) => (
            <button
              key={i}
              className="block text-left text-destructive underline-offset-4 hover:underline"
              onClick={() => focusError(e.path || "")}
            >
              {e.path ? `${e.path}：` : ""}
              {e.message}
            </button>
          ))}
        </div>
      )}
      <Card>
        <CardHeader>
          <div className="flex flex-wrap justify-between gap-3">
            <div>
              <CardTitle>
                {stages.find((s) => s.id === active)?.title}
              </CardTitle>
              <CardDescription>
                {stages.find((s) => s.id === active)?.hint}
              </CardDescription>
            </div>
            <Badge variant="outline">
              {draft.dirty
                ? "有未保存编辑"
                : draft.retained
                  ? "已保存编辑草稿"
                  : head.currentRevisionId
                    ? `当前修订 ${currentRevision?.revision ?? head.revision}${currentRevision?.approved ? " · 已批准" : ""}`
                    : "尚无修订"}
            </Badge>
          </div>
        </CardHeader>
        <CardContent className="space-y-5">
          <fieldset disabled={locked} className="space-y-5">
            {draft.dirty &&
              head.revision !== undefined &&
              head.revision !== draft.expectedRevision && (
                <div className="rounded-lg border border-amber-500/40 p-3 text-sm">
                  服务端出现了新修订，当前输入与原编辑基线已保留。可以载入新版后合并，再保存。
                </div>
              )}
            <div className="flex flex-wrap items-end gap-2">
              <div className="min-w-44 flex-1">
                <Label>历史修订</Label>
                <select
                  aria-label="故事历史修订"
                  value={historyId}
                  onChange={(e) => setHistoryId(e.target.value)}
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                >
                  <option value="">选择已保存修订</option>
                  {revisions.map((r: any) => (
                    <option key={r.id} value={r.id}>
                      修订 {r.revision} ·{" "}
                      {r.needsReview
                        ? "来源需复核"
                        : r.approved
                          ? "已批准"
                          : "待确认"}
                    </option>
                  ))}
                </select>
              </div>
              <Button
                variant="outline"
                disabled={!historyId}
                onClick={() =>
                  load(revisions.find((r: any) => r.id === historyId).content)
                }
              >
                载入修订编辑
              </Button>
              <Button
                variant="ghost"
                onClick={() =>
                  load(defaultStoryContent(active, story, ctx.entities))
                }
              >
                新建本阶段草稿
              </Button>
            </div>
            {active === "source" ? (
              <div className="space-y-4">
                <StoryInput
                  label="原文标题"
                  path="title"
                  value={draft.content.title || ""}
                  onChange={(title) => edit({ ...draft.content, title })}
                />
                <div className="space-y-2">
                  <Label htmlFor="story-source-file">
                    导入 UTF-8 文本（.txt，最大 256 KiB）
                  </Label>
                  <Input
                    id="story-source-file"
                    type="file"
                    accept=".txt,text/plain"
                    onChange={async (e) => {
                      const file = e.target.files?.[0];
                      if (!file) return;
                      setBusy(true);
                      try {
                        if (!file.name.toLowerCase().endsWith(".txt"))
                          throw new Error("请选择 UTF-8 .txt 文件。");
                        if (file.size > 256 * 1024)
                          throw new Error("原文最大256 KiB，不会截断。");
                        const text = parseSourceFile(await file.arrayBuffer());
                        load({ title: file.name.replace(/\.txt$/i, ""), text });
                      } catch (error) {
                        setErrors([
                          { path: "text", message: errorText(error) },
                        ]);
                      } finally {
                        setBusy(false);
                      }
                    }}
                  />
                </div>
                <StoryInput
                  label="故事原文"
                  path="text"
                  value={draft.content.text || ""}
                  multiline
                  rows={12}
                  onChange={(text) => edit({ ...draft.content, text })}
                />
                <p className="text-xs text-muted-foreground">
                  {Array.from(draft.content.text || "").length} 个 Unicode 字符
                  · {new TextEncoder().encode(draft.content.text || "").length}{" "}
                  字节。保留原文，不自动截断。
                </p>
              </div>
            ) : (
              <StoryForm
                kind={active}
                content={draft.content}
                onChange={edit}
                story={story}
                ctx={ctx}
                aux={aux.evidence || {}}
                onAux={(key, value) =>
                  setAux((old: any) => ({
                    ...old,
                    dirty: true,
                    evidence: { ...(old.evidence || {}), [key]: value },
                  }))
                }
                onBusy={setBusy}
              />
            )}
            <div className="flex flex-wrap gap-2 border-t pt-4">
              <Button onClick={save}>
                <Save className="mr-2 size-4" />
                保存{stages.find((s) => s.id === active)?.title}新修订
              </Button>
              {active !== "source" && (
                <Button
                  variant="outline"
                  onClick={() =>
                    write(
                      `/episodes/${segment(eid)}/${active}-drafts`,
                      {
                        rawText: JSON.stringify(draft.content),
                        expectedRevision: draft.expectedDraftRevision || 0,
                      },
                      "draft",
                    )
                  }
                >
                  先保存编辑草稿
                </Button>
              )}
              <Button variant="ghost" onClick={check}>
                检查结构与引用
              </Button>
              {["beats", "script"].includes(active) && (
                <Button
                  variant="outline"
                  disabled={
                    draft.dirty ||
                    draft.retained ||
                    !currentRevision ||
                    currentRevision.approved ||
                    currentRevision.needsReview
                  }
                  onClick={() =>
                    write(
                      `/story-revisions/${segment(currentRevision.id)}/approve`,
                      { expectedRevision: head.revision },
                      "approve",
                    )
                  }
                >
                  <Check className="mr-2 size-4" />
                  批准当前{active === "beats" ? "节拍" : "剧本"}
                </Button>
              )}
            </div>
          </fieldset>
        </CardContent>
      </Card>
      {active === "entities" && currentRevision && (
        <EntityConfirm
          ctx={ctx}
          revision={currentRevision}
          mappings={story.entityMappings || []}
          disabled={locked || draft.dirty || !!draft.retained}
          aux={aux}
          setAux={setAux}
          confirm={(candidate: any, merge: string) =>
            write(
              `/story-revisions/${segment(currentRevision.id)}/confirm-entity`,
              {
                candidateId: candidate.candidateId,
                expectedRevision: head.revision,
                ...(merge
                  ? {
                      mergeEntityId: merge,
                      expectedEntityRevision:
                        aux.merge?.[candidate.candidateId]?.revision,
                    }
                  : {}),
              },
              "entity",
            )
          }
        />
      )}
      {active === "storyboard" && currentRevision && (
        <Card>
          <CardHeader>
            <CardTitle>选择要制作的镜头</CardTitle>
            <CardDescription>
              导入只建立未批准镜头草稿，不提交图片或视频生成。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <fieldset
              disabled={
                locked ||
                draft.dirty ||
                !!draft.retained ||
                currentRevision.needsReview
              }
              className="space-y-3"
            >
              {currentRevision.content.shots.map((s: any) => (
                <label
                  key={s.candidateId}
                  className="flex items-center gap-3 rounded-lg border p-3"
                >
                  <Checkbox
                    checked={aux.selections?.includes(s.candidateId) || false}
                    onCheckedChange={(yes) =>
                      setAux((old: any) => ({
                        ...old,
                        dirty: true,
                        selections: yes
                          ? [...(old.selections || []), s.candidateId]
                          : (old.selections || []).filter(
                              (id: string) => id !== s.candidateId,
                            ),
                      }))
                    }
                  />
                  <span className="text-sm">
                    {s.title} · {s.targetEditFrames / 24} 秒
                  </span>
                </label>
              ))}
              <Button
                disabled={!aux.selections?.length}
                onClick={() =>
                  write(
                    `/story-revisions/${segment(currentRevision.id)}/import`,
                    {
                      candidateIds: aux.selections,
                      expectedRevision: head.revision,
                    },
                    "import",
                  )
                }
              >
                导入选中镜头草稿
              </Button>
            </fieldset>
            <Button variant="ghost" onClick={() => ctx.navigate("shots")}>
              去镜头设计继续
              <ChevronRight className="ml-1 size-4" />
            </Button>
          </CardContent>
        </Card>
      )}
      {active !== "source" && (
        <Card>
          <CardHeader>
            <CardTitle>让模型提供候选</CardTitle>
            <CardDescription>
              使用已保存的来源与批准版本。剧本和分镜生成沿用所选批准上游的原文与实体修订。结果要先载入、编辑、保存，再人工批准。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Button
              disabled={
                locked ||
                activeTextJobs.length > 0 ||
                draft.dirty ||
                !ctx.capabilities?.textGenerationEnabled
              }
              onClick={() => {
                try {
                  void write(
                    `/episodes/${segment(eid)}/text-jobs`,
                    storyGenerationPayload(active, draft.content, story),
                    "generate",
                  );
                } catch (e) {
                  setErrors([{ path: "", message: errorText(e) }]);
                }
              }}
            >
              <WandSparkles className="mr-2 size-4" />
              生成本阶段候选
            </Button>
            {activeTextJobs.length > 0 && (
              <div role="status" aria-label="当前生成进度" className="space-y-2 rounded-xl border border-primary/25 bg-primary/5 p-4 text-sm">
                <p className="font-medium">本分集已有生成任务正在处理，请先核查原任务。仍可继续编辑和保存。</p>
                {activeTextJobs.map((job: any) => (
                  <p key={job.id} className="break-all">
                    {stages.find((stage) => STORY_TASK_KINDS[stage.id] === job.kind)?.title || job.kind} · {TEXT_JOB_STATES[job.state] || job.state} · 任务 {job.id}
                  </p>
                ))}
                <Button variant="outline" size="sm" disabled={locked} onClick={() => refresh().catch((error) => ctx.notify(errorText(error), "error"))}>
                  <RefreshCw className="mr-2 size-4" />核查任务进度
                </Button>
                <p className="text-muted-foreground">核查只读取现有任务，不会重新提交生成。</p>
              </div>
            )}
            {!ctx.capabilities?.textGenerationEnabled && (
              <p className="text-sm text-muted-foreground">
                {ctx.capabilities?.textGenerationReason ||
                  "文本生成尚未完成 Gateway 资格验证；可以继续手写和保存，不会伪造生成结果。"}
              </p>
            )}
            {(story.editingDrafts?.[active] || []).map((d: any) => (
              <details key={d.id} className="rounded-lg border p-3">
                <summary className="cursor-pointer text-sm">
                  编辑草稿 {d.revision} ·{" "}
                  {d.validation?.valid ? "结构可继续编辑" : "需要修正"}
                </summary>
                <div className="mt-3 space-y-3">
                  {(d.validation?.errors || []).map((e: any, i: number) => (
                    <p key={i} className="text-sm text-destructive">
                      {e.path}：{e.message}
                    </p>
                  ))}
                  <Button
                    variant="outline"
                    disabled={locked}
                    onClick={() => {
                      try {
                        load(JSON.parse(d.rawText));
                      } catch {
                        setErrors([
                          {
                            path: "",
                            message:
                              "该原始内容不是完整结构，请参照原文在本阶段表单手工整理。原始草稿继续保留。",
                          },
                        ]);
                      }
                    }}
                  >
                    载入草稿字段继续编辑
                  </Button>
                  <Textarea
                    readOnly
                    rows={5}
                    aria-label={`原始编辑草稿 ${d.revision}`}
                    value={d.rawText || ""}
                  />
                </div>
              </details>
            ))}
          </CardContent>
        </Card>
      )}
      <TextJobRecords
        jobs={story.textJobs || []}
        disabled={locked}
        generationReady={!!ctx.capabilities?.textGenerationEnabled && !activeTextJobs.length}
        onAction={(job, action) =>
          action === "repair"
            ? write(
                `/episodes/${segment(eid)}/text-jobs`,
                { ...job.inputRevisions, kind: job.kind, repairOf: job.id },
                "generate",
              )
            : write(`/text-jobs/${segment(job.id)}/${action}`, {}, action)
        }
      />
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
              其他阶段的草稿和已保存历史继续保留。这个操作只替换当前阶段本地编辑内容。
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
export function StoryInput({
  label,
  path,
  value,
  onChange,
  multiline,
  rows = 3,
  type = "text",
}: {
  label: string;
  path: string;
  value: any;
  onChange: (value: string) => void;
  multiline?: boolean;
  rows?: number;
  type?: string;
}) {
  return (
    <div className="space-y-2">
      <Label>{label}</Label>
      {multiline ? (
        <Textarea
          aria-label={label}
          data-story-path={path}
          rows={rows}
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
      ) : (
        <Input
          aria-label={label}
          data-story-path={path}
          type={type}
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
        />
      )}
    </div>
  );
}
function EntityConfirm({
  ctx,
  revision,
  mappings,
  disabled,
  aux,
  setAux,
  confirm,
}: any) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>确认为人物与场景资料</CardTitle>
        <CardDescription>
          只建立文字草稿；角色参考图依然需要独立制作和批准。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <fieldset disabled={disabled} className="space-y-3">
          {revision.content.candidates.map((c: any) => (
            <div
              key={c.candidateId}
              className="flex flex-wrap items-center gap-3 rounded-lg border p-3"
            >
              <span className="min-w-32 flex-1 text-sm font-medium">
                {c.name}
              </span>
              <select
                className="h-9 rounded-md border bg-background px-3 text-sm"
                aria-label={`${c.name} 合并目标`}
                value={aux.merge?.[c.candidateId]?.id || ""}
                onChange={(e) => {
                  const entity = ctx.entities.find(
                    (v: any) => v.id === e.target.value,
                  );
                  setAux((old: any) => ({
                    ...old,
                    dirty: true,
                    merge: {
                      ...old.merge,
                      [c.candidateId]: {
                        id: e.target.value,
                        revision: entity?.revision,
                      },
                    },
                  }));
                }}
              >
                <option value="">建立新资料草稿</option>
                {ctx.entities
                  .filter((e: any) => e.kind === c.kind)
                  .map((e: any) => (
                    <option value={e.id} key={e.id}>
                      合并到 {e.name || e.revisions?.[0]?.name || e.id}
                    </option>
                  ))}
              </select>
              <Button
                variant="outline"
                disabled={mappings.some(
                  (m: any) =>
                    m.candidate_id === c.candidateId &&
                    m.proposal_revision_id === revision.id,
                )}
                onClick={() => confirm(c, aux.merge?.[c.candidateId]?.id || "")}
              >
                {mappings.some(
                  (m: any) =>
                    m.candidate_id === c.candidateId &&
                    m.proposal_revision_id === revision.id,
                )
                  ? `${c.name} 已确认为资料草稿`
                  : `确认 ${c.name} 为资料草稿`}
              </Button>
            </div>
          ))}
        </fieldset>
      </CardContent>
    </Card>
  );
}
function TextJobRecords({
  jobs,
  disabled,
  generationReady,
  onAction,
}: {
  jobs: any[];
  disabled: boolean;
  generationReady: boolean;
  onAction: (job: any, action: string) => any;
}) {

  return jobs.length ? (
    <Card>
      <CardHeader>
        <CardTitle>文本任务记录</CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {jobs.map((job) => (
          <details key={job.id} className="rounded-lg border p-3">
            <summary className="cursor-pointer text-sm">
              {TEXT_JOB_STATES[job.state] || job.state} ·{" "}
              {stages.find((s) => STORY_TASK_KINDS[s.id] === job.kind)?.title ||
                job.kind}
            </summary>
            <div className="mt-3 space-y-2">
              <p className="break-all text-xs text-muted-foreground">
                任务 {job.id} · 原 Gateway {job.gatewayId || "尚未确认"} · 输出{" "}
                {job.outputStatus || "暂无"} · 资源{" "}
                {job.resourceStatus || "待核查"}
              </p>
              {job.error && (
                <p className="text-sm text-destructive">
                  {errorText(job.error)}
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={
                    disabled ||
                    ["SUCCEEDED", "FAILED", "CANCELED"].includes(job.state) ||
                    job.cancelRequested
                  }
                  onClick={() => onAction(job, "cancel")}
                >
                  请求取消文本任务
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={
                    disabled ||
                    !["SUBMISSION_UNKNOWN", "RECOVERY_REQUIRED"].includes(
                      job.state,
                    )
                  }
                  onClick={() => onAction(job, "reconcile")}
                >
                  核对原文本任务
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={
                    disabled ||
                    !generationReady ||
                    job.outputStatus !== "invalid_draft" ||
                    job.resourceStatus !== "released" ||
                    !job.inputRevisions ||
                    job.repairOf ||
                    job.repairedBy ||
                    jobs.some((other) => other.repairOf === job.id) ||
                    ["SUBMISSION_UNKNOWN", "RECOVERY_REQUIRED"].includes(
                      job.state,
                    )
                  }
                  onClick={() => onAction(job, "repair")}
                >
                  基于原输入请求一次修复候选
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                取消先保存意图；已发出的模型调用需等待退出或核对，不代表资源已释放。修复会创建关联的新候选尝试，最多一次。
              </p>
              {(job.attempts || []).map((attempt: any) => (
                <details key={attempt.id}>
                  <summary className="cursor-pointer text-xs text-muted-foreground">
                    原始响应与校验 · 尝试 {attempt.epoch}
                  </summary>
                  <div className="mt-2 space-y-2">
                    {(attempt.validation?.errors || []).map(
                      (e: any, i: number) => (
                        <p className="text-xs text-destructive" key={i}>
                          {e.path}：{e.message}
                        </p>
                      ),
                    )}
                    <Textarea
                      readOnly
                      rows={5}
                      aria-label={`文本任务原始响应 ${attempt.id}`}
                      value={attempt.rawText || ""}
                    />
                    <p className="break-all text-xs text-muted-foreground">
                      响应校验值 {attempt.rawHash || "未返回"}
                    </p>
                  </div>
                </details>
              ))}
            </div>
          </details>
        ))}
      </CardContent>
    </Card>
  ) : null;
}
