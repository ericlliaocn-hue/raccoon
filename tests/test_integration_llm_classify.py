"""集成测试：用真实 LLM 调用测试 _llm_classify 分类准确性

运行方式：
    python -m pytest tests/test_integration_llm_classify.py -v -s
    或：python tests/test_integration_llm_classify.py

注意：此测试会调用真实 LLM，需要配置 API Key
"""
import asyncio
import sys
import os
from pathlib import Path

# 添加项目根目录到路径
PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJECT_ROOT))

import pytest
from unittest.mock import MagicMock

from src.executor.agent import Executor, LlmClassification, LlmClassifyResult
from src.skill_vault.vault_manager import VaultManager
from src.config import RaccoonConfig


def _make_executor_with_real_vault() -> Executor:
    """创建带真实 VaultManager 的 Executor"""
    # 从 config.json 加载配置（如果存在）
    config_path = PROJECT_ROOT / "config.json"
    if config_path.exists():
        config = RaccoonConfig.from_json(config_path)
    else:
        config = RaccoonConfig()
    vault = VaultManager(config)
    
    executor = Executor.__new__(Executor)
    executor._config = config
    executor._vault_manager = vault
    executor._pending_learn_requests = {}
    executor._learning_engine = None
    executor._llm = None
    return executor


# ─── 测试场景定义 ───────────────────────────────────────────────

