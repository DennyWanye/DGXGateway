export const IMAGE_RATIOS = [
  { label: '1:1 方图', width: 1024, height: 1024 },
  { label: '3:4 人像', width: 768, height: 1024 },
  { label: '4:3 横图', width: 1024, height: 768 },
  { label: '9:16 竖屏', width: 864, height: 1536 },
  { label: '16:9 横屏', width: 1536, height: 864 },
];
export function imageSizeRules(model = 'z-image-turbo') {
  return model === 'z-image-turbo' ? { min: 16, max: 4096, step: 16 } : { min: 256, max: 1536, step: 32 };
}
export function fitImageSize(width: unknown, height: unknown, model = 'z-image-turbo') {
  const rules = imageSizeRules(model);
  let w = Number(width), h = Number(height);
  if (!Number.isFinite(w) || !Number.isFinite(h) || w <= 0 || h <= 0) return { width: 1024, height: 1024 };
  if ([w,h].every(n => Number.isInteger(n) && n >= rules.min && n <= rules.max && n % rules.step === 0)) return { width: w, height: h };
  const ratio = IMAGE_RATIOS.find(p => Math.abs(w / h - p.width / p.height) < 0.0001);
  if (ratio) return { width: ratio.width, height: ratio.height };
  const scale = Math.min(1, rules.max / Math.max(w,h));
  const align = (n: number) => Math.max(rules.min, Math.min(rules.max, Math.round(n * scale / rules.step) * rules.step));
  return { width: align(w), height: align(h) };
}
export function imageDimensions(width: unknown, height: unknown, model = 'z-image-turbo') {
  const { min, max, step } = imageSizeRules(model);
  const w = Number(width), h = Number(height);
  if ([width,height].some(v => typeof v !== 'number' && typeof v !== 'string') ||
      ![w,h].every(n => Number.isInteger(n) && n >= min && n <= max && n % step === 0)) {
    const suggestion = fitImageSize(width,height,model);
    throw new Error(`当前模型要求宽高为 ${min}–${max} 像素、${step} 的倍数。请选择画面比例，或使用 ${suggestion.width} × ${suggestion.height}。`);
  }
  return { width: w, height: h };
}
