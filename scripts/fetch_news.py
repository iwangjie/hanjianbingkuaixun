#!/usr/bin/env python3
"""
罕见病每日快讯采集脚本（多通道版）
- 支持 Exa / Tavily / Jina / Metaso 等多个搜索通道
- 通道级容错：单个通道失败不影响整体
- 统一数据模型 + 合并去重
- 使用 NVIDIA LLM API 翻译 + 快讯格式化
- 按日期存储 JSON 文件（英文/中文/快讯 三个版本）
"""

import json
import os
import re
import sys
import time
import hashlib
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

BJT = timezone(timedelta(hours=8))
UTC = timezone.utc

NVIDIA_API_KEY = os.environ.get("NVIDIA_API_KEY", "")
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 每个通道最多返回的条目数（避免 LLM 成本爆炸）
MAX_ITEMS_PER_CHANNEL = 15
# 合并后送入 LLM 管线的最大条目数
MAX_TOTAL_ITEMS = 30
# 快讯最大条目数
MAX_BULLETIN_ITEMS = 15

# URL 追踪参数黑名单（去重用）
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "ref", "source", "spm", "from", "isappinstalled",
}

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
    "pharmaphorum.com": "PharmaPHorum",
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
# 通道注册表 — 增删通道只需修改此处 + 实现对应 search 函数
# ============================================================

def build_channel_registry():
    """构建通道注册表，每个通道包含：搜索函数、API Key、查询列表、单通道上限"""
    return {
        "exa": {
            "fn": channel_exa,
            "env_key": "EXA_API_KEY",
            "enabled_env": "CHANNEL_EXA_ENABLED",
            "label": "Exa",
            "queries_en": [
                "rare disease news treatment therapy breakthrough",
                "orphan drug approval clinical trial rare disease",
            ],
            "queries_zh": ["罕见病 治疗 新闻 药物"],
            "max_items": 20,
        },
        "tavily": {
            "fn": channel_tavily,
            "env_key": "TAVILY_API_KEY",
            "enabled_env": "CHANNEL_TAVILY_ENABLED",
            "label": "Tavily",
            "queries_en": [
                "rare disease treatment drug approval news",
                "orphan drug clinical trial breakthrough",
            ],
            "queries_zh": [],
            "max_items": 15,
        },
        "jina": {
            "fn": channel_jina,
            "env_key": "JINA_API_KEY",
            "enabled_env": "CHANNEL_JINA_ENABLED",
            "label": "Jina",
            "queries_en": ["rare disease drug approval treatment news 2026"],
            "queries_zh": ["罕见病 药物 审批 治疗 新闻"],
            "max_items": 10,
        },
        "metaso": {
            "fn": channel_metaso,
            "env_key": "METASO_API_KEY",
            "enabled_env": "CHANNEL_METASO_ENABLED",
            "label": "秘塔搜索",
            "queries_en": [],
            "queries_zh": [
                "罕见病 药物 治疗 审批 新闻",
                "罕见病 基因疗法 临床试验",
            ],
            "max_items": 10,
        },
    }


CHANNELS = None  # 延迟初始化，见 get_channels()


def get_channels():
    """获取通道注册表（延迟初始化）"""
    global CHANNELS
    if CHANNELS is None:
        CHANNELS = build_channel_registry()
    return CHANNELS


def is_channel_enabled(name):
    """检查通道是否启用：有 API Key 且未被显式禁用"""
    cfg = get_channels()[name]
    api_key = os.environ.get(cfg["env_key"], "")
    if not api_key:
        return False
    disabled = os.environ.get(cfg["enabled_env"], "1").strip()
    return disabled != "0"


# ============================================================
# HTTP 工具函数
# ============================================================

def http_post(url, headers, payload, retries=3, timeout=60):
    """带重试的 HTTP POST 请求"""
    headers = {**headers, "User-Agent": "RareDiseaseNewsBot/1.0"}
    data = json.dumps(payload).encode("utf-8")
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"  请求失败 (第{attempt+1}次): HTTP {e.code} - {body[:200]}")
        except Exception as e:
            print(f"  请求失败 (第{attempt+1}次): {e}")
        if attempt < retries - 1:
            time.sleep(5 * (attempt + 1))
    return None


