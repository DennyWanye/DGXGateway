// @vitest-environment jsdom
import { useState } from 'react';
import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';
import { get, post } from '@/lib/api';
import * as workspace from '@/lib/workspace';

vi.mock('@/lib/api', () => ({ get: vi.fn(), post: vi.fn(), segment: encodeURIComponent, mediaUrl: (pid: string, aid: string) => `/media/${pid}/${aid}` }));
vi.mock('sonner', () => ({ Toaster: () => null, toast: Object.assign(vi.fn(), { error: vi.fn(), success: vi.fn() }) }));
vi.mock('./ProductionPanel', () => ({ ProductionPanel: ({ ctx, mode }: any) => <div data-testid="production-panel">{mode}<button onClick={() => ctx.openCanvas('shot-1')}>测试打开画布</button></div> }));
vi.mock('./VideoPanel', () => ({ VideoPanel: ({ ctx }: any) => {
  const [value, setValue] = useState(''); const [busy, setBusy] = useState(false);
  return <div data-testid="video-panel" data-dirty={!!value} aria-busy={busy}><button onClick={() => setBusy(value => !value)}>测试切换保存状态</button><label>测试视频草稿<input aria-label="测试视频草稿" value={value} onChange={event => setValue(event.target.value)} /></label><output data-testid="video-context">{ctx.project.id}/{ctx.episode?.id}</output><button onClick={() => void ctx.refresh()}>测试子面板刷新</button></div>;
} }));
vi.mock('./EditPanel', () => ({ EditPanel: () => <div data-testid="edit-panel">剪辑面板</div> }));
vi.mock('./SongPanel', () => ({ SongPanel: () => <div data-testid="song-panel">歌曲面板</div> }));

const projects = [{ id: 'p1', title: '测试项目甲', width: 1344, height: 768, fps: 24, revision: 1 }, { id: 'p2', title: '测试项目乙', width: 1344, height: 768, fps: 24, revision: 1 }];
const episodes = [{ id: 'e1', title: '甲第一集' }, { id: 'e2', title: '甲第二集' }];
function fixture(path: string): any {
  if (path === '/tasks' || path.startsWith('/tasks?')) return { tasks: [{ id: 'task2', kind: 'video', projectId: 'p2', projectTitle: '测试项目乙', episodeId: 'e3', state: 'SUCCEEDED', resultCount: 1, targetName: '乙项目视频' }] };
  if (path === '/projects') return { projects };
  if (path === '/capabilities') return { enabled: true, videoBatchEnabled: true };
  if (path.endsWith('/entities')) return { entities: [] };
  if (path === '/projects/p1') return { project: projects[0], episodes, assets: [] };
  if (path === '/projects/p2') return { project: projects[1], episodes: [{ id: 'e3', title: '乙第一集' }], assets: [] };
  if (path.startsWith('/episodes/')) return { episode: { id: path.split('/').at(-1), title: path.endsWith('e3') ? '乙第一集' : episodes.find(e => path.endsWith(e.id))?.title, revision: 1 }, shots: [] };
  throw new Error(`unexpected GET ${path}`);
}
async function choose(label: string, name: string) {
  const user = userEvent.setup();
  await user.click(screen.getByRole('combobox', { name: label }));
  await user.click(await screen.findByRole('option', { name }));
}
async function mountVideo() {
  render(<App />);
  await screen.findByRole('heading', { name: '测试项目甲' });
  fireEvent.click(screen.getByRole('button', { name: '生成与审片' }));
  return screen.getByLabelText('测试视频草稿') as HTMLInputElement;
}
beforeEach(() => {
  vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear();
  Object.defineProperty(window,'matchMedia',{configurable:true,value:vi.fn(()=>({matches:false,addEventListener:vi.fn(),removeEventListener:vi.fn()}))});
  vi.mocked(get).mockImplementation(async path => fixture(path));
  vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => {});
  Object.defineProperty(HTMLElement.prototype, 'hasPointerCapture', { configurable: true, value: () => false });
  Object.defineProperty(HTMLElement.prototype, 'setPointerCapture', { configurable: true, value: () => {} });
  Object.defineProperty(HTMLElement.prototype, 'releasePointerCapture', { configurable: true, value: () => {} });
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: () => {} });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); history.replaceState({}, '', '/'); });

