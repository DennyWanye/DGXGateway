// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { QuickImageReviewPanel } from './QuickImageReviewPanel';
import { get, post, requestKey } from '@/lib/api';
import { selectImageCandidate } from '@/lib/image-selection';

vi.mock('@/lib/api', () => ({
  get: vi.fn(), post: vi.fn(), requestKey: vi.fn(() => 'quick-review-key'),
  segment: encodeURIComponent, mediaUrl: (_project: string, asset: string) => `/media/${asset}`,
}));
vi.mock('@/lib/image-selection', () => ({ selectImageCandidate: vi.fn() }));

const base = (overrides: Record<string, any> = {}) => ({
  id: 'candidate-a', candidateId: 'candidate-a', assetId: 'asset-a', index: 0,
  targetType: 'entity', targetId: 'entity-a', targetRevisionId: 'revision-a',
  currentRevision: 5, currentRevisionId: 'revision-a', targetName: '阿黎',
  role: 'identity_front', jobId: 'job-a', reviewStatus: 'pending', selected: false,
  stale: false, episodeIds: ['episode-a'], episodeTitles: ['第 1 集'], scopeKind: 'episode',
  ...overrides,
});

const context = () => ({
  project: { id: 'project-a' }, episode: { id: 'episode-a' }, refresh: vi.fn().mockResolvedValue(undefined),
  navigate: vi.fn(), notify: vi.fn(), shots: [], assets: [], entities: [], capabilities: {}, params: {}, openCanvas: vi.fn(),
});

let queue: any[] = [];
const mount = (ctx = context()) => {
  vi.mocked(get).mockImplementation(async () => ({ items: queue }));
  render(<QuickImageReviewPanel ctx={ctx as any} active />);
  return ctx;
};
const checkboxFor = (jobId: string) => screen.getByRole('checkbox', { name: new RegExp(jobId) });
const focusRole = (role: string) => fireEvent.click(screen.getAllByRole('button').find(button => button.textContent?.includes(role))!);

beforeEach(() => {
  queue = []; vi.clearAllMocks(); localStorage.clear(); sessionStorage.clear();
  Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: () => {} });
  vi.mocked(post).mockResolvedValue({});
  vi.mocked(selectImageCandidate).mockResolvedValue({ revision: 6 } as any);
});
afterEach(() => cleanup());

