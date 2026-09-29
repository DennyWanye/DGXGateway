import { useEffect, useRef, useState } from "react";
import type { WorkbenchContext } from "@/lib/types";
import { mediaUrl, uploadImage } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Dialog, DialogContent, DialogHeader, DialogTitle, DialogDescription, DialogFooter } from "@/components/ui/dialog";
import { Image as ImageIcon, LoaderCircle, Upload } from "lucide-react";

const nameOf = (a: any) => a?.name || a?.title || a?.filename || "未命名";

export function AssetPicker({ ctx, value, onChange, label }: {
  ctx: WorkbenchContext; value: string; onChange: (id: string) => void; label: string;
}) {
  const [open, setOpen] = useState(false), [viewing, setViewing] = useState(false), [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [imported, setImported] = useState<any[]>([]);
  const input = useRef<HTMLInputElement>(null), inFlight = useRef(false);
  const alive = useRef(true), projectId = useRef(ctx.project.id);
  projectId.current = ctx.project.id;
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  useEffect(() => { setImported([]); setError(""); setNotice(""); }, [ctx.project.id]);
  const all = [...(ctx.assets || []), ...imported.filter(a => !ctx.assets.some(b => b.id === a.id))];
  const assets = all.filter(a => (a.kind === "image" || a.contentType?.startsWith("image/")) &&
    `${nameOf(a)} ${a.id}`.toLowerCase().includes(query.toLowerCase()));
  const selected = all.find(a => a.id === value);
  const chooseFile = () => { setOpen(true); input.current?.click(); };
  const importFile = async (file?: File) => {
    if (!file || inFlight.current) return;
    const pid = ctx.project.id;
    const current = () => alive.current && projectId.current === pid;
    inFlight.current = true; setBusy(true); setError(""); setNotice("");
    try {
      const { asset } = await uploadImage(pid, file);
      if (!current()) return;
      setImported(old => [...old.filter(a => a.id !== asset.id), asset]);
      onChange(asset.id);
      const message = `已导入并选中「${nameOf(asset)}」。图片已保存到项目素材库。`;
      setNotice(message); setOpen(false); ctx.notify(message, "success");
      try { await ctx.refresh(); }
      catch {
        if (current()) {
          const warning = "图片已导入并选中，但素材列表刷新失败。请稍后刷新项目数据，无需重复导入。";
          setNotice(warning); ctx.notify(warning, "info");
        }
      }
    } catch (e: any) {
      if (current()) { setError(e.message || "导入失败，请重试。"); setOpen(true); }
    } finally {
      inFlight.current = false;
      if (current()) setBusy(false);
    }
  };
  const picture = (id: string, alt: string, className: string) => id
    ? <img src={mediaUrl(ctx.project.id, id)} alt={alt} loading="lazy" className={`rounded-lg bg-black/15 object-contain ${className}`} />
    : <div className={`grid place-items-center rounded-lg bg-muted text-muted-foreground ${className}`}><ImageIcon className="size-8 opacity-40" /></div>;
  return <div className="space-y-2" aria-busy={busy}>
    <Label>{label}</Label>
    <div className="flex min-h-24 w-full items-center gap-3 rounded-xl border p-3">
      {value ? <button type="button" aria-label={`${label}：查看图片`} disabled={busy}
        className="shrink-0 rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => setViewing(true)}>
        {picture(value, nameOf(selected), "h-16 w-24")}
      </button> : picture("", "参考图片", "h-16 w-24 shrink-0")}
      <button type="button" aria-label={`${label}：${selected ? nameOf(selected) : "选择参考图片"}`}
        disabled={busy} className="min-w-0 flex-1 text-left outline-none focus-visible:ring-2 focus-visible:ring-ring"
        onClick={() => setOpen(true)}>
        <span className="block truncate">{selected ? nameOf(selected) : "选择参考图片"}</span>
        <span className="mt-1 block text-xs text-muted-foreground">{selected
          ? `${selected.width || "?"} × ${selected.height || "?"} · 点击更换`
          : "从项目素材库选择，或从电脑导入"}</span>
      </button>
    </div>
    <Button type="button" variant="outline" size="sm" disabled={busy}
      aria-label={`${label}：从电脑导入`} onClick={chooseFile}>
      {busy ? <LoaderCircle className="size-4 animate-spin" /> : <Upload className="size-4" />}
      {busy ? "正在导入…" : "从电脑导入"}
    </Button>
    <input ref={input} type="file" className="hidden" accept="image/png,image/jpeg,image/webp,.png,.jpg,.jpeg,.webp"
      aria-label={`${label}：选择电脑图片文件`} disabled={busy}
      onChange={e => { const file = e.currentTarget.files?.[0]; e.currentTarget.value = ""; void importFile(file); }} />
    {notice && <p role="status" className="text-xs text-muted-foreground">{notice}</p>}
    <Dialog open={viewing} onOpenChange={setViewing}>
      <DialogContent aria-label="查看图片" className="flex h-[100dvh] max-h-none w-screen max-w-none translate-x-[-50%] translate-y-[-50%] items-center justify-center rounded-none border-0 bg-black/95 p-6 sm:max-w-none">
        <DialogHeader className="sr-only"><DialogTitle>查看图片</DialogTitle>
          <DialogDescription>{selected ? nameOf(selected) : label}</DialogDescription>
        </DialogHeader>
        {value && <img src={mediaUrl(ctx.project.id, value)} alt={nameOf(selected)} className="max-h-[92dvh] max-w-[94vw] object-contain" />}
      </DialogContent>
    </Dialog>
    <Dialog open={open} onOpenChange={v => { if (!inFlight.current) setOpen(v); }}>
      <DialogContent aria-busy={busy} className="max-h-[85vh] sm:max-w-3xl overflow-y-auto"
        onEscapeKeyDown={e => { if (inFlight.current) e.preventDefault(); }}
        onInteractOutside={e => { if (inFlight.current) e.preventDefault(); }}>
        <DialogHeader><DialogTitle>{label}</DialogTitle>
          <DialogDescription>选择项目图片，或从电脑任意文件夹导入。PNG / JPEG / WebP，最大 8 MiB；导入后自动选中，不会自动批准或生成。</DialogDescription>
        </DialogHeader>
        <div className="flex flex-wrap items-center gap-3">
          <Button type="button" disabled={busy} onClick={chooseFile}><Upload className="size-4" />从电脑导入</Button>
          <span className="text-xs text-muted-foreground">导入的图片也会保存在项目素材库。</span>
        </div>
        {busy && <p role="status" className="flex items-center gap-2 text-sm"><LoaderCircle className="size-4 animate-spin" />正在上传并校验图片，请稍候…</p>}
        {error && <p role="alert" className="rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</p>}
        <Input aria-label="搜索参考图片" placeholder="搜索文件名或素材编号" value={query} disabled={busy} onChange={e => setQuery(e.target.value)} />
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
          {assets.map(a => <button type="button" disabled={busy} key={a.id} aria-label={`${nameOf(a)} · ${a.width} × ${a.height}`}
            onClick={() => { onChange(a.id); setNotice(""); setOpen(false); }}
            className={`space-y-2 rounded-xl border p-2 text-left transition hover:border-primary ${value === a.id ? "border-primary bg-primary/5" : ""}`}>
            {picture(a.id, nameOf(a), "aspect-video w-full")}
            <span className="block truncate text-xs">{nameOf(a)}</span><span className="block text-xs text-muted-foreground">{a.width} × {a.height}{value === a.id ? " · 已选择" : ""}</span>
          </button>)}
        </div>
        {!assets.length && <p className="py-6 text-center text-sm text-muted-foreground">{query ? "没有匹配的图片，可修改搜索或从电脑导入。" : "项目还没有图片。点击上方「从电脑导入」即可开始。"}</p>}
        <DialogFooter><Button type="button" variant="outline" disabled={busy} onClick={() => { onChange(""); setNotice(""); setOpen(false); }}>清除此项选择</Button></DialogFooter>
      </DialogContent>
    </Dialog>
  </div>;
}