describe('主壳草稿与上下文交互（子面板模拟）', () => {
  it('explicit discard removes the cached current draft while retaining durable and foreign drafts', async () => {
    const input = await mountVideo();
    fireEvent.change(input, {target:{value:'本集临时编辑'}});
    sessionStorage.setItem('dgx.drama.edit.draft:p1/e1','cached temporary edit');
    sessionStorage.setItem('dgx.drama.edit.draft:p1/e2','another episode');
    sessionStorage.setItem('dgx.drama.video.pending.v1:p1:e1','original request identity');
    await choose('当前项目','测试项目乙');
    fireEvent.click(await screen.findByRole('button',{name:'放弃草稿并继续'}));
    await waitFor(()=>expect(screen.getByTestId('video-context')).toHaveTextContent('p2/e3'));
    expect(sessionStorage.getItem('dgx.drama.edit.draft:p1/e1')).toBeNull();
    expect(sessionStorage.getItem('dgx.drama.edit.draft:p1/e2')).toBe('another episode');
    expect(sessionStorage.getItem('dgx.drama.video.pending.v1:p1:e1')).toBe('original request identity');
  });
  it('普通页面切换保留已挂载的输入草稿，返回不重建播放器容器', async () => {
    const input = await mountVideo(), panel = screen.getByTestId('video-panel');
    fireEvent.change(input, { target: { value: '尚未提交的动作提示词' } });
    fireEvent.click(screen.getByRole('button', { name: '剪辑与导出' }));
    expect(screen.getByTestId('video-panel')).toBe(panel);
    expect(input.value).toBe('尚未提交的动作提示词');
    expect(panel.closest('section')?.hidden).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '生成与审片' }));
    expect(screen.getByLabelText('测试视频草稿')).toBe(input);
    expect(panel.closest('section')?.hidden).toBe(false);
  });
  it('有草稿切项目先确认，取消保持项目和草稿，确认后才读取新项目', async () => {
    const input = await mountVideo();
    fireEvent.change(input, { target: { value: '保留我的草稿' } });
    vi.mocked(get).mockClear();
    await choose('当前项目', '测试项目乙');
    await screen.findByRole('dialog');
    expect(get).not.toHaveBeenCalled();
    expect(screen.getByTestId('video-context').textContent).toBe('p1/e1');
    fireEvent.click(screen.getByRole('button', { name: '返回继续编辑' }));
    expect(input.value).toBe('保留我的草稿');
    await choose('当前项目', '测试项目乙');
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
    await waitFor(() => expect(screen.getByTestId('video-context').textContent).toBe('p2/e3'));
    expect(get).toHaveBeenCalledWith('/projects/p2');
    expect((screen.getByLabelText('测试视频草稿') as HTMLInputElement).value).toBe('');
  });
  it('有草稿切分集取消保持，确认后才加载目标分集', async () => {
    const input = await mountVideo(); fireEvent.change(input, { target: { value: '第一集草稿' } });
    vi.mocked(get).mockClear();
    await choose('当前分集', '甲第二集');
    await screen.findByRole('dialog');
    expect(get).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '返回继续编辑' }));
    expect(input.value).toBe('第一集草稿');
    await choose('当前分集', '甲第二集');
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
    await waitFor(() => expect(screen.getByTestId('video-context').textContent).toBe('p1/e2'));
    expect(get).toHaveBeenCalledWith('/episodes/e2');
  });
  it('首次加载失败显示错误和重试入口，重试成功才挂载业务面板', async () => {
    vi.mocked(get).mockImplementationOnce(async () => { throw new Error('业务服务暂时不可用'); });
    render(<App />);
    fireEvent.click(await screen.findByRole('button', { name: '重新连接' }));
    await screen.findByRole('heading', { name: '测试项目甲' });
    expect(screen.getByTestId('video-context').textContent).toBe('p1/e1');
  });
  it('已有项目加载失败不卸载当前草稿，重试成功保留原输入节点', async () => {
    const input = await mountVideo(); fireEvent.change(input, { target: { value: '失败也不要丢失' } });
    const panel = screen.getByTestId('video-panel');
    vi.mocked(get).mockImplementation(async path => { if (path === '/projects/p2') throw new Error('新项目暂不可用'); return fixture(path); });
    await choose('当前项目', '测试项目乙');
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
    const retry = await screen.findByRole('button', { name: '重新连接' });
    expect(screen.getByTestId('video-panel')).toBe(panel);
    expect(input.value).toBe('失败也不要丢失');
    fireEvent.click(retry);
    await waitFor(() => expect(screen.queryByRole('button', { name: '重新连接' })).toBeNull());
    expect(screen.getByTestId('video-panel')).toBe(panel);
    expect(input.value).toBe('失败也不要丢失');
  });
  it('在途保存期间禁止切项目与分集，提示等待回执后才可继续', async () => {
    await mountVideo();
    fireEvent.click(screen.getByRole('button', { name: '测试切换保存状态' }));
    vi.mocked(get).mockClear();
    await choose('当前项目', '测试项目乙');
    expect(get).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(screen.getByTestId('video-context').textContent).toBe('p1/e1');
    expect(screen.getByText('正在处理当前操作，请等待回执后再切换项目或分集。')).toBeTruthy();
    await choose('当前分集', '甲第二集');
    expect(get).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '测试切换保存状态' }));
    await choose('当前分集', '甲第二集');
    await waitFor(() => expect(screen.getByTestId('video-context').textContent).toBe('p1/e2'));
  });
  it('旧项目的延迟刷新回执不能覆盖已经切换的新项目', async () => {
    await mountVideo();
    let resolveOld: (value: any) => void = () => {};
    let delay = true;
    vi.mocked(get).mockImplementation(async path => {
      if (path === '/projects/p1' && delay) { delay = false; return new Promise(resolve => { resolveOld = resolve; }); }
      return fixture(path);
    });
    fireEvent.click(screen.getByRole('button', { name: '刷新项目数据' }));
    await choose('当前项目', '测试项目乙');
    await waitFor(() => expect(screen.getByTestId('video-context').textContent).toBe('p2/e3'));
    await act(async () => resolveOld({ ...fixture('/projects/p1'), project: { ...projects[0], title: '迟到的旧项目内容' } }));
    expect(screen.getByTestId('video-context').textContent).toBe('p2/e3');
    expect(screen.queryByText('迟到的旧项目内容')).toBeNull();
  });
});

