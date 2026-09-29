// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, screen, waitFor } from '@testing-library/dom';
import '@testing-library/jest-dom/vitest';
import { createProductionWorkbench, imageBatchParameters } from '../../web/drama/production.js';
import { get, post, requestKey } from '../../web/drama/api.js';
vi.mock('../../web/drama/api.js', () => ({ get: vi.fn(), post: vi.fn(), requestKey: vi.fn(() => 'drama-ui-legacy-mf05'), segment: encodeURIComponent, mediaUrl: (pid: string, aid: string) => `/media/${pid}/${aid}` }));
vi.mock('../../web/drama/canvas.js', () => ({ openShotOnCanvas: vi.fn() }));
let workbench: ReturnType<typeof createProductionWorkbench>;
let currentJobs: any[] = [];
beforeEach(() => {
 vi.clearAllMocks(); vi.mocked(requestKey).mockReset().mockReturnValue('drama-ui-legacy-mf05'); sessionStorage.clear(); currentJobs = [];
 vi.mocked(get).mockImplementation(async path => path.endsWith('/entities') ? { entities: [{ id: 'actor', kind: 'character', revision: 1, currentRevisionId: 'r1', revisions: [{ id: 'r1', name: '人物', revision: 1, references: [] }] }] } : path.endsWith('/reference-jobs') ? { jobs: currentJobs } : path.endsWith('/image-library') ? { sources: [] } : { assets: [{ id: 'asset1', kind: 'image', filename: '人物.png' }, { id: 'asset2', kind: 'image', filename: '人物2.png' }] });
 vi.mocked(post).mockResolvedValue({ job: { id: 'new', state: 'PREPARED', candidates: [] } });
 Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.setAttribute('open', ''); } });
 Object.defineProperty(HTMLDialogElement.prototype, 'close', { configurable: true, value() { this.removeAttribute('open'); this.dispatchEvent(new Event('close')); } });
});
afterEach(() => { workbench?.pause(); document.body.replaceChildren(); });
async function mount() {
 const context = { getContext: () => ({ project: { id: 'legacy' }, episode: { id: 'ep' } }), capabilities: { referenceGenerationEnabled: true }, isBusy: () => false, isVisible: () => false, report: vi.fn(), run: async (action: () => any) => { try { await action(); } catch (error: any) { context.report(error.message, true); } }, syncEpisode: vi.fn() };
 workbench = createProductionWorkbench(context);
 workbench.mount(document.body);
 await screen.findByRole('button', { name: '图片制作' });
 await workbench.activate('jobs');
 return context;
}
describe('legacy MF05 contracts', () => {
 it('normalizes both tool contracts and accepts user concurrency and rejects nonpositive values', () => {
  expect(imageBatchParameters(1, 2)).toEqual({ batch_count: 1, parallelism: 1 });
  expect(imageBatchParameters('4', '2')).toEqual({ batch_count: 4, parallelism: 2 });
  expect(imageBatchParameters(4, 3)).toEqual({ batch_count: 4, parallelism: 3 });
  expect(() => imageBatchParameters(4, 0)).toThrow();
 });
 it('sends identical batch fields for text-to-image and reference editing', async () => {
  await mount();
  fireEvent.change(screen.getByLabelText('制作目标（固定当前内容版本）'), { target: { value: 'entity:r1' } });
  fireEvent.change(screen.getByLabelText('画面提示词'), { target: { value: '候选画面' } });
  fireEvent.change(screen.getByLabelText('候选数量'), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: '提交图片制作' }));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/entity-revisions/r1/reference-jobs', expect.objectContaining({ tool: 'generate_image', parameters: expect.objectContaining({ batch_count: 4, parallelism: 2 }) })));
  fireEvent.change(screen.getByLabelText('图片工具'), { target: { value: 'edit_image' } });
  fireEvent.change(screen.getByLabelText('编辑要求'), { target: { value: '编辑多候选' } });
  fireEvent.change(screen.getByLabelText('输入图 1（必选）'), { target: { value: 'asset1' } });
  fireEvent.change(screen.getByLabelText('候选数量'), { target: { value: '4' } });
  fireEvent.click(screen.getByRole('button', { name: '提交图片制作' }));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/entity-revisions/r1/reference-jobs', expect.objectContaining({ tool: 'edit_image', parameters: expect.objectContaining({ batch_count: 4, parallelism: 2, asset_ids: ['asset1'] }) })));
 });
 it('compares available candidates without generating or approving and states that concurrency is unobserved', async () => {
  currentJobs = [{ id: 'job', state: 'RUNNING', tool: 'edit_image', targetType: 'entity', candidates: [{ id: 'c1', index: 0, assetId: 'asset1', seed: 42, state: 'SUCCEEDED' }, { id: 'c2', index: 1, assetId: 'asset2', seed: 43, state: 'SUCCEEDED' }], imageBatch: { candidateCount: 4, parallelLimit: 2, candidates: [] } }];
  await mount();
  expect(screen.getByText('候选总数 4 · 请求每批上限 2 张 · 实际并行尚未观测')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 1' }));
  fireEvent.click(screen.getByRole('checkbox', { name: '对比候选 2' }));
  fireEvent.click(screen.getByRole('button', { name: '对比已选图片（2/4）' }));
  expect(screen.getByRole('dialog', { name: '候选图片对比' })).toBeInTheDocument();
  expect(screen.getByText('候选 2 · 种子 43')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button', { name: '关闭图片查看' }));
  expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  expect(post).not.toHaveBeenCalled();
 });
});

