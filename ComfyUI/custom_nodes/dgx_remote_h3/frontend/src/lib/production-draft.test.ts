import { describe, it, expect } from 'vitest';
import { buildShotPrompts, planningImagePrompt, shotImageSlot, shotReferenceInputs } from './production-draft';
const entities = [
 { kind: 'character', approvedRevisionId: 'actor2', currentRevisionId: 'actor3', revisions: [
  { id: 'actor1', revision: 1, name: '悟空旧形象', referenceAssetId: 'actor-image1' },
  { id: 'actor2', revision: 2, name: '悟空', referenceAssetId: 'actor-image2' },
  { id: 'actor3', revision: 3, name: '未批准新草稿', referenceAssetId: 'wrong-new-image' }] },
 { kind: 'location', approvedRevisionId: 'lake1', revisions: [{ id: 'lake1', revision: 1, name: '湖畔', referenceAssetId: 'lake-image' }] },
];
describe('MF02 frozen shot input drafts', () => {
 it('orders location before character and uses the exact bound revision rather than latest draft', () => {
  const value = shotReferenceInputs({ locationRevisionId: 'lake1', characterRevisionIds: ['actor1'] }, entities);
  expect(value.inputs.map(r => r.assetId)).toEqual(['lake-image', 'actor-image1']);
  expect(value.inputs[1]).toMatchObject({ name: '悟空旧形象', revisionId: 'actor1', revision: 1 });
 });
 it('keeps every input above three instead of truncating', () => {
  const many = Array.from({ length: 4 }, (_, i) => ({ kind: 'character', approvedRevisionId: `r${i}`, revisions: [{ id: `r${i}`, name: `角色${i}`, referenceAssetId: `a${i}` }] }));
  expect(shotReferenceInputs({ characterRevisionIds: ['r0', 'r1', 'r2', 'r3'] }, many).inputs).toHaveLength(4);
 });
 it('uses the confirmed full shot reference when present', () => {
  expect(shotReferenceInputs({ approvedReferenceAssetId: 'full-frame', characterRevisionIds: ['actor1'] }, entities, true).inputs.map(r => r.assetId)).toEqual(['full-frame']);
 });
 it('reports missing dependency rather than substituting another identity', () => {
  expect(shotReferenceInputs({ characterRevisionIds: ['missing'] }, entities).issues).toHaveLength(1);
 });
 it('does not splice hidden director notes into prompts', () => {
  const value = buildShotPrompts({ prompt: '黄昏站立画面', action: '向前走三步', camera: '缓慢推进', framing: '全景', characterRevisionIds: ['actor2'] }, entities);
  expect(value.staticPrompt).toBe('黄昏站立画面。人物：悟空');
  expect(value.actionPrompt).toBe(value.staticPrompt);
 });
 it('maps planning purposes to workbench-only slots', () => {
  expect(shotImageSlot('primary')).toBeNull();
  expect(shotImageSlot('action')?.sendsToH3).toBe(true);
  expect(shotImageSlot('scene')?.sendsToH3).toBe(false);
  expect(shotImageSlot('blocking')?.path).toBe('blocking-reference-selection');
  expect(shotImageSlot('keyframeGrid')?.assetKey).toBe('keyframeGridAssetId');
  const prompt = planningImagePrompt('keyframeGrid', { keyframeGridPrompt: '九个姿态', action: '向前走三步' }, entities);
  expect(prompt).toContain('九个姿态');
  expect(prompt).toContain('不要当主参考图');
  expect(prompt).not.toContain('向前走三步');
 });
});

it('does not treat an inherited asset ID in an unapproved shot revision as confirmed', () => {
 expect(shotReferenceInputs({ approvedReferenceAssetId: 'old-frame', characterRevisionIds: ['actor1'] }, entities).inputs.map(r => r.assetId)).toEqual(['actor-image1']);
});

// MF05 decisive oracle: requested concurrency is not evidence of observed GPU batching.
it('MF05 distinguishes requested and observed parallelism and preserves seed strings', async () => {
 const { imageBatchOf, retryableImageIndices } = await import('./production-draft');
 const job = { state: 'FAILED', memoryReleased: true, inputSnapshot: { gatewayStatus: { imageBatch: { candidateCount: 4, parallelLimit: 2, executionMode: 'gpu_batch', candidates: [{ index: 0, seed: '9007199254740993', state: 'SUCCEEDED' }, { index: 1, seed: 43, state: 'FAILED' }, { index: 2, state: 'UNKNOWN' }, { index: 3, state: 'CANCELED' }] } } } };
 expect(imageBatchOf(job)?.observedParallelism).toBeUndefined();
 expect(imageBatchOf(job)?.candidates[0].seed).toBe('9007199254740993');
 expect(retryableImageIndices(job)).toEqual([1, 3]);
 expect(retryableImageIndices({ ...job, memoryReleased: false })).toEqual([]);
 expect(retryableImageIndices({ ...job, state: 'RUNNING' })).toEqual([]);
});

it("replaces a video-page draft when the shot revision changes", async () => {
  const { promptAfterShotReload } = await import("./production-draft");
  expect(promptAfterShotReload({ shotRevisionId: "old", prompt: "<Environment 1> old" }, "new", "subject_definitions:\nfull")).toBe("subject_definitions:\nfull");
  expect(promptAfterShotReload({ shotRevisionId: "same", prompt: "local edit" }, "same", "server")).toBe("local edit");
});

it('keeps the stored shot prompt as the video draft', async () => {
  const { isCompleteRef2vaPrompt, videoDraftPrompt } = await import('./production-draft');
  const contract = [
    'subject_definitions:', '<Picture 1> identity.',
    'summary:', 'One shot.',
    'retention_analysis:', 'Keep the face.',
    'detailed_description:', 'Dance once.',
    'overall_soundscape:', 'Silk only.',
    'non_diegetic_music:', 'No added score.',
  ].join('\n');
  const snapshot = { prompt: contract, action: '向前走三步', camera: '缓慢推进', framing: '全景' };
  expect(isCompleteRef2vaPrompt(contract)).toBe(true);
  expect(isCompleteRef2vaPrompt('summary: 不是整篇合同')).toBe(false);
  expect(videoDraftPrompt(snapshot, entities)).toBe(contract);
  expect(videoDraftPrompt({ ...snapshot, prompt: '黄昏站立画面' }, entities)).toBe('黄昏站立画面');
});
