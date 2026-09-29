// @vitest-environment jsdom
import '@testing-library/jest-dom/vitest';
import userEvent from '@testing-library/user-event';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { TaskPanel, taskDestination, type StudioTask } from './TaskPanel';
import { get, post } from '@/lib/api';
vi.mock('@/lib/api', () => ({ get: vi.fn(), post: vi.fn(), segment: encodeURIComponent }));
const image: StudioTask = { id: 'image1', kind: 'image', projectId: 'p1', projectTitle: '项目甲', targetName: '人物参考', targetType: 'entity', targetId: 'entity1', targetRevisionId: 'rev1', state: 'QUEUED', stage: 'QUEUED', queuePosition: 2, resultCount: 0, createdAt: 1788846469, cancelPath: '/reference-jobs/image1/cancel' };
const video: StudioTask = { id: 'video1', kind: 'video', projectId: 'p2', projectTitle: '项目乙', episodeId: 'e2', targetName: '镜头二', targetType: 'shot', targetId: 'shot2', state: 'SUCCEEDED', resultCount: 1, memoryReleased: true };
const ctx: any = { project: { id: 'p1', title: '项目甲' }, notify: vi.fn(), capabilities: {} };
async function choose(label: string, name: string) { const user = userEvent.setup(); await user.click(screen.getByRole('combobox', { name: label })); await user.click(await screen.findByRole('option', { name })); }
beforeEach(() => {
 for (const method of ['hasPointerCapture', 'setPointerCapture', 'releasePointerCapture', 'scrollIntoView']) Object.defineProperty(HTMLElement.prototype, method, { configurable: true, value: () => false }); vi.clearAllMocks(); vi.mocked(get).mockResolvedValue({ tasks: [image, video] }); vi.mocked(post).mockResolvedValue({}); });
