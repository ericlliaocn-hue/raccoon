# Raccoon `0.6.0` 开发计划（标准化通道 + 可交付长任务）

> 最后更新：2026-04-26  
> 当前基线：`0.6.0`  
> 版本定位：`0.6.0`（通道能力产品化版本）

---

## 1. 版本目标（不是“多接几个 Bot”）

`0.6.0` 的核心是把通道从“能聊天”升级为“能稳定完成任务并交付结果”：

1. **标准化通道设计**：统一 adapter 契约、消息信封、会话映射、投递语义。
2. **长任务不阻塞**：视频生成、文件打包、远程传输默认后台执行，用户可继续对话。
3. **结果可达可追踪**：任务完成后主动通知，文件/视频必须送达或可重试送达。
4. **稳定性门禁化**：以指标决定发布，不靠主观“看起来通了”。

---

## 2. 竞品经验吸收（做得好的要学，踩过的坑要避）

### 2.1 借鉴点（保留）

1. 多通道网关（统一入口、统一会话）
2. 通道细粒度策略（DM/群聊、@提及门控、allowlist）
3. webhook/websocket 双模式（内网/公网部署都可行）
4. 通道独立配置与运行状态诊断（doctor/health）

### 2.2 已知坑（前置规避）

1. **connected 但无 inbound**：只看连接状态，不看端到端消息探活。
2. **多账号串线**：回复目标丢失，回到默认通道或默认会话。
3. **升级掉依赖**：adapter 依赖缺失但服务仍启动，运行期才失败。
4. **长会话意外压缩/死循环**：用户感知不可解释，任务中断不可恢复。
5. **系统消息泄漏给用户**：内部状态文案直接出现在聊天通道。

---

## 3. 设计原则（0.6.0 强约束）

1. **稳定优先于覆盖**：先把 2-3 个通道打透，再扩数量。
2. **通道无业务特权**：业务逻辑在核心层，通道只做 I/O 适配。
3. **默认异步交付**：长任务返回 `job_id`，不中断主对话。
4. **先存储后投递**：产物先落 artifacts，再通知/发送，避免“发到一半丢”。
5. **可观测优先**：每次投递、重试、失败都必须有 trace。

---

## 4. 通道标准：`Channel Contract v1`

### 4.1 统一入站信封

`ChannelInboundEvent`（核心字段）：

- `channel` / `account_id`
- `chat_id` / `thread_id` / `message_id`
- `sender_id` / `sender_display`
- `conversation_id`（稳定映射后）
- `text`
- `mentions[]`
- `attachments[]`（统一文件元数据）
- `timestamp`
- `trace_id`

### 4.2 统一出站信封

`ChannelOutboundMessage`：

- `target`（chat/thread/reply_to）
- `text` / `blocks` / `card`
- `attachments[]`
- `delivery_mode`（immediate/deferred）
- `correlation_id`（关联 job / run）

### 4.3 Adapter 能力声明

每个通道显式声明 `capabilities`：

- `inbound_text`, `outbound_text`
- `inbound_file`, `outbound_file`
- `reply_thread`, `edit_message`, `typing`
- `mention_gate`, `group_policy`, `allowlist`
- `webhook_mode`, `websocket_mode`
- `signature_verify`, `rate_limit_hint`

---

## 5. 长任务交互模型（视频/文件/长链路）

### 5.1 默认策略

1. **默认不阻塞**：创建后台 `job` 后立即返回 `job_id` + 预计耗时。
2. **可继续聊天**：用户可并行发起其它任务。
3. **两类阻塞例外**：
   - 缺关键参数（进入 `WAITING_INPUT`）
   - 审批未通过（进入 `WAITING_APPROVAL`）

### 5.2 Job 状态机

`QUEUED -> RUNNING -> WAITING_INPUT/WAITING_APPROVAL -> UPLOADING -> DELIVERED -> FAILED/CANCELLED`

### 5.3 完成通知与交付保证

1. 主通道主动通知（SSE + 通道消息）
2. 投递失败自动重试（指数退避）
3. 超过重试阈值进入死信队列（DLQ）并告警
4. 有 home-channel 时自动兜底投递
5. `GET /jobs/{id}` 永远可查状态与产物

---

## 6. Skill 命中稳定方案（复用 webchat 核心 + 薄增强）

1. `ChannelNormalize`：先把各通道输入归一化，再进 Router。
2. 通道门控前置：群聊未 @ 不进入技能路由。
3. Router 权威优先：精确匹配 > playbook > 意图分类 > LLM fallback。
4. 低置信度确认：`confidence < threshold` 必须先确认，不直接执行。
5. 通道回归包：每通道每日固定样本跑命中率与误判率。

