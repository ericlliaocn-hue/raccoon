# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.5] - 2026-04-25

### 🎯 执行成功率冲刺（第一批）

> 目标：优先压降“该追问却直接执行”和“失败后不可运营”的问题，把执行稳定性再抬一档。

- **执行前置拦截补强**：`Executor` 对 `change_detector` 缺少 URL/SKU、`web_automate/web_browse` 占位地址等输入统一追问阻断，减少无效执行与假成功
- **失败码治理收敛**：新增统一 `failure_code` 修复建议中心，LearningEngine / benchmark / doctor 复用同一套建议，避免提示漂移
- **诊断可运营化**：`raccoon doctor` 输出失败码 TopN 及对应修复建议，支持按失败集中度快速排障
- **基准报告增强**：核心场景 benchmark 的 `failure_code_topn` 增加可执行修复建议字段（hint），周报可直接落动作
- **场景识别误判修复**：修复 `remote_exec` 识别中 `oa` 子串误判（如 `~/Downloads` 被误判为登录场景）的问题，新增边界匹配逻辑
- **工作日调度识别修复**：`_try_create_schedule()` 增加 `工作日/每个工作日` 信号词，修复“每个工作日 09:30 …”被错误追问的问题

## [0.5.4] - 2026-04-25

### 📊 双指标体系 + 场景 Playbook 编排

> 目标：解决"追问也算成功"的统计偏差，6 场景走稳定复用路径。

- **LearningRun 双指标落库**：新增 `decision_success`、`execution_attempted`、`execution_success`、`handling_outcome`、`clarification_reason`
- **核心场景稳定编排 Playbook**：新增 `CoreScenarioPlaybook`，6 场景优先走稳定复用路径，缺参统一追问
- **/learning/runs 筛选增强**：新增 `execution_success`、`handling_outcome`、`clarification_reason` 过滤参数
- **基准双指标升级**：`/benchmarks/core-scenarios/latest` 与 `raccoon benchmark core` 输出决策成功率、执行成功率、浏览器链路执行成功率、卡死率、handling_outcome 分布和 failure_code TopN
- **发布门禁更新**：decision_success >=95%、execution_success >=85%、browser_chain >=90%、stuck_rate <=1%
- **doctor 执行质量告警**：新增 execution_success 趋势下滑、场景退化、失败码集中检测
- **样本包补充**：新增 `clarification_pack.json`、`execution_pack.json`

## [0.5.3] - 2026-04-25

### 🚦 版本先行落地（0.5.3 起）+ 📊 核心场景基线固化

> 目标：从本版本开始，先升版本再开发；把 6 周节奏、样本包和门禁报告基线固定下来。

