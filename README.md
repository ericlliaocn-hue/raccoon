# 🦝 Raccoon — 会自己学技能的 AI Agent

Raccoon 是一个本地优先、事件驱动的 AI Agent 框架。核心理念：**不是你给它装工具它去用，而是它自己会长出能力来。**

> 💬 你说一句话 → 没有对应 Skill → LLM 自动生成代码 → 先写入 staging 验证 → 审批通过后安装执行 → 下次直接复用

## 为什么选 Raccoon

| | 传统 Agent | Raccoon |
|---|---|---|
| 新需求 | 等人开发插件 | 对话中自动学会 |
| 执行报错 | 停下来等你修 | 先验证、再审批、再安装执行 |
| 能力增长 | 装多少用多少 | 越用越强，经验自动积累 |

## 特性

- **🧠 L3 对话自学**：没有的 Skill？说一句话，Raccoon 先生成到 staging，再做验证、审批、安装、执行，学习过程有完整运行记录
- **🔄 自修复闭环**：生成后的 Skill 先过 metadata/编译/协议检查，失败时自动修复，再决定是否进入安装审批
- **📡 事件驱动架构**：EventBus 解耦所有模块，消息即指令
- **🏗 三层能力体系**：L1 基础技能 → L2 市场扩展 → L3 对话自学
- **🔒 Skill 沙箱隔离**：每个 Skill 以 subprocess 运行，崩溃不影响主进程
- **🔀 内置 Flow 编排**：Skill 自带多步骤流程（用户选择 / LLM 推理 / 交互输入），无需外部 Workflow 引擎
- **🌐 CDP 浏览器操控**：原生 Playwright 集成，可操控网页完成复杂任务
- **⏰ 定时调度**：Cron 表达式驱动的定时任务，支持自动重试
- **🔔 多通道通知**：系统通知、Bark、Webhook 等多渠道推送
- **💾 记忆系统**：MemCore 持久化经验，对话自学能力持续积累

## 架构

```
┌─────────────────────────────────────────────────┐
│                   Adapters                       │
│           (CLI / HTTP / Webhook)                 │
├─────────────────────────────────────────────────┤
│                  Gateway                         │
│          (认证 / 路由 / 限流)                      │
├─────────────────────────────────────────────────┤
│  Router ──→ Brain ──→ Supervisor ──→ Executor   │
│  (意图)    (推理)     (审批)        (执行)        │
├─────────────────────────────────────────────────┤
│  EventBus  │  MemCore  │  Scheduler  │  Notifier │
│  (消息)    │  (记忆)    │  (定时)      │  (通知)    │
├─────────────────────────────────────────────────┤
│              SkillVault (技能市场)                │
│   L1 基础技能  │  L2 市场安装  │  L3 对话自学     │
└─────────────────────────────────────────────────┘
```

## 快速开始

### 环境要求

- Python >= 3.11
- pip（或 uv / poetry 等包管理器）
- （可选）Playwright 浏览器：用于 web_automate 等 Skill

### 安装

```bash
# 1. 克隆仓库
git clone https://github.com/ericlliaocn-hue/reccoon.git
cd reccoon

# 2. 安装依赖
pip install -e .

# 3. 安装 Playwright 浏览器（可选，web_automate Skill 需要）
playwright install chromium

# 4. 复制配置模板并填写 API Key
cp config.example.json config.json
# 编辑 config.json，填入你的 LLM API Key
```

### 配置

Raccoon 支持三种配置方式，优先级从高到低：

1. **环境变量**（最高优先级）：前缀 `RACCOON_`，如 `RACCOON_LLM_API_KEY`
2. **config.json**：项目根目录，从 `config.example.json` 复制修改
3. **默认值**：代码内置默认值

关键配置项：

| 配置项 | 说明 | 环境变量 |
|--------|------|----------|
| `llm_provider` | LLM 提供商：`spark` / `openai` / `mock` | `RACCOON_LLM_PROVIDER` |
| `llm_api_key` | LLM API Key（必填） | `RACCOON_LLM_API_KEY` |
| `llm_model` | 模型名称 | `RACCOON_LLM_MODEL` |
| `llm_base_url` | API 端点 | `RACCOON_LLM_BASE_URL` |
| `http_port` | HTTP 服务端口，默认 8900 | `RACCOON_HTTP_PORT` |
| `auto_approve` | 自动审批，默认 false | `RACCOON_AUTO_APPROVE` |
| `learning_require_approval` | 自学习安装前是否要求审批，默认 true | `RACCOON_LEARNING_REQUIRE_APPROVAL` |
| `learning_staging_dir` | 自学习 staging 目录 | `RACCOON_LEARNING_STAGING_DIR` |

### 支持的 LLM

#### 讯飞星火（默认）

```json
{
  "llm_provider": "spark",
  "llm_api_key": "你的APIKey",
  "llm_model": "generalv3.5",
  "llm_base_url": "https://spark-api-open.xf-yun.com/v1"
}
```

#### OpenAI 兼容

```json
{
  "llm_provider": "openai",
  "llm_api_key": "sk-xxx",
  "llm_model": "gpt-4o",
  "llm_base_url": "https://api.openai.com/v1"
}
```

#### Mock（测试用）

```json
{
  "llm_provider": "mock"
}
```

### 运行

```bash
# CLI 交互模式
raccoon

# HTTP Web UI 模式
raccoon --http

# 后台启动
raccoon start
raccoon start --cli     # 后台 CLI 模式

# 查看状态
raccoon status

# 诊断检查
raccoon doctor

# 核心场景基准（发布门禁）
raccoon benchmark core
raccoon benchmark core --json

# 查看日志
raccoon logs
raccoon logs -f         # 持续跟踪
raccoon logs --audit    # 审计日志
```

