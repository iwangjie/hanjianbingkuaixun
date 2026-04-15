#!/usr/bin/env python3
"""
罕见病每日快讯采集脚本
- 使用 Exa API 搜索罕见病相关新闻
- 使用 NVIDIA LLM API 翻译为中文
- 按日期存储 JSON 文件
"""

import json
import os
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone

# 北京时间 UTC+8
BJT = timezone(timedelta(hours=8))

EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def api_request(url, headers, payload, retries=3):
    """带重试的 API 请求"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as e:
            print(f"  请求失败 (第{attempt+1}次): {e}")
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
            else:
                raise


def search_exa(query, start_date, end_date, num_results=10):
    """调用 Exa API 搜索新闻"""
    url = "https://api.exa.ai/search"
    headers = {
        "x-api-key": EXA_API_KEY,
        "Content-Type": "application/json",
    }
    payload = {
        "query": query,
        "type": "auto",
        "category": "news",
        "num_results": num_results,
        "startPublishedDate": start_date,
        "endPublishedDate": end_date,
        "contents": {
            "highlights": {"max_characters": 4000},
            "text": {"max_characters": 2000},
        },
    }
    print(f"  搜索: {query}")
    result = api_request(url, headers, payload)
    return result.get("results", [])


def fetch_all_news():
    """多关键词搜索并合并去重"""
    now = datetime.now(BJT)
    yesterday = (now - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    start_date = yesterday.strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_date = now.strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"搜索时间范围: {start_date} ~ {end_date}")

    queries = [
        "rare disease news treatment therapy breakthrough",
        "orphan drug approval clinical trial rare disease",
        "罕见病 治疗 新闻 药物",
    ]

    all_results = []
    seen_urls = set()

    for query in queries:
        try:
            results = search_exa(query, start_date, end_date, num_results=10)
            for r in results:
                url = r.get("url", "")
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    all_results.append(r)
            print(f"  找到 {len(results)} 条结果")
        except Exception as e:
            print(f"  搜索失败: {e}")

    print(f"合计去重后: {len(all_results)} 条新闻")
    return all_results


def extract_news_items(raw_results):
    """从搜索结果中提取结构化新闻"""
    items = []
    for r in raw_results:
        title = r.get("title", "").strip()
        if not title:
            continue

        # 优先使用 highlights，否则使用 text 截取
        highlights = r.get("highlights", [])
        text = r.get("text", "")
        if highlights:
            summary = " ".join(highlights)[:800]
        elif text:
            summary = text[:800]
        else:
            summary = ""

        pub_date = r.get("publishedDate", "")
        url = r.get("url", "")

        items.append(
            {
                "title": title,
                "summary": summary,
                "date": pub_date,
                "url": url,
            }
        )
    return items


def translate_batch(news_items):
    """批量翻译新闻标题和摘要为中文"""
    if not news_items:
        return []

    # 构建翻译请求，每次最多翻译 5 条以控制 token 长度
    translated_items = []
    batch_size = 5

    for i in range(0, len(news_items), batch_size):
        batch = news_items[i : i + batch_size]
        batch_data = []
        for idx, item in enumerate(batch):
            batch_data.append(
                {"id": idx, "title": item["title"], "summary": item["summary"]}
            )

        prompt = f"""请将以下新闻标题和摘要翻译为中文。保持 JSON 数组格式输出，每条包含 id、title、summary 字段。
只输出 JSON 数组，不要输出其他内容。

{json.dumps(batch_data, ensure_ascii=False)}"""

        url = "https://integrate.api.nvidia.com/v1/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {NVIDIA_API_KEY}",
        }
        payload = {
            "model": "stepfun-ai/step-3.5-flash",
            "messages": [
                {
                    "role": "system",
                    "content": "你是一个专业的医学新闻翻译员。请准确翻译罕见病相关新闻。只输出 JSON，不要添加任何其他文字或 markdown 标记。",
                },
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.3,
            "top_p": 0.9,
            "max_tokens": 8192,
            "stream": False,
        }

        try:
            print(f"  翻译第 {i+1}-{i+len(batch)} 条...")
            result = api_request(url, headers, payload)
            content = result["choices"][0]["message"]["content"]

            # 清理可能的 markdown 包裹
            content = content.strip()
            if content.startswith("```"):
                lines = content.split("\n")
                content = "\n".join(lines[1:])
                if content.endswith("```"):
                    content = content[:-3]
                content = content.strip()

            translated = json.loads(content)

            for item_zh in translated:
                idx = item_zh.get("id", 0)
                if idx < len(batch):
                    original = batch[idx]
                    translated_items.append(
                        {
                            "title": item_zh.get("title", original["title"]),
                            "summary": item_zh.get("summary", original["summary"]),
                            "date": original["date"],
                            "url": original["url"],
                        }
                    )

        except Exception as e:
            print(f"  翻译批次失败: {e}")
            # 翻译失败时保留原文
            for item in batch:
                translated_items.append(item.copy())

    return translated_items


def save_json(filepath, data):
    """保存 JSON 文件"""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"  已保存: {filepath}")


def update_dates_index(date_str):
    """更新日期索引文件"""
    index_file = os.path.join(PROJECT_ROOT, "data", "dates.json")
    dates = []
    if os.path.exists(index_file):
        with open(index_file, "r", encoding="utf-8") as f:
            try:
                dates = json.load(f)
            except json.JSONDecodeError:
                dates = []

    if date_str not in dates:
        dates.append(date_str)
        dates.sort(reverse=True)

    save_json(index_file, dates)


def main():
    if not EXA_API_KEY:
        print("错误: 未设置 EXA_API_KEY 环境变量")
        sys.exit(1)
    if not NVIDIA_API_KEY:
        print("错误: 未设置 NVIDIA_API_KEY 环境变量")
        sys.exit(1)

    now = datetime.now(BJT)
    date_str = now.strftime("%Y-%m-%d")
    data_dir = os.path.join(PROJECT_ROOT, "data", date_str)

    print(f"=== 罕见病每日快讯采集 ===")
    print(f"日期: {date_str}")
    print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. 搜索新闻
    print("[1/4] 搜索罕见病新闻...")
    raw_results = fetch_all_news()

    if not raw_results:
        print("未找到任何新闻，创建空文件。")
        save_json(os.path.join(data_dir, "news_en.json"), [])
        save_json(os.path.join(data_dir, "news_zh.json"), [])
        update_dates_index(date_str)
        return

    # 2. 提取结构化数据
    print("\n[2/4] 提取新闻数据...")
    news_items = extract_news_items(raw_results)
    print(f"  提取到 {len(news_items)} 条有效新闻")

    # 3. 保存英文版
    print("\n[3/4] 保存英文版...")
    en_path = os.path.join(data_dir, "news_en.json")
    save_json(en_path, news_items)

    # 4. 翻译并保存中文版
    print("\n[4/4] 翻译为中文并保存...")
    news_items_zh = translate_batch(news_items)
    zh_path = os.path.join(data_dir, "news_zh.json")
    save_json(zh_path, news_items_zh)

    # 更新日期索引
    update_dates_index(date_str)

    print(f"\n=== 完成! 共 {len(news_items)} 条快讯 ===")


if __name__ == "__main__":
    main()
