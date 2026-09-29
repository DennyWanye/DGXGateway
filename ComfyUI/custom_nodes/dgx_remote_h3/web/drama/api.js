const BASE = "/dgx-drama";
const ACTION = { "X-DGX-Drama-Action": "explicit-user-action" };

export const requestKey = () => "drama-ui-" + Array.from(
  crypto.getRandomValues(new Uint8Array(16)), b => b.toString(16).padStart(2, "0"),
).join("");
export const segment = value => encodeURIComponent(value);
export const mediaUrl = (projectId, assetId) =>
  `${BASE}/projects/${segment(projectId)}/assets/${segment(assetId)}/media`;

async function decode(response) {
  let value;
  try { value = await response.json(); }
  catch { throw new Error(`服务未返回有效 JSON（HTTP ${response.status}），请刷新核查任务状态。`); }
  if (!response.ok || value.error) {
    const detail = typeof value.error === "string" ? value.error : value.error?.message;
    const conflict = response.status === 409 ? "数据已变化或请求冲突，请先刷新核查。 " : "";
    const error = new Error(`${conflict}${detail || `请求失败（HTTP ${response.status}）`}${value.requestId ? ` · 请求 ${value.requestId}` : ""}`);
    error.status = response.status;
    throw error;
  }
  return value;
}

export async function get(path) {
  return decode(await fetch(BASE + path, { cache: "no-store" }));
}

// Never retry a write here: an interrupted response may still have committed.
export async function post(path, body = {}, explicitRequestKey) {
  const key = explicitRequestKey ?? requestKey();
  if (typeof key !== "string" || !key.trim()) throw new Error("请求键必须为非空字符串。");
  try {
    return await decode(await fetch(BASE + path, {
      method: "POST", headers: { ...ACTION, "Content-Type": "application/json" },
      body: JSON.stringify({ ...body, requestKey: key }),
    }));
  } catch (error) {
    if (!error.status) error.message += ` 提交结果可能尚未确认；请先刷新，勿立即重复提交。请求键：${key}`;
    throw error;
  }
}

export async function upload(projectId, file) {
  if (!file) throw new Error("请选择视频文件。");
  if (!file.size || file.size > 512 * 1024 * 1024) throw new Error("视频必须非空且不超过 512 MiB。");
  const key = requestKey();
  try {
    return await decode(await fetch(`${BASE}/projects/${segment(projectId)}/assets`, {
      method: "POST", headers: { ...ACTION, "Content-Type": "application/octet-stream",
        "X-Request-Key": key, "X-Filename": encodeURIComponent(file.name) },
      body: file,
    }));
  } catch (error) {
    if (!error.status) error.message += ` 请先刷新确认素材是否已入库。请求键：${key}`;
    throw error;
  }
}

export function validateReferenceAudioBytes(bytes) {
  if (!(bytes instanceof Uint8Array) || bytes.byteLength < 44 || bytes.byteLength > 256 * 1024) throw new Error("参考音频须为非空 WAV，最大 256 KiB。");
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  const tag = offset => String.fromCharCode(...bytes.subarray(offset, offset + 4));
  if (tag(0) !== "RIFF" || tag(8) !== "WAVE") throw new Error("参考音频必须为 RIFF WAV 文件。");
  let format = null, dataBytes = 0, dataChunks = 0;
  for (let offset = 12; offset + 8 <= bytes.byteLength;) {
    const size = view.getUint32(offset + 4, true);
    const start = offset + 8;
    if (start + size > bytes.byteLength) throw new Error("WAV 数据不完整，请重新导出参考音频。");
    if (tag(offset) === "fmt ") {
      if (format || size < 16) throw new Error("WAV 格式头无效。");
      format = { encoding: view.getUint16(start, true), channels: view.getUint16(start + 2, true),
        sampleRate: view.getUint32(start + 4, true), byteRate: view.getUint32(start + 8, true),
        blockAlign: view.getUint16(start + 12, true), bits: view.getUint16(start + 14, true) };
    }
    if (tag(offset) === "data") { dataBytes += size; ++dataChunks; }
    offset = start + size + (size % 2);
  }
  if (!format || format.encoding !== 1 || format.channels !== 1 || format.sampleRate !== 24000 || format.bits !== 16 || format.blockAlign !== 2 || format.byteRate !== 48000 || dataChunks !== 1 || dataBytes !== 192000) {
    throw new Error("参考音频必须严格为 4 秒、24000 Hz、单声道、PCM16 WAV（96000 帧）。请在音频工具中裁切并导出后上传。");
  }
}

export async function uploadReferenceAudio(projectId, file) {
  if (!file || !file.size || file.size > 256 * 1024) throw new Error("请选择最大 256 KiB 的参考 WAV 音频。");
  validateReferenceAudioBytes(new Uint8Array(await file.arrayBuffer()));
  const key = requestKey();
  try {
    return await decode(await fetch(`${BASE}/projects/${segment(projectId)}/reference-audio`, {
      method: "POST", headers: { ...ACTION, "Content-Type": "application/octet-stream",
        "X-Request-Key": key, "X-Filename": encodeURIComponent(file.name) }, body: file,
    }));
  } catch (error) {
    if (!error.status) error.message += ` 上传结果尚未确认，请先刷新核查素材。请求键：${key}`;
    throw error;
  }
}
