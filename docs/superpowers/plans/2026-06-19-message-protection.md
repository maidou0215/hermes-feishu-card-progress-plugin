# 消息保护（Message Protection）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 当用户撤回提问或进度卡片 PATCH 失败时，终止该对话的所有卡片操作（停 PATCH、不发回复、不 finalize），并把进度卡片翻成灰色「Aborted」态。

**Architecture:** 在 `card_handler.py` 加 `_aborted_chats` 集合 + `abort()` async 入口 + `_build_aborted_card()` 纯函数 + `_update_progress_card_aborted()` 渲染方法；5 个卡片方法开头加守卫短路。`__init__.py` monkey-patch `FeishuAdapter._on_message_recalled`（主动触发）+ `_patched_send` 回复分支守卫。主动触发走 `run_coroutine_threadsafe` 跨线程调度。

**Tech Stack:** Python 3.9+，stdlib `asyncio`/`unittest`（无 pytest），Hermes 插件 SDK，`lark_oapi`。

参考 spec：`docs/superpowers/specs/2026-06-19-message-protection-design.md`

---

## File Structure

| 文件 | 责任 | 改动 |
|---|---|---|
| `card_handler.py` | 卡片状态机 + 渲染 | **改**：`__init__` 加 `_aborted_chats`；新增 `_mark_aborted`/`abort`/`_build_aborted_card`/`_update_progress_card_aborted`；4 个方法加守卫；`_patch_progress_card` except 触发被动 abort |
| `__init__.py` | monkey-patch 装载 | **改**：`register()` 里 patch `_on_message_recalled`（主动触发）；`_patched_send` 回复分支加守卫 |
| `tests/test_message_protection.py` | 消息保护单测 | **新建**：stdlib unittest，覆盖标记/幂等/守卫/渲染/主动/被动/回复 |

---

## Task 1: `_aborted_chats` 状态 + `_mark_aborted` + 入口守卫

**Files:**
- Modify: `card_handler.py:112`（`__init__` 加字段）
- Modify: `card_handler.py:119-124`（`_bump_seq` 后加 `_mark_aborted`）
- Modify: `card_handler.py:451` / `513` / `567` / `654`（4 方法加守卫）
- Test: `tests/test_message_protection.py`（新建）

- [ ] **Step 1: 写失败测试（新建测试文件）**

创建 `tests/test_message_protection.py`：

```python
"""Unit tests for message protection (abort on recall / patch failure).

Uses stdlib unittest only. Run with:

    python -m unittest tests.test_message_protection -v
"""

import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

import unittest

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_handler():
    """Load card_handler.py fresh and return (handler, module, cls)."""
    spec = importlib.util.spec_from_file_location(
        "card_handler_under_test", _REPO_ROOT / "card_handler.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    adapter = MagicMock()
    adapter._client = MagicMock()
    handler = mod.FeishuCardHandler(adapter)
    return handler, mod


class TestAbortMarking(unittest.TestCase):
    def test_mark_aborted_first_returns_true(self):
        handler, _ = _load_handler()
        self.assertTrue(handler._mark_aborted("c1", "recalled"))
        self.assertIn("c1", handler._aborted_chats)

    def test_mark_aborted_idempotent(self):
        handler, _ = _load_handler()
        handler._mark_aborted("c1", "recalled")
        self.assertFalse(handler._mark_aborted("c1", "recalled"))

    def test_mark_aborted_clears_footer_and_response_tracking(self):
        handler, _ = _load_handler()
        handler._pending_footer["c1"] = {"duration": 1.0}
        handler._first_response_ids["c1"] = "msg1"
        handler._mark_aborted("c1", "recalled")
        self.assertNotIn("c1", handler._pending_footer)
        self.assertNotIn("c1", handler._first_response_ids)

    def test_aborted_patch_short_circuits(self):
        """_patch_progress_card must return early when chat is aborted."""
        handler, mod = _load_handler()
        handler._mark_aborted("c1", "recalled")
        handler._active_progress_cards["c1"] = "card1"
        to_thread = MagicMock()

        async def run():
            real = mod.FeishuCardHandler._patch_progress_card.__get__(handler)
            with patch("asyncio.to_thread", new=to_thread), \
                 patch("asyncio.wait_for", new=AsyncMock()):
                await real("card1", "c1",
                           [{"type": "tool_use", "tool": "bash", "preview": "ls"}],
                           seq=1)

        asyncio.run(run())
        # Short-circuited: no PATCH sent, seq not recorded.
        to_thread.assert_not_called()
        self.assertNotIn("c1", handler._last_sent_seq)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m unittest tests.test_message_protection -v`
