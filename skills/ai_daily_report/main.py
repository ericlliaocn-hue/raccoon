#!/usr/bin/env python3
import sys
import json
import re
import httpx
import feedparser
from bs4 import BeautifulSoup


def read_input():
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        return json.loads(raw)
    except json.JSONDecodeError:
        return {}


def extract_param(data, key, pattern=None, default=None):
    if data.get("params") and data["params"].get(key) is not None:
        return data["params"][key]
    origin = data.get("origin_message", "")
    if origin and pattern:
        match = re.search(pattern, origin, re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return default


def http_get(url, timeout=15):
    """用 httpx 发起 GET 请求（项目已有依赖，API 与 requests 兼容）"""
    with httpx.Client(timeout=timeout, headers={
        'User-Agent': 'Mozilla/5.0 (compatible; RaccoonBot/1.0)'
    }) as client:
        resp = client.get(url)
        resp.raise_for_status()
        return resp.content


def fetch_rss_feed(url, source_name, max_items=5):
    items = []
    try:
        content = http_get(url)
        feed = feedparser.parse(content)
        for entry in feed.entries[:max_items]:
            title = entry.get('title', '').strip()
            summary = entry.get('summary', '').strip()
            if not summary and entry.get('description'):
                summary = entry.get('description').strip()
            if summary:
                soup = BeautifulSoup(summary, 'html.parser')
                summary = soup.get_text(separator=' ', strip=True)
            if title:
                items.append({
                    "source": source_name,
                    "title": title,
                    "summary": summary[:200] + '...' if len(summary) > 200 else summary
                })
    except Exception as e:
        items.append({"source": source_name, "title": f"获取失败: {str(e)}", "summary": ""})
    return items


def fetch_web_page(url, source_name, selector=None, max_items=5):
    items = []
    try:
        content = http_get(url)
        soup = BeautifulSoup(content, 'html.parser')

        if selector:
            elements = soup.select(selector)
            for el in elements[:max_items]:
                title = el.get_text(strip=True)
                if title:
                    items.append({"source": source_name, "title": title, "summary": ""})
        else:
            title = soup.title.string.strip() if soup.title and soup.title.string else "无标题"
            items.append({"source": source_name, "title": title, "summary": ""})
    except Exception as e:
        items.append({"source": source_name, "title": f"获取失败: {str(e)}", "summary": ""})
    return items


def generate_daily_report(news_items):
    report_lines = ["📰 AI 日报", "=" * 30]

    grouped = {}
    for item in news_items:
        src = item["source"]
        if src not in grouped:
            grouped[src] = []
        grouped[src].append(item)

    for source, items in grouped.items():
        report_lines.append(f"\n🌐 {source}")
        for idx, item in enumerate(items, 1):
            report_lines.append(f"  {idx}. {item['title']}")
            if item['summary']:
                report_lines.append(f"     💡 {item['summary']}")

    report_lines.append("\n" + "=" * 30)
    report_lines.append("🦝 由浣熊 (Project Raccoon) 为您整理")
    return "\n".join(report_lines)


def main():
    data = read_input()
    task_id = data.get("task_id", "unknown")

    try:
        # 参数读取优先级: params > origin_message
        custom_sources = extract_param(data, "sources", r"来源[:：]\s*(.+)")
        max_per_source = int(extract_param(data, "max_items", r"数量[:：]\s*(\d+)", default=5))

        # 默认AI资讯源 (RSS优先)
        sources = [
            {"url": "https://hnrss.org/newest?q=AI", "name": "HackerNews AI", "type": "rss"},
            {"url": "https://techcrunch.com/category/artificial-intelligence/feed/", "name": "TechCrunch AI", "type": "rss"},
            {"url": "https://www.jiqizhixin.com/rss", "name": "机器之心", "type": "rss"},
            {"url": "https://www.36kr.com/feed", "name": "36氪", "type": "rss"}
        ]

        all_news = []

        # 如果指定了自定义来源，覆盖默认配置
        if custom_sources:
            custom_urls = [s.strip() for s in custom_sources.split(",")]
            sources = [{"url": url, "name": url.split("//")[1].split("/")[0], "type": "web"} for url in custom_urls if url.startswith("http")]

        for src in sources:
            if src["type"] == "rss":
                all_news.extend(fetch_rss_feed(src["url"], src["name"], max_per_source))
            else:
                all_news.extend(fetch_web_page(src["url"], src["name"], max_items=max_per_source))

        if not all_news:
            reply = "未能获取到任何AI资讯，请检查网络连接或稍后再试。"
        else:
            reply = generate_daily_report(all_news)

        result = {
            "task_id": task_id,
            "reply": reply,
            "files": []
        }

    except Exception as e:
        result = {
            "task_id": task_id,
            "reply": f"生成AI日报时发生错误: {str(e)}",
            "files": []
        }

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
