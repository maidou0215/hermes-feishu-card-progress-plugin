# Changelog

## v1.1.0 (2026-05-03)

### Added: Reply Chain 交互式卡片内容提取

用户在飞书中引用 bot 的交互式卡片消息时，Hermes 原本只能看到 `[Interactive message]` 占位符。

**修复**:
- Monkey-patch `_build_get_message_request`：API 请求增加 `card_msg_content_type=raw_card_content` 参数，获取卡片原始 JSON
- Monkey-patch `_extract_text_from_raw_content`：解析 `json_card` 包裹的 schema 1.0/2.0 卡片结构
- 新增 `_extract_interactive_card_text` 和 `_extract_card_elements` 辅助函数（移植自 cc-connect Go 实现）

**上游补丁**:
- `run.py` reply chain 截断从 500 字符改为不截断，与 cc-connect 行为对齐
- 上游补丁从 1 处增加到 2 处

### Changed: 保留所有 Reasoning 条目

`on_thinking()` 不再删除旧 thinking 条目，改为直接 append。
多条 reasoning 会在进度卡片中按时间顺序显示，受 `_MAX_ENTRIES=10` 截断限制。
与 cc-connect 行为对齐。

## v1.0.0 (2026-05-01)

### Core features
- Monkey-patch `FeishuAdapter` + `AIAgent` 实现交互式进度卡片
- Lazy 创建卡片（首个 tool_use 时才创建）
- `_completed_chats` 防止竞态
- 无 tool 条目的卡片静默删除
- Reasoning 实时显示（灰色 notation）
- Schema 2.0 卡片渲染所有 markdown 响应
- 网关重启容错（活跃卡片 ID 持久化到 `feishu_active_cards.json`）

### Patches (2026-05-01 ~ 2026-05-02)

- **Reasoning 泄漏修复**: `run.py` 在 card 模式下跳过 reasoning 拼接；插件用 `startswith` + `rfind` 兜底剥离
- **表格 >5 行**: 移除分页逻辑（根因是 handler 检查导致回退 post 格式，已修复）
- **Thinking 触发孤儿卡片**: `on_thinking` 不再调用 `_ensure_card`
- **`<text_tag>` 兼容性**: 改用纯 markdown 粗体
- **Thinking 泄漏正文**: 改用 `_extract_reasoning()` 提取真正的 thinking tokens
- **精简上游补丁**: 确认只需 1 处 run.py 补丁（其余功能通过 monkey-patching 实现）
