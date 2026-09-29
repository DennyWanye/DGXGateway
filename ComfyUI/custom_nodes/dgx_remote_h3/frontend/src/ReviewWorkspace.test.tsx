// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import '@testing-library/jest-dom/vitest';
import { ReviewWorkspace, reviewGroups } from './ReviewWorkspace';
afterEach(cleanup);
const shots = [{ id: 'one', title: '开场' }, { id: 'two', title: '接近' }, { id: 'three', title: '格挡' }];
const jobs = [
  { id: 'new', episodeId: 'ep', items: [{ shotId: 'three', takeId: 'new3', assetId: 'v3' }] },
  { id: 'old', state: 'INTERRUPTED', episodeId: 'ep', items: [{ shotId: 'three', takeId: 'old3', assetId: 'v2' }, { shotId: 'one', takeId: 'one1', assetId: 'v1' }] },
  { id: 'other', episodeId: 'other', items: [{ shotId: 'three', takeId: 'foreign', assetId: 'foreign' }] },
];
const props = { shots, jobs, takes: {}, episodeId: 'ep', busy: false, renderCandidate: ({ item }: any) => <video aria-label="当前视频" data-take={item.takeId} />, onPrepare: vi.fn(), onEdit: vi.fn() };
describe('shot-centric review', () => {
  it('brings the review panel into view after choosing a shot from a stacked list', async () => {
    render(<ReviewWorkspace {...props} />);
    const panel = screen.getByRole('region', {name:'当前镜头审片'});
    const scrollIntoView = vi.fn();
    panel.scrollIntoView = scrollIntoView;
    fireEvent.click(screen.getByRole('button', {name:/S03 · 格挡/}));
    await waitFor(()=>expect(scrollIntoView).toHaveBeenCalledWith({block:'start',behavior:'smooth'}));
    expect(screen.getByRole('heading', {name:'S03 · 格挡'})).toBeVisible();
  });
  it('opens an explicitly targeted task result in its review form after comparison', async () => {
    const r = render(<ReviewWorkspace {...props} target={{shotId:'three'}} />);
    const details=screen.getByText('并排对比两个候选').closest('details')!;
    details.open=true; fireEvent(details,new Event('toggle'));
    await waitFor(()=>expect(document.querySelectorAll('video')).toHaveLength(2));
    r.rerender(<ReviewWorkspace {...props} target={{jobId:'old',shotId:'three',takeId:'old3'}} />);
    await waitFor(()=>expect(screen.getByLabelText('当前视频')).toHaveAttribute('data-take','old3'));
    expect(document.querySelectorAll('video')).toHaveLength(1);
  });
  it('keeps the currently displayed candidate when another batch finishes or its review status changes', () => {
    const old = [jobs[1]];
    const r = render(<ReviewWorkspace {...props} jobs={old} target={{shotId:'three'}} />);
    expect(screen.getByLabelText('当前视频')).toHaveAttribute('data-take','old3');
    r.rerender(<ReviewWorkspace {...props} jobs={jobs} target={{shotId:'three'}} />);
    expect(screen.getByLabelText('当前视频')).toHaveAttribute('data-take','old3');
    r.rerender(<ReviewWorkspace {...props} jobs={jobs} takes={{old3:{reviewStatus:'approved'}}} target={{shotId:'three'}} />);
    expect(screen.getByLabelText('当前视频')).toHaveAttribute('data-take','old3');
    fireEvent.click(screen.getByRole('button',{name:'候选 1 · 待审'}));
    expect(screen.getByLabelText('当前视频')).toHaveAttribute('data-take','new3');
  });
  it('compares two candidates without leaving a third player, then returns to the chosen review', async () => {
    render(<ReviewWorkspace {...props} target={{shotId:'three'}} />);
    const details=screen.getByText('并排对比两个候选').closest('details')!;
    details.open=true; fireEvent(details,new Event('toggle'));
    await waitFor(()=>expect(document.querySelectorAll('video')).toHaveLength(2));
    expect(screen.queryByLabelText('当前视频')).toBeNull();
    fireEvent.click(screen.getAllByRole('button',{name:'定位此候选审片'})[1]);
    await waitFor(()=>expect(document.querySelectorAll('video')).toHaveLength(1));
    expect(screen.getByLabelText('当前视频')).toHaveAttribute('data-take','old3');
  });
  it('does not replace either comparison video when a third candidate arrives', async () => {
    const r = render(<ReviewWorkspace {...props} projectId="p" target={{shotId:'three'}} />);
    const details=screen.getByText('并排对比两个候选').closest('details')!;
    details.open=true; fireEvent(details,new Event('toggle'));
    await waitFor(()=>expect(document.querySelectorAll('video')).toHaveLength(2));
    const before=Array.from(document.querySelectorAll('video')).map(v=>v.getAttribute('src'));
    r.rerender(<ReviewWorkspace {...props} projectId="p" target={{shotId:'three'}} jobs={[
      {id:'latest',episodeId:'ep',items:[{shotId:'three',takeId:'latest3',assetId:'v4'}]}, ...jobs
    ]} />);
    expect(Array.from(document.querySelectorAll('video')).map(v=>v.getAttribute('src'))).toEqual(before);
  });
  it('keeps episode numbering and groups candidates across interrupted batches without foreign results', () => {
    const groups = reviewGroups(shots, jobs, {}, 'ep');
    expect(groups[2].code).toBe('S03');
    expect(groups[2].candidates.map(c => c.item.takeId)).toEqual(['new3', 'old3']);
    expect(groups[1].candidates).toEqual([]);
  });
  it('finds S03 by search and mounts only the chosen candidate', () => {
    render(<ReviewWorkspace {...props} />);
    fireEvent.change(screen.getByRole('textbox', { name: '搜索镜头' }), { target: { value: 's03' } });
    expect(screen.getByRole('heading', { name: 'S03 · 格挡' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: '候选 2 · 待审' }));
    expect(screen.getByLabelText('当前视频')).toHaveAttribute('data-take', 'old3');
    expect(document.querySelectorAll('video')).toHaveLength(1);
  });
  it('opens a task result once and does not jump back after polling', () => {
    const target = { jobId: 'new' };
    const r = render(<ReviewWorkspace {...props} target={target} />);
    expect(screen.getByRole('heading', { name: 'S03 · 格挡' })).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: /S01 · 开场/ }));
    r.rerender(<ReviewWorkspace {...props} jobs={[...jobs]} target={target} />);
    expect(screen.getByRole('heading', { name: 'S01 · 开场' })).toBeVisible();
  });
  it('does not display a stale selection when fresh state explicitly clears it', () => {
    const groups = reviewGroups([{ ...shots[2], selectedTakeId: 'new3' }], jobs, { new3: { selectedTakeId: null }, old3: { selectedTakeId: null } }, 'ep');
    expect(groups[0].selected).toBeUndefined();
  });
  it('retains imported selected takes without generation jobs and excludes reviewed candidates from pending', () => {
    const groups = reviewGroups([{ id: 'import', selectedTakeId: 't', takes: [{ id: 't', assetId: 'v', reviewStatus: 'approved' }] }], [], {}, 'ep');
    expect(groups[0].selected?.item.takeId).toBe('t');
    expect(groups[0].pending).toHaveLength(0);
  });
});
