export type MediaKind = "image" | "video" | "audio";
export type BuiltinRole = "character" | "costume" | "scene" | "action" | "first" | "last";

export type ReferenceNode = {
  id: string;
  media: MediaKind;
  assetId: string;
  role: BuiltinRole | "custom";
  customRoleId?: string;
  description: string;
};

export type CustomRole = { id: string; label: string; token: string };

export const BUILTIN_ROLES: { id: BuiltinRole; label: string; hint: string }[] = [
  { id: "character", label: "人物", hint: "Subject / <Picture N>" },
  { id: "costume", label: "服装", hint: "Subject / <Picture N>" },
  { id: "scene", label: "场景", hint: "Subject / <Picture N>" },
  { id: "action", label: "动作样子", hint: "Subject / <Picture N>" },
  { id: "first", label: "开头", hint: "单开 <Picture N> · 首帧" },
  { id: "last", label: "结尾", hint: "单开 <Picture N> · 尾帧" },
];

const SECTIONS = ["subject_definitions", "summary", "retention_analysis", "detailed_description", "overall_soundscape", "non_diegetic_music"];

export function roleLabel(node: ReferenceNode, customRoles: CustomRole[] = []): string {
  if (node.role === "custom") return customRoles.find((role) => role.id === node.customRoleId)?.label || "自定义";
  return BUILTIN_ROLES.find((role) => role.id === node.role)?.label || node.role;
}

export function roleHint(node: ReferenceNode, customRoles: CustomRole[] = []): string {
  if (node.media === "video") return "<Video N>";
  if (node.media === "audio") return "<Audio N>";
  if (node.role === "custom") {
    const token = customRoles.find((role) => role.id === node.customRoleId)?.token || "Custom";
    return `自定义 · ${token}`;
  }
  return BUILTIN_ROLES.find((role) => role.id === node.role)?.hint || "";
}

type Numbered = ReferenceNode & { label: "Picture" | "Video" | "Audio"; index: number };

export function numberNodes(nodes: ReferenceNode[]): Numbered[] {
  const counts = { Picture: 0, Video: 0, Audio: 0 };
  return nodes.filter((node) => node.assetId).map((node) => {
    const label = node.media === "video" ? "Video" : node.media === "audio" ? "Audio" : "Picture";
    counts[label] += 1;
    return { ...node, label, index: counts[label] };
  });
}

function tag(item: Numbered): string {
  return `<${item.label} ${item.index}>`;
}

function isComplete(prompt: string): boolean {
  const labels = new Set(prompt.split(/\r?\n/).map((line) => line.match(/^([a-z_]+):/)?.[1]).filter(Boolean));
  return SECTIONS.every((section) => labels.has(section));
}

function citesAll(prompt: string, items: Numbered[]): boolean {
  return items.every((item) => prompt.includes(tag(item)));
}

function subjectLine(item: Numbered, subjectIndex: number, customRoles: CustomRole[]): string {
  const token = customRoles.find((role) => role.id === item.customRoleId)?.token;
  const note = item.description.trim();
  const cited = tag(item);
  const suffix = note ? ` ${note}` : "";
  if (item.role === "costume") return `<Subject ${subjectIndex}> wears the costume in ${cited}.${suffix}`;
  if (item.role === "scene") return `<Subject ${subjectIndex}> is the location in ${cited}.${suffix}`;
  if (item.role === "action") return `<Subject ${subjectIndex}> uses ${cited} for pose and body mechanics only, not identity.${suffix}`;
  if (item.role === "custom") return `<Subject ${subjectIndex}> is defined by ${cited} as ${token || "Custom"}.${suffix}`;
  return `<Subject ${subjectIndex}> is the person in ${cited}.${suffix}`;
}

function shotInstruction(stored: string): string {
  if (!isComplete(stored)) return stored || "Hold the supplied references and perform one continuous action.";
  const match = stored.match(/detailed_description:\s*([\s\S]*?)(?:\n[a-z_]+:|$)/);
  const body = match?.[1]?.replace(/^\[Shot 1\]\s*/, "").trim();
  return body || "Hold the supplied references and perform one continuous action.";
}

