#!/usr/bin/env python3
"""
罕见病每日快讯采集脚本
- 使用 Exa API 搜索罕见病相关新闻
- 使用 NVIDIA LLM API 翻译为中文
- 使用 LLM 生成快讯板式（过滤 + 分类 + 格式化）
- 按日期存储 JSON 文件（英文/中文/快讯 三个版本）
"""

import json
import os
import sys
import time
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

BJT = timezone(timedelta(hours=8))
UTC = timezone.utc

EXA_API_KEY = os.environ.get("EXA_API_KEY", "")
NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 已知域名 → 中文名(英文名) 映射表
DOMAIN_NAMES = {
    "finance.yahoo.com": "雅虎财经(Yahoo Finance)",
    "news.yahoo.com": "雅虎新闻(Yahoo News)",
    "yahoo.com": "雅虎(Yahoo)",
    "reuters.com": "路透社(Reuters)",
    "nbcnews.com": "NBC新闻(NBC News)",
    "bbc.com": "BBC新闻(BBC News)",
    "bbc.co.uk": "BBC新闻(BBC News)",
    "nytimes.com": "纽约时报(New York Times)",
    "washingtonpost.com": "华盛顿邮报(Washington Post)",
    "globenewswire.com": "环球新闻社(GlobeNewswire)",
    "prnewswire.com": "美通社(PR Newswire)",
    "businesswire.com": "商业资讯(Business Wire)",
    "biospace.com": "BioSpace",
    "fiercepharma.com": "Fierce Pharma",
    "fiercebiotech.com": "Fierce Biotech",
    "pharmaceutical-technology.com": "制药技术(Pharmaceutical Technology)",
    "rarediseases.org": "美国罕见病组织(NORD)",
    "fda.gov": "美国FDA(U.S. FDA)",
    "nih.gov": "美国国立卫生研究院(NIH)",
    "nature.com": "自然(Nature)",
    "science.org": "科学(Science)",
    "thelancet.com": "柳叶刀(The Lancet)",
    "nejm.org": "新英格兰医学杂志(NEJM)",
    "neurosciencenews.com": "神经科学新闻(Neuroscience News)",
    "medscape.com": "Medscape",
    "statnews.com": "STAT新闻(STAT News)",
    "evaluate.com": "Evaluate",
    "marketwatch.com": "市场观察(MarketWatch)",
    "galvnews.com": "加尔维斯顿新闻(Galveston News)",
    "beingpatient.com": "Being Patient",
    "healio.com": "Healio",
    "medpagetoday.com": "MedPage Today",
    "sciencedaily.com": "科学日报(ScienceDaily)",
    "eurekalert.org": "EurekAlert",
    "cnbc.com": "CNBC",
    "foxnews.com": "福克斯新闻(Fox News)",
    "cnn.com": "CNN",
    "theguardian.com": "卫报(The Guardian)",
    "sina.com.cn": "新浪网(Sina)",
    "163.com": "网易(NetEase)",
    "sohu.com": "搜狐(Sohu)",
    "qq.com": "腾讯网(Tencent)",
    "xinhuanet.com": "新华网(Xinhua)",
    "people.com.cn": "人民网(People's Daily)",
    "chinadaily.com.cn": "中国日报(China Daily)",
    "36kr.com": "36氪(36Kr)",
    "dxy.cn": "丁香园(DXY)",
    "pharmadj.com": "医药经济报(PharmaDJ)",
}


# ============================================================
# API 工具函数
# ============================================================

def api_request(url, headers, payload, retries=3, timeout=None):
    """带重试的 HTTP POST 请求"""
    headers = {**headers, "User-Agent": "RareDiseaseNewsBot/1.0"}
    data = json.dumps(payload).encode("utf-8")
    req_timeout = timeout or 60
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=req_timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"  请求失败 (第{attempt+1}次): HTTP {e.code} - {body[:200]}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                raise
        except Exception as e:
            print(f"  请求失败 (第{attempt+1}次): {e}")
            if attempt < retries - 1:
                time.sleep(5 * (attempt + 1))
            else:
                raise


def llm_request(system_prompt, user_prompt, retries=5):
    """调用 NVIDIA LLM API（串行、带重试）"""
    url = "https://integrate.api.nvidia.com/v1/chat/completions"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {NVIDIA_API_KEY}",
    }
    payload = {
        "model": "stepfun-ai/step-3.5-flash",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.3,
        "top_p": 0.9,
        "max_tokens": 4096,
        "stream": False,
    }
    result = api_request(url, headers, payload, retries=retries, timeout=300)
    content = result["choices"][0]["message"]["content"].strip()
    # 清理 markdown 代码块包裹
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return content


