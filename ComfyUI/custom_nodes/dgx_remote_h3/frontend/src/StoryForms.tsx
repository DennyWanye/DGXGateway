import { useState } from "react";
import type { WorkbenchContext } from "@/lib/types";
import { requestKey } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { Checkbox } from "@/components/ui/checkbox";
import { Badge } from "@/components/ui/badge";
import { Plus, Trash2 } from "lucide-react";
import { sourceSpan, sourceSlice } from "./StorySource";
export type StoryKind =
  "source" | "entities" | "beats" | "script" | "storyboard";
const unique = (prefix: string) =>
  `${prefix}-${requestKey().replace("drama-ui-", "").slice(0, 16)}`;
const entityName = (e: any) => e.name || e.revisions?.[0]?.name || e.id;
const revision = (story: any, kind: string, id?: string) =>
  (story.revisions?.[kind] || []).find(
    (r: any) => r.id === (id || story.heads?.[kind]?.currentRevisionId),
  );
const approved = (story: any, kind: string) =>
  revision(story, kind, story.heads?.[kind]?.approvedRevisionId);
export function defaultStoryContent(
  kind: StoryKind,
  story: any,
  entities: any[],
) {
  const source = story.heads?.source?.currentRevisionId,
    common = { schemaVersion: 1, sourceRevisionIds: source ? [source] : [] },
    entityRevisionIds = entities
      .map((e) => e.currentRevisionId)
      .filter(Boolean);
  if (kind === "source") return { title: "", text: "" };
  if (kind === "entities") return { ...common, candidates: [] };
  if (kind === "beats")
    return {
      ...common,
      entityRevisionIds,
      targetFrames: 96,
      beats: [],
      endingState: { summary: "", unresolvedQuestions: [] },
    };
  if (kind === "script")
    return {
      ...common,
      entityRevisionIds,
      beatRevisionId: approved(story, "beats")?.id || "",
      scenes: [],
    };
  return {
    schemaVersion: 1,
    scriptRevisionId: approved(story, "script")?.id || "",
    shots: [],
    omittedLines: [],
  };
}
export function validateStoryContent(
  kind: StoryKind,
  content: any,
  story: any,
  entities: any[],
) {
  const errors: any[] = [],
    fail = (path: string, message: string) => errors.push({ path, message });
  if (!content || typeof content !== "object" || Array.isArray(content))
    return [{ path: "", message: "请填写本阶段的结构化内容。" }];
  const needed = (value: any, path: string, label: string) => {
    if (typeof value !== "string" || !value.trim())
      fail(path, `${label}不能为空。`);
  };
  if (kind === "source") {
    needed(content.title, "title", "标题");
    needed(content.text, "text", "原文");
    if (new TextEncoder().encode(content.text || "").length > 256 * 1024)
      fail("text", "原文超过 256 KiB，不能截断保存。");
    return errors;
  }
  if (content.schemaVersion !== 1) fail("schemaVersion", "结构版本必须为 1。");
  if (
    kind !== "storyboard" &&
    (!Array.isArray(content.sourceRevisionIds) ||
      !content.sourceRevisionIds.length ||
      content.sourceRevisionIds.length > 16)
  )
    fail("sourceRevisionIds", "请选择 1–16 个已保存的原文修订。");
  if (kind === "entities") {
    if (!Array.isArray(content.candidates) || !content.candidates.length)
      fail("candidates", "至少添加一位人物或一个场景候选。");
    (content.candidates || []).forEach((c: any, i: number) => {
      needed(c.name, `candidates.${i}.name`, "候选名称");
      needed(c.description, `candidates.${i}.description`, "候选描述");
    });
  }
  if (kind === "beats") {
    if (!Number.isInteger(content.targetFrames) || content.targetFrames < 1)
      fail("targetFrames", "目标长度必须是正整数帧。");
    if (!(content.beats || []).length || content.beats.length > 128)
      fail("beats", "节拍数量须为 1–128。");
    (content.beats || []).forEach((b: any, i: number) => {
      needed(b.title, `beats.${i}.title`, "节拍标题");
      needed(b.description, `beats.${i}.description`, "节拍描述");
    });
    needed(content.endingState?.summary, "endingState.summary", "结尾状态");
  }
  if (kind === "script") {
    const beats = revision(story, "beats", content.beatRevisionId);
    if (!beats?.approved || beats.needsReview)
      fail("beatRevisionId", "请选择已批准且来源有效的节拍修订。");
    if (!(content.scenes || []).length || content.scenes.length > 64)
      fail("scenes", "场景数量须为 1–64。");
    let count = 0;
    const ids = new Set();
    (content.scenes || []).forEach((scene: any, i: number) => {
      if (!scene.locationId) fail(`scenes.${i}.locationId`, "请选择场景资料。");
      (scene.lines || []).forEach((line: any, j: number) => {
        count++;
        needed(line.text, `scenes.${i}.lines.${j}.text`, "台词");
        if (line.text?.length > 2000)
          fail(`scenes.${i}.lines.${j}.text`, "单条台词最多 2000 字符。");
        if (ids.has(line.lineId))
          fail(`scenes.${i}.lines.${j}.lineId`, "台词编号不能重复。");
        ids.add(line.lineId);
      });
    });
    if (count > 512) fail("scenes", "整份剧本最多512条台词。");
  }
  if (kind === "storyboard") {
    const script = revision(story, "script", content.scriptRevisionId);
    if (!script?.approved || script.needsReview)
      fail("scriptRevisionId", "请选择已批准且来源有效的剧本。");
    if (!(content.shots || []).length || content.shots.length > 128)
      fail("shots", "镜头提案数量须为1–128。");
    const allLines = (script?.content.scenes || []).flatMap(
        (s: any) => s.lines || [],
      ),
      used = new Set<string>();
    (content.shots || []).forEach((shot: any, i: number) => {
      needed(shot.title, `shots.${i}.title`, "镜头名称");
      if (!shot.sceneId) fail(`shots.${i}.sceneId`, "请选择所属剧本场景。");
      if (
        !Number.isInteger(shot.targetEditFrames) ||
        shot.targetEditFrames < 1 ||
        shot.targetEditFrames >
          Number(shot.generation?.requestDurationSeconds) * 24
      )
        fail(
          `shots.${i}.targetEditFrames`,
          "保留长度须为正整数帧，且不超过请求生成长度。",
        );
      (shot.dialogue || []).forEach((line: any, j: number) => {
        const source = allLines.find((l: any) => l.lineId === line.lineId);
        if (
          !source ||
          source.text !== line.text ||
          source.speakerId !== line.speakerId
        )
          fail(
            `shots.${i}.dialogue.${j}`,
            "对白必须匹配已批准剧本的完整原句与说话者。",
          );
        used.add(line.lineId);
      });
    });
    for (const line of allLines)
      if (
        !used.has(line.lineId) &&
        !(content.omittedLines || []).some(
          (o: any) => o.lineId === line.lineId && o.reason?.trim(),
        )
      )
        fail(
          "omittedLines",
          `台词“${line.text}”尚未安排；请选择镜头或明确省略理由。`,
        );
  }
  return errors;
}
function Field({
  label,
  path,
  value,
  onChange,
  multiline = false,
  type = "text",
}: any) {
  return (
    <div className="space-y-1">
      <Label>{label}</Label>
      {multiline ? (
        <Textarea
          aria-label={label}
          data-story-path={path}
          rows={3}
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
function Choice({ label, path, value, options, onChange }: any) {
  return (
    <div className="space-y-1">
      <Label>{label}</Label>
      <select
        aria-label={label}
        data-story-path={path}
        value={value || ""}
        onChange={(e) => onChange(e.target.value)}
        className="h-9 w-full rounded-md border bg-background px-3 text-sm"
      >
        <option value="">请选择</option>
        {options.map(([id, name]: any) => (
          <option value={id} key={id}>
            {name}
          </option>
        ))}
      </select>
    </div>
  );
}
function PanelList({ title, children, remove }: any) {
  return (
    <article className="space-y-4 rounded-xl border p-4">
      <div className="flex items-center justify-between gap-3">
        <h4 className="text-sm font-semibold">{title}</h4>
        <Button
          variant="ghost"
          size="sm"
          aria-label={`删除${title}`}
          onClick={remove}
        >
          <Trash2 className="size-4" />
        </Button>
      </div>
      {children}
    </article>
  );
}
function FrozenSources({ content, onChange, story, ctx, withEntities }: any) {
  return (
    <details className="rounded-lg border p-3" open>
      <summary className="cursor-pointer text-sm font-medium">
        本次使用的来源与资料修订
      </summary>
      <div className="mt-3 space-y-3">
        <div className="grid gap-2 sm:grid-cols-2">
          {(story.revisions?.source || []).map((r: any) => (
            <label className="flex items-center gap-2 text-sm" key={r.id}>
              <Checkbox
                checked={(content.sourceRevisionIds || []).includes(r.id)}
                onCheckedChange={(yes) =>
                  onChange({
                    ...content,
                    sourceRevisionIds: yes
                      ? [...(content.sourceRevisionIds || []), r.id]
                      : (content.sourceRevisionIds || []).filter(
                          (id: string) => id !== r.id,
                        ),
                  })
                }
              />
              {r.content.title} · 原文修订 {r.revision}
            </label>
          ))}
        </div>
        {!(story.revisions?.source || []).length && (
          <p className="text-xs text-muted-foreground">请先保存原始故事。</p>
        )}
        {withEntities && (
          <div className="grid gap-2 sm:grid-cols-2">
            {ctx.entities.map((e: any) => (
              <label className="flex items-center gap-2 text-sm" key={e.id}>
                <Checkbox
                  checked={(content.entityRevisionIds || []).includes(
                    e.currentRevisionId,
                  )}
                  onCheckedChange={(yes) =>
                    onChange({
                      ...content,
                      entityRevisionIds: yes
                        ? [
                            ...(content.entityRevisionIds || []),
                            e.currentRevisionId,
                          ]
                        : (content.entityRevisionIds || []).filter(
                            (id: string) => id !== e.currentRevisionId,
                          ),
                    })
                  }
                />
                {entityName(e)} · 资料修订 {e.revision}
              </label>
            ))}
          </div>
        )}
        {withEntities &&
          (content.entityRevisionIds || [])
            .filter(
              (id: string) =>
                !ctx.entities.some((e: any) => e.currentRevisionId === id),
            )
            .map((id: string) => (
              <label
                key={id}
                className="flex items-center gap-2 text-xs text-amber-600"
              >
                <Checkbox
                  checked
                  onCheckedChange={() =>
                    onChange({
                      ...content,
                      entityRevisionIds: content.entityRevisionIds.filter(
                        (v: string) => v !== id,
                      ),
                    })
                  }
                />
                已冻结的旧资料修订 {id}（保留或明确取消后选择新版）
              </label>
            ))}
      </div>
    </details>
  );
}
export function SourceEvidence({
  value,
  onChange,
  story,
  label,
  auxiliary,
  onAux,
  onBusy,
  buttonLabel = "加入原文依据",
}: any) {
  const rid = auxiliary?.rid || "",
    start = auxiliary?.start ?? "0",
    end = auxiliary?.end ?? "1";
  const setRid = (rid: string) => onAux({ ...auxiliary, rid });
  const setStart = (start: string) => onAux({ ...auxiliary, start });
  const setEnd = (end: string) => onAux({ ...auxiliary, end });
  const [error, setError] = useState(""),
    [busy, setBusy] = useState(false);
  const source = revision(story, "source", rid),
    text = source?.content.text || "";
  let preview = "";
  try {
    preview = sourceSlice(text, Number(start), Number(end));
  } catch {}
  return (
    <details className="rounded-lg border p-3">
      <summary className="cursor-pointer text-xs text-muted-foreground">
        原文依据（可选）· {value?.length || 0} 处
      </summary>
      <div className="mt-3 space-y-3">
        {(value || []).map((span: any, i: number) => (
          <div
            className="flex items-center justify-between gap-2 text-xs"
            key={i}
          >
            <span>
              原文 {span.start}–{span.end} 字符
            </span>
            <Button
              variant="ghost"
              size="sm"
              onClick={() =>
                onChange(value.filter((_: any, n: number) => n !== i))
              }
            >
              移除依据 {i + 1}
            </Button>
          </div>
        ))}
        <Choice
          label={`${label} 依据原文`}
          value={rid}
          options={(story.revisions?.source || []).map((r: any) => [
            r.id,
            `${r.content.title} · 修订${r.revision}`,
          ])}
          onChange={setRid}
        />
        <div className="grid grid-cols-2 gap-2">
          <Field
            label={`${label} 起始字符`}
            value={start}
            type="number"
            onChange={setStart}
          />
          <Field
            label={`${label} 结束字符`}
            value={end}
            type="number"
            onChange={setEnd}
          />
        </div>
        <p className="text-xs text-muted-foreground">
          按 Unicode 字符计数（包含emoji），左闭右开：
          {preview || "请选择有效范围"}
        </p>
        {error && (
          <p role="alert" className="text-xs text-destructive">
            {error}
          </p>
        )}
        <Button
          variant="outline"
          size="sm"
          disabled={busy || !source}
          onClick={async () => {
            setBusy(true);
            onBusy(true);
            try {
              const span = await sourceSpan(
                rid,
                text,
                Number(start),
                Number(end),
              );
              onChange([...(value || []), span]);
              setError("");
            } catch (e: any) {
              setError(e.message);
            } finally {
              setBusy(false);
              onBusy(false);
            }
          }}
        >
          {buttonLabel}
        </Button>
      </div>
    </details>
  );
}
export function StoryForm({
  kind,
  content,
  onChange,
  story,
  ctx,
  aux,
  onAux,
  onBusy,
}: {
  aux: any;
  onAux: (key: string, value: any) => void;
  onBusy: (busy: boolean) => void;
  kind: StoryKind;
  content: any;
  onChange: (value: any) => void;
  story: any;
  ctx: WorkbenchContext;
}) {
  const change = (field: string, value: any) =>
    onChange({ ...content, [field]: value });
  const update = (field: string, index: number, patch: any) =>
    change(
      field,
      (content[field] || []).map((v: any, i: number) =>
        i === index ? { ...v, ...patch } : v,
      ),
    );
  const remove = (field: string, index: number) =>
    change(
      field,
      (content[field] || []).filter((_: any, i: number) => i !== index),
    );
  const entityOptions = (kind?: string) =>
    ctx.entities
      .filter(
        (e: any) =>
          (!kind || e.kind === kind) &&
          ((content.entityRevisionIds || []).includes(e.currentRevisionId) ||
            (e.revisions || []).some((r: any) =>
              (content.entityRevisionIds || []).includes(r.id),
            )),
      )
      .map((e: any) => [e.id, entityName(e)]);
  if (kind === "entities")
    return (
      <div className="space-y-4">
        <FrozenSources {...{ content, onChange, story, ctx }} />
        {(content.candidates || []).map((c: any, i: number) => (
          <PanelList
            title={`候选 ${i + 1}`}
            key={c.candidateId || i}
            remove={() => remove("candidates", i)}
          >
            <div className="grid gap-3 sm:grid-cols-2">
              <Choice
                label={`候选 ${i + 1} 类型`}
                path={`candidates.${i}.kind`}
                value={c.kind}
                onChange={(kind: string) => update("candidates", i, { kind })}
                options={Object.entries({
                  character: "人物",
                  location: "场景",
                  costume: "服装",
                  prop: "道具",
                })}
              />
              <Field
                label={`候选 ${i + 1} 名称`}
                path={`candidates.${i}.name`}
                value={c.name}
                onChange={(name: string) => update("candidates", i, { name })}
              />
            </div>
            <Field
              label={`候选 ${i + 1} 描述`}
              path={`candidates.${i}.description`}
              value={c.description}
              multiline
              onChange={(description: string) =>
                update("candidates", i, { description })
              }
            />
            <Field
              label={`候选 ${i + 1} 别名（每行一个）`}
              value={(c.aliases || []).join("\n")}
              multiline
              onChange={(value: string) =>
                update("candidates", i, {
                  aliases: value.split("\n").filter(Boolean),
                })
              }
            />
            <SourceEvidence
              story={story}
              auxiliary={aux[`candidate:${c.candidateId}`]}
              onAux={(value: any) => onAux(`candidate:${c.candidateId}`, value)}
              onBusy={onBusy}
              label={`候选 ${i + 1}`}
              value={c.sourceSpans}
              onChange={(sourceSpans: any) =>
                update("candidates", i, { sourceSpans })
              }
            />
          </PanelList>
        ))}
        <Button
          variant="outline"
          onClick={() =>
            change("candidates", [
              ...(content.candidates || []),
              {
                candidateId: unique("entity"),
                kind: "character",
                name: "",
                aliases: [],
                description: "",
                sourceSpans: [],
              },
            ])
          }
        >
          <Plus className="mr-2 size-4" />
          添加人物或场景候选
        </Button>
      </div>
    );
  if (kind === "beats")
    return (
      <div className="space-y-4">
        <FrozenSources {...{ content, onChange, story, ctx }} withEntities />
        <Field
          label="本集目标长度（帧）"
          path="targetFrames"
          type="number"
          value={content.targetFrames}
          onChange={(v: string) =>
            change("targetFrames", v === "" ? "" : Number(v))
          }
        />
        <p className="text-xs text-muted-foreground">
          {Number(content.targetFrames) / 24} 秒 · 24 fps
        </p>
        {(content.beats || []).map((b: any, i: number) => (
          <PanelList
            title={`节拍 ${i + 1}`}
            key={b.beatId || i}
            remove={() => remove("beats", i)}
          >
            <Field
              label={`节拍 ${i + 1} 标题`}
              path={`beats.${i}.title`}
              value={b.title}
              onChange={(title: string) => update("beats", i, { title })}
            />
            <Field
              label={`节拍 ${i + 1} 发生什么`}
              path={`beats.${i}.description`}
              multiline
              value={b.description}
              onChange={(description: string) =>
                update("beats", i, { description })
              }
            />
            <SourceEvidence
              story={story}
              auxiliary={aux[`beat:${b.beatId}`]}
              onAux={(value: any) => onAux(`beat:${b.beatId}`, value)}
              onBusy={onBusy}
              label={`节拍 ${i + 1}`}
              value={b.sourceSpans}
              onChange={(sourceSpans: any) =>
                update("beats", i, { sourceSpans })
              }
            />
          </PanelList>
        ))}
        <Button
          variant="outline"
          onClick={() =>
            change("beats", [
              ...(content.beats || []),
              {
                beatId: unique("beat"),
                title: "",
                description: "",
                sourceSpans: [],
              },
            ])
          }
        >
          <Plus className="mr-2 size-4" />
          添加情节节拍
        </Button>
        <Field
          label="结尾时故事处于什么状态"
          path="endingState.summary"
          multiline
          value={content.endingState?.summary}
          onChange={(summary: string) =>
            change("endingState", { ...content.endingState, summary })
          }
        />
        <Field
          label="留下的悬念（每行一个）"
          multiline
          value={(content.endingState?.unresolvedQuestions || []).join("\n")}
          onChange={(v: string) =>
            change("endingState", {
              ...content.endingState,
              unresolvedQuestions: v.split("\n").filter(Boolean),
            })
          }
        />
      </div>
    );
  if (kind === "script") {
    const selectedBeats =
      revision(story, "beats", content.beatRevisionId)?.content.beats || [];
    return (
      <div className="space-y-4">
        <FrozenSources {...{ content, onChange, story, ctx }} withEntities />
        <Choice
          label="使用的已批准节拍"
          path="beatRevisionId"
          value={content.beatRevisionId}
          options={(story.revisions?.beats || [])
            .filter((r: any) => r.approved && !r.needsReview)
            .map((r: any) => [r.id, `节拍修订 ${r.revision}`])}
          onChange={(id: string) => change("beatRevisionId", id)}
        />
        {(content.scenes || []).map((scene: any, i: number) => (
          <PanelList
            title={`场景 ${i + 1}`}
            key={scene.sceneId || i}
            remove={() => remove("scenes", i)}
          >
            <Choice
              label={`场景 ${i + 1} 地点`}
              path={`scenes.${i}.locationId`}
              value={scene.locationId}
              options={entityOptions("location")}
              onChange={(locationId: string) =>
                update("scenes", i, { locationId })
              }
            />
            <div className="flex flex-wrap gap-3">
              {selectedBeats.map((b: any) => (
                <label
                  key={b.beatId}
                  className="flex items-center gap-2 text-sm"
                >
                  <Checkbox
                    checked={(scene.beatIds || []).includes(b.beatId)}
                    onCheckedChange={(yes) =>
                      update("scenes", i, {
                        beatIds: yes
                          ? [...(scene.beatIds || []), b.beatId]
                          : (scene.beatIds || []).filter(
                              (id: string) => id !== b.beatId,
                            ),
                      })
                    }
                  />
                  {b.title}
                </label>
              ))}
            </div>
            <Field
              label={`场景 ${i + 1} 动作（每行一条）`}
              multiline
              value={(scene.actions || []).join("\n")}
              onChange={(v: string) =>
                update("scenes", i, { actions: v.split("\n").filter(Boolean) })
              }
            />
            <div className="space-y-3">
              {(scene.lines || []).map((line: any, j: number) => (
                <div
                  className="space-y-3 rounded-lg bg-muted/30 p-3"
                  key={line.lineId || j}
                >
                  <Choice
                    label={`场景 ${i + 1} 台词 ${j + 1} 说话者`}
                    path={`scenes.${i}.lines.${j}.speakerId`}
                    value={line.speakerId || "narration"}
                    options={[
                      ["narration", "旁白"],
                      ...entityOptions("character"),
                    ]}
                    onChange={(id: string) =>
                      update("scenes", i, {
                        lines: scene.lines.map((l: any, n: number) =>
                          n === j
                            ? {
                                ...l,
                                speakerId: id === "narration" ? null : id,
                              }
                            : l,
                        ),
                      })
                    }
                  />
                  <Field
                    label={`场景 ${i + 1} 台词 ${j + 1} 文字`}
                    path={`scenes.${i}.lines.${j}.text`}
                    multiline
                    value={line.text}
                    onChange={(text: string) =>
                      update("scenes", i, {
                        lines: scene.lines.map((l: any, n: number) =>
                          n === j
                            ? {
                                ...l,
                                text,
                                provenance: { kind: "user_authored" },
                              }
                            : l,
                        ),
                      })
                    }
                  />
                  <SourceEvidence
                    story={story}
                    auxiliary={aux[`line:${line.lineId}`]}
                    onAux={(value: any) => onAux(`line:${line.lineId}`, value)}
                    onBusy={onBusy}
                    label={`场景 ${i + 1} 台词 ${j + 1}`}
                    value={
                      line.provenance?.kind === "source"
                        ? [line.provenance.span]
                        : []
                    }
                    buttonLabel="引用原句并替换这条台词"
                    onChange={(spans: any[]) => {
                      const span = spans.at(-1);
                      const text = span
                        ? sourceSlice(
                            revision(story, "source", span.sourceRevisionId)
                              .content.text,
                            span.start,
                            span.end,
                          )
                        : line.text;
                      update("scenes", i, {
                        lines: scene.lines.map((l: any, n: number) =>
                          n === j
                            ? {
                                ...l,
                                text,
                                provenance: span
                                  ? { kind: "source", span }
                                  : { kind: "user_authored" },
                              }
                            : l,
                        ),
                      });
                    }}
                  />
                  <p className="text-xs text-muted-foreground">
                    {line.provenance?.kind === "source"
                      ? "原文引用；改字后会标记人工创作"
                      : line.provenance?.kind === "model_authored"
                        ? "模型候选台词"
                        : "人工创作台词"}{" "}
                    · 台词编号 {line.lineId}
                  </p>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      update("scenes", i, {
                        lines: scene.lines.filter(
                          (_: any, n: number) => n !== j,
                        ),
                      })
                    }
                  >
                    删除台词 {j + 1}
                  </Button>
                </div>
              ))}
            </div>
            <Button
              variant="outline"
              onClick={() =>
                update("scenes", i, {
                  lines: [
                    ...(scene.lines || []),
                    {
                      lineId: unique("line"),
                      speakerId: null,
                      text: "",
                      provenance: { kind: "user_authored" },
                    },
                  ],
                })
              }
            >
              添加场景 {i + 1} 的台词
            </Button>
          </PanelList>
        ))}
        <Button
          variant="outline"
          onClick={() =>
            change("scenes", [
              ...(content.scenes || []),
              {
                sceneId: unique("scene"),
                locationId: "",
                beatIds: [],
                actions: [],
                lines: [],
              },
            ])
          }
        >
          <Plus className="mr-2 size-4" />
          添加剧本场景
        </Button>
      </div>
    );
  }
  const script = revision(story, "script", content.scriptRevisionId),
    scenes = script?.content.scenes || [],
    allLines = scenes.flatMap((s: any) => s.lines || []);
  return (
    <div className="space-y-4">
      <Choice
        label="使用的已批准剧本"
        path="scriptRevisionId"
        value={content.scriptRevisionId}
        options={(story.revisions?.script || [])
          .filter((r: any) => r.approved && !r.needsReview)
          .map((r: any) => [r.id, `剧本修订 ${r.revision}`])}
        onChange={(id: string) => change("scriptRevisionId", id)}
      />
      {(content.shots || []).map((shot: any, i: number) => (
        <PanelList
          title={`提案镜头 ${i + 1}`}
          key={shot.candidateId || i}
          remove={() => remove("shots", i)}
        >
          <Choice
            label={`提案镜头 ${i + 1} 剧本场景`}
            path={`shots.${i}.sceneId`}
            value={shot.sceneId}
            options={scenes.map((s: any, n: number) => [
              s.sceneId,
              `剧本场景 ${n + 1}`,
            ])}
            onChange={(sceneId: string) => update("shots", i, { sceneId })}
          />
          {[
            ["title", "名称"],
            ["action", "动作"],
            ["framing", "景别"],
            ["camera", "运镜"],
            ["prompt", "画面描述"],
          ].map(([key, label]) => (
            <Field
              key={key}
              label={`提案镜头 ${i + 1} ${label}`}
              path={`shots.${i}.${key}`}
              value={shot[key]}
              multiline={["action", "prompt"].includes(key)}
              onChange={(value: string) => update("shots", i, { [key]: value })}
            />
          ))}
          <div className="grid gap-3 sm:grid-cols-3">
            <Field
              label={`提案镜头 ${i + 1} 保留长度（帧）`}
              path={`shots.${i}.targetEditFrames`}
              value={shot.targetEditFrames}
              type="number"
              onChange={(value: string) =>
                update("shots", i, {
                  targetEditFrames: value === "" ? "" : Number(value),
                })
              }
            />
            <Choice
              label={`提案镜头 ${i + 1} 生成模式`}
              value={shot.generation?.profile}
              options={(
                ctx.capabilities?.videoCapabilities?.profiles || [
                  { id: "heretic-ref2va-v1", name: "H3 稳定4秒" },
                ]
              ).map((p: any) => [p.id, p.name])}
              onChange={(profile: string) =>
                update("shots", i, {
                  generation: {
                    contractVersion: "h3-ref2va-v1",
                    profile,
                    requestDurationSeconds: 4,
                  },
                })
              }
            />
            <Choice
              label={`提案镜头 ${i + 1} 请求时长`}
              value={String(shot.generation?.requestDurationSeconds || 4)}
              options={(shot.generation?.profile === "heretic-ref2va-hmnsfw-v1"
                ? [4, 15]
                : [4]
              ).map((n) => [String(n), `${n}秒`])}
              onChange={(v: string) =>
                update("shots", i, {
                  generation: {
                    ...shot.generation,
                    requestDurationSeconds: Number(v),
                  },
                })
              }
            />
          </div>
          <div className="space-y-2">
            <p className="text-sm font-medium">安排这段镜头的对白</p>
            {(
              scenes.find((s: any) => s.sceneId === shot.sceneId)?.lines || []
            ).map((line: any) => (
              <div
                key={line.lineId}
                className="space-y-2 rounded-lg border p-3"
              >
                <label className="flex items-start gap-2 text-sm">
                  <Checkbox
                    checked={(shot.dialogue || []).some(
                      (l: any) => l.lineId === line.lineId,
                    )}
                    onCheckedChange={(yes) =>
                      update("shots", i, {
                        dialogue: yes
                          ? [
                              ...(shot.dialogue || []),
                              {
                                lineId: line.lineId,
                                speakerId: line.speakerId,
                                text: line.text,
                              },
                            ]
                          : (shot.dialogue || []).filter(
                              (l: any) => l.lineId !== line.lineId,
                            ),
                      })
                    }
                  />
                  {line.text}
                </label>
                {(shot.dialogue || [])
                  .filter((l: any) => l.lineId === line.lineId)
                  .map((l: any) => (
                    <DialogueRange
                      key={l.lineId}
                      line={l}
                      label={`镜头 ${i + 1} 台词 ${line.lineId}`}
                      onChange={(patch: any) =>
                        update("shots", i, {
                          dialogue: shot.dialogue.map((v: any) =>
                            v.lineId === l.lineId ? { ...v, ...patch } : v,
                          ),
                        })
                      }
                    />
                  ))}
              </div>
            ))}
          </div>
        </PanelList>
      ))}
      <Button
        variant="outline"
        onClick={() =>
          change("shots", [
            ...(content.shots || []),
            {
              candidateId: unique("shot"),
              sceneId: scenes[0]?.sceneId || "",
              title: "",
              action: "",
              framing: "medium",
              camera: "locked",
              prompt: "",
              targetEditFrames: 96,
              generation: {
                contractVersion: "h3-ref2va-v1",
                profile: "heretic-ref2va-v1",
                requestDurationSeconds: 4,
              },
              dialogue: [],
            },
          ])
        }
      >
        <Plus className="mr-2 size-4" />
        添加镜头提案
      </Button>
      <details className="rounded-lg border p-3" open>
        <summary className="cursor-pointer text-sm">明确不拍摄的台词</summary>
        <div className="mt-3 space-y-3">
          {allLines.map((line: any) => {
            const omitted = (content.omittedLines || []).find(
              (l: any) => l.lineId === line.lineId,
            );
            return (
              <div key={line.lineId} className="space-y-2">
                <label className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={!!omitted}
                    onCheckedChange={(yes) =>
                      change(
                        "omittedLines",
                        yes
                          ? [
                              ...(content.omittedLines || []),
                              { lineId: line.lineId, reason: "" },
                            ]
                          : content.omittedLines.filter(
                              (l: any) => l.lineId !== line.lineId,
                            ),
                      )
                    }
                  />
                  省略：{line.text}
                </label>
                {omitted && (
                  <Field
                    label={`省略台词 ${line.lineId} 的理由`}
                    value={omitted.reason}
                    onChange={(reason: string) =>
                      change(
                        "omittedLines",
                        content.omittedLines.map((l: any) =>
                          l.lineId === line.lineId ? { ...l, reason } : l,
                        ),
                      )
                    }
                  />
                )}
              </div>
            );
          })}
        </div>
      </details>
    </div>
  );
}
function DialogueRange({ line, onChange, label }: any) {
  return (
    <details className="text-xs text-muted-foreground">
      <summary className="cursor-pointer">跨镜拆分这句台词（可选）</summary>
      <div className="mt-2 space-y-2">
        <p>
          保持完整原句不变；以 Unicode
          字符区间分配，各镜头同一连续编号，区间必须连续覆盖整句。
        </p>
        <label className="flex items-center gap-2">
          <Checkbox
            checked={!!line.range}
            onCheckedChange={(yes) =>
              onChange(
                yes
                  ? {
                      range: { start: 0, end: Array.from(line.text).length },
                      continuityId: line.lineId,
                    }
                  : { range: undefined, continuityId: undefined },
              )
            }
          />
          启用跨镜区间
        </label>
        {line.range && (
          <div className="grid gap-2 sm:grid-cols-3">
            <Field
              label={`${label} 起始字符`}
              type="number"
              value={line.range.start}
              onChange={(v: string) =>
                onChange({ range: { ...line.range, start: Number(v) } })
              }
            />
            <Field
              label={`${label} 结束字符`}
              type="number"
              value={line.range.end}
              onChange={(v: string) =>
                onChange({ range: { ...line.range, end: Number(v) } })
              }
            />
            <Field
              label={`${label} 连续编号`}
              value={line.continuityId}
              onChange={(v: string) => onChange({ continuityId: v })}
            />
          </div>
        )}
      </div>
    </details>
  );
}
