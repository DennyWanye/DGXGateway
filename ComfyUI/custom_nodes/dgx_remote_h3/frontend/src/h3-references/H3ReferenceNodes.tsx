import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import {
  BUILTIN_ROLES,
  compileOfficialPrompt,
  roleHint,
  type CustomRole,
  type MediaKind,
  type ReferenceNode,
} from "@/lib/reference-nodes";

type Asset = { id: string; name: string; kind: MediaKind };
type Uploaded = { id: string; name: string };

type Props = {
  nodes: ReferenceNode[];
  customRoles: CustomRole[];
  assets: Asset[];
  instruction: string;
  previewUrl: (assetId: string) => string;
  onChange: (nodes: ReferenceNode[]) => void;
  onCreateRole: (role: CustomRole) => void;
  onUpload: (file: File, media: MediaKind) => Promise<Uploaded>;
};

const MEDIA: [MediaKind, string, string][] = [
  ["image", "图片", "image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"],
  ["video", "视频", "video/mp4,.mp4"],
  ["audio", "音频", "audio/wav,.wav"],
];

export function H3ReferenceNodes({ nodes, customRoles, assets, instruction, previewUrl, onChange, onCreateRole, onUpload }: Props) {
  const [label, setLabel] = useState("");
  const [token, setToken] = useState("");
  const [preview, setPreview] = useState("");
  const [uploading, setUploading] = useState("");
  const [error, setError] = useState("");
  const [names, setNames] = useState<Record<string, string>>({});
  const update = (id: string, patch: Partial<ReferenceNode>) => onChange(nodes.map((node) => node.id === id ? { ...node, ...patch } : node));
  const add = () => onChange([...nodes, { id: `node-${nodes.length + 1}-${Date.now()}`, media: "image", assetId: "", role: "character", description: "" }]);
  const createRole = () => {
    const name = label.trim();
    const word = token.trim();
    if (!name || !word || !/^[A-Za-z][A-Za-z0-9_-]{0,40}$/.test(word)) return;
    onCreateRole({ id: `custom-${customRoles.length + 1}`, label: name, token: word });
    setLabel("");
    setToken("");
  };
  const upload = async (node: ReferenceNode, file?: File) => {
    if (!file) return;
    setUploading(node.id);
    setError("");
    try {
      const saved = await onUpload(file, node.media);
      update(node.id, { assetId: saved.id });
      setNames((current) => ({ ...current, [saved.id]: saved.name }));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "上传失败");
    } finally {
      setUploading("");
    }
  };
  const openPreview = () => setPreview(compileOfficialPrompt({ instruction, nodes, customRoles }));
  return (
    <div className="space-y-4">
      {nodes.map((node, index) => {
        const name = names[node.assetId] || assets.find((asset) => asset.id === node.assetId)?.name || "";
        return (
          <div key={node.id} className="space-y-3 rounded-lg border p-4">
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm font-medium">参考节点 {index + 1}</p>
              <Button type="button" variant="ghost" onClick={() => onChange(nodes.filter((item) => item.id !== node.id))}>移除</Button>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="space-y-2 text-sm">
                <span>文件类型</span>
                <select aria-label={`参考节点 ${index + 1} 文件类型`} className="h-9 w-full rounded-md border bg-background px-3" value={node.media} onChange={(event) => update(node.id, { media: event.target.value as MediaKind, assetId: "" })}>
                  {MEDIA.map(([value, title]) => <option key={value} value={value}>{title}</option>)}
                </select>
              </label>
              <label className="space-y-2 text-sm">
                <span>用途 · H3 稿子写法</span>
                <select aria-label={`参考节点 ${index + 1} 用途`} className="h-9 w-full rounded-md border bg-background px-3" value={node.role === "custom" ? `custom:${node.customRoleId || ""}` : node.role} onChange={(event) => {
                  const value = event.target.value;
                  if (value.startsWith("custom:")) update(node.id, { role: "custom", customRoleId: value.slice(7) });
                  else update(node.id, { role: value as ReferenceNode["role"], customRoleId: undefined });
                }}>
                  {BUILTIN_ROLES.map((role) => <option key={role.id} value={role.id}>{role.label} · {role.hint}</option>)}
                  {customRoles.map((role) => <option key={role.id} value={`custom:${role.id}`}>{role.label} · 自定义 · {role.token}</option>)}
                </select>
              </label>
            </div>
            <p className="text-xs text-muted-foreground">{roleHint(node, customRoles)}</p>
            <div className="flex flex-wrap items-center gap-3">
              <Button type="button" variant="outline" disabled={uploading === node.id} onClick={() => document.getElementById(`upload-${node.id}`)?.click()}>
                {uploading === node.id ? "正在上传…" : node.assetId ? "更换文件" : "从电脑上传"}
              </Button>
              <input id={`upload-${node.id}`} className="hidden" type="file" aria-label={`参考节点 ${index + 1} 上传`} accept={MEDIA.find(([value]) => value === node.media)?.[2]} onChange={(event) => { void upload(node, event.target.files?.[0]); event.target.value = ""; }} />
              {name && <span className="text-sm text-muted-foreground">{name}</span>}
            </div>
            {node.assetId && <NodePreview media={node.media} name={name || "参考素材"} src={previewUrl(node.assetId)} />}
            <label className="space-y-2 text-sm">
              <span>参考图描述</span>
              <Textarea aria-label={`参考节点 ${index + 1} 参考图描述`} value={node.description} onChange={(event) => update(node.id, { description: event.target.value })} />
            </label>
          </div>
        );
      })}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
      <Button type="button" variant="outline" onClick={add}>添加参考节点</Button>
      <div className="grid gap-3 rounded-lg bg-muted/40 p-4 sm:grid-cols-[1fr_1fr_auto] sm:items-end">
        <label className="space-y-2 text-sm"><span>用途名称（例如：人物）</span><Input aria-label="用途名称" value={label} placeholder="人物" onChange={(event) => setLabel(event.target.value)} /></label>
        <label className="space-y-2 text-sm"><span>提示词 title（例如：Subject / &lt;Picture N&gt;）</span><Input aria-label="提示词 title" value={token} placeholder="Subject" onChange={(event) => setToken(event.target.value)} /></label>
        <Button type="button" variant="secondary" onClick={createRole}>添加自定义用途</Button>
      </div>
      <Button type="button" onClick={openPreview}>预览提示词</Button>
      <p className="text-xs text-muted-foreground">只弹出预览，不会改上面的视频生成提示词，也不会向后台发送生成任务。</p>
      <Dialog open={Boolean(preview)} onOpenChange={(open) => { if (!open) setPreview(""); }}>
        <DialogContent className="flex max-h-[85vh] flex-col overflow-hidden sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>发给 MiniMax H3 的提示词</DialogTitle>
            <DialogDescription>这是当前视频生成提示词。已经是完整六段稿时按原文显示，还没有发送。</DialogDescription>
          </DialogHeader>
          <pre aria-label="H3 提示词预览" className="max-h-[60vh] min-h-0 overflow-y-auto whitespace-pre-wrap rounded-lg bg-muted p-4 text-sm">{preview}</pre>
        </DialogContent>
      </Dialog>
    </div>
  );
}

function NodePreview({ media, name, src }: { media: MediaKind; name: string; src: string }) {
  if (media === "video") return <video aria-label={`${name} 预览`} className="max-h-48 w-full rounded-lg bg-black" controls src={src} />;
  if (media === "audio") return <audio aria-label={`${name} 预览`} className="w-full" controls src={src} />;
  return <img alt={`${name} 预览`} className="max-h-48 rounded-lg bg-black object-contain" src={src} />;
}
