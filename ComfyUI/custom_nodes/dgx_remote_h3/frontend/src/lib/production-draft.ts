import { compileOfficialPrompt, nodesFromSnapshot } from "@/lib/reference-nodes";

type Any = Record<string, any>;
export type ReferenceInput = { assetId: string; name: string; revisionId: string; revision: number | string; kind: string };
const PRIMARY: Record<string, string> = { character: 'identity_front', location: 'location_wide', costume: 'costume_fullbody', prop: 'prop_detail' };
/** Resolve the exact frozen dependency, never the entity's newer current draft. */
export function shotReferenceInputs(snapshot: Any, entities: Any[], referenceApproved = false) {
  const inputs: ReferenceInput[] = [], issues: string[] = [];
  if (referenceApproved && snapshot.approvedReferenceAssetId)
    return { inputs: [{ assetId: snapshot.approvedReferenceAssetId, name: '已确认镜头参考画面', revisionId: '', revision: '', kind: 'shot' }], issues };
  const ids = [snapshot.locationRevisionId, ...(snapshot.characterRevisionIds || []), ...(snapshot.costumeRevisionIds || []), ...(snapshot.propRevisionIds || [])].filter(Boolean);
  for (const id of ids) {
    const entity = entities.find(e => e.revisions?.some((r: Any) => r.id === id));
    const revision = entity?.revisions?.find((r: Any) => r.id === id);
    if (!revision) { issues.push(`关联资料版本 ${id} 尚未读取，请返回镜头核查。`); continue; }
    const assetId = Array.isArray(revision.references) ? revision.references.find((r: Any) => r.role === PRIMARY[entity.kind])?.assetId : revision.referenceAssetId;
    if (!assetId) { issues.push(`${revision.name} · v${revision.revision} 缺少主参考图，请先确认资料。`); continue; }
    // Revisions previously approved and explicitly retained remain valid; backend owns invalidation review.
    if (!entity.approvedRevisionId) issues.push(`${revision.name} 尚未确认参考版本。`);
    if (!inputs.some(r => r.assetId === assetId)) inputs.push({ assetId, name: revision.name, revisionId: id, revision: revision.revision, kind: entity.kind });
  }
  return { inputs, issues };
}
const REF2VA_SECTIONS = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"] as const;
const REF2VA_TEXT_ONLY = ["integrated_multimodal_description", "overall_soundscape", "non_diegetic_music"] as const;

export function isCompleteRef2vaPrompt(prompt: string): boolean {
  const labels = new Set(
    String(prompt || "")
      .split(/\r?\n/)
      .map((line) => line.match(/^([a-z_]+):/)?.[1])
      .filter((label): label is string => Boolean(label)),
  );
  const has = (sections: readonly string[]) => sections.every((section) => labels.has(section));
  return has(REF2VA_SECTIONS) || has(REF2VA_TEXT_ONLY);
}

export function videoDraftPrompt(snapshot: Any, _entities: Any[] = []): string {
  const stored = String(snapshot?.prompt || "").trim();
  const nodes = nodesFromSnapshot(snapshot);
  if (!nodes.length) return stored;
  const roles = Array.isArray(snapshot?.customReferenceRoles) ? snapshot.customReferenceRoles : [];
  return compileOfficialPrompt({ instruction: stored, nodes, customRoles: roles });
}

export function promptAfterShotReload(previous: { shotRevisionId?: string; prompt?: string } | undefined, revisionId: string, snapshotPrompt: string): string {
  if (previous?.shotRevisionId === revisionId && typeof previous.prompt === "string") return previous.prompt;
  return snapshotPrompt;
}

export function buildShotPrompts(snapshot: Any, entities: Any[] = []) {
  const refs = shotReferenceInputs({ ...snapshot, approvedReferenceAssetId: null }, entities).inputs;
  const identity = refs.map(r => `${r.kind === 'location' ? '场景' : r.kind === 'character' ? '人物' : '资料'}：${r.name}`).join('；');
  const staticPrompt = [snapshot.prompt?.trim(), identity].filter(Boolean).join('。');
  return { staticPrompt, actionPrompt: staticPrompt };
}