it('legacy retries only selected failed indices and restores the original key after an ambiguous response', async () => {
 currentJobs = [{ id: 'source-job', state: 'FAILED', memoryReleased: true, tool: 'edit_image', targetType: 'entity', candidates: [], imageBatch: { candidateCount: 3, parallelLimit: 2, candidates: [{ index: 0, seed: 42, state: 'SUCCEEDED' }, { index: 1, seed: 43, state: 'FAILED' }, { index: 2, seed: 44, state: 'UNKNOWN' }] } }];
 vi.mocked(post).mockRejectedValueOnce(new Error('lost response'));
 const context = await mount();
 expect(screen.queryByRole('checkbox', { name: '重拍候选 1' })).not.toBeInTheDocument();
 expect(screen.queryByRole('checkbox', { name: '重拍候选 3' })).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('checkbox', { name: '重拍候选 2' }));
 fireEvent.click(screen.getByRole('button', { name: '重拍所选失败候选' }));
 await waitFor(() => expect(context.report).toHaveBeenCalledWith('lost response', true));
 expect(vi.mocked(post).mock.calls[0]).toEqual(['/reference-jobs/source-job/retry', { candidateIndices: [1] }, 'drama-ui-legacy-mf05']);
 workbench.pause(); document.body.replaceChildren();
 vi.mocked(post).mockResolvedValue({ job: { id: 'child-job', state: 'PREPARED', candidates: [] } });
 await mount();
 expect(post).toHaveBeenCalledTimes(1);
 fireEvent.click(screen.getByRole('button', { name: '按原请求键核查重拍结果' }));
 await screen.findByText('已创建重拍任务 child-job，请在任务列表查看。');
 expect(vi.mocked(post).mock.calls[1]).toEqual(vi.mocked(post).mock.calls[0]);
});

it('legacy preserves first subset receipt while another failed subset starts a new request', async () => {
 vi.mocked(requestKey).mockReturnValueOnce('drama-ui-first').mockReturnValueOnce('drama-ui-second');
 currentJobs = [{ id: 'source-job', state: 'FAILED', memoryReleased: true, candidates: [], imageBatch: { candidateCount: 4, parallelLimit: 2, candidates: [{ index: 1, state: 'FAILED' }, { index: 3, state: 'CANCELED' }] } }];
 vi.mocked(post).mockResolvedValueOnce({ job: { id: 'first-child', state: 'PREPARED', candidates: [] } }).mockResolvedValueOnce({ job: { id: 'second-child', state: 'PREPARED', candidates: [] } });
 await mount();
 fireEvent.click(screen.getByRole('checkbox', { name: '重拍候选 2' })); fireEvent.click(screen.getByRole('button', { name: '重拍所选失败候选' }));
 await screen.findByRole('button', { name: '查看重拍任务 · 候选 2' });
 expect(screen.queryByRole('checkbox', { name: '重拍候选 2' })).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('checkbox', { name: '重拍候选 4' })); fireEvent.click(screen.getByRole('button', { name: '重拍所选失败候选' }));
 await screen.findByRole('button', { name: '查看重拍任务 · 候选 4' });
 expect(screen.getByRole('button', { name: '查看重拍任务 · 候选 2' })).toBeInTheDocument();
 expect(vi.mocked(post).mock.calls.map(call => [call[1], call[2]])).toEqual([[{ candidateIndices: [1] }, 'drama-ui-first'], [{ candidateIndices: [3] }, 'drama-ui-second']]);
 currentJobs = [...currentJobs, { id: 'first-child', retryOf: 'source-job', state: 'PREPARED', parameters: { candidate_indices: [1] }, candidates: [] }, { id: 'second-child', retryOf: 'source-job', state: 'PREPARED', parameters: { candidate_indices: [3] }, candidates: [] }];
 workbench.pause(); document.body.replaceChildren(); await mount();
 expect(screen.getByRole('button', { name: '查看重拍任务 · 候选 2' })).toBeInTheDocument();
 expect(screen.getByRole('button', { name: '查看重拍任务 · 候选 4' })).toBeInTheDocument();
 expect(post).toHaveBeenCalledTimes(2);
});
it('legacy explicitly recovers a saved unknown image without resubmission', async () => {
 currentJobs = [{ id: 'source-job', state: 'FAILED', memoryReleased: true, candidates: [], imageBatch: { candidateCount: 1, parallelLimit: 1, candidates: [{ index: 0, state: 'UNKNOWN', error: 'Saved image download requires recovery' }] } }];
 vi.mocked(post).mockResolvedValue({ job: { id: 'source-job', state: 'SUCCEEDED', candidates: [] } });
 await mount(); expect(post).not.toHaveBeenCalled();
 expect(screen.queryByRole('checkbox', { name: '重拍候选 1' })).not.toBeInTheDocument();
 fireEvent.click(screen.getByRole('button', { name: '恢复已生成图片' }));
 await waitFor(() => expect(post).toHaveBeenCalledExactlyOnceWith('/reference-jobs/source-job/recover', {}));
 await waitFor(() => expect(screen.queryByRole('button', { name: '恢复已生成图片' })).not.toBeInTheDocument());
});