def get_source_name(url):
    """从 URL 确定性提取来源名称（不依赖 LLM）"""
    try:
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        # 精确匹配
        if domain in DOMAIN_NAMES:
            return DOMAIN_NAMES[domain]
        # 匹配父域名
        parts = domain.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[i:])
            if parent in DOMAIN_NAMES:
                return DOMAIN_NAMES[parent]
        # 兜底：使用域名本身
        name = domain.replace(".com", "").replace(".org", "").replace(".net", "")
        name = name.replace(".cn", "").replace(".co.uk", "")
        return f"{name}({domain})"
    except Exception:
        return "未知来源"


def make_id(url):
    """根据 URL 生成稳定 ID"""
    return hashlib.md5(url.encode()).hexdigest()[:12]


# ============================================================
# 搜索与提取
# ============================================================

def search_exa(query, start_date, end_date, num_results=10):
    """调用 Exa API 搜索新闻"""
    url = "https://api.exa.ai/search"
    headers = {"x-api-key": EXA_API_KEY, "Content-Type": "application/json"}
    payload = {
        "query": query,
        "type": "auto",
        "category": "news",
        "num_results": num_results,
        "startPublishedDate": start_date,
        "endPublishedDate": end_date,
        "contents": {
            "highlights": {"max_characters": 2000},
        },
    }
    print(f"  搜索: {query}")
    result = api_request(url, headers, payload)
    return result.get("results", [])


def fetch_all_news():
    """多关键词搜索并合并去重"""
    now = datetime.now(BJT)
    yesterday = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    start_date = yesterday.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_date = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"搜索时间范围 (UTC): {start_date} ~ {end_date}")
    print(f"对应北京时间: {yesterday.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%Y-%m-%d %H:%M')}")

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
        highlights = r.get("highlights", [])
        summary = " ".join(highlights)[:500] if highlights else ""
        items.append({
            "id": make_id(r.get("url", "")),
            "title": title,
            "summary": summary,
            "date": r.get("publishedDate", ""),
            "url": r.get("url", ""),
        })
    return items


# ============================================================
# 翻译
# ============================================================

def translate_batch(news_items):
    """批量翻译新闻为中文"""
    if not news_items:
        return []

    translated_items = []
    batch_size = 3

    for i in range(0, len(news_items), batch_size):
        batch = news_items[i : i + batch_size]
        batch_data = [
            {"id": idx, "title": item["title"], "summary": item["summary"][:300]}
            for idx, item in enumerate(batch)
        ]
        prompt = (
            "请将以下新闻标题和摘要翻译为中文。保持 JSON 数组格式输出，"
            "每条包含 id、title、summary 字段。只输出 JSON 数组。\n\n"
            + json.dumps(batch_data, ensure_ascii=False)
        )
        try:
            print(f"  翻译第 {i+1}-{i+len(batch)} 条...")
            content = llm_request(
                "你是专业的医学新闻翻译员。准确翻译罕见病相关新闻。只输出 JSON。",
                prompt,
            )
            translated = json.loads(content)
            for item_zh in translated:
                idx = item_zh.get("id", 0)
                if idx < len(batch):
                    orig = batch[idx]
                    translated_items.append({
                        "id": orig["id"],
                        "title": item_zh.get("title", orig["title"]),
                        "summary": item_zh.get("summary", orig["summary"]),
                        "date": orig["date"],
                        "url": orig["url"],
                    })
        except Exception as e:
            print(f"  翻译批次失败: {e}")
            for item in batch:
                translated_items.append(item.copy())

    return translated_items


# ============================================================
# 快讯管线：过滤 → 格式化
# ============================================================

def filter_news_with_llm(zh_items):
    """使用 LLM 过滤不相关和重复新闻"""
    if not zh_items:
        return []

    items_for_filter = [
        {"id": i, "title": item["title"][:80], "summary": item["summary"][:100]}
        for i, item in enumerate(zh_items)
    ]

    prompt = f"""你是罕见病领域的医学编辑。请审核以下新闻列表，筛选出适合发布为"罕见病快讯"的新闻。

**保留标准（宽松保留，宁多勿少）：**
- 药物审批、临床试验进展、基因疗法
- 新治疗方案、诊断技术突破
- 罕见病相关政策法规
- 重要研究发现、学术成果
- 企业并购、融资、合作等行业动态
- 患者用药可及性相关新闻

**过滤标准（严格过滤，只滤明显不符的）：**
- 个人患者故事/感人叙事（如"XX岁女孩患上罕见病每隔两天就要做透析"这类标题党新闻）
- 明显的软文/广告/推广
- 与罕见病无直接关系的一般健康新闻
- 内容高度重复的新闻（同一事件保留信息量最大的一条）

请返回应**保留**的新闻 id 列表，JSON 数组格式。只输出 JSON 数组，不要输出其他内容。

新闻列表：
{json.dumps(items_for_filter, ensure_ascii=False)}"""

    try:
        print("  调用 LLM 筛选快讯...")
        content = llm_request(
            "你是罕见病领域的资深医学编辑。严格按要求筛选新闻。只输出 JSON 数组。",
            prompt,
        )
        keep_ids = json.loads(content)
        if not isinstance(keep_ids, list):
            print("  筛选结果格式异常，保留全部")
            return zh_items

        filtered = [zh_items[i] for i in keep_ids if isinstance(i, int) and 0 <= i < len(zh_items)]
        print(f"  筛选结果: {len(zh_items)} → {len(filtered)} 条")
        return filtered
    except Exception as e:
        print(f"  筛选失败: {e}，保留全部")
        return zh_items