describe('ComfyUI 嵌入消息来源和画布保存转发', () => {
  it('拒绝异源和非parent消息，仅同源parent消息发出保存事件并刷新', async () => {
    await mountVideo();
    const saved = vi.fn(); window.addEventListener('dgx-drama-shot-saved', saved);
    vi.mocked(get).mockClear();
    try {
      fireEvent(window, new MessageEvent('message', { source: window.parent, origin: 'https://untrusted.example', data: { type: 'dgx-studio:shot-saved', shotId: 'bad-origin' } }));
      fireEvent(window, new MessageEvent('message', { source: null, origin: location.origin, data: { type: 'dgx-studio:shot-saved', shotId: 'bad-source' } }));
      expect(saved).not.toHaveBeenCalled(); expect(get).not.toHaveBeenCalled();
      fireEvent(window, new MessageEvent('message', { source: window.parent, origin: location.origin, data: { type: 'dgx-studio:shot-saved', shotId: 'shot-1' } }));
      expect(saved).toHaveBeenCalledTimes(1);
      expect((saved.mock.calls[0][0] as CustomEvent).detail).toEqual({ shotId: 'shot-1' });
      await waitFor(() => expect(get).toHaveBeenCalledWith('/projects/p1'));
    } finally { window.removeEventListener('dgx-drama-shot-saved', saved); }
  });
  it('隐藏消息仅接受同源parent，暂停媒体但不丢失草稿', async () => {
    const input = await mountVideo(); fireEvent.change(input, { target: { value: '关闭后保留' } });
    const video = document.createElement('video'); document.body.append(video);
    const pause = vi.spyOn(video, 'pause');
    try {
      fireEvent(window, new MessageEvent('message', { source: null, origin: location.origin, data: { type: 'dgx-studio:hidden' } }));
      expect(pause).not.toHaveBeenCalled();
      fireEvent(window, new MessageEvent('message', { source: window.parent, origin: location.origin, data: { type: 'dgx-studio:hidden' } }));
      expect(pause).toHaveBeenCalledTimes(1);
      expect(input.value).toBe('关闭后保留');
    } finally { video.remove(); }
  });
});

 it('窄屏关闭导航后设为inert，展开后Escape还原关闭状态',async()=>{
  vi.mocked(window.matchMedia).mockReturnValue({matches:true,addEventListener:vi.fn(),removeEventListener:vi.fn()} as any);
  render(<App/>);await screen.findByRole('heading',{name:'测试项目甲'});
  const sidebar=document.querySelector('aside')!;expect(sidebar.hasAttribute('inert')).toBe(true);
  fireEvent.click(screen.getByRole('button',{name:'展开导航'}));
  expect(sidebar.hasAttribute('inert')).toBe(false);expect(sidebar.getAttribute('role')).toBe('dialog');
  fireEvent.keyDown(document,{key:'Escape'});expect(sidebar.hasAttribute('inert')).toBe(true);
 });

