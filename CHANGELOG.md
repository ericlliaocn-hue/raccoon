# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.6.1] - 2026-04-26

### 🔧 飞书通道补全 + 调度竞态修复 + SSE/前端稳定性

> 目标：补齐飞书通道落地细节（API 回发 + WS 长连接生命周期），修复调度器竞态条件，增强前端 SSE 与调度面板健壮性。

- **版本先行同步**：`pyproject.toml`、`src/__init__.py` 统一到 `0.6.1`
- **飞书 API 动态回发**：新增 `FeishuApiClient`，支持按 `chat_id` 回发文本消息到原会话；回发失败自动走 webhook 兜底
- **飞书 WS 长连接生命周期**：`FeishuWebSocketBridge` 接入 FastAPI lifespan，启动/关闭自动管理；断线自动重连
- **飞书回调路由完整接入**：新增 `/channels/feishu/events` 端点，支持 URL 验证、消息事件归一化、群聊 @ 门控、allowlist 与异步处理
- **飞书回调鉴权豁免**：`/channels/feishu/` 路径不要求 HTTP Auth Token，避免飞书平台回调被拦截
- **多通道上下文透传**：`Executor` 将 `channel_source`/`channel_chat_id`/`channel_sender_id`/`channel_message_id` 写入 Task.context，便于 TASK_COMPLETED 后按渠道回传
- **飞书任务结果回传**：新增 `on_task_finished_reply_to_feishu` 事件监听，任务完成/失败后自动通过飞书 API 或 webhook 回传结果
- **调度器竞态修复**：触发前重新读取最新 entry 防止使用过期快照；获取锁后二次确认避免"删后回写"；重试时使用最新 entry 避免竞态
- **SSE 断线重连防抖**：前端 SSE 重连改为防抖模式，避免抖动时堆积重连定时器；页面关闭前主动清理
- **调度面板增强**：创建调度按钮防重复点击；加载失败时显示错误提示而非静默；调度卡片显示 schedule_id 前缀
- **新增文件**：`src/channels/adapters/feishu.py`、`src/channels/adapters/feishu_api_client.py`、`src/channels/adapters/feishu_ws_client.py`、`docs/integrations/feishu-quickstart.md`
- **新增测试**：`test_feishu_adapter.py`、`test_feishu_api_client.py`、`test_feishu_ws_client.py`、`test_auth_routes.py`

## [0.6.0] - 2026-04-26

### 🚀 通道标准化与长任务交付（第一批落地）

> 目标：启动 `0.6.0` 主线，把“多通道聊天”升级为“可稳定交付任务结果”的基础能力。

- **版本先行同步**：`pyproject.toml`、`src/__init__.py`、静态资源版本参数与 UI 文案统一到 `0.6.0`
- **`Channel Contract v1` 基础模型**：新增统一入站/出站消息信封、附件模型与通道能力声明，作为后续 Feishu/Telegram/Slack 适配器共同契约
- **飞书回调适配层**：新增 `parse_feishu_callback` 与 `/channels/feishu/events`，支持 URL 验证、消息事件归一化、群聊 @ 门控、allowlist 与异步处理
- **飞书 WS 双模式接入**：新增 `feishu_mode=callback|websocket` 可选模式；websocket 通过官方 `lark-oapi` 长连接接收事件（无需公网回调），callback 保留为备选
- **飞书动态回发**：新增 Feishu OpenAPI 回发客户端，默认优先按 `chat_id` 回原会话；当 API 回发失败时自动走 webhook 兜底
- **飞书接入助手命令**：新增 `raccoon feishu init/check`，一键初始化 `config.json` 并输出回调配置要点
- **长任务 `Job` 基础框架**：新增后台任务状态模型与管理器（`queued/running/waiting/uploading/delivered/failed/cancelled`），支持创建、进度更新、完成/失败/取消
- **Job 持久化存储**：新增 `JobStore`（SQLite），`jobs` 与 `job_artifacts` 全量落库，重启后任务与交付产物可恢复
- **交付重试协调器**：新增 `JobDeliveryCoordinator`，支持 `pending/retry/sent/dlq` 生命周期、指数退避重试与 DLQ 收敛
- **HTTP `jobs` API 增强**：新增 delivery 字段（state/attempts/next/error），`/jobs` 支持 `delivery_state` 过滤，创建与完成接口支持声明 `delivery_required`
- **事件可观测性增强**：新增 `JOB_CREATED/JOB_PROGRESS/JOB_COMPLETED/JOB_FAILED/JOB_CANCELLED` 事件，统一进入 EventBus/SSE 观测链路
- **通道安全默认收口**：`/jobs` 纳入受保护 API，遵循现有 token 认证策略
- **`0.6.0` 计划文档落盘**：新增 `docs/roadmap-0.6.0.md`，固定里程碑、门禁指标与风险回滚策略
- **接入文档补齐**：新增 `docs/integrations/feishu-quickstart.md`
- **回归验证**：`python3.11 -m compileall`、`ruff check`、`pytest -m "not integration"` 均通过（508 passed）

