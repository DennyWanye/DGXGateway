import { useEffect, useState } from "react";
import type { WorkbenchContext } from "@/lib/types";
import { requestKey, segment, uploadAudio } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import {
  PendingAction,
  putDraft,
  readDraft,
  useDurablePost,
} from "./PostproductionPanel";
import { Music, Upload } from "lucide-react";

const message = (error: any) => error?.message || String(error);
const knownRejection = (error: any) =>
  [400, 401, 403, 404, 409, 413, 415, 422].includes(error?.status);

export function SongPanel({ ctx }: { ctx: WorkbenchContext }) {
  const pid = ctx.project?.id || "";
  const eid = ctx.episode?.id || "";
  const fps = ctx.project?.fps || 24;
  const audio = (ctx.assets || []).filter((a: any) => a.kind === "audio");
  const boundId = ctx.episode?.song_asset_id;
  const [file, setFile] = useState<File>();
  const [uploading, setUploading] = useState(false);
  const key = `dgx.drama.audio.upload:${pid}`;
  const [uploadPending, setUploadPending] = useState<any>(() => readDraft(key));
  const bind = useDurablePost(
    `${pid}/${eid}`,
    "song",
    async () => {
      await ctx.refresh();
      ctx.notify("已绑定本集歌曲。时间线尚未改写。", "success");
    },
    ctx.notify,
  );
  useEffect(() => {
    setUploadPending(readDraft(key));
    setFile(undefined);
  }, [key]);
  const uploadFile = async () => {
    if (!file || uploading || !pid) return;
    setUploading(true);
    try {
      if (
        !file.size ||
        file.size > 256 * 1024 * 1024 ||
        !/\.(wav|mp3|m4a|flac)$/i.test(file.name)
      )
        throw new Error(
          "请选择非空、最大 256 MiB 的 WAV、MP3、M4A 或 FLAC 音频。",
        );
      if (uploadPending && uploadPending.filename !== file.name)
        throw new Error("请重新选择原文件名的音频，以保持请求身份。");
      const digest = Array.from(
        new Uint8Array(
          await crypto.subtle.digest("SHA-256", await file.arrayBuffer()),
        ),
        (b) => b.toString(16).padStart(2, "0"),
      ).join("");
      if (uploadPending && uploadPending.digest !== digest)
        throw new Error("请选择原提交的同一音频文件，以保持请求身份。");
      const intent = uploadPending || {
        key: requestKey(),
        digest,
        filename: file.name,
      };
      putDraft(key, intent);
      setUploadPending(intent);
      await uploadAudio(pid, file, intent.key);
      sessionStorage.removeItem(key);
      setUploadPending(null);
      setFile(undefined);
      await ctx.refresh();
      ctx.notify("声音素材已保存，可绑定为本集歌曲。", "success");
    } catch (error) {
      if (knownRejection(error)) {
        sessionStorage.removeItem(key);
        setUploadPending(null);
      }
      ctx.notify(message(error), "error");
    } finally {
      setUploading(false);
    }
  };
  const bindSong = (assetId: string) => {
    if (!eid || bind.busy || bind.pending) return;
    void bind.execute(
      `/episodes/${segment(eid)}/song`,
      { songAssetId: assetId, expectedRevision: ctx.episode.revision },
      "song",
    );
  };
  if (!pid || !eid) {
    return (
      <div className="max-w-lg py-16 text-muted-foreground">
        先创建分集，再绑定本集歌曲。
      </div>
    );
  }
  return (
    <div className="space-y-6 max-w-3xl">
      <div>
        <p className="text-xs tracking-[.15em] text-muted-foreground mb-2">
          本集主时钟
        </p>
        <h1 className="text-2xl font-semibold tracking-tight">歌曲</h1>
        <p className="text-sm text-muted-foreground mt-2 leading-7">
          选定一条已归档音频作为成片长度。绑定不会保存时间线、不会改选片。
        </p>
      </div>
      <PendingAction {...bind} />
      <Card>
        <CardHeader>
          <CardTitle className="text-base">上传音频</CardTitle>
          <CardDescription>
            走现有声音素材通道。不提交 Comfy，不切换模型。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <Label htmlFor="song-file">音频文件</Label>
          <Input
            id="song-file"
            type="file"
            accept=".wav,.mp3,.m4a,.flac,audio/wav,audio/mpeg,audio/mp4,audio/flac"
            disabled={uploading || bind.busy}
            onChange={(event) => setFile(event.target.files?.[0])}
          />
          <Button
            disabled={!file || uploading || bind.busy}
            onClick={() => void uploadFile()}
          >
            <Upload size={15} />
            {uploading ? "上传中…" : "保存到项目"}
          </Button>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle className="text-base">项目音频</CardTitle>
          <CardDescription>
            {audio.length
              ? "选择一条绑定为本集歌曲。"
              : "还没有音频素材。先上传一首歌。"}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {audio.map((asset: any) => {
            const duration = Number(asset.duration);
            const frames = Number.isFinite(duration)
              ? Math.floor(duration * fps)
              : null;
            const bound = boundId === asset.id;
            return (
              <div
                key={asset.id}
                className="flex flex-wrap items-start justify-between gap-3 rounded-xl border border-border/60 p-4"
              >
                <div className="min-w-0 space-y-1">
                  <div className="flex items-center gap-2">
                    <Music size={16} className="text-primary/80" />
                    <p className="text-sm font-medium truncate">
                      {asset.filename || asset.id}
                    </p>
                    {bound && <Badge variant="secondary">已绑定</Badge>}
                  </div>
                  <p className="text-xs text-muted-foreground tabular-nums break-all">
                    {Number.isFinite(duration)
                      ? `${duration.toFixed(3)} 秒 · ${frames} 帧`
                      : "时长未知"}
                  </p>
                  <p className="text-[11px] text-muted-foreground/80 break-all font-mono">
                    {asset.id}
                  </p>
                </div>
                <Button
                  variant={bound ? "outline" : "default"}
                  size="sm"
                  disabled={bound || bind.busy || !!bind.pending}
                  onClick={() => bindSong(asset.id)}
                >
                  {bound ? "当前歌曲" : "绑定为本集歌曲"}
                </Button>
              </div>
            );
          })}
        </CardContent>
      </Card>
    </div>
  );
}
