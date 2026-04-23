import sys
import json
import httpx

def main():
    try:
        input_data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        print(json.dumps({"task_id": "", "reply": "输入数据解析失败", "files": []}))
        return

    task_id = input_data.get("task_id", "")
    params = input_data.get("params", {})
    origin_message = input_data.get("origin_message", "")

    # 参数读取优先级: params.xxx > origin_message 提取
    count = params.get("count")
    if count is None:
        import re
        match = re.search(r'(\d+)', origin_message)
        count = int(match.group(1)) if match else 10
    else:
        count = int(count)
    
    # 限制最大数量，防止请求过大
    count = min(max(1, count), 50)

    url = "https://api.bilibili.com/x/web-interface/popular"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }
    query_params = {
        "ps": count,
        "pn": 1
    }

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.get(url, headers=headers, params=query_params)
            response.raise_for_status()
            data = response.json()

        if data.get("code") != 0:
            reply = f"获取B站热门视频失败：{data.get('message', '未知错误')}"
        else:
            video_list = data.get("data", {}).get("list", [])
            if not video_list:
                reply = "当前没有获取到B站热门视频数据。"
            else:
                reply_lines = [f"🔥 B站热门视频 Top {len(video_list)}：\n"]
                for idx, video in enumerate(video_list, 1):
                    title = video.get("title", "未知标题")
                    owner = video.get("owner", {}).get("name", "未知UP主")
                    play = video.get("stat", {}).get("view", 0)
                    danmaku = video.get("stat", {}).get("danmaku", 0)
                    bvid = video.get("bvid", "")
                    link = f"https://www.bilibili.com/video/{bvid}" if bvid else "链接缺失"
                    
                    # 格式化播放量和弹幕量
                    play_str = f"{play // 10000}万" if play >= 10000 else str(play)
                    danmaku_str = f"{danmaku // 10000}万" if danmaku >= 10000 else str(danmaku)
                    
                    reply_lines.append(
                        f"{idx}. {title}\n"
                        f"   UP主: {owner} | 播放: {play_str} | 弹幕: {danmaku_str}\n"
                        f"   链接: {link}"
                    )
                
                reply = "\n".join(reply_lines)

    except httpx.HTTPStatusError as e:
        reply = f"请求B站API失败，状态码: {e.response.status_code}"
    except httpx.RequestError:
        reply = "网络请求异常，请检查网络连接或稍后重试。"
    except Exception as e:
        reply = f"处理B站热门数据时发生错误: {str(e)}"

    result = {
        "task_id": task_id,
        "reply": reply,
        "files": []
    }
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()