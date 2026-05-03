# Changelog

## 2026-05-03

### Added: Reply Chain 交互式卡片内容提取

用户在飞书中引用 bot 的交互式卡片消息时，Hermes 原本只能看到 `[Interactive message]` 占位符。

**修复**:
- Monkey-patch `_build_get_message_request`：API 请求增加 `card_msg_content_type=raw_card_content` 参数，获取卡片原始 JSON
- Monkey-patch `_extract_text_from_raw_content`：解析 `json_card` 包裹的 schema 1.0/2.0 卡片结构
- 新增 `_extract_interactive_card_text` 和 `_extract_card_elements` 辅助函数（移植自 cc-connect Go 实现）

**上游补丁**:
- `run.py` reply chain 截断从 500 字符改为不截断，与 cc-connect 行为对齐

### Changed: 保留所有 Reasoning 条目

`on_thinking()` 不再删除旧 thinking 条目，改为直接 append。
多条 reasoning 会在进度卡片中按时间顺序显示，受 `_MAX_ENTRIES=10` 截断限制。
与 cc-connect 行为对齐。

## 2026-05-02

### Changed: 精简为 1 处上游补丁

经调研确认，插件只需 `gateway/run.py` 的 1 处补丁（跳过 reasoning 拼接）。
其余 3 处补丁（run_agent.py、base.py、config.py）实际不存在也无需存在：

- **run_agent.py**: 上游自带的 `_extract_reasoning()` 和 `last_reasoning` 机制正常工作，插件通过 monkey-patch `_build_assistant_message` 独立提取 reasoning
- **base.py**: 插件直接调用 `handler.on_thinking()`，不经过 adapter 基类
- **config.py**: 插件直接 `os.environ.get()` 读取，无需上游注册

run.py 补丁不可省略：去掉后 `_patched_send` 的字符串剥离在 response 含代码块时失败率 ~30-40%。

### Removed: 表格分页逻辑

移除 `_split_large_tables()` 及相关的 `_TABLE_RE`、`_MAX_TABLE_DATA_ROWS`。
表格不可见的根因是 handler 检查导致回退到 `post` 格式（已修复），飞书卡片本身无 5 行表格限制。
分页反而会把完整的表格拆成多个小表，降低可读性。

## 2026-05-02

### Fixed: Reasoning 泄漏到正文

**现象**: 当 `show_reasoning=true` 且 reasoning 内容包含嵌入式代码块（如 `` ```html ``）时，response 正文中出现大段英文 reasoning，真正的中文回复消失。

**根因**: `run.py:5929` 将 reasoning 拼接到 response 前面（格式 `💭 **Reasoning:**\n```\n...\n```\n\n<response>`）。插件的旧正则 `_REASONING_PREFIX_RE` 用 `.*?`（非贪婪）匹配 reasoning 内容，遇到内嵌的 `` ``` `` 代码块就提前终止，只剥了一半 reasoning。

**修复（两层）**:

1. **run.py（根本）**: 当 `FEISHU_PROGRESS_STYLE=card` 时，跳过 reasoning 拼接。Card 插件已在进度卡片中显示 reasoning，正文不需要再重复。
2. **插件（兜底）**: 用 `startswith("💭 **Reasoning:**")` + `rfind("```\n\n")` 替代正则。`rfind` 找最后一个闭合标记，嵌入的代码块不影响匹配。

### Fixed: 表格 >5 行被飞书静默丢弃

**现象**: 包含超过 5 行数据的 markdown 表格在飞书卡片中完全不显示（静默丢弃，无报错）。

**根因**: 飞书 Schema 2.0 卡片的 markdown 元素对表格有隐含的行数限制。

**修复**: 新增 `_split_large_tables()` 函数，自动将大表格分页为多个独立表格（每个 ≤5 行数据行），各自重复 header + separator。

### Fixed: handler 为 None 时表格走 post 格式

**现象**: 某些场景下 `_patched_build_outbound_payload` 回退到 `post` 格式渲染 markdown，表格丢失。

**根因**: 旧代码检查 `_card_handler_instance` 是否存在才用卡片格式。当 handler 未初始化时回退到不支持表格的 post 格式。

**修复**: 移除 handler 检查，所有含 markdown 语法的响应一律用 Schema 2.0 卡片格式。

## 2026-05-01

### Fixed: Thinking 触发孤儿卡片

**现象**: 纯 reasoning（无 tool_use）事件创建了空进度卡片，处理完成后卡片无内容。

**修复**: `on_thinking` 不再调用 `_ensure_card`，仅更新已有卡片。

### Fixed: 网关重启后孤儿卡片

**现象**: 网关重启后，上次的 "Running" 进度卡片永远无法完成。

**修复**: 活跃卡片 ID 持久化到 `feishu_active_cards.json`，重启时自动清理。

### Fixed: `<text_tag>` 兼容性

**现象**: 部分飞书版本不支持 `<text_tag>` rich text 语法。

**修复**: 改用纯 markdown 粗体 `**Tool**`，兼容所有飞书版本。

### Fixed: Thinking 泄漏正文

**现象**: 模型的 response content 被误当成 reasoning 显示在卡片中。

**根因**: `run_agent.py` 使用 `assistant_message.content`（正文）当 reasoning。

**修复**: 改用 `_extract_reasoning()` 提取真正的 thinking tokens。

## 2026-04-30

### Initial release

- Monkey-patch `FeishuAdapter` + `AIAgent` 实现交互式进度卡片
- Lazy 创建卡片（首个 tool_use 时才创建）
- `_completed_chats` 防止竞态
- 无 tool 条目的卡片静默删除
- Reasoning 实时显示（灰色 notation）
- Schema 2.0 卡片渲染所有 markdown 响应