def http_get(url, headers, retries=3, timeout=30):
    """带重试的 HTTP GET 请求"""
    headers = {**headers, "User-Agent": "RareDiseaseNewsBot/1.0"}
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers, method="GET")
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            print(f"  请求失败 (第{attempt+1}次): HTTP {e.code} - {body[:200]}")
        except Exception as e:
            print(f"  请求失败 (第{attempt+1}次): {e}")
        if attempt < retries - 1:
            time.sleep(3 * (attempt + 1))
    return None


def llm_request(system_prompt, user_prompt, retries=3, max_tokens=4096, timeout=120):
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
        "max_tokens": max_tokens,
        "stream": False,
    }
    result = http_post(url, headers, payload, retries=retries, timeout=timeout)
    if not result:
        raise RuntimeError("LLM 请求失败（已耗尽重试次数）")
    content = (result.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
    if not content:
        raise RuntimeError("LLM 返回空内容")
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.endswith("```"):
            content = content[:-3]
        content = content.strip()
    return content


# ============================================================
# URL 规范化与工具函数
# ============================================================

def canonical_url(raw_url):
    """规范化 URL：去追踪参数、去 fragment、统一斜杠"""
    try:
        p = urlparse(raw_url)
        host = (p.hostname or "").lower()
        if host.startswith("www."):
            host = host[4:]
        qs = parse_qs(p.query, keep_blank_values=False)
        clean_qs = {k: v for k, v in qs.items() if k.lower() not in TRACKING_PARAMS}
        path = p.path.rstrip("/") or "/"
        return urlunparse(("https", host, path, "", urlencode(clean_qs, doseq=True), ""))
    except Exception:
        return raw_url


def get_source_name(url):
    """从 URL 确定性提取来源名称"""
    try:
        parsed = urlparse(url)
        domain = (parsed.hostname or "").lower()
        if domain.startswith("www."):
            domain = domain[4:]
        if domain in DOMAIN_NAMES:
            return DOMAIN_NAMES[domain]
        parts = domain.split(".")
        for i in range(1, len(parts)):
            parent = ".".join(parts[i:])
            if parent in DOMAIN_NAMES:
                return DOMAIN_NAMES[parent]
        name = re.sub(r"\.(com|org|net|cn|co\.uk|io)$", "", domain)
        return f"{name}({domain})"
    except Exception:
        return "未知来源"


def make_id(url):
    """根据规范化 URL 生成稳定 ID"""
    return hashlib.md5(canonical_url(url).encode()).hexdigest()[:12]


def parse_date_flexible(date_str):
    """尝试多种格式解析日期，返回统一的 UTC ISO 8601 字符串 (含 Z)"""
    if not date_str:
        return ""
    
    # 已经是 ISO 格式且含 Z
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$", date_str):
        return date_str
        
    # 如果是带时区偏移的 ISO 格式
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        pass

    # RFC 2822: "Tue, 14 Apr 2026 10:04:45 GMT"
    for fmt in [
        "%a, %d %b %Y %H:%M:%S %Z",
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]:
        try:
            dt = datetime.strptime(date_str, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    # 中文日期: "2026年02月28日"
    m = re.match(r"(\d{4})年(\d{1,2})月(\d{1,2})日", date_str)
    if m:
        dt = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)), tzinfo=BJT)
        return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Jina 格式: "Mar 26, 2026"
    for fmt in ["%b %d, %Y", "%B %d, %Y"]:
        try:
            dt = datetime.strptime(date_str, fmt)
            dt = dt.replace(tzinfo=UTC) # 假设国际来源为 UTC
            return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            continue

    return date_str


def to_bjt_datetime(date_str):
    """将任何解析后的日期字符串转换为 BJT datetime 对象"""
    if not date_str:
        return None
    try:
        # 处理带有 Z 或偏移量的 ISO 格式
        dt_str = parse_date_flexible(date_str)
        if dt_str.endswith("Z"):
            dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(dt_str)
        return dt.astimezone(BJT)
    except Exception:
        return None