Expected: FAIL — `AttributeError: 'FeishuCardHandler' object has no attribute '_aborted_chats'`（或 `_mark_aborted`）。

- [ ] **Step 3: 加 `_aborted_chats` 字段**

`card_handler.py` 的 `__init__`，在 `self._response_text_len` 那行（约 112 行）之后加：

```python
        self._aborted_chats: set = set()                   # chat_ids aborted (recall / patch-fail)
```

- [ ] **Step 4: 加 `_mark_aborted` 方法**

在 `_bump_seq` 方法（约 119-124 行）之后加：

```python
    def _mark_aborted(self, chat_id: str, reason: str = "recalled") -> bool:
        """Mark a chat as aborted. Idempotent; returns True on first abort.

        Clears reply/footer tracking so no final reply is sent and no
        Response header is finalized. Downstream PATCH/finalize paths
        short-circuit via ``chat_id in self._aborted_chats``.
        """
        if chat_id in self._aborted_chats:
            return False
        self._aborted_chats.add(chat_id)
        self._pending_footer.pop(chat_id, None)
        self._first_response_ids.pop(chat_id, None)
        logger.info("[Card] Aborted chat %s (reason=%s)", chat_id, reason)
        return True
```

- [ ] **Step 5: 4 个方法加守卫**

`_patch_progress_card`（约 451 行）方法体开头（`a = self._a` 之前）加：

```python
        if chat_id in self._aborted_chats:
            return
```

`_update_progress_card_completed`（约 513 行）开头同样加。

`_update_progress_card_failed`（约 567 行）开头同样加。

`_finalize_response_card`（约 654 行）开头（`msg_id = self._first_response_ids.pop(...)` 之前）加：

```python
        if chat_id in self._aborted_chats:
            return
```

- [ ] **Step 6: 运行测试，确认通过**

Run: `python3 -m unittest tests.test_message_protection -v`
Expected: PASS（4 个测试）。

- [ ] **Step 7: 回归现有测试**

Run: `python3 -m unittest tests.test_card_handler -v`
Expected: PASS（20 个测试不受影响）。

- [ ] **Step 8: Commit**

```bash
git add card_handler.py tests/test_message_protection.py
git commit -m "feat: abort chat state + entry guards for message protection"
```

---

## Task 2: `abort()` async 入口 + `_build_aborted_card` + 中断态渲染

**Files:**
- Modify: `card_handler.py`（`_mark_aborted` 后加 `abort` / `_build_aborted_card` / `_update_progress_card_aborted`）
- Test: `tests/test_message_protection.py`（加 `TestAbortRendering`）

- [ ] **Step 1: 写失败测试**

在 `tests/test_message_protection.py` 的 `if __name__` 之前加：

```python
class TestAbortRendering(unittest.TestCase):
    def test_build_aborted_card_payload(self):
        handler, mod = _load_handler()
        entries = [{"type": "tool_use", "tool": "bash", "preview": "ls"}]
        card = mod.FeishuCardHandler._build_aborted_card(entries, "recalled")
        self.assertEqual(card["header"]["template"], "grey")
        self.assertIn("Aborted", card["header"]["title"]["content"])
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("User recalled", body)

    def test_build_aborted_card_patch_failed_reason(self):
        handler, mod = _load_handler()
        card = mod.FeishuCardHandler._build_aborted_card([], "patch_failed")
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("Card update failed", body)

    def test_abort_renders_aborted_card_once(self):
        """abort() flips an active progress card to Aborted; idempotent."""
        handler, mod = _load_handler()
        handler._active_progress_cards["c1"] = "card1"
        handler._progress_entries["c1"] = [
            {"type": "tool_use", "tool": "bash", "preview": "ls"}
        ]
        to_thread = MagicMock()

        async def run():
            with patch("asyncio.to_thread", new=to_thread), \
                 patch("asyncio.wait_for", new=AsyncMock()):
                await handler.abort("c1", "recalled")
                await handler.abort("c1", "recalled")  # idempotent

        asyncio.run(run())
        # Exactly one PATCH (the aborted-state render); second abort no-ops.
        self.assertEqual(to_thread.call_count, 1)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m unittest tests.test_message_protection.TestAbortRendering -v`
