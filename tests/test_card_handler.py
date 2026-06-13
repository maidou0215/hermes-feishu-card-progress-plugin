"""Unit tests for feishu-card-progress pure logic.

Uses stdlib unittest only — no pytest dependency. Run with:

    python -m unittest tests.test_card_handler -v
"""

import os
import sys
import unittest
from pathlib import Path

# Make repo importable without activating the plugin (FEISHU_PROGRESS_STYLE unset).
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))
os.environ.pop("FEISHU_PROGRESS_STYLE", None)

# The plugin directory is named `feishu-card-progress` (hyphen), which is not
# a valid Python identifier.  Load __init__.py via importlib to test its
# pure functions without triggering register().
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "feishu_card_progress_under_test",
    _REPO_ROOT / "__init__.py",
)
_mod = importlib.util.module_from_spec(_spec)

# Mock the card_handler import to avoid ModuleNotFoundError
import sys
from types import ModuleType

# Create a mock card_handler module
mock_card_handler = ModuleType("card_handler")
mock_card_handler.FeishuCardHandler = object
sys.modules["card_handler"] = mock_card_handler
sys.modules["feishu_card_progress_under_test.card_handler"] = mock_card_handler

# Now load the module
_spec.loader.exec_module(_mod)
_strip_think_tags = _mod._strip_think_tags


class TestStripThinkTags(unittest.TestCase):
    def test_removes_closed_think_block(self):
        text = "<think" + ">internal reasoning here</think" + ">visible answer"
        self.assertEqual(_strip_think_tags(text), "visible answer")

    def test_removes_closed_thinking_block(self):
        text = "<thinking>long\nmulti\nline</thinking>answer"
        self.assertEqual(_strip_think_tags(text), "answer")

    def test_case_insensitive(self):
        text = "<THINK>reasoning</THINK>answer"
        self.assertEqual(_strip_think_tags(text), "answer")

    def test_multiline_block(self):
        text = "<think" + ">\nline1\nline2</think" + ">\nanswer"
        self.assertEqual(_strip_think_tags(text).strip(), "answer")

    def test_orphan_closing_tag_removed(self):
        text = "answer</think" + ">tail"
        self.assertEqual(_strip_think_tags(text), "answertail")

    def test_orphan_opening_tag_removed(self):
        # No closing tag — strip the bare opening tag, keep text after.
        text = "answer<think" + ">more"
        self.assertEqual(_strip_think_tags(text), "answermore")

    def test_no_tags_unchanged(self):
        text = "plain answer with **markdown** and `code`"
        self.assertEqual(_strip_think_tags(text), text)

    def test_multiple_blocks(self):
        text = "<think" + ">a</think" + ">mid<think" + ">b</think" + ">end"
        self.assertEqual(_strip_think_tags(text), "midend")


class TestPatchStaleDrop(unittest.TestCase):
    """Verify that out-of-order patches are dropped to prevent the
    'old snapshot overwrites new content' bug (mirrors issue #31
    from hermes-feishu-streaming-card)."""

    def _make_handler(self):
        # Load card_handler.py from file (directory name has a hyphen).
        spec = importlib.util.spec_from_file_location(
            "card_handler_under_test",
            _REPO_ROOT / "card_handler.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        adapter = MagicMock()
        adapter._client = MagicMock()
        handler = mod.FeishuCardHandler(adapter)
        return handler, mod

    def test_seq_monotonic_per_chat(self):
        handler, _ = self._make_handler()
        s1 = handler._bump_seq("c1")
        s2 = handler._bump_seq("c1")
        s3 = handler._bump_seq("c2")
        self.assertEqual((s1, s2, s3), (1, 2, 1))

    def test_locks_are_per_chat(self):
        handler, _ = self._make_handler()
        l1 = handler._get_patch_lock("c1")
        l2 = handler._get_patch_lock("c2")
        l1b = handler._get_patch_lock("c1")
        self.assertIsNot(l1, l2)
        self.assertIs(l1, l1b)

    def test_stale_patch_dropped(self):
        """A patch with seq < last_sent_seq is dropped without calling Feishu."""
        handler, mod = self._make_handler()
        handler._last_sent_seq["c1"] = 5

        async def run():
            # Use the REAL _patch_progress_card to verify drop logic.
            real_patch = mod.FeishuCardHandler._patch_progress_card.__get__(handler)
            with patch.object(
                handler, "_render_progress_entries", return_value=[]
            ), patch(
                "asyncio.wait_for", new=AsyncMock()
            ), patch(
                "asyncio.to_thread", new=MagicMock()
            ):
                # Patch should be dropped because seq=3 < last_sent=5.
                await real_patch("msg_id", "c1", [], seq=3)

        asyncio.run(run())
        # last_sent_seq should remain 5 (not downgraded to 3).
        self.assertEqual(handler._last_sent_seq.get("c1"), 5)


if __name__ == "__main__":
    unittest.main()
