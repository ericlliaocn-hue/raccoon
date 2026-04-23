# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.0] - 2025-04-23

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
