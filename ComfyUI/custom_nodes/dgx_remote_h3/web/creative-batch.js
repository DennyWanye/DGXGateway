export function appendReference(ids, assetId) {
  if (!Array.isArray(ids)) throw new Error('已有参考图列表不是数组，请先检查，原输入保持不变。');
  const result = [...new Set([...ids, assetId])];
  if (result.length > 3) throw new Error('最多3张输入参考图，原参考图已保留。新素材已经注册，请明确替换参考图后再提交；这与输出候选数量不同。');
  return result;
}
export function batchSummary(row) {
  const batch = row.image_batch;
  if (!batch) return '历史或通用工作流任务：未记录候选并行信息。';
  const candidates = batch.candidates || [];
  const succeeded = candidates.filter(c => c.state === 'SUCCEEDED').length;
  const failed = candidates.filter(c => c.state === 'FAILED').length;
  const parallel = Number.isInteger(batch.observedParallelism) ? batch.observedParallelism : '尚未记录';
  return `共 ${batch.candidateCount} 张候选 · 已成功 ${succeeded} · 失败 ${failed} · 每批上限 ${batch.parallelLimit} · 实际同时生成 ${parallel}。完成组的图片先显示，未完成组继续处理。`;
}
export function candidateCaption(artifact) {
  return Number.isInteger(artifact.candidate_index) ? `候选 ${artifact.candidate_index + 1} · seed ${artifact.seedText ?? artifact.seed ?? '未记录'}` : '历史产物 · 未记录逐张seed';
}

export function parseCreativeParameter(type, value) {
  const exact = (number) => {
    if (!Number.isFinite(number) || (Number.isInteger(number) && !Number.isSafeInteger(number))) throw new Error('数值超出浏览器可精确表示范围，请使用安全整数或通过Python接口传入完整种子。');
    return number;
  };
  if (type === 'integer') {
    const number = exact(Number(value));
    if (!Number.isInteger(number)) throw new Error('请输入整数');
    return number;
  }
  if (['array', 'object', 'boolean'].includes(type)) return JSON.parse(value, (_key, item) => typeof item === 'number' ? exact(item) : item);
  return value;
}

export function creativeImageURL(artifact, jobId) {
  try {
    if (artifact.kind !== 'images' || !/^[a-f0-9]{32}$/.test(jobId)) return null;
    const url = new URL(artifact.url);
    const match = url.pathname.match(/^\/api\/v1\/artifacts\/([a-f0-9]{32})\/([^/]+)$/);
    if (!['http:', 'https:'].includes(url.protocol) || !match || match[1] !== jobId || url.search || url.hash) return null;
    const filename = decodeURIComponent(match[2]);
    if (!filename || filename.length > 255 || /[/\\\x00-\x1f]/.test(filename) || ['.', '..'].includes(filename)) return null;
    return `/dgx-creative/artifacts/${jobId}/${encodeURIComponent(filename)}`;
  } catch { return null; }
}