- **版本先行规则落库**：固定版本同步清单（`pyproject.toml`、`src/__init__.py`、静态资源版本参数、页面版本文案、`CHANGELOG.md`）
- **doctor 新增版本一致性检查**：启动诊断时会核对 package/runtime/static/changelog 的版本是否一致，避免“代码变了但版本没同步”
- **版本一致性单测**：新增 `tests/test_version_sync.py`，在测试阶段提前拦截版本不一致
- **核心场景基线样本包**：新增 `benchmarks/core/offline_sample_pack.json` 与 `benchmarks/core/real_task_sample_pack.json`
- **发布报告模板**：新增 `benchmarks/core/baseline_report_template.md`，统一记录场景命中率、failure_code TopN、候选池变化
- **路线图重排**：`docs/roadmap.md` 更新为 `0.5.3 -> 0.5.8` 周更节奏，发布门槛保持不变（首轮命中率 `>=70%`、最终成功率 `>=85%`、浏览器关键链路 `>=90%`、卡死率 `<=1%`）
- **学习命中率补丁**：LearningEngine 新增学习前 preflight，优先复用 Scheduler、shell_exec、内容热榜 Skill 包，不再默认造新 Skill
- **缺参数追问**：价格监控缺商品链接/SKU、登录表单缺真实 URL/字段、远程执行缺具体命令时，标记为 clarification_required 并停止生成
- **假入口拦截**：阻止 `example.com`、`oa.internal`、`{SKU_ID}` 等占位/假地址进入生成、验证和安装
- **生成代码清洗**：统一提取 Python 代码块，避免 LLM 把 Markdown 围栏写进 `main.py` 导致编译失败
- **shell_exec 保护**：直接命中 `shell_exec` 但缺具体命令时先追问，不进入审批/执行链路
- **LearningRun 双指标落库**：新增 `decision_success`、`execution_attempted`、`execution_success`、`handling_outcome`、`clarification_reason`，解决“追问也算执行成功”的统计偏差
- **核心场景稳定编排 Playbook**：新增 `CoreScenarioPlaybook`，6 场景优先走稳定复用路径（scheduler/content bundle/web_automate/shell_exec），缺参统一追问
- **/learning/runs 筛选增强**：新增 `execution_success`、`handling_outcome`、`clarification_reason` 过滤参数
- **基准双指标升级**：`/benchmarks/core-scenarios/latest` 与 `raccoon benchmark core` 输出决策成功率、执行成功率、浏览器链路执行成功率、卡死率、`handling_outcome` 分布和 `failure_code TopN`
- **发布门禁更新**：发布 pass 改为 `decision_success >=95%`、`execution_success >=85%`、`browser_chain_execution_success >=90%`、`stuck_rate <=1%`
- **doctor 执行质量告警**：新增 execution_success 趋势下滑、场景退化、失败码集中检测
- **基准样本包补充**：新增 `benchmarks/core/clarification_pack.json`、`benchmarks/core/execution_pack.json`，并更新周报模板为双指标版

## [0.5.2] - 2026-04-25

### 🔧 场景补强收口 + 浏览器断点续跑

> 目标：候选池接口落地、浏览器长链路断点续跑、doctor 检查补齐。

- **候选池接口**：新增 `GET /learning/candidates`，按失败聚类输出新增 Skill 候选（含 sample request、建议 skill 名）
- **浏览器断点续跑**：`_resolve_resume_plan()` 解析"继续"指令，从上次失败步骤恢复执行；`execute()` 支持 `start_index` 参数跳过已完成步骤
- **doctor 候选池检查**：`raccoon doctor` 新增 Skill 候选池待处理检查
- **候选池接口鉴权**：`/learning/candidates` 纳入受保护端点
- **测试补齐**：新增断点续跑单元测试、候选池接口集成测试

## [0.5.1] - 2026-04-24

### 🎯 场景驱动补强（0.5.x）

> 目标：围绕 6 个核心场景把“该中 Skill 就中、该学习就学、学出来要稳定”做成可观测可验收。

- **LearningRun 场景指标补全**：新增 `scenario_id`、`first_pass`、`final_success`、`failure_code`、`quality_score`、`artifacts` 字段并落库；`/learning/runs` 支持 `scenario_id/failure_code/first_pass` 过滤
- **质量门升级**：学习验证阶段增加场景质量评分（字段完整性、来源可解释性、数据新鲜度）；低于阈值会被 `quality_gate_failed` 拦下，不进入安装
- **失败码驱动修复**：学习失败从“纯字符串重试”升级为 `failure_code` 分类（parse/auth/dependency/data_hollow/timeout 等）+ 定向修复提示
- **失败聚类触发新增 Skill 候选**：同类失败 7 天内达到阈值（`>=5` 且跨 `>=2` 会话）会在 run artifacts 标记“可新增通用 Skill”候选
- **候选池接口**：新增 `GET /learning/candidates`，按失败聚类输出新增 Skill 候选（含 sample request、建议 skill 名）
- **核心场景看板接口**：新增 `GET /benchmarks/core-scenarios/latest`，按 6 场景返回首轮命中率、最终成功率、平均修复次数、质量分和发布门禁
- **CLI 基准入口**：新增 `raccoon benchmark core`（支持 `--json`），可作为发布前门禁命令
- **学习前复用更稳**：候选召回接入意图短语归一化；中低置信候选增加二次确认，不再直接误学
- **浏览器链路稳定性补丁**：`web_automate` 新增可靠性执行层（`wait-visible -> wait-stable -> action -> assert` + 重试 + 超时原因）；失败自动采集截图/DOM/请求摘要并写入 artifacts；动作返回 `step_checkpoint` 支持长链路恢复
- **doctor 运营检查补充**：新增 LearningRun 卡死、失败码集中度、浏览器会话回收异常检查

