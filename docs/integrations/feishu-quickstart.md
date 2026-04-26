# Feishu 集成快速指南（Raccoon `0.6.0`）

本文目标：让开发者安装后最快完成飞书接入，不需要阅读业务代码。

## 1. 先选模式（callback / websocket）

Raccoon 现在支持两种飞书接入方式，按场景二选一：

- `callback`：平台回调到你的 HTTP 地址（需要公网可达）
- `websocket`：Raccoon 主动连飞书长连接（本机开发更省事）

### A) WebSocket（推荐本机）

```bash
raccoon feishu init \
  --mode websocket \
  --app-id cli_xxx \
  --app-secret xxx \
  --domain feishu \
  --webhook https://open.feishu.cn/open-apis/bot/v2/hook/xxx
```

飞书后台：事件订阅选择「长连接（WebSocket）」并开启 `im.message.receive_v1`。

### B) Callback（保留备选）

```bash
raccoon feishu init \
  --mode callback \
  --callback-base https://bot.example.com \
  --webhook https://open.feishu.cn/open-apis/bot/v2/hook/xxx \
  --webhook-secret sec_xxx
```

飞书后台配置：

- 请求网址：`https://bot.example.com/channels/feishu/events`
- Verification Token：`config.json` 中 `feishu_verification_token`
- 订阅事件：`im.message.receive_v1`

启动与验证（两种模式通用）：

```bash
raccoon --http
raccoon feishu check
```

## 2. 配置说明

`config.json` 关键字段：

- `feishu_enabled`: 开关
- `feishu_mode`: `callback` / `websocket`
- `feishu_verification_token`: 回调 token
- `feishu_app_id`: websocket 模式 App ID
- `feishu_app_secret`: websocket 模式 App Secret
- `feishu_domain`: `feishu` / `lark` / 自定义域名
- `feishu_mention_required_in_group`: 群聊是否要求 @ 才触发
- `feishu_allow_chat_ids`: 群聊白名单（可选）
- `feishu_allow_user_ids`: 用户白名单（可选）
- `feishu_reply_via_api`: 是否按 `chat_id` 动态回发到原会话（推荐 `true`）
- `feishu_reply_via_webhook`: 动态回发失败时是否 webhook 兜底
- `feishu_async_process`: 是否异步处理回调（建议 `true`）

通知通道：

```json
{
  "notify_channels": [
    {
      "type": "feishu",
      "name": "feishu-main",
      "webhook": "https://open.feishu.cn/open-apis/bot/v2/hook/xxx",
      "secret": "sec_xxx"
    }
  ]
}
```

## 3. 通道行为

- `feishu_mode=websocket`
  - 启动时建立飞书长连接
  - 收到 `im.message.receive_v1` 后走统一 Router/Executor
  - 回复优先按 `chat_id` 动态回原会话
  - 无需公网回调地址

- `feishu_mode=callback`
  - 使用 `POST /channels/feishu/events`
  - 支持 URL 验证（返回 `challenge`）
  - 消息归一化进入 Router/Executor
  - 回复优先按 `chat_id` 动态回原会话，失败再 webhook 兜底

## 4. 默认安全策略

- 群聊默认需要 @ 才触发
- 支持 chat/user allowlist
- 回调入口不依赖 `http_auth_token`，依赖飞书 `verification_token` 校验（callback 模式）
- 推荐公网入口前置网关/WAF 与来源 IP 策略

## 5. 已知限制

- 加密回调（`encrypt` 字段）当前未启用，建议先用非加密模式联调
- 文件回传目前走 webhook 文本通知路径，复杂文件卡片可在后续版本增强

## 6. 排障

1. URL 验证失败：
   - 检查 `feishu_verification_token` 与飞书后台一致

2. 收到消息但不触发：
   - 群聊是否 @ 机器人
   - `feishu_allow_chat_ids`/`feishu_allow_user_ids` 是否误拦截

3. 有执行但无回消息：
   - `feishu_reply_via_api` 是否为 `true`
   - `feishu_app_id` / `feishu_app_secret` 是否配置
   - `notify_channels.feishu.webhook` 是否可用
   - `feishu_reply_via_webhook` 是否为 `true`
