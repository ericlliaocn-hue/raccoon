# Raccoon Skill 标准写法规范

> 版本：1.0.0 | 更新：2026-04-22

## 一、背景与定位

Raccoon Skill 是 Raccoon Agent 的能力扩展单元。与主流方案的对比如下：

| 方案 | 定位 | 参数定义 | 执行方式 | 多步骤编排 |
|------|------|----------|----------|------------|
| **OpenAI Function Calling** | LLM 调用工具 | JSON Schema (`parameters`) | 远程 API 调用 | 无（单次调用） |
| **MCP** | LLM 连接外部系统 | JSON Schema (`inputSchema`/`outputSchema`) | Client-Server 协议 | 无（单次调用） |
| **LangChain Tool** | Agent 调用工具 | Python 函数签名 + docstring | 进程内函数调用 | 无（由 Agent 自行编排） |
| **Coze 插件** | Bot 能力扩展 | OpenAPI Schema | 云端托管运行 | 由 Workflow 编排 |
| **Raccoon Skill** | Agent 能力扩展 | `schema_def` (JSON Schema 子集) | subprocess 沙箱隔离 | 内置 Flow 编排 |

**Raccoon Skill 的差异化**：

1. **subprocess 沙箱隔离**：Skill 以子进程运行，崩溃不影响主进程，天然安全
2. **内置 Flow 编排**：Skill 自带多步骤流程定义（user_choice / llm / user_input / skill_exec），不需要外部 Workflow 引擎
3. **stdin/stdout JSON 通信**：简单、语言无关、可调试
4. **state 传递**：Flow 步骤间通过 session.state 自动传递数据

## 二、Skill 目录结构

```
skills/<skill_name>/
├── metadata.json      # 必需 — Skill 元数据、参数定义、Flow 编排
├── main.py            # 必需 — Skill 入口，读取 stdin JSON，输出 stdout JSON
├── <helper>.py        # 可选 — 辅助模块，main.py 可直接 import
├── requirements.txt   # 可选 — 额外依赖（安装时 pip install）
└── README.md          # 可选 — Skill 说明文档
```

## 三、metadata.json 规范

### 3.1 基础字段

```json
{
  "name": "skill_name",           // 必需 — 唯一标识，小写+下划线，对应目录名
  "version": "1.0.0",            // 必需 — 语义化版本号
  "description": "一句话描述",     // 必需 — 用于路由匹配和展示
  "author": "raccoon",           // 可选 — 作者标识（市场展示）
  "dependencies": ["web_automate"], // 可选 — 依赖的其他 Skill（安装时检查）

  // ── 路由匹配 ──
  "trigger_words": ["触发词1", "触发词2"],  // 必需 — 用户消息匹配关键词，用于路由
  "aliases": ["别名1", "别名2"],            // 可选 — @命令别名，如 @生图
  "intent_tags": ["tag1", "tag2"],          // 必需 — 语义标签，用于 LLM 意图识别兜底

  // ── 安全控制 ──
  "risk_level": "low",            // 必需 — low / medium / high
  "requires_approval": false,     // 必需 — 是否需要用户确认后执行
  "timeout_seconds": 30,          // 必需 — 单次执行超时（秒）

  // ── 运行时 ──
  "entry": "main.py",             // 必需 — 入口文件
  "permissions": [],              // 必需 — 声明所需权限：network / subprocess / filesystem / browser_automation / screen_capture
  "interactive": false            // 可选 — 是否支持多轮交互（默认 false）
}
```

### 3.2 参数定义（schema_def）

使用 JSON Schema 子集定义 Skill 接受的参数。**这是 Skill 与引擎之间的契约。**

```json
{
  "schema_def": {
    "type": "object",
    "properties": {
      "prompt": {
        "type": "string",
        "required": true,
        "description": "图片描述"
      },
      "ratio": {
        "type": "string",
        "default": "16:9",
        "enum": ["9:16", "16:9", "1:1", "4:3", "21:9"],
        "description": "画幅比例"
      },
      "action": {
        "type": "string",
        "required": false,
        "description": "操作类型",
        "enum": ["generate", "login", "fill_phone", "fill_code"]
      }
    }
  }
}
```

**与 OpenAI/MCP 的对比**：

