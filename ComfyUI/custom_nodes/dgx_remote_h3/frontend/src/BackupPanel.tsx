import {
  restoredWorkspaceUrl,
  sessionStore as sessionStorage,
} from "@/lib/workspace";
import { useEffect, useRef, useState } from "react";
import type { WorkbenchContext } from "@/lib/types";
import {
  get,
  post,
  requestKey,
  segment,
  apiUrl,
  uploadRestore,
} from "@/lib/api";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Archive, Download, FolderOpen, RefreshCw, Upload } from "lucide-react";
type Intent = {
  path: string;
  body: any;
  key: string;
  upload?: { name: string; size: number };
};
const read = (key: string) => {
  try {
    return JSON.parse(sessionStorage.getItem(key) || "null");
  } catch {
    return null;
  }
};
const sizes = (bytes: number) =>
  `${((bytes || 0) / 1024 / 1024).toFixed(1)} MiB`;
const states: Record<string, string> = {
  QUEUED: "等待处理",
  RUNNING: "正在处理",
  SUCCEEDED: "已完成",
  FAILED: "未完成",
  INTERRUPTED: "需要重新核查",
};
export function BackupPanel({ ctx }: { ctx: WorkbenchContext }) {
  const pid = ctx.project?.id || "",
    storage = `dgx.drama.backup.pending:${pid}`;
  const [uploaded, setUploaded] = useState<any>(() =>
    read(storage + ":uploaded"),
  );
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [operations, setOperations] = useState<any[]>([]),
    [pending, setPending] = useState<Intent | null>(() => read(storage)),
    [busy, setBusy] = useState(false),
    [file, setFile] = useState<File | null>(null),
    [confirm, setConfirm] = useState<any>(null);
  const lock = useRef(false),
    scope = useRef(pid),
    root = useRef<HTMLElement>(null),
    initiated = useRef<any[]>(read(storage + ":operations") || []);
  scope.current = pid;
  const refresh = async () => {
    if (!pid || !ctx.capabilities?.backupEnabled) return;
    const value = await get(`/projects/${segment(pid)}/backups`);
    const watched = await Promise.all(
      initiated.current.map(async (op) => {
        try {
          return (
            (
              await get(
                `/${op.kind === "restore" ? "restores" : "backups"}/${segment(op.id)}`,
              )
            ).operation || op
          );
        } catch {
          return op;
        }
      }),
    );
    if (scope.current === pid) {
      const unique = new Map(
        [...(value.operations || []), ...watched].map((op) => [op.id, op]),
      );
      setOperations([...unique.values()]);
      setNextCursor(value.nextCursor || null);
    }
  };
  useEffect(() => {
    setOperations([]);
    setNextCursor(null);
    initiated.current = read(storage + ":operations") || [];
    setPending(read(storage));
    setUploaded(read(storage + ":uploaded"));
    setFile(null);
    setConfirm(null);
    void refresh().catch((e) => ctx.notify(e.message, "error"));
  }, [pid, !!ctx.capabilities?.backupEnabled]);
  useEffect(() => {
    const timer = setInterval(() => {
      if (
        !lock.current &&
        !root.current?.closest("[hidden]") &&
        operations.some((x) => ["QUEUED", "RUNNING"].includes(x.state))
      )
        void refresh().catch((e) => ctx.notify(e.message, "error"));
    }, 2000);
    return () => clearInterval(timer);
  }, [pid, operations]);
  const dirty = !!pending || !!file;
  useEffect(() => {
    const before = (e: BeforeUnloadEvent) => {
      if (dirty) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", before);
    return () => window.removeEventListener("beforeunload", before);
  }, [dirty]);
  const send = async (intent: Intent) => {
    if (lock.current) return;
    lock.current = true;
    setBusy(true);
    try {
      sessionStorage.setItem(storage, JSON.stringify(intent));
      setPending(intent);
      let result: any;
      if (intent.upload) {
        if (
          !file ||
          file.name !== intent.upload.name ||
          file.size !== intent.upload.size
        )
          throw new Error(
            "请重新选择原项目包，再按原请求键核查。服务会核对完整内容hash。",
          );
        result = await uploadRestore(file, intent.key);
        if (!result.upload?.id)
          throw new Error("上传回执缺少编号，请保留原键核查。");
        if (scope.current !== pid) return;
        sessionStorage.setItem(
          storage + ":uploaded",
          JSON.stringify(result.upload),
        );
        setUploaded(result.upload);
        sessionStorage.removeItem(storage);
        setPending(null);
        setFile(null);
        ctx.notify("项目包校验完成。请核对摘要后，再明确恢复。", "success");
        return;
      } else result = await post(intent.path, intent.body, intent.key);
      if (scope.current !== pid) return;
      if (!result.operation?.id && !result.mode)
        throw new Error("回执缺少操作编号，请按原请求键核查。");
      if (result.operation?.id) {
        const op = {
          ...result.operation,
          kind:
            result.operation.kind ||
            (intent.path === "/restores" ? "restore" : "backup"),
        };
        initiated.current = [
          op,
          ...initiated.current.filter((x) => x.id !== op.id),
        ].slice(0, 100);
        sessionStorage.setItem(
          storage + ":operations",
          JSON.stringify(initiated.current),
        );
        setOperations((old) => [op, ...old.filter((x) => x.id !== op.id)]);
      }
      sessionStorage.removeItem(storage);
      setPending(null);
      await refresh();
      ctx.notify(
        result.mode
          ? "原任务仅查询模式已启用；生成和取消仍隔离。"
          : "操作已记录，可在下方查看进度。",
        "success",
      );
    } catch (e: any) {
      if (
        [400, 403, 404, 409, 413, 422, 507].includes(e.status) &&
        !(intent.upload && e.status === 409)
      ) {
        sessionStorage.removeItem(storage);
        setPending(null);
      }
      ctx.notify(e.message, "error");
    } finally {
      lock.current = false;
      if (scope.current === pid) setBusy(false);
    }
  };
  const write = (path: string, body: any) => {
    if (!pending) void send({ path, body, key: requestKey() });
  };
  if (!ctx.capabilities?.backupEnabled)
    return (
      <Card>
        <CardContent className="p-6 text-muted-foreground">
          备份服务尚未启用。
        </CardContent>
      </Card>
    );
  return (
    <section
      ref={root}
      data-dirty={dirty}
      aria-busy={busy}
      className="space-y-6"
      aria-label="项目备份与恢复"
    >
      <div className="flex flex-wrap justify-between gap-3">
        <div>
          <p className="text-xs tracking-[.2em] text-primary">
            PROJECT ARCHIVE
          </p>
          <h2 className="mt-2 text-2xl font-semibold">让故事安全地留下来</h2>
          <p className="mt-2 text-sm text-muted-foreground">
            备份当前项目的素材、修订与制作记录。恢复到独立工作区，模型任务保持暂停。
          </p>
        </div>
        <Button
          variant="outline"
          disabled={busy}
          onClick={() =>
            void refresh().catch((e) => ctx.notify(e.message, "error"))
          }
        >
          <RefreshCw className="size-4" />
          刷新归档记录
        </Button>
      </div>
      {ctx.capabilities.restoreMode && (
        <div
          role="status"
          className="rounded-lg border border-amber-500/30 p-4 text-sm"
        >
          这是恢复工作区 ·{" "}
          {ctx.capabilities.restoreMode === "query_only"
            ? "只允许显式核查原任务"
            : "模型任务已隔离"}
          。可以继续手写、查看媒体和新建CPU导出。
        </div>
      )}
      {pending && (
        <Card>
          <CardContent className="space-y-3 pt-6">
            <p role="alert">原操作结果尚未确认。重开页面不会自动重做。</p>
            <p className="break-all text-xs text-muted-foreground">
              {pending.key}
            </p>
            <Button disabled={busy} onClick={() => void send(pending)}>
              按原请求键核查归档操作
            </Button>
          </CardContent>
        </Card>
      )}
      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <Archive className="mb-2 size-6 text-primary" />
            <CardTitle>备份这个项目</CardTitle>
            <CardDescription>
              {ctx.project?.title} · 只包含本项目，保留所有历史与绑定素材。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <p className="text-sm text-muted-foreground">
              快照不会停止模型。存在未完成任务时，项目包会标记“需要核查恢复”。空间不足时会停止，保留至少128MiB余量。
            </p>
            <Button
              disabled={busy || !!pending}
              onClick={() =>
                write(`/projects/${segment(pid)}/backups`, {
                  expectedProjectRevision: ctx.project.revision,
                })
              }
            >
              创建项目备份
            </Button>
          </CardContent>
        </Card>
        <Card>
          <CardHeader>
            <Upload className="mb-2 size-6 text-primary" />
            <CardTitle>从项目包恢复</CardTitle>
            <CardDescription>
              选择本工作台生成的 .drama.zip；将创建新的空工作区。
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <Label htmlFor="backup-file">项目备份文件</Label>
            <Input
              id="backup-file"
              type="file"
              accept=".zip"
              disabled={busy || (!!pending && !pending.upload)}
              onChange={(e) => setFile(e.target.files?.[0] || null)}
            />
            {file && (
              <p className="text-sm text-muted-foreground">
                {file.name} · {sizes(file.size)}
              </p>
            )}
            <Button
              variant="outline"
              disabled={busy || !!pending || !file}
              onClick={() =>
                file &&
                void send({
                  path: "/restore-uploads",
                  body: {},
                  key: requestKey(),
                  upload: { name: file.name, size: file.size },
                })
              }
            >
              上传并检查项目包
            </Button>
            {file && !pending && (
              <Button
                variant="ghost"
                disabled={busy}
                onClick={() => setFile(null)}
              >
                清除文件选择
              </Button>
            )}
          </CardContent>
        </Card>
      </div>
      {uploaded && (
        <Card>
          <CardHeader>
            <CardTitle>项目包已检查</CardTitle>
            <CardDescription>
              原项目 {uploaded.preview?.projectId} · {sizes(uploaded.bytes)} ·{" "}
              {uploaded.preview?.assetCount || 0} 个素材 ·{" "}
              {Object.keys(uploaded.preview?.tables || {}).length} 张业务表
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            <p className="text-sm text-muted-foreground">
              {uploaded.preview?.snapshotKind === "stable"
                ? "快照任务已稳定。"
                : "包含待核查任务。"}
              恢复后仍保持隔离，不会重新生成。
            </p>
            <Button
              disabled={busy || !!pending}
              onClick={() => setConfirm({ uploaded })}
            >
              恢复已检查项目包
            </Button>
            <Button
              variant="ghost"
              disabled={busy || !!pending}
              onClick={() => {
                setUploaded(null);
                sessionStorage.removeItem(storage + ":uploaded");
              }}
            >
              收起项目包摘要
            </Button>
          </CardContent>
        </Card>
      )}
      <Card>
        <CardHeader>
          <CardTitle>归档与恢复记录</CardTitle>
          <CardDescription>
            生成包不等于已验证恢复；请在新工作区打开媒体并重新导出。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          {!operations.length && (
            <p className="py-6 text-center text-sm text-muted-foreground">
              还没有这个项目的归档记录。
            </p>
          )}
          {nextCursor && (
            <Button
              variant="outline"
              disabled={busy}
              onClick={async () => {
                try {
                  const value = await get(
                    `/projects/${segment(pid)}/backups?before=${segment(nextCursor)}`,
                  );
                  if (scope.current === pid) {
                    setOperations((old) => [
                      ...old,
                      ...(value.operations || []).filter(
                        (x: any) => !old.some((y) => y.id === x.id),
                      ),
                    ]);
                    setNextCursor(value.nextCursor || null);
                  }
                } catch (e: any) {
                  ctx.notify(e.message, "error");
                }
              }}
            >
              查看更早的备份记录
            </Button>
          )}
          {operations.map((op) => (
            <div key={op.id} className="space-y-3 rounded-xl border p-4">
              <div className="flex flex-wrap justify-between gap-2">
                <p className="font-medium">
                  {op.kind === "backup" ? "项目备份" : "空工作区恢复"}
                </p>
                <Badge
                  variant={op.state === "FAILED" ? "destructive" : "outline"}
                >
                  {states[op.state] || op.state}
                </Badge>
              </div>
              <p className="text-sm text-muted-foreground">
                {op.phase} · {sizes(op.bytesDone)} / {sizes(op.bytesTotal)}
              </p>
              {op.error && (
                <p role="alert" className="text-sm text-destructive">
                  {op.error}
                </p>
              )}
              {op.result?.manifest && (
                <p className="text-sm">
                  {op.result.manifest.snapshotKind === "stable"
                    ? "快照完整，任务已稳定"
                    : "包含待核查任务；恢复后保持隔离"}{" "}
                  · {sizes(op.result.bytes)}
                </p>
              )}
              <div className="flex flex-wrap gap-2">
                {op.state === "SUCCEEDED" && op.kind === "backup" && (
                  <>
                    <Button variant="outline" asChild>
                      <a
                        href={apiUrl(`/backups/${segment(op.id)}/download`)}
                        download
                      >
                        <Download className="size-4" />
                        下载项目包
                      </a>
                    </Button>
                    <Button
                      variant="outline"
                      disabled={busy || !!pending}
                      onClick={() => setConfirm({ backup: op })}
                    >
                      恢复这个备份
                    </Button>
                  </>
                )}
                {op.state === "SUCCEEDED" && op.kind === "restore" && (
                  <>
                    <Button variant="outline" asChild>
                      <a
                        href={restoredWorkspaceUrl(op.id)}
                        target="_blank"
                        rel="noreferrer"
                      >
                        <FolderOpen className="size-4" />
                        打开恢复工作区
                      </a>
                    </Button>
                    <Button
                      variant="ghost"
                      disabled={busy || !!pending}
                      onClick={() => setConfirm({ query: op })}
                    >
                      启用原任务只读核查
                    </Button>
                  </>
                )}
              </div>
              <details className="text-xs text-muted-foreground">
                <summary>操作编号与校验摘要</summary>
                <p className="mt-2 break-all">
                  {op.id} ·{" "}
                  {op.result?.sha256 || op.result?.backupId || op.requestKey}
                </p>
                {op.result?.recovery?.tasks?.map((task: any) => (
                  <p key={task.id} className="mt-2 break-all">
                    {task.table} · {task.id} · {task.state} · 原Gateway{" "}
                    {task.gatewayId || "待核查"}
                  </p>
                ))}
              </details>
            </div>
          ))}
        </CardContent>
      </Card>
      <Dialog
        open={!!confirm}
        onOpenChange={(open) => {
          if (!busy && !open) setConfirm(null);
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {confirm?.query
                ? "启用原任务只读核查？"
                : "恢复到独立的新工作区？"}
            </DialogTitle>
            <DialogDescription>
              {confirm?.query
                ? "只允许显式查询原Gateway任务，不会重新生成、取消远端任务或导入凭据。"
                : "只写新的空工作区，不覆盖当前项目。程序将校验每个文件hash；恢复后模型任务保持隔离。"}
              {confirm?.backup &&
                ` 项目包：${sizes(confirm.backup.result?.bytes)}。`}
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setConfirm(null)}
            >
              返回
            </Button>
            <Button
              disabled={busy}
              onClick={() => {
                const choice = confirm;
                setConfirm(null);
                if (choice.query)
                  write(`/restores/${segment(choice.query.id)}/query-mode`, {
                    mode: "query_only",
                  });
                else if (choice.backup)
                  write("/restores", { backupId: choice.backup.id });
                else if (choice.uploaded)
                  write("/restores", { uploadId: choice.uploaded.id });
                else if (file)
                  void send({
                    path: "/restore-uploads",
                    body: {},
                    key: requestKey(),
                    upload: { name: file.name, size: file.size },
                  });
              }}
            >
              确认继续
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </section>
  );
}