afterEach(() => { cleanup(); vi.useRealTimers(); });
describe('模型待复用状态', () => {
 it('资源收尾阶段兼容释放与驻留交接，不承诺物理释放', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...image, state: 'RUNNING', stage: 'RELEASING' }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText('正在处理模型资源')).toBeVisible();
  expect(screen.queryByText('正在释放模型资源')).toBeNull();
  expect(post).not.toHaveBeenCalled();
 });
 it('排队说明允许前序完成后交接资源，不宣称必须先物理释放', async () => {
  const waitReason = '任务已保存，等待前序制作完成并交接资源；不会抢占现有任务。';
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...image, waitReason }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText(waitReason)).toBeVisible();
  expect(screen.getByText('队列第 2 位')).toBeVisible();
  expect(screen.queryByText(/等待前序制作完成并释放资源/)).toBeNull();
  expect(post).not.toHaveBeenCalled();
 });
 it('完成但暂留时不误报等待释放或已经释放', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...video, gatewayJobId: 'gateway-resident', memoryReleased: false, resourceStatus: 'resident', resourceSettled: true }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText(/生成完成，模型暂留待复用；空闲5分钟后自动释放/)).toBeVisible();
  expect(screen.queryByText('任务已结束，仍在等待确认模型资源释放。')).toBeNull();
  expect(screen.queryByText(/模型资源已释放/)).toBeNull();
  expect(post).not.toHaveBeenCalled();
 });
 it('cleanupRequired不被resident标签掩盖', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...video, memoryReleased: false, resourceStatus: 'resident', resourceSettled: true, cleanupRequired: true }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText('任务仍需清理资源，尚未确认释放。')).toBeVisible();
  expect(screen.queryByText(/生成完成，模型暂留待复用/)).toBeNull();
 });
});
describe('服务端任务中心', () => {
 it('默认当前项目，过滤不写入，切全部项目重新查询', async () => {
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  await screen.findByText('人物参考'); expect(get).toHaveBeenCalledWith('/tasks?projectId=p1'); expect(screen.getByText('队列第 2 位')).toBeVisible();
  await choose('制作类型', '视频');
  expect(screen.queryByText('人物参考')).toBeNull(); expect(screen.getByText('镜头二')).toBeVisible();
  await choose('任务状态', '需要处理'); expect(screen.getByText('这个范围内还没有匹配的任务')).toBeVisible();
  await choose('任务范围', '全部项目'); await waitFor(() => expect(get).toHaveBeenCalledWith('/tasks')); expect(post).not.toHaveBeenCalled();
 });
 it('取消先确认，成功只标记请求已发送，直到服务端给出终态', async () => {
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  fireEvent.click(await screen.findByRole('button', { name: '取消任务' })); expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '确认取消任务' })); await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
  expect(post).toHaveBeenCalledWith('/reference-jobs/image1/cancel'); expect(await screen.findByRole('button', { name: '取消请求已发送' })).toBeDisabled();
  expect(screen.queryByText('已取消', { selector: '[data-slot=badge]' })).toBeNull();
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...image, state: 'CANCELED' }] }); fireEvent.click(screen.getByRole('button', { name: '刷新任务' }));
  await waitFor(() => expect(screen.queryByRole('button', { name: '取消请求已发送' })).toBeNull()); expect(screen.getByText('已取消', { selector: '[data-slot=badge]' })).toBeVisible();
 });
 it('取消失败不假报成功，也不会自动重试', async () => {
  vi.mocked(post).mockRejectedValue(new Error('网关暂不可用')); render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  fireEvent.click(await screen.findByRole('button', { name: '取消任务' })); fireEvent.click(screen.getByRole('button', { name: '确认取消任务' }));
  expect(await screen.findByText('网关暂不可用')).toBeVisible(); expect(screen.getByRole('button', { name: '确认取消任务' })).toBeDisabled();
  expect(ctx.notify).not.toHaveBeenCalled(); expect(post).toHaveBeenCalledTimes(1);
 });
 it('UNKNOWN待核查，断连保留旧列表并标记可能过时', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...image, state: 'UNKNOWN', stage: '', queuePosition: undefined }] }); render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  await screen.findByText('任务结果尚未确认，请核查原任务，避免重复提交。'); expect(screen.queryByText(/队列第/)).toBeNull();
  vi.mocked(get).mockRejectedValue(new Error('连接断开')); fireEvent.click(screen.getByRole('button', { name: '刷新任务' }));
  expect(await screen.findByText(/以下保留上次读取的任务/)).toBeVisible(); expect(screen.getByText('人物参考')).toBeVisible(); expect(post).not.toHaveBeenCalled();
 });
 it('隐藏停止轮询，重新打开重新读取，轮询只有GET', async () => {
  vi.useFakeTimers(); const { rerender } = render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  await act(async () => {}); expect(get).toHaveBeenCalledTimes(1); await act(async () => { vi.advanceTimersByTime(5000); }); expect(get).toHaveBeenCalledTimes(2);
  rerender(<TaskPanel ctx={ctx} active={false} onOpenTask={vi.fn()}/>); await act(async () => { vi.advanceTimersByTime(15000); }); expect(get).toHaveBeenCalledTimes(2);
  rerender(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>); await act(async () => {}); expect(get).toHaveBeenCalledTimes(3); expect(post).not.toHaveBeenCalled();
 });
 it('旧查询不会覆盖切换后的全部项目列表', async () => {
  let resolveOld: (data: any) => void = () => {}; vi.mocked(get).mockImplementation(path => path.includes('?') ? new Promise(resolve => { resolveOld = resolve; }) : Promise.resolve({ tasks: [video] }));
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>); await choose('任务范围', '全部项目'); await screen.findByText('镜头二');
  await act(async () => { resolveOld({ tasks: [image] }); }); expect(screen.queryByText('人物参考')).toBeNull(); expect(screen.getByText('镜头二')).toBeVisible();
 });
 it('结果导航携带原目标，不自动选用', async () => {
  const open = vi.fn(); render(<TaskPanel ctx={ctx} active onOpenTask={open}/>); fireEvent.click(await screen.findByRole('button', { name: '查看结果' })); expect(open).toHaveBeenCalledWith(video); expect(post).not.toHaveBeenCalled();
  expect(taskDestination(image)).toEqual({ page: 'images', params: { jobId: 'image1', target: 'entity:rev1', entityId: 'entity1' } });
  expect(taskDestination(video)).toEqual({ page: 'video', params: { jobId: 'video1', shotId: 'shot2', view: 'review' } });
 });
});


