# Changelog

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
