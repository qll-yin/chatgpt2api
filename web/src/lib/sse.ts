/**
 * SSE 客户端（带鉴权）。
 *
 * 为什么不用 EventSource
 * ----------------------
 * EventSource **不能自定义请求头** —— 没法带 `Authorization: Bearer ...`，
 * 而后端的 SSE 端点要 `require_admin`。结果就是：路径写对了也会 401。
 * 把 key 塞进 query 参数能绕过，但 key 会进 URL、进日志、进浏览器历史。
 *
 * 所以用 fetch + ReadableStream 自己读流：能带 header，key 不外泄。
 *
 * 另一个坑：EventSource 用的是**相对路径**就会打到前端自己的端口
 * （开发时前端 3000 / 后端 8000）→ 404，日志一个字都收不到。
 * 这里统一用 webConfig.apiUrl 拼绝对地址。
 */

import webConfig from "@/constants/common-env";
import { getStoredAuthKey } from "@/store/auth";

export type SSEEvent = Record<string, unknown>;

/**
 * 订阅一个 SSE 端点。
 *
 * @param path     端点路径（如 "/api/register/batch/stream"）
 * @param onEvent  每收到一条事件回调
 * @param onError  连接出错回调
 * @returns 取消订阅的函数
 */
export function subscribeSSE(
  path: string,
  onEvent: (event: SSEEvent) => void,
  onError?: (error: unknown) => void,
): () => void {
  const controller = new AbortController();
  let closed = false;

  const run = async () => {
    try {
      const authKey = await getStoredAuthKey();
      const resp = await fetch(`${webConfig.apiUrl}${path}`, {
        headers: {
          Accept: "text/event-stream",
          ...(authKey ? { Authorization: `Bearer ${authKey}` } : {}),
        },
        signal: controller.signal,
        cache: "no-store",
      });
      if (!resp.ok || !resp.body) {
        throw new Error(`SSE 连接失败：HTTP ${resp.status}`);
      }

      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";

      // eslint-disable-next-line no-constant-condition
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // SSE 以空行分隔事件块
        const chunks = buffer.split("\n\n");
        buffer = chunks.pop() ?? "";
        for (const chunk of chunks) {
          for (const line of chunk.split("\n")) {
            if (!line.startsWith("data:")) continue;
            const payload = line.slice(5).trim();
            if (!payload) continue;
            try {
              onEvent(JSON.parse(payload) as SSEEvent);
            } catch {
              // 不是 JSON 的行忽略（心跳等纯文本场景）
            }
          }
        }
      }
    } catch (error) {
      if (closed) return;
      onError?.(error);
    }
  };

  void run();
  return () => {
    closed = true;
    controller.abort();
  };
}
