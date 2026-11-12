"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  Eraser,
  LoaderCircle,
  Pause,
  Play,
  Square,
  Users,
} from "lucide-react";
import { toast } from "sonner";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  fetchBatchStatus,
  fetchMailProviders,
  pauseBatchRegister,
  resumeBatchRegister,
  startBatchRegister,
  stopBatchRegister,
  type BatchStatus,
  type MailProvider,
} from "@/lib/register-api";

type LogLine = {
  id: number;
  kind: string;
  message: string;
  level?: string;
  time: string;
};

const stateLabel: Record<string, string> = {
  running: "运行中",
  paused: "已暂停",
  stopped: "已停止",
};

export function BatchRegisterPanel() {
  const [status, setStatus] = useState<BatchStatus | null>(null);
  const [providers, setProviders] = useState<MailProvider[]>([]);
  const [mailSource, setMailSource] = useState("");
  const [concurrency, setConcurrency] = useState(1);
  const [targetCount, setTargetCount] = useState(0);
  const [coolDown, setCoolDown] = useState(3);
  const [otpTimeout, setOtpTimeout] = useState(180);
  const [wantRefreshToken, setWantRefreshToken] = useState(false);
  const [want2fa, setWant2fa] = useState(false);
  const [autoImport, setAutoImport] = useState(true);
  const [withProxy, setWithProxy] = useState(true);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const logIdRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const pushLog = useCallback((kind: string, message: string, level?: string) => {
    logIdRef.current += 1;
    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, "0")}:${String(
      now.getMinutes(),
    ).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
    setLogs((prev) =>
      [...prev, { id: logIdRef.current, kind, message, level, time }].slice(-500),
    );
  }, []);

  // 状态轮询
  useEffect(() => {
    let active = true;
    const load = async () => {
      try {
        const data = await fetchBatchStatus();
        if (active) setStatus(data);
      } catch {
        /* 忽略轮询失败 */
      }
    };
    void load();
    const timer = setInterval(load, 2000);
    return () => {
      active = false;
      clearInterval(timer);
    };
  }, []);

  // 邮箱来源
  useEffect(() => {
    let active = true;
    void (async () => {
      try {
        const { providers: list } = await fetchMailProviders();
        if (!active) return;
        setProviders(list);
        if (list.length > 0) setMailSource(list[0].kind);
      } catch {
        /* 忽略 */
      }
    })();
    return () => {
      active = false;
    };
  }, []);

  // SSE 实时日志
  useEffect(() => {
    const source = new EventSource("/api/register/batch/stream");
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as {
          kind?: string;
          data?: Record<string, unknown>;
          message?: string;
        };
        if (payload.kind === "state" || payload.kind === "run_started" || payload.kind === "run_finished") {
          const data = payload.data ?? {};
          if (payload.kind === "state" && data.state) {
            setStatus((prev) => (prev ? { ...prev, ...(data as Partial<BatchStatus>) } : prev));
          } else if (payload.kind === "run_started") {
            pushLog("run", `开始注册 ${String(data.email ?? "")}`);
          } else if (payload.kind === "run_finished") {
            const ok = Boolean(data.ok);
            pushLog(
              "run",
              ok
                ? `注册成功 ${String(data.email ?? "")}`
                : `注册失败 ${String(data.email ?? "")}${data.category ? `（${String(data.category)}）` : ""}`,
              ok ? "ok" : "error",
            );
          }
          return;
        }
        if (payload.kind === "ping") return;
        pushLog("log", payload.message ?? String(payload.data ?? ""));
      } catch {
        /* 忽略解析失败 */
      }
    };
    source.onerror = () => {
      // EventSource 会自动重连，这里不额外处理
    };
    return () => source.close();
  }, [pushLog]);

  // 日志滚动到底
  useEffect(() => {
    logEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [logs]);

  const handleStart = async () => {
    setBusy(true);
    try {
      await startBatchRegister({
        concurrency: Number(concurrency) || 1,
        target_count: Number(targetCount) || 0,
        cool_down_seconds: Number(coolDown) || 0,
        proxy: "",
        mail_source: mailSource,
        want_access_token: true,
        want_session_token: true,
        want_refresh_token: wantRefreshToken,
        want_2fa: want2fa,
        otp_timeout: Number(otpTimeout) || 180,
        auto_import: autoImport,
        with_proxy: withProxy,
      });
      setLogs([]);
      toast.success("批量注册已启动");
    } catch (error) {
      toast.error(`启动失败：${(error as Error).message ?? error}`);
    } finally {
      setBusy(false);
    }
  };

  const callAction = async (
    fn: () => Promise<unknown>,
    okMessage: string,
  ) => {
    setBusy(true);
    try {
      await fn();
      toast.success(okMessage);
    } catch (error) {
      toast.error((error as Error).message ?? String(error));
    } finally {
      setBusy(false);
    }
  };

  const state = status?.state ?? "stopped";
  const isRunning = state === "running";

  return (
    <div className="grid gap-4 lg:grid-cols-[minmax(320px,380px)_1fr]">
      {/* 左：参数 + 控制 */}
      <div className="space-y-4">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">批量参数</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label>邮箱来源</Label>
              <Select value={mailSource} onValueChange={setMailSource}>
                <SelectTrigger>
                  <SelectValue placeholder="选择邮箱来源" />
                </SelectTrigger>
                <SelectContent>
                  {providers.map((p) => (
                    <SelectItem key={p.kind} value={p.kind}>
                      {p.display_name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="reg-concurrency">并发数</Label>
                <Input
                  id="reg-concurrency"
                  type="number"
                  min={1}
                  max={20}
                  value={concurrency}
                  onChange={(e) => setConcurrency(Number(e.target.value))}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reg-target">目标成功数</Label>
                <Input
                  id="reg-target"
                  type="number"
                  min={0}
                  value={targetCount}
                  onChange={(e) => setTargetCount(Number(e.target.value))}
                />
              </div>
            </div>
            <p className="text-xs text-muted-foreground">目标成功数填 0 表示不限制</p>

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="reg-cooldown">冷却（秒）</Label>
                <Input
                  id="reg-cooldown"
                  type="number"
                  min={0}
                  value={coolDown}
                  onChange={(e) => setCoolDown(Number(e.target.value))}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="reg-otp">验证码超时（秒）</Label>
                <Input
                  id="reg-otp"
                  type="number"
                  min={60}
                  value={otpTimeout}
                  onChange={(e) => setOtpTimeout(Number(e.target.value))}
                />
              </div>
            </div>

            <div className="space-y-2.5 border-t pt-3">
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={autoImport}
                  onCheckedChange={(v) => setAutoImport(Boolean(v))}
                />
                注册成功后自动导入号池
              </label>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={withProxy}
                  onCheckedChange={(v) => setWithProxy(Boolean(v))}
                />
                入池时携带代理
              </label>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={wantRefreshToken}
                  onCheckedChange={(v) => setWantRefreshToken(Boolean(v))}
                />
                获取 refresh_token（更慢，短效邮箱慎开）
              </label>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox checked={want2fa} onCheckedChange={(v) => setWant2fa(Boolean(v))} />
                绑定 2FA
              </label>
            </div>

            <div className="flex flex-wrap gap-2 border-t pt-3">
              <Button onClick={handleStart} disabled={busy || isRunning}>
                {busy ? <LoaderCircle className="size-4 animate-spin" /> : <Users className="size-4" />}
                开始
              </Button>
              <Button
                variant="secondary"
                onClick={() => callAction(pauseBatchRegister, "已请求暂停")}
                disabled={busy || state !== "running"}
              >
                <Pause className="size-4" />
                暂停
              </Button>
              <Button
                variant="secondary"
                onClick={() => callAction(resumeBatchRegister, "已恢复")}
                disabled={busy || state !== "paused"}
              >
                <Play className="size-4" />
                恢复
              </Button>
              <Button
                variant="destructive"
                onClick={() => callAction(stopBatchRegister, "已请求停止")}
                disabled={busy || state === "stopped"}
              >
                <Square className="size-4" />
                停止
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">进度</CardTitle>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex items-center gap-2">
              <Badge variant={isRunning ? "default" : "secondary"}>
                {stateLabel[state] ?? state}
              </Badge>
              <span className="text-xs text-muted-foreground">
                {status?.last_message || "—"}
              </span>
            </div>
            <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-sm">
              <dt className="text-muted-foreground">成功</dt>
              <dd className="text-right font-medium tabular-nums">
                {status?.registered_ok ?? 0}
              </dd>
              <dt className="text-muted-foreground">失败</dt>
              <dd className="text-right font-medium tabular-nums">
                {status?.registered_fail ?? 0}
              </dd>
              <dt className="text-muted-foreground">目标</dt>
              <dd className="text-right font-medium tabular-nums">
                {status?.target_count ? status.target_count : "不限"}
              </dd>
              <dt className="text-muted-foreground">剩余</dt>
              <dd className="text-right font-medium tabular-nums">
                {status?.remaining ?? "—"}
              </dd>
              <dt className="text-muted-foreground">已用时</dt>
              <dd className="text-right font-medium tabular-nums">
                {status ? `${Math.floor(status.elapsed / 60)}分${Math.floor(status.elapsed % 60)}秒` : "—"}
              </dd>
              <dt className="text-muted-foreground">邮箱池可用</dt>
              <dd className="text-right font-medium tabular-nums">
                {status?.pool_stats?.available ?? 0}
              </dd>
            </dl>
            {status?.workers?.length ? (
              <div className="space-y-1 border-t pt-2 text-xs text-muted-foreground">
                {status.workers.map((w) => (
                  <div key={w.id} className="truncate">
                    worker-{w.id}: {w.email || "准备中"}
                  </div>
                ))}
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>

      {/* 右：实时日志 */}
      <Card className="flex min-h-[480px] flex-col">
        <CardHeader className="flex-row items-center justify-between gap-2">
          <CardTitle className="text-base">
            实时日志
            <span className="ml-2 text-xs font-normal text-muted-foreground">
              {logs.length} 行
            </span>
          </CardTitle>
          <Button
            variant="secondary"
            size="sm"
            onClick={() => setLogs([])}
            disabled={logs.length === 0}
          >
            <Eraser className="size-4" />
            清空
          </Button>
        </CardHeader>
        <CardContent className="flex-1 overflow-hidden">
          <div className="h-[420px] overflow-y-auto rounded-md bg-stone-950 p-3 font-mono text-xs leading-relaxed text-stone-200">
            {logs.length === 0 ? (
              <div className="text-stone-500">暂无日志。点击「开始」启动批量注册。</div>
            ) : (
              logs.map((line) => (
                <div
                  key={line.id}
                  className={
                    line.level === "error"
                      ? "text-red-400"
                      : line.level === "ok"
                        ? "text-emerald-400"
                        : line.level === "warn"
                          ? "text-amber-400"
                          : "text-stone-300"
                  }
                >
                  <span className="text-stone-500">{line.time}</span> {line.message}
                </div>
              ))
            )}
            <div ref={logEndRef} />
          </div>
        </CardContent>
      </Card>
    </div>
  );
}
