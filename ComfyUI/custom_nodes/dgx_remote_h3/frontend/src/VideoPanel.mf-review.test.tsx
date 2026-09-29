// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import userEvent from '@testing-library/user-event';
import { VideoPanel } from './VideoPanel';
import { get, post } from '@/lib/api';
vi.mock('@/lib/api', () => ({ get: vi.fn(), post: vi.fn(), segment: encodeURIComponent, requestKey: () => 'drama-ui-independent-review', mediaUrl: () => '/media', uploadReferenceAudio: vi.fn() }));
const stable = { contractVersion: 'h3-ref2va-v1', profile: 'heretic-ref2va-v1', requestDurationSeconds: 4 };
const initial = () => ({ revision: 2, currentRevisionId: 'r1', invalidations: [], current: { approved: true, snapshot: { title: '镜头1', action: '向前走', camera: '固定', framing: '全景', prompt: '只用于静态图的湖边构图', approvedReferenceAssetId: 'image', generation: stable } } });
const ctx = () => ({ project: { id: 'qa-review', fps: 24 }, episode: { id: 'ep' }, shots: [{ id: 's1', title: '镜头1', currentRevisionId: 'r1' }], assets: [], entities: [], capabilities: { videoBatchEnabled: true, videoCapabilities: { profiles: [{ id: stable.profile, name: '稳定', workbenchDurationSeconds: [4] }, { id: 'heretic-ref2va-hmnsfw-v1', name: '长视频', workbenchDurationSeconds: [4, 15] }] } }, notify: vi.fn(), refresh: vi.fn(async () => {}), navigate: vi.fn(), openCanvas: vi.fn(), params: { shotId: 's1' } });
beforeEach(() => {
 sessionStorage.clear(); vi.clearAllMocks();
 vi.spyOn(HTMLMediaElement.prototype, "pause").mockImplementation(() => {});
 vi.spyOn(HTMLMediaElement.prototype, "load").mockImplementation(() => {});
 for (const name of ['hasPointerCapture', 'setPointerCapture', 'releasePointerCapture', 'scrollIntoView']) Object.defineProperty(HTMLElement.prototype, name, { configurable: true, value: () => false });
 Object.defineProperty(HTMLElement.prototype, 'checkVisibility', { configurable: true, value: () => true });
 vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
const read = (production: any) => vi.mocked(get).mockImplementation(async path => path.endsWith('/production') ? production : path.endsWith('/production-state') ? { paused: false } : path.endsWith('/video-jobs') ? { jobs: [] } : { shots: [] });
describe('independent MF video failure boundaries', () => {
 it('describes video resource finalization without promising unload', async () => {
  const context = ctx(); context.params = { view: 'review' } as any;
  vi.mocked(get).mockImplementation(async path => path.endsWith('/production') ? initial() : path.endsWith('/video-jobs') ? { jobs: [{ id: 'finishing-job', episodeId: 'ep', state: 'RELEASING', memoryReleased: false, items: [] }] } : path.endsWith('/production-state') ? { paused: false } : { shots: [] });
  render(<VideoPanel ctx={context} />);
  expect(await screen.findByText('正在处理模型资源')).toBeVisible();
  expect(screen.queryByText('正在释放模型资源')).toBeNull();
  expect(post).not.toHaveBeenCalled();
 });
 it('shows completed resident video without claiming resource return', async () => {
  const context = ctx(); context.params = { view: 'review' } as any;
  vi.mocked(get).mockImplementation(async path => path.endsWith('/production') ? initial() : path.endsWith('/video-jobs') ? { jobs: [{ id: 'resident-job', episodeId: 'ep', state: 'SUCCEEDED', memoryReleased: false, resourceStatus: 'resident', resourceSettled: true, items: [] }] } : path.endsWith('/production-state') ? { paused: false } : { shots: [] });
  render(<VideoPanel ctx={context} />);
  expect(await screen.findByText(/生成完成，模型暂留待复用；空闲5分钟后自动释放/)).toBeVisible();
  expect(screen.queryByText(/尚未确认资源归还|资源已归还或未占用/)).toBeNull();
  expect(post).not.toHaveBeenCalled();
 });
 it('uses the stored shot prompt and ignores hidden director notes', async () => {
  const prod = initial(); prod.current.snapshot.action = '';
  read(prod); render(<VideoPanel ctx={ctx()} />);
  expect(await screen.findByText(/只用于静态图的湖边构图/)).toBeTruthy();
  expect(screen.queryByText("固定")).toBeNull();
 });
 it('reports saved generation even when confirming reference and reloading both fail', async () => {
  const context = ctx(); let saved = false;
  vi.mocked(get).mockImplementation(async path => {
   if (path.endsWith('/production')) { if (saved) throw new Error('读取也断网'); return initial(); }
   return path.endsWith('/production-state') ? { paused: false } : path.endsWith('/video-jobs') ? { jobs: [] } : { shots: [] };
  });
  vi.mocked(post).mockImplementation(async path => {
   if (path.endsWith('/revisions')) { saved = true; return { revision: 3, currentRevisionId: 'r2' }; }
   throw new Error('确认参考时断网');
  });
  render(<VideoPanel ctx={context} />);
  await screen.findByText('查看将发给 H3 的内容');
  fireEvent.change(screen.getByRole('spinbutton', { name: '生成视频时长' }), { target: { value: '10' } });
  fireEvent.click(screen.getByRole('button', { name: '保存时长' }));
  await waitFor(() => expect(context.notify).toHaveBeenCalledWith(expect.stringContaining('生成设置已保存'), 'error'));
  expect(vi.mocked(post).mock.calls.map(call => call[0])).toEqual(['/shots/s1/revisions', '/shots/s1/reference-selection']);
 });
 it('never sends a video request if its frozen identity cannot be saved', async () => {
  const context = ctx(); read(initial());
  vi.mocked(post).mockImplementation(async path => path.endsWith('/silence') ? { asset: { id: 'silent' } } : { job: { id: 'job', items: [] } });
  render(<VideoPanel ctx={context} />);
  await screen.findByText('查看将发给 H3 的内容');
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota exceeded'); });
  fireEvent.click(screen.getByRole('button', { name: '提交当前批次' }));
  await waitFor(() => expect(context.notify).toHaveBeenCalledWith(expect.stringMatching(/保存|存储|暂存/), 'error'));
  expect(vi.mocked(post).mock.calls.filter(call => call[0].endsWith('/video-batches'))).toHaveLength(0);
 });
});

describe('independent combined review and pending navigation', () => {
 it('recovers a failed player without discarding review notes or submitting a business operation', async () => {
  const context: any = ctx(); context.params = { view: 'review' };
  const item = { id: 'i1', shotId: 's1', shotRevisionId: 'r1', takeId: 't1', assetId: 'v1', state: 'SUCCEEDED', submissionPhase: 'RECEIVED' };
  vi.mocked(get).mockImplementation(async path => path.endsWith('/production-state') ? { paused: false } : path.endsWith('/video-jobs') ? { jobs: [{ id: 'j1', episodeId: 'ep', state: 'SUCCEEDED', items: [item] }] } : { shots: [] });
  const load = vi.spyOn(HTMLMediaElement.prototype, 'load').mockImplementation(() => {});
  render(<VideoPanel ctx={context} />);
  const notes = await screen.findByLabelText('审片意见');
  fireEvent.change(notes, { target: { value: '先确认播放恢复，再决定是否采用' } });
  const previous = screen.getByLabelText('镜头1 视频候选');
  fireEvent.error(previous);
  expect(screen.getByRole('alert')).toHaveTextContent('视频无法播放');
  fireEvent.click(screen.getByRole('button', { name: '重新加载视频' }));
  expect(screen.getByLabelText('镜头1 视频候选')).not.toBe(previous);
  expect(previous).not.toHaveAttribute('src');
  expect(load).toHaveBeenCalledOnce();
  expect(notes).toHaveValue('先确认播放恢复，再决定是否采用');
  expect(screen.queryByRole('alert')).not.toBeInTheDocument();
  expect(screen.getByRole('status')).toHaveTextContent('播放器已重置');
  expect(post).not.toHaveBeenCalled();
 });
 it('shows confirmed queued cancellation without claiming resource release for uncertain jobs', async () => {
  const context: any = ctx(); context.params = { view: 'review' };
  const item = { id: 'i1', shotId: 's1', state: 'QUEUED', submissionPhase: 'NOT_SUBMITTED' };
  vi.mocked(get).mockImplementation(async path => path.endsWith('/production-state') ? { paused: false } : path.endsWith('/video-jobs') ? { jobs: [
   { id: 'queued', episodeId: 'ep', state: 'CANCELED', items: [item] },
   { id: 'uncertain', episodeId: 'ep', state: 'CANCELED', gatewayJobId: 'original', items: [{ ...item, id: 'i2', submissionPhase: 'REQUEST_STARTED' }] }
  ] } : { shots: [] });
  render(<VideoPanel ctx={context} />);
  await screen.findByText('当前分集 · 排队时取消 · 未占用模型资源');
  expect(screen.getByText('当前分集 · 尚未确认资源归还')).toBeInTheDocument();
  expect(screen.getAllByText('等待派发')).toHaveLength(1);
 });
 it('requires a separate explicit confirmation before selecting an old revision after review succeeds', async () => {
  const context: any = ctx(); context.params = { view: 'review' };
  const shot = { id: 's1', currentRevisionId: 'r2', selectedTakeId: '', takes: [{ id: 'take1', reviewStatus: 'pending', stale: true }] };
  const item = { id: 'item1', shotId: 's1', shotRevisionId: 'r1', takeId: 'take1', assetId: 'video1', state: 'SUCCEEDED', submissionPhase: 'RECEIVED', reviewStatus: 'pending' };
  vi.mocked(get).mockImplementation(async path => path.endsWith('/production') ? { ...initial(), currentRevisionId: 'r2', revision: 8 } : path.endsWith('/production-state') ? { paused: false } : path.endsWith('/video-jobs') ? { jobs: [{ id: 'job1', episodeId: 'ep', state: 'SUCCEEDED', memoryReleased: true, items: [item] }] } : { shots: [shot] });
  vi.mocked(post).mockImplementation(async path => { if (path.endsWith('/review')) shot.takes[0].reviewStatus = 'approved'; return {}; });
  render(<VideoPanel ctx={context} />);
  fireEvent.change(await screen.findByLabelText('审片意见'), { target: { value: '此旧版本动作更自然' } });
  fireEvent.click(screen.getByRole('button', { name: '通过并设为本镜头选片' }));
  await screen.findByRole('dialog', { name: '采用旧修订的视频？' });
  expect(vi.mocked(post).mock.calls.map(call => call[0])).toEqual(['/takes/take1/review']);
  fireEvent.click(screen.getByRole('button', { name: '确认继续' }));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/shots/s1/selection', { takeId: 'take1', expectedRevision: 8, acceptStale: true }));
 });
 it('never replaces an unresolved frozen submission when navigation brings another shot', async () => {
  const context = ctx();
  const frozen = { shots: [{ shotId: 'old-shot', shotRevisionId: 'old-r', audioAssetId: 'old-audio', prompt: '原请求冻结的动作', seed: 7, generation: stable }] };
  sessionStorage.setItem('dgx.drama.video.pending.v1:qa-review:ep', JSON.stringify({ version: 1, projectId: 'qa-review', episodeId: 'ep', key: 'drama-ui-original-frozen', payload: frozen }));
  read(initial());
  vi.mocked(post).mockResolvedValue({ job: { id: 'recovered-job', items: [] } });
  render(<VideoPanel ctx={context} />);
  await screen.findByText('原提交结果仍需确认');
  expect(vi.mocked(get).mock.calls.filter(call => call[0] === '/shots/s1/production')).toHaveLength(0);
  expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '按原请求键确认提交结果' }));
  await waitFor(() => expect(post).toHaveBeenCalledWith('/episodes/ep/video-batches', frozen, 'drama-ui-original-frozen'));
 });
});


