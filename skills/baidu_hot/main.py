import sys
import json
import re
import httpx
from bs4 import BeautifulSoup

def fetch_baidu_hot():
    url = "https://top.baidu.com/board?tab=realtime"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Referer": "https://www.baidu.com/"
    }
    
    response = httpx.get(url, headers=headers, timeout=15, follow_redirects=True)
    response.raise_for_status()
    return response.text, response.status_code

def parse_html(html_content):
    soup = BeautifulSoup(html_content, 'html.parser')
    
    # 尝试提取 SSR 注入的 JSON 数据
    script_tags = soup.find_all('script')
    for script in script_tags:
        if script.string and 'window.__INITIAL_STATE__' in script.string:
            # 匹配 JSON 字符串，通常格式为 window.__INITIAL_STATE__ = {...};
            match = re.search(r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});', script.string, re.DOTALL)
            if match:
                json_str = match.group(1)
                # 替换 JavaScript 的 undefined 为 Python 的 None 以便 JSON 解析
                json_str = re.sub(r'\bundefined\b', 'null', json_str)
                try:
                    data = json.loads(json_str)
                    # 根据百度热搜页面结构，提取数据
                    # 通常路径为 data -> content -> cards -> [0] -> content 或类似结构
                    if 'content' in data:
                        content = data['content']
                        if isinstance(content, dict) and 'cards' in content:
                            cards = content['cards']
                            if isinstance(cards, list) and len(cards) > 0:
                                card = cards[0]
                                if isinstance(card, dict) and 'content' in card:
                                    items = card['content']
                                    if isinstance(items, list):
                                        return items
                except json.JSONDecodeError:
                    pass
                break
    
    # 降级方案：直接解析 HTML DOM
    items = []
    category_items = soup.select('.category-wrap_iQLoo')
    for idx, item in enumerate(category_items):
        try:
            title_tag = item.select_one('.c-single-text-ellipsis')
            title = title_tag.get_text(strip=True) if title_tag else "未知标题"
            
            hot_tag = item.select_one('.hot-index_1Bl1a')
            hot_index = hot_tag.get_text(strip=True) if hot_tag else "-"
            
            desc_tag = item.select_one('.hot-desc_1m_jR')
            desc = desc_tag.get_text(strip=True) if desc_tag else ""
            
            link_tag = item.select_one('a[href]')
            link = link_tag['href'] if link_tag and link_tag.has_attr('href') else ""
            
            items.append({
                "title": title,
                "hot_index": hot_index,
                "desc": desc,
                "url": link
            })
        except Exception:
            continue
            
    return items

def main():
    input_data = {}
    try:
        input_str = sys.stdin.read()
        if input_str:
            input_data = json.loads(input_str)
    except Exception:
        pass

    task_id = input_data.get("task_id", "")
    params = input_data.get("params", {})
    origin_message = input_data.get("origin_message", "")

    # 参数读取优先级: params.xxx > origin_message 提取
    limit = params.get("limit")
    if not limit and origin_message:
        limit_match = re.search(r'(\d+)', origin_message)
        if limit_match:
            limit = int(limit_match.group(1))
    if not limit:
        limit = 20

    debug_info = {}
    try:
        html_content, status_code = fetch_baidu_hot()
        
        if status_code != 200:
            debug_info = {
                "api_url": "https://top.baidu.com/board?tab=realtime",
                "api_status": status_code,
                "api_response": html_content[:500],
                "error_detail": f"API 请求失败，状态码: {status_code}"
            }
            result = {
                "task_id": task_id,
                "reply": f"获取百度热搜失败，状态码: {status_code}",
                "files": [],
                "_debug": debug_info
            }
            print(json.dumps(result, ensure_ascii=False))
            return

        items = parse_html(html_content)
        
        if not items:
            debug_info = {
                "api_url": "https://top.baidu.com/board?tab=realtime",
                "api_status": status_code,
                "api_response": html_content[:500],
                "error_detail": "页面结构变化或解析失败，未能提取到热搜数据"
            }
            result = {
                "task_id": task_id,
                "reply": "未能解析出百度热搜数据，页面结构可能已更新。",
                "files": [],
                "_debug": debug_info
            }
            print(json.dumps(result, ensure_ascii=False))
            return

        # 截取指定数量
        items = items[:int(limit)]
        
        # 格式化输出
        reply_lines = ["🔥 百度热搜排行榜 🔥\n"]
        for idx, item in enumerate(items, 1):
            # 兼容 JSON 提取和 DOM 提取的字段名
            title = item.get("word") or item.get("title", "未知")
            hot_index = item.get("hotScore") or item.get("hot_index", "-")
            desc = item.get("desc") or item.get("desc", "")
            url = item.get("url") or item.get("rawUrl", "")
            
            # 清理标题前可能残留的序号
            title = re.sub(r'^\d+\.\s*', '', title)
            
            line = f"{idx}. {title}"
            if str(hot_index) != "-":
                line += f"  (热度: {hot_index})"
            if desc:
                line += f"\n   {desc[:50]}{'...' if len(desc)>50 else ''}"
            if url:
                line += f"\n   🔗 {url}"
            reply_lines.append(line)
            
        reply = "\n".join(reply_lines)
        
        result = {
            "task_id": task_id,
            "reply": reply,
            "files": [],
            "_debug": {}
        }
        print(json.dumps(result, ensure_ascii=False))

    except Exception as e:
        debug_info = {
            "api_url": "https://top.baidu.com/board?tab=realtime",
            "api_status": -1,
            "api_response": "",
            "error_detail": str(e)
        }
        result = {
            "task_id": task_id,
            "reply": f"获取百度热搜时发生错误: {str(e)}",
            "files": [],
            "_debug": debug_info
        }
        print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()