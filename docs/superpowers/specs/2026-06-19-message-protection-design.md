# 消息保护（Message Protection）设计

- **日期**: 2026-06-19
- **状态**: 设计已批准，待实现计划
- **版本**: v1.5.0 核心功能之一
- **借鉴**: Cheerwhy/hermes-lark-streaming、hermes-feishu-streaming-card

## 背景

参考库 Cheerwhy/hermes-lark-streaming 有「消息保护」功能：消息被删除/撤回后自动终止更新，避免无效 API 调用。本插件架构不同（进度卡片 PATCH + 完成后回复，非流式卡片），需重新定位落点。

**关键约束（飞书撤回 API）**：只有机器人能撤回自己发的消息，普通用户撤不了 bot 消息（除非群管理员）。所以用户「撤回」撤的是**自己的提问消息**，撤不掉 bot 的进度卡片/回复。

因此对本插件，消息保护的真实价值：

1. 用户撤回提问 → 该对话的进度卡片更新、最终回复发送都是无效工作（对话已断）
2. PATCH 失败（卡片异常消失 / 管理员删除）→ 后续 PATCH 全 404，刷错误日志

## 目标

撤回提问或 PATCH 失败时，终止该对话的所有后续卡片操作，避免无效 API 调用和孤儿 Running 卡片。

## 范围

### 触发（主 + 被动双保险）

**主动 — 监听撤回事件**

- monkey-patch `FeishuAdapter._on_message_recalled`（Hermes `gateway/platforms/feishu.py:2460`，当前空实现只记 debug）
- 从 `data.event.message_id` 取被撤回消息 ID
- 匹配 adapter 当前请求的 `_reply_to_message_id`（用户提问消息）
- 匹配则 `abort(chat_id, reason="recalled")`（chat_id 取 adapter `_current_chat_id`）

**被动 — PATCH 失败检测**

- `_patch_progress_card` 的 except 分支（patch 报错，含卡片不存在 / 网络错误）
- 报错即 `abort(chat_id, reason="patch_failed")`（保守：patch 失败后重试无意义）

### `abort(chat_id, reason)` 行为

1. 标记 `self._aborted_chats.add(chat_id)`；幂等（已 abort 则直接返回）
2. 若有活跃进度卡片，PATCH **一次性**更新为中断态：
   - 灰色 header `Hermes · Aborted`
   - 保留已有 entries（思考 / 工具步骤）
   - 追加 hr + 灰色提示文案（按 reason）：
     - `recalled` → `⏹ User recalled the message`
     - `patch_failed` → `⏹ Card update failed, stopped`
   - 走现有 `_get_patch_lock(chat_id)` 串行，**之后不再 PATCH**
3. 清理该 chat 的回复相关状态：不发最终回复、不 finalize response header

### 入口守卫

以下方法开头检查 `chat_id in self._aborted_chats`，命中则直接 return（跳过）：

- `_patch_progress_card`
- `_update_progress_card_completed`
- `_update_progress_card_failed`
- `_finalize_response_card`
- `_patched_send` 的**最终回复分支**（progress text 分支不受影响，进度已在卡片里）

### 线程安全

- `_on_message_recalled` 在 SDK 回调线程运行（非 async）
- `abort` 只做 `set.add`（原子）+ 通过 `run_coroutine_threadsafe` 调度一次性中断态 PATCH（复用 `_handle_reasoning_event` 同款跨线程模式）
- 中断态 PATCH 走 `_get_patch_lock(chat_id)`，与正常 PATCH 串行

## 不做（YAGNI）

- bot 自己撤回消息（飞书只允许 bot 撤自己的，场景极少）
- 流式文本中断（那是 v2.0 CardKit 流式架构的事）
- 撤回后停 Hermes 的 LLM processing（插件层做不到，只能控制发不发消息）
- 多 chat 跨 adapter 的撤回关联（单 adapter 单当前请求模型，足够）

## 测试计划（TDD）

新增 `tests/test_message_protection.py`，覆盖：

1. recalled 事件 message_id 匹配提问 → chat 进入 `_aborted_chats`，reason=recalled
2. recalled 事件 message_id 不匹配 → 无影响
3. `_patch_progress_card` 报错 → chat 进入 `_aborted_chats`，reason=patch_failed
4. 已 abort 再 abort → 幂等，不重复 PATCH 中断态
5. aborted 后 `_patch_progress_card` / `_finalize_response_card` 被跳过（不调飞书 API）
6. aborted 后 `_patched_send` 回复分支被跳过
7. abort 时进度卡片被 PATCH 成中断态（灰 header + 对应文案）
8. 现有功能回归（footer / think / PATCH 锁）不受影响

测试用 mock adapter + mock client（不真实调飞书），与现有 `tests/test_card_handler.py` 风格一致（stdlib unittest）。

## 参考

- Cheerwhy/hermes-lark-streaming README「消息保护」
- 飞书撤回 API 约束（只有 bot 能撤自己的消息）
- Hermes `FeishuAdapter._on_message_recalled`（`gateway/platforms/feishu.py:2460`）
- 本插件 `card_handler.py` / `__init__.py` monkey-patch 架构