## [0.5.0] - 2026-04-24

### 🧠 自学习闭环补强

> 目标：把“没有 Skill → 生成 → 验证 → 审批 → 安装 → 执行 → 记忆复用”真正接成一条稳定链路。

- **LearningRun 持久化**：新增 `learning_runs` 运行记录，保存学习请求、分析结果、staging 路径、验证结果、审批状态、执行结果和失败原因；新增 `GET /learning/runs`、`GET /learning/runs/{run_id}`
- **staging 验证闸门**：LearningEngine 生成的新 Skill 先写入 `data/learning/staging/{run_id}/...`，先做 metadata/schema 校验、Python 编译检查、风险扫描和协议 smoke test，通过后才进入安装审批
- **审批后恢复执行**：学习安装审批通过后，不需要用户再发一条消息，`ApprovalEngine` 事件会自动恢复安装和执行；新增 `LEARNING_STARTED`、`LEARNING_VALIDATED`、`LEARNING_APPROVAL_REQUIRED`、`LEARNING_INSTALLED`、`LEARNING_FAILED` 事件
- **依赖安装收紧**：验证阶段不再静默把依赖装进运行环境；审批通过后才安装依赖并做严格复验，失败不会污染正式 Skill 目录
- **MemCore 真接线**：HTTP/CLI 默认初始化 `MemCoreWriter` 并注入 LearningEngine；修复学习经验读写 API，用结构化 JSON 写入经验并在相似需求下优先复用已有 Skill
- **学习前先复用已有 Skill**：用户确认学习前，Executor 会再做一次本地 Skill 候选召回；高置信度命中时直接复用已有 Skill，减少重复造轮子
- **工程收口**：CLI/daemon 默认 host 调整为 `127.0.0.1`；`raccoon doctor` 新增 MemCore、Skill metadata、learning staging 和监听地址检查

## [0.4.1] - 2026-04-24

### 🛡️ LLM Fallback 本地候选兜底

> 目标：LLM 超时/失败/误判 CHITCHAT 时，动作请求不再漏成闲聊。

- **本地 Skill 候选召回**：LLM 不可用或返回 CHITCHAT 时，先用本地 Skill 元数据（trigger_words/aliases/intent_tags）召回候选；有明确候选则命中 Skill，无候选但明显是动作则进入 `NEEDS_LEARN`
- **`SkillCandidate` 数据结构**：新增 `SkillCandidate` dataclass，记录 skill_name/confidence/matched_terms
- **强动作信号词区分**：`_STRONG_ACTION_SIGNALS` 与弱信号 `_ACTION_SIGNALS` 分离，"帮我"不再单独作为无 LLM 时的动作证据
- **触发词补强**：`app_control` 补充"控制应用"、"应用控制"触发词
- **基准测试**：新增真实 Skill 目录基准测试，覆盖 25+ 动作场景 + 8 闲聊场景，LLM 全挂时验证不漏判
- **`.gitignore` 补充**：排除 `workflows.migrated/`、`.playwright-cli/`、`schedules.migrated/`、`before-market.yaml`

## [0.4.0] - 2026-04-24

### 🔒 安全闭环 + ⏰ 调度闭环 + 🧭 路由归一 + 🧪 兼容性修复（非 Docker）

> 目标：把 v0.3.x 已写好的能力真正接入默认运行路径，避免“写了但没接线”。范围排除 Docker。

#### Module 1: 审批接线（默认仅高风险人工审批）

- **Task 模型增强**：新增 `Task.context: dict`、`TaskStatus.PENDING_APPROVAL`、`EventType.APPROVAL_RESOLVED`
- **Executor/FlowEngine 统一刹车**：普通 Skill 与交互式 Flow 步骤执行前统一走 `ApprovalEngine.review()`；pending 时挂起，批准后自动恢复执行，拒绝/超时转失败并发 `TASK_FAILED`
- **默认策略调整**：`auto_approve` 默认改为 `false`；low/medium 自动通过，high 或 `requires_approval=true` 才进入人工审批

