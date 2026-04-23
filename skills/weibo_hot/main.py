"""weibo_hot Skill - 获取微博实时热搜排行榜

使用 weibo.com 公开热搜接口，返回热搜词条、热度及链接。
"""
import sys
import json
import httpx


def fetch_weibo_hot():
    """从 weibo.com 热搜接口获取实时热搜数据

    Returns:
        tuple: (hot_list, debug_info)
    """
    url = "https://weibo.com/ajax/side/hotSearch"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://weibo.com/",
    }

    debug = {"api_url": url}
    try:
        with httpx.Client(timeout=15.0, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            debug["api_status"] = response.status_code
            response.raise_for_status()
            raw_text = response.text[:500]
            data = response.json()

        if data.get("ok") != 1:
            debug["api_response"] = raw_text
            debug["error_detail"] = f"接口返回异常: ok={data.get('ok')}"
            raise RuntimeError(f"接口返回异常: ok={data.get('ok')}")

        realtime = data.get("data", {}).get("realtime", [])
        hot_list = []

        for item in realtime:
            word = item.get("word", "") or item.get("note", "")
            if not word:
                continue

            num = item.get("num", 0)
            label = item.get("label_name", "") or item.get("icon_desc", "")
            # 构造搜索链接
            search_url = f"https://s.weibo.com/weibo?q=%23{word}%23"

            hot_list.append({
                "title": word,
                "hot": num,
                "label": label,
                "url": search_url,
            })

        if not hot_list:
            debug["api_response"] = raw_text
            debug["error_detail"] = "API 返回数据为空，realtime 列表解析结果为空"

        return hot_list, debug

    except Exception as e:
        if "error_detail" not in debug:
            debug["error_detail"] = str(e)
        raise RuntimeError(f"获取微博热搜失败: {e}")


def main():
    input_data = json.loads(sys.stdin.read())
    task_id = input_data.get("task_id", "")
    params = input_data.get("params", {})
    origin_message = input_data.get("origin_message", "")

    # 参数读取优先级: params.top_n > origin_message 提取 > 默认10
    top_n = params.get("top_n")
    if top_n is None:
        import re
        match = re.search(r"前(\d+)", origin_message)
        top_n = int(match.group(1)) if match else 10
    else:
        top_n = int(top_n)

    try:
        hot_list, fetch_debug = fetch_weibo_hot()

        if not hot_list:
            reply = "当前未能获取到微博热搜数据，请稍后再试。"
            debug = fetch_debug
        else:
            limited_list = hot_list[:top_n]
            reply_lines = [f"🔥 微博实时热搜 Top {len(limited_list)} 🔥\n"]

            for idx, item in enumerate(limited_list, 1):
                title = item["title"]
                hot = item["hot"]
                label = item.get("label", "")
                url = item["url"]

                # 标签显示
                tag = f" [{label}]" if label else ""

                # 格式化热度
                if hot > 0:
                    if hot >= 100_000_000:
                        hot_str = f"{hot / 100_000_000:.1f}亿"
                    elif hot >= 10_000:
                        hot_str = f"{hot / 10_000:.1f}万"
                    else:
                        hot_str = str(hot)
                    line = f"{idx}. {title}{tag} (热度: {hot_str})"
                else:
                    line = f"{idx}. {title}{tag}"

                if url:
                    line += f"\n   🔗 {url}"

                reply_lines.append(line)

            reply = "\n".join(reply_lines)
            debug = {}

    except Exception as e:
        reply = f"获取微博热搜时发生错误: {e}"
        debug = fetch_debug if 'fetch_debug' in dir() else {"error_detail": str(e)}

    output = {
        "task_id": task_id,
        "reply": reply,
        "files": [],
        "_debug": debug,
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