it('opens a different production page at its beginning without discarding the old draft', async () => {
  const input = await mountVideo();
  fireEvent.change(input, {target:{value:'保留动作草稿'}});
  const main = screen.getByRole('main');
  main.scrollTop = 600;
  fireEvent.click(screen.getByRole('button', {name:/^剪辑与导出$/}));
  expect(main.scrollTop).toBe(0);
  expect(input.value).toBe('保留动作草稿');
});

it('P6 story navigation is reachable and keeps existing production pages mounted', async () => {
  render(<App />);
  await screen.findByRole('heading', { name: '测试项目甲' });
  fireEvent.click(screen.getByRole('button', { name: /^故事与剧本$/ }));
  expect(await screen.findByText('故事与剧本服务尚未启用。现有资料、镜头和剪辑功能继续可用。')).toBeVisible();
  expect(screen.getByTestId('production-panel')).toBeInTheDocument();
  expect(screen.getByTestId('video-panel')).toBeInTheDocument();
  expect(screen.getByTestId('edit-panel')).toBeInTheDocument();
});
it('restored workspace cannot send same shot ID to the original shared ComfyUI canvas', async () => {
  history.replaceState({}, '', `/?restored=${'a'.repeat(32)}`);
  const send = vi.spyOn(window.parent, 'postMessage');
  try {
    render(<App />);
    await screen.findByTestId('production-panel');
    fireEvent.click(screen.getByRole('button', {name:'测试打开画布', hidden:true}));
    expect(await screen.findByText('恢复工作区请使用镜头表单编辑；共享 ComfyUI 画布尚未绑定恢复工作区。')).toBeInTheDocument();
    expect(send).not.toHaveBeenCalledWith(expect.objectContaining({type:'dgx-studio:canvas'}),expect.anything());
  } finally { send.mockRestore(); history.replaceState({}, '', '/'); }
});