#### Module 2: 调度结果闭环（run_id 回写 + 重试新 run_id）

- **调度上下文贯穿**：`schedule_id/run_id/schedule_name/cron/is_retry` 写入 `Task.context`，并在 `TASK_COMPLETED/TASK_FAILED` payload 中带出
- **HTTP 层回写**：监听 `TASK_COMPLETED/TASK_FAILED`，自动调用 `scheduler.record_run_result()` 更新 `schedule_runs`
- **重试 run_id 修复**：每次重试先创建新的 `run_id`，保证每次尝试对应一条独立 run 记录

#### Module 3: HTTP 安全 + 上传下载安全

- **全局可选认证**：新增 `http_auth_token`（为空不启用）；受保护端点要求 `Authorization: Bearer <token>`，SSE `/events` 同时支持 `?token=`
- **默认只监听本地**：`http_host` 默认改为 `127.0.0.1`
- **路径穿越修复**：上传/下载统一使用安全文件名 + `resolve()/relative_to()` 目录边界校验，直接拒绝 `..`、路径分隔符等

#### Module 4: 路由与 LLM 分流收敛

- **Adapter 不再持有 LLM 分流规则**：`/chat/stream` 的 LLM fallback 分类、Skill 匹配和学习确认统一收敛到 `Executor.decide_llm_stream()`；adapter 只负责渲染 SSE
- **LLM fallback 本地候选兜底**：LLM 超时、连接失败、返回不可解析或误判 `CHITCHAT` 时，会先用本地 Skill 元数据召回候选；有明确候选则命中 Skill，无候选但明显是动作则进入 `NEEDS_LEARN`，避免动作请求漏成闲聊
- **低置信度保护**：`LlmClassifyResult` 支持 `confidence`；低于阈值（默认 `0.7`）不直接执行 Skill，改为提示用户确认/学习
- **触发词补强**：补充常用 trigger/alias（文件整理、读取文件、浏览器截图等）减少 LLM 抢路由

#### Module 5: 浏览器与意图分类

- **BrowserSessionManager 接入默认路径**：内置 `web_automate` 不再默认走一次性 subprocess，`SkillRunner` 在父进程内调用 `run_browser_skill()`，通过 `BrowserSessionManager.acquire()/release()` 复用浏览器实例
- **CDP 复用增强**：`web_automate` 在 CDP 模式启动时传入 `reuse_url`，优先复用同域已有 tab/登录态；无 URL 的截图/读取不再把当前页导航到 `about:blank`
- **SessionManager 导入修复**：修复 `skills/web_automate/session_manager.py` 包导入路径，startup 启动清理循环，shutdown 清理不再因为导入失败报警
- **意图分类可配置**：新增 `intent_classifier_type`（默认 `keyword`，设为 `model` 才启用 `ModelIntentClassifier`）

#### Module 6: 兼容性、健康检查与版本一致性

- **Python 3.11 编译通过**：修复 `learning_engine.py` 中不兼容语法
- **FastAPI lifespan**：HTTP app 启停从 deprecated `on_event` 切换到 lifespan，避免 CI/运行日志带已知弃用警告
- **/health 修复**：改用真实 SQLite 连接检查，避免访问不存在的内部属性导致 degraded
- **依赖补齐**：补齐内置 Skill/通知通道常用依赖（`beautifulsoup4`、`feedparser`、`aiosmtplib`、`requests`）
- **版本统一**：统一 `pyproject.toml`、`src/__init__.py`、FastAPI version、静态资源 cache 参数

#### Breaking Changes

- 默认 `http_host=127.0.0.1`：需要局域网/公网访问时必须显式配置 `0.0.0.0` 并建议同时设置 `http_auth_token`
- 默认 `auto_approve=false`：高风险/显式审批 Skill 会进入 `PENDING_APPROVAL`，需审批后才能执行