Expected: FAIL — `AttributeError: ... has no attribute '_build_aborted_card'` / `abort`。

- [ ] **Step 3: 加 `_build_aborted_card`（静态纯函数）**

在 `card_handler.py` 的 `_render_progress_entries`（约 762 行）之前加：

```python
    @staticmethod
    def _build_aborted_card(entries: List[Dict], reason: str) -> Dict:
        """Build the grey 'Aborted' card payload for a terminated chat.

        Pure function — easy to unit test without a live Feishu client.
        """
        trimmed = entries[-_MAX_ENTRIES:]
        truncated = len(entries) > _MAX_ENTRIES
        elements = FeishuCardHandler._render_progress_entries(trimmed, truncated)
        message = ("⏹ User recalled the message" if reason == "recalled"
                   else "⏹ Card update failed, stopped")
        elements.append({"tag": "hr"})
        elements.append({
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": message,
                "text_size": "notation",
                "text_color": "grey",
            },
        })
        return {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text",
                          "content": f"Hermes · Aborted"},
                "template": "grey",
            },
            "body": {"elements": elements},
        }
```

- [ ] **Step 4: 加 `abort()` async 入口 + `_update_progress_card_aborted`**

在 `_mark_aborted` 方法之后加：

```python
    async def abort(self, chat_id: str, reason: str = "recalled") -> None:
        """Abort a chat: stop all card updates and flip the active progress
        card to a grey 'Aborted' state. Idempotent.

        Safe to drive via ``run_coroutine_threadsafe`` from the SDK recall
        callback (which runs in a non-async thread).
        """
        if not self._mark_aborted(chat_id, reason):
            return  # already aborted
        active_card_id = self._active_progress_cards.get(chat_id)
        if active_card_id:
            try:
                await self._update_progress_card_aborted(active_card_id, chat_id, reason)
            except Exception as exc:
                logger.warning("[Card] Failed to render aborted card: %s", exc)

    async def _update_progress_card_aborted(
        self, card_message_id: str, chat_id: str, reason: str
    ) -> None:
        a = self._a
        if not a._client:
            return
        async with self._get_patch_lock(chat_id):
            try:
                entries = self._progress_entries.get(chat_id, [])
                card = self._build_aborted_card(entries, reason)
                from lark_oapi.api.im.v1 import PatchMessageRequestBody, PatchMessageRequest
                body = (
                    PatchMessageRequestBody.builder()
                    .content(json.dumps(card, ensure_ascii=False))
                    .build()
                )
                request = (
                    PatchMessageRequest.builder()
                    .message_id(card_message_id)
                    .request_body(body)
                    .build()
                )
                await asyncio.wait_for(
                    asyncio.to_thread(a._client.im.v1.message.patch, request),
                    timeout=_API_TIMEOUT,
                )
                logger.info("[Card] Rendered aborted card %s (reason=%s)",
                            card_message_id, reason)
            except asyncio.TimeoutError:
                logger.warning("[Card] Aborted card update timed out (%ds)", _API_TIMEOUT)
            except Exception as exc:
                logger.warning("[Card] Aborted card update error: %s", exc)
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `python3 -m unittest tests.test_message_protection.TestAbortRendering -v`
Expected: PASS（3 个测试）。

- [ ] **Step 6: Commit**

```bash
git add card_handler.py tests/test_message_protection.py
git commit -m "feat: render grey Aborted card on abort()"
```

---

## Task 3: 被动触发 — PATCH 失败时 abort

**Files:**
- Modify: `card_handler.py:508-511`（`_patch_progress_card` 的 except 分支）
- Test: `tests/test_message_protection.py`（加 `TestPassiveAbort`）

- [ ] **Step 1: 写失败测试**

在测试文件加：

```python
class TestPassiveAbort(unittest.TestCase):
    def test_patch_failure_aborts_chat(self):
        """A raising PATCH marks the chat aborted (reason=patch_failed)."""
        handler, mod = _load_handler()
        handler._active_progress_cards["c1"] = "card1"
        handler._progress_entries["c1"] = [
            {"type": "tool_use", "tool": "bash", "preview": "ls"}
        ]

        async def run():
            real = mod.FeishuCardHandler._patch_progress_card.__get__(handler)
            # First call: PATCH raises -> should abort. _update_progress_card_aborted
            # then also PATCHs (the aborted render); let it raise too so we only
            # assert the abort side-effect.
            call_count = {"n": 0}

            def boom(*a, **kw):
                call_count["n"] += 1
                raise RuntimeError("card not found")

            with patch("asyncio.to_thread", new=MagicMock(side_effect=boom)), \
                 patch("asyncio.wait_for", new=AsyncMock()):
                await real("card1", "c1",
                           [{"type": "tool_use", "tool": "bash", "preview": "ls"}],
                           seq=1)

        asyncio.run(run())
        self.assertIn("c1", handler._aborted_chats)
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m unittest tests.test_message_protection.TestPassiveAbort -v`
Expected: FAIL — chat 未进入 `_aborted_chats`（当前 except 只 log，不 abort）。

- [ ] **Step 3: 在 `_patch_progress_card` except 触发 abort**

`card_handler.py` 的 `_patch_progress_card`，把最后的两个 except（约 508-511 行）：

```python
            except asyncio.TimeoutError:
                logger.warning("[Card] Progress card patch timed out (%ds)", _API_TIMEOUT)
            except Exception as exc:
                logger.warning("[Card] Progress card patch error: %s", exc)
