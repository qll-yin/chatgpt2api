"use client";

import { useState } from "react";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAuthGuard } from "@/lib/use-auth-guard";

import { AutoRegisterPanel } from "./components/auto-register-panel";
import { BatchRegisterPanel } from "./components/batch-register-panel";
import { MailConfigPanel } from "./components/mail-config-panel";
import { MailOrdersPanel } from "./components/mail-orders-panel";
import { MailPoolPanel } from "./components/mail-pool-panel";
import { ProxyPoolPanel } from "./components/proxy-pool-panel";
import { RegisteredPanel } from "./components/registered-panel";
import { RunsPanel } from "./components/runs-panel";
import { SmsConfigPanel } from "./components/sms-config-panel";

/**
 * 注册管理。
 *
 * 九个标签页：
 *   批量注册 —— 起批量任务、实时日志、暂停/恢复/停止
 *   注册结果 —— 已注册的账号，勾选后批量导入号池 / 标注异常 / 看详情
 *   运行记录 —— 按批次看每一轮的邮箱数与完整日志
 *   代理池   —— 代理的启用/上限/用量
 *   邮箱配置 —— 邮箱来源与各 provider 的配置项
 *   邮箱池   —— 导入邮箱列表（outlook / icloud 中转）
 *   邮箱订单 —— Remail 订单（下手、取件、复用状态）
 *   接码配置 —— SMS 接码（取 refresh_token 时命中 add-phone 要用）
 *   自动补充 —— 号池低于阈值时自动注册
 *
 * 布局说明：宽度交给根布局的 max-w 容器统一控制（这里不再额外限宽，
 * 否则表格类页面在宽屏上还是一窄条）。
 */
export default function RegisterPage() {
  const { isCheckingAuth, session } = useAuthGuard(["admin"]);
  const [tab, setTab] = useState("batch");

  if (isCheckingAuth) {
    return <div className="p-8 text-sm text-muted-foreground">加载中…</div>;
  }
  if (!session || session.role !== "admin") {
    return <div className="p-8 text-sm text-muted-foreground">仅管理员可访问</div>;
  }

  return (
    <div className="w-full space-y-5 px-4 py-6 sm:px-6 lg:px-8">
      <header className="flex flex-col gap-1.5">
        <h1 className="text-2xl font-semibold tracking-tight">注册管理</h1>
        <p className="text-sm text-muted-foreground">
          批量注册账号、查看注册结果，并直接导入到「号池管理」
        </p>
      </header>

      <Tabs value={tab} onValueChange={setTab} className="space-y-5">
        {/* 标签栏粘顶 —— 表格类页面很长，翻到下面还能随时切标签 */}
        <div className="sticky top-2 z-30 -mx-1 overflow-x-auto px-1">
          <div className="min-w-max rounded-2xl border border-white/80 bg-white/85 px-2 py-1.5 shadow-sm backdrop-blur-md dark:border-white/10 dark:bg-stone-900/85">
            <TabsList variant="line" className="justify-start gap-1">
              <TabsTrigger value="batch">批量注册</TabsTrigger>
              <TabsTrigger value="registered">注册结果</TabsTrigger>
              <TabsTrigger value="runs">运行记录</TabsTrigger>
              <TabsTrigger value="proxies">代理池</TabsTrigger>
              <TabsTrigger value="mail">邮箱配置</TabsTrigger>
              <TabsTrigger value="pool">邮箱池</TabsTrigger>
              <TabsTrigger value="orders">邮箱订单</TabsTrigger>
              <TabsTrigger value="sms">接码配置</TabsTrigger>
              <TabsTrigger value="auto">自动补充</TabsTrigger>
            </TabsList>
          </div>
        </div>

        <TabsContent value="batch" className="pt-0">
          <BatchRegisterPanel />
        </TabsContent>
        <TabsContent value="registered" className="pt-0">
          <RegisteredPanel />
        </TabsContent>
        <TabsContent value="runs" className="pt-0">
          <RunsPanel />
        </TabsContent>
        <TabsContent value="proxies" className="pt-0">
          <ProxyPoolPanel />
        </TabsContent>
        <TabsContent value="mail" className="pt-0">
          <MailConfigPanel />
        </TabsContent>
        <TabsContent value="pool" className="pt-0">
          <MailPoolPanel />
        </TabsContent>
        <TabsContent value="orders" className="pt-0">
          <MailOrdersPanel />
        </TabsContent>
        <TabsContent value="sms" className="pt-0">
          <SmsConfigPanel />
        </TabsContent>
        <TabsContent value="auto" className="pt-0">
          <AutoRegisterPanel />
        </TabsContent>
      </Tabs>
    </div>
  );
}