def format_bulletin_batch(filtered_zh_items, en_items):
    """逐条使用 LLM 格式化快讯（串行请求，追求质量）"""
    if not filtered_zh_items:
        return []

    # 建立 URL → 英文原文 的索引
    en_map = {item["url"]: item for item in en_items}

    bulletin_items = []

    for i, item in enumerate(filtered_zh_items):
        print(f"  格式化第 {i+1}/{len(filtered_zh_items)} 条...")

        # 确定性提取来源
        source_name = get_source_name(item["url"])

        # 获取英文原文作为 LLM 上下文
        en_item = en_map.get(item["url"], {})
        en_context = ""
        if en_item:
            en_context = f"\n英文原标题：{en_item.get('title', '')[:100]}\n英文原摘要：{en_item.get('summary', '')[:200]}"

        prompt = f"""请将以下罕见病新闻格式化为专业快讯。

中文标题：{item['title'][:100]}
中文摘要：{item['summary'][:400]}{en_context}
来源：{source_name}
日期：{item['date']}

请返回严格的 JSON（不要添加任何其他文字）：
{{
  "category": "从以下选一个：药物进展、临床试验、政策法规、研究发现、企业动态、诊断技术、患者关怀、行业资讯",
  "title": "简洁精炼的中文标题（15-30字，不含类别标签）",
  "summary": "专业摘要（100-250字，客观描述新闻要点，包含关键数据和时间节点，以日期开头如'2026年4月14日，...'）"
}}"""

        fallback = False
        try:
            content = llm_request(
                "你是罕见病领域资深医学编辑。将新闻格式化为专业快讯。只输出 JSON。",
                prompt,
            )
            formatted = json.loads(content)
            bulletin_items.append({
                "id": item.get("id", make_id(item["url"])),
                "category": formatted.get("category", "行业资讯"),
                "title": formatted.get("title", item["title"]),
                "summary": formatted.get("summary", item["summary"]),
                "source": source_name,
                "date": item["date"],
                "url": item["url"],
                "fallback": False,
            })
        except Exception as e:
            print(f"    格式化失败: {e}，使用降级数据")
            bulletin_items.append({
                "id": item.get("id", make_id(item["url"])),
                "category": "行业资讯",
                "title": item["title"],
                "summary": item["summary"],
                "source": source_name,
                "date": item["date"],
                "url": item["url"],
                "fallback": True,
            })

        # 串行间隔，减少并发压力
        time.sleep(1)

    return bulletin_items


# ============================================================
# 存储
# ============================================================

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


# ============================================================
# 主流程
# ============================================================

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

    print("=== 罕见病每日快讯采集 ===")
    print(f"日期: {date_str}")
    print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. 搜索
    print("[1/6] 搜索罕见病新闻...")
    raw_results = fetch_all_news()

    if not raw_results:
        print("未找到任何新闻，创建空文件。")
        for f in ["news_en.json", "news_zh.json", "news_bulletin.json"]:
            save_json(os.path.join(data_dir, f), [])
        update_dates_index(date_str)
        return

    # 2. 提取结构化数据
    print("\n[2/6] 提取新闻数据...")
    news_items_en = extract_news_items(raw_results)
    print(f"  提取到 {len(news_items_en)} 条有效新闻")

    # 3. 保存英文版
    print("\n[3/6] 保存英文版...")
    save_json(os.path.join(data_dir, "news_en.json"), news_items_en)

    # 4. 翻译为中文
    print("\n[4/6] 翻译为中文...")
    news_items_zh = translate_batch(news_items_en)
    save_json(os.path.join(data_dir, "news_zh.json"), news_items_zh)

    # 5. LLM 筛选快讯
    print("\n[5/6] 筛选快讯内容...")
    filtered_items = filter_news_with_llm(news_items_zh)

    # 6. LLM 格式化快讯
    print("\n[6/6] 生成快讯格式...")
    bulletin_items = format_bulletin_batch(filtered_items, news_items_en)
    save_json(os.path.join(data_dir, "news_bulletin.json"), bulletin_items)

    # 更新日期索引
    update_dates_index(date_str)

    print(f"\n=== 完成! 英文 {len(news_items_en)} 条 | 中文 {len(news_items_zh)} 条 | 快讯 {len(bulletin_items)} 条 ===")


if __name__ == "__main__":
    main()
