export type Generation = {
  contractVersion: "h3-ref2va-v1";
  profile: "heretic-ref2va-v1" | "heretic-ref2va-hmnsfw-v1" | "10eros-fl2va-v1";
  requestDurationSeconds: number;
};
export const stableGeneration: Generation = {
  contractVersion: "h3-ref2va-v1",
  profile: "heretic-ref2va-v1",
  requestDurationSeconds: 4,
};
export function generationOf(value?: Generation): Generation {
  if (value === undefined || value === null) return { ...stableGeneration };
  if (
    value.contractVersion !== stableGeneration.contractVersion ||
    !["heretic-ref2va-v1", "heretic-ref2va-hmnsfw-v1", "10eros-fl2va-v1"].includes(
      value.profile,
    ) ||
    !Number.isInteger(value.requestDurationSeconds) ||
    !(value.profile === "heretic-ref2va-v1"
      ? value.requestDurationSeconds === 4
      : value.requestDurationSeconds >= 4 && value.requestDurationSeconds <= 15)
  )
    throw new Error("生成设置不符合当前 H3 合同，请重新读取镜头版本。");
  return { ...value };
}
export const textGeneration: Generation = { ...stableGeneration, profile: "10eros-fl2va-v1" };
export const ring4Generation: Generation = { ...stableGeneration, profile: "heretic-ref2va-hmnsfw-v1" };
export const generationForReferences = (value: Generation | undefined, _hasReference: boolean): Generation => {
  const generation = generationOf(value);
  return { ...generation, profile: ring4Generation.profile };
};
export const generationForReference = (value: Generation | undefined, image?: string): Generation =>
  generationForReferences(value, Boolean(image));
export const generationName = (_value?: Generation) => "H3 Ring4";