```

改为（在 log 之后触发被动 abort）：

```python
            except asyncio.TimeoutError:
                logger.warning("[Card] Progress card patch timed out (%ds)", _API_TIMEOUT)
                await self.abort(chat_id, "patch_failed")
            except Exception as exc:
                logger.warning("[Card] Progress card patch error: %s", exc)
                await self.abort(chat_id, "patch_failed")
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m unittest tests.test_message_protection.TestPassiveAbort -v`
Expected: PASS。

- [ ] **Step 5: 回归**

Run: `python3 -m unittest tests.test_message_protection tests.test_card_handler -v`
Expected: 全部 PASS。

- [ ] **Step 6: Commit**

```bash
git add card_handler.py tests/test_message_protection.py
git commit -m "feat: abort chat on PATCH failure (passive protection)"
```

---

## Task 4: 主动触发 — patch `_on_message_recalled`

**Files:**
- Modify: `__init__.py:737-750`（`register()` 里 root_id patch 之后，加 recalled patch）
- Test: `tests/test_message_protection.py`（加 `TestRecallHook`）

- [ ] **Step 1: 写失败测试**

在测试文件加（注意：recalled 钩子是 `__init__.py` 的 patch 逻辑，直接测模块级 helper）：

```python
class TestRecallHook(unittest.TestCase):
    """The recall hook matches the recalled message_id against the adapter's
    current _reply_to_message_id and aborts that chat."""

    def _load_init_mod(self):
        import os
        os.environ.pop("FEISHU_PROGRESS_STYLE", None)
        spec = importlib.util.spec_from_file_location(
            "feishu_card_progress_recall", _REPO_ROOT / "__init__.py"
        )
        # Stub card_handler import so __init__ loads cleanly.
        from types import ModuleType
        mock_ch = ModuleType("card_handler")
        mock_ch.FeishuCardHandler = MagicMock()
        sys.modules["card_handler"] = mock_ch
        sys.modules["feishu_card_progress_recall.card_handler"] = mock_ch
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_recall_match_triggers_abort(self):
        mod = self._load_init_mod()
        adapter = MagicMock()
        adapter._current_chat_id = "c1"
        adapter._reply_to_message_id = "om_question"
        handler = MagicMock()
        adapter._card_handler_instance = handler
        loop = MagicMock()
        data = MagicMock()
        data.event.message_id = "om_question"  # matches the question

        mod._handle_message_recalled(adapter, loop, data)

        # abort scheduled on the loop via run_coroutine_threadsafe
        loop.run_coroutine_threadsafe.assert_called_once()
        # And only when message_id matches (see next test for non-match)

    def test_recall_non_match_no_abort(self):
        mod = self._load_init_mod()
        adapter = MagicMock()
        adapter._current_chat_id = "c1"
        adapter._reply_to_message_id = "om_question"
        handler = MagicMock()
        adapter._card_handler_instance = handler
        loop = MagicMock()
        data = MagicMock()
        data.event.message_id = "om_other"  # different message

        mod._handle_message_recalled(adapter, loop, data)

        loop.run_coroutine_threadsafe.assert_not_called()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m unittest tests.test_message_protection.TestRecallHook -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_handle_message_recalled'`。

