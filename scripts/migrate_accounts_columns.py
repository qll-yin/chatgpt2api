#!/usr/bin/env python3
"""把 accounts 表的 data(JSON) 拆进真列 + 补索引。

背景
----
老结构 `accounts` 只有 `access_token` 和 `data`（整个账号 JSON）。
想按 status / quota / email 查询做不到，而且改一个号的额度要重写整行 JSON。

新结构把热字段拆成真列（email / type / status / quota / success / fail /
source_type / proxy / user_id / created_at / last_used_at / restore_at /
invalid_count / default_model_slug / export_type），冷字段放 `extra`，
完整快照仍写一份到 `data`（回滚/降级用）。

补列和建索引在应用启动时会自动做（纯 DDL，很快）；**数据回填**用这个脚本，
因为几十万行需要时间，不适合卡在启动流程里。

用法
----
    # 走环境变量 DATABASE_URL（compose 里已经有）
    docker compose exec app python scripts/migrate_accounts_columns.py

    # 显式指定连接串
    python scripts/migrate_accounts_columns.py --database-url postgresql://user:pass@host:5432/db

    # 先看看会动多少行，不写库
    python scripts/migrate_accounts_columns.py --dry-run

    # 每批 1000 行（默认 500）
    python scripts/migrate_accounts_columns.py --batch-size 1000

幂等：只处理 created_at 为 NULL 的老行，重复执行不会重复写。
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main() -> int:
    parser = argparse.ArgumentParser(description="accounts 表 JSON 拆列迁移")
    parser.add_argument("--database-url", default="", help="数据库连接串（默认读 DATABASE_URL）")
    parser.add_argument("--batch-size", type=int, default=500, help="每批处理行数（默认 500）")
    parser.add_argument("--dry-run", action="store_true", help="只统计，不写库")
    args = parser.parse_args()

    url = (args.database_url or os.getenv("DATABASE_URL") or "").strip()
    if not url:
        print("[migrate] 缺少数据库连接串：请设置 DATABASE_URL 或传 --database-url", file=sys.stderr)
        return 2

    from services.storage.database_storage import DatabaseStorageBackend

    masked = url
    if "://" in url and "@" in url:
        protocol, rest = url.split("://", 1)
        credentials, host = rest.split("@", 1)
        user = credentials.split(":", 1)[0]
        masked = f"{protocol}://{user}:****@{host}"

    print(f"[migrate] 目标数据库: {masked}")
    started = time.time()
    # 构造后端时已经做了：create_all + 补列 + 补索引（幂等）
    backend = DatabaseStorageBackend(url)
    print("[migrate] 表结构已对齐（补列 + 补索引）")

    result = backend.backfill_account_columns(batch_size=args.batch_size, dry_run=args.dry_run)
    elapsed = time.time() - started
    print(
        f"[migrate] 扫描 {result['scanned']} 行，"
        f"{'待更新' if args.dry_run else '已更新'} {result['updated']} 行，"
        f"跳过 {result['skipped']} 行，耗时 {elapsed:.2f}s"
    )
    if args.dry_run:
        print("[migrate] --dry-run：没有写库。去掉该参数再跑一次即可真正迁移。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