## [0.3.5] - 2026-04-24

### 🌐 CDP 浏览器增强 + 🐳 部署标准化 + 📦 Skill 生态增强

> 目标：浏览器操控复杂页面，`docker run` 一键启动，Skill 升级更安全。

#### Module 1: CDP 浏览器增强

- **#1 复杂页面交互**：`Actions` 新增 7 种交互方法：`select_option`（下拉框选择，支持 value/label/index）、`upload_file`（文件上传，支持多文件）、`enter_iframe`/`exit_iframe`（iframe 进入退出）、`hover`（鼠标悬停）、`drag_and_drop`（拖拽）、`check`（勾选/取消勾选）、`double_click`（双击）
- **#2 浏览器会话复用**：新增 `BrowserSessionManager`，跨 Skill 共享浏览器实例，支持 acquire/release 会话池模式，后台自动清理超时空闲会话，全局单例 `get_session_manager()`
- **#3 Chrome 路径可配置**：`RaccoonConfig` 新增 `chrome_path`、`cdp_port`、`cdp_incognito_port` 配置项；`browser_engine.py` 从硬编码 macOS 路径改为自动检测（macOS/Linux/Windows + PATH 查找 + config.json 配置）
- **#4 logger 修复**：修复 `browser_engine.py` 中 `logger` 未定义导致运行时 NameError 的问题，改用标准 `logging.getLogger()`

#### Module 2: 部署标准化

- **#5 Dockerfile + docker-compose.yml**：多阶段构建（builder 安装依赖 + Playwright → runtime 精简镜像），docker-compose 支持环境变量配置、数据持久化、健康检查
- **#6 GitHub Actions CI/CD**：`.github/workflows/ci.yml`，Python 3.11/3.12 矩阵测试 + ruff lint + Docker 镜像自动构建发布
- **#7 健康检查端点**：`GET /health` 返回整体状态 + 各组件检查（LLM/EventBus/Scheduler/Database），供 Docker/K8s 探针使用
- **#8 优雅关闭**：shutdown 时清理浏览器会话（`BrowserSessionManager.shutdown()`）+ Skill 子进程

#### Module 3: Skill 生态增强

- **#9 版本管理增强**：`VaultManager.upgrade()` 升级前比较版本号（`_compare_versions()`），相同版本跳过，远程版本低于本地拒绝升级
- **#10 升级备份 + 回滚**：升级前自动备份到 `.backup/{name}_{version}/`，安装失败自动回滚；新增 `rollback()` 方法手动回滚到指定版本，`list_backups()` 查看可用备份
- **#11 热更新**：`VaultManager.hot_update()` 运行时更新 Skill 代码无需重启服务，保留 enabled/starred 等用户状态；新增 `POST /skills/{name}/hot-update`、`POST /skills/{name}/rollback`、`GET /skills/backups` API

#### 配置变更

- `RaccoonConfig` 新增字段：`chrome_path`（str）、`cdp_port`（int, 默认 9222）、`cdp_incognito_port`（int, 默认 9223）

#### 新增文件

- `skills/web_automate/session_manager.py`：浏览器会话管理器
- `Dockerfile`：多阶段 Docker 构建
- `docker-compose.yml`：Docker Compose 编排
- `.github/workflows/ci.yml`：CI/CD 流水线

## [0.3.4] - 2026-04-24

### 🔧 工作流 + 记忆进化

> 目标：工作流引擎从简单顺序执行升级为支持条件分支/循环/并行/LLM自动分解的完整引擎；MemCore 从静态存储进化为带衰减/归档/偏好学习的智能记忆系统；意图分类新增可选 LLM 模型分类 + 在线学习能力。默认运行路径仍使用 keyword，需通过 `intent_classifier_type=model` 显式启用模型分类。

#### Module 1: 工作流引擎增强

