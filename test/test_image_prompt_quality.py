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

    def test_low_is_promoted_to_medium_on_web_path(self):
        """网页链路把 low 提升到 medium —— 上游对「低质量」的理解是字面的。

        实测 1254×1254：low → 425 output tokens / 字节每像素 0.96（糊），
        medium → 1650 tokens / 1.40（清晰）。尺寸一样、细节密度差 25%。
        """
        low_prompt = conversation.build_image_prompt("x", "1024x1024", "low")
        medium_prompt = conversation.build_image_prompt("x", "1024x1024", "medium")

        self.assertEqual(low_prompt, medium_prompt)
        self.assertIn("标准生产质量", low_prompt)

    def test_low_promotion_is_case_insensitive(self):
        self.assertEqual(
            conversation.normalize_web_image_quality("LOW"),
            "medium",
        )
        self.assertEqual(
            conversation.normalize_web_image_quality(" Low "),
            "medium",
        )

    def test_non_low_quality_untouched(self):
        """只有 low 被提升，其余档位原样透传。"""
        for quality in ("medium", "high", "xhigh", "max", "auto"):
            self.assertEqual(
                conversation.normalize_web_image_quality(quality),
                quality,
            )

    def test_codex_path_keeps_low(self):
        """codex 链路的 quality 是真参数 —— low 真实生效且上游按 low 计价。

        提升反而是白花钱（官方定价 low $0.006/张 vs medium $0.053/张）。
        """
        self.assertEqual(
            conversation.effective_image_quality("low", "codex-gpt-image-2"),
            "low",
        )
        self.assertEqual(
            conversation.effective_image_quality("low", "plus-codex-gpt-image-2"),
            "low",
        )

    def test_codex_path_normalizes_other_qualities(self):
        self.assertEqual(
            conversation.effective_image_quality("medium", "codex-gpt-image-2"),
            "medium",
        )
        self.assertEqual(
            conversation.effective_image_quality("auto", "codex-gpt-image-2"),
            "auto",
        )

    def test_web_path_promotes_low_regardless_of_model(self):
        """网页模型（gpt-image-2 / 2.5-*）走归一化。"""
        for model in ("gpt-image-2", "gpt-image-2.5-sunburst", "gpt-image-2.6"):
            self.assertEqual(
                conversation.effective_image_quality("low", model),
                "medium",
            )

    def test_effective_quality_without_model_defaults_to_web(self):
        """不传 model 时按网页链路处理（保守：提后不会出糊图）。"""
        self.assertEqual(conversation.effective_image_quality("low"), "medium")
        self.assertEqual(conversation.effective_image_quality("high"), "high")

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


class ImageQualityBillingTests(unittest.TestCase):
    """计费口径必须跟实际渲染档位一致。

    否则会出现「按 medium 渲染、按 low 收 token」—— 对外服务就是亏的，
    而且 token 数对不上会让排障时误判画质档位。
    """

    def test_billing_uses_promoted_quality_on_web_path(self):
        from utils.image_tokens import count_image_output_items_tokens

        size = "1254x1254"
        low_billing = count_image_output_items_tokens(
            [{"url": "http://x/a.png"}], size, "low"
        )
        medium_billing = count_image_output_items_tokens(
            [{"url": "http://x/a.png"}], size, "medium"
        )
        promoted = conversation.effective_image_quality("low", "gpt-image-2")
        actual_billing = count_image_output_items_tokens(
            [{"url": "http://x/a.png"}], size, promoted
        )

        self.assertNotEqual(low_billing, medium_billing)
        self.assertEqual(actual_billing, medium_billing)

    def test_billing_matches_prompt_for_low(self):
        """提示词按 medium 渲染 → 计费也必须按 medium。"""
        from utils.image_tokens import count_image_output_items_tokens

        size = "1024x1024"
        prompt = conversation.build_image_prompt("x", size, "low")
        billing_quality = conversation.effective_image_quality("low", "gpt-image-2")

        # 提示词里已经是 medium 的措辞
        self.assertIn("标准生产质量", prompt)
        # 计费也是 medium
        self.assertEqual(
            count_image_output_items_tokens([{"url": "http://x/a.png"}], size, billing_quality),
            count_image_output_items_tokens([{"url": "http://x/a.png"}], size, "medium"),
        )

    def test_codex_billing_keeps_low(self):
        """codex 链路 low 真实生效，计费也必须是 low。"""
        from utils.image_tokens import count_image_output_items_tokens

        size = "1024x1024"
        billing_quality = conversation.effective_image_quality("low", "codex-gpt-image-2")

        self.assertEqual(billing_quality, "low")
        self.assertEqual(
            count_image_output_items_tokens([{"url": "http://x/a.png"}], size, billing_quality),
            count_image_output_items_tokens([{"url": "http://x/a.png"}], size, "low"),
        )


if __name__ == "__main__":
    unittest.main()
