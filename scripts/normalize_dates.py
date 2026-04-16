#!/usr/bin/env python3
"""
历史数据日期格式清洗脚本
- 遍历 data/ 下所有 news_en.json / news_zh.json / news_bulletin.json
- 用修复后的 parse_date_flexible 统一日期格式为 UTC ISO 8601（含 Z）
- 修复：无时区 ISO 串补 UTC、含美国时区缩写（ET 等）正确转换
"""

import json
import os
import sys

# 复用 fetch_news.py 中的解析函数
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fetch_news import parse_date_flexible

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(PROJECT_ROOT, "data")

TARGET_FILES = ["news_en.json", "news_zh.json", "news_bulletin.json"]


def normalize_date_field(date_str):
    """对日期字段执行清洗，返回标准化的 UTC ISO 8601 字符串"""
    if not date_str:
        return date_str
    parsed = parse_date_flexible(date_str)
    # 如果解析后与原始相同（未能解析），返回原始值
    return parsed


def process_file(filepath):
    """处理单个 JSON 文件，返回 (修改数, 总数)"""
    with open(filepath, "r", encoding="utf-8") as f:
        items = json.load(f)

    if not isinstance(items, list):
        return 0, 0

    changed = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        orig_date = item.get("date", "")
        new_date = normalize_date_field(orig_date)
        if new_date != orig_date:
            # 显示变更详情
            print(f"    📅 {orig_date!r} → {new_date!r}")
            item["date"] = new_date
            changed += 1

    if changed > 0:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)

    return changed, len(items)


def main():
    print("=== 历史数据日期格式清洗 ===\n")

    total_changed = 0
    total_items = 0

    # 遍历 data/ 下的日期目录
    date_dirs = sorted(
        d for d in os.listdir(DATA_DIR)
        if os.path.isdir(os.path.join(DATA_DIR, d))
    )

    for date_dir in date_dirs:
        dir_path = os.path.join(DATA_DIR, date_dir)
        for filename in TARGET_FILES:
            filepath = os.path.join(dir_path, filename)
            if not os.path.exists(filepath):
                continue

            print(f"  [{date_dir}/{filename}]")
            changed, count = process_file(filepath)
            total_changed += changed
            total_items += count
            if changed == 0:
                print(f"    ✅ 全部 {count} 条日期格式正确")
            else:
                print(f"    🔧 修复 {changed}/{count} 条")

    print(f"\n=== 完成! 共检查 {total_items} 条，修复 {total_changed} 条 ===")


if __name__ == "__main__":
    main()
