# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.2] - 2025-04-24

### 🎨 UI: 技能商城重构 + 收藏功能

- **商城弹窗重构为左右布局**：左侧 180px 分类 Tab 竖排（全部/系统自带/装机必装/效率/资讯/创作/开发/浏览器/自学习），右侧技能列表内容区，固定尺寸 960×680px
- **搜索框移至左侧 sidebar 顶部**：搜索仅匹配技能名称，Tab 过滤（未启用/已启用）始终生效
- **新增技能收藏功能**：每个技能卡片增加 ★ star 按钮，点击收藏/取消收藏，状态持久化到 metadata.json
- **新增 API**：`POST /skills/{name}/star` 切换收藏状态
- **SkillMetadata 新增 `starred` 字段**：`starred: bool = False`
- **静态资源缓存控制**：CSS/JS 引用加版本号参数，避免浏览器缓存旧文件
- **Config 迁移**：旧版单模型 LLM 配置自动迁移到 `llm_models` 列表

## [0.3.1] - 2025-04-23

### 📦 Release v0.3.1 — LLM Classifier Integration Test + Dev Conventions

- **Integration test passed (43/43 — 100%)**: `_llm_classify` verified across CHITCHAT (9), ACTION (25), and BOUNDARY (9) scenarios.
- **New Skill: `baidu_hot`**: 百度热搜排行榜.
- **Dev conventions documented** (`docs/dev-conventions.md`): Testing standards for major changes — concurrent asyncio.gather, three scenario categories, 100% pass rate required.
- **Test files**: Added `test_integration_llm_classify.py` and `quick_classify_test.py`.

## [0.3.0] - 2025-04-23

### 🏷️ LLM Single-Pass Classifier — "方案C"

This release replaces the two-stage hardcoded+LLM skill matching with a **single LLM classifier** (`_llm_classify`), making Raccoon's message routing simpler, more accurate, and more extensible.

> **Before**: `_is_pure_creative_task` (hardcoded signals) → `_match_skill` (LLM) → fallback to NEEDS_LEARN
> **Now**: `_llm_classify` (one LLM call) → CHITCHAT / SKILL:xxx / NEEDS_LEARN

#### What Changed

- **Deleted `_PURE_CREATIVE_SIGNALS` and `_is_pure_creative_task`**: Removed 27 lines of hardcoded creative task signals. No more safety net — LLM handles all classification.

- **New `_llm_classify` method**: Single LLM call that simultaneously determines:
  1. Whether the message needs external data/action (vs. pure text generation)
  2. Which installed Skill matches (if any)
  3. Whether a new Skill should be learned (NEEDS_LEARN)

- **New `_build_skill_catalog` helper**: Constructs a concise Skill summary (name, description, trigger words, tags) for LLM context.

- **New `_verify_skill_name` helper**: Validates LLM-returned skill names with fuzzy matching (supports aliases and partial matches).

- **Deprecated `_match_skill`**: Now delegates to `_llm_classify` for backward compatibility.

- **Simplified `classify_llm_message`**: Replaced three-step flow with `_might_need_action` → `_llm_classify`.

