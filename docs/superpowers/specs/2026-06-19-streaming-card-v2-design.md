# v2.0.0 流式卡片（CardKit Streaming）设计

- **日期**: 2026-06-19
- **状态**: 设计已批准，待实现计划
- **版本**: v2.0.0（架构级演进）
- **决策**: 全替换 + monkey-patch + MVP

## 背景

v1.x 用 Schema 2.0 interactive 消息 + 手动 PATCH（per-chat `asyncio.Lock` + 单调 seq stale-drop）实现进度卡片。最大体验代差：AI 回复是「完成后整段发」，非实时流式打字机。Cheerwhy/hermes-lark-streaming、baileyh8/hermes-feishu-streaming-card 均用飞书 CardKit 流式 API 实现了流式。

v2.0 迁移到飞书 CardKit 流式 API，实现 AI 回复实时流式打字机。

## 决策

| 维度 | 决策 | 理由 |
|---|---|---|
| **迁移策略** | **全替换** | 废弃 PATCH/interactive 渲染那套（含最复杂的 seq/lock），用 CardKit 流式 API 整体替代。飞书流式自动算增量 + 打字机，反而去掉 v1.4 最复杂的并发逻辑。代价：v1.5 验证过的 PATCH 路径废弃，不可回退 |
| **架构路线** | **monkey-patch** | 接 Hermes 流式回调（扩展 `_handle_reasoning_event` 跨线程模式）+ 调 CardKit API。与 v1.x 架构一致，轻量，无新进程/IPC。Cheerwhy 也是 monkey-patch，已验证可行 |
| **范围** | **MVP 先行** | 流式回复 + 思考流式 + footer + Response header + 消息保护。表格溢出流式版等作为 stretch |
| **打字机** | **可配置** | `FEISHU_PROGRESS_PRINT_STEP` / `_FREQUENCY`，逐字打字机或快速上屏可选 |

## 飞书 CardKit 流式 API（技术基础）

- `card/create`（`streaming_mode:true` + `streaming_config`）→ 得 `card_id`（卡片实体）
- `message/create`（`content={"type":"card","data":{"card_id":...}}`）→ 发卡片实体（**只能发一次，必须创建应用发**）
- `card-element/content` 流式更新文本（传**全量**文本，平台自动算增量 + 打字机渲染）
- `card/update` 全量更新（终态：markdown + footer + header）
- `card/settings`（`streaming_mode:false`）关闭流式（10 分钟自动关闭，建议手动关）

**硬约束**：
- JSON 2.0，要求客户端 **7.20+**（低版本降级提示）
- 流式模式期间**不支持交互回调**（需先关闭流式）
- 流式卡片**无法转发**（需关闭流式才能转）
- 卡片实体只能发一次

## 架构

```text
Hermes 流式回调 (thinking.delta / answer.delta / tool.updated / message.completed)
        │  (monkey-patch 接入，扩展 _handle_reasoning_event 跨线程模式)
        ▼
StreamingCardHandler (新，替代 FeishuCardHandler 的 PATCH 逻辑)
        │
        ├─ CardKitClient: card/create → stream update text → card/settings → 全量更新
        ├─ 累积文本状态 (card_id, streaming session, 全量回复缓冲)
        └─ 配置: print_step/frequency (打字机可配)
```

## 卡片生命周期（数据流）

| 事件 | 动作 |
|---|---|
| `on_processing_start` | `card/create`（`streaming_mode:true`）→ `message/create` 发 `card_id` |
| `thinking.delta` | stream update（灰色思考块，可折叠） |
| `tool.updated` | 局部更新（工具步骤面板） |
| `answer.delta` | `stream_update_text`（**累积全量回复**，打字机/快速可配） |
| `message.completed` | 全量更新（终态 markdown + footer + Response header）→ `card/settings` 关闭流式 |
| 失败 / abort | 全量更新（红 Failed / 灰 Aborted）→ 关闭流式 |

**关键简化**：不再需要 seq/lock/手动节流 —— 飞书流式 API 自动算增量 + 打字机。

## 组件分解（文件结构）