| 特性 | OpenAI Function Calling | MCP | Raccoon schema_def |
|------|------------------------|-----|---------------------|
| 参数类型 | JSON Schema 完整支持 | JSON Schema 完整支持 | JSON Schema 子集（string/number/integer/boolean/array） |
| 必填声明 | `required` 数组 | `required` 数组 | 每个属性内 `required: true` |
| 枚举约束 | `enum` 数组 | `enum` 数组 | `enum` 数组 |
| 默认值 | 无（由代码处理） | 无（由代码处理） | `default` 字段 |
| 输出定义 | 无 | `outputSchema` | 无（stdout JSON 自由格式） |
| strict 模式 | `strict: true` | 无 | 无 |

### 3.3 Flow 编排（flow）

Raccoon Skill 独有的多步骤编排能力，在 `metadata.json` 中声明式定义：

```json
{
  "flow": {
    "steps": [
      {
        "name": "选择画面风格",
        "type": "user_choice",
        "prompt": "请选择你想要的画面风格：",
        "options": ["写实电影", "二次元动漫", "水彩插画"]
      },
      {
        "name": "润色提示词",
        "type": "llm",
        "prompt": "根据用户描述和风格，润色生成专业AI绘图提示词",
        "llm_system_prompt": "你是AI绘图提示词专家。直接输出提示词，不要解释。"
      },
      {
        "name": "确认提示词",
        "type": "user_input",
        "prompt": "你可以修改提示词，或直接发送确认："
      },
      {
        "name": "生成图片",
        "type": "skill_exec",
        "skill_action": "generate",
        "background": false
      }
    ],
    "on_complete": "🎨 图片生成完成！",
    "on_cancel": "流程已取消"
  }
}
```

**步骤类型**：

| 类型 | 作用 | state 写入 key | 说明 |
|------|------|----------------|------|
| `user_choice` | 展示选项，等用户选择 | `step_{id}_choice` + `{id}` | 用户选择结果写入 state |
| `user_input` | 等待用户自由输入 | `step_{id}_input` + `{id}` | 用户输入内容写入 state |
| `llm` | 调用 LLM 生成内容 | `step_{id}_llm_result` + `{id}` | LLM 输出写入 state |
| `skill_exec` | 执行 Skill 代码 | 由 Skill 返回值决定 | `skill_action` 指定 action |

**Flow 与 state 的关系**：每个步骤的结果自动写入 `session.state`，后续步骤可通过 state 读取。`skill_exec` 步骤执行时，引擎会自动从 state 中提取 LLM 步骤的输出作为 `prompt` 注入 `skill_params`，同时提取 `user_choice` / `user_input` 步骤的结果，以步骤名称（小写+下划线）为 key 注入 `skill_params`。

## 四、通信协议

### 4.1 输入（stdin JSON）

引擎通过 stdin 传入 JSON，Skill 读取并解析：

```json
{
  "task_id": "uuid-string",
  "conversation_id": "conv-uuid",
  "user_id": "user-id 或 flow_engine",
  "origin_message": "用户原始消息 或 flow_step:{step_name}",
  "params": {
    "action": "generate",
    "prompt": "一只猫,写实电影风格...",
    "state": { ... },
    "step_id": "a3b4c5d6",
    "step_name": "生成图片"
  },
  "state": { ... }
}
```

**关键字段说明**：

| 字段 | 来源 | 说明 |
|------|------|------|
| `origin_message` | 直接调用时为用户原始消息；Flow 模式下为 `flow_step:{step_name}` | **不应依赖此字段提取参数** |
| `params` | 引擎构建的参数字典 | **应优先从此字段读取参数** |
| `params.prompt` | Flow 模式下由引擎自动从 state 中提取 LLM 结果注入 | 核心提示词 |
| `params.action` | Flow 步骤的 `skill_action` 字段 | 操作类型 |
| `params.state` | 完整的 session.state | 包含所有步骤的中间结果 |
| `state` | `params.state` 的提升副本 | 方便直接访问 |

### 4.2 输出（stdout JSON）

Skill 通过 stdout 输出 JSON 结果：

```json
{
  "task_id": "uuid-string",
  "reply": "执行结果描述",
  "files": [
    {
      "type": "image",
      "path": "/path/to/output.png",
      "name": "生成图片.png"
    }
  ],
  "image_url": "https://...",
  "need_login": false,
  "login_step": null
}
```

**输出字段规范**：

