import { useId } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { IMAGE_RATIOS, fitImageSize, imageDimensions, imageSizeRules } from '@/lib/image-size';

export function ImageSizePicker({width,height,model,project,onChange}: {
  width: number|string; height: number|string; model: string;
  project: {width?: number; height?: number}; onChange: (width: number|string,height: number|string)=>void;
}) {
  const id = useId(), rules = imageSizeRules(model);
  let error = '';
  try { imageDimensions(width,height,model); } catch(e) { error = (e as Error).message; }
  const fit = fitImageSize(width,height,model);
  return <div className="space-y-3 rounded-xl border bg-muted/20 p-4">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <span className="text-sm font-medium">画面比例</span>
      <Button size="sm" variant="ghost" onClick={()=>{const s=fitImageSize(project.width||1344,project.height||768,model);onChange(s.width,s.height)}}>使用项目画幅</Button>
    </div>
    <div role="group" aria-label="画面比例" className="grid grid-cols-3 gap-2 sm:grid-cols-5">
      {IMAGE_RATIOS.map(p=><Button key={p.label} variant={Number(width)===p.width&&Number(height)===p.height?'secondary':'outline'}
        aria-pressed={Number(width)===p.width&&Number(height)===p.height} className="h-auto min-h-16 flex-col gap-2 py-3"
        onClick={()=>onChange(p.width,p.height)}>
        <span aria-hidden="true" className="block rounded-sm border-2 border-current opacity-70" style={{width:Math.round(22*p.width/Math.max(p.width,p.height)),height:Math.round(22*p.height/Math.max(p.width,p.height))}}/>
        <span>{p.label}</span>
      </Button>)}
    </div>
    <p className="text-xs text-muted-foreground" aria-live="polite">生成尺寸：{width} × {height} 像素。比例选项已适配模型；MV 最终导出画幅仍使用项目设置。</p>
    {error && <div role="alert" className="rounded-lg border border-destructive/40 p-3 text-sm">
      <p>{error}</p><Button size="sm" className="mt-2" onClick={()=>onChange(fit.width,fit.height)}>适配为 {fit.width} × {fit.height}</Button>
    </div>}
    <details>
      <summary className="cursor-pointer text-xs text-muted-foreground">自定义像素（高级）</summary>
      <div className="mt-3 grid grid-cols-2 gap-3">
        <div className="space-y-2"><Label htmlFor={id+'w'}>宽度（像素）</Label><Input id={id+'w'} type="number" min={rules.min} max={rules.max} step={rules.step} value={width} onChange={e=>onChange(e.target.value,height)}/></div>
        <div className="space-y-2"><Label htmlFor={id+'h'}>高度（像素）</Label><Input id={id+'h'} type="number" min={rules.min} max={rules.max} step={rules.step} value={height} onChange={e=>onChange(width,e.target.value)}/></div>
      </div>
    </details>
  </div>;
}