def is_within_date_range(date_str, start_dt_bjt, end_dt_bjt):
    """检查日期是否在采集范围内（使用 BJT 进行判断）"""
    if not date_str:
        return True
    try:
        dt_bjt = to_bjt_datetime(date_str)
        if not dt_bjt:
            return True
        # 宽松判断：允许在 BJT 范围的前后各 12 小时内（照顾全球时差）
        return (start_dt_bjt - timedelta(hours=12)) <= dt_bjt <= (end_dt_bjt + timedelta(hours=12))
    except Exception:
        return True


# ============================================================
# 通道实现 — Exa
# ============================================================

def channel_exa(queries_en, queries_zh, start_date_utc, end_date_utc, max_items):
    """Exa API 通道"""
    api_key = os.environ.get("EXA_API_KEY", "")
    items = []
    seen = set()
    all_queries = queries_en + queries_zh

    for query in all_queries:
        print(f"    Exa 搜索: {query}")
        result = http_post(
            "https://api.exa.ai/search",
            {"x-api-key": api_key, "Content-Type": "application/json"},
            {
                "query": query,
                "type": "auto",
                "category": "news",
                "num_results": 10,
                "startPublishedDate": start_date_utc,
                "endPublishedDate": end_date_utc,
                "contents": {"highlights": {"max_characters": 2000}},
            },
        )
        if not result:
            print(f"    Exa 查询失败: {query}")
            continue

        for r in result.get("results", []):
            url = r.get("url", "")
            curl = canonical_url(url)
            if not url or curl in seen:
                continue
            seen.add(curl)
            highlights = r.get("highlights", [])
            items.append({
                "title": (r.get("title") or "").strip(),
                "summary": " ".join(highlights)[:500] if highlights else "",
                "url": url,
                "date": parse_date_flexible(r.get("publishedDate", "")),
                "channel": "exa",
            })
            if len(items) >= max_items:
                break
        print(f"    找到 {len(result.get('results', []))} 条")
        if len(items) >= max_items:
            break

    return items


# ============================================================
# 通道实现 — Tavily
# ============================================================

def channel_tavily(queries_en, queries_zh, start_date_utc, end_date_utc, max_items):
    """Tavily API 通道"""
    api_key = os.environ.get("TAVILY_API_KEY", "")
    items = []
    seen = set()
    all_queries = queries_en + queries_zh

    for query in all_queries:
        print(f"    Tavily 搜索: {query}")
        result = http_post(
            "https://api.tavily.com/search",
            {"Content-Type": "application/json"},
            {
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "topic": "news",
                "days": 3,
                "max_results": 10,
                "include_answer": False,
            },
        )
        if not result:
            print(f"    Tavily 查询失败: {query}")
            continue

        for r in result.get("results", []):
            url = r.get("url", "")
            curl = canonical_url(url)
            if not url or curl in seen:
                continue
            seen.add(curl)
            items.append({
                "title": (r.get("title") or "").strip(),
                "summary": (r.get("content") or "")[:500],
                "url": url,
                "date": parse_date_flexible(r.get("published_date", "")),
                "channel": "tavily",
            })
            if len(items) >= max_items:
                break
        print(f"    找到 {len(result.get('results', []))} 条")
        if len(items) >= max_items:
            break

    return items


# ============================================================
# 通道实现 — Jina
# ============================================================

def channel_jina(queries_en, queries_zh, start_date_utc, end_date_utc, max_items):
    """Jina Search API 通道"""
    api_key = os.environ.get("JINA_API_KEY", "")
    items = []
    seen = set()
    all_queries = queries_en + queries_zh

    for query in all_queries:
        print(f"    Jina 搜索: {query}")
        encoded_q = urllib.request.quote(query)
        result = http_get(
            f"https://s.jina.ai/{encoded_q}",
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "X-Retain-Images": "none",
            },
            timeout=30,
        )
        if not result or "data" not in result:
            print(f"    Jina 查询失败: {query}")
            continue

        for r in result["data"]:
            url = r.get("url", "")
            curl = canonical_url(url)
            if not url or curl in seen:
                continue
            seen.add(curl)
            desc = r.get("description") or ""
            content = r.get("content") or ""
            summary = desc[:500] if desc else content[:500]
            items.append({
                "title": (r.get("title") or "").strip(),
                "summary": summary,
                "url": url,
                "date": parse_date_flexible(r.get("publishedTime") or r.get("date") or ""),
                "channel": "jina",
            })
            if len(items) >= max_items:
                break
        print(f"    找到 {len(result.get('data', []))} 条")
        if len(items) >= max_items:
            break

    return items