- **#1 LLM 自动分解**：`WorkflowEngine.execute_from_decomposition()` 集成 `Planner.decompose()`，复杂请求自动拆解为多步骤工作流
- **#2 条件分支增强**：`WorkflowStep` 新增 `if/else`（`if_condition` + `then_steps` + `else_steps`）、`loop`（`loop_var` + `loop_over` + `loop_steps` + `max_iterations`）、`parallel`（`parallel_steps`）三种步骤类型
- **#3 工作流模板市场**：`WorkflowTemplateMarket` 提供 5 个预置模板（写作辅助、日报生成、代码审查、研究摘要、内容发布），支持搜索和一键加载
- **#4 崩溃恢复**：`WorkflowStore` 从 JSON 迁移到 SQLite，新增 `workflow_executions` 表存储执行中间状态，`WorkflowEngine.resume_execution()` 支持从崩溃点恢复执行

#### Module 2: MemCore 记忆进化

- **#5 置信度衰减算法**：`MemCoreLifecycle.decay()` 从 noop 升级为指数衰减算法（`confidence *= e^(-λ*age_days)`），高频访问记忆衰减更慢（access_count > 5 时 λ 减半）
- **#6 经验复用率统计**：`MemCoreReader` 新增访问追踪（`_track_access` 更新 `access_count` + `last_accessed_at`），`get_top_accessed()` 和 `get_access_stats()` 提供复用率统计，搜索结果按 `confidence * (1 + access_count * 0.1)` 加权排序
- **#7 记忆归档**：`MemCoreLifecycle.archive_unused()` 归档超过 30 天未访问且 confidence < 0.3 的记忆
- **#8 偏好学习**：`MemCoreWriter.learn_preference_from_behavior()` 从用户行为中提取偏好（skill_used → frequent_skill, model_switched → preferred_model 等），相同偏好增加 access_count，偏好变化自动归档旧偏好

#### Module 3: 意图分类升级

- **#9 ModelIntentClassifier**：新增可选 LLM 轻量模型分类，三级分类流程：关键词优先（高置信度快速返回）→ 在线学习修正 → LLM 单次调用分类；默认仍是 `KeywordIntentClassifier`
- **#10 在线学习**：`OnlineLearningStore` 记录用户纠正，关键词重叠度 + 子串匹配评分，后续相似文本直接使用纠正后分类，最多保留 100 条纠正记录

#### Module 4: 流式聊天修复

- **#11 chat_stream LLM 分流**：`classify_llm_message()` 对 LLM 路由消息做三段式前两步分类，闲聊走 `llm.chat_stream()` 流式推送（保留打字机效果），Skill 匹配/学习确认走一次性 SSE 返回
- **#12 _ACTION_SIGNALS 清理**：移除冗余信号词，"帮我"前缀通过子串匹配覆盖所有"帮我X"变体

#### 数据模型变更

- `WorkflowStep` 新增字段：`if_condition`, `then_steps`, `else_steps`, `loop_var`, `loop_over`, `loop_steps`, `parallel_steps`, `max_iterations`
- `MemoryEntry` 新增字段：`access_count`, `last_accessed_at`
- `WorkflowStore` 从 JSON 迁移到 SQLite，自动迁移旧 `workflows/` 目录

#### 测试

- `test_workflow.py`：51/51 通过（Store CRUD + Engine 执行 + if/else/loop/parallel + 持久化 + 模板市场 + Planner）
- `test_memcore.py`：14/14 通过（读写 + 偏好冲突 + 衰减 + 归档 + 访问追踪 + 复用统计 + 偏好学习）
- `test_intent_classifier.py`：12/12 通过（关键词分类 + 模型分类 + 在线学习 + 纠正记录）

## [0.3.3] - 2025-04-24

### ⏰ 定时 + 通知 + 审批

> 目标：用户设一个定时任务，到点执行，结果推到手机。

#### 调度器加固