### 0.5.x 发布节奏与基线

- 路线图：`docs/roadmap.md`（`0.5.3 -> 0.5.8` 周更）
- 核心场景离线样本包：`benchmarks/core/offline_sample_pack.json`
- 核心场景真实任务样本包：`benchmarks/core/real_task_sample_pack.json`
- 发布报告模板：`benchmarks/core/baseline_report_template.md`

### 定时任务

```bash
# 列出定时任务
raccoon schedule list

# 添加定时任务
raccoon schedule add --name "每日简报" --cron "0 8 * * *" --message "生成今日简报"

# 禁用/启用
raccoon schedule toggle --id <task-id>

# 删除
raccoon schedule remove --id <task-id>
```

### Skill 管理

```bash
# 列出已安装的 Skill
raccoon skills list

# 从 Git 仓库安装 Skill
raccoon skills install <git-url>

# 查看 Skill 详情
raccoon skills info <skill-name>

# 卸载 Skill
raccoon skills uninstall <skill-name>
```

## 内置 Skill

| Skill | 说明 | 权限 |
|-------|------|------|
| `web_automate` | CDP 浏览器自动化，操控网页完成任务 | `browser_automation`, `network` |
| `ai_daily_report` | 每日 AI 日报，聚合 HackerNews/TechCrunch/机器之心/36氪 资讯 | `httpx`, `feedparser`, `beautifulsoup4` |
| `xiaohongshu_daily_report` | 小红书每日爆文榜单（开发中） | `httpx`, `beautifulsoup4` |

更多 Skill 可通过 `raccoon skills install <git-url>` 安装，或**直接对话让 Raccoon 自己学会**。

## 开发自定义 Skill

详见 [Skill 标准规范](docs/skill-standard.md)。

快速上手：

```
skills/my_skill/
├── metadata.json      # 必需 — 元数据、参数定义、Flow 编排
├── main.py            # 必需 — 入口，读取 stdin JSON，输出 stdout JSON
├── requirements.txt   # 可选 — 额外依赖
└── README.md          # 可选 — 说明文档
```

最简 `main.py`：

```python
import json, sys

def main():
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    params = data.get("params", {})
    prompt = params.get("prompt") or data.get("origin_message", "").strip()

    result = {"task_id": task_id, "reply": f"处理完成: {prompt}", "files": []}
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__":
    main()
```

## 项目结构

```
raccoon/
├── raccoon.py              # CLI 入口
├── config.example.json     # 配置模板
├── pyproject.toml          # 项目依赖
├── src/
│   ├── adapters/           # 适配层（CLI / HTTP / Webhook）
│   ├── brain/              # 推理引擎 + 学习引擎
│   ├── eventbus/           # 事件总线
│   ├── executor/           # 执行引擎（Skill 沙箱 + Flow 编排）
│   ├── gateway/            # 网关（认证 / 路由）
│   ├── memcore/            # 记忆系统
│   ├── notifier/           # 通知系统
│   ├── router/             # 意图路由
│   ├── scheduler/          # 定时调度
│   ├── skill_vault/        # 技能市场
│   ├── supervisor/         # 审批引擎
│   ├── workflow/           # 工作流引擎
│   ├── config.py           # 配置管理
│   ├── llm.py              # LLM 调用封装
│   └── types.py            # 类型定义
├── skills/                 # Skill 目录
│   └── web_automate/       # 浏览器自动化 Skill
├── docs/
│   └── skill-standard.md   # Skill 开发规范
└── tests/                  # 测试
```

## 部署注意事项

### 安全

- **`config.json` 不入库**：已通过 `.gitignore` 排除，包含 API Key 等敏感信息
- **环境变量优先**：生产环境建议通过环境变量注入敏感配置，而非写入文件
- **Skill 沙箱**：所有 Skill 以 subprocess 运行，受内存/超时限制
- **审批机制**：默认 `auto_approve: false`；高风险操作和自学习安装都会进入审批

### LLM 配置

- 必须配置至少一个 LLM 提供商的 API Key，否则 Agent 无法推理
- 推荐使用讯飞星火（国内延迟低）或 OpenAI 兼容接口
- `mock` 模式仅用于开发测试，不会调用真实 LLM

### Playwright

- `web_automate` Skill 依赖 Playwright，需单独安装浏览器：
  ```bash
  playwright install chromium
  ```
- 首次安装可能需要下载 ~150MB 的浏览器二进制文件
- 服务器无头环境需安装系统依赖：`playwright install-deps`

### 数据目录

- `data/`：运行时数据（MemCore 数据库等），首次运行自动创建
- `output/`：Skill 产出文件（图片、视频等）
- `tasks/`：任务执行记录
- `logs/`：运行日志和审计日志
- 以上目录均已在 `.gitignore` 中排除

### 端口

- 默认 HTTP 端口：8900
- 可通过 `config.json` 的 `http_port` 或 `--port` 参数修改

### 系统依赖

- Python >= 3.11（使用了 `list[dict]` 等新语法）
- 推荐操作系统：macOS / Linux（Windows 需 WSL）
- 内存建议 >= 512MB（Playwright 浏览器会额外占用内存）

## 开发

```bash
# 安装开发依赖
pip install -e ".[dev]"

# 运行测试
pytest

# 代码检查
ruff check src/ skills/

# 格式化
ruff format src/ skills/
```

## License

MIT
