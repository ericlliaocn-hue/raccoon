import sys
import json
import re
import httpx


def get_stdin_input():
    try:
        return json.loads(sys.stdin.read())
    except Exception:
        return {"task_id": "unknown", "params": {}, "origin_message": ""}


def extract_params(data):
    params = data.get("params", {})
    origin_message = data.get("origin_message", "")

    category = params.get("category")
    top_n = params.get("top_n")

    if not category:
        match = re.search(r'(美食|穿搭|美妆|旅游|数码|影视|职场|情感|健身|家居)', origin_message)
        category = match.group(1) if match else "推荐"

    if not top_n:
        match = re.search(r'(?:top|前)\s*(\d+)', origin_message, re.IGNORECASE)
        top_n = int(match.group(1)) if match else 20
    else:
        top_n = int(top_n)

    return category, top_n


def fetch_xiaohongshu_trending(category, top_n):
    """
    调用小红书搜索/榜单API或通过 web_automate 抓取数据。
    当前使用模拟数据演示完整流程，生产环境需对接真实接口。
    """
    try:
        # TODO: 对接小红书真实 API 或通过 Raccoon 的 web_automate Skill 抓取
        # with httpx.Client(timeout=10) as client:
        #     resp = client.get("https://www.xiaohongshu.com/api/v1/trending",
        #                       params={"category": category, "limit": top_n})
        #     resp.raise_for_status()
        #     data = resp.json()

        # 模拟数据
        mock_data = []
        for i in range(1, top_n + 1):
            mock_data.append({
                "rank": i,
                "title": f"【{category}】今日爆款分享：超实用的第{i}名技巧与心得，赶紧收藏！",
                "url": f"https://www.xiaohongshu.com/explore/{100000 + i}",
                "likes": 10000 - i * 400 + i * 15,
                "comments": 500 - i * 20 + i * 3,
            })
        return mock_data
    except httpx.HTTPError as e:
        raise Exception(f"网络请求失败: {str(e)}")
    except json.JSONDecodeError:
        raise Exception("解析小红书响应数据失败")


def format_report(trending_data, category):
    report_lines = [
        f"🔥 小红书每日爆文日报 - {category}频道",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]

    for item in trending_data:
        report_lines.append(
            f"{item['rank']}. {item['title']}\n"
            f"   👍 {item['likes']}  💬 {item['comments']}\n"
            f"   🔗 {item['url']}"
        )

    report_lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    report_lines.append("数据来源：小红书 | 仅供学习参考")

    return "\n".join(report_lines)


def main():
    data = get_stdin_input()
    task_id = data.get("task_id", "unknown")

    try:
        category, top_n = extract_params(data)
        trending_data = fetch_xiaohongshu_trending(category, top_n)
        reply = format_report(trending_data, category)

        output = {
            "task_id": task_id,
            "reply": reply,
            "files": [],
        }
    except Exception as e:
        output = {
            "task_id": task_id,
            "reply": f"生成小红书日报失败：{str(e)}，请检查网络或参数设置。",
            "files": [],
        }

    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
