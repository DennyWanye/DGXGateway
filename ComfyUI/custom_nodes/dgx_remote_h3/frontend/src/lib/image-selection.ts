import { get, post, requestKey } from "@/lib/api";

type Any = Record<string, any>;

const path = (kind: string, id: string) => `/${kind}/${encodeURIComponent(id)}`;
const currentOf = (entity: Any) =>
  entity?.current ||
  entity?.revisions?.find((revision: Any) => revision.id === entity.currentRevisionId) ||
  entity?.revisions?.at(-1);
const conflict = (message: string) => Object.assign(new Error(message), { status: 409, code: "revision_conflict" });

/** Read the server's current target before rendering a stale confirmation. */
export async function inspectImageSelection(job: Any, candidate: Any) {
  const targetType = candidate.targetType || job.targetType;
  const targetId = candidate.targetId || job.targetId;
  const sourceRevisionId = candidate.targetRevisionId || job.targetRevisionId;
  if (targetType === "entity") {
    const entity = (await get(path("entities", targetId))).entity;
    const sourceRevision = entity.revisions?.find((revision: Any) => revision.id === sourceRevisionId);
    if (!sourceRevision) throw conflict("候选来源资料版本已不可用，请刷新任务后再选择。");
    const current = currentOf(entity);
    const sourceRole = job.referenceRole || sourceRevision.referenceRole || "identity_front";
    const selected = (current?.references || []).some((reference: Any) => reference.role === sourceRole && reference.assetId === candidate.assetId);
    return { targetType, targetId, target: entity, sourceRevision, sourceRole, selected,
      stale: Boolean(candidate.stale || sourceRevisionId !== entity.currentRevisionId), expectedRevision: entity.revision };
  }
  if (targetType === "shot") {
    const production = await get(`${path("shots", targetId)}/production`);
    const sourceRevision = production.currentRevisionId === sourceRevisionId
      ? production.current : production.history?.find((revision: Any) => revision.id === sourceRevisionId);
    if (!sourceRevision) throw conflict("候选来源镜头版本已不可用，请刷新任务后再选择。");
    return { targetType, targetId, target: production, sourceRevision,
      selected: production.current?.snapshot?.approvedReferenceAssetId === candidate.assetId,
      stale: Boolean(candidate.stale || sourceRevisionId !== production.currentRevisionId), expectedRevision: production.revision };
  }
  throw new Error("图片候选的目标类型不正确。");
}

/**
 * Persist exactly one candidate. This has no confirmation UI and never
 * navigates; callers can retain a comparison dialog and decide what to do.
 */
export async function selectImageCandidate(job: Any, candidate: Any, options: {
  acceptStale?: boolean; expectedRevision?: number; requestKey?: string;
} = {}) {
  const inspection = await inspectImageSelection(job, candidate);
  if (options.expectedRevision != null && options.expectedRevision !== inspection.expectedRevision)
    throw conflict("目标已在打开选择窗口后更新，请刷新后再选择。");
  if (inspection.stale && options.acceptStale !== true)
    throw conflict("候选来自旧版本，需要明确确认后才能选用。");
  const key = options.requestKey || requestKey();
  const expectedRevision = options.expectedRevision ?? inspection.expectedRevision;
  if (inspection.targetType === "entity") {
    const current = currentOf(inspection.target);
    const role = inspection.sourceRole;
    const references = [
      ...(current.references || []).filter((reference: Any) => reference.role !== role),
      { role, assetId: candidate.assetId },
    ];
    const result = await post(`${path("entities", inspection.targetId)}/revisions`, {
      expectedRevision, name: current.name, description: current.description || "", references,
      referenceRole: role, referenceAssetId: candidate.assetId, candidateId: candidate.id,
      acceptStaleCandidate: inspection.stale,
    }, key);
    return { ...inspection, expectedRevision, revision: result.entity?.revision, result };
  }
  const result = await post(`${path("shots", inspection.targetId)}/reference-selection`, {
    expectedRevision, assetId: candidate.assetId, candidateId: candidate.id,
    acceptStaleCandidate: inspection.stale,
  }, key);
  return { ...inspection, expectedRevision, revision: result.revision, result };
}
