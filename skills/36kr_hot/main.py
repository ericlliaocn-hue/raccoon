import sys
import json
import httpx
from bs4 import BeautifulSoup

def fetch_36kr_hot():
    url = "https://36kr.com/hot-list/catalog"
    headers = {
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    
    with httpx.Client(headers=headers, follow_redirects=True, timeout=15.0) as client:
        response = client.get(url)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        articles = soup.select("div.hot-list-item article.article-item")
        
        if not articles:
            articles = soup.select("div.hot-list-main article.article-item")
            
        if not articles:
            articles = soup.select("a.article-item-title")
            
        results = []
        
        for idx, article in enumerate(articles[:20], 1):
            title_tag = article.select_one("a.title, a.article-item-title, h2 a, div.hot-item-title a")
            if not title_tag:
                continue
                
            title = title_tag.get_text(strip=True)
            link = title_tag.get("href", "")
            
            if link and not link.startswith("http"):
                link = f"https://36kr.com{link}"
                
            heat_tag = article.select_one("span.item-mobile-stat, span.stat-item, div.hot-item-heat span")
            heat = heat_tag.get_text(strip=True) if heat_tag else "N/A"
            
            if title:
                results.append({
                    "rank": idx,
                    "title": title,
                    "heat": heat,
                    "link": link
                })
                
        if not results:
            api_url = "https://gateway.36kr.com/api/mis/nav/home/nav/rank/hot"
            api_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json",
            }
            try:
                api_resp = client.get(api_url, headers=api_headers)
                api_resp.raise_for_status()
                data = api_resp.json()
                items = data.get("data", {}).get("hotRankList", [])
                for idx, item in enumerate(items[:20], 1):
                    title = item.get("title") or item.get("entityName", "")
                    link = item.get("url") or f"https://36kr.com/p/{item.get('entityId', '')}"
                    heat = item.get("hotScore") or item.get("count", "N/A")
                    if title:
                        results.append({
                            "rank": idx,
                            "title": title,
                            "heat": str(heat),
                            "link": link
                        })
            except Exception:
                pass
                
        return results

def main():
    input_data = json.loads(sys.stdin.read())
    task_id = input_data.get("task_id", "")
    params = input_data.get("params", {})
    origin_message = input_data.get("origin_message", "")
    
    try:
        hot_list = fetch_36kr_hot()
        
        if not hot_list:
            reply = "未能获取到36氪热门榜单，请稍后再试。"
        else:
            lines = ["🔥 36氪热门榜单 🔥\n"]
            for item in hot_list:
                lines.append(f"{item['rank']}. {item['title']}")
                if item['heat'] != "N/A":
                    lines.append(f"   热度: {item['heat']}")
                if item['link']:
                    lines.append(f"   链接: {item['link']}")
                lines.append("")
            reply = "\n".join(lines).strip()
            
    except httpx.HTTPStatusError as e:
        reply = f"请求36氪失败，HTTP状态码: {e.response.status_code}，请稍后再试。"
    except httpx.RequestError as e:
        reply = f"网络请求异常: {str(e)}，请稍后再试。"
    except json.JSONDecodeError:
        reply = "解析36氪响应数据失败，请稍后再试。"
    except Exception as e:
        reply = f"获取36氪热门榜单时发生错误: {str(e)}"
        
    output = {
        "task_id": task_id,
        "reply": reply,
        "files": []
    }
    print(json.dumps(output, ensure_ascii=False))

if __name__ == "__main__":
    main()