- **调度持久化到 SQLite**：`ScheduleStore` 从 JSON 文件迁移到 SQLite（共享 `data/memcore.db`），自动迁移旧 `schedules/` 目录到 `schedules.migrated/`
- **ScheduleStatus 三态模型**：`ENABLED` / `PAUSED` / `DISABLED` 替代旧的 `enabled: bool`，保留 `enabled` property 兼容旧代码
- **RetryPolicy 重试策略**：`max_retries`、`retry_interval_seconds`、`retry_on_failure`，失败后自动异步重试
- **调度执行记录**：`schedule_runs` 表记录 triggered_at / finished_at / result / error / duration_ms
- **跨进程 PID 锁**：`schedule_pid_lock` 表 + `os.kill(pid, 0)` 检测进程存活，防止多进程重复触发
- **暂停/恢复 API**：`POST /schedules/{id}/pause`、`POST /schedules/{id}/resume`、`GET /schedules/{id}/runs`

#### 通知网关扩展

- **钉钉 Webhook 通道**：HMAC-SHA256 签名，Markdown 消息格式，高优先级 @all
- **飞书 Webhook 通道**：HMAC-SHA256 签名，普通 Post / 高优先级交互卡片
- **Email 通道（SMTP）**：`aiosmtplib` 异步发送，支持 HTML body
- **通知模板系统**：5 种内置模板（default / brief / alert / approval / schedule_result），`string.Template` 渲染，支持自定义注册
- **通知去重**：`NotificationDedup` 基于 `time.monotonic()` 的时间窗口去重，默认 5 分钟

#### 审批引擎补全

- **RiskAssessor 风险等级评估**：根据 `SkillMetadata.risk_level` + `permissions` 自动判定（subprocess → high, filesystem → high, network → high, requires_approval → medium）
- **ApprovalEngine 审批引擎**：`review()` / `approve()` / `reject()` / `get_pending()`，后台超时清理循环
- **ApprovalStatus 五态模型**：PENDING / APPROVED / REJECTED / EXPIRED / AUTO_APPROVED
- **审批通知集成**：需要审批时通过 Notifier + NotificationTemplate 推送
- **审批 HTTP API**：`GET /approvals/pending`、`POST /approvals/{id}/approve`、`POST /approvals/{id}/reject`、`GET /approvals/{id}`
- **Config 新增**：`approval_timeout_seconds`（默认 300 秒）

#### 测试

- `test_scheduler.py`：33/33 通过（SQLite store、三态模型、重试策略、PID 锁、执行记录）
- `test_notifier.py`：56/56 通过（7 通道、模板系统、去重机制）
- `test_approval.py`：38/38 通过（风险评估、审批流程、通知集成、生命周期）

## [0.3.2.1] - 2025-04-24

### 🏪 Skill 市场完整实现

- **市场索引**：新增 `market_index.json`，包含 25 个官方 Skill 元数据（名称、版本、描述、分类、触发词、风险等级等）
- **后端市场 API**：
  - `GET /market` — 列出市场 Skill，支持 `?category=` 分类过滤和 `?q=` 搜索
  - `POST /market/{name}/install` — 从市场安装 Skill（支持 Git URL 远程安装和 builtin 本地加载）
  - `DELETE /skills/{name}` — 卸载已安装的 Skill
  - `POST /skills/{name}/upgrade` — 升级已安装的 Skill（从 `installed_from` 重新拉取）
- **前端市场 UI**：
  - 商城弹窗从 `/market` API 加载，合并市场索引与已安装状态
  - Tab 切换改为「未安装 / 已安装 / 排行榜」
  - 未安装 Skill 显示「📥 安装」按钮，已安装 Skill 显示启用开关 + 🗑 卸载按钮
  - 安装中按钮显示「⏳ 安装中...」状态
  - 版本不一致时显示升级提示 badge
  - 已安装卡片左侧绿色边框标识
- **Skill 版本管理**：
  - `SkillMetadata` 新增 `installed_from`（安装来源 Git URL / "builtin"）和 `installed_at`（ISO 8601 时间戳）
  - `VaultManager.upgrade(name)` 方法：卸载旧版 → 重新从 `installed_from` 安装
  - Git 安装时自动记录来源和时间
- **安装进度 SSE**：
  - `EventType` 新增 `SKILL_INSTALLING` / `SKILL_INSTALLED` / `SKILL_INSTALL_FAILED`
  - 前端 SSE 监听安装事件，实时显示安装状态通知
  - 安装完成/失败自动刷新技能列表

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