describe('原任务显式核查与资源反馈', () => {
 it('只有点击核查才POST原任务刷新，失败不伪造状态', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...image, refreshPath: '/reference-jobs/image1/refresh', state: 'SUBMISSION_UNKNOWN', stage: 'SUBMISSION_UNKNOWN' }] });
  vi.mocked(post).mockRejectedValue(new Error('核查服务断开'));
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText('提交待核查', { selector: '[data-slot=badge]' })).toBeVisible();
  expect(post).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole('button', { name: '核查原任务' }));
  await waitFor(() => expect(ctx.notify).toHaveBeenCalledWith('核查服务断开', 'error'));
  expect(post).toHaveBeenCalledWith('/reference-jobs/image1/refresh');
  expect(screen.getByText('提交待核查', { selector: '[data-slot=badge]' })).toBeVisible();
 });
 it('终态没有释放资源须提示，未知阶段不直接展示英文内部值', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...video, gatewayJobId: 'accepted-gateway-job', memoryReleased: false, stage: 'FUTURE_INTERNAL_PHASE' }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText('任务已结束，仍在等待确认模型资源释放。')).toBeVisible();
  expect(screen.getByText('服务状态待核查')).toBeVisible();
  expect(screen.queryByText('FUTURE_INTERNAL_PHASE')).toBeNull();
 });
});


it('图片页的查看原任务带入类型并突出原任务，后续刷新不反复抢滚动', async () => {
 const scroll = vi.spyOn(HTMLElement.prototype, 'scrollIntoView');
 render(<TaskPanel ctx={{ ...ctx, params: { kind: 'image', jobId: 'image1' } }} active onOpenTask={vi.fn()}/>);
 await screen.findByText('人物参考');
 expect(screen.queryByText('镜头二')).toBeNull();
 expect(screen.getByRole('article')).toHaveClass('border-primary/60');
 expect(scroll).toHaveBeenCalledTimes(1);
 fireEvent.click(screen.getByRole('button', { name: '刷新任务' }));
 await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
 expect(scroll).toHaveBeenCalledTimes(1);
});


describe('未派发与未知提交的资源提示边界', () => {
 it.each(['BLOCKED', 'CANCELED'])('无 Gateway 身份的 %s 不误报等待资源释放', async state => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...video, state, gatewayJobId: null, memoryReleased: false }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  await screen.findByText('镜头二');
  expect(screen.queryByText('任务已结束，仍在等待确认模型资源释放。')).toBeNull();
  expect(screen.queryByText(/模型资源已释放/)).toBeNull();
 });
 it('未知提交没有 Gateway ID 也不能推断模型未被占用', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...video, state: 'SUBMISSION_UNKNOWN', gatewayJobId: null, memoryReleased: false }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText('任务结果尚未确认，请核查原任务，避免重复提交。')).toBeVisible();
  expect(screen.queryByText(/未占用模型资源|模型资源已释放/)).toBeNull();
 });
 it('后端明确要求清理时，无 Gateway ID 仍保留清理提示', async () => {
  vi.mocked(get).mockResolvedValue({ tasks: [{ ...video, state: 'INTERRUPTED', gatewayJobId: null, memoryReleased: false, cleanupRequired: true }] });
  render(<TaskPanel ctx={ctx} active onOpenTask={vi.fn()}/>);
  expect(await screen.findByText('任务仍需清理资源，尚未确认释放。')).toBeVisible();
 });
});
