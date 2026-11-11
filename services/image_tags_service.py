from __future__ import annotations

from services.storage import runtime_store as store

# 标签存储：数据库（services/storage/runtime_store.py）。
#
# 旧实现用单个 JSON 文件存 {rel_path: [tag, ...]}：
#   - 每次读写都是「读全文件 → 改 → 写全文件」，标签越多越慢
#   - 按标签反查图片只能全量扫
#
# 现在用 (rel_path, tag) 复合主键的表：
#   - 改某张图的标签 = 删该 rel 的行 + 插新行（同事务）
#   - 按标签反查走 ix_image_tags_tag 索引
#   - 标签总数几万个也只是索引扫描


def load_tags() -> dict[str, list[str]]:
    """返回 {rel_path: [tag, ...]} 全量映射。

    注意：这个接口是给「批量展示所有标签」用的，返回体天然是全量。
    真正的按需查询应该用 `get_tags` / `list_tagged_images`。
    """
    result: dict[str, list[str]] = {}
    for tag in store.list_all_tags():
        for rel in store.list_tagged_images(tag):
            result.setdefault(rel, []).append(tag)
    return result


def get_tags(image_rel: str) -> list[str]:
    return store.get_image_tags(image_rel)


def set_tags(image_rel: str, tags: list[str]) -> list[str]:
    return store.replace_image_tags(image_rel, tags)


def remove_tags(image_rel: str) -> None:
    store.delete_image_tags(image_rel)


def delete_tag(tag: str) -> int:
    """从所有图片中删除指定标签，返回受影响的图片数。"""
    return store.delete_tag_everywhere(tag)


def get_all_tags() -> list[str]:
    return store.list_all_tags()
