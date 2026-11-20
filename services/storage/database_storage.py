from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import Column, Index, String, Text, create_engine, Integer, text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from services.storage.base import StorageBackend

logger = logging.getLogger(__name__)

Base = declarative_base()

# 拆成真列的热字段：查询/筛选/排序高频用到。
# 其余冷字段（limits_progress、各种 refresh 错误时间等）留在 extra 里，
# 完整快照仍然写一份到 data（回滚/降级时用）。
_ACCOUNT_COLUMN_FIELDS = (
    "email",
    "type",
    "status",
    "quota",
    "success",
    "fail",
    "source_type",
    "proxy",
    "user_id",
    "created_at",
    "last_used_at",
    "restore_at",
    "invalid_count",
    "default_model_slug",
    "export_type",
)


class AccountModel(Base):
    """账号数据模型。

    ⚠️ 历史结构只有 `data` 一列（整个账号 JSON）。这样有两个问题：
      1. 想在数据库里按 status / quota / email 查完全做不到；
      2. 每改一个号的额度，都要把整行 JSON 重新序列化写回去。

    现在把热字段拆成真列并建索引；`extra` 存剩余的冷字段；
    `data` 保留完整 JSON 快照（可空）—— 老数据没回填时靠它兜底，
    需要回滚到旧版本时也能直接读。
    """

    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(2048), unique=True, nullable=False, index=True)

    # ── 热字段（真列）──
    email = Column(String(255), nullable=True, index=True)
    type = Column(String(32), nullable=True)
    status = Column(String(16), nullable=True, index=True)
    quota = Column(Integer, nullable=True)
    success = Column(Integer, nullable=True)
    fail = Column(Integer, nullable=True)
    source_type = Column(String(16), nullable=True, index=True)
    proxy = Column(String(512), nullable=True)
    user_id = Column(String(128), nullable=True)
    created_at = Column(String(32), nullable=True)
    last_used_at = Column(String(32), nullable=True)
    restore_at = Column(String(32), nullable=True)
    invalid_count = Column(Integer, nullable=True)
    default_model_slug = Column(String(128), nullable=True)
    export_type = Column(String(16), nullable=True)

    # 冷字段（JSON）：limits_progress、last_refresh_error*、last_token_refresh* 等
    extra = Column(Text, nullable=True)
    # 完整快照（JSON）：老数据兜底 + 回滚用
    data = Column(Text, nullable=True)

    __table_args__ = (
        # 「还能用的号」是最常见的筛选：状态 + 剩余额度
        Index("ix_accounts_status_quota", "status", "quota"),
    )


class AuthKeyModel(Base):
    """鉴权密钥数据模型"""
    __tablename__ = "auth_keys"

    id = Column(Integer, primary_key=True, autoincrement=True)
    key_id = Column(String(255), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)


