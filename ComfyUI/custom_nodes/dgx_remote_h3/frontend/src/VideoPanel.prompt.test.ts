// @vitest-environment jsdom
import { describe, expect, it } from "vitest";
import { buildVideoBatch, videoPrompt } from "./VideoPanel";

const draft = (prompt: string) => ({
  shotId: "shot-1",
  shotRevisionId: "revision-1",
  imageAssetId: "image-1",
  imageExtension: "png",
  approved: true,
  needsReview: false,
  audioAssetId: "audio-1",
  soundMode: "reference" as const,
  prompt,
  seed: 42,
});

function contract(body: string) {
  return [
    "subject_definitions:",
    "<Picture 1> identity.",
    "summary:",
    "One uninterrupted shot.",
    "retention_analysis:",
    "Keep the face.",
    "detailed_description:",
    body,
    "overall_soundscape:",
    "Silk only. No dialogue.",
    "non_diegetic_music:",
    "No added score.",
  ].join("\n");
}

describe("finished Ref2VA prompts are submitted unchanged", () => {
  it("does not wrap a six-section contract, even when that wrap would exceed 4096 bytes", () => {
    const prompt = contract("x".repeat(3600));
    expect(new TextEncoder().encode(prompt).length).toBeLessThanOrEqual(4096);
    const submitted = buildVideoBatch([draft(prompt)]).shots[0].prompt;
    expect(submitted).toBe(prompt);
    expect(submitted.match(/subject_definitions:/g)).toHaveLength(1);
    expect(submitted).not.toContain("Create one coherent H3 Ring4 shot");
  });

  it("still wraps a director note and a prompt that only mentions one section label", () => {
    const wrapped = videoPrompt(draft("悟空缓缓握拳"));
    expect(wrapped).toContain("subject_definitions:");
    expect(wrapped).toContain("detailed_description: 悟空缓缓握拳");
    const partial = videoPrompt(draft("向前走。summary: 不是整篇合同"));
    expect(partial).toContain("subject_definitions:");
    expect(partial).not.toBe("向前走。summary: 不是整篇合同");
  });

  it("passes a finished no-reference shell through and does not demand dialogue", () => {
    const prompt = [
      "integrated_multimodal_description: walk forward",
      "overall_soundscape: quiet room",
      "non_diegetic_music: none",
    ].join("\n\n");
    expect(videoPrompt({ ...draft(prompt), imageAssetId: "", audioAssetId: "", soundMode: "native", dialogue: "" })).toBe(prompt);
  });
});

describe("Ref2VA optional inputs", () => {
  it("submits empty text and no references without fabricated content", () => {
    const item = { ...draft(""), imageAssetId: "", audioAssetId: "", approved: false, soundMode: "silent" as const };
    const submitted = buildVideoBatch([item]).shots[0];
    expect(submitted.prompt).toBe("");
    expect(submitted.audioAssetId).toBe("");
    expect(submitted.generation?.profile).toBe("heretic-ref2va-hmnsfw-v1");
  });
  it("submits referenced and unreferenced shots together on Ref2VA", () => {
    const textOnly = { ...draft("word"), shotId: "second", imageAssetId: "", audioAssetId: "", approved: false, soundMode: "silent" as const };
    const shots = buildVideoBatch([draft(""), textOnly]).shots;
    expect(shots).toHaveLength(2);
    expect(shots[0].prompt).toBe("");
    expect(shots[1].generation?.profile).toBe("heretic-ref2va-hmnsfw-v1");
  });
});