export const SHOT_IMAGE_SLOTS = {
  action: {
    purpose: 'action',
    assetKey: 'actionReferenceAssetId',
    promptKey: 'actionReferencePrompt',
    slot: 'shot-action',
    path: 'action-reference-selection',
    label: '动作参考图',
    button: 'AI 绘制动作参考图',
    help: '动作图只约束关键姿势、重心和肢体方向；人物身份与构图仍以主参考图为准。静态动作图不能保证完整动作迁移，连续动作优先再加参考视频。',
    sendsToH3: true,
    suffix: '全身动作参考图，姿势方向清晰，四肢轮廓与重心明确，服装飘带不遮挡关键关节，简洁背景；保持已确认人物身份与服装，不添加文字。',
  },
  scene: {
    purpose: 'scene',
    assetKey: 'sceneReferenceAssetId',
    promptKey: 'sceneReferencePrompt',
    slot: 'shot-scene',
    path: 'scene-reference-selection',
    label: '场景图',
    button: 'AI 绘制场景图',
    help: '场景图只给戏台看，不作为 H3 主图或动作图。',
    sendsToH3: false,
    suffix: '16:9 场景图，空镜，不要人物正脸，不要文字水印。',
  },
  blocking: {
    purpose: 'blocking',
    assetKey: 'blockingReferenceAssetId',
    promptKey: 'blockingReferencePrompt',
    slot: 'shot-blocking',
    path: 'blocking-reference-selection',
    label: '站位图',
    button: 'AI 绘制站位图',
    help: '站位图只给戏台看，不会送入 H3。',
    sendsToH3: false,
    suffix: '16:9 站位示意图，标出人物位置与镜头朝向，简笔即可，不要写成可当主参考的写实人物图。',
  },
  keyframeGrid: {
    purpose: 'keyframeGrid',
    assetKey: 'keyframeGridAssetId',
    promptKey: 'keyframeGridPrompt',
    slot: 'shot-keyframe-grid',
    path: 'keyframe-grid-selection',
    label: '动作九宫格',
    button: 'AI 绘制动作九宫格',
    help: '九宫格只给戏台规划用，禁止作为 H3 主图或动作图。',
    sendsToH3: false,
    suffix: '16:9 三乘三动作九宫格，同一镜头内九个关键姿态，简洁线稿，不要当主参考图。',
  },
} as const;

export type ShotImagePurpose = keyof typeof SHOT_IMAGE_SLOTS;
export const PLANNING_IMAGE_PURPOSES = ['scene', 'blocking', 'keyframeGrid'] as const;

export function shotImageSlot(purpose: string | undefined) {
  if (!purpose || !(purpose in SHOT_IMAGE_SLOTS)) return null;
  return SHOT_IMAGE_SLOTS[purpose as ShotImagePurpose];
}

export function planningImagePrompt(purpose: string | undefined, snapshot: Any, entities: Any[] = []) {
  const slot = shotImageSlot(purpose);
  const built = buildShotPrompts(snapshot, entities);
  if (!slot) return built.staticPrompt;
  const stored = String(snapshot[slot.promptKey] || '').trim();
  const base = stored || (slot.purpose === 'action' ? built.actionPrompt : built.staticPrompt);
  return [base, slot.suffix].filter(Boolean).join('。');
}

/** Older jobs have no execution observations; absence never proves parallelism. */
export function imageBatchOf(job: Any): Any | null {
 const value = job.imageBatch || job.inputSnapshot?.gatewayStatus?.imageBatch;
 return value && Array.isArray(value.candidates) ? value : null;
}
export function retryableImageIndices(job: Any): number[] {
 if (!['SUCCEEDED', 'FAILED', 'CANCELED', 'INTERRUPTED'].includes(job.state) || job.memoryReleased !== true || job.cleanupRequired) return [];
 return [...new Set<number>((imageBatchOf(job)?.candidates || []).filter((c: Any) => Number.isInteger(c.index) && c.index >= 0 && ['FAILED', 'CANCELED'].includes(c.state)).map((c: Any) => c.index))];
}
export function completedImageRetries(intent?: Any): Any[] {
 return [...(Array.isArray(intent?.history) ? intent.history : []), ...(intent?.jobId ? [intent] : [])]
  .filter(attempt => typeof attempt.jobId === 'string' && Array.isArray(attempt.indices));
}
export function knownImageRetries(job: Any, jobs: Any[] = [], intent?: Any): Any[] {
 const server = jobs.filter(child => (child.retryOf || child.inputSnapshot?.retryOf) === job.id).map(child => ({ jobId: child.id, indices: child.parameters?.candidate_indices || child.inputSnapshot?.retryIndices || [] }));
 const merged = new Map(completedImageRetries(intent).map(attempt => [attempt.jobId, attempt]));
 for (const attempt of server) if (Array.isArray(attempt.indices) && attempt.indices.length) merged.set(attempt.jobId, attempt);
 return [...merged.values()];
}
export function remainingImageRetryIndices(job: Any, intent?: Any, jobs: Any[] = []): number[] {
 const completed = new Set(knownImageRetries(job, jobs, intent).flatMap(attempt => attempt.indices));
 return retryableImageIndices(job).filter(index => !completed.has(index));
}

/** Saved payload recovery is a read/download action, never another generation. */
export function recoverableImageJob(job: Any): boolean {
 return ['SUCCEEDED', 'FAILED', 'CANCELED', 'INTERRUPTED'].includes(job.state) && job.memoryReleased === true && !job.cleanupRequired &&
  !!imageBatchOf(job)?.candidates.some((candidate: Any) => candidate.state === 'UNKNOWN' && candidate.error === 'Saved image download requires recovery');
}

export function pendingImageBatchMessage(job: Any): string {
 const parameters = job.parameters || job.inputSnapshot?.parameters || {};
 const count = Number(parameters.batch_count), limit = Number(parameters.parallelism);
 return Number.isInteger(count) && count > 0 && Number.isInteger(limit) && limit > 0
  ? `计划制作${count}张，每批上限${Math.min(count, limit)}张；等待执行后显示实测并行信息`
  : '等待执行后显示实测并行信息';
}
