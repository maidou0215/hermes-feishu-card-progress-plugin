# Changelog

## v1.4.0 (2026-06-13)

### feat: runtime stats footer on completed cards

- 完成态卡片新增运行统计 footer（duration / model / input_tokens / output_tokens）
- 示例：`⏱ 4.2s · 🤖 claude-sonnet-4-6 · ↑1.2k ↓320 tokens`
- 通过 `_patched_agent_setattr` 捕获 AIAgent 实例，在 `on_processing_complete` 读取 `session_input_tokens` / `session_output_tokens` / `model`
- Duration 使用 `time.monotonic()` 在 `on_processing_start` / `on_processing_complete` 之间计算

### feat: $/<thinking> tag fallback stripping

- DeepSeek / Qwen / Moonshot 等模型偶发把原始 `$` 标签泄漏到最终回答文本中
- 在 `_patched_send` 中加 `_strip_think_tags` 兜底剥离完整块和孤立标签
- 借鉴 hermes-feishu-streaming-card 的 defensive layer

### fix: per-chat PATCH serialization + stale-drop

- 多线程回调并发 PATCH 同一卡片时存在内容回退竞态（旧快照后到覆盖新内容）
- 加 per-chat `asyncio.Lock` 串行化所有 PATCH 调用
- 加 monotonic seq counter，落后于 `last_sent_seq` 的 patch 在 lock 内被 drop
- 完成/失败终态 PATCH 同样使用该 lock，并刷新 `last_sent_seq` 防止后续 stale patch 覆盖
- 借鉴 hermes-feishu-streaming-card issue #31

### test: stdlib unittest harness

- 新增 `tests/test_card_handler.py`（stdlib `unittest`，无 pytest 依赖）
- 覆盖 `$` 剥离（8 tests）、PATCH seq stale-drop（3 tests）、footer 渲染（3 tests）
- 通过 `importlib` 加载 `__init__.py` / `card_handler.py`（目录名含连字符无法直接 import）

## v1.3.0 (2026-05-23)

### feat: retroactive response header, replace green-header mechanism

- Removed green header feature (`_pending_completed_chat` tracking variable)
- Added turquoise response header on final response cards (configurable via `FEISHU_PROGRESS_RESPONSE_HEADER`)
- Response header is applied retroactively by patching the last interactive card payload after the final answer is sent
- Simplified upstream patch guide from three patches to one (feishu.py only)
- Feature flag infrastructure for response header

### feat: table overflow handling — multi-card split + Post fallback

Feishu interactive cards have a hard limit of 5 table components per card (ErrCode 11310). Previously, responses with >5 markdown tables would fail to send, and even the gateway's plain-text fallback would fail because it re-entered the same card conversion pipeline.

**Two strategies**, switchable via `FEISHU_PROGRESS_TABLE_OVERFLOW` env var:

- **`split`** (default): Split content into multiple interactive cards, each with ≤5 tables. Each card renders at full quality. Only the first card carries `reply_to` and response header tracking.
- **`post`**: Fall back to Feishu Post message type (`md` tag) which has no table limit. Single message, simpler rendering. Same approach as cc-connect.

**New env vars:**
- `FEISHU_PROGRESS_TABLE_OVERFLOW` — `split` (default) or `post`

**Internal changes:**
- Added `_count_tables()`, `_find_table_blocks()`, `_split_content_by_tables()` for markdown table detection and splitting
- `_patched_send` now splits multi-table content into sequential `_orig_send` calls
- `_patched_build_outbound_payload` handles Post fallback when mode is `post`
- `_build_post_payload()` constructs Feishu Post JSON with `md` content tag

## v1.2.0 (2026-05-22)

### feat: reply-to user message, root_id strip, clarify suppression (#8)

- Reply messages now correctly reference the user's original message (`reply_to`)
- `root_id` stripped from inbound messages to prevent auto-creating topics in group chats
- Clarify progress messages suppressed (Hermes already sends the question directly)
- Reasoning prefix stripped from final responses (already shown in progress card)

## v1.1.0 (2026-05-21)

### feat: thinking/reasoning support in progress cards

- Intercept `reasoning.available` events and display thinking text in progress card
- Patch `AIAgent._build_assistant_message` for reasoning extraction
- Wrap `tool_progress_callback` to capture reasoning before gateway drops it

## v1.0.0 (2026-05-20)

### feat: initial interactive card progress

- Monkey-patch FeishuAdapter to use interactive cards (schema 2.0) for markdown content
- Progress card with tool step entries (create/update via PATCH)
- Code block separation in card elements
- Wide screen mode for card rendering