describe('FU12 quick image review', () => {
  it('adopts two initially-current slots of one target, advancing the CAS and allowing only its own later write as stale', async () => {
    queue = [base(), base({ id: 'candidate-b', candidateId: 'candidate-b', assetId: 'asset-b', role: 'identity_3q', jobId: 'job-b' })];
    mount();
    await screen.findByText('阿黎 · 正面身份图');
    fireEvent.click(checkboxFor('job-a'));
    focusRole('四分之三侧面');
    fireEvent.click(checkboxFor('job-b'));
    fireEvent.click(screen.getByRole('button', { name: /批量选用勾选图片（2）/ }));
    await waitFor(() => expect(selectImageCandidate).toHaveBeenCalledTimes(2));
    expect(vi.mocked(selectImageCandidate).mock.calls[0][2]).toMatchObject({ expectedRevision: 5, acceptStale: false });
    expect(vi.mocked(selectImageCandidate).mock.calls[1][2]).toMatchObject({ expectedRevision: 6, acceptStale: true });
  });

  it('does not let an already-selected image enter batch adoption', async () => {
    queue = [base({ selected: true, reviewStatus: 'approved' })];
    mount();
    const checkbox = await screen.findByRole('checkbox', { name: /job-a/ });
    expect(checkbox).toBeDisabled();
    expect(screen.getByRole('button', { name: /批量选用勾选图片（0）/ })).toBeDisabled();
    expect(selectImageCandidate).not.toHaveBeenCalled();
  });

  it('keeps the target episode when opening a project-wide candidate from another episode', async () => {
    queue = [base({ targetType: 'shot', targetId: 'shot-other', targetName: '异集镜头', role: 'shot', jobId: 'job-other', episodeIds: ['episode-other'], episodeTitles: ['第 2 集'] })];
    const ctx = mount();
    await screen.findByText('异集镜头 · 本镜头画面');
    fireEvent.click(screen.getByRole('button', { name: '查看目标与参考版本' }));
    expect(ctx.navigate).toHaveBeenCalledWith('shots', { shotId: 'shot-other', episodeId: 'episode-other' });
  });

  it('retains only failed candidates checked after a partial batch failure', async () => {
    queue = [base({ targetType: 'shot', targetId: 'shot-a', targetName: '镜头甲', role: 'shot', jobId: 'job-a' }), base({ id: 'candidate-b', candidateId: 'candidate-b', assetId: 'asset-b', targetType: 'shot', targetId: 'shot-b', targetName: '镜头乙', role: 'shot', jobId: 'job-b' })];
    vi.mocked(selectImageCandidate).mockResolvedValueOnce({ revision: 6 } as any).mockRejectedValueOnce(new Error('修订冲突'));
    mount();
    await screen.findByText('镜头甲 · 本镜头画面');
    fireEvent.click(checkboxFor('job-a'));
    focusRole('镜头乙');
    fireEvent.click(checkboxFor('job-b'));
    fireEvent.click(screen.getByRole('button', { name: /批量选用勾选图片（2）/ }));
    await screen.findByRole('alert');
    expect(screen.getByRole('button', { name: /批量选用勾选图片（1）/ })).toBeEnabled();
    expect(screen.getByRole('alert')).toHaveTextContent('镜头乙');
  });

  it('falls back to an available pending focus after refresh removes the old target', async () => {
    const first = base({ targetName: '镜头甲', targetType: 'shot', targetId: 'shot-a', role: 'shot' });
    const second = base({ id: 'candidate-b', candidateId: 'candidate-b', targetName: '镜头乙', targetType: 'shot', targetId: 'shot-b', role: 'shot', jobId: 'job-b' });
    queue = [first, second];
    mount();
    await screen.findByText('镜头甲 · 本镜头画面');
    focusRole('镜头乙');
    await screen.findByText('镜头乙 · 本镜头画面');
    queue = [first];
    fireEvent.click(screen.getByRole('button', { name: '刷新候选' }));
    await screen.findByText('镜头甲 · 本镜头画面');
  });

  it('sends empty notes for quality review, while rejecting a same-slot batch before any selection write', async () => {
    queue = [base(), base({ id: 'candidate-b', candidateId: 'candidate-b', assetId: 'asset-b', jobId: 'job-b' })];
    mount();
    await screen.findByText('阿黎 · 正面身份图');
    fireEvent.click(checkboxFor('job-a'));
    fireEvent.click(screen.getByRole('button', { name: /批量质量通过/ }));
    await waitFor(() => expect(post).toHaveBeenCalledWith('/projects/project-a/image-reviews', { candidateIds: ['candidate-a'], decision: 'approved', notes: '' }, 'quick-review-key'));
    // Restore the two pending candidates after the review refresh, then assert slot protection.
    fireEvent.click(checkboxFor('job-a'));
    fireEvent.click(checkboxFor('job-b'));
    fireEvent.click(screen.getByRole('button', { name: /批量选用勾选图片（2）/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('同一参考用途只能选用一张');
    expect(selectImageCandidate).not.toHaveBeenCalled();
  });

  it('writes approve, reject, then approve again with a new request key after each confirmed review', async () => {
    queue = [base()];
    vi.mocked(requestKey).mockReturnValueOnce('review-key-1').mockReturnValueOnce('review-key-2').mockReturnValueOnce('review-key-3');
    mount();
    await screen.findByText('阿黎 · 正面身份图');
    fireEvent.click(screen.getByRole('button', { name: '质量通过' }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole('button', { name: '退回' }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(2));
    fireEvent.click(screen.getByRole('button', { name: '质量通过' }));
    await waitFor(() => expect(post).toHaveBeenCalledTimes(3));
    expect(vi.mocked(post).mock.calls).toEqual([
      ['/projects/project-a/image-reviews', { candidateIds: ['candidate-a'], decision: 'approved', notes: '' }, 'review-key-1'],
      ['/projects/project-a/image-reviews', { candidateIds: ['candidate-a'], decision: 'rejected', notes: '' }, 'review-key-2'],
      ['/projects/project-a/image-reviews', { candidateIds: ['candidate-a'], decision: 'approved', notes: '' }, 'review-key-3'],
    ]);
  });

  it('numbers same-index candidates by time within one target', async () => {
    queue = [
      base({ targetType: 'shot', targetId: 'shot-a', targetName: '镜头甲', role: 'shot', jobId: 'job-a', index: 0, createdAt: 2 }),
      base({ id: 'candidate-b', candidateId: 'candidate-b', assetId: 'asset-b', targetType: 'shot', targetId: 'shot-a', targetName: '镜头甲', role: 'shot', jobId: 'job-b', index: 0, createdAt: 1 }),
    ];
    mount();
    expect(await screen.findByRole('article', { name: '镜头甲 候选 1' })).toBeInTheDocument();
    expect(screen.getByRole('article', { name: '镜头甲 候选 2' })).toBeInTheDocument();
  });

  it('saves a shot candidate to the scene slot without replacing the main picture', async () => {
    queue = [base({ targetType: 'shot', targetId: 'shot-a', targetName: '镜头甲', role: 'shot', jobId: 'job-a', currentRevision: 4, assetId: 'asset-scene' })];
    mount();
    fireEvent.click(await screen.findByRole('button', { name: '存到场景' }));
    await waitFor(() => expect(post).toHaveBeenCalledWith(
      '/shots/shot-a/scene-reference-selection',
      { expectedRevision: 4, assetId: 'asset-scene' },
      'quick-review-key',
    ));
    expect(selectImageCandidate).not.toHaveBeenCalled();
  });
});
