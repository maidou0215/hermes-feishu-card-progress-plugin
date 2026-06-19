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
    """Load card_handler.py fresh and return (handler, module)."""
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
        to_thread.assert_not_called()
        self.assertNotIn("c1", handler._last_sent_seq)


class TestAbortRendering(unittest.TestCase):
    def test_build_aborted_card_payload(self):
        handler, mod = _load_handler()
        entries = [{"type": "tool_use", "tool": "bash", "preview": "ls"}]
        card = mod.FeishuCardHandler._build_aborted_card(entries, "recalled")
        self.assertEqual(card["header"]["template"], "grey")
        self.assertIn("Aborted", card["header"]["title"]["content"])
        body = json.dumps(card, ensure_ascii=False)
        self.assertIn("User recalled", body)
        # Test agent_label parameter
        custom = mod.FeishuCardHandler._build_aborted_card([], "recalled", agent_label="CustomBot")
        self.assertIn("CustomBot · Aborted", custom["header"]["title"]["content"])

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
            def boom(*a, **kw):
                raise RuntimeError("card not found")
            with patch("asyncio.to_thread", new=MagicMock(side_effect=boom)), \
                 patch("asyncio.wait_for", new=AsyncMock()):
                await real("card1", "c1",
                           [{"type": "tool_use", "tool": "bash", "preview": "ls"}],
                           seq=1)

        asyncio.run(run())
        self.assertIn("c1", handler._aborted_chats)


class TestAbortedGuards(unittest.TestCase):
    """Task 1 added guards to 4 methods; _patch_progress_card is tested in
    TestAbortMarking. These cover the other 3 (completed/failed/finalize)."""

    def test_aborted_completed_short_circuits(self):
        handler, mod = _load_handler()
        handler._mark_aborted("c1", "recalled")
        to_thread = MagicMock()

        async def run():
            real = mod.FeishuCardHandler._update_progress_card_completed.__get__(handler)
            with patch("asyncio.to_thread", new=to_thread), \
                 patch("asyncio.wait_for", new=AsyncMock()):
                await real("card1", "c1")

        asyncio.run(run())
        to_thread.assert_not_called()

    def test_aborted_failed_short_circuits(self):
        handler, mod = _load_handler()
        handler._mark_aborted("c1", "recalled")
        to_thread = MagicMock()

        async def run():
            real = mod.FeishuCardHandler._update_progress_card_failed.__get__(handler)
            with patch("asyncio.to_thread", new=to_thread), \
                 patch("asyncio.wait_for", new=AsyncMock()):
                await real("card1", "c1")

        asyncio.run(run())
        to_thread.assert_not_called()

    def test_aborted_finalize_short_circuits(self):
        handler, mod = _load_handler()
        handler._mark_aborted("c1", "recalled")
        handler._first_response_ids["c1"] = "msg1"  # would normally proceed
        handler._last_response_payloads["c1"] = "{}"
        to_thread = MagicMock()

        async def run():
            real = mod.FeishuCardHandler._finalize_response_card.__get__(handler)
            with patch("asyncio.to_thread", new=to_thread), \
                 patch("asyncio.wait_for", new=AsyncMock()):
                await real("c1")

        asyncio.run(run())
        to_thread.assert_not_called()


class TestRecallHook(unittest.TestCase):
    """The recall hook matches the recalled message_id against the adapter's
    current _reply_to_message_id and aborts that chat."""

    def _load_init_mod(self):
        import os
        os.environ.pop("FEISHU_PROGRESS_STYLE", None)
        spec = importlib.util.spec_from_file_location(
            "feishu_card_progress_recall", _REPO_ROOT / "__init__.py"
        )
        from types import ModuleType
        mock_ch = ModuleType("card_handler")
        mock_ch.FeishuCardHandler = MagicMock()
        sys.modules["card_handler"] = mock_ch
        sys.modules["feishu_card_progress_recall.card_handler"] = mock_ch
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_recall_match_triggers_abort(self):
        import asyncio
        mod = self._load_init_mod()
        adapter = MagicMock()
        adapter._current_chat_id = "c1"
        adapter._reply_to_message_id = "om_question"
        handler = MagicMock()
        # Make abort() return a coroutine
        async def fake_abort(*a, **kw):
            pass
        handler.abort = fake_abort
        adapter._card_handler_instance = handler
        loop = MagicMock()
        loop.is_closed.return_value = False  # Important: mock loop as not closed
        data = MagicMock()
        data.event.message_id = "om_question"  # matches the question

        with patch("asyncio.run_coroutine_threadsafe") as mock_run:
            mod._handle_message_recalled(adapter, loop, data)
            mock_run.assert_called_once()

    def test_recall_non_match_no_abort(self):
        mod = self._load_init_mod()
        adapter = MagicMock()
        adapter._current_chat_id = "c1"
        adapter._reply_to_message_id = "om_question"
        handler = MagicMock()
        adapter._card_handler_instance = handler
        loop = MagicMock()
        loop.is_closed.return_value = False  # Important: mock loop as not closed
        data = MagicMock()
        data.event.message_id = "om_other"  # different message

        mod._handle_message_recalled(adapter, loop, data)

        loop.run_coroutine_threadsafe.assert_not_called()

    def test_recall_missing_context_no_raise(self):
        """helper must not raise when handler/chat_id/loop are missing."""
        mod = self._load_init_mod()
        adapter = MagicMock()
        adapter._card_handler_instance = None  # no handler
        loop = MagicMock()
        data = MagicMock()
        data.event.message_id = "om_x"
        # Should return early without raising.
        mod._handle_message_recalled(adapter, loop, data)
        loop.run_coroutine_threadsafe.assert_not_called()


