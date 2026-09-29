export function sourceSlice(text: string, start: number, end: number) {
  const points = Array.from(text);
  if (
    !Number.isInteger(start) ||
    !Number.isInteger(end) ||
    start < 0 ||
    end <= start ||
    end > points.length
  )
    throw new Error("来源范围须为有效的 Unicode 字符区间。");
  return points.slice(start, end).join("");
}
export async function sourceSpan(
  sourceRevisionId: string,
  text: string,
  start: number,
  end: number,
) {
  const quote = sourceSlice(text, start, end);
  const hash = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(quote),
  );
  return {
    sourceRevisionId,
    start,
    end,
    quoteSha256: Array.from(new Uint8Array(hash), (x) =>
      x.toString(16).padStart(2, "0"),
    ).join(""),
  };
}
export function parseSourceFile(bytes: ArrayBuffer) {
  if (bytes.byteLength > 256 * 1024)
    throw new Error("原文最大 256 KiB，不会自动截断。");
  return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
}
