# v2.0.0 流式卡片（CardKit Streaming）设计

- **日期**: 2026-06-19
- **状态**: 设计已批准（v2，整合完整性审查 21 项 gap），待实现计划
- **版本**: v2.0.0（架构级演进）
- **决策**: 全替换 + monkey-patch + MVP（含流式多卡拆分）

## 背景

v1.x 用 Schema 2.0 interactive 消息 + 手动 PATCH（per-chat `asyncio.Lock` + 单调 seq stale-drop）实现进度卡片。最大体验代差：AI 回复是「完成后整段发」，非实时流式打字机。Cheerwhy/hermes-lark-streaming、baileyh8/hermes-feishu-streaming-card 均用飞书 CardKit 流式 API 实现了流式。

v2.0 迁移到飞书 CardKit 流式 API，实现 AI 回复实时流式打字机。

## 决策

| 维度 | 决策 | 理由 |
|---|---|---|
| **迁移策略** | **全替换** | 废弃 PATCH/interactive 渲染那套，用 CardKit 流式 API 整体替代 |
| **架构路线** | **monkey-patch** | 接 Hermes 流式回调 + 调 CardKit API，与 v1.x 一致，无新进程 |
| **范围** | **MVP 含多卡拆分** | 流式回复 + 思考 + footer + Response header + 消息保护 + **200 元素多卡拆分**（生产硬约束，非 stretch） |
| **seq/lock** | **保留**（⚠️ 修正）| 飞书 `sequence` 字段强制递增（300317），seq/lock 从「视觉节流」重新定位为「**API 时序保证**」。不能废弃 |
| **打字机** | **可配置** | `FEISHU_PROGRESS_PRINT_STEP` / `_FREQUENCY`（下限 100ms，单卡 10 次/秒） |

## 飞书 CardKit 流式 API（技术基础 + 硬约束）

**API 流程**：
- `card/create`（`streaming_mode:true` + `streaming_config` + `update_multi:true` + `summary`）→ 得 `card_id`
- `message/create`（`content={"type":"card","data":{"card_id":...}}`）→ 发卡片实体
- `card-element/content`（路径 `/cards/:card_id/elements/:element_id/content`）流式更新文本（传全量，平台打字机）
- `card/update` 全量更新（终态）
- `card/settings`（`streaming_mode:false`）关闭流式

**硬约束**（带错误码）：
| 约束 | 错误码 | 影响 |
|---|---|---|
| `sequence` 必填、严格递增 | 300317 | 乱序被拒 → **必须保留 seq/lock** |
| `element_id` 必填、全局唯一（1-20 字符）| 300301 | 创建时定义，流式更新路径要匹配 |
| `update_multi` 必须 true | 300302 | 显式设置 |
| 单卡 200 元素 / 30KB / 单文本 100k 字符 | 300305 / 200860 | **多卡拆分 MVP 必做** |
| 单卡操作 10 次/秒上限 | 429 | print_frequency ≥ 100ms |
| 卡片实体只能创建应用操作 | 300311 | 多 profile 必须独立 app_id |
| 卡片实体有效期 14 天 | 200750 | stale cleanup 区分消息层/实体层 |
| 流式中不支持交互回调 | 200810 | MVP 不加交互组件规避 |
| 客户端 7.20+（7.20-7.22 静默忽略自定义流式参数，7.23+ 生效）| — | 文档说明 |
| 卡片实体只能发一次、流式中无法转发 | — | 关闭流式后才能转 |

**打字机前提**：新文本须是旧文本的**前缀子串**才逐字；否则全量闪现（无打字机）。→ think 剥离只能在终态做（流式中途剥离会破坏前缀）。

## 架构

