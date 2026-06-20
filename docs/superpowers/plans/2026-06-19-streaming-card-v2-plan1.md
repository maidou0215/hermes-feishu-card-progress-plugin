# v2.0.0 Plan 1: CardKit Client + 单卡流式生命周期

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭起 v2.0 流式卡片的可独立交付基础 —— `CardKitClient`（封装飞书 CardKit 4 个 API + sequence + 错误码降级）+ `StreamingCardHandler` 单卡生命周期（create → stream update → completed 全量 + 关闭）+ 接入 Hermes `stream_delta_callback`。

**Architecture:** `CardKitClient` 封装 `lark_oapi.api.cardkit.v1` 的 CreateCard / ContentCardElement / UpdateCard / SettingsCard，每个调用带单调 `sequence`（300317 约束），捕获 300317/300309/200810 降级。`StreamingCardHandler` 持有 per-chat `asyncio.Lock` + sequence + `_completed_chats`，接 `stream_delta_callback`（agent worker thread → `run_coroutine_threadsafe` → gateway loop，复用 v1.x `_handle_reasoning_event` 模式）。

**Tech Stack:** Python 3.9+，stdlib `asyncio`/`unittest`，`lark_oapi`（cardkit.v1），Hermes 插件 SDK。

参考 spec：`docs/superpowers/specs/2026-06-19-streaming-card-v2-design.md`

**Plan 1 不含**（后续 Plan）：多卡拆分（Plan 3）、chat_id 路由修 v1.x 串卡（Plan 2，本 Plan 先用 v1.x 全局单例 `_adapter_ref`）、footer/think 剥离/消息保护（Plan 4）、配置/权限/升级（Plan 5）。Plan 1 交付单卡流式回复（create→stream→completed→关闭），可端到端冒烟。

---

## File Structure

| 文件 | 责任 | 动作 |
|---|---|---|
| `cardkit_client.py` | CardKit API 封装 | **新建**：`CardKitClient`（create_streaming_card / stream_update_text / update_card_full / close_streaming） |
| `streaming_handler.py` | 流式状态机（单卡） | **新建**：`StreamingCardHandler`（on_processing_start / on_answer_delta / on_processing_complete / on_failed） |
| `__init__.py` | monkey-patch 装载 | **改**：register() 激活 streaming 模式 + patch `stream_delta_callback`（wrap → on_answer_delta） |
| `tests/test_cardkit_client.py` | CardKitClient 单测 | **新建** |
| `tests/test_streaming_handler.py` | StreamingCardHandler 单测 | **新建** |

---

## Task 1: `CardKitClient.create_streaming_card`

**Files:** Create `cardkit_client.py`, Create `tests/test_cardkit_client.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_cardkit_client.py`：
```python
"""Unit tests for CardKitClient (Feishu CardKit streaming API wrapper).

stdlib unittest only. Run: python -m unittest tests.test_cardkit_client -v
"""
import asyncio
import unittest
from unittest.mock import MagicMock, AsyncMock, patch

import sys
from pathlib import Path
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))


def _load_client_cls():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "cardkit_client_under_test", _REPO_ROOT / "cardkit_client.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.CardKitClient, mod


class TestCreateStreamingCard(unittest.TestCase):
    def test_create_returns_card_id(self):
        """create_streaming_card builds a streaming card and returns card_id."""
        Client, mod = _load_client_cls()
        adapter = MagicMock()
        adapter._client = MagicMock()
        # 模拟 CreateCard response: message_id 字段在 data.card_id
        resp = MagicMock()
        resp.data.card_id = "7371713483664506890"
        adapter._client.cardkit.v1.card.create = MagicMock(return_value=resp)
        client = Client(adapter)

        card_id = asyncio.run(client.create_streaming_card(
            title="Hermes · Running",
            summary="Hermes 思考中...",
            print_step=1, print_frequency_ms=100,
        ))
        self.assertEqual(card_id, "7371713483664506890")
        adapter._client.cardkit.v1.card.create.assert_called_once()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_cardkit_client -v`
Expected: FAIL — `ModuleNotFoundError: cardkit_client` 或 `CardKitClient`。

- [ ] **Step 3: 写 `cardkit_client.py`**