describe('恢复工作区常驻提示与受保护返回', () => {
  it('原工作区没有恢复提示，返回链接移除全部恢复层且保留其他参数', async () => {
    await mountVideo();
    expect(screen.queryByRole('region', { name: '恢复工作区状态' })).toBeNull();
    history.replaceState({}, '', `/studio/index.html?dramaUI=studio&restored=${'a'.repeat(32)}/${'b'.repeat(32)}#saved`);
    expect(workspace.originalWorkspaceUrl()).toBe('/studio/index.html?dramaUI=studio#saved');
  });

  it('隔离提示加载前已可见，项目加载失败也不消失', async () => {
    history.replaceState({}, '', `/?restored=${'a'.repeat(32)}`);
    vi.mocked(get).mockRejectedValue(new Error('fixture offline'));
    render(<App />);
    expect(screen.getByRole('region', { name: '恢复工作区状态' })).toBeVisible();
    expect(screen.getByText('恢复工作区 · 模型任务已隔离')).toBeVisible();
    await screen.findByRole('button', { name: '重新连接' });
    expect(screen.getByRole('button', { name: '返回原工作区' })).toBeVisible();
  });

  it('query_only说明跨所有制作页常驻且不新增能力轮询', async () => {
    history.replaceState({}, '', `/?restored=${'a'.repeat(32)}`);
    vi.mocked(get).mockImplementation(async path => path === '/capabilities' ? { enabled: true, restoreMode: 'query_only' } : fixture(path));
    await mountVideo();
    const banner = screen.getByRole('region', { name: '恢复工作区状态' });
    expect(banner).toHaveTextContent('已启用原任务只读核查。不会重新生成、取消原任务或接管模型。');
    const count = vi.mocked(get).mock.calls.filter(([path]) => path === '/capabilities').length;
    for (const label of ['制作概览','故事与剧本','人物与场景','图片制作','镜头设计','生成与审片','备份与恢复','剪辑与导出']) {
      fireEvent.click(screen.getByRole('button', { name: new RegExp(`^${label}$`) }));
      expect(banner).toBeVisible();
      expect(banner.closest('main')).toBeNull(); // outside the scrolling page content
    }
    expect(vi.mocked(get).mock.calls.filter(([path]) => path === '/capabilities')).toHaveLength(count);
  });

  it('未保存返回先确认，取消保留输入与离开保护，确认后当前页面返回一次', async () => {
    history.replaceState({}, '', `/?restored=${'a'.repeat(32)}`);
    const leave = vi.spyOn(workspace, 'returnToOriginalWorkspace').mockImplementation(() => {});
    const open = vi.spyOn(window, 'open');
    const input = await mountVideo();
    fireEvent.change(input, { target: { value: '恢复副本未保存草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
    expect(await screen.findByRole('dialog')).toHaveTextContent('切换项目、分集或工作区');
    expect(leave).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: '返回继续编辑' }));
    expect(input.value).toBe('恢复副本未保存草稿');
    const beforeCancel = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(beforeCancel);
    expect(beforeCancel.defaultPrevented).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
    expect(leave).toHaveBeenCalledTimes(1);
    expect(open).not.toHaveBeenCalled();
    const afterConfirm = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(afterConfirm);
    expect(afterConfirm.defaultPrevented).toBe(false); // no duplicate native prompt
  });

  it('在途保存阻止返回，回执结束后干净表单直接同页返回', async () => {
    history.replaceState({}, '', `/?restored=${'a'.repeat(32)}`);
    const leave = vi.spyOn(workspace, 'returnToOriginalWorkspace').mockImplementation(() => {});
    await mountVideo();
    fireEvent.click(screen.getByRole('button', { name: '测试切换保存状态' }));
    fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
    expect(leave).not.toHaveBeenCalled();
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '测试切换保存状态' }));
    fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
    expect(leave).toHaveBeenCalledTimes(1);
  });

  it('明确放弃后也抑制子面板已有beforeunload，取消时仍执行子面板保护', async () => {
    history.replaceState({}, '', `/?restored=${'a'.repeat(32)}`);
    // Edit/Story/Video/Production/Backup own their own native unload guards.
    // Register before App to reproduce child effects running before parent effects.
    const childGuard = vi.fn((event: Event) => event.preventDefault());
    window.addEventListener('beforeunload', childGuard);
    const leave = vi.spyOn(workspace, 'returnToOriginalWorkspace').mockImplementation(() => {});
    try {
      const input = await mountVideo();
      fireEvent.change(input, { target: { value: '3秒未保存剪辑' } });
      fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
      fireEvent.click(await screen.findByRole('button', { name: '返回继续编辑' }));
      const canceled = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(canceled);
      expect(canceled.defaultPrevented).toBe(true);
      expect(childGuard).toHaveBeenCalledTimes(1);
      childGuard.mockClear();
      fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
      fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
      const confirmed = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(confirmed);
      expect(leave).toHaveBeenCalledTimes(1);
      expect(confirmed.defaultPrevented).toBe(false);
      expect(childGuard).not.toHaveBeenCalled();
      const later = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(later);
      expect(later.defaultPrevented).toBe(true); // the allowance is consumed once
      expect(childGuard).toHaveBeenCalledTimes(1);
    } finally { window.removeEventListener('beforeunload', childGuard); }
  });

  it('跳转未发生时新编辑或短时到期都会恢复离开保护', async () => {
    history.replaceState({}, '', `/?restored=${'a'.repeat(32)}`);
    vi.spyOn(workspace, 'returnToOriginalWorkspace').mockImplementation(() => {});
    const input = await mountVideo();
    fireEvent.change(input, { target: { value: '3秒' } });
    fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
    fireEvent.change(input, { target: { value: '新的2秒草稿' } });
    const changed = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(changed);
    expect(changed.defaultPrevented).toBe(true);
    vi.useFakeTimers();
    try {
      fireEvent.click(screen.getByRole('button', { name: '返回原工作区' }));
      fireEvent.click(screen.getByRole('button', { name: '放弃草稿并继续' }));
      act(() => { vi.advanceTimersByTime(1001); });
      const expired = new Event('beforeunload', { cancelable: true });
      window.dispatchEvent(expired);
      expect(expired.defaultPrevented).toBe(true);
    } finally { vi.useRealTimers(); }
  });
});


