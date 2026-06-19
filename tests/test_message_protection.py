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


if __name__ == "__main__":
    unittest.main()
