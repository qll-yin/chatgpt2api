"use client";

import { ChevronLeft, ChevronRight } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * 列表分页条。
 *
 * 注册结果 / 运行记录都用它 —— 两个列表在后端都是 limit/offset 分页，
 * 前端统一在这里翻页。
 */
export function Pagination({
  page,
  pageSize,
  total,
  onPageChange,
}: {
  /** 当前页（从 1 开始） */
  page: number;
  pageSize: number;
  /** 总条数 */
  total: number;
  onPageChange: (page: number) => void;
}) {
  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
  const from = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const to = Math.min(page * pageSize, total);

  return (
    <div className="flex items-center justify-between gap-2 pt-3 text-xs text-muted-foreground">
      <span>
        {total === 0 ? "暂无数据" : `第 ${from}-${to} 条 / 共 ${total} 条`}
      </span>
      <div className="flex items-center gap-2">
        <span>
          第 {page} / {totalPages} 页
        </span>
        <Button
          variant="outline"
          size="sm"
          className="h-7 px-2"
          disabled={page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          <ChevronLeft className="size-4" />
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          className="h-7 px-2"
          disabled={page >= totalPages}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
          <ChevronRight className="size-4" />
        </Button>
      </div>
    </div>
  );
}
