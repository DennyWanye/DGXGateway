import { DndContext, closestCenter, KeyboardSensor, PointerSensor, useSensor, useSensors } from '@dnd-kit/core';
import { SortableContext, useSortable, arrayMove, horizontalListSortingStrategy, sortableKeyboardCoordinates } from '@dnd-kit/sortable';
import { CSS } from '@dnd-kit/utilities';
import { GripVertical } from 'lucide-react';
import { MiniTimelineRuler } from './vendor/freecut/MiniTimelineRuler';

type Item = { clipId: string; takeId: string; frames: number | string };
export function TimelineStrip({ clips, titles, fps, selectedId, disabled, onSelect, onReorder }: {
  clips: Item[]; titles: Record<string, string>; fps: number; selectedId: string; disabled: boolean;
  onSelect: (id: string) => void; onReorder: (from: number, to: number) => void;
}) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 8 } }), useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }));
  return <div className="space-y-2" aria-label="时间线片段顺序">
    <p className="text-xs text-muted-foreground">点击片段查看和裁切；拖动手柄调整顺序。键盘：聚焦手柄，空格拿起，方向键移动，空格放下。</p>
    <div className="overflow-x-auto rounded-lg border px-2 pb-2 pr-12">
      <div style={{ minWidth: Math.max(500, clips.length * 100) }}>
        <MiniTimelineRuler labelWidth={0} maxFrame={clips.reduce((sum, c) => sum + Math.max(0, Number(c.frames) || 0), 0)} fps={fps} />
        <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={({ active, over }) => {
          if (disabled || !over || active.id === over.id) return;
          const from = clips.findIndex(c => c.clipId === active.id), to = clips.findIndex(c => c.clipId === over.id);
          if (from >= 0 && to >= 0) onReorder(from, to);
        }}>
          <SortableContext items={clips.map(c => c.clipId)} strategy={horizontalListSortingStrategy}>
            <div className="flex gap-1 py-2">{clips.map((clip, index) => <Tile key={clip.clipId} clip={clip} index={index} title={titles[clip.takeId] || '候选不可用'} fps={fps} selected={clip.clipId === selectedId} disabled={disabled} onSelect={onSelect} />)}</div>
          </SortableContext>
        </DndContext>
      </div>
    </div>
  </div>;
}
function Tile({ clip, index, title, fps, selected, disabled, onSelect }: { clip: Item; index: number; title: string; fps: number; selected: boolean; disabled: boolean; onSelect: (id: string) => void }) {
  const { attributes, listeners, setNodeRef, transform, transition } = useSortable({ id: clip.clipId, disabled });
  return <div ref={setNodeRef} style={{ flex: Math.max(1, Number(clip.frames) || 1), transform: CSS.Transform.toString(transform), transition }} className={`flex min-w-24 overflow-hidden rounded-lg border ${selected ? 'border-primary bg-primary/20' : 'bg-muted'}`}>
    <button type="button" {...attributes} {...listeners} disabled={disabled} aria-label={`拖动片段 ${index + 1}`} className="touch-none px-1 text-muted-foreground"><GripVertical className="size-4" /></button>
    <button type="button" disabled={disabled} aria-pressed={selected} onClick={() => onSelect(clip.clipId)} className="min-w-0 flex-1 py-3 pr-2 text-left text-xs" aria-label={`编辑片段 ${index + 1} ${title}`}>
      <span className="block truncate font-medium">{index + 1}. {title}</span><span className="text-muted-foreground">{(Number(clip.frames) / fps).toFixed(2)} 秒</span>
    </button>
  </div>;
}
export { arrayMove };