class TestReplyGuard(unittest.TestCase):
    """Task 5: Reply guard — _patched_send must skip final replies for aborted chats."""

    def _load_init_mod(self):
        import os
        os.environ.pop("FEISHU_PROGRESS_STYLE", None)
        spec = importlib.util.spec_from_file_location(
            "feishu_card_progress_reply", _REPO_ROOT / "__init__.py"
        )
        from types import ModuleType
        mock_ch = ModuleType("card_handler")
        mock_ch.FeishuCardHandler = MagicMock()
        sys.modules["card_handler"] = mock_ch
        sys.modules["feishu_card_progress_reply.card_handler"] = mock_ch
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

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

        # Mock gateway.platforms.base.SendResult before loading the module
        mock_send_result = MagicMock()
        mock_send_result.success = True
        mock_gateway_base = ModuleType("gateway.platforms.base")
        mock_gateway_base.SendResult = lambda **kw: mock_send_result
        sys.modules["gateway"] = ModuleType("gateway")
        sys.modules["gateway.platforms"] = ModuleType("gateway.platforms")
        sys.modules["gateway.platforms.base"] = mock_gateway_base

        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        adapter = MagicMock()
        handler = MagicMock()
        handler._active_progress_cards = {"c1": "card1"}
        handler._aborted_chats = {"c1"}  # already aborted
        adapter._card_handler_instance = handler

        # Mock _orig_send as an async function that should NOT be called
        result_mock = MagicMock()
        result_mock.success = True
        result_mock.message_id = "msg123"

        async def mock_orig_send(*a, **kw):
            return result_mock

        orig_send = AsyncMock(side_effect=mock_orig_send)
        mod._orig_send = orig_send

        async def run():
            await mod._patched_send(adapter, "c1", "final reply text",
                                    reply_to=None, metadata=None)

        asyncio.run(run())
        orig_send.assert_not_called()

    def test_non_aborted_chat_sends_reply(self):
        """Non-aborted chat must still send the reply (no false skip)."""
        import os
        os.environ.pop("FEISHU_PROGRESS_STYLE", None)
        from types import ModuleType
        spec = importlib.util.spec_from_file_location(
            "feishu_card_progress_reply2", _REPO_ROOT / "__init__.py"
        )
        mock_ch = ModuleType("card_handler")
        mock_ch.FeishuCardHandler = MagicMock()
        sys.modules["card_handler"] = mock_ch
        sys.modules["feishu_card_progress_reply2.card_handler"] = mock_ch
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        adapter = MagicMock()
        handler = MagicMock()
        handler._active_progress_cards = {}
        handler._aborted_chats = set()  # NOT aborted
        adapter._card_handler_instance = handler

        # Mock _orig_send as an async function that SHOULD be called
        result_mock = MagicMock()
        result_mock.success = True
        result_mock.message_id = "msg123"

        async def mock_orig_send(*a, **kw):
            return result_mock

        orig_send = AsyncMock(side_effect=mock_orig_send)
        mod._orig_send = orig_send

        async def run():
            await mod._patched_send(adapter, "c1", "final reply text",
                                    reply_to=None, metadata=None)

        asyncio.run(run())
        orig_send.assert_called_once()


if __name__ == "__main__":
    unittest.main()
