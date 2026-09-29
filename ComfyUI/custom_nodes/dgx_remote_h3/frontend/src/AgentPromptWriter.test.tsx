// @vitest-environment jsdom
import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { PromptWriter, agentPromptBrief } from './PromptWriter';
import { get, post } from '@/lib/api';
vi.mock('@/lib/api', () => ({ get: vi.fn(), post: vi.fn(), segment: encodeURIComponent, mediaUrl: (p: string, a: string) => `/dgx-drama/projects/${p}/assets/${a}/media` }));
const execute = vi.hoisted(() => vi.fn());
vi.mock('./PostproductionPanel', () => ({ useDurablePost: () => ({ busy: false, pending: null, execute }), PendingAction: () => null }));
const ctx: any = { project: { id: 'project' }, episode: { id: 'episode' }, params: {}, capabilities: { textGenerationEnabled: false }, notify: vi.fn() };
const draft = { shotId: 'shot', title: '直拳格挡', shotRevisionId: 'revision', imageAssetId: 'image', generation: { requestDurationSeconds: 4 }, soundMode: 'native', dialogue: '我回来了。', prompt: '打一拳' };
afterEach(() => { cleanup(); vi.clearAllMocks(); });
it('defaults to agent mode without text API calls even when text generation is unavailable', () => {
  render(<PromptWriter ctx={ctx} draft={draft} disabled={false} onApply={vi.fn()} />);
  expect(screen.getByRole('combobox', { name: '提示词编写方式' })).toHaveValue('agent');
  expect(screen.getByRole('button', { name: '复制给 agent 的编写要求' })).toBeEnabled();
  expect(screen.queryByRole('button', { name: '根据当前描述编写' })).toBeNull();
  expect(get).not.toHaveBeenCalled(); expect(post).not.toHaveBeenCalled(); expect(execute).not.toHaveBeenCalled();
});
it('exports reference, duration, dialogue and revision with no-generation instructions', () => {
  const brief = agentPromptBrief(ctx, draft);
  for (const text of ['revision', '/projects/project/assets/image/media', 'requestDurationSeconds": 4', '我回来了。', '不要调用 DGX 文本任务', '16384', 'subject_definitions']) expect(brief).toContain(text);
});
it('identifies the optional Gateway model without submitting when selected', () => {
  render(<PromptWriter ctx={ctx} draft={draft} disabled={false} onApply={vi.fn()} />);
  fireEvent.change(screen.getByRole('combobox', { name: '提示词编写方式' }), { target: { value: 'gateway' } });
  expect(screen.getByText(/deepseek-v4-flash-0731-abliterated-anchored/)).toBeInTheDocument();
  expect(screen.getByRole('button', { name: '根据当前描述编写' })).toBeDisabled();
  expect(execute).not.toHaveBeenCalled();
});
it('copies the handoff without applying or submitting a video', () => {
  const writeText = vi.fn().mockResolvedValue(undefined);
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } });
  const onApply = vi.fn();
  render(<PromptWriter ctx={ctx} draft={draft} disabled={false} onApply={onApply} />);
  fireEvent.click(screen.getByRole('button', { name: '复制给 agent 的编写要求' }));
  expect(writeText).toHaveBeenCalledWith(agentPromptBrief(ctx, draft));
  expect(onApply).not.toHaveBeenCalled(); expect(execute).not.toHaveBeenCalled();
});