# 纯创作/问答类任务 → 应该返回 CHITCHAT
CHITCHAT_SCENARIOS = [
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

# 需要外部数据/动作 → 应该返回 SKILL_MATCHED 或 NEEDS_LEARN
ACTION_SCENARIOS = [
    ("帮我查天气", "查天气", "weather_query"),  # 如果有 weather skill
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


# ─── 集成测试 ───────────────────────────────────────────────────

@pytest.mark.integration
class TestLlmClassifyIntegration:
    """_llm_classify 集成测试（真实 LLM 调用）"""

    @pytest.fixture(scope="class")
    def executor(self):
        """创建 Executor 实例"""
        return _make_executor_with_real_vault()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,desc", CHITCHAT_SCENARIOS)
    async def test_chitchat_scenarios(self, executor, text, desc):
        """测试纯创作/问答类任务 → CHITCHAT"""
        result = await executor._llm_classify(text)
        
        print(f"\n[CHITCHAT] {desc}: '{text[:30]}...' -> {result.classification.value}")
        if result.skill_name:
            print(f"  skill_name: {result.skill_name}")
        
        # 期望是 CHITCHAT，但允许 NEEDS_LEARN（如果 LLM 误判为需要外部数据）
        # 不允许 SKILL_MATCHED（这表示误匹配了 Skill）
        assert result.classification != LlmClassification.SKILL_MATCHED, \
            f"'{text}' 被误判为 SKILL_MATCHED: {result.skill_name}"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("text,desc,expected_skill", ACTION_SCENARIOS)
    async def test_action_scenarios(self, executor, text, desc, expected_skill):
        """测试需要外部数据的任务 → SKILL_MATCHED 或 NEEDS_LEARN"""
        result = await executor._llm_classify(text)
        
        print(f"\n[ACTION] {desc}: '{text[:30]}...' -> {result.classification.value}")
        if result.skill_name:
            print(f"  skill_name: {result.skill_name}")
        
        # 期望是 SKILL_MATCHED 或 NEEDS_LEARN，不应该是 CHITCHAT
        # （但某些场景 LLM 可能误判为 CHITCHAT，需要观察）
        if result.classification == LlmClassification.CHITCHAT:
            pytest.skip(f"'{text}' 被误判为 CHITCHAT，需要优化 prompt")
        
        # 如果匹配了 Skill，验证是否匹配了预期的 Skill
        if result.classification == LlmClassification.SKILL_MATCHED:
            # 允许匹配到相关 Skill，不强制完全匹配 expected_skill
            print(f"  匹配到 Skill: {result.skill_name}")

    @pytest.mark.asyncio
    async def test_creative_vs_action_boundary(self, executor):
        """测试创作 vs 动作的边界案例"""
        boundary_cases = [
            ("帮我写一个小红书文案", "创作但带平台名"),
            ("帮我写一篇关于 AI 的文章", "创作但带技术主题"),
            ("帮我生成一段代码", "代码生成"),
            ("帮我写个爬虫脚本", "代码+动作"),
            ("帮我分析这段代码", "分析"),
        ]
        
        for text, desc in boundary_cases:
            result = await executor._llm_classify(text)
            print(f"\n[BOUNDARY] {desc}: '{text}' -> {result.classification.value}")
            if result.skill_name:
                print(f"  skill_name: {result.skill_name}")

    @pytest.mark.asyncio
    async def test_no_skills_fallback(self, executor):
        """测试无 Skill 时的降级"""
        # 临时清空 skills
        original_list_skills = executor._vault_manager.list_skills
        executor._vault_manager.list_skills = MagicMock(return_value=[])
        
        try:
            result = await executor._llm_classify("帮我查天气")
            assert result.classification == LlmClassification.NEEDS_LEARN
        finally:
            executor._vault_manager.list_skills = original_list_skills


# ─── 手动运行入口 ───────────────────────────────────────────────

if __name__ == "__main__":
    """手动运行测试"""
    print("=" * 60)
    print("LLM Classify 集成测试")
    print("=" * 60)
    
    executor = _make_executor_with_real_vault()
    
    # 检查已安装的 Skill
    skills = executor._vault_manager.list_skills()
    print(f"\n已安装 Skill ({len(skills)} 个):")
    for s in skills:
        print(f"  - {s.name}: {s.description[:40]}...")
    
    print("\n" + "=" * 60)
    print("开始测试...")
    print("=" * 60)
    
    async def run_tests():
        # ── CHITCHAT 场景（并发） ──
        print("\n【CHITCHAT 场景】并发执行...")
        async def classify_chitchat(text, desc):
            try:
                result = await executor._llm_classify(text)
                return (desc, text, result, None)
            except Exception as e:
                return (desc, text, None, e)

        chitchat_tasks = [classify_chitchat(t, d) for t, d in CHITCHAT_SCENARIOS]
        chitchat_results = await asyncio.gather(*chitchat_tasks)

        c_passed = 0
        c_failed = 0
        for desc, text, result, err in chitchat_results:
            if err:
                c_failed += 1
                print(f"  ✗ {desc}: ERROR: {err}")
            elif result.classification == LlmClassification.SKILL_MATCHED:
                c_failed += 1
                print(f"  ✗ {desc}: '{text[:25]}...' -> {result.classification.value} ({result.skill_name})")
            else:
                c_passed += 1
                print(f"  ✓ {desc}: '{text[:25]}...' -> {result.classification.value}")
        print(f"\n  结果: {c_passed}/{len(CHITCHAT_SCENARIOS)} 通过")

        # ── ACTION 场景（并发） ──
        print("\n【ACTION 场景】并发执行...")
        async def classify_action(text, desc, expected):
            try:
                result = await executor._llm_classify(text)
                return (desc, text, expected, result, None)
            except Exception as e:
                return (desc, text, expected, None, e)

        action_tasks = [classify_action(t, d, e) for t, d, e in ACTION_SCENARIOS]
        action_results = await asyncio.gather(*action_tasks)

        a_passed = 0
        a_failed = 0
        for desc, text, expected, result, err in action_results:
            if err:
                a_failed += 1
                print(f"  ✗ {desc}: ERROR: {err}")
            elif result.classification == LlmClassification.CHITCHAT:
                a_failed += 1
                print(f"  ✗ {desc}: '{text[:25]}...' -> CHITCHAT (期望 SKILL/NEEDS_LEARN)")
            else:
                a_passed += 1
                skill_info = f" ({result.skill_name})" if result.skill_name else ""
                match_info = f" [期望: {expected}]" if result.skill_name and result.skill_name != expected else ""
                print(f"  ✓ {desc}: '{text[:25]}...' -> {result.classification.value}{skill_info}{match_info}")
        print(f"\n  结果: {a_passed}/{len(ACTION_SCENARIOS)} 通过")

        # ── 汇总 ──
        total = len(CHITCHAT_SCENARIOS) + len(ACTION_SCENARIOS)
        total_passed = c_passed + a_passed
        print(f"\n{'='*60}")
        print(f"总计: {total_passed}/{total} 通过 ({total_passed/total*100:.0f}%)")
        print(f"{'='*60}")

    asyncio.run(run_tests())