describe('任务中心跨项目结果导航', () => {
  it('取消草稿保护保留输入，明确放弃后加载正确分集并进入审片页', async () => {
    const input = await mountVideo();
    fireEvent.change(input, { target: { value: '不能丢失的草稿' } });
    fireEvent.click(screen.getByRole('button', { name: '生成任务' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看结果' }));
    await screen.findByRole('dialog');
    expect(get).not.toHaveBeenCalledWith('/projects/p2');
    fireEvent.click(screen.getByRole('button', { name: '返回继续编辑' }));
    expect(input.value).toBe('不能丢失的草稿');
    fireEvent.click(screen.getByRole('button', { name: '查看结果' }));
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
    await waitFor(() => expect(screen.getByTestId('video-context').textContent).toBe('p2/e3'));
    expect(screen.getByRole('button', { name: '生成与审片' })).toHaveAttribute('aria-current', 'page');
  });
  it('结果所属项目加载失败保持任务页和原草稿，不假导航到其他项目', async () => {
    const input = await mountVideo();
    fireEvent.change(input, { target: { value: '保持原始内容' } });
    vi.mocked(get).mockImplementation(async path => { if(path === '/projects/p2') throw new Error('结果项目暂不可用'); return fixture(path); });
    fireEvent.click(screen.getByRole('button', { name: '生成任务' }));
    fireEvent.click(await screen.findByRole('button', { name: '查看结果' }));
    fireEvent.click(await screen.findByRole('button', { name: '放弃草稿并继续' }));
    await screen.findByRole('button', { name: '重新连接' });
    expect(screen.getByRole('button', { name: '生成任务' })).toHaveAttribute('aria-current', 'page');
    expect(input.value).toBe('保持原始内容');
    expect(screen.getByTestId('video-context').textContent).toBe('p1/e1');
  });
});


describe('MV导航', () => {
  it('短剧项目不显示歌曲项', async () => {
    render(<App />);
    await screen.findByRole('heading', { name: '测试项目甲' });
    expect(screen.getByLabelText('短剧制作步骤')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '歌曲' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: '故事与歌词' })).not.toBeInTheDocument();
  });
  it('MV项目显示歌曲导航且创建时提交kind', async () => {
    vi.mocked(get).mockImplementation(async path => path === '/projects/p1'
      ? { project: { ...projects[0], kind: 'mv' }, episodes, assets: [] }
      : fixture(path));
    render(<App />);
    await screen.findByRole('heading', { name: '测试项目甲' });
    expect(screen.getByLabelText('MV制作步骤')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '歌曲' }));
    expect(screen.getByTestId('song-panel')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '新建项目' }));
    fireEvent.change(screen.getByLabelText('项目名称'), { target: { value: '第二个航班' } });
    await choose('作品类型', 'MV');
    vi.mocked(post).mockResolvedValueOnce({ project: { ...projects[0], title: '第二个航班', kind: 'mv' } });
    fireEvent.click(screen.getByRole('button', { name: '创建项目' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/projects', expect.objectContaining({
      title: '第二个航班', kind: 'mv', fps: 24,
    })));
  });
});

describe('项目画幅设置',()=>{
 it('保存用户选定画幅后刷新显示',async()=>{
  render(<App/>);await screen.findByRole('heading',{name:'测试项目甲'});
  fireEvent.click(screen.getByRole('button',{name:'项目画幅设置'}));
  await choose('成片比例','16:9 横屏 · 1920 × 1080');
  vi.mocked(post).mockResolvedValueOnce({});
  vi.mocked(get).mockImplementation(async path=>path==='/projects/p1'?{project:{...projects[0],width:1920,height:1080},episodes,assets:[]}:fixture(path));
  fireEvent.click(screen.getByRole('button',{name:'保存画幅'}));
  await waitFor(()=>expect(post).toHaveBeenCalledWith('/projects/p1/canvas',{width:1920,height:1080,expectedRevision:1}));
  await waitFor(()=>expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  expect(screen.getByRole('button',{name:'项目画幅设置'})).toHaveTextContent('1920 × 1080');
 });
 it('服务拒绝修改时保留设置窗口和原画幅',async()=>{
  render(<App/>);await screen.findByRole('heading',{name:'测试项目甲'});
  fireEvent.click(screen.getByRole('button',{name:'项目画幅设置'}));
  await choose('成片比例','16:9 横屏 · 1920 × 1080');
  vi.mocked(post).mockRejectedValueOnce(new Error('项目已有剪辑时间线'));
  fireEvent.click(screen.getByRole('button',{name:'保存画幅'}));
  await waitFor(()=>expect(screen.getByRole('button',{name:'保存画幅'})).toBeEnabled());
  expect(screen.getByRole('dialog')).toBeInTheDocument();
  fireEvent.click(screen.getByRole('button',{name:'取消'}));
  expect(screen.getByRole('button',{name:'项目画幅设置'})).toHaveTextContent('1344 × 768');
 });
});
