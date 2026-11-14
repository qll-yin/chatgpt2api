"""网页生图提示词构造。

背景
----
网页生图链路（/backend-api/conversation + picture_v2）没有 quality / size
结构化请求字段，唯一能影响上游的就是提示词。而上游对提示词里 quality 一词
的理解是概率性的 —— 同一个 medium 有时按 medium 渲染、有时按 low 渲染，
表现为「同样的请求配置，有的图清晰有的图糊」。

实测反推（1254×1254）：
    low 档    → 425 output tokens，字节/像素 0.96  （糊）
    medium 档 → 1650 output tokens，字节/像素 1.40 （清晰）

所以提示词必须：
    1. 把 quality 译成具体的视觉描述，而不是只丢一个英文枚举值；
    2. 显式排除低质量特征（模糊 / 像素感 / 涂抹感）；
    3. 补上尺寸与构图要求（上游只靠提示词控制输出）。
"""
from __future__ import annotations

import unittest

from services.protocol import conversation


class ImageQualityPromptTests(unittest.TestCase):
    def test_medium_prompt_contains_quality_and_negative_constraints(self):
        prompt = conversation.build_image_prompt("一只猫", "1024x1024", "medium")

        # 质量档位要译成视觉描述，而不是只留一个英文枚举
        self.assertIn("标准生产质量", prompt)
        self.assertIn("清晰锐利", prompt)
        # 反面锚点：糊图的三种典型特征
        self.assertIn("模糊", prompt)
        self.assertIn("像素感", prompt)
        self.assertIn("涂抹感", prompt)
        # 原 prompt 不能丢
        self.assertIn("一只猫", prompt)

    def test_high_prompt_is_stricter_than_medium(self):
        high = conversation.build_image_prompt("x", "1024x1024", "high")
        medium = conversation.build_image_prompt("x", "1024x1024", "medium")

        self.assertIn("最高生产质量", high)
        self.assertIn("照片级", high)
        self.assertIn("标准生产质量", medium)
        # high 的排除项要更绝对
        self.assertIn("绝对不能出现", high)

    def test_low_prompt_avoids_negative_wording(self):
        """low 档也不能写「低质量」——模型会照着字面理解，真的画糊。"""
        prompt = conversation.build_image_prompt("x", "1024x1024", "low")

        self.assertNotIn("低质量", prompt)
        self.assertNotIn("草稿", prompt)
        self.assertNotIn("快速", prompt)
        # 但仍然要保证不糊
        self.assertIn("模糊", prompt)
        self.assertIn("像素感", prompt)

    def test_auto_prompt_defaults_to_medium_or_better(self):
        prompt = conversation.build_image_prompt("x", "1024x1024", "auto")

        self.assertIn("标准或更高生产质量", prompt)
        self.assertIn("高清图像", prompt)

    def test_unknown_quality_falls_back_to_auto(self):
        """上游新增枚举时不猜，按 auto 处理但保留原词不报错。"""
        prompt = conversation.build_image_prompt("x", "1024x1024", "brand_new_tier")

        self.assertIn("标准或更高生产质量", prompt)
        self.assertIn("高清图像", prompt)

    def test_size_hint_includes_layout_description(self):
        """上游只靠提示词控尺寸，比例描述能提高命中率。"""
        square = conversation.build_image_prompt("x", "1024x1024", "medium")
        landscape = conversation.build_image_prompt("x", "1536x1024", "medium")
        portrait = conversation.build_image_prompt("x", "1024x1536", "medium")

        self.assertIn("1024x1024", square)
        self.assertIn("1:1 正方形构图", square)
        self.assertIn("横向构图", landscape)
        self.assertIn("竖向构图", portrait)

    def test_quality_hint_comes_after_size(self):
        """质量提示放最后，紧邻生成指令更容易被遵守。"""
        prompt = conversation.build_image_prompt("x", "1024x1024", "high")

        self.assertLess(prompt.index("1024x1024"), prompt.index("最高生产质量"))

    def test_no_size_still_keeps_quality_hint(self):
        prompt = conversation.build_image_prompt("x", None, "medium")

        self.assertIn("标准生产质量", prompt)
        self.assertNotIn("输出图片尺寸为", prompt)

    def test_gif_prompt_unchanged(self):
        """GIF 提示词保持原样，不能被质量提示冲掉。"""
        prompt = conversation.build_image_prompt("小猫", None, "auto", "gif", 4, 100)

        self.assertIn("GIF", prompt)
        self.assertIn("4 帧", prompt)
        self.assertIn("100 毫秒", prompt)


if __name__ == "__main__":
    unittest.main()
