import { describe, expect, it } from "vitest";
import { compileOfficialPrompt, numberNodes, type ReferenceNode } from "./reference-nodes";

const nodes: ReferenceNode[] = [
  { id: "person", media: "image", assetId: "a", role: "character", description: "Keep the face and ribbons." },
  { id: "place", media: "image", assetId: "b", role: "scene", description: "Palace stage." },
  { id: "open", media: "image", assetId: "c", role: "first", description: "" },
  { id: "close", media: "image", assetId: "d", role: "last", description: "" },
  { id: "move", media: "video", assetId: "e", role: "action", description: "Use the turn cadence only." },
];

describe("official H3 prompt", () => {
  it("numbers each attached file in its own family and skips an empty node", () => {
    const numbered = numberNodes([...nodes, { id: "empty", media: "image", assetId: "", role: "scene", description: "" }]);
    expect(numbered.map((item) => `${item.label} ${item.index}`)).toEqual(["Picture 1", "Picture 2", "Picture 3", "Picture 4", "Video 1"]);
  });

  it("writes subjects inside Subject and frames as standalone Picture lines", () => {
    const prompt = compileOfficialPrompt({ instruction: "Turn once and settle.", nodes });
    expect(prompt).toContain("subject_definitions:");
    expect(prompt).toContain("summary:");
    expect(prompt).toContain("retention_analysis:");
    expect(prompt).toContain("detailed_description:");
    expect(prompt).toContain("overall_soundscape:");
    expect(prompt).toContain("non_diegetic_music:");
    expect(prompt).toContain("<Subject 1> is the person in <Picture 1>.");
    expect(prompt).toContain("<Subject 2> is the location in <Picture 2>.");
    expect(prompt).toContain("<Picture 3> is the first frame of [Shot 1].");
    expect(prompt).toContain("<Picture 4> is the last frame of [Shot 1].");
    expect(prompt).toContain("<Video 1> supplies the motion cadence");
    expect(prompt).toContain("[Shot 1] Turn once and settle.");
    expect(prompt).toContain("No dialogue, no speech, no voices.");
    expect(prompt).not.toContain("<Audio 1>");
  });

  it("keeps a complete draft that already cites every attached file", () => {
    const stored = ["subject_definitions:", "<Picture 1> identity.", "summary:", "One.", "retention_analysis:", "Keep.", "detailed_description:", "[Shot 1] Dance.", "overall_soundscape:", "Silk.", "non_diegetic_music:", "None."].join("\n");
    expect(compileOfficialPrompt({ instruction: stored, nodes: [nodes[0]] })).toBe(stored);
  });

  it("numbers subjects separately from pictures when a frame comes first", () => {
    const prompt = compileOfficialPrompt({
      instruction: "Turn once.",
      nodes: [
        { id: "open", media: "image", assetId: "c", role: "first", description: "" },
        { id: "person", media: "image", assetId: "a", role: "character", description: "Keep the face." },
      ],
    });
    expect(prompt).toContain("<Subject 1> is the person in <Picture 2>.");
    expect(prompt).toContain("<Picture 1> is the first frame of [Shot 1].");
    expect(prompt).not.toContain("<Picture 2> is the first frame");
    expect(prompt.indexOf("subject_definitions:")).toBeLessThan(prompt.indexOf("summary:"));
    expect(prompt.indexOf("summary:")).toBeLessThan(prompt.indexOf("retention_analysis:"));
    expect(prompt.indexOf("retention_analysis:")).toBeLessThan(prompt.indexOf("detailed_description:"));
    expect(prompt.indexOf("detailed_description:")).toBeLessThan(prompt.indexOf("overall_soundscape:"));
    expect(prompt.indexOf("overall_soundscape:")).toBeLessThan(prompt.indexOf("non_diegetic_music:"));
    expect(prompt).toContain("summary:\n[reference generation]");
    expect(prompt).toContain("No dialogue, no speech, no voices.");
  });

  it("writes a custom token beside its Picture citation without adding a seventh section", () => {
    const prompt = compileOfficialPrompt({
      instruction: "Hold.",
      nodes: [{ id: "env", media: "image", assetId: "f", role: "custom", customRoleId: "env", description: "" }],
      customRoles: [{ id: "env", label: "环境", token: "Environment" }],
    });
    expect(prompt).toContain("as Environment.");
    expect(prompt).not.toContain("environment:");
  });
});