class DatabaseStorageBackend(StorageBackend):
    """数据库存储后端（支持 SQLite、PostgreSQL、MySQL 等）"""

    def __init__(self, database_url: str):
        self.database_url = database_url
        self.engine = create_engine(
            database_url,
            pool_pre_ping=True,  # 自动检测连接是否有效
            pool_recycle=3600,   # 1小时回收连接
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        # 老库里 accounts 只有 data 一列 —— create_all 不会改已存在的表，
        # 这里补列 + 补索引（幂等，纯 DDL，不搬数据）。
        # 数据回填走 `scripts/migrate_accounts_columns.py`；
        # 没回填也不会错，load_accounts 会从 data 快照兜底。
        self._ensure_account_columns()

    def _ensure_account_columns(self) -> list[str]:
        from services.storage.migrations import ensure_columns

        try:
            added = ensure_columns(self.engine, "accounts", list(AccountModel.__table__.columns))
        except Exception as exc:  # noqa: BLE001
            logger.warning("[storage] 账号表补列失败: %s", exc)
            return []
        self._ensure_account_indexes()
        if added:
            logger.info("[storage] 账号表新增列: %s", ", ".join(added))
        return added

    def _ensure_account_indexes(self) -> None:
        """给已存在的表补索引（create_all 只对新表生效）。"""
        from sqlalchemy import inspect

        try:
            existing = {item["name"] for item in inspect(self.engine).get_indexes("accounts")}
        except Exception:  # noqa: BLE001
            existing = set()
        for index in AccountModel.__table__.indexes:
            if index.name in existing:
                continue
            try:
                index.create(bind=self.engine, checkfirst=True)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[storage] 创建账号索引 %s 失败: %s", index.name, exc)

    @staticmethod
    def _account_row(item: dict[str, Any]) -> dict[str, Any]:
        """账号 dict → 一行真列 + extra(冷字段 JSON) + data(完整快照)。"""

        def _str(key: str) -> str | None:
            value = item.get(key)
            if value is None:
                return None
            text_value = str(value)
            return text_value if text_value else None

        def _int(key: str) -> int | None:
            try:
                value = item.get(key)
                return None if value is None else int(value)
            except (TypeError, ValueError):
                return None

        extra = {
            key: value
            for key, value in item.items()
            if key not in _ACCOUNT_COLUMN_FIELDS
            and key not in {"access_token", "accessToken", "extra", "data", "id"}
        }
        return {
            "access_token": str(item.get("access_token") or "").strip(),
            "email": _str("email"),
            "type": _str("type"),
            "status": _str("status"),
            "quota": _int("quota"),
            "success": _int("success"),
            "fail": _int("fail"),
            "source_type": _str("source_type"),
            "proxy": _str("proxy"),
            "user_id": _str("user_id"),
            # ⚠️ 这里用 "" 而不是 None：created_at 同时充当「这行拆过列没有」的标记。
            #    老 JSON 里没有 created_at 时如果留 NULL，回填完仍然会被当成老行，
            #    下次还会重复回填（幂等性就没了）。
            "created_at": _str("created_at") or "",
            "last_used_at": _str("last_used_at"),
            "restore_at": _str("restore_at"),
            "invalid_count": _int("invalid_count"),
            "default_model_slug": _str("default_model_slug"),
            "export_type": _str("export_type"),
            "extra": json.dumps(extra, ensure_ascii=False) if extra else None,
            "data": json.dumps(item, ensure_ascii=False),
        }

    @staticmethod
    def _row_to_account(row) -> dict[str, Any] | None:
        """一行 → 账号 dict。

        ⚠️ 判断「这行有没有回填过」用 `created_at`：
          · 新结构一定会写 created_at（账号归一化时必填）
          · 老行的 created_at 是后加的列，默认 NULL
        老行直接读完整快照 `data`，保证不丢字段。
        """
        if getattr(row, "created_at", None) is None and getattr(row, "data", None):
            try:
                legacy = json.loads(row.data)
            except (TypeError, ValueError):
                legacy = None
            if isinstance(legacy, dict) and legacy.get("access_token"):
                return legacy

        item: dict[str, Any] = {}
        if getattr(row, "extra", None):
            try:
                loaded = json.loads(row.extra)
                if isinstance(loaded, dict):
                    item.update(loaded)
            except (TypeError, ValueError):
                pass
        item["access_token"] = str(getattr(row, "access_token", "") or "")
        for field in _ACCOUNT_COLUMN_FIELDS:
            item[field] = getattr(row, field, None)
        return item

    def load_accounts(self) -> list[dict[str, Any]]:
        """从数据库加载账号数据（真列重建；老行回退到 data 快照）。"""
        session = self.Session()
        try:
            accounts = []
            for row in session.query(AccountModel).all():
                item = self._row_to_account(row)
                if item:
                    accounts.append(item)
            return accounts
        finally:
            session.close()

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到数据库（真列 + extra + 完整快照）。"""
        self._save_rows(
            AccountModel,
            accounts,
            "access_token",
            row_builder=self._account_row,
        )

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从数据库加载鉴权密钥数据"""
        return self._load_rows(AuthKeyModel)

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到数据库"""
        self._save_rows(AuthKeyModel, auth_keys, "id", "key_id")

    def save_account(self, item: dict[str, Any]) -> None:
        """只写一个账号（单行 upsert，不做差集删除）。

        为什么需要：`save_accounts()` 是「整表对齐」语义 —— 每次生图成功扣一次额度
        都要把几千行重新 UPSERT 一遍，写放大 O(N)。有了这个方法，
        单账号的额度/状态变化只碰它自己那一行。
        JSON 后端没有这个能力（整文件重写），所以不实现，由上层回退。
        """
        row = self._account_row(item)
        if not row.get("access_token"):
            return
        session = self.Session()
        try:
            if self._supports_upsert():
                self._upsert_many(session, AccountModel, [row], "access_token")
            else:
                existing = (
                    session.query(AccountModel)
                    .filter(AccountModel.access_token == row["access_token"])
                    .one_or_none()
                )
                if existing is None:
                    session.add(AccountModel(**row))
                else:
                    for key, value in row.items():
                        if key != "access_token":
                            setattr(existing, key, value)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def backfill_account_columns(self, batch_size: int = 500, dry_run: bool = False) -> dict[str, Any]:
        """把老行的 `data` JSON 拆进真列（幂等）。

        判断依据：`created_at IS NULL` 说明这行还是「只有 data」的老结构
        （账号归一化时一定会写 created_at，所以新行不会是 NULL）。

        分批按 id 推进，避免一次性把几十万行读进内存；失败的行跳过并计数，
        不会因为一行脏数据卡住整个迁移。
        """
        batch_size = max(1, int(batch_size))
        scanned = updated = skipped = 0
        session = self.Session()
        try:
            last_id = 0
            while True:
                rows = (
                    session.query(AccountModel)
                    .filter(
                        AccountModel.id > last_id,
                        AccountModel.created_at.is_(None),
                        AccountModel.data.isnot(None),
                    )
                    .order_by(AccountModel.id.asc())
                    .limit(batch_size)
                    .all()
                )
                if not rows:
                    break
                for row in rows:
                    last_id = int(row.id or 0)
                    scanned += 1
                    try:
                        item = json.loads(row.data)
                    except (TypeError, ValueError):
                        skipped += 1
                        continue
                    if not isinstance(item, dict) or not item.get("access_token"):
                        skipped += 1
                        continue
                    values = self._account_row(item)
                    # data 本来就是完整的，不回写（省一次大字段写）
                    values.pop("data", None)
                    if dry_run:
                        updated += 1
                        continue
                    session.execute(
                        sa_update(AccountModel)
                        .where(AccountModel.id == row.id)
                        .values(**values)
                    )
                    updated += 1
                if not dry_run:
                    session.commit()
            if dry_run:
                session.rollback()
            return {
                "scanned": scanned,
                "updated": updated,
                "skipped": skipped,
                "dry_run": bool(dry_run),
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def _load_rows(self, model: type[AccountModel] | type[AuthKeyModel]) -> list[dict[str, Any]]:
        session = self.Session()
        try:
            items = []
            for row in session.query(model).all():
                try:
                    item_data = json.loads(row.data)
                    if isinstance(item_data, dict):
                        items.append(item_data)
                except json.JSONDecodeError:
                    continue
            return items
        finally:
            session.close()

    def _save_rows(
        self,
        model: type[AccountModel] | type[AuthKeyModel],
        items: list[dict[str, Any]],
        source_key: str,
        target_key: str | None = None,
        row_builder=None,
    ) -> None:
        """整表覆盖写入。

        历史实现是「DELETE 全表 → 逐条 INSERT」：号池几千个账号时，
        每改一个号的 quota 就要删掉全部再全部插回去 —— 写放大 O(N)，
        且期间表是空的（并发读会看到空号池）。

        现在改为 **UPSERT（upsert）+ 差集删除**：
        - 已存在的行 → ON CONFLICT DO UPDATE，只更新该行
        - 新增的行 → INSERT
        - 不在本次集合里的旧行 → 单独 DELETE
        写入量与实际改动量成正比，不再随号池规模线性放大。

        row_builder：把一条业务数据转成「整行各列」的函数。
        accounts 表拆成真列后必须用它；auth_keys 仍走 data 单列。
        """
        session = self.Session()
        try:
            key_col = target_key or source_key
            incoming: dict[str, dict[str, Any]] = {}
            for item in items:
                if not isinstance(item, dict):
                    continue
                key_value = str(item.get(source_key) or "").strip()
                if not key_value:
                    continue
                incoming[key_value] = item

            if incoming:
                payload = []
                for key_value, item in incoming.items():
                    row = (
                        row_builder(item)
                        if row_builder is not None
                        else {key_col: key_value, "data": json.dumps(item, ensure_ascii=False)}
                    )
                    row[key_col] = key_value
                    payload.append(row)
                if self._supports_upsert():
                    self._upsert_many(session, model, payload, key_col)
                else:
                    # 不支持 UPSERT 的方言：退化为「先插已存在的，再补新的」
                    existing = {
                        str(getattr(row, key_col))
                        for row in session.query(model).all()
                    }
                    for row in payload:
                        if row[key_col] in existing:
                            session.execute(
                                sa_update(model)
                                .where(getattr(model, key_col) == row[key_col])
                                .values(**{k: v for k, v in row.items() if k != key_col})
                            )
                        else:
                            session.add(model(**row))

            # 删除本次集合之外的旧行（数量差集）
            if incoming:
                keep = list(incoming.keys())
                session.query(model).filter(~getattr(model, key_col).in_(keep)).delete(
                    synchronize_session=False
                )
            else:
                session.query(model).delete()

            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def _supports_upsert(self) -> bool:
        """当前方言是否支持 ON CONFLICT / ON DUPLICATE KEY。"""
        return self.engine.dialect.name in {"postgresql", "sqlite", "mysql", "mariadb"}

    def _upsert_many(
        self,
        session,
        model: type[AccountModel] | type[AuthKeyModel],
        payload: list[dict[str, Any]],
        key_col: str,
    ) -> None:
        """批量 UPSERT（一条语句搞定整批，避免 N 次往返）。"""
        dialect = self.engine.dialect.name
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as dialect_insert
        elif dialect == "sqlite":
            from sqlalchemy.dialects.sqlite import insert as dialect_insert
        elif dialect in {"mysql", "mariadb"}:
            from sqlalchemy.dialects.mysql import insert as dialect_insert
        else:
            for row in payload:
                session.add(model(**row))
            return

        stmt = dialect_insert(model).values(payload)
        # 冲突时更新「除主键外的所有列」——
        # accounts 拆成真列后不能再只更新 data（否则列永远是第一次写入的值）。
        update_cols = {
            column.name: stmt.excluded[column.name]
            for column in model.__table__.columns
            if column.name not in {key_col, "id"}
        }
        if dialect in {"mysql", "mariadb"}:
            stmt = stmt.on_duplicate_key_update(**update_cols)
        else:
            stmt = stmt.on_conflict_do_update(index_elements=[key_col], set_=update_cols)
        session.execute(stmt)

    def health_check(self) -> dict[str, Any]:
        """健康检查"""
        try:
            session = self.Session()
            try:
                # 尝试执行简单查询
                session.execute(text("SELECT 1"))
                count = session.query(AccountModel).count()
                auth_key_count = session.query(AuthKeyModel).count()
                return {
                    "status": "healthy",
                    "backend": "database",
                    "database_url": self._mask_password(self.database_url),
                    "account_count": count,
                    "auth_key_count": auth_key_count,
                }
            finally:
                session.close()
        except Exception as e:
            return {
                "status": "unhealthy",
                "backend": "database",
                "error": str(e),
            }

    def get_backend_info(self) -> dict[str, Any]:
        """获取存储后端信息"""
        db_type = "unknown"
        if "sqlite" in self.database_url:
            db_type = "sqlite"
        elif "postgresql" in self.database_url or "postgres" in self.database_url:
            db_type = "postgresql"
        elif "mysql" in self.database_url:
            db_type = "mysql"
        
        return {
            "type": "database",
            "db_type": db_type,
            "description": f"数据库存储 ({db_type})",
            "database_url": self._mask_password(self.database_url),
        }

    @staticmethod
    def _mask_password(url: str) -> str:
        """隐藏数据库连接字符串中的密码"""
        if "://" not in url:
            return url
        try:
            protocol, rest = url.split("://", 1)
            if "@" in rest:
                credentials, host = rest.split("@", 1)
                if ":" in credentials:
                    username, _ = credentials.split(":", 1)
                    return f"{protocol}://{username}:****@{host}"
            return url
        except Exception:
            return url