## [0.5.11] - 2026-04-25

### 🌍 开放世界泛化 + 执行覆盖率冲刺（按 1→5 顺序落地）

> 目标：把“复杂外站可执行性”和“完整输入默认执行覆盖”变成可量化门禁，而不是主观体感。

- **版本先行同步**：`pyproject.toml`、`src/__init__.py`、静态资源版本参数与 UI 文案统一到 `0.5.11`
- **门槛固化**：新增开放世界门禁指标 `open_world_execution_success_rate`、`browser_long_chain_success_rate`、`false_clarification_rate_max`、`complete_input_auto_execute_rate`
- **开放世界压测包**：新增 `open_world_pack`（30+ 样本）和 `benchmark open_world`，输出场景识别、执行成功、误追问与 mismatch 样本
- **浏览器链路再硬化**：补充动态 selector 候选、请求级等待信号和登录页探测，提升长链路连续执行稳定性
- **执行覆盖率补强**：价格监控支持“商品关键词目标”自动补参；远程执行支持常见命令模板归一，减少可执行请求被误追问
- **一键门禁收口**：`benchmark all` 联合输出 `core + live + open_world` 报告，用于发布前统一验收

## [0.5.10] - 2026-04-25

### 🌐 浏览器链路硬化（登录态信号 + checkpoint 恢复 + 失败证据增强）

> 目标：把登录/提交流程的长链路失败从“偶发中断难复盘”压到“可阻断、可恢复、可定位”。

- **版本先行同步**：`pyproject.toml`、`src/__init__.py`、静态资源版本参数和 UI 版本文案统一到 `0.5.10`
- **登录态治理增强**：`BrowserEngine` 增加域名健康详情、认证失败信号（HTTP 401/403/407/419/440）与最近证据窗口；执行前会基于信号阻断高风险后续动作，避免“登录失效还硬跑”
- **链路时序与重试可观测**：每个动作新增结构化执行轨迹（wait/action/assert 分段耗时、重试次数、失败原因），并写入结果 `artifacts`
- **checkpoint 恢复细化**：恢复逻辑支持从上次最后成功 checkpoint 的下一步继续（即使未记录 `failed_step` 也可恢复），减少整条链路重跑
- **失败证据增强**：失败归档新增当前动作指纹、最近网络失败/认证异常、控制台错误摘要，提升复盘与修复效率
- **回归测试补齐**：新增浏览器硬化单测（认证信号阻断、执行轨迹写入、checkpoint 回退恢复）并通过

## [0.5.9] - 2026-04-25

### 🎯 学习闭环提纯（failure_code 修复模板 + 复用召回增强）

> 目标：在不引入长时压测成本的前提下，优先提升“先复用、再学习”的命中率与失败可修复性。

- **版本先行同步**：`pyproject.toml`、`src/__init__.py`、静态资源版本参数和 UI 版本文案统一到 `0.5.9`
- **复用召回增强**：`LearningEngine._search_experience()` 升级为多查询变体（用户域 + learning_engine 域）检索与候选打分；写经验时补充 alias key，提升相似请求命中
- **复用安全闸门**：经验复用新增 `reuse_confidence` 下限与 `scenario_id` 一致性检查，降低“命中错经验”的误复用
- **failure_code 定向修复增强**：保留细分 `execution_contract_*` 失败码（不再全部折叠），新增 `dependency_requires_approval` / `skill_runner_missing` / `vault_unavailable` 等模板化修复指引
- **意图归一增强**：扩展中英文同义归一（summary/screenshot/monitor/notify 等），减少因表达差异导致的复用漏召回
- **回归测试补充**：新增学习闭环召回与失败码细分测试，核心回归通过

## [0.5.8] - 2026-04-25

### 🛠️ 双向修复（平衡档契约）+ 真实外站压测固化

> 目标：修复 `price_monitor` / `remote_exec` 在真实外站压测中“执行成功却被契约误杀”的确定性失败。