describe('text-only video submission', () => {
 it('submits without image approval or creating silent audio, and stays on Ref2VA', async () => {
  const context = ctx(); const prod = initial();
  prod.current.snapshot.approvedReferenceAssetId = ''; prod.current.approved = false;
  prod.current.snapshot.prompt = '纸船在溪流上漂动，镜头跟随';
  read(prod); vi.mocked(post).mockResolvedValue({ job: { id: 'text-job', items: [] } });
  render(<VideoPanel ctx={context} />);
  await screen.findByText('查看将发给 H3 的内容');
  expect(screen.queryByText('H3 Ring4 · 自适应多模态')).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: '提交当前批次' }));
  await waitFor(() => expect(post).toHaveBeenCalled());
  expect(vi.mocked(post).mock.calls).toHaveLength(1);
  expect(vi.mocked(post).mock.calls[0][0]).toBe('/episodes/ep/video-batches');
  const payload: any = vi.mocked(post).mock.calls[0][1];
  expect(payload.shots[0].audioAssetId).toBe('');
  expect(payload.shots[0].generation).toEqual({ contractVersion: 'h3-ref2va-v1', profile: 'heretic-ref2va-hmnsfw-v1', requestDurationSeconds: 4 });
  expect(payload.shots[0].prompt).toContain('纸船');
  expect(payload.shots[0].prompt).toContain('No dialogue');
 });
});
