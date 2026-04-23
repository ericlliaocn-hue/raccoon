import sys
import json
import httpx

def get_juejin_hot():
    url = "https://api.juejin.cn/recommend_api/v1/article/recommend_all_feed"
    payload = {
        "id_type": 2,
        "sort_type": 200,
        "cursor": "0",
        "limit": 20
    }
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    
    with httpx.Client(timeout=15.0) as client:
        response = client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        
    if data.get("err_no") != 0:
        raise Exception(f"掘金API返回错误: {data.get('err_msg', '未知错误')}")
        
    articles = data.get("data", [])
    if not articles:
        raise Exception("未获取到掘金热榜数据")
        
    result = []
    for idx, item in enumerate(articles, 1):
        item_info = item.get("item_info", item)
        article_info = item_info.get("article_info", {})
        title = article_info.get("title", "无标题")
        article_id = article_info.get("article_id", "")
        view_count = article_info.get("view_count", 0)
        digg_count = article_info.get("digg_count", 0)
        comment_count = article_info.get("comment_count", 0)
        hot_index = article_info.get("hot_index", 0)
        
        author_info = item_info.get("author_user_info", {})
        author_name = author_info.get("user_name", "未知作者")
        
        category = item_info.get("category", {})
        category_name = category.get("category_name", "")
        
        tags = item_info.get("tags", [])
        tag_names = ", ".join(t.get("tag_name", "") for t in tags[:3]) if tags else category_name
        
        link = f"https://juejin.cn/post/{article_id}" if article_id else ""
        
        result.append({
            "rank": idx,
            "title": title,
            "author": author_name,
            "category": tag_names or category_name,
            "views": view_count,
            "likes": digg_count,
            "comments": comment_count,
            "hot_index": hot_index,
            "link": link
        })
    
    return result

def format_reply(articles):
    lines = ["🔥 **掘金热榜 Top 20** 🔥\n"]
    for a in articles:
        hot = f" 🔥{a['hot_index']}" if a.get('hot_index') else ""
        line = (
            f"{a['rank']}. **{a['title']}**{hot}\n"
            f"   👤 {a['author']} | 📂 {a['category']} | "
            f"👁 {a['views']} | 👍 {a['likes']} | 💬 {a['comments']}\n"
            f"   🔗 {a['link']}"
        )
        lines.append(line)
    return "\n\n".join(lines)

def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except Exception:
        input_data = {}

    task_id = input_data.get("task_id", "")
    params = input_data.get("params", {})
    origin_message = input_data.get("origin_message", "")

    try:
        limit = params.get("limit")
        if not limit and origin_message:
            import re
            match = re.search(r'(\d+)', origin_message)
            if match:
                limit = int(match.group(1))
        
        articles = get_juejin_hot()
        
        if limit and isinstance(limit, int) and limit > 0:
            articles = articles[:limit]
            
        reply = format_reply(articles)
        debug_info = None
        
    except httpx.HTTPStatusError as e:
        reply = f"❌ 请求掘金API失败，状态码: {e.response.status_code}"
        debug_info = {"api_url": str(e.request.url), "api_status": e.response.status_code, "error_detail": str(e)}
    except httpx.RequestError as e:
        reply = f"❌ 网络请求异常: {str(e)}"
        debug_info = {"api_url": "juejin_api", "api_status": 0, "error_detail": str(e)}
    except Exception as e:
        reply = f"❌ 获取掘金热榜失败: {str(e)}"
        debug_info = {"error_detail": str(e)}

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