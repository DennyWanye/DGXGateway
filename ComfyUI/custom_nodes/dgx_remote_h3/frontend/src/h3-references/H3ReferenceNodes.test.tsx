// @vitest-environment jsdom
import { useState } from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom/vitest";
import { H3ReferenceNodes } from "./H3ReferenceNodes";
import type { ReferenceNode } from "@/lib/reference-nodes";

afterEach(cleanup);

function Harness({ submit }: { submit: () => void }) {
  const [nodes, setNodes] = useState<ReferenceNode[]>([
    { id: "person", media: "image", assetId: "sheet", role: "character", description: "Keep the face." },
  ]);
  const [instruction, setInstruction] = useState("Turn once and settle.");
  return (
    <div>
      <textarea aria-label="视频生成提示词" value={instruction} onChange={(event) => setInstruction(event.target.value)} />
      <H3ReferenceNodes
        nodes={nodes}
        customRoles={[]}
        assets={[{ id: "sheet", name: "天舞全位图", kind: "image" }]}
        instruction={instruction}
        previewUrl={(id) => `/media/${id}`}
        onChange={setNodes}
        onCreateRole={() => {}}
        onUpload={async (file) => ({ id: "uploaded", name: file.name })}
      />
      <button type="button" onClick={submit}>提交当前批次</button>
    </div>
  );
}

describe("reference node prompt button", () => {
  it("previews an official prompt without changing the prompt field or submitting", async () => {
    const submit = vi.fn();
    render(<Harness submit={submit} />);
    fireEvent.click(screen.getByRole("button", { name: "添加参考节点" }));
    fireEvent.change(screen.getAllByLabelText(/文件类型/)[1], { target: { value: "video" } });
    fireEvent.change(screen.getAllByLabelText(/上传/)[1], { target: { files: [new File(["video"], "turn.mp4", { type: "video/mp4" })] } });
    expect(await screen.findByLabelText("turn.mp4 预览")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "预览提示词" }));
    const preview = await screen.findByLabelText("H3 提示词预览");
    for (const section of ["subject_definitions:", "summary:", "retention_analysis:", "detailed_description:", "overall_soundscape:", "non_diegetic_music:"]) {
      expect(preview.textContent).toContain(section);
    }
    expect(preview.textContent).toContain("<Subject 1> is the person in <Picture 1>");
    expect(preview.textContent).toContain("<Video 1>");
    expect(preview.textContent).toContain("[Shot 1] Turn once and settle.");
    expect(screen.getByLabelText("视频生成提示词")).toHaveValue("Turn once and settle.");
    expect(screen.getByLabelText("H3 提示词预览").className).toContain("overflow-y-auto");
    expect(screen.getByLabelText("用途名称")).toHaveAttribute("placeholder", "人物");
    expect(screen.getByLabelText("提示词 title")).toHaveAttribute("placeholder", "Subject");
    expect(submit).not.toHaveBeenCalled();
  });

  it("shows a saved six-section prompt in full instead of a short rewrite", () => {
    const stored = [
      "subject_definitions:",
      "<Subject 1> is the dancer in <Picture 1>. She is not merely beautiful. She is mesmerizing.",
      "summary:",
      "[reference generation] One 7-second shot.",
      "retention_analysis:",
      "Keep <Picture 1>.",
      "detailed_description:",
      "[Shot 1] A long temporal structure that must remain visible in the preview. Infinite scale. Celestial ritual.",
      "overall_soundscape:",
      "No dialogue, no speech, no voices.",
      "non_diegetic_music:",
      "No added score.",
    ].join("\n");
    render(
      <H3ReferenceNodes
        nodes={[{ id: "person", media: "image", assetId: "sheet", role: "character", description: "short node note" }]}
        customRoles={[]}
        assets={[{ id: "sheet", name: "天舞全位图", kind: "image" }]}
        instruction={stored}
        previewUrl={(id) => `/media/${id}`}
        onChange={() => {}}
        onCreateRole={() => {}}
        onUpload={async () => ({ id: "x", name: "x" })}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "预览提示词" }));
    const preview = screen.getByLabelText("H3 提示词预览").textContent || "";
    expect(preview).toContain("Infinite scale. Celestial ritual.");
    expect(preview).toContain("not merely beautiful");
    expect(preview).not.toContain("short node note");
  });
});
