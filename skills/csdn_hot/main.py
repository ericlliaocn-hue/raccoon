# requires: httpx
import sys
import json
import httpx


def fetch_csdn_hot(limit=20):
    """通过 CSDN hot-rank API 获取热门文章"""
    url = "https://blog.csdn.net/phoenix/web/blog/hot-rank"
    params = {"page": 0, "pageSize": limit}
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    debug_info = {}

    try:
        with httpx.Client(timeout=15.0) as client:
            resp = client.get(url, params=params, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        return [], {"api_url": url, "api_status": e.response.status_code, "error_detail": str(e)}
    except Exception as e:
        return [], {"api_url": url, "error_detail": str(e)}

    if data.get("code") != 200 or not data.get("data"):
        debug_info = {
            "api_url": url,
            "api_status": data.get("code"),
            "api_response": json.dumps(data, ensure_ascii=False)[:500],
            "error_detail": f"API返回非200或数据为空: code={data.get('code')}",
        }
        return [], debug_info

    articles = []
    for item in data["data"][:limit]:
        article_id = item.get("productId", "")
        username = item.get("userName", "")
        detail_url = item.get("articleDetailUrl", "")

        # 优先用 API 返回的完整 URL，否则自行拼接
        if not detail_url and username and article_id:
            detail_url = f"https://blog.csdn.net/{username}/article/details/{article_id}"

        articles.append({
            "title": item.get("articleTitle", "未知标题"),
            "author": item.get("nickName", "未知作者"),
            "views": item.get("viewCount", "0"),
            "comments": item.get("commentCount", "0"),
            "favors": item.get("favorCount", "0"),
            "hot_score": item.get("pcHotRankScore", "-"),
            "link": detail_url or "#",
        })

    return articles, debug_info


def format_reply(articles):
    if not articles:
        return "暂未获取到 CSDN 热门文章，请稍后再试。"

    reply_lines = ["🔥 **CSDN 热门文章排行榜** 🔥\n"]
    for idx, a in enumerate(articles, 1):
        reply_lines.append(
            f"**{idx}. [{a['title']}]({a['link']})**\n"
            f"   👤 {a['author']} | 👁️ 阅读: {a['views']} | 💬 评论: {a['comments']} | 👍 收藏: {a['favors']} | 🔥 热度: {a['hot_score']}\n"
        )

    return "\n".join(reply_lines)


def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        input_data = {}

    task_id = input_data.get("task_id", "")
    params = input_data.get("params", {})
    origin_message = input_data.get("origin_message", "")

    limit = params.get("limit") or 20
    if not params.get("limit") and origin_message and not origin_message.startswith("flow_step:"):
        import re
        match = re.search(r'\d+', origin_message)
        if match:
            limit = int(match.group())

    try:
        articles, debug_info = fetch_csdn_hot(limit=limit)
        reply = format_reply(articles)
    except Exception as e:
        reply = f"❌ 获取 CSDN 热门文章失败: {str(e)}"
        debug_info = {"error_detail": str(e)}

    output = {
        "task_id": task_id,
        "reply": reply,
        "files": [],
    }
    if debug_info:
        output["_debug"] = debug_info
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