export function compileOfficialPrompt(input: {
  instruction?: string;
  nodes: ReferenceNode[];
  customRoles?: CustomRole[];
  fresh?: boolean;
}): string {
  const customRoles = input.customRoles || [];
  const items = numberNodes(input.nodes);
  const stored = String(input.instruction || "").trim();
  if (!input.fresh && isComplete(stored) && citesAll(stored, items)) return stored;
  const pictures = items.filter((item) => item.label === "Picture");
  const subjects = pictures.filter((item) => item.role !== "first" && item.role !== "last");
  const frames = pictures.filter((item) => item.role === "first" || item.role === "last");
  const videos = items.filter((item) => item.label === "Video");
  const audios = items.filter((item) => item.label === "Audio");
  const instruction = shotInstruction(stored);
  const sound = audios.length
    ? audios.map((item) => `Use ${tag(item)} as the sound reference.${item.description.trim() ? ` ${item.description.trim()}` : ""}`).join(" ")
    : "No dialogue, no speech, no voices.";
  const music = audios.length
    ? "Follow only the music present in the audio reference; do not add unrelated music."
    : "No added score.";
  const retention = [
    ...subjects.map((item) => `Retain the ${roleLabel(item, customRoles)} reference from ${tag(item)}.`),
    ...frames.map((item) => `${tag(item)} anchors the ${item.role === "first" ? "opening" : "closing"} of [Shot 1].`),
    ...videos.map((item) => `${tag(item)} supplies motion and camera cadence only.`),
  ].join(" ") || "No reference image is attached.";
  const frameLines = frames.map((item) => `${tag(item)} is the ${item.role === "first" ? "first" : "last"} frame of [Shot 1].${item.description.trim() ? ` ${item.description.trim()}` : ""}`);
  const videoLines = videos.map((item) => `${tag(item)} supplies the motion cadence and camera path. Do not copy identity or costume from the video.${item.description.trim() ? ` ${item.description.trim()}` : ""}`);
  return [
    `subject_definitions:\n${subjects.map((item, index) => subjectLine(item, index + 1, customRoles)).join("\n") || "No pictured subject is attached."}`,
    `summary:\n[reference generation] One shot using the supplied references.`,
    `retention_analysis:\n${retention}`,
    `detailed_description:\n[Shot 1] ${instruction}\n${[...frameLines, ...videoLines].join("\n")}`.trimEnd(),
    `overall_soundscape:\n${sound}`,
    `non_diegetic_music:\n${music}`,
  ].join("\n\n");
}

type Snapshot = Record<string, unknown>;

export function nodesFromSnapshot(snapshot: Snapshot | null | undefined): ReferenceNode[] {
  if (!snapshot) return [];
  if (Array.isArray(snapshot.referenceNodes)) return snapshot.referenceNodes as ReferenceNode[];
  const nodes: ReferenceNode[] = [];
  const push = (id: string, media: MediaKind, assetId: unknown, role: BuiltinRole, description = "") => {
    if (typeof assetId === "string" && assetId) nodes.push({ id, media, assetId, role, description });
  };
  const referenceRole = snapshot.referenceRole === "first_frame" ? "first" : "character";
  push("legacy-approved", "image", snapshot.approvedReferenceAssetId, referenceRole);
  push("legacy-scene", "image", snapshot.sceneReferenceAssetId, "scene", String(snapshot.sceneReferencePrompt || ""));
  push("legacy-action", "image", snapshot.actionReferenceAssetId, "action", String(snapshot.actionReferencePrompt || ""));
  if (referenceRole !== "first") push("legacy-first", "image", snapshot.firstFrameAssetId, "first");
  push("legacy-last", "image", snapshot.lastFrameAssetId, "last");
  push("legacy-video", "video", snapshot.videoReferenceAssetId, "action");
  push("legacy-audio", "audio", snapshot.audioReferenceAssetId, "action", String(snapshot.audioDescription || ""));
  return nodes;
}
