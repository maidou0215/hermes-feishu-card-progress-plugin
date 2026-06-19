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


if __name__ == "__main__":
    unittest.main()
