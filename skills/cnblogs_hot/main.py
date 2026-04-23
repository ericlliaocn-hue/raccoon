# requires: httpx beautifulsoup4
import sys
import json
import httpx
from bs4 import BeautifulSoup

def get_cnblogs_hot():
    url = "https://www.cnblogs.com/aggsite/topviews"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    debug_info = {}

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=headers, follow_redirects=True)
            response.raise_for_status()
    except httpx.RequestError as e:
        return "❌ 请求博客园失败", {"api_url": url, "error_detail": str(e)}
    except httpx.HTTPStatusError as e:
        return "❌ 博客园返回错误状态码", {"api_url": url, "api_status": e.response.status_code, "error_detail": str(e)}

    try:
        soup = BeautifulSoup(response.text, "html.parser")
        articles = soup.select("article.post-item")

        if not articles:
            debug_info = {"api_url": url, "api_status": 200, "error_detail": "未找到 article.post-item 元素，页面结构可能已更新"}
            return "⚠️ 未抓取到博客园热门文章，页面结构可能已更新", debug_info

        result_lines = ["🔥 **博客园热门文章排行榜** 🔥\n"]

        for idx, article in enumerate(articles[:20], start=1):
            title_tag = article.select_one("a.post-item-title")
            author_tag = article.select_one("a.post-item-author")

            title = title_tag.get_text(strip=True) if title_tag else "未知标题"
            link = title_tag["href"] if title_tag and title_tag.has_attr("href") else "#"
            author = author_tag.get_text(strip=True) if author_tag else "未知作者"

            # 博客园数据在 a.post-meta-item.btn 的 title 属性中，如 title="阅读 569"
            read_count = "0"
            comment_count = "0"
            like_count = "0"

            for meta_a in article.select("a.post-meta-item.btn"):
                title_attr = meta_a.get("title", "")
                text_span = meta_a.select_one("span")
                text_val = text_span.get_text(strip=True) if text_span else "0"
                if "阅读" in title_attr:
                    read_count = text_val
                elif "评论" in title_attr:
                    comment_count = text_val
                elif "推荐" in title_attr:
                    like_count = text_val

            result_lines.append(
                f"{idx}. [{title}]({link})\n"
                f"   👤 {author} | 👁 阅读:{read_count} | 💬 评论:{comment_count} | 👍 推荐:{like_count}"
            )

        return "\n\n".join(result_lines), debug_info

    except Exception as e:
        return f"❌ 解析博客园页面失败：{e}", {"api_url": url, "error_detail": str(e)}

def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        input_data = {}

    task_id = input_data.get("task_id", "")
    params = input_data.get("params", {})
    origin_message = input_data.get("origin_message", "")

    count = params.get("count") or 20
    if not params.get("count"):
        import re
        count_match = re.search(r'(\d+)', origin_message)
        if count_match:
            count = int(count_match.group(1))

    reply, debug_info = get_cnblogs_hot()

    output = {
        "task_id": task_id,
        "reply": reply,
        "files": []
    }
    if debug_info:
        output["_debug"] = debug_info
    print(json.dumps(output, ensure_ascii=False))

if __name__ == "__main__":
    main()