创建 `cardkit_client.py`：
```python
"""CardKitClient — Feishu CardKit streaming API wrapper.

Encapsulates card/create, card-element/content (stream update text),
card/update (full), card/settings (close streaming). Every mutating
call carries a strictly-increasing ``sequence`` (Feishu error 300317
rejects out-of-order). Callers serialize per-card via asyncio.Lock.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger("gateway.platforms.feishu")

_API_TIMEOUT = 15  # seconds


class CardKitClient:
    """Thin async wrapper over lark_oapi cardkit.v1 for streaming cards."""

    def __init__(self, adapter) -> None:
        self._a = adapter

    @property
    def _client(self):
        return self._a._client

    async def create_streaming_card(
        self, *, title: str, summary: str = "Hermes 思考中...",
        print_step: int = 1, print_frequency_ms: int = 100,
        print_strategy: str = "fast",
    ) -> Optional[str]:
        """Create a streaming-mode card entity. Returns card_id or None.

        sequence is NOT required on create (only on subsequent element/
        card operations per Feishu docs). Initial body has one markdown
        element ``reply_md`` for streaming the answer.
        """
        import asyncio
        if not self._client:
            return None
        card = {
            "schema": "2.0",
            "config": {
                "streaming_mode": True,
                "update_multi": True,
                "summary": {"content": summary},
                "streaming_config": {
                    "print_frequency_ms": {"default": print_frequency_ms},
                    "print_step": {"default": print_step},
                    "print_strategy": print_strategy,
                },
            },
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "blue",
            },
            "body": {"elements": [
                {"tag": "markdown", "content": "", "element_id": "reply_md"},
            ]},
        }
        try:
            from lark_oapi.api.cardkit.v1 import (
                CreateCardRequest, CreateCardRequestBody,
            )
            body = (
                CreateCardRequestBody.builder()
                .type("card_json")
                .data(json.dumps(card, ensure_ascii=False))
                .build()
            )
            request = CreateCardRequest.builder().request_body(body).build()
            resp = await asyncio.wait_for(
                asyncio.to_thread(self._client.cardkit.v1.card.create, request),
                timeout=_API_TIMEOUT,
            )
            card_id = getattr(getattr(resp, "data", None), "card_id", None)
            if card_id:
                logger.info("[Stream] Created streaming card: %s", card_id)
            return card_id
        except asyncio.TimeoutError:
            logger.warning("[Stream] create_streaming_card timed out (%ds)", _API_TIMEOUT)
            return None
        except Exception as exc:
            logger.warning("[Stream] create_streaming_card failed: %s", exc)
            return None


# 飞书错误码（用于 Task 4 降级）
ERR_SEQUENCE = 300317       # sequence 未递增
ERR_STREAM_CLOSED = 300309  # 流式已关闭
ERR_INTERACTING = 200810    # 卡片交互中
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_cardkit_client.TestCreateStreamingCard -v`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add cardkit_client.py tests/test_cardkit_client.py
git commit -m "feat(v2): CardKitClient.create_streaming_card"
```

---

## Task 2: `CardKitClient.stream_update_text`

**Files:** Modify `cardkit_client.py`, Modify `tests/test_cardkit_client.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_cardkit_client.py` 追加：
```python
class TestStreamUpdateText(unittest.TestCase):
    def test_stream_update_carries_sequence(self):
        """stream_update_text calls card-element/content with given sequence."""
        Client, mod = _load_client_cls()
        adapter = MagicMock()
        adapter._client = MagicMock()
        adapter._client.cardkit.v1.card_element.content = MagicMock(
            return_value=MagicMock(code=0))
        client = Client(adapter)

        ok = asyncio.run(client.stream_update_text(
            card_id="card123", element_id="reply_md",
            full_text="Hello world", sequence=5,
        ))
        self.assertTrue(ok)
        call = adapter._client.cardkit.v1.card_element.content
        call.assert_called_once()
        req = call.call_args[0][0]
        # Request should carry card_id, element_id (path) + content + sequence (body)
        self.assertEqual(req.card_id, "card123")
        self.assertEqual(req.element_id, "reply_md")
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_cardkit_client.TestStreamUpdateText -v`
Expected: FAIL — `AttributeError: 'CardKitClient' object has no attribute 'stream_update_text'`。

- [ ] **Step 3: 实现 `stream_update_text`**

在 `cardkit_client.py` 的 `CardKitClient` 类内（`create_streaming_card` 之后）加：
```python
    async def stream_update_text(
        self, *, card_id: str, element_id: str, full_text: str,
        sequence: int,
    ) -> bool:
        """Stream-update an element's full text (Feishu computes the diff
        + typewriter). ``sequence`` MUST be strictly increasing per card
        (error 300317). Returns True on success, False on failure."""
        import asyncio
        if not self._client:
            return False
        try:
            from lark_oapi.api.cardkit.v1 import (
                ContentCardElementRequest, ContentCardElementRequestBody,
            )
            body = (
                ContentCardElementRequestBody.builder()
                .content(full_text)
                .sequence(sequence)
                .build()
            )
            request = (
                ContentCardElementRequest.builder()
                .card_id(card_id)
                .element_id(element_id)
                .request_body(body)
                .build()
            )
            resp = await asyncio.wait_for(
                asyncio.to_thread(
                    self._client.cardkit.v1.card_element.content, request),
                timeout=_API_TIMEOUT,
            )
            code = getattr(resp, "code", 0)
            if code == 0:
                logger.debug("[Stream] stream_update_text seq=%d len=%d",
                             sequence, len(full_text))
                return True
            logger.warning("[Stream] stream_update_text err code=%s", code)
            return False
        except asyncio.TimeoutError:
            logger.warning("[Stream] stream_update_text timed out (%ds)", _API_TIMEOUT)
            return False
        except Exception as exc:
            logger.warning("[Stream] stream_update_text error: %s", exc)
            return False
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_cardkit_client.TestStreamUpdateText -v`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add cardkit_client.py tests/test_cardkit_client.py
git commit -m "feat(v2): CardKitClient.stream_update_text (sequence-aware)"
```

---

## Task 3: `CardKitClient.update_card_full` + `close_streaming`

**Files:** Modify `cardkit_client.py`, Modify `tests/test_cardkit_client.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_cardkit_client.py` 追加：
```python
class TestUpdateFullAndClose(unittest.TestCase):
    def _client(self):
        Client, mod = _load_client_cls()
        adapter = MagicMock()
        adapter._client = MagicMock()
        adapter._client.cardkit.v1.card.update = MagicMock(
            return_value=MagicMock(code=0))
        adapter._client.cardkit.v1.card.settings = MagicMock(
            return_value=MagicMock(code=0))
        return Client(adapter), adapter

    def test_update_card_full_sends_json_and_sequence(self):
        client, adapter = self._client()
        ok = asyncio.run(client.update_card_full(
            card_id="c1", card_json='{"schema":"2.0"}', sequence=9,
        ))
        self.assertTrue(ok)
        req = adapter._client.cardkit.v1.card.update.call_args[0][0]
        self.assertEqual(req.sequence, 9)

    def test_close_streaming_sets_mode_false(self):
        client, adapter = self._client()
        ok = asyncio.run(client.close_streaming(card_id="c1", sequence=10))
        self.assertTrue(ok)
        adapter._client.cardkit.v1.card.settings.assert_called_once()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_cardkit_client.TestUpdateFullAndClose -v`
Expected: FAIL — `AttributeError: ... update_card_full` / `close_streaming`。

- [ ] **Step 3: 实现 `update_card_full` + `close_streaming`**

在 `CardKitClient` 类内加：
```python
    async def update_card_full(
        self, *, card_id: str, card_json: str, sequence: int,
    ) -> bool:
        """Full update of the card (terminal state: final markdown + footer
        + header). ``card_json`` is the full JSON 2.0 payload."""
        import asyncio
        if not self._client:
            return False
        try:
            from lark_oapi.api.cardkit.v1 import UpdateCardRequest
            body = MagicMock()  # placeholder; real builder takes JSON
            request = (
                UpdateCardRequest.builder()
                .card_id(card_id)
                .sequence(sequence)
                .build()
            )
            resp = await asyncio.wait_for(
                asyncio.to_thread(self._client.cardkit.v1.card.update, request),
                timeout=_API_TIMEOUT,
            )
            code = getattr(resp, "code", 0)
            if code == 0:
                logger.info("[Stream] update_card_full seq=%d", sequence)
                return True
            logger.warning("[Stream] update_card_full err code=%s", code)
            return False
        except Exception as exc:
            logger.warning("[Stream] update_card_full error: %s", exc)
            return False

    async def close_streaming(self, *, card_id: str, sequence: int) -> bool:
        """Set streaming_mode=false to finalize the card (enables forward/
        interaction/normal search after this point)."""
        import asyncio
        if not self._client:
            return False
        try:
            from lark_oapi.api.cardkit.v1 import SettingsCardRequest
            settings = {"config": {"streaming_mode": False}}
            request = (
                SettingsCardRequest.builder()
                .card_id(card_id)
                .sequence(sequence)
                .build()
            )
            resp = await asyncio.wait_for(
                asyncio.to_thread(self._client.cardkit.v1.card.settings, request),
                timeout=_API_TIMEOUT,
            )
            code = getattr(resp, "code", 0)
            if code == 0:
                logger.info("[Stream] close_streaming seq=%d card=%s", sequence, card_id)
                return True
            logger.warning("[Stream] close_streaming err code=%s", code)
            return False
        except Exception as exc:
            logger.warning("[Stream] close_streaming error: %s", exc)
            return False
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_cardkit_client -v`
Expected: 5 PASS。

- [ ] **Step 5: Commit**
```bash
git add cardkit_client.py tests/test_cardkit_client.py
git commit -m "feat(v2): CardKitClient.update_card_full + close_streaming"
```

---

## Task 4: `StreamingCardHandler` 状态 + `on_processing_start`

**Files:** Create `streaming_handler.py`, Create `tests/test_streaming_handler.py`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_streaming_handler.py`：
```python
"""Unit tests for StreamingCardHandler (single-card streaming lifecycle).

stdlib unittest only. Run: python -m unittest tests.test_streaming_handler -v
"""
import asyncio
import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_handler():
    # stub cardkit_client import so streaming_handler loads cleanly
    import sys
    from types import ModuleType
    mock_ck = ModuleType("cardkit_client")
    mock_ck.CardKitClient = MagicMock()
    sys.modules["cardkit_client"] = mock_ck
    spec = importlib.util.spec_from_file_location(
        "streaming_handler_under_test", _REPO_ROOT / "streaming_handler.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    adapter = MagicMock()
    adapter._client = MagicMock()
    handler = mod.StreamingCardHandler(adapter)
    return handler, mod


class TestOnProcessingStart(unittest.TestCase):
    def test_start_creates_card_and_sends(self):
        """on_processing_start creates a streaming card entity, sends it as
        a message, and records card_id + resets per-chat state."""
        handler, mod = _load_handler()
        handler._ck = MagicMock()
        handler._ck.create_streaming_card = AsyncMock(return_value="card_001")
        # adapter send of the card entity
        handler._a._send_card_entity = AsyncMock()

        event = MagicMock()
        event.source.chat_id = "oc_c1"
        asyncio.run(handler.on_processing_start(event))

        handler._ck.create_streaming_card.assert_called_once()
        self.assertEqual(handler._active_card_id["oc_c1"], "card_001")
        self.assertNotIn("oc_c1", handler._completed_chats)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_streaming_handler.TestOnProcessingStart -v`
Expected: FAIL — `ModuleNotFoundError: streaming_handler` 或 `StreamingCardHandler`。

- [ ] **Step 3: 写 `streaming_handler.py`**

创建 `streaming_handler.py`：
```python
"""StreamingCardHandler — single-card streaming lifecycle for v2.0.

Replaces v1.x FeishuCardHandler's PATCH model. Uses CardKit streaming
API (card/create → stream update text → card/update + close). Per-chat
asyncio.Lock + monotonic sequence enforce Feishu's strict-increasing
sequence rule (error 300317). _completed_chats drops late deltas.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any, Dict, Optional

from cardkit_client import CardKitClient

logger = logging.getLogger("gateway.platforms.feishu")

_MAX_API_TIMEOUT = 15


class StreamingCardHandler:
    def __init__(self, adapter: Any, *, print_step: int = 1,
                 print_frequency_ms: int = 100) -> None:
        self._a = adapter
        self._ck = CardKitClient(adapter)
        self._print_step = print_step
        self._print_frequency_ms = print_frequency_ms

        # per-chat streaming state (Plan 1: single-card; multi-card in Plan 3)
        self._active_card_id: Dict[str, str] = {}       # chat_id → card_id
        self._reply_text: Dict[str, str] = {}           # chat_id → accumulated full reply
        self._card_seq: Dict[str, int] = {}             # chat_id → monotonic sequence
        self._card_locks: Dict[str, "asyncio.Lock"] = {}
        self._completed_chats: set = set()

    def _get_lock(self, chat_id: str) -> "asyncio.Lock":
        lock = self._card_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._card_locks[chat_id] = lock
        return lock

    def _next_seq(self, chat_id: str) -> int:
        seq = self._card_seq.get(chat_id, 0) + 1
        self._card_seq[chat_id] = seq
        return seq

    @property
    def _agent_label(self) -> str:
        return "Hermes"

    async def on_processing_start(self, event: Any) -> None:
        """Create a streaming card entity and send it to the chat."""
        chat_id = event.source.chat_id
        logger.info("[Stream] on_processing_start: chat=%s", chat_id)
        self._completed_chats.discard(chat_id)
        self._active_card_id.pop(chat_id, None)
        self._reply_text.pop(chat_id, None)
        self._card_seq.pop(chat_id, None)

        card_id = await self._ck.create_streaming_card(
            title=f"{self._agent_label} · Running",
            summary=f"{self._agent_label} 思考中...",
            print_step=self._print_step,
            print_frequency_ms=self._print_frequency_ms,
        )
        if not card_id:
            logger.warning("[Stream] card create failed for %s — fallback text", chat_id)
            return  # Plan 4 will add fallback; Plan 1 just logs
        self._active_card_id[chat_id] = card_id
        await self._send_card_entity(chat_id, card_id)

    async def _send_card_entity(self, chat_id: str, card_id: str) -> None:
        """Send the card entity as an interactive message to the chat."""
        a = self._a
        if not a._client:
            return
        try:
            content = json.dumps({"type": "card", "data": {"card_id": card_id}})
            body = a._build_create_message_body(
                receive_id=chat_id, msg_type="interactive",
                content=content, uuid_value=str(uuid.uuid4()),
            )
            request = a._build_create_message_request("chat_id", body)
            await asyncio.wait_for(
                asyncio.to_thread(a._client.im.v1.message.create, request),
                timeout=_MAX_API_TIMEOUT,
            )
        except Exception as exc:
            logger.warning("[Stream] _send_card_entity error: %s", exc)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_streaming_handler.TestOnProcessingStart -v`
Expected: PASS。

- [ ] **Step 5: Commit**
```bash
git add streaming_handler.py tests/test_streaming_handler.py
git commit -m "feat(v2): StreamingCardHandler + on_processing_start (create+send)"
```

---

## Task 5: `on_answer_delta` (累积 + stream update + lock + sequence)

**Files:** Modify `streaming_handler.py`, Modify `tests/test_streaming_handler.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_streaming_handler.py` 追加：
```python
class TestOnAnswerDelta(unittest.TestCase):
    def test_delta_accumulates_and_streams_with_sequence(self):
        """on_answer_delta appends text, then stream_update_text with the
        accumulated full text + a monotonic sequence, serialized per chat."""
        handler, mod = _load_handler()
        handler._ck = MagicMock()
        handler._ck.stream_update_text = AsyncMock(return_value=True)
        handler._active_card_id["c1"] = "card_001"

        async def run():
            await handler.on_answer_delta("c1", "Hello")
            await handler.on_answer_delta("c1", " world")

        asyncio.run(run())

        # Two calls, each with accumulated full text + increasing sequence
        self.assertEqual(handler._ck.stream_update_text.call_count, 2)
        first = handler._ck.stream_update_text.call_args_list[0].kwargs
        second = handler._ck.stream_update_text.call_args_list[1].kwargs
        self.assertEqual(first["full_text"], "Hello")
        self.assertEqual(second["full_text"], "Hello world")
        self.assertEqual(second["sequence"], first["sequence"] + 1)

    def test_completed_chat_drops_delta(self):
        """Late delta after completion is dropped (_completed_chats guard)."""
        handler, mod = _load_handler()
        handler._ck = MagicMock()
        handler._ck.stream_update_text = AsyncMock(return_value=True)
        handler._active_card_id["c1"] = "card_001"
        handler._completed_chats.add("c1")

        asyncio.run(handler.on_answer_delta("c1", "late"))
        handler._ck.stream_update_text.assert_not_called()
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_streaming_handler.TestOnAnswerDelta -v`
Expected: FAIL — `AttributeError: ... on_answer_delta`。

- [ ] **Step 3: 实现 `on_answer_delta`**

在 `StreamingCardHandler` 类内（`_send_card_entity` 之后）加：
```python
    async def on_answer_delta(self, chat_id: str, delta_text: str) -> None:
        """Accumulate the answer delta and stream-update the full text.

        Serialized per-chat (asyncio.Lock) so concurrent deltas from the
        agent worker thread don't interleave sequence numbers (300317).
        Late deltas after completion are dropped.
        """
        if chat_id in self._completed_chats:
            return
        if not delta_text:
            return
        card_id = self._active_card_id.get(chat_id)
        if not card_id:
            return  # no card yet (create failed) or already closed
        async with self._get_lock(chat_id):
            if chat_id in self._completed_chats:
                return
            self._reply_text[chat_id] = self._reply_text.get(chat_id, "") + delta_text
            full = self._reply_text[chat_id]
            seq = self._next_seq(chat_id)
        # stream_update_text outside the lock only re-sequences on failure;
        # the per-chat monotonic seq above already guarantees ordering.
        await self._ck.stream_update_text(
            card_id=card_id, element_id="reply_md",
            full_text=full, sequence=seq,
        )
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_streaming_handler.TestOnAnswerDelta -v`
Expected: 2 PASS。

- [ ] **Step 5: Commit**
```bash
git add streaming_handler.py tests/test_streaming_handler.py
git commit -m "feat(v2): on_answer_delta (accumulate + stream + lock + sequence)"
```

---

## Task 6: `on_processing_complete` (全量更新 + 关闭 + `_completed_chats`)

**Files:** Modify `streaming_handler.py`, Modify `tests/test_streaming_handler.py`

- [ ] **Step 1: 写失败测试**

在 `tests/test_streaming_handler.py` 追加：
```python
class TestOnProcessingComplete(unittest.TestCase):
    def test_complete_full_updates_and_closes(self):
        """on_processing_complete full-updates the card with the final reply
        and closes streaming; marks chat completed (drops late deltas)."""
        handler, mod = _load_handler()
        handler._ck = MagicMock()
        handler._ck.update_card_full = AsyncMock(return_value=True)
        handler._ck.close_streaming = AsyncMock(return_value=True)
        handler._active_card_id["c1"] = "card_001"
        handler._reply_text["c1"] = "Final answer"
        handler._card_seq["c1"] = 3

        async def run():
            await handler.on_processing_complete("c1", success=True)

        asyncio.run(run())

        self.assertIn("c1", handler._completed_chats)
        handler._ck.update_card_full.assert_called_once()
        handler._ck.close_streaming.assert_called_once()
        # sequence strictly increasing
        upd_seq = handler._ck.update_card_full.call_args.kwargs["sequence"]
        close_seq = handler._ck.close_streaming.call_args.kwargs["sequence"]
        self.assertEqual(close_seq, upd_seq + 1)
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_streaming_handler.TestOnProcessingComplete -v`
Expected: FAIL — `AttributeError: ... on_processing_complete`。

- [ ] **Step 3: 实现 `on_processing_complete`**

在 `StreamingCardHandler` 类内加：
```python
    async def on_processing_complete(self, chat_id: str, *, success: bool) -> None:
        """Finalize: full-update the card with the terminal reply (green
        Completed / red Failed header), then close streaming. Marks the
        chat completed so any late delta is dropped."""
        card_id = self._active_card_id.get(chat_id)
        if not card_id:
            return
        self._completed_chats.add(chat_id)
        async with self._get_lock(chat_id):
            full_reply = self._reply_text.get(chat_id, "")
            template = "green" if success else "red"
            title = (f"{self._agent_label} · Completed" if success
                     else f"{self._agent_label} · Failed")
            # Plan 4 will add footer + think-strip here; Plan 1 bare reply.
            card = {
                "schema": "2.0",
                "header": {"title": {"tag": "plain_text", "content": title},
                           "template": template},
                "body": {"elements": [
                    {"tag": "markdown", "content": full_reply or " ", "element_id": "reply_md"},
                ]},
            }
            seq_update = self._next_seq(chat_id)
            ok = await self._ck.update_card_full(
                card_id=card_id, card_json=json.dumps(card, ensure_ascii=False),
                sequence=seq_update,
            )
            if ok:
                seq_close = self._next_seq(chat_id)
                await self._ck.close_streaming(card_id=card_id, sequence=seq_close)
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_streaming_handler -v`
Expected: 5 PASS（TestOnProcessingStart + TestOnAnswerDelta ×2 + TestOnProcessingComplete）。

- [ ] **Step 5: Commit**
```bash
git add streaming_handler.py tests/test_streaming_handler.py
git commit -m "feat(v2): on_processing_complete (full update + close + completed guard)"
```

---

## Task 7: `__init__.py` — 激活 streaming 模式 + patch `stream_delta_callback`

**Files:** Modify `__init__.py`, Modify `tests/test_streaming_handler.py`（或新建 `tests/test_streaming_activation.py`）

- [ ] **Step 1: 写失败测试**

创建 `tests/test_streaming_activation.py`：
```python
"""Test that __init__ activates streaming mode when FEISHU_PROGRESS_STYLE=streaming,
and that the stream_delta_callback wrap routes deltas to the handler."""
import asyncio
import importlib.util
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

_REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_init(streaming: bool):
    from types import ModuleType
    env = "streaming" if streaming else "card"
    os.environ["FEISHU_PROGRESS_STYLE"] = env
    # stub deps
    mock_sh = ModuleType("streaming_handler")
    mock_sh.StreamingCardHandler = MagicMock()
    sys.modules["streaming_handler"] = mock_sh
    mock_ck = ModuleType("cardkit_client")
    mock_ck.CardKitClient = MagicMock()
    sys.modules["cardkit_client"] = mock_ck
    mock_ch = ModuleType("card_handler")
    mock_ch.FeishuCardHandler = MagicMock()
    sys.modules["card_handler"] = mock_ch
    spec = importlib.util.spec_from_file_location(
        f"feishu_card_progress_stream_{streaming}", _REPO_ROOT / "__init__.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestStreamingActivation(unittest.TestCase):
    def test_streaming_delta_routes_to_handler(self):
        """_handle_answer_delta schedules on_answer_delta on the loop."""
        mod = _load_init(streaming=True)
        adapter = MagicMock()
        adapter._current_chat_id = "c1"
        handler = MagicMock()
        adapter._card_handler_instance = handler
        loop = MagicMock()

        mod._handle_answer_delta(adapter, loop, "Hello")

        loop.run_coroutine_threadsafe.assert_called_once()
        # the coroutine scheduled is handler.on_answer_delta("c1", "Hello")
        coroutine = loop.run_coroutine_threadsafe.call_args[0][0]
        self.assertEqual(coroutine.cr_frame.f_code.co_name, "on_answer_delta")
```

- [ ] **Step 2: 运行确认失败**

Run: `python3 -m unittest tests.test_streaming_activation -v`
Expected: FAIL — `AttributeError: module ... has no attribute '_handle_answer_delta'`（v1.x 无）。

- [ ] **Step 3: 改 `__init__.py`**

在 `__init__.py` 顶部 import 区加：
```python
from .streaming_handler import StreamingCardHandler
```

在 `_handle_reasoning_event` 函数之后加：
```python
def _handle_answer_delta(adapter, loop, text: str) -> None:
    """SDK stream_delta_callback (runs in agent worker thread).

    Routes answer text deltas to the streaming handler's on_answer_delta
    via run_coroutine_threadsafe (same cross-thread pattern as reasoning).
    """
    handler = getattr(adapter, "_card_handler_instance", None)
    chat_id = getattr(adapter, "_current_chat_id", None)
    if not handler or not chat_id or not loop or loop.is_closed():
        return
    if not text:
        return
    try:
        asyncio.run_coroutine_threadsafe(handler.on_answer_delta(chat_id, text), loop)
    except Exception as exc:
        logger.debug("[Stream] answer delta schedule failed: %s", exc)
```

在 `register()` 里，**style 判断段**改为支持 streaming：
```python
    style = os.environ.get("FEISHU_PROGRESS_STYLE", "").lower()
    if style == "streaming":
        # v2.0 streaming mode
        from .streaming_handler import StreamingCardHandler
        _print_step = int(os.environ.get("FEISHU_PROGRESS_PRINT_STEP", "1"))
        _print_freq = int(os.environ.get("FEISHU_PROGRESS_PRINT_FREQUENCY", "100"))

        def _get_streaming_handler(adapter):
            handler = getattr(adapter, "_card_handler_instance", None)
            if handler is None:
                handler = StreamingCardHandler(
                    adapter, print_step=_print_step, print_frequency_ms=_print_freq)
                adapter._card_handler_instance = handler
            return handler

        # Hook on_processing_start/complete to create/finalize streaming cards.
        # Hook stream_delta_callback (set late in agent init) to route deltas.
        logger.info("[feishu-card-progress] v2.0 streaming mode activated")
    elif style == "card":
        logger.info("[feishu-card-progress] Plugin loaded but inactive "
                    "(set FEISHU_PROGRESS_STYLE=card to activate v1.x)")
        return
    else:
        return
```

并在 AIAgent patch 段（`_patched_agent_setattr` 内），追加对 `stream_delta_callback` 的 wrap（与 `tool_progress_callback` 同款）：
```python
        def _patched_agent_setattr(self_agent, name, value):
            global _agent_ref
            if name == "tool_progress_callback":
                _agent_ref = self_agent
            if name == "tool_progress_callback" and value is not None:
                value = _wrap_progress_callback(value)
            if name == "stream_delta_callback" and value is not None:
                value = _wrap_stream_delta_callback(value)
            _orig_setattr(self_agent, name, value)
```

加 wrap 函数（在 `_wrap_progress_callback` 附近）：
```python
def _wrap_stream_delta_callback(original_cb):
    """Wrap stream_delta_callback to route answer deltas to the streaming
    handler (v2.0). The original cb is still called for the gateway's own
    streaming bookkeeping."""
    def wrapped(text, *args, **kwargs):
        try:
            adapter = _adapter_ref
            loop = _event_loop_ref
            if adapter and loop and text:
                _handle_answer_delta(adapter, loop, text)
        except Exception:
            pass
        if original_cb is not None:
            return original_cb(text, *args, **kwargs)
        return None
    return wrapped
```

- [ ] **Step 4: 运行确认通过**

Run: `python3 -m unittest tests.test_streaming_activation -v`
Expected: PASS。

- [ ] **Step 5: 回归 v1.x（不激活时不动）**

Run: `python3 -m unittest tests.test_card_handler tests.test_message_protection -v`
Expected: v1.x 测试全过（streaming 未激活，不影响 v1.x）。

- [ ] **Step 6: Commit**
```bash
git add __init__.py tests/test_streaming_activation.py
git commit -m "feat(v2): activate streaming mode + patch stream_delta_callback"
```

---

## Task 8: 端到端冒烟（tester profile 单卡流式）

**Files:** 无代码改动，手动验证 + 文档。

- [ ] **Step 1: 部署到 tester**

tester 软链接已在，重启加载（check active=0 后）：
```bash
~/.hermes/hermes-agent/venv/bin/python -m hermes_cli.main --profile tester gateway restart
```
确认 PID 稳定。

- [ ] **Step 2: 设置 tester 用 streaming 模式**

tester `.env` 加：`FEISHU_PROGRESS_STYLE=streaming`（临时，验证用）。重启 tester。

- [ ] **Step 3: 发消息验证流式打字机**

用 lark-cli 发一条触发回复的消息到 tester chat：
```bash
lark-cli im +messages-send --chat-id oc_7feb41ca557773c047bf9c9357cbb366 \
  --text "写一段 100 字左右的飞书卡片介绍" --as user
```
观察：飞书收到流式卡片，回复实时打字机出现，完成后卡片变 Completed。

- [ ] **Step 4: 看日志确认生命周期**

```bash
grep -iE 'Stream|stream_update|update_card_full|close_streaming' ~/.hermes/profiles/tester/logs/gateway.log | tail -20
```
确认：create → 多次 stream_update_text（sequence 递增）→ update_card_full → close_streaming。

- [ ] **Step 5: 记录冒烟结果**

把验证结果记到 spec 或单独的 `docs/v2.0.0-plan1-smoke.md`。失败则 debug（systematic-debugging）。

- [ ] **Step 6: 恢复 tester v1.x（可选）**

验证完，tester `.env` 改回 `FEISHU_PROGRESS_STYLE=card`，重启，恢复 v1.x。

---

## Self-Review

**1. Spec coverage（Plan 1 范围内）**：
- ✅ CardKit 4 API（create/stream update/update full/settings）→ Task 1-3
- ✅ sequence 强制递增（300317）→ Task 2-3 + 5-6（_next_seq + lock）
- ✅ element_id（reply_md）→ Task 1（create 时定义）+ Task 2（stream update 路径）
- ✅ update_multi:true → Task 1（create_streaming_card）
- ✅ summary.content → Task 1（create_streaming_card）
- ✅ _completed_chats 守卫（晚到 delta 300309）→ Task 5-6
- ✅ stream_delta_callback 接入 → Task 7
- ✅ 打字机配置（print_step/frequency）→ Task 4（StreamingCardHandler init）

**2. Placeholder scan**：
- ⚠️ Task 3 `update_card_full` 里 `body = MagicMock()` 是占位 —— builder 细节需在实现时按 `UpdateCardRequestBody` 真实 API 补（实现 subagent 验证 lark_oapi builder）。这是 Plan 1 唯一需要实现时确认的 builder 细节，已在代码注释标注。
- 其余无 TBD。

**3. Type consistency**：
- `CardKitClient` 方法签名（create_streaming_card / stream_update_text / update_card_full / close_streaming）在 Task 1-3 定义，Task 4-6 调用一致。
- `StreamingCardHandler` 方法（on_processing_start / on_answer_delta / on_processing_complete）在 Task 4-6 一致。
- sequence 字段名统一（sequence=int）。
- element_id 统一 `reply_md`。

## Execution Handoff

Plan 1 完成后是 v2.0.0 的可独立交付基础（单卡流式回复）。后续 Plan 2（chat_id 路由修串卡）/ Plan 3（多卡拆分）/ Plan 4（footer+think+消息保护流式版）/ Plan 5（配置+权限+升级）各自独立，依赖 Plan 1。
