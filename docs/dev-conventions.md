# Raccoon 开发规范

## 核心原则

1. **每次大改动必须先做真实集成测试**，通过后才算完成
2. **不自动 git commit/push**，等用户明确要求

## 大改动集成测试规范

### 什么时候需要

- 架构变更（如路由、分类器重构）
- 核心逻辑修改（如 LLM 调用、Skill 匹配、工作流编排）
- 新增重要功能模块

### 测试风格

```python
# 1. 独立小脚本，放在 tests/ 下
# 2. 用 asyncio.gather 并发调用真实接口（加速）
# 3. 分批执行避免 IDE 超时
# 4. 覆盖三类场景
# 5. 输出 ✓/✗ + 分类结果 + 期望对比
# 6. 100% 通过率才算通过
```

### 三类必测场景

| 场景类型 | 说明 | 示例 |
|----------|------|------|
| **CHITCHAT** | 闲聊/创作，不应匹配 Skill | 写诗、翻译、润色、总结 |
| **ACTION** | 需要外部数据/动作，应匹配 Skill 或 NEEDS_LEARN | 查热搜、截图、读文件 |
| **BOUNDARY** | 容易混淆的边界案例 | "帮我写个小红书文案"→CHITCHAT（不是小红书日报） |

### 测试脚本模板

```python
import asyncio, sys, os
from pathlib import Path
PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, str(PROJECT_ROOT))
from src.executor.agent import Executor, LlmClassification
from src.skill_vault.vault_manager import VaultManager
from src.config import RaccoonConfig

config_path = PROJECT_ROOT / "config.json"
config = RaccoonConfig.from_json(config_path) if config_path.exists() else RaccoonConfig()
vault = VaultManager(config)
ex = Executor.__new__(Executor)
ex._config = config; ex._vault_manager = vault
ex._pending_learn_requests = {}; ex._learning_engine = None; ex._llm = None

CASES = [
    # (输入, 描述, 期望分类, 期望skill名或None)
    ("帮我写一首诗", "写诗", "chitchat", None),
    ("获取微博热搜", "热搜", "skill_matched", "weibo_hot"),
    ("帮我订机票", "无对应skill", "needs_learn", None),
]

async def main():
    async def test(text, desc, expect_cls, expect_skill):
        r = await ex._llm_classify(text)
        return (desc, r.classification.value, r.skill_name, expect_cls, expect_skill)

    results = await asyncio.gather(*[test(t,d,c,s) for t,d,c,s in CASES])
    passed = 0
    for desc, cls, skill, expect_cls, expect_skill in results:
        ok = cls == expect_cls and (not expect_skill or skill == expect_skill)
        if ok: passed += 1
        mark = "✓" if ok else "✗"
        print(f"  {mark} {desc}: {cls} ({skill}) [期望: {expect_cls}]")
    print(f"\n结果: {passed}/{len(CASES)}")

asyncio.run(main())
```

### 执行方式

- IDE 内分批执行（每批 ~15 个场景，避免超时）
- 终端可直接 `python tests/_mini_test.py` 运行完整批次
- 测试通过后清理临时脚本

## Git 规范

- **不自动 commit/push**，等用户明确说"提交"或"推送"
- commit message 格式：`feat/fix/refactor: 简要描述`