---

## 7. 远程文件场景（手机拿电脑文件）

### 7.1 场景目标

用户在手机通道发指令，系统从受控目录取文件并完成：

1. 权限校验
2. 预览生成（能预览优先）
3. 原件送达（附件或短时链接）
4. 审计留痕（谁、何时、取了什么）

### 7.2 文件类型矩阵（`0.6.0` 必达）

1. `txt/md/json/log`：直接文本预览 + 原件
2. `csv/xlsx`：表格预览（截断） + 原件
3. `doc/docx/ppt/pptx`：转 PDF/图片预览（至少第一页） + 原件
4. `png/jpg/webp/gif`：原图预览 + 原件
5. `mp4/mov`：封面预览 + 原视频
6. 大文件：异步上传 + 进度通知 + 续传

---

## 8. 安全基线（通道层）

1. webhook 认证默认 header-only（禁 query token）
2. 签名校验统一中间件（飞书/钉钉/Slack 等）
3. 通道 allowlist（用户、群、租户）默认最小授权
4. 文件外发受策略控制（目录/后缀/大小/风险等级）
5. 所有下载链接短时有效、可撤销、可审计

---

## 9. 里程碑与交付（建议 8 周）

| 里程碑 | 周次 | 目标 | 核心交付 |
|---|---|---|---|
| M0 | Week 0 | 设计冻结 | `Channel Contract v1` RFC + 数据模型 + 门禁定义 |
| M1 | Week 1 | 通道底座 | adapter SDK、消息信封、会话映射、健康探活 |
| M2 | Week 2 | Feishu 双向稳定 | inbound/outbound、@门控、allowlist、签名校验 |
| M3 | Week 3 | 长任务框架 | Job 状态机、异步执行、通知回执、DLQ |
| M4 | Week 4 | 文件交付闭环 | artifact 存储、预览管线、下载令牌、审计 |
| M5 | Week 5 | 第二通道接入 | Telegram 或 Slack（复用同一 contract） |
| M6 | Week 6 | 稳定性硬化 | 重连/限流/幂等/升级 preflight |
| M7 | Week 7-8 | 发布门禁周 | 连续 7 天分段压测 + 真实通道灰度 |

---

## 10. 发布门禁（`0.6.0`）

### 10.1 功能门槛

1. 长任务不中断主对话（并发会话可用）
2. 文件场景支持上述类型矩阵
3. 每个已接入通道可用 doctor 一键诊断

### 10.2 稳定性指标

1. 入站端到端成功率 `>= 99%`
2. 回复错通道率 `<= 0.1%`
3. 长任务完成通知送达率 `>= 99%`
4. 浏览器长链路执行成功率 `>= 90%`（夹具）
5. 卡死率 `<= 1%`

### 10.3 命中与执行指标

1. `decision_success_rate >= 95%`
2. `execution_success_rate >= 85%`
3. `false_clarification_rate <= 5%`

---

## 11. 测试与压测计划

1. 单元测试：contract 序列化、路由门控、幂等、重试策略
2. 集成测试：Feishu/Telegram 通道入站到交付全链路
3. 场景测试：`remote_file_fetch`、`video_generate_and_deliver`
4. 分段压测：每天 2-3 段，累计统计，不要求 7x24 持续开机
5. 扰动注入：断网、429、websocket 重连、平台回调重复投递

---

## 12. 可观测性与运维

1. 统一 trace：`trace_id` 贯穿通道、job、skill、artifact
2. Dashboard 指标：
   - inbound_success_rate
   - outbound_delivery_success_rate
   - wrong_channel_delivery_rate
   - retry_count / dlq_count
   - median_completion_latency
3. 告警策略：连续失败、重连风暴、DLQ 堆积、依赖失效

---

## 13. 风险与回滚

1. 风险：通道 API 变更导致突发不可用  
   - 对策：adapter 版本探测 + feature flag + canary
2. 风险：长任务堆积占满资源  
   - 对策：队列配额 + 并发上限 + 过载拒绝
3. 风险：文件预览链路耗时高  
   - 对策：预览异步化 + 缓存 + 大文件分级策略
4. 回滚：每个通道可独立熔断，降级到仅通知或只读模式

---

## 14. 非目标（`0.6.0` 不做）

1. 不追求一次性接入“所有通道”
2. 不在本版本扩 Docker 战线
3. 不为生态数量空造 Skill

---

## 15. 版本完成定义（Definition of Done）

`0.6.0` 只有在以下条件同时满足时才发布：

1. M0-M7 交付全部完成
2. 发布门禁指标连续 7 天达标
3. 至少 2 个通道达到生产可用稳定度
4. 远程文件场景可稳定交付并可审计