it.each([
 { state: 'RUNNING', memoryReleased: true, error: 'Saved image download requires recovery' },
 { state: 'FAILED', memoryReleased: false, error: 'Saved image download requires recovery' },
 { state: 'UNKNOWN', memoryReleased: true, error: 'Saved image download requires recovery' },
 { state: 'FAILED', memoryReleased: true, error: 'Gateway request unknown' },
])('legacy does not offer recovery for unsafe or unrelated unknown %j', async ({ state, memoryReleased, error }) => {
 currentJobs = [{ id: 'source-job', state, memoryReleased, candidates: [], imageBatch: { candidateCount: 1, parallelLimit: 1, candidates: [{ index: 0, state: 'UNKNOWN', error }] } }];
 await mount(); expect(screen.queryByRole('button', { name: '恢复已生成图片' })).not.toBeInTheDocument(); expect(post).not.toHaveBeenCalled();
});

it('legacy uses seedText for exact large seed in status, card and enlarged image', async () => {
 const exact = '18446744073709551615';
 const candidate = { id: 'c0', index: 0, assetId: 'asset1', seed: 18446744073709551615, seedText: exact, state: 'SUCCEEDED' };
 currentJobs = [{ id: 'job', state: 'SUCCEEDED', candidates: [candidate], imageBatch: { candidateCount: 1, parallelLimit: 1, candidates: [candidate] } }];
 await mount(); expect(screen.getAllByText(new RegExp(exact))).toHaveLength(2);
 fireEvent.click(screen.getByRole('button', { name: '放大候选 1' }));
 expect(screen.getByText(`候选 1 · 种子 ${exact}`)).toBeInTheDocument();
 expect(screen.queryByText(/18446744073709552000/)).not.toBeInTheDocument();
});

it.each(['PREPARED', 'QUEUED', 'RUNNING'])('legacy distinguishes planned counts from observed parallelism for %s', async state => {
 currentJobs = [{ id: 'job', state, parameters: { batch_count: 2, parallelism: 2 }, candidates: [] }];
 await mount(); expect(screen.getByText('计划制作2张，每批上限2张；等待执行后显示实测并行信息')).toBeInTheDocument();
 expect(screen.queryByText(/历史任务未记录/)).not.toBeInTheDocument(); expect(screen.queryByText(/已观测有效并行/)).not.toBeInTheDocument();
});
it('legacy terminal history still reports missing batch metadata', async () => {
 currentJobs = [{ id: 'job', state: 'SUCCEEDED', candidates: [] }];
 await mount(); expect(screen.getByText('历史任务未记录实际并行信息')).toBeInTheDocument();
});

it('legacy reads another UI retry child from server with no local session receipt', async () => {
 const parent = { id: 'parent', state: 'FAILED', memoryReleased: true, candidates: [], imageBatch: { candidateCount: 8, parallelLimit: 2, candidates: [4,5,6,7].map(index => ({ index, state: 'CANCELED' })) } };
 currentJobs = [parent, { id: 'react-child', retryOf: 'parent', state: 'PREPARED', parameters: { candidate_indices: [4, 5] }, candidates: [] }];
 await mount();
 expect(screen.getByRole('button', { name: '查看重拍任务 · 候选 5、6' })).toBeInTheDocument();
 expect(screen.queryByRole('checkbox', { name: '重拍候选 5' })).not.toBeInTheDocument();
 expect(screen.queryByRole('checkbox', { name: '重拍候选 6' })).not.toBeInTheDocument();
 expect(screen.getByRole('checkbox', { name: '重拍候选 7' })).toBeEnabled();
 expect(screen.getByRole('checkbox', { name: '重拍候选 8' })).toBeEnabled();
 expect(post).not.toHaveBeenCalled();
});
