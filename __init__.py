"""
feishu-card-progress plugin — interactive card progress overlay.

Turns the default text-based tool progress messages into a live-updating
Feishu interactive card (schema 2.0).  Activated by setting the
environment variable ``FEISHU_PROGRESS_STYLE=card`` in your profile.

Architecture
~~~~~~~~~~~~

The plugin monkey-patches ``FeishuAdapter`` at ``register()`` time:

1. **on_processing_start** — cleans up stale cards, resets per-chat state.
2. **on_processing_complete** — finalizes the card (green/red header + footer).
3. **send()** — intercepts the first tool-progress ``send()`` call and
   creates a progress card instead of a text message.
4. **edit_message()** — redirects subsequent progress edits to PATCH the
   card in-place.
5. **_build_outbound_payload()** — renders final markdown responses as
   interactive cards (schema 2.0) for better formatting.

Detection of progress messages uses the tool-emoji prefix pattern
(``⚙️``, ``🔍``, etc.) that the gateway's ``progress_callback`` always
produces.  Final responses (arbitrary markdown) pass through untouched.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any, Optional

from .card_handler import FeishuCardHandler

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Progress-text detection
# ---------------------------------------------------------------------------

# Tool emoji prefixes used by gateway's progress_callback (via get_tool_emoji).
# Covers the full set from agent/display.py.
# Progress message pattern: "<emoji> <tool_name>:" or "<emoji> <tool_name>..." or "<emoji> <tool_name>(...)"
# The emoji comes from get_tool_emoji() which can be anything (skin/registry dependent).
# We detect by the structural pattern: non-word prefix + space + word + punctuation.
_PROGRESS_LINE_RE = re.compile(r'^\S+\s+\w+(?::[\s"]|\.\.\.|\s*\()')

# Regex to extract tool info from a single progress line:
#   "⚙️ bash: \"ls -la\""  →  ("bash", "ls -la")
#   "⚡ read..."            →  ("read", "")
#   "⚙️ bash (×3)"         →  ("bash", "")
_TOOL_PARSE_RE = re.compile(
    r"^\S+\s+(\w+)(?::\s*\"(.*)\")?(?:\s*\((?:×\d+)?\))?(?:\.\.\.)?$"
)

# Gateway's show_reasoning (run.py:5929) prepends a reasoning block to the
# response content when display.show_reasoning=true.  Format:
#   "💭 **Reasoning:**\n```\n<text>\n```\n\n<actual response>"
# NOTE: run.py now skips this prepend when FEISHU_PROGRESS_STYLE=card.
# The string-based stripping below is a safety fallback.
_REASONING_PREFIX = "💭 **Reasoning:**\n```"
_REASONING_SUFFIX = "```\n\n"

# Last reasoning text captured by on_thinking, used to strip from final response
_last_reasoning_text: str = ""

# Whether the agent is currently processing a request (set by on_processing_start/complete).
# When True, _patched_build_outbound_payload skips interactive conversion so streaming
# messages stay in 'post' format (Feishu rejects msg_type changes on PATCH).
_processing_active: bool = False


def _is_progress_text(content: str) -> bool:
    """Return True if *content* looks like a gateway tool-progress message."""
    if not isinstance(content, str) or not content.strip():
        return False
    first_line = content.strip().split("\n")[0]
    return bool(_PROGRESS_LINE_RE.match(first_line))


def _parse_progress_text(content: str) -> list[tuple[str, str]]:
    """Parse accumulated progress text into [(tool_name, preview), ...]."""
    entries: list[tuple[str, str]] = []
    for line in content.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        m = _TOOL_PARSE_RE.match(line)
        if m:
            entries.append((m.group(1), m.group(2) or ""))
    return entries


# ---------------------------------------------------------------------------
# Lazy card-handler accessor (set on each adapter instance)
# ---------------------------------------------------------------------------

def _get_card_handler(adapter) -> Optional[FeishuCardHandler]:
    """Return (and lazy-create) the card handler for this adapter."""
    handler = getattr(adapter, "_card_handler_instance", None)
    if handler is None:
        handler = FeishuCardHandler(adapter)
        adapter._card_handler_instance = handler
    return handler


# ---------------------------------------------------------------------------
# Cross-thread state for reasoning interception
# ---------------------------------------------------------------------------

# The agent runs in a thread pool; the gateway event loop is in the main
# thread.  We store adapter + chat_id so the agent-thread wrapper can
# schedule card updates via asyncio.run_coroutine_threadsafe.
_adapter_ref: Any = None       # FeishuAdapter instance (set per-request)
_event_loop_ref: Any = None    # Gateway event loop   (set per-request)


# ---------------------------------------------------------------------------
# Monkey-patched adapter methods
# ---------------------------------------------------------------------------

async def _patched_on_processing_start(self, event) -> None:
    """Wrap original on_processing_start + card setup."""
    global _adapter_ref, _event_loop_ref, _processing_active
    _processing_active = True

    # Store references for cross-thread reasoning interception
    self._current_chat_id = event.source.chat_id
    _adapter_ref = self
    try:
        _event_loop_ref = asyncio.get_running_loop()
    except RuntimeError:
        _event_loop_ref = None

    # Call original (adds Typing reaction)
    await _orig_on_processing_start(self, event)
    handler = _get_card_handler(self)
    await handler.on_processing_start(event)


async def _patched_on_processing_complete(self, event, outcome) -> None:
    """Wrap original on_processing_complete + card finalization."""
    global _processing_active
    _processing_active = False
    handler = _get_card_handler(self)
    await handler.on_processing_complete(event, outcome)
    # Call original (removes Typing reaction, adds failure reaction)
    await _orig_on_processing_complete(self, event, outcome)


async def _patched_send(self, chat_id, content, reply_to=None, metadata=None):
    """Intercept progress messages and create/update card instead."""
    if isinstance(content, str) and _is_progress_text(content):
        handler = _get_card_handler(self)
        entries = _parse_progress_text(content)

        # First progress message — use on_tool_started (append + create card)
        card_id = None
        for tool_name, preview in entries:
            card_id = await handler.on_tool_started(chat_id, tool_name, preview)

        if card_id:
            from gateway.platforms.base import SendResult
            return SendResult(success=True, message_id=card_id)
        # Card creation failed — fall through to normal send

    # Strip reasoning prefix from final response (already shown in progress card).
    # Gateway prepends "💭 **Reasoning:**\n```\n...\n```\n\n<response>" when
    # show_reasoning=true (run.py:5929).  Normally skipped by run.py when
    # FEISHU_PROGRESS_STYLE=card; this is a safety fallback.
    if isinstance(content, str) and content.startswith(_REASONING_PREFIX):
        # Find the LAST occurrence of the closing fence separator.
        # Using rfind avoids false matches on embedded ``` inside reasoning.
        end_pos = content.rfind(_REASONING_SUFFIX)
        if end_pos != -1:
            content = content[end_pos + len(_REASONING_SUFFIX):].strip()
        else:
            # No closing fence — strip the entire prefix marker
            content = content[len(_REASONING_PREFIX):].lstrip()
        # Fallback: if content starts with the captured reasoning text, strip it.
        if _last_reasoning_text and content.startswith(_last_reasoning_text):
            content = content[len(_last_reasoning_text):].strip()

    return await _orig_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)


async def _patched_edit_message(self, chat_id, message_id, content, *, finalize=False):
    """Redirect progress edits to card PATCH (replace, not append).

    Non-progress edits (e.g. streaming consumer updates) must use the
    original _build_outbound_payload to avoid converting a 'post' message
    to 'interactive' mid-stream — Feishu rejects msg_type changes on PATCH.
    """
    if isinstance(content, str) and _is_progress_text(content):
        handler = _get_card_handler(self)
        # Use passed chat_id, or fall back to reverse lookup from active cards
        card_chat_id = chat_id
        if not card_chat_id:
            for cid, mid in handler._active_progress_cards.items():
                if mid == message_id:
                    card_chat_id = cid
                    break
        if card_chat_id:
            # edit_message receives ACCUMULATED text — replace entries entirely
            entries = _parse_progress_text(content)
            await handler.update_entries(card_chat_id, entries)
            from gateway.platforms.base import SendResult
            return SendResult(success=True, message_id=message_id)
        # Card not found — fall through to normal edit

    # For non-progress edits (streaming, etc.), bypass _patched_build_outbound_payload
    # to keep the original msg_type (post/text).  Feishu rejects PATCH requests that
    # change msg_type (e.g. post → interactive).
    content = self.format_message(content)
    msg_type, payload = _orig_build_outbound_payload(self, content)
    body = self._build_update_message_body(msg_type=msg_type, content=payload)
    request = self._build_update_message_request(message_id=message_id, request_body=body)
    try:
        response = await asyncio.to_thread(self._client.im.v1.message.update, request)
        result = self._finalize_send_result(response, "update failed")
        if result.success:
            result.message_id = message_id
        return result
    except Exception as exc:
        logger.error("[Feishu] Failed to edit message %s: %s", message_id, exc, exc_info=True)
        return SendResult(success=False, error=str(exc))


_CODE_BLOCK_RE = re.compile(r'(```[a-z_]*\n.*?```)', re.DOTALL)


def _split_content_to_elements(content: str) -> list:
    """Split content into card elements, separating code blocks from text."""
    parts = _CODE_BLOCK_RE.split(content)
    elements = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        elements.append({"tag": "markdown", "content": part})
    return elements


def _patched_build_outbound_payload(self, content: str) -> tuple:
    """Use interactive card (schema 2.0) for markdown content.

    When _processing_active is True (agent is running), skip interactive
    conversion so streaming messages stay in 'post' format — Feishu
    rejects msg_type changes on PATCH, and the StreamConsumer creates
    the first message via send() then edits it repeatedly.

    After processing completes, the flag is cleared and the final
    non-streamed send() (if any) gets the interactive card format.
    """
    if _processing_active:
        return _orig_build_outbound_payload(self, content)

    if _MARKDOWN_HINT_RE.search(content):
        elements = _split_content_to_elements(content)
        if not elements:
            elements = [{"tag": "markdown", "content": content}]
        card = {
            "schema": "2.0",
            "config": {"wide_screen_mode": True},
            "body": {"elements": elements},
        }
        payload = json.dumps(card, ensure_ascii=False)
        logger.info(
            "[Card] build_outbound_payload: format=interactive "
            "content_len=%d payload_len=%d elements=%d preview=%.80s",
            len(content), len(payload), len(elements), content[:80],
        )
        return "interactive", payload

    orig_result = _orig_build_outbound_payload(self, content)
    logger.info(
        "[Card] build_outbound_payload: format=%s content_len=%d preview=%.80s",
        orig_result[0], len(content), content[:80],
    )
    return orig_result


# ---------------------------------------------------------------------------
# Agent-level reasoning interception
# ---------------------------------------------------------------------------

def _handle_reasoning_event(text: str) -> None:
    """Called from the agent thread when reasoning is extracted.

    Schedules the card update on the gateway event loop via
    ``asyncio.run_coroutine_threadsafe``.
    """
    global _last_reasoning_text
    adapter = _adapter_ref
    if not adapter or not text:
        return
    _last_reasoning_text = text.strip()
    handler = getattr(adapter, "_card_handler_instance", None)
    chat_id = getattr(adapter, "_current_chat_id", None)
    if not handler or not chat_id:
        return
    loop = _event_loop_ref
    if not loop or loop.is_closed():
        return
    try:
        asyncio.run_coroutine_threadsafe(
            handler.on_thinking(chat_id, text), loop
        )
    except Exception:
        pass


def _wrap_progress_callback(original_cb):
    """Wrap the gateway's progress_callback to intercept reasoning events.

    The gateway's ``progress_callback`` ignores ``reasoning.available``
    events (it only processes ``tool.started``).  This wrapper intercepts
    reasoning BEFORE the original callback drops it, and routes the text
    to the card handler.
    """
    def wrapped(event_type, *args, **kwargs):
        if event_type == "reasoning.available":
            text = kwargs.get("text", "")
            _handle_reasoning_event(text)
            # Don't forward — the original callback ignores it anyway
            return None
        return original_cb(event_type, *args, **kwargs)
    return wrapped


_orig_agent_setattr = None
_MARKDOWN_HINT_RE = re.compile(
    r"(?:\[.*?\]\(.*?\)|\*\*.*?\*\*|^\s*[-*]\s|\|.*\||```|`[^`]+`)",
    re.MULTILINE,
)


# ---------------------------------------------------------------------------
# Saved references to original methods (set during register)
# ---------------------------------------------------------------------------
_orig_on_processing_start = None
_orig_on_processing_complete = None
_orig_send = None
_orig_edit_message = None
_orig_build_outbound_payload = None
_orig_agent_setattr = None


# ---------------------------------------------------------------------------
# Plugin registration
# ---------------------------------------------------------------------------

def register(ctx) -> None:
    """Monkey-patch FeishuAdapter + Agent to add interactive card progress."""
    global _orig_on_processing_start, _orig_on_processing_complete
    global _orig_send, _orig_edit_message, _orig_build_outbound_payload
    global _orig_agent_setattr

    # Only activate when explicitly enabled
    style = os.environ.get("FEISHU_PROGRESS_STYLE", "").lower()
    if style != "card":
        logger.info("[feishu-card-progress] Plugin loaded but inactive "
                    "(set FEISHU_PROGRESS_STYLE=card to activate)")
        return

    try:
        from gateway.platforms.feishu import FeishuAdapter
    except ImportError:
        logger.warning("[feishu-card-progress] FeishuAdapter not found — "
                       "skipping registration (Feishu platform not installed)")
        return

    # Save originals
    _orig_on_processing_start = FeishuAdapter.on_processing_start
    _orig_on_processing_complete = FeishuAdapter.on_processing_complete
    _orig_send = FeishuAdapter.send
    _orig_edit_message = FeishuAdapter.edit_message
    _orig_build_outbound_payload = FeishuAdapter._build_outbound_payload

    # Apply adapter patches
    FeishuAdapter.on_processing_start = _patched_on_processing_start
    FeishuAdapter.on_processing_complete = _patched_on_processing_complete
    FeishuAdapter.send = _patched_send
    FeishuAdapter.edit_message = _patched_edit_message
    FeishuAdapter._build_outbound_payload = _patched_build_outbound_payload

    # Patch AIAgent._build_assistant_message to intercept reasoning and route to card.
    # Gateway never sets reasoning_callback, so the built-in reasoning_callback path
    # is dead for gateway mode.  We hook _build_assistant_message directly instead,
    # which is where _extract_reasoning() is called.
    try:
        from run_agent import AIAgent

        _orig_build_msg = AIAgent._build_assistant_message

        def _patched_build_assistant_message(self_agent, assistant_message, finish_reason):
            result = _orig_build_msg(self_agent, assistant_message, finish_reason)
            # Route reasoning to card handler
            try:
                reasoning = self_agent._extract_reasoning(assistant_message)
                if reasoning:
                    _handle_reasoning_event(reasoning[:500])
            except Exception as exc:
                logger.warning("[Card] _build_assistant_message reasoning extraction failed: %s", exc)
            return result

        AIAgent._build_assistant_message = _patched_build_assistant_message

        # Also wrap tool_progress_callback for reasoning.available events
        _orig_setattr = AIAgent.__setattr__

        def _patched_agent_setattr(self_agent, name, value):
            if name == "tool_progress_callback" and value is not None:
                value = _wrap_progress_callback(value)
            _orig_setattr(self_agent, name, value)

        AIAgent.__setattr__ = _patched_agent_setattr

        logger.info("[feishu-card-progress] AIAgent patched for reasoning interception")
    except ImportError as e:
        logger.debug("[feishu-card-progress] AIAgent class not found — "
                     "reasoning interception skipped: %s", e)

    logger.info("[feishu-card-progress] Activated — FeishuAdapter + Agent patched "
                "for interactive card progress with thinking support")
