"""快速集成测试：并发调用 LLM 测试 _llm_classify"""
import asyncio, sys, os
from pathlib import Path

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJECT_ROOT))

from src.executor.agent import Executor, LlmClassification, LlmClassifyResult
from src.skill_vault.vault_manager import VaultManager
from src.config import RaccoonConfig

def make_executor():
    config_path = PROJECT_ROOT / "config.json"
    config = RaccoonConfig.from_json(config_path) if config_path.exists() else RaccoonConfig()
    vault = VaultManager(config)
    ex = Executor.__new__(Executor)
    ex._config = config
    ex._vault_manager = vault
    ex._pending_learn_requests = {}
    ex._learning_engine = None
    ex._llm = None
    return ex

CHITCHAT = [
    ("帮我写一首关于春天的诗", "写诗"),
    ("讲个笑话", "讲笑话"),
    ("翻译一下这段英文", "翻译"),
    ("润色这段话", "润色"),
    ("总结一下这篇文章", "总结"),
    ("帮我写个故事", "写故事"),
    ("解释一下量子力学", "解释概念"),
    ("Python 怎么排序列表", "编程问答"),
    ("今天心情怎么样", "闲聊"),
]

ACTION = [
    ("帮我查天气", "查天气", "weather_query"),
    ("打开浏览器访问百度", "打开网页", "web_automate"),
    ("监控网站变化", "监控网站", "change_detector"),
    ("生成一张猫的图片", "生成图片", "image_gen"),
    ("查看系统信息", "系统信息", "system_info"),
    ("执行 ls 命令", "执行命令", "shell_exec"),
    ("截图", "截图", "screenshot"),
    ("获取微博热搜", "热搜", "weibo_hot"),
    ("获取 36kr 热门", "资讯", "36kr_hot"),
    ("获取小红书日报", "小红书", "xiaohongshu_daily_report"),
    ("获取 AI 日报", "AI日报", "ai_daily_report"),
    ("获取 B站热门", "B站", "bilibili_hot"),
    ("获取掘金热门", "掘金", "juejin_hot"),
    ("获取 CSDN 热门", "CSDN", "csdn_hot"),
    ("获取百度热搜", "百度", "baidu_hot"),
    ("获取博客园热门", "博客园", "cnblogs_hot"),
    ("读取文件", "读文件", "file_read"),
    ("搜索文件", "搜文件", "file_search"),
    ("分析文件", "分析文件", "file_analyze"),
    ("批量处理文件", "批量文件", "file_batch"),
    ("控制应用", "控制应用", "app_control"),
    ("操作剪贴板", "剪贴板", "clipboard"),
    ("查看日志", "看日志", "log_watcher"),
    ("Git 帮助", "Git", "git_helper"),
    ("网页浏览", "浏览网页", "web_browse"),
]

async def main():
    ex = make_executor()
    skills = ex._vault_manager.list_skills()
    print(f"已安装 {len(skills)} 个 Skill")

    # 并发所有请求
    async def test_one(text, desc, expected_class, expected_skill=None):
        try:
            r = await ex._llm_classify(text)
            ok = r.classification == expected_class
            if expected_skill and r.classification == LlmClassification.SKILL_MATCHED:
                ok = r.skill_name == expected_skill
            return (desc, text[:25], r.classification.value, r.skill_name, ok, None)
        except Exception as e:
            return (desc, text[:25], None, None, False, str(e))

    coros = [test_one(t, d, LlmClassification.CHITCHAT) for t, d in CHITCHAT]
    coros += [test_one(t, d, LlmClassification.SKILL_MATCHED, s) for t, d, s in ACTION]

    print(f"\n并发发送 {len(coros)} 个请求...")
    results = await asyncio.gather(*coros)

    # 分组输出
    print("\n" + "="*60)
    print("【CHITCHAT 场景】")
    c_pass = 0
    for desc, txt, cls, skill, ok, err in results[:len(CHITCHAT)]:
        if err:
            print(f"  ✗ {desc}: ERROR {err}")
        elif cls != "chitchat":
            print(f"  ✗ {desc}: '{txt}...' -> {cls} ({skill})")
        else:
            c_pass += 1
            print(f"  ✓ {desc}: '{txt}...' -> {cls}")
    print(f"  结果: {c_pass}/{len(CHITCHAT)}")

    print("\n【ACTION 场景】")
    a_pass = 0
    a_skill_match = 0
    for desc, txt, cls, skill, ok, err in results[len(CHITCHAT):]:
        if err:
            print(f"  ✗ {desc}: ERROR {err}")
        elif cls == "chitchat":
            print(f"  ✗ {desc}: '{txt}...' -> CHITCHAT (应为 SKILL/NEEDS_LEARN)")
        else:
            a_pass += 1
            if cls == "skill_matched":
                a_skill_match += 1
            skill_info = f" ({skill})" if skill else ""
            print(f"  ✓ {desc}: '{txt}...' -> {cls}{skill_info}")
    print(f"  结果: {a_pass}/{len(ACTION)} (其中 SKILL_MATCHED: {a_skill_match})")

    total = len(CHITCHAT) + len(ACTION)
    total_pass = c_pass + a_pass
    print(f"\n{'='*60}")
    print(f"总计: {total_pass}/{total} 通过 ({total_pass/total*100:.0f}%)")
    print(f"{'='*60}")

asyncio.run(main())