- [ ] **Step 3: 加 `_handle_message_recalled` helper + patch 注册**

在 `__init__.py` 的 `_handle_reasoning_event` 函数（约 597 行）之后加：

```python
def _handle_message_recalled(adapter, loop, data) -> None:
    """SDK recall callback (runs in a non-async thread).

    If the recalled message_id is the current request's user question
    (``_reply_to_message_id``), abort that chat so we stop PATCHing its
    progress card and skip sending a reply.
    """
    handler = getattr(adapter, "_card_handler_instance", None)
    chat_id = getattr(adapter, "_current_chat_id", None)
    if not handler or not chat_id or not loop or loop.is_closed():
        return
    try:
        event = getattr(data, "event", None)
        recalled_id = str(getattr(event, "message_id", "") or "")
    except Exception:
        return
    reply_to = str(getattr(adapter, "_reply_to_message_id", "") or "")
    if not recalled_id or recalled_id != reply_to:
        return
    logger.info("[Card] User question recalled (%s) — aborting chat %s",
                recalled_id, chat_id)
    try:
        asyncio.run_coroutine_threadsafe(handler.abort(chat_id, "recalled"), loop)
    except Exception:
        pass
```

- [ ] **Step 4: 在 `register()` 注册 recalled patch**

在 `__init__.py` 的 `register()` 里，root_id stripping patch（约 749 行 `FeishuAdapter._on_message_event = ...` 之后）加：

```python
    # Message protection: when a user recalls their question message,
    # abort that chat's card updates (stop PATCH, no reply, flip to Aborted).
    _orig_on_message_recalled = FeishuAdapter._on_message_recalled

    def _patched_on_message_recalled(self, data):
        try:
            _handle_message_recalled(self, _event_loop_ref, data)
        except Exception as exc:
            logger.debug("[Card] recall hook error: %s", exc)
        return _orig_on_message_recalled(self, data)

    FeishuAdapter._on_message_recalled = _patched_on_message_recalled
    logger.info("[feishu-card-progress] message-recall protection hooked")
```

- [ ] **Step 5: 运行测试，确认通过**

Run: `python3 -m unittest tests.test_message_protection.TestRecallHook -v`
Expected: PASS（2 个测试）。

- [ ] **Step 6: Commit**

```bash
git add __init__.py tests/test_message_protection.py
git commit -m "feat: abort chat on user question recall (active protection)"
```

---

## Task 5: 回复守卫 — `_patched_send` 跳过 aborted chat 的回复

**Files:**
- Modify: `__init__.py:403-410`（`_patched_send` 正常 send 分支前加守卫）
- Test: `tests/test_message_protection.py`（加 `TestReplyGuard`）

- [ ] **Step 1: 写失败测试**

在测试文件加：

```python
class TestReplyGuard(unittest.TestCase):
    def test_aborted_chat_skips_final_reply(self):
        """When a chat is aborted, _patched_send must not forward the final
        reply to the real Feishu send."""
        import os
        os.environ.pop("FEISHU_PROGRESS_STYLE", None)
        from types import ModuleType
        spec = importlib.util.spec_from_file_location(
            "feishu_card_progress_reply", _REPO_ROOT / "__init__.py"
        )
        mock_ch = ModuleType("card_handler")
        mock_ch.FeishuCardHandler = MagicMock()
        sys.modules["card_handler"] = mock_ch
        sys.modules["feishu_card_progress_reply.card_handler"] = mock_ch
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        adapter = MagicMock()
        handler = MagicMock()
        handler._active_progress_cards = {"c1": "card1"}
        handler._aborted_chats = {"c1"}  # already aborted
        adapter._card_handler_instance = handler

        orig_send = MagicMock()
        mod._orig_send = orig_send

        async def run():
            await mod._patched_send(adapter, "c1", "final reply text",
                                    reply_to=None, metadata=None)

        asyncio.run(run())
        # Aborted chat: real send must NOT be called for the final reply.
        orig_send.assert_not_called()
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `python3 -m unittest tests.test_message_protection.TestReplyGuard -v`
Expected: FAIL — `_orig_send` 被调用了（当前无守卫，回复照发）。

- [ ] **Step 3: 在 `_patched_send` 回复分支加守卫**

`__init__.py` 的 `_patched_send`，在 `result = await _orig_send(...)`（约 403 行）之前加守卫。找到这行：

```python
    result = await _orig_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)