| 字段 | 必需 | 说明 |
|------|------|------|
| `task_id` | 是 | 原样返回输入的 task_id |
| `reply` | 是 | 执行结果文本，展示给用户 |
| `files` | 否 | 文件列表，每个元素包含 type/path/name |
| `image_url` | 否 | 图片 URL（如有） |
| `need_login` | 否 | 是否需要登录（交互式 Skill） |
| `login_step` | 否 | 当前登录阶段 |

## 五、main.py 标准模板

### 5.1 最简 Skill（无 Flow）

```python
"""skill_name — 一句话描述

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params" }
  输出: { "task_id", "reply": "...", "files": [] }
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    params = data.get("params", {})
    origin = data.get("origin_message", "")

    # 从 params 读取参数（优先），origin_message 兜底
    # ⚠️ 不要只依赖 origin_message，Flow 模式下它可能是 "flow_step:xxx"
    prompt = params.get("prompt") or origin.strip()

    if not prompt:
        result = {"task_id": task_id, "reply": "请提供必要参数", "files": []}
        print(json.dumps(result, ensure_ascii=False))
        return

    # 执行核心逻辑
    output = do_something(prompt)

    result = {
        "task_id": task_id,
        "reply": f"执行完成：{output}",
        "files": [],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

### 5.2 带 Flow 的 Skill

```python
"""skill_name — 一句话描述

通信协议（subprocess stdin/stdout JSON）：
  输入: { "task_id", "conversation_id", "user_id", "origin_message", "params", "state" }
  输出: { "task_id", "reply": "...", "files": [] }

params 可选字段：
  action: str     — 操作类型（对应 flow.steps[].skill_action）
  prompt: str     — 核心输入（Flow 模式下由引擎自动从 LLM 步骤结果注入）
  state: dict     — Flow session 状态（包含所有步骤中间结果）
  step_id: str    — 当前步骤 ID
  step_name: str  — 当前步骤名称
"""

from __future__ import annotations

import json
import sys


def main() -> None:
    data = json.loads(sys.stdin.read())
    task_id = data.get("task_id", "")
    params = data.get("params", {})
    origin = data.get("origin_message", "")
    state = data.get("state", {})

    action = params.get("action", "")

    if action == "generate":
        _handle_generate(task_id, params, origin, state)
    elif action == "other_action":
        _handle_other(task_id, params, origin, state)
    else:
        _handle_default(task_id, params, origin, state)