```text
Hermes 流式回调 (thinking.delta / answer.delta / tool.updated / message.completed)
        │  (monkey-patch，按 chat_id 路由 — 改 contextvars/dict，不再全局单例)
        ▼
StreamingCardHandler (新)
        │
        ├─ CardKitClient: card/create → stream update text → card/update → card/settings
        ├─ 时序: per-chat asyncio.Lock + 单调 sequence（API 时序保证，非视觉节流）
        ├─ 多卡: _active_cards[chat_id] = List[card_id]，200 元素拆分
        ├─ 守卫: _aborted_chats / _completed_chats
        └─ 配置: print_step/frequency (≥100ms)
```

## 卡片生命周期（数据流）

| 事件 | 动作 |
|---|---|
| `on_processing_start` | `card/create`（streaming + summary "Hermes 思考中..."）→ `message/create`；记 `created_at` |
| `thinking.delta` | stream update（灰色思考块 `thinking_md`） |
| `tool.updated` | 局部更新（工具面板 `tools_md`） |
| `answer.delta` | `stream_update_text`（累积全量回复 `reply_md`，**流式中不剥离 think**） |
| 接近 200 元素 | 封存旧卡（`close_streaming` + 改 Completed）→ 新 `card/create` 继续流式 |
| `message.completed` | `_completed_chats.add` → 终态全量更新（剥 think + footer + Response header）→ `close_streaming` |
| 失败 | 全量更新（红 Failed）→ 关闭流式 |
| abort（撤回提问 / 撤回卡片本身 / stream 失败）| 遍历所有子卡：全量 Aborted → 关闭流式 |

**关键守卫**：所有 stream update 入口检查 `_aborted_chats` **和** `_completed_chats`（晚到 delta 命中 300309/300317）。

## 组件分解（文件结构）

| 文件 | 职责 | 动作 |
|---|---|---|
| `__init__.py` | monkey-patch 装载 | **改**：接流式回调（`answer.delta`/`tool.updated`/`message.completed`），按 **chat_id 路由**（`contextvars` 或 `Dict[chat_id, adapter]`，修 v1.x 多 chat 串卡 bug）；保留 root_id / agent patch；**删** `send`/`edit_message`/`_build_outbound_payload` patch |
| `streaming_handler.py` | 流式状态机 | **新建**`StreamingCardHandler`：`_active_cards: Dict[str, List[str]]`（多卡）、`_card_seqs: Dict[str, int]`（per-card sequence）、`_aborted_chats`、`_completed_chats`、累积文本缓冲；复用 `_build_footer_elements`/`_strip_think_tags`/`_render_*` |
| `cardkit_client.py` | CardKit API 封装 | **新建**：`create_streaming_card`/`stream_update_text`/`update_card_full`/`close_streaming`，每个调用带 `sequence`，捕获 300317/300309/200810 降级 |
| `card_handler.py` | v1.x PATCH 逻辑 | **删除**（复用部分移到 streaming_handler） |

**element_id 命名**：`reply_md`（回复）、`thinking_md`（思考）、`tools_md`（工具面板）；多卡拆分 `{type}_{n}`。

## 兼容功能（流式下具体做法）

