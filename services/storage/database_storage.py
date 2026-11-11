from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Column, String, Text, create_engine, Integer, text
from sqlalchemy import update as sa_update
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker

from services.storage.base import StorageBackend

Base = declarative_base()


class AccountModel(Base):
    """账号数据模型"""
    __tablename__ = "accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    access_token = Column(String(2048), unique=True, nullable=False, index=True)
    data = Column(Text, nullable=False)  # JSON 格式存储完整账号数据


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

    def load_accounts(self) -> list[dict[str, Any]]:
        """从数据库加载账号数据"""
        session = self.Session()
        try:
            accounts = []
            for row in session.query(AccountModel).all():
                try:
                    account_data = json.loads(row.data)
                    if isinstance(account_data, dict):
                        accounts.append(account_data)
                except json.JSONDecodeError:
                    continue
            return accounts
        finally:
            session.close()

    def save_accounts(self, accounts: list[dict[str, Any]]) -> None:
        """保存账号数据到数据库"""
        self._save_rows(AccountModel, accounts, "access_token")

    def load_auth_keys(self) -> list[dict[str, Any]]:
        """从数据库加载鉴权密钥数据"""
        return self._load_rows(AuthKeyModel)

    def save_auth_keys(self, auth_keys: list[dict[str, Any]]) -> None:
        """保存鉴权密钥数据到数据库"""
        self._save_rows(AuthKeyModel, auth_keys, "id", "key_id")

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
                payload = [
                    {
                        key_col: key_value,
                        "data": json.dumps(item, ensure_ascii=False),
                    }
                    for key_value, item in incoming.items()
                ]
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
                                .values(data=row["data"])
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
        update_cols = {
            "data": stmt.excluded["data"],
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