# ============================================================
# 通道实现 — 秘塔搜索 (Metaso)
# ============================================================

def channel_metaso(queries_en, queries_zh, start_date_utc, end_date_utc, max_items):
    """秘塔搜索 API 通道"""
    api_key = os.environ.get("METASO_API_KEY", "")
    items = []
    seen = set()
    all_queries = queries_zh + queries_en

    for query in all_queries:
        print(f"    秘塔搜索: {query}")
        result = http_post(
            "https://metaso.cn/api/v1/search",
            {
                "Authorization": f"Bearer {api_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            {
                "q": query,
                "scope": "webpage",
                "includeSummary": False,
                "size": "10",
                "includeRawContent": False,
                "conciseSnippet": False,
            },
        )
        if not result or "webpages" not in result:
            print(f"    秘塔查询失败: {query}")
            continue

        for r in result["webpages"]:
            url = r.get("link", "")
            curl = canonical_url(url)
            if not url or curl in seen:
                continue
            seen.add(curl)
            items.append({
                "title": (r.get("title") or "").strip(),
                "summary": (r.get("snippet") or "")[:500],
                "url": url,
                "date": parse_date_flexible(r.get("date") or ""),
                "channel": "metaso",
            })
            if len(items) >= max_items:
                break
        print(f"    找到 {len(result.get('webpages', []))} 条")
        if len(items) >= max_items:
            break

    return items


# ============================================================
# 多通道采集 + 合并去重
# ============================================================