| 功能 | 流式版做法 |
|---|---|
| **footer** | `message.completed` 全量更新加（复用 `_build_footer_elements`）；多卡仅末卡 |
| **消息保护** | 撤回提问 **或撤回卡片本身** → 遍历所有子卡 close + Aborted。`_aborted_chats` + stream update 守卫 |
| **Response header** | 创建时 Running（蓝）；`completed` 终态改 Response（turquoise） |
| **think 剥离** | **仅终态全量更新时** `_strip_think_tags`（流式中途剥离破坏打字机前缀）|
| **代码块空格** | `_preprocess_feishu_markdown` 流式版剥离 ``` 块前后空格（否则渲染失败）|
| **root_id 清除** | 不变 |
| **agent 捕获** | `__setattr__` patch 保留（footer token/model） |
| **打字机配置** | `FEISHU_PROGRESS_PRINT_STEP`（默认 1）/ `_FREQUENCY`（默认 100ms，下限 100ms） |
| **summary** | `card/create` 显式设 `summary.content`（"Hermes 思考中..." / 首 thinking 截取），避免默认「[生成中...]」 |

## 失败处理

| 场景 | 处理 |
|---|---|
| `card/create` 或 `message/create` 失败 | 清 `_active_cards`/`_pending_footer`/`_first_response_ids`，回退 `_orig_send` 纯文本（reply chain 不断） |
| `stream_update_text` 失败（300317 乱序）| per-chat lock 保证时序；若仍失败，跳过本次（不 abort） |
| `stream_update_text` 失败（300309 流式已关 / 200810 交互中）| 降级：改 `card/update` 全量 |
| `completed` 后晚到 delta | `_completed_chats` 守卫，直接 drop |
| gateway 重启孤儿流式卡 | 持久化 `card_id`+`message_id`+`created_at`；重启删消息层；`created_at > 14 天` 跳过实体操作 |
| 多 profile 共用 hermes home | stale 文件按 `app_id` 分桶（`feishu_active_cards_{app_id}.json`） |

## 配置（环境变量）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FEISHU_PROGRESS_STYLE` | — | `streaming` 激活 v2.0（`card` v1.x 废弃） |
| `FEISHU_PROGRESS_PRINT_STEP` | `1` | 打字机步长（1=逐字） |
| `FEISHU_PROGRESS_PRINT_FREQUENCY` | `100` | ms，**下限 100**（单卡 10 次/秒） |
| `FEISHU_PROGRESS_PRINT_STRATEGY` | `fast` | `fast` / `delay` |
| `FEISHU_PROGRESS_RESPONSE_HEADER` | `true` | 沿用 v1.x |
| `FEISHU_PROGRESS_ELEMENT_LIMIT` | `150` | 多卡拆分阈值（留 buffer，<200） |

## 测试策略

- **单测**（`tests/test_streaming_handler.py`）：累积文本、stream update（带 sequence）、打字机配置、CardKitClient（mock，含 300317/300309/200810 降级）、生命周期（start/completed/abort）、多卡拆分（150 元素触发）、晚到 delta 守卫、多 chat 路由（不串卡）
- **消息保护流式版**：撤回提问 / 撤回卡片本身 / 多卡 abort
- **端到端**：tester profile 真实飞书流式（打字机 + 终态 + abort + 多卡）
- **升级兼容**：v1.x 持久化文件（无 version）被 v2.0 读，只删消息不操作实体

## 不做（stretch / 飞书限制）

- 流式中交互回调组件（200810，MVP 无交互组件规避；CardKitClient 捕获降级）
- i18n 卡片文案（MVP 英文硬编码）
- 流式卡搜索/pin（飞书限制，关闭流式后可用）
- cron / 后台任务流式（MVP 不支持，这些场景退化为文本消息）
- 表格溢出流式版特化（多卡拆分已覆盖超长；表格特定优化 stretch）

## 权限 / 部署

- 飞书 scope：`cardkit:card:write` + `im:message:send_as_bot` + `im:message`
- 各 profile **独立 app_id**（300311 约束）
- 客户端 7.23+（自定义流式参数生效）
- v1.x → v2.0 升级：删 `card_handler.py`，改 `__init__.py`，新增 `streaming_handler.py`/`cardkit_client.py`，持久化文件加版本号，profile 重启

## 参考

- [飞书流式更新卡片](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview?lang=zh-CN)
- [流式更新文本](https://open.feishu.cn/document/cardkit-v1/card-element/content?lang=zh-CN) · [创建卡片](https://open.feishu.cn/document/cardkit-v1/card/create?lang=zh-CN) · [全量更新](https://open.feishu.cn/document/cardkit-v1/card/update?lang=zh-CN)
- [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming)
- v1.5.0 消息保护 spec（`_aborted_chats` / abort 模式复用 + 扩展多卡）
- 完整性审查：21 项 gap（seq/lock 保留 / element_id / 200 元素拆分 / 多 chat 路由 / 14 天 / 10 次秒 等）
