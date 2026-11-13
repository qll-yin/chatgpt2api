"""GIF 输出在**数据库任务存储**上的回归测试。

为什么单独一个文件
------------------
作者 v1.10.0 的 `test/test_gif_output.py` 是照着**内存 dict** 版任务服务写的，
完全不碰存储层。而本项目把图片任务搬进了数据库
（`services/storage/runtime_store.py` + `runtime_models.py`），
GIF 的三个字段（output_format / frame_count / frame_duration_ms）
是**移植**到 DB 版上的 —— 这条路径必须有测试守着，
否则「任务落库后 output_format 丢了 → GIF 结果被标成 png」这种问题
要等到真实生图才暴露。

覆盖：
- 模型有这三列，且老库能自动补列
- 落库 → 读回，GIF 字段不丢
- 提交任务时参数被归一化并写进库
- 非法组合（png + 帧参数）在提交阶段就报错
"""

from __future__ import annotations

import unittest

# ⚠️ 必须早于 services.* 的导入：把数据库指向临时库
from test.db_fixture import reset_database_state, use_temp_database

use_temp_database("chatgpt2api-test-gif-db-")

from services import image_task_service as svc  # noqa: E402
from services.storage import runtime_store as store  # noqa: E402
from services.storage.runtime_models import ImageTaskModel  # noqa: E402


OWNER = {"id": "gif-owner", "name": "Owner", "role": "admin"}


class GifTaskModelTests(unittest.TestCase):
    """模型层：三列存在 + 老库自动补列。"""

    def test_model_has_gif_columns(self):
        cols = {c.name for c in ImageTaskModel.__table__.columns}
        self.assertTrue({"output_format", "frame_count", "frame_duration_ms"} <= cols)

    def test_existing_table_gets_new_columns(self):
        """已存在的 image_tasks 表必须有这三列 —— create_all 不会给老表加列，
        靠 migrations.ensure_columns 补。缺列会直接报 no such column。"""
        from sqlalchemy import inspect

        from services.storage.db import get_engine
        from services.storage.migrations import ensure_columns, reset_check_cache

        reset_check_cache()
        engine = get_engine()
        added = ensure_columns(engine, "image_tasks", list(ImageTaskModel.__table__.columns))
        self.assertEqual(added, [])  # 已在上面的用例里建过，不该重复补

        cols = {c["name"] for c in inspect(engine).get_columns("image_tasks")}
        self.assertTrue({"output_format", "frame_count", "frame_duration_ms"} <= cols)


class GifTaskStoreTests(unittest.TestCase):
    """存储层：GIF 字段落库后能读回。"""

    def setUp(self):
        reset_database_state()

    def test_gif_fields_round_trip(self):
        store.upsert_image_task({
            "owner_id": "gif-owner",
            "task_id": "gif-task-1",
            "status": "queued",
            "mode": "generate",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "auto",
            "output_format": "gif",
            "frame_count": 9,
            "frame_duration_ms": 200,
        })
        row = store.get_image_task("gif-owner", "gif-task-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["output_format"], "gif")
        self.assertEqual(row["frame_count"], 9)
        self.assertEqual(row["frame_duration_ms"], 200)

    def test_default_output_format_is_png(self):
        """老任务没这三列 → 读出来必须是 png，不能是 None
        （None 会让 format_image_result 的 output_format 判断炸掉）。"""
        store.upsert_image_task({
            "owner_id": "gif-owner",
            "task_id": "gif-task-2",
            "status": "queued",
            "mode": "generate",
            "model": "gpt-image-2",
        })
        row = store.get_image_task("gif-owner", "gif-task-2")
        self.assertEqual(row["output_format"], "png")
        self.assertIsNone(row["frame_count"])
        self.assertIsNone(row["frame_duration_ms"])

    def test_public_task_exposes_gif_fields(self):
        task = {
            "id": "gif-task-3",
            "status": "queued",
            "mode": "generate",
            "model": "gpt-image-2",
            "size": "1024x1024",
            "quality": "auto",
            "output_format": "gif",
            "frame_count": 12,
            "frame_duration_ms": 100,
        }
        public = svc._public_task(task)
        self.assertEqual(public["output_format"], "gif")
        self.assertEqual(public["frame_count"], 12)
        self.assertEqual(public["frame_duration_ms"], 100)


class GifSubmitTests(unittest.TestCase):
    """提交层：参数归一化 + 落库。"""

    def setUp(self):
        reset_database_state()

    def _service(self) -> svc.ImageTaskService:
        import tempfile
        from pathlib import Path

        return svc.ImageTaskService(
            Path(tempfile.mkdtemp()) / "tasks",
            generation_handler=lambda _payload: {"data": [{"url": "http://example.test/a.gif"}]},
            edit_handler=lambda _payload: {"data": [{"url": "http://example.test/a.png"}]},
            retention_days_getter=lambda: 30,
        )

    def test_submit_generation_persists_gif_params(self):
        service = self._service()
        result = service.submit_generation(
            OWNER,
            client_task_id="gif-submit-1",
            prompt="a bouncing ball",
            model="gpt-image-2",
            size="1024x1024",
            output_format="gif",
            frame_count=6,
            frame_duration_ms=150,
        )
        self.assertEqual(result["output_format"], "gif")
        row = store.get_image_task("gif-owner", "gif-submit-1")
        self.assertIsNotNone(row)
        self.assertEqual(row["output_format"], "gif")
        self.assertEqual(row["frame_count"], 6)
        self.assertEqual(row["frame_duration_ms"], 150)

    def test_png_request_keeps_defaults(self):
        service = self._service()
        result = service.submit_generation(
            OWNER,
            client_task_id="gif-submit-2",
            prompt="a cat",
            model="gpt-image-2",
            size="1024x1024",
        )
        self.assertEqual(result["output_format"], "png")
        row = store.get_image_task("gif-owner", "gif-submit-2")
        self.assertEqual(row["output_format"], "png")
        self.assertIsNone(row["frame_count"])

    def test_invalid_combination_rejected_at_submit(self):
        """png 不允许带帧参数 —— 非法组合必须在提交阶段就报错，
        不能等生图跑完才失败。"""
        service = self._service()
        with self.assertRaises(ValueError):
            service.submit_generation(
                OWNER,
                client_task_id="gif-submit-3",
                prompt="a cat",
                model="gpt-image-2",
                size="1024x1024",
                output_format="png",
                frame_count=8,
            )


if __name__ == "__main__":
    unittest.main()