def fetch_all_news():
    """遍历所有启用的通道，采集并合并去重"""
    now = datetime.now(BJT)
    # 扩大搜索窗口到 48 小时以覆盖全球“今天”
    start_dt_bjt = (now - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
    
    start_utc = start_dt_bjt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    end_utc = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    print(f"搜索时间范围 (UTC): {start_utc} ~ {end_utc}")
    print(f"对应北京时间: {start_dt_bjt.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%Y-%m-%d %H:%M')}")

    channels = get_channels()
    enabled = [name for name in channels if is_channel_enabled(name)]
    disabled = [name for name in channels if not is_channel_enabled(name)]
    print(f"启用通道: {', '.join(enabled) or '无'}")
    if disabled:
        print(f"未启用通道: {', '.join(disabled)}")

    if not enabled:
        print("错误: 没有任何可用通道")
        return [], 0, 0

    # 逐通道采集
    all_items = []
    success_count = 0

    for name in enabled:
        cfg = channels[name]
        print(f"\n  [{cfg['label']}] 开始采集...")
        try:
            items = cfg["fn"](
                cfg["queries_en"], cfg["queries_zh"],
                start_utc, end_utc, cfg["max_items"],
            )
            # 过滤空标题
            items = [it for it in items if it.get("title")]
            # 日期范围过滤
            items = [it for it in items if is_within_date_range(it.get("date"), start_dt_bjt, now)]
            print(f"  [{cfg['label']}] 采集到 {len(items)} 条有效新闻")
            all_items.extend(items)
            success_count += 1
        except Exception as e:
            print(f"  [{cfg['label']}] 通道失败: {e}")

    # 跨通道 URL 去重（保留第一条，通常来自更权威的通道）
    seen_urls = set()
    deduped = []
    for item in all_items:
        curl = canonical_url(item["url"])
        if curl not in seen_urls:
            seen_urls.add(curl)
            deduped.append(item)

    # 控制总量
    if len(deduped) > MAX_TOTAL_ITEMS:
        deduped = deduped[:MAX_TOTAL_ITEMS]

    print(f"\n合计: {len(all_items)} 条 → 去重后 {len(deduped)} 条 "
          f"(成功通道 {success_count}/{len(enabled)})")

    return deduped, success_count, len(enabled)


def extract_news_items(raw_items):
    """标准化新闻条目（添加 id，规范日期）"""
    items = []
    for r in raw_items:
        items.append({
            "id": make_id(r["url"]),
            "title": r["title"],
            "summary": r.get("summary", ""),
            "date": r.get("date", ""),
            "url": r["url"],
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
                max_tokens=2048,
                timeout=90,
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

def filter_news_with_llm(zh_items, max_bulletin=20):
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
            max_tokens=1024,
            timeout=60,
        )
        keep_ids = json.loads(content)
        if not isinstance(keep_ids, list):
            print("  筛选结果格式异常，保留前 {max_bulletin} 条")
            return zh_items[:max_bulletin]

        filtered = [zh_items[i] for i in keep_ids if isinstance(i, int) and 0 <= i < len(zh_items)]
        print(f"  筛选结果: {len(zh_items)} → {len(filtered)} 条")
        return filtered[:max_bulletin]
    except Exception as e:
        print(f"  筛选失败: {e}，保留前 {max_bulletin} 条")
        return zh_items[:max_bulletin]


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

        # 转换为北京时间以便 LLM 生成
        dt_bjt = to_bjt_datetime(item["date"])
        date_bjt_str = dt_bjt.strftime("%Y年%m月%d日") if dt_bjt else "近期"

        # 获取英文原文作为 LLM 上下文
        en_item = en_map.get(item["url"], {})
        en_context = ""
        if en_item:
            en_context = f"\n英文原标题：{en_item.get('title', '')[:100]}\n英文原摘要：{en_item.get('summary', '')[:200]}"

        prompt = f"""请将以下罕见病新闻格式化为专业快讯。

中文标题：{item['title'][:100]}
中文摘要：{item['summary'][:400]}{en_context}
来源：{source_name}
原始发布日期（UTC/混合）：{item['date']}
北京时间发布日期：{date_bjt_str}

请返回严格的 JSON（不要添加任何其他文字）：
{{
  "category": "从以下选一个：药物进展、临床试验、政策法规、研究发现、企业动态、诊断技术、患者关怀、行业资讯",
  "title": "简洁精炼的中文标题（15-30字，不含类别标签）",
  "summary": "专业摘要（100-250字，客观描述新闻要点，包含关键数据和时间节点，**必须以北京时间日期开头**，如'{date_bjt_str}，...'）"
}}"""

        fallback = False
        try:
            content = llm_request(
                "你是罕见病领域资深医学编辑。将新闻格式化为专业快讯。只输出 JSON。",
                prompt,
                max_tokens=1024,
                timeout=90,
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
    if not NVIDIA_API_KEY:
        print("错误: 未设置 NVIDIA_API_KEY 环境变量")
        sys.exit(1)

    now = datetime.now(BJT)
    date_str = now.strftime("%Y-%m-%d")
    data_dir = os.path.join(PROJECT_ROOT, "data", date_str)

    print("=== 罕见病每日快讯采集（多通道） ===")
    print(f"日期: {date_str}")
    print(f"当前北京时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print()

    # 1. 多通道搜索
    print("[1/6] 多通道搜索罕见病新闻...")
    raw_items, success_count, total_channels = fetch_all_news()

    if success_count == 0:
        print("错误: 所有通道均失败，无法继续")
        sys.exit(1)

    if not raw_items:
        print("未找到任何新闻，创建空文件。")
        for f in ["news_en.json", "news_zh.json", "news_bulletin.json"]:
            save_json(os.path.join(data_dir, f), [])
        update_dates_index(date_str)
        return

    # 2. 提取结构化数据
    print("\n[2/6] 提取新闻数据...")
    news_items_en = extract_news_items(raw_items)
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
    filtered_items = filter_news_with_llm(news_items_zh, max_bulletin=MAX_BULLETIN_ITEMS)

    # 6. LLM 格式化快讯
    print("\n[6/6] 生成快讯格式...")
    bulletin_items = format_bulletin_batch(filtered_items, news_items_en)
    save_json(os.path.join(data_dir, "news_bulletin.json"), bulletin_items)

    # 更新日期索引
    update_dates_index(date_str)

    print(f"\n=== 完成! 英文 {len(news_items_en)} 条 | 中文 {len(news_items_zh)} 条 | 快讯 {len(bulletin_items)} 条 ==="
          f"\n通道状态: {success_count}/{total_channels} 成功")


if __name__ == "__main__":
    main()
