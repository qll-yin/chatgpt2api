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

import { subscribeSSE } from "@/lib/sse";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
  // 等资源（号池空 / 代理用尽）—— 此时没有任何注册在跑，
  // 跟「运行中」区分开，避免用户以为还在注册。
  waiting: "等待资源",
};

export function BatchRegisterPanel() {
  const [status, setStatus] = useState<BatchStatus | null>(null);
  const [providers, setProviders] = useState<MailProvider[]>([]);
  const [mailSource, setMailSource] = useState("");
  const [concurrency, setConcurrency] = useState(1);
  const [targetCount, setTargetCount] = useState(0);
  const [coolDown, setCoolDown] = useState(3);
  const [maxRetries, setMaxRetries] = useState(3);
  const [otpTimeout, setOtpTimeout] = useState(180);
  const [wantRefreshToken, setWantRefreshToken] = useState(false);
  const [want2fa, setWant2fa] = useState(false);
  const [autoImport, setAutoImport] = useState(true);
  const [withProxy, setWithProxy] = useState(true);
  const [busy, setBusy] = useState(false);
  const [logs, setLogs] = useState<LogLine[]>([]);
  const logIdRef = useRef(0);
  const logEndRef = useRef<HTMLDivElement | null>(null);

  const selectedProvider = providers.find((p) => p.kind === mailSource) ?? null;

  const pushLog = useCallback((kind: string, message: string, level?: string) => {
    // ⚠️ id 必须在**调用时**取，不能在 setState 的更新函数里读 logIdRef.current。
    //    React 18 会把同一批里的多次 setState 合并，等真正执行更新函数时，
    //    logIdRef.current 已经是最后的值 —— 这一批的每一行都会拿到**同一个 id**，
    //    于是控制台刷「Encountered two children with the same key, `5`」。
    //    （实测 SSE 一批推十几行日志，必现。）
    const id = ++logIdRef.current;
    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, "0")}:${String(
      now.getMinutes(),
    ).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
    setLogs((prev) =>
      [...prev, { id, kind, message, level, time }].slice(-500),
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
        const { providers: list, current } = await fetchMailProviders();
        if (!active) return;
        // 就绪的（配置齐了 + 池化来源还有可用邮箱）排前面 ——
        // 用户配好了 remail 就该默认用 remail，而不是列表里第一个。
        const sorted = [...list].sort((a, b) => Number(b.ready) - Number(a.ready));
        setProviders(sorted);
        const preferred =
          sorted.find((p) => p.kind === current && p.ready) ??
          sorted.find((p) => p.ready) ??
          sorted[0];
        if (preferred) setMailSource(preferred.kind);
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
    // ⚠️ 用自带的 subscribeSSE 而不是 EventSource：
    //    EventSource 不能自定义请求头 → 带不上 Authorization → 401；
    //    而且用相对路径会打到前端自己的端口 → 404。
    const unsubscribe = subscribeSSE(
      "/api/register/batch/stream",
      (payload) => {
        const kind = String(payload.kind ?? "");
        const data = (payload.data ?? {}) as Record<string, unknown>;
        if (kind === "state") {
          if (data.state) {
            setStatus((prev) => (prev ? { ...prev, ...(data as Partial<BatchStatus>) } : prev));
          }
          return;
        }
        if (kind === "run_started") {
          pushLog("run", `开始注册 ${String(data.email ?? "")}`);
          return;
        }
        if (kind === "run_finished") {
          const ok = Boolean(data.ok);
          pushLog(
            "run",
            ok
              ? `注册成功 ${String(data.email ?? "")}`
              : `注册失败 ${String(data.email ?? "")}${data.category ? `（${String(data.category)}）` : ""}`,
            ok ? "ok" : "error",
          );
          return;
        }
        if (kind === "imported") {
          pushLog("run", `已导入号池：${String(data.email ?? "")}`, "ok");
          return;
        }
        if (kind === "quota_refreshed") {
          pushLog("run", `已刷新 ${String(data.refreshed ?? 0)} 个号的额度`, "ok");
          return;
        }
        if (kind === "proxy_disabled") {
          pushLog(
            "run",
            `代理已自动停用（${String(data.reason ?? "连续失效")}）：${String(data.proxy ?? "")}`,
            "error",
          );
          return;
        }
        if (kind === "circuit_break") {
          pushLog("run", `批次已暂停：${String(data.reason ?? "")}`, "error");
          return;
        }
        if (kind === "run_event") {
          // registrar 内部的结构化事件（error / progress / warning…）
          const inner = String(data.kind ?? "");
          const msg = String(data.message ?? "");
          if (inner === "error") {
            pushLog("run", `注册出错：${msg}`, "error");
          } else if (msg) {
            pushLog("run", msg, inner === "warning" ? "warn" : undefined);
          }
          return;
        }
        if (kind === "ping" || kind === "ended") return;
        // 详细日志行：registrar 写的是 "16:13:02 [ERROR] services.register.auth_flow: ..."
        // 解析出级别，好让错误行能标红。
        const raw = String(data.message ?? payload.message ?? "");
        const levelMatch = raw.match(/\[(ERROR|WARNING|INFO|DEBUG|CRITICAL)\]/i);
        const level = levelMatch ? levelMatch[1].toLowerCase() : undefined;
        // 多 worker 并发时，每行都带上是哪个号 —— 不然分不清是谁的
        const who = data.email ? `[${String(data.email).slice(0, 18)}] ` : "";
        pushLog("log", who + raw.replace(/\d{2}:\d{2}:\d{2}\s+/, ""), level);
      },
      (error) => {
        pushLog("log", `日志连接断开：${(error as Error).message ?? error}`, "error");
      },
    );
    return unsubscribe;
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
        // ⚠️ 不能用 `|| 3` —— 那会把「0 = 不重试」覆盖掉。
        max_retries: Math.max(0, Math.min(20, Number(maxRetries) || 0)),
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
    <div className="grid items-start gap-5 lg:grid-cols-[minmax(340px,400px)_minmax(0,1fr)] xl:grid-cols-[minmax(400px,460px)_minmax(0,1fr)]">
      {/* 左：参数 + 控制 */}
      <div className="space-y-5">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">批量参数</CardTitle>
            <CardDescription className="text-xs">
              参数与旧注册机的 auto-loop 一致，邮箱来源以这里选的为准
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="space-y-1.5">
              <Label>邮箱来源</Label>
              {/* ⚠️ Radix 的 Select 不接受空串 value —— 会抛错并让整棵树崩掉 */}
              <Select
                value={mailSource || "__none__"}
                onValueChange={setMailSource}
              >
                <SelectTrigger>
                  <SelectValue placeholder="选择邮箱来源" />
                </SelectTrigger>
                <SelectContent>
                  {providers.map((p) => {
                    // 没配好的来源直接禁掉 —— 让用户去「邮箱配置」补齐，
                    // 而不是选完了点开始才发现一个号都领不到。
                    const reason = !p.configured
                      ? "缺少必填配置"
                      : p.pooled && !p.pool_available
                        ? "邮箱池里没有可用邮箱"
                        : "";
                    return (
                      <SelectItem key={p.kind} value={p.kind} disabled={!p.ready}>
                        {p.display_name}
                        {p.ready ? "" : `（${reason}）`}
                      </SelectItem>
                    );
                  })}
                </SelectContent>
              </Select>
              {selectedProvider && !selectedProvider.ready ? (
                <p className="text-xs text-amber-700 dark:text-amber-300">
                  {selectedProvider.pooled && !selectedProvider.pool_available
                    ? "这个来源的邮箱池是空的 —— 去「邮箱池」页导入邮箱列表后才能用。"
                    : "这个来源还没配齐 —— 去「邮箱配置」页填完必填项后才能用。"}
                </p>
              ) : null}
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

            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1.5">
                <Label htmlFor="reg-retries">失败重试次数</Label>
                <Input
                  id="reg-retries"
                  type="number"
                  min={0}
                  max={20}
                  value={maxRetries}
                  onChange={(e) => setMaxRetries(Number(e.target.value))}
                />
              </div>
              <div className="space-y-1.5" />
            </div>
            <p className="text-xs leading-5 text-muted-foreground">
              一个完整注册流程算 1 次（流程内部撞 409 重建会话那种重试不额外计数）。
              流程失败后同一个 worker 会换个条件再跑
              <span className="font-medium text-foreground">{maxRetries || 0}</span>{" "}
              遍，都不行才停掉这个 worker。填 0 表示不重试，失败一次就换号继续。
              <br />
              只对「出口 IP 被拦 / 网络抽风」这类失败重试；邮箱或账号本身的问题
              （如邮箱已注册）不消耗重试次数，直接换号。
            </p>

            <div className="space-y-2.5 border-t pt-4">
              <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                凭证与入池
              </div>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={autoImport}
                  onCheckedChange={(v) => setAutoImport(Boolean(v))}
                />
                注册成功后自动导入号池管理
              </label>
              <label className="flex items-center gap-2 text-sm">
                <Checkbox
                  checked={withProxy}
                  onCheckedChange={(v) => setWithProxy(Boolean(v))}
                />
                入池时携带注册所用代理
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
              <p className="text-xs text-muted-foreground">
                自动导入写入的是顶部导航「号池管理」里的账号池（chatgpt2api 账号池），
                不是本页的「注册结果」；批量结束后会自动刷一次额度。
              </p>
            </div>

            <div className="flex flex-wrap gap-2 border-t pt-4">
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
            <CardDescription className="text-xs">
              批次结束后「已用时」会冻结，不会一直累加
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={isRunning ? "default" : "secondary"}>
                {stateLabel[state] ?? state}
              </Badge>
              {state === "waiting" ? (
                <span className="text-xs text-amber-700 dark:text-amber-300">
                  没有可用邮箱/代理，暂未在注册 —— 补上资源后会自动继续
                </span>
              ) : null}
              <span className="text-xs text-muted-foreground">
                {status?.last_message || "—"}
              </span>
            </div>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-2 text-sm sm:grid-cols-3">
              <div className="rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/5">
                <dt className="text-xs text-muted-foreground">成功</dt>
                <dd className="text-lg font-semibold tabular-nums text-emerald-600 dark:text-emerald-400">
                  {status?.registered_ok ?? 0}
                </dd>
              </div>
              <div className="rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/5">
                <dt className="text-xs text-muted-foreground">失败</dt>
                <dd className="text-lg font-semibold tabular-nums text-red-600 dark:text-red-400">
                  {status?.registered_fail ?? 0}
                </dd>
              </div>
              <div className="rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/5">
                <dt className="text-xs text-muted-foreground">目标</dt>
                <dd className="text-lg font-semibold tabular-nums">
                  {status?.target_count ? status.target_count : "不限"}
                </dd>
              </div>
              <div className="rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/5">
                <dt className="text-xs text-muted-foreground">剩余</dt>
                <dd className="text-lg font-semibold tabular-nums">
                  {status?.remaining ?? "—"}
                </dd>
              </div>
              <div className="rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/5">
                <dt className="text-xs text-muted-foreground">已用时</dt>
                <dd className="text-lg font-semibold tabular-nums">
                  {status ? `${Math.floor(status.elapsed / 60)}分${Math.floor(status.elapsed % 60)}秒` : "—"}
                </dd>
              </div>
              <div className="rounded-xl bg-stone-50 px-3 py-2 dark:bg-white/5">
                <dt className="text-xs text-muted-foreground">邮箱池可用</dt>
                <dd className="text-lg font-semibold tabular-nums">
                  {status?.pool_stats?.available ?? 0}
                </dd>
              </div>
            </dl>
            {status?.workers?.length ? (
              <div className="space-y-1 border-t pt-3 text-xs text-muted-foreground">
                <div className="font-medium">正在跑的 worker</div>
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
      <Card className="flex min-h-[520px] flex-col overflow-hidden">
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
          <div className="h-[calc(100vh-300px)] min-h-[440px] overflow-y-auto rounded-2xl bg-stone-950 p-4 font-mono text-xs leading-relaxed text-stone-200">
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