```

在它**之前**插入：

```python
    # Message protection: if this chat was aborted (user recalled the
    # question / PATCH failed), do not send the final reply.
    if has_active_card and chat_id in handler._aborted_chats:
        logger.info("[Card] Skipping final reply for aborted chat %s", chat_id)
        from gateway.platforms.base import SendResult
        return SendResult(success=True)

```

（`has_active_card` 和 `handler` 已在上方约 371-372 行定义，复用即可。）

- [ ] **Step 4: 运行测试，确认通过**

Run: `python3 -m unittest tests.test_message_protection.TestReplyGuard -v`
Expected: PASS。

- [ ] **Step 5: 全量回归**

Run: `python3 -m unittest discover -s tests -v`
Expected: 所有测试 PASS（消息保护 + 现有 card_handler）。

- [ ] **Step 6: Commit**

```bash
git add __init__.py tests/test_message_protection.py
git commit -m "feat: skip final reply for aborted chats"
```

---

## Task 6: CHANGELOG + 文档收尾

**Files:**
- Modify: `CHANGELOG.md`（加 v1.5.0 条目）

- [ ] **Step 1: 加 CHANGELOG 条目**

在 `CHANGELOG.md` 顶部（`# Changelog` 之后、`## v1.4.0` 之前）加：

```markdown
## v1.5.0 (2026-06-19)

### feat: message protection (abort on recall / PATCH failure)

- 用户撤回提问（`im.message.recalled_v1`）→ 终止该对话的进度卡片更新、不发回复
- 进度卡片 PATCH 失败 → 终止后续 PATCH，避免无效 API 调用刷错误日志
- 终止时把进度卡片翻成灰色 `Hermes · Aborted` 态（保留思考/工具步骤 + 提示文案）
- 文案按原因区分：`⏹ User recalled the message` / `⏹ Card update failed, stopped`
- 借鉴 Cheerwhy/hermes-lark-streaming；因飞书约束（用户撤不了 bot 消息）落点调整为「撤回提问 + PATCH 失败」
- 新增 `tests/test_message_protection.py`（标记/幂等/守卫/渲染/主动/被动/回复）

```

- [ ] **Step 2: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: v1.5.0 changelog — message protection"
```

---

## Self-Review（写完后自查）

**1. Spec coverage**:
- ✅ 主动撤回触发 → Task 4
- ✅ 被动 PATCH 失败触发 → Task 3
- ✅ abort 三件事（标记/中断态卡片/清理）→ Task 1（标记+清理）+ Task 2（中断态卡片）
- ✅ 5 个入口守卫 → Task 1（4 个 card_handler 方法）+ Task 5（_patched_send 回复）
- ✅ 线程安全（run_coroutine_threadsafe）→ Task 4
- ✅ TDD 8 测试点 → Task 1-5 覆盖（标记/幂等/清理/patch短路/build payload×2 reason/abort幂等渲染/被动abort/recall匹配/recall不匹配/回复跳过）

**2. Placeholder scan**: 无 TBD/TODO；每个代码步骤含完整代码。

**3. Type consistency**: `_mark_aborted(chat_id, reason)` / `abort(chat_id, reason)` / `_build_aborted_card(entries, reason)` / `_update_progress_card_aborted(card_message_id, chat_id, reason)` / `_handle_message_recalled(adapter, loop, data)` — 签名在各 Task 一致。`_aborted_chats` 命名统一。reason 取值 `"recalled"` / `"patch_failed"` 统一。