def _handle_generate(task_id: str, params: dict, origin: str, state: dict) -> None:
    # ✅ 正确：优先从 params 读取，params 由引擎保证注入
    prompt = params.get("prompt")

    # ✅ 正确：从 state 读取上游步骤结果作为兜底
    if not prompt:
        for key, value in state.items():
            if key.endswith("_llm_result") and isinstance(value, str) and value.strip():
                prompt = value
                break

    # ⚠️ 最后兜底：origin_message（注意 Flow 模式下可能是 "flow_step:xxx"）
    if not prompt:
        prompt = _extract_prompt(origin)

    if not prompt:
        result = {"task_id": task_id, "reply": "请提供图片描述", "files": []}
        print(json.dumps(result, ensure_ascii=False))
        return

    # 从 state 读取用户选择（user_choice 步骤的结果）
    style = params.get("style")
    if not style:
        for key, value in state.items():
            if key.endswith("_choice") and isinstance(value, str):
                style = value
                break

    # 执行核心逻辑
    output = do_generate(prompt, style)

    result = {
        "task_id": task_id,
        "reply": f"生成完成",
        "files": [{"type": "image", "path": output, "name": "result.png"}],
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
```

## 六、关键规则与踩坑总结

### 6.1 参数读取优先级

```
params.xxx  >  state 中查找  >  origin_message 提取
```

| 优先级 | 来源 | 可靠性 | 说明 |
|--------|------|--------|------|
| 1 | `params.get("prompt")` | ✅ 最可靠 | Flow 模式下由引擎自动注入 |
| 2 | `state` 中遍历查找 | ✅ 可靠 | 兜底方案，key 格式为 `step_{id}_llm_result` / `step_{id}_choice` |
| 3 | `_extract_prompt(origin)` | ⚠️ 不可靠 | Flow 模式下 origin 是 `"flow_step:xxx"` |

### 6.2 Flow 模式下的 origin_message

**直接调用**时：`origin_message = "@生图 一只猫"` → 可提取参数
**Flow 模式**下：`origin_message = "flow_step:生成图片"` → **不可提取参数**

因此，**永远不要只依赖 origin_message 提取参数**，必须优先从 `params` 读取。

### 6.3 state 的 key 命名规则

| 步骤类型 | state key 格式 | 示例 |
|----------|---------------|------|
| user_choice | `step_{uuid}_choice` + `{uuid}` | `step_a3b4c5d6_choice` = "写实电影" |
| user_input | `step_{uuid}_input` + `{uuid}` | `step_a3b4c5d6_input` = "用户输入的文本" |
| llm | `step_{uuid}_llm_result` + `{uuid}` | `step_a3b4c5d6_llm_result` = "润色后的提示词" |
| skill_exec | 由 Skill 返回值决定 | — |

**注意**：`{uuid}` 是 step_id（UUID 前8位），不是步骤序号。Skill 不应硬编码 step_id，应通过后缀匹配查找。

### 6.4 引擎对 skill_params 的自动注入

`flow_engine.py` 在执行 `skill_exec` 步骤时，会自动：

1. 从 state 中提取 LLM 步骤的输出 → 注入 `skill_params["prompt"]`
2. 传入完整 `state` → `skill_params["state"]`
3. 传入步骤信息 → `skill_params["step_id"]` / `skill_params["step_name"]`
4. 传入 action → `skill_params["action"]`（对应 flow 中的 `skill_action`）

**TODO**：目前 `user_choice` 的结果已自动注入 `skill_params`（key 为步骤名称小写+下划线，如 `选择画面风格` → `选择画面风格`），Skill 可通过 `params.get("选择画面风格")` 读取。

### 6.5 多轮交互（登录流程）

交互式 Skill 可返回 `need_login: true` + `login_step`，引擎会接管多轮交互：

```python
result = {
    "task_id": task_id,
    "reply": "请输入手机号登录",
    "files": files,
    "need_login": True,
    "login_step": "input_phone",
    "pending_prompt": prompt,       # 保存原始 prompt，登录后恢复
    "pending_params": {...},        # 保存其他参数
}
```

## 七、与主流方案的差异总结

### 7.1 相比 OpenAI Function Calling

| 维度 | OpenAI | Raccoon |
|------|--------|---------|
| 定位 | LLM 调用外部 API 的接口描述 | Agent 的完整能力单元 |
| 执行 | 远程 HTTP API | 本地 subprocess |
| 编排 | 无 | 内置 Flow |
| 状态 | 无状态 | session.state 跨步骤传递 |
| 安全 | API 级别 | risk_level + requires_approval + permissions |

### 7.2 相比 MCP

| 维度 | MCP | Raccoon |
|------|-----|---------|
| 定位 | LLM 与外部系统的连接协议 | Agent 能力扩展 |
| 架构 | Client-Server | 单进程 subprocess |
| 输出定义 | `outputSchema` | 无（自由 JSON） |
| 编排 | 无 | 内置 Flow |
| 发现 | 动态注册 | metadata.json 静态声明 |

### 7.3 相比 Coze 插件

| 维度 | Coze | Raccoon |
|------|------|---------|
| 运行环境 | 云端托管 | 本地沙箱 |
| 编排 | 独立 Workflow 引擎 | Skill 内置 Flow |
| 登录流程 | 平台托管 | Skill 自行管理 |
| 浏览器操控 | 无 | CDP 集成 |

## 八、checklist：写一个新 Skill 前的检查项

- [ ] `metadata.json` 的 `name` 与目录名一致
- [ ] `trigger_words` 和 `aliases` 覆盖用户常见说法
- [ ] `schema_def` 声明了所有 `params` 中会使用的字段
- [ ] `risk_level` 和 `requires_approval` 正确评估
- [ ] `permissions` 声明了所有需要的权限
- [ ] `main.py` 优先从 `params` 读取参数，不依赖 `origin_message`
- [ ] 如有 Flow，`skill_exec` 步骤的 `skill_action` 与 `main.py` 中的 action 分支对应
- [ ] 如需从 state 读取上游结果，使用后缀匹配（`_llm_result` / `_choice` / `_input`），不硬编码 step_id
- [ ] 输出 JSON 包含 `task_id` 和 `reply`
- [ ] 文件输出通过 `files` 数组返回，包含 `type`/`path`/`name`
- [ ] 超时设置合理（网络操作 ≥ 60s，本地操作 ≤ 30s）
