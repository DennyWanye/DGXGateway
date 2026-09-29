// @vitest-environment jsdom
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { GatewayPromptWriter as PromptWriter } from './PromptWriter';
import { get } from '@/lib/api';

vi.mock('@/lib/api', () => ({ get: vi.fn(), post: vi.fn(), segment: encodeURIComponent }));
const calls = vi.hoisted(() => ({ execute: vi.fn() }));
vi.mock('./PostproductionPanel', () => ({ useDurablePost: () => ({ busy: false, pending: null, execute: calls.execute }), PendingAction: () => null }));
const draft = { shotId: 'shot', shotRevisionId: 'revision', imageAssetId: 'image', soundMode: 'silent', dialogue: '', prompt: '打一拳' };
const ctx: any = { project: { id: 'project' }, episode: { id: 'episode' }, params: { shotId: 'shot', promptJobId: 'job' }, capabilities: { textGenerationEnabled: true }, notify: vi.fn() };
const job = { id: 'job', state: 'SUCCEEDED', outputStatus: 'valid_draft', inputRevisions: { ...draft }, compiledPrompt: 'Validated full H3 text. No dialogue, no speech, no voices.', normalizationNotes: ['已保留原文并补充声音约束'], attempts: [{ rawText: '{"overall_soundscape":"无对白，无人声"}' }] };
beforeEach(() => { vi.clearAllMocks(); sessionStorage.clear(); vi.mocked(get).mockResolvedValue({ job }); });
afterEach(cleanup);

it('applies the server-validated complete prompt and retains the model raw output separately', async () => {
  const onApply = vi.fn();
  render(<PromptWriter ctx={ctx} draft={draft} disabled={false} onApply={onApply} />);
  const apply = await screen.findByRole('button', { name: '应用到当前视频提示词' });
  await waitFor(() => expect(apply).toBeEnabled());
  expect(screen.getByText('已保留原文并补充声音约束')).toBeInTheDocument();
  fireEvent.click(apply);
  expect(onApply).toHaveBeenCalledWith(job.compiledPrompt, 'job');
  expect(screen.getByText('查看模型原始输出')).toBeInTheDocument();
});

it.each([{ shotRevisionId: 'new' }, { imageAssetId: 'new' }, { soundMode: 'native', dialogue: '我回来了。' }, { dialogue: '后来填写的台词' }])('blocks applying a result after its frozen input changes: %j', async patch => {
  const onApply = vi.fn();
  render(<PromptWriter ctx={ctx} draft={{ ...draft, ...patch }} disabled={false} onApply={onApply} />);
  const apply = await screen.findByRole('button', { name: '应用到当前视频提示词' });
  expect(apply).toBeDisabled();
  expect(screen.getByRole('alert')).toHaveTextContent('已改变');
  fireEvent.click(apply);
  expect(onApply).not.toHaveBeenCalled();
});

it('does not construct an applicable prompt from an unvalidated raw response', async () => {
  vi.mocked(get).mockResolvedValue({ job: { ...job, compiledPrompt: null, outputStatus: 'invalid_draft' } });
  render(<PromptWriter ctx={ctx} draft={draft} disabled={false} onApply={vi.fn()} />);
  await screen.findByText('查看模型原始输出');
  expect(screen.queryByRole('button', { name: '应用到当前视频提示词' })).toBeNull();
});

it('repairs using the archived input instead of the edited current description', async () => {
  const promptInput = { shotRevisionId: 'revision', brief: '原始说明', soundMode: 'silent', dialogue: '' };
  vi.mocked(get).mockResolvedValue({ job: { ...job, outputStatus: 'invalid_draft', compiledPrompt: null, resourceStatus: 'released', promptInput } });
  render(<PromptWriter ctx={ctx} draft={{ ...draft, prompt: '后来编辑的说明' }} disabled={false} onApply={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '按原输入修复一次' }));
  expect(calls.execute).toHaveBeenCalledWith('/episodes/episode/text-jobs', { ...promptInput, kind: 'draft_video_prompt', sourceRevisionIds: [], repairOf: 'job' }, 'prompt');
});

it('opens the existing repair instead of submitting it twice', async () => {
  vi.mocked(get).mockImplementation(async path => ({ job: path.endsWith('/repaired') ? { ...job, id: 'repaired', repairOf: 'job' } : { ...job, outputStatus: 'invalid_draft', compiledPrompt: null, repairJobId: 'repaired' } }));
  const onApply = vi.fn();
  render(<PromptWriter ctx={ctx} draft={draft} disabled={false} onApply={onApply} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看已有修复任务' }));
  fireEvent.click(await screen.findByRole('button', { name: '应用到当前视频提示词' }));
  expect(onApply).toHaveBeenCalledWith(job.compiledPrompt, 'repaired');
  expect(calls.execute).not.toHaveBeenCalled();
});

it('resumes observation when opening a running repair from a finished parent', async () => {
  let reads = 0;
  vi.mocked(get).mockImplementation(async path => ({ job: path.endsWith('/repaired')
    ? { ...job, id: 'repaired', repairOf: 'job', state: ++reads === 1 ? 'LOADING' : 'SUCCEEDED', compiledPrompt: reads === 1 ? null : job.compiledPrompt }
    : { ...job, outputStatus: 'invalid_draft', compiledPrompt: null, repairJobId: 'repaired' } }));
  render(<PromptWriter ctx={ctx} draft={draft} disabled={false} onApply={vi.fn()} />);
  fireEvent.click(await screen.findByRole('button', { name: '查看已有修复任务' }));
  expect(await screen.findByRole('button', { name: '应用到当前视频提示词' })).toBeEnabled();
  expect(reads).toBeGreaterThanOrEqual(2);
  expect(calls.execute).not.toHaveBeenCalled();
});