- **LLM failure fallback**: Changed from NEEDS_LEARN to CHITCHAT (safer — don't trigger learning on LLM errors).

#### Integration Test Results (43/43 passed — 100%)

| Category | Count | Pass | Notes |
|----------|-------|------|-------|
| CHITCHAT | 9 | 9 | 写诗/翻译/润色/总结/闲聊等 |
| ACTION | 25 | 25 | 24 SKILL_MATCHED + 1 NEEDS_LEARN (weather) |
| BOUNDARY | 9 | 9 | "写小红书文案"→CHITCHAT, "订机票"→NEEDS_LEARN |

### 🧠 Data Acquisition Strategy — "API First, Crawl Later"

This release adds a **data acquisition strategy layer** to LearningEngine, making Raccoon's self-learning smarter about *how* to get data — not just *what* code to generate.

> **Before**: LLM guesses an approach (might pick HTML scraping when a JSON API exists).
> **Now**: LLM lists candidate data sources → engine probes each one → picks the best (API > HTML > CDP).

#### What Changed

- **Data source candidate list in `_analyze_need`**: Instead of asking LLM for a single `approach`, the prompt now asks for a `data_sources` array with priority-ordered candidates (API, HTML, CDP). Backward compatible — if LLM still returns `approach`, it's auto-converted.

- **New `_decide_data_strategy` method**: Probes each candidate data source in priority order:
  1. Sends a lightweight HTTP request to each candidate URL
  2. Checks if response is structured JSON (API), HTML page, or unreachable
  3. For API candidates: verifies JSON structure, detects error codes, checks auth requirements
  4. For HTML candidates: checks content length (too short = error page)
  5. Picks the highest-priority usable source; falls back gracefully if all fail

- **New `_probe_source` method**: Single-source probe with smart type detection:
  - API type: expects JSON, rejects HTML responses (marks as "API not available, let HTML candidate handle it")
  - HTML type: expects HTML content, validates meaningful content length
  - CDP type: always marked usable (browser rendering can't be validated via HTTP)

- **Experience records data strategy**: When a Skill is successfully learned, the experience written to MemCore now includes `data_strategy` and `chosen_source`, so future similar requests can skip the probe phase.

- **Updated `learn()` flow**: New step 3 (data strategy decision) inserted between analysis and dependency installation. Step numbering: 1→experience, 2→analyze, 3→**strategy**, 4→install, 5→probe, 6→generate, 7→register, 8→execute+repair, 9→result.

### 🔧 Improvements

- **Approach replan with strategy awareness**: When code repair fails and `_replan_approach` kicks in, the new plan also benefits from the data strategy layer — it will try a different data source type (e.g., switch from API to HTML, or HTML to CDP).

### 🛠 New Skills

- **`bilibili_hot`**: Bilibili (B站) hot video ranking. Uses B站 API with WBI signature.
- **`weibo_hot`**: Weibo (微博) hot search ranking. Uses Weibo mobile API.
- **`csdn_hot`**: CSDN blog hot ranking. Uses CSDN phoenix API (structured JSON).
- **`juejin_hot`**: 掘金 hot article ranking. Uses 掘金 recommend API.
- **`cnblogs_hot`**: 博客园 top views ranking. HTML scraping (no public API available — a perfect example of the API-first strategy in action).
- **`36kr_hot`**: 36氪 hot article ranking. Uses 36氪 API.
- **`baidu_hot`**: 百度热搜排行榜. Uses 百度热搜 API.

### 🎨 Web UI

- **Major UI overhaul**: Redesigned chat interface with modern card-based layout, improved message bubbles, and better mobile responsiveness.
- **Skill execution feedback**: Real-time status indicators for Skill execution and learning flow.

---

## [0.2.0] - 2025-04-22

### 🧠 L3 Self-Learning — The Core Update

This release introduces the **self-healing Skill generation loop**, making Raccoon the first AI Agent framework that can truly *learn* from conversations — not just execute pre-built tools.

> **Before**: Skill generation fails → user sees an error → manual fix required.
> **Now**: Skill generation fails → auto-install missing deps → auto-repair code → retry up to 3 times → success.

#### What Changed

- **Auto-repair loop in LearningEngine**: When a generated Skill fails to execute, Raccoon now automatically:
  1. Analyzes the error (e.g., `ModuleNotFoundError`)
  2. Tries `pip install` for missing packages
  3. If install fails, replaces `requests` with `httpx` (project dependency, API-compatible)
  4. Falls back to LLM-powered code repair for other errors
  5. Retries execution — up to 3 attempts

- **Smart dependency resolution**: `_IMPORT_TO_PIP` mapping table covers common Python packages (`bs4→beautifulsoup4`, `PIL→Pillow`, `yaml→PyYAML`, etc.), so `ModuleNotFoundError` triggers the right `pip install` command automatically.

- **Skill generation prompt overhaul**: Removed restrictive "don't use requests" constraints. Instead, recommends `httpx` (already installed, modern, async-capable) and trusts the auto-repair loop to handle any dependency issues.

### 🤖 LLM Intelligence Layer

- **Message classification**: New `classify_llm_message()` routes incoming messages into three categories:
  - `CHITCHAT` → direct LLM stream response
  - `SKILL_MATCHED` → execute matched Skill
  - `NEEDS_LEARN` → trigger LearningEngine

- **Three-stage LLM handling**: `_handle_llm()` implements a structured pipeline:
  1. Classify intent
  2. Route to appropriate handler
  3. Stream response with source attribution (`source: llm` / `source: skill`)

- **LLM fallback chain**: `SparkLLMClient` now supports multi-model fallback — if the primary model fails, it automatically tries the next configured model.

### 🛠 New Skills

- **`ai_daily_report`**: Fetches and summarizes AI news from HackerNews, TechCrunch, 机器之心, 36氪. Supports custom sources and configurable item count. Uses `httpx` + `feedparser` + `BeautifulSoup`.

- **`xiaohongshu_daily_report`**: Generates daily trending posts report from Xiaohongshu (小红书). Supports category filtering and top-N configuration. (Currently uses mock data; real API integration pending.)

### 🔧 Improvements

- **HTTP adapter**: SSE streaming now properly handles all three message types (chitchat, skill execution, learning flow) with consistent `source` field in responses.
- **CLI adapter**: Added learn-confirm interaction flow — when Raccoon detects a new skill is needed, it asks the user before generating.
- **Type system**: Added `MessageType` enum for message classification.

### 🧪 Testing

- Added `test_llm_fallback_learning.py` — 28 tests covering:
  - Skill matching (LLM-based and rule-based)
  - Learn confirm/reject flow
  - Schedule creation from learn requests
  - Message classification (chitchat / action / needs-learn)
  - Three-stage LLM handling
  - Chat-with-tools fallback

---

## [0.1.0] - 2025-04-21

### Initial Release

- Event-driven architecture with EventBus
- Three-tier skill system (L1 built-in / L2 marketplace / L3 self-learning)
- Skill sandbox isolation (subprocess per Skill)
- Built-in Flow orchestration (multi-step Skill workflows)
- CDP browser automation via Playwright
- Cron-based scheduler with auto-retry
- Multi-channel notifications (Bark, ServerChan, Webhook)
- MemCore persistent memory system
- HTTP + CLI adapters
- `web_automate` built-in Skill