- **版本先行同步**：`pyproject.toml`、`src/__init__.py`、静态资源版本参数和 UI 版本文案统一到 `0.5.8`
- **Skill 结构化证据补齐**：`change_detector` 输出标准化 `artifacts`（`target/url/path/snapshot_id/content_hash/subcmd`）；`shell_exec` 输出 `command/exit_code/task_id/trace` 并保留向后兼容 reply/files
- **执行契约平衡化**：`price_monitor`、`remote_exec` 改为“结构化证据优先 + 文案兜底”，降低误杀但不放宽 `login_form_chain` 的严格约束
- **LearningEngine 证据归一**：执行结果入库前补齐最小证据（来自 task/params 的 `target`、`command`、`task_id`、`trace` 等），避免因 Skill 返回字段缺失导致误判
- **外站压测一键化**：新增 `raccoon benchmark live`，统一输出 `decision_success` / `execution_success` / 场景识别准确率 / 浏览器链路成功率 / failure_code TopN / mismatch 样本
- **测试补齐**：新增契约正反例、Skill `artifacts` 完整性、稳定路径执行回归测试，覆盖本轮修复点

### 🚀 学习闭环 + 浏览器稳定性硬化

> 目标：把“该中 Skill 就执行、该追问就追问、浏览器失败可恢复可复盘”落到可验收门禁。

- **场景契约接入学习主链**：新增 `scenario_contracts`，统一核心场景请求前置校验与执行后契约校验（price/login/remote/schedule）
- **reused 可控执行补强**：`content_skill_bundle` 在 runner 可用时会尝试真实执行，保留 `reused` 语义但增加 `execution_attempted/execution_success` 数据
- **失败修复模板化**：按 `failure_code` 注入定向修复策略，降低泛化重试比例
- **浏览器恢复落盘**：`web_automate` 增加跨进程 resume 状态持久化（`output/web_automate_resume/*.json`）
- **失败证据标准化**：新增 failure evidence manifest（失败步骤 + checkpoint + domain_health + artifacts）
- **浏览器强断言**：动作支持 `expected_url_contains` / `expected_url_not_contains` / `assert_selector` / `assert_text_contains`，减少“执行成功但业务未完成”
- **benchmark 三轨观测**：在双包断言外新增 `offline + real_task` 观测包输出，避免只看夹具数据
- **门禁防空跑**：`raccoon benchmark core` 新增 `runs=0/total_cases=0` 保护并输出明确失败
- **远程监听安全闸门**：新增 fail-closed 启动校验（非 localhost 且未配置/弱 token 时拒绝启动），并在 doctor 明确报错
- **doctor 依赖检查补齐**：新增 `pydantic-settings`、`playwright` 依赖可用性检查

## [0.5.7] - 2026-04-25

### 🔧 地基加固（学习闭环 + 浏览器链路）

> 目标：在不扩生态/多平台的前提下，优先把“真实学习可执行性”和“浏览器长链路稳定性”继续压实。

- **学习闭环执行深化**：`login_form_chain` 增加登录流程上下文约束（URL 不是唯一条件），减少“地址有了但动作条件不足”导致的伪执行
- **稳定编排入参增强**：`web_automate` 稳定执行路径可注入结构化动作序列（登录/上传/提交），降低纯自然语言解析漂移
- **浏览器失败策略增强**：长链路默认 fail-fast（失败即停），减少级联错误污染，恢复点更明确
- **浏览器证据沉淀**：每次运行生成 artifacts manifest（检查点/失败证据/域名健康），支持后续定位与复盘
- **回归覆盖补强**：新增学习闭环与浏览器 fail-fast/continue-on-error 测试，保障修复可持续

## [0.5.6] - 2026-04-25

### ⚙️ 稳定化冲刺（执行成功率优先）

> 目标：把“命中可复用”升级成“可控执行”，并给基准与浏览器链路补齐可复盘能力。

- **版本先行落地**：按规则先完成 `pyproject/src/static/changelog` 版本同步，再开始本轮开发
- **Playbook 执行通道补强**：`price_monitor/login_form_chain/remote_exec` 命中后在参数完整时走稳定执行路径，不再只记 `reused`
- **浏览器链路硬化**：统一 `wait-visible -> wait-stable -> action -> assert`，失败自动留证据（截图/DOM/请求摘要）并携带 checkpoint
- **基准一键化升级**：`raccoon benchmark core` 支持双包（clarification + execution）执行与 mismatch 报告，避免空报告
- **路由误判回归补充**：新增路径/英文词干扰测试，减少误判和误触发

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
