# Changelog

## v1.2.1 (2026-05-20)

### Fixed: 绿色 Completed 头部误加到所有消息

v1.2.0 将 `header: {Hermes · Completed, green}` 硬编码到 `_build_outbound_payload`，
导致每条 markdown 回复（包括普通对话）都显示绿色头部。改为仅在 chat 有活跃进度卡片时
才给最终回复添加绿色头部。

### Fixed: 引用回复自动创建话题

Hermes 上游使用 `root_id` 作为 `thread_id` 的回退值，导致每次引用回复都在群聊中
自动创建话题。插件新增 monkey-patch `_on_message_event`，在消息处理前将
`message.root_id` 置为 `None`，不再需要手动修改 `feishu.py`，且 Hermes 更新后
不会复发。

## v1.2.0 (2026-05-09)

### Changed: Thinking 渲染回退

Thinking 条目渲染从 markdown `<text_tag>` 回退为 `div` + `plain_text` +
`text_color: "grey"`，避免 Feishu schema 2.0 不支持 HTML 标签导致的渲染问题。
与 cc-connect 的样式保持一致。

### Fixed: 进度卡片顶部三重 hr 分隔线

截断提示 banner 后多余的 `hr` 元素与自动分隔逻辑叠加，导致卡片顶部出现三条
连续分隔线。移除手动 `hr` 追加，由分隔逻辑统一处理。

## v1.1.1 (2026-05-04)

### Fixed: 进度卡片引用回复提取只返回 `---`

v1.1.0 引入的 `_extract_card_elements` 中 `div` 分支使用了 `elif content:` 而非 `if content:`。
由于 `elem.get("text", {})` 返回空 dict（仍是 dict 实例），`elif` 分支永远不会执行，
导致 `raw_card_content` 格式的进度卡片只能提取到 `hr` 元素（`---`）。

同时增加了 `property.text.property.content` 嵌套路径处理，完整支持 `raw_card_content` API
返回的 schema 2.0 卡片结构。

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