| 文件 | 职责 | 动作 |
|---|---|---|
| `__init__.py` | monkey-patch 装载 | **改**：接 Hermes 流式回调（`answer.delta`/`tool.updated`/`message.completed`，扩展 `_handle_reasoning_event`）；保留 root_id / agent patch；**删** `send`/`edit_message`/`_build_outbound_payload` patch |
| `streaming_handler.py` | 流式状态机 | **新建**：`StreamingCardHandler`（card_id、累积文本缓冲、streaming session、`_aborted_chats`）；复用 v1.x 的 `_build_footer_elements`/`_strip_think_tags`/`_render_*` |
| `cardkit_client.py` | CardKit API 封装 | **新建**：`create_streaming_card` / `stream_update_text` / `update_card_full` / `close_streaming`（封装 card/create、card-element/content、card/update、card/settings） |
| `card_handler.py` | v1.x PATCH 逻辑 | **删除**（PATCH 生命周期废弃；可复用部分 `_build_footer_elements`/`_strip_think_tags`/`_render_*` 移到 streaming_handler） |

## 兼容功能（流式下具体做法）

| 功能 | 流式版做法 |
|---|---|
| **footer** | `message.completed` 时 `update_card_full` 加 footer（复用 `_build_footer_elements`） |
| **消息保护** | recalled / 失败 → `close_streaming` + `update_card_full`(Aborted/Failed)。`_aborted_chats` 保留，stream update 入口守卫 |
| **Response header** | 创建时 header = Running（蓝）；`message.completed` 终态全量更新时改为 Response（turquoise）。流式卡本身即回复卡 |
| **think 剥离** | `answer.delta` 累积时 `_strip_think_tags` |
| **root_id 清除** | 不变（独立功能，`_on_message_event` patch 保留） |
| **agent 捕获** | `__setattr__` patch 保留（footer token / model 用） |
| **打字机配置** | `FEISHU_PROGRESS_PRINT_STEP`（默认 1 逐字）/ `FEISHU_PROGRESS_PRINT_FREQUENCY`（默认 70ms） |

## 配置（环境变量）

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `FEISHU_PROGRESS_STYLE` | — | v2.0 下设 `streaming` 激活流式（v1.x 的 `card` 废弃） |
| `FEISHU_PROGRESS_PRINT_STEP` | `1` | 打字机步长（1=逐字，大值=关闭逐字快速上屏） |
| `FEISHU_PROGRESS_PRINT_FREQUENCY` | `70` | 打字机频率 ms |
| `FEISHU_PROGRESS_PRINT_STRATEGY` | `fast` | `fast` / `delay` |
| `FEISHU_PROGRESS_RESPONSE_HEADER` | `true` | 沿用 v1.x |

## 测试策略

- **单测**（新建 `tests/test_streaming_handler.py`）：
  - 累积文本（delta → 全量缓冲）
  - `stream_update_text` 调用（传全量）
  - 打字机配置解析
  - `CardKitClient`（mock API：create / stream update / settings / 全量更新）
  - 生命周期（start→create / completed→全量+关闭 / abort→Aborted+关闭）
- **消息保护流式版**：abort 关闭流式 + Aborted 全量更新
- **端到端**：tester profile 真实飞书流式（打字机 + 终态 + abort）
- **回归**：footer / think 剥离 / root_id 不破坏

## 不做（stretch / 飞书限制）

- 表格溢出流式版（card entity 表格限制待验证；stretch）
- 多 bot 路由 / sidecar 架构（YAGNI）
- 流式中交互回调（飞书硬限制，不支持）

## 权限 / 部署

- 飞书 scope：`cardkit:card:write` + `im:message:send_as_bot` + `im:message`
- 各 profile app 后台开通 scope + 确认长连接（WebSocket）
- 客户端 7.20+
- v1.x → v2.0 升级：删 `card_handler.py`，更新 `__init__.py`，新增 `streaming_handler.py` / `cardkit_client.py`，profile 重启

## 参考

- [飞书流式更新卡片](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview?lang=zh-CN)
- [Cheerwhy/hermes-lark-streaming](https://github.com/Cheerwhy/hermes-lark-streaming)
- v1.5.0 消息保护 spec（`_aborted_chats` / abort 模式复用）
