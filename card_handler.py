"""
FeishuCardHandler — interactive card progress overlay for FeishuAdapter.

Self-contained card handler extracted as a Hermes plugin.  When activated
(via ``FEISHU_PROGRESS_STYLE=card`` env var), this handler replaces the
default text-based progress messages with a live-updating interactive card.

Two-message architecture (mirrors cc-connect):
  1. Progress card — lazy-created on first tool event, updated in-place
     via Patch API on every subsequent event.
  2. Final response — sent as a normal Reply message via ``send()``;
     the progress card is finalized to green "Completed" (or red "Failed")
     with a footer pointing to the next message.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("gateway.platforms.feishu")

# ---------------------------------------------------------------------------
# Constants (matching cc-connect)
# ---------------------------------------------------------------------------
_MAX_ENTRIES = 10          # cc-connect: compactProgressWriter.maxEntries
_API_TIMEOUT = 15          # seconds, cc-connect: compactProgressAPITimeout
_MAX_PREVIEW = 2000        # generous limit for tool preview

# ---------------------------------------------------------------------------
# Module-level helpers matching cc-connect's formatting logic
# ---------------------------------------------------------------------------
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")


def _sanitize_markdown_urls(text: str) -> str:
    """Convert links with non-HTTP(S) schemes to plain text."""
    def _replace(m: re.Match) -> str:
        url = m.group(2)
        if url and (url.startswith("http://") or url.startswith("https://")):
            return m.group(0)
        return f"{m.group(1)} ({url})"
    return _MD_LINK_RE.sub(_replace, text)


def _preprocess_feishu_markdown(text: str) -> str:
    """Ensure ``` has a newline before it."""
    result = []
    for i, ch in enumerate(text):
        if (ch == '`' and i + 2 < len(text)
                and text[i + 1] == '`' and text[i + 2] == '`'
                and i > 0 and text[i - 1] != '\n'):
            result.append('\n')
        result.append(ch)
    return ''.join(result)


def _format_tool_input(tool_name: str, text: str) -> str:
    """Format tool input text for card display."""
    text = (text or "").strip()
    if not text:
        return ""
    text = _sanitize_markdown_urls(text)
    if "```" in text:
        return _preprocess_feishu_markdown(text)
    if tool_name.lower() in ("bash", "shell", "run_shell_command", "terminal"):
        return f"```bash\n{text}\n```"
    if "\n" in text or len(text) > 180:
        return f"```text\n{text}\n```"
    safe = text.replace("`", "'")
    return f"`{safe}`"


def _humanize_tokens(n: Optional[int]) -> str:
    """1234 → '1.2k', 5678900 → '5.7M', None → ''."""
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


class FeishuCardHandler:
    """Composition-based handler that adds interactive card progress.

    Holds a reference to the FeishuAdapter and delegates API calls
    through it.
    """

    def __init__(self, adapter: Any, *, response_header: bool = True) -> None:
        self._a = adapter
        self._response_header = response_header
        self._active_progress_cards: Dict[str, str] = {}    # chat_id → card_msg_id
        self._progress_entries: Dict[str, List[Dict]] = {}  # chat_id → [entries]
        self._completed_chats: set = set()                   # chat_ids that finished
        self._stale_cards: Dict[str, str] = {}               # orphaned cards from previous run
        self._stale_cleanup_done = False
        self._first_response_ids: Dict[str, str] = {}        # chat_id → last response msg_id
        self._last_response_payloads: Dict[str, str] = {}    # chat_id → last interactive payload
        self._turn_start_times: Dict[str, float] = {}      # chat_id → monotonic start
        self._patch_locks: Dict[str, "asyncio.Lock"] = {}  # chat_id → PATCH serialization lock
        self._progress_seq: Dict[str, int] = {}            # chat_id → monotonic entry counter
        self._last_sent_seq: Dict[str, int] = {}           # chat_id → last PATCH seq actually sent
        self._pending_footer: Dict[str, Dict[str, Any]] = {}  # chat_id → footer kwargs, applied on Response card
        self._load_stale_cards()

    @property
    def _agent_label(self) -> str:
        return "Hermes"

    def _bump_seq(self, chat_id: str) -> int:
        """Return the next monotonic seq for this chat. Call every time
        `_progress_entries[chat_id]` is mutated so patches can be ordered."""
        seq = self._progress_seq.get(chat_id, 0) + 1
        self._progress_seq[chat_id] = seq
        return seq

    def _get_patch_lock(self, chat_id: str) -> "asyncio.Lock":
        lock = self._patch_locks.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._patch_locks[chat_id] = lock
        return lock

    # -----------------------------------------------------------------
    # Card state persistence (survives gateway restarts)
    # -----------------------------------------------------------------

    @property
    def _cards_state_path(self) -> Path:
        try:
            from hermes_constants import get_hermes_home
            return get_hermes_home() / "feishu_active_cards.json"
        except ImportError:
            return Path.home() / ".hermes" / "feishu_active_cards.json"

    def _save_active_cards(self) -> None:
        try:
            self._cards_state_path.write_text(
                json.dumps(self._active_progress_cards, ensure_ascii=False)
            )
        except Exception:
            pass

    def _load_stale_cards(self) -> None:
        try:
            path = self._cards_state_path
            if path.exists():
                self._stale_cards = json.loads(path.read_text())
                path.unlink()
                if self._stale_cards:
                    logger.info("[Card] Found %d stale card(s) from previous run",
                                len(self._stale_cards))
        except Exception:
            self._stale_cards = {}

    async def _cleanup_stale_cards(self) -> None:
        if self._stale_cleanup_done or not self._stale_cards:
            return
        self._stale_cleanup_done = True
        a = self._a
        if not a._client:
            return
        for chat_id, card_msg_id in list(self._stale_cards.items()):
            logger.info("[Card] Cleaning up stale card: %s (chat=%s)", card_msg_id, chat_id)
            await self._delete_message(card_msg_id)
        self._stale_cards.clear()

    # -----------------------------------------------------------------
    # Processing lifecycle hooks
    # -----------------------------------------------------------------

    async def on_processing_start(self, event: Any) -> None:
        a = self._a
        chat_id = event.source.chat_id
        logger.info("[Card] on_processing_start: chat_id=%s", chat_id)
        await self._cleanup_stale_cards()

        import time
        self._turn_start_times[chat_id] = time.monotonic()

        self._completed_chats.discard(chat_id)
        self._active_progress_cards.pop(chat_id, None)
        self._progress_entries.pop(chat_id, None)
        self._first_response_ids.pop(chat_id, None)
        self._last_response_payloads.pop(chat_id, None)
        self._pending_footer.pop(chat_id, None)

    async def on_processing_complete(
        self, event: Any, outcome: Any, *, agent: Any = None
    ) -> None:
        a = self._a
        chat_id = event.source.chat_id
        logger.info("[Card] on_processing_complete: outcome=%s chat_id=%s",
                     outcome, chat_id)
        self._completed_chats.add(chat_id)

        import time
        start = self._turn_start_times.pop(chat_id, None)
        duration = (time.monotonic() - start) if start is not None else None

        # Read token/model stats from the AIAgent instance, if available.
        # Attributes are optional — some agent code paths may not populate them.
        input_tokens = getattr(agent, "session_input_tokens", None) if agent else None
        output_tokens = getattr(agent, "session_output_tokens", None) if agent else None
        model = getattr(agent, "model", None) if agent else None

        from gateway.platforms.base import ProcessingOutcome
        active_card_id = self._active_progress_cards.get(chat_id)
        entries = self._progress_entries.get(chat_id, [])

        # Count tool invocations for the footer.  Bash-family tools
        # (bash/shell/terminal/run_shell_command) are broken out so users
        # can see shell activity at a glance.
        all_tools = [e for e in entries if e.get("type") == "tool_use"]
        tool_calls = len(all_tools)
        bash_calls = sum(
            1 for e in all_tools
            if (e.get("tool", "") or "").lower()
            in ("bash", "shell", "run_shell_command", "terminal")
        )

        # Stage footer data for the Response card finalize step.  Only stage
        # when we have a real chance to render it — i.e. a successful turn
        # where the response card finalize will actually fire.
        if outcome is not ProcessingOutcome.FAILURE:
            self._pending_footer[chat_id] = {
                "duration": duration,
                "model": model,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "tool_calls": tool_calls,
                "bash_calls": bash_calls,
            }

        if active_card_id:
            has_tool_entries = any(e.get("type") == "tool_use" for e in entries)
            if not has_tool_entries:
                logger.info("[Card] Deleting empty progress card (no tool entries)")
                await self._delete_message(active_card_id)
            else:
                if outcome is ProcessingOutcome.FAILURE:
                    await self._update_progress_card_failed(active_card_id, chat_id)
                else:
                    await self._update_progress_card_completed(active_card_id, chat_id)

        self._active_progress_cards.pop(chat_id, None)
        self._progress_entries.pop(chat_id, None)
        self._progress_seq.pop(chat_id, None)
        self._last_sent_seq.pop(chat_id, None)
        self._save_active_cards()

        # Add response header to the final response message to distinguish
        # it from the green "Completed" progress card above.
        if self._response_header and active_card_id and outcome is not ProcessingOutcome.FAILURE:
            await self._finalize_response_card(chat_id)

    # -----------------------------------------------------------------
    # Tool callbacks — called from monkey-patched adapter methods
    # -----------------------------------------------------------------

    async def on_tool_started(
        self, chat_id: str, tool_name: str, preview: str = ""
    ) -> Optional[str]:
        """Create/update card with tool info. Returns card message_id or None."""
        logger.info("[Card] on_tool_started: tool=%s preview=%s chat=%s",
                     tool_name, (preview or "")[:60], chat_id)

        # New turn: clear completed state so a fresh card can be created.
        # Normally on_processing_start handles this, but some gateway paths
        # (e.g. interrupt + retry) skip that callback.
        self._completed_chats.discard(chat_id)

        entries = self._progress_entries.setdefault(chat_id, [])
        entries.append({
            "type": "tool_use",
            "tool": tool_name,
            "preview": (preview or "")[:_MAX_PREVIEW],
        })
        seq = self._bump_seq(chat_id)

        active_card_id = self._active_progress_cards.get(chat_id)
        if not active_card_id:
            # Lazy-create the card (reply to user message if available)
            reply_to = getattr(self._a, "_reply_to_message_id", None)
            active_card_id = await self._send_progress_card(chat_id, reply_to=reply_to)
            if active_card_id:
                self._active_progress_cards[chat_id] = active_card_id
                self._save_active_cards()
            else:
                return None

        await self._patch_progress_card(active_card_id, chat_id, entries, seq=seq)
        return active_card_id

    async def update_entries(
        self, chat_id: str, tool_entries: list[tuple[str, str]]
    ) -> None:
        """Replace tool entries with the parsed tool list (for accumulated text).

        Unlike ``on_tool_started`` which appends, this method **replaces** the
        tool entries — necessary because the gateway's progress system
        sends accumulated text on every edit, not incremental deltas.
        Thinking entries are preserved across updates.
        """
        if chat_id in self._completed_chats:
            return

        # Preserve thinking entries (added by on_thinking) across tool updates
        existing = self._progress_entries.get(chat_id, [])
        thinking_entries = [e for e in existing if e.get("type") == "thinking"]

        self._progress_entries[chat_id] = thinking_entries + [
            {
                "type": "tool_use",
                "tool": name,
                "preview": (preview or "")[:_MAX_PREVIEW],
            }
            for name, preview in tool_entries
        ]
        seq = self._bump_seq(chat_id)

        active_card_id = self._active_progress_cards.get(chat_id)
        if active_card_id:
            await self._patch_progress_card(
                active_card_id, chat_id, self._progress_entries[chat_id], seq=seq
            )

    async def on_thinking(self, chat_id: str, text: str) -> None:
        """Update card with thinking content (grey notation text).

        Does NOT trigger card creation — only tool_use events create cards.
        This avoids orphaned "Running" cards when thinking is the only event.
        """
        if not text or not text.strip():
            return
        if chat_id in self._completed_chats:
            return

        entries = self._progress_entries.setdefault(chat_id, [])
        entries.append({
            "type": "thinking",
            "text": text.strip()[:500],
        })
        seq = self._bump_seq(chat_id)

        # Only patch if a card already exists (created by on_tool_started)
        active_card_id = self._active_progress_cards.get(chat_id)
        if active_card_id:
            await self._patch_progress_card(active_card_id, chat_id, entries, seq=seq)

    # -----------------------------------------------------------------
    # Card creation / patching / finalization
    # -----------------------------------------------------------------

    def _trim_entries(self, entries: List[Dict]) -> tuple:
        if len(entries) <= _MAX_ENTRIES:
            return entries, False
        return entries[-_MAX_ENTRIES:], True

    async def _send_progress_card(
        self, chat_id: str, reply_to: Optional[str] = None
    ) -> Optional[str]:
        a = self._a
        if not a._client:
            return None
        try:
            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": f"{self._agent_label} · Running"},
                    "template": "blue",
                },
                "body": {
                    "elements": [
                        {"tag": "markdown", "content": " "},
                    ],
                },
            }
            card_json = json.dumps(card, ensure_ascii=False)
            if reply_to:
                body = a._build_reply_message_body(
                    content=card_json,
                    msg_type="interactive",
                    reply_in_thread=False,
                    uuid_value=str(uuid.uuid4()),
                )
                request = a._build_reply_message_request(reply_to, body)
                response = await asyncio.wait_for(
                    asyncio.to_thread(a._client.im.v1.message.reply, request),
                    timeout=_API_TIMEOUT,
                )
            else:
                body = a._build_create_message_body(
                    receive_id=chat_id,
                    msg_type="interactive",
                    content=card_json,
                    uuid_value=str(uuid.uuid4()),
                )
                request = a._build_create_message_request("chat_id", body)
                response = await asyncio.wait_for(
                    asyncio.to_thread(a._client.im.v1.message.create, request),
                    timeout=_API_TIMEOUT,
                )
            msg_id = a._extract_response_field(response, "message_id")
            if msg_id:
                logger.info("[Card] Created progress card: %s", msg_id)
            return msg_id
        except asyncio.TimeoutError:
            logger.warning("[Card] _send_progress_card timed out (%ds)", _API_TIMEOUT)
            return None
        except Exception as exc:
            logger.warning("[Card] Failed to send progress card: %s", exc)
            return None

    async def _patch_progress_card(
        self, card_message_id: str, chat_id: str, entries: List[Dict],
        *, seq: Optional[int] = None,
    ) -> None:
        """PATCH the card with the given entries.

        Serialized per-chat via ``_get_patch_lock`` so concurrent
        callbacks don't race.  If *seq* is provided and is older than
        the last PATCH actually sent to this chat, the call is dropped
        — prevents an older snapshot from overwriting newer content
        when network reordering happens.
        """
        a = self._a
        if not a._client:
            return
        lock = self._get_patch_lock(chat_id)
        async with lock:
            if seq is not None and seq < self._last_sent_seq.get(chat_id, 0):
                logger.debug(
                    "[Card] Dropping stale patch seq=%d (last_sent=%d) for %s",
                    seq, self._last_sent_seq.get(chat_id, 0), chat_id,
                )
                return
            try:
                trimmed, truncated = self._trim_entries(entries)
                elements = self._render_progress_entries(trimmed, truncated)
                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text",
                                  "content": f"{self._agent_label} · Running"},
                        "template": "blue",
                    },
                    "body": {"elements": elements},
                }
                card_json = json.dumps(card, ensure_ascii=False)
                logger.debug("[Card] Patching card %s (%d entries, seq=%s)",
                             card_message_id, len(entries), seq)
                from lark_oapi.api.im.v1 import PatchMessageRequestBody, PatchMessageRequest
                body = (
                    PatchMessageRequestBody.builder()
                    .content(card_json)
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
                if seq is not None:
                    self._last_sent_seq[chat_id] = seq
            except asyncio.TimeoutError:
                logger.warning("[Card] Progress card patch timed out (%ds)", _API_TIMEOUT)
            except Exception as exc:
                logger.warning("[Card] Progress card patch error: %s", exc)

    async def _update_progress_card_completed(
        self, card_message_id: str, chat_id: str,
    ) -> None:
        a = self._a
        if not a._client:
            return
        try:
            async with self._get_patch_lock(chat_id):
                entries = self._progress_entries.get(chat_id, [])
                trimmed, truncated = self._trim_entries(entries)
                elements = self._render_progress_entries(trimmed, truncated)
                elements.append({"tag": "hr"})
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "plain_text",
                        "content": "This progress card is no longer updating. "
                                   "Full response is in the next message.",
                        "text_size": "notation",
                        "text_color": "grey",
                    },
                })
                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text",
                                  "content": f"{self._agent_label} · Completed"},
                        "template": "green",
                    },
                    "body": {"elements": elements},
                }
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
                self._last_sent_seq[chat_id] = self._progress_seq.get(chat_id, 0)
        except asyncio.TimeoutError:
            logger.warning("[Card] Completed card update timed out (%ds)", _API_TIMEOUT)
        except Exception as exc:
            logger.warning("[Card] Completed card update error: %s", exc)

    async def _update_progress_card_failed(
        self, card_message_id: str, chat_id: str
    ) -> None:
        a = self._a
        if not a._client:
            return
        async with self._get_patch_lock(chat_id):
            try:
                card = {
                    "schema": "2.0",
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text",
                                  "content": f"{self._agent_label} · Failed"},
                        "template": "red",
                    },
                    "body": {
                        "elements": [
                            {"tag": "markdown", "content": "<text_tag color='red'>Error</text_tag>\n\u274c Processing failed. Please retry."},
                            {"tag": "hr"},
                            {
                                "tag": "div",
                                "text": {
                                    "tag": "plain_text",
                                    "content": "This progress card has stopped (failed). "
                                               "See the next message for details.",
                                    "text_size": "notation",
                                    "text_color": "grey",
                                },
                            },
                        ],
                    },
                }
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
                self._last_sent_seq[chat_id] = self._progress_seq.get(chat_id, 0)
            except asyncio.TimeoutError:
                logger.warning("[Card] Failed card update timed out (%ds)", _API_TIMEOUT)
            except Exception as exc:
                logger.warning("[Card] Failed card update error: %s", exc)

    async def _delete_message(self, message_id: str) -> None:
        a = self._a
        if not a._client:
            return
        try:
            from lark_oapi.api.im.v1 import DeleteMessageRequest
            request = DeleteMessageRequest.builder().message_id(message_id).build()
            response = await asyncio.wait_for(
                asyncio.to_thread(a._client.im.v1.message.delete, request),
                timeout=_API_TIMEOUT,
            )
            if not a._response_succeeded(response):
                logger.warning("[Card] Failed to delete card %s", message_id)
        except asyncio.TimeoutError:
            logger.warning("[Card] Delete card timed out (%ds)", _API_TIMEOUT)
        except Exception as exc:
            logger.warning("[Card] Failed to delete card %s: %s", message_id, exc)

    # -----------------------------------------------------------------
    # Response card finalization (indigo header on first response msg)
    # -----------------------------------------------------------------

    def track_response_message(self, chat_id: str, message_id: str) -> None:
        """Record the last response message_id for later header patching."""
        if message_id:
            self._first_response_ids[chat_id] = message_id

    def track_response_payload(self, chat_id: str, payload: str) -> None:
        """Record the last interactive payload for later header patching."""
        if payload:
            self._last_response_payloads[chat_id] = payload

    async def _finalize_response_card(self, chat_id: str) -> None:
        """Patch the last response message with an indigo header and footer."""
        msg_id = self._first_response_ids.pop(chat_id, None)
        payload = self._last_response_payloads.pop(chat_id, None)
        footer_data = self._pending_footer.pop(chat_id, None)
        logger.info("[Card] _finalize_response_card: chat=%s msg_id=%s has_payload=%s footer=%s",
                     chat_id, msg_id, bool(payload), bool(footer_data))
        if not msg_id or not payload:
            return
        a = self._a
        if not a._client:
            return
        try:
            card = json.loads(payload)
            card["header"] = {
                "title": {"tag": "plain_text", "content": f"{self._agent_label} · Response"},
                "template": "turquoise",
            }
            # Append runtime-stats footer (duration / model / tokens) to the
            # response body.  This is where users actually look, not on the
            # ephemeral Completed card.
            if footer_data:
                footer_elements = self._build_footer_elements(**footer_data)
                if footer_elements:
                    body = card.setdefault("body", {})
                    if not isinstance(body, dict):
                        body = {}
                        card["body"] = body
                    elements = body.setdefault("elements", [])
                    if not isinstance(elements, list):
                        elements = []
                        body["elements"] = elements
                    elements.append({"tag": "hr"})
                    elements.extend(footer_elements)
            from lark_oapi.api.im.v1 import PatchMessageRequestBody, PatchMessageRequest
            patch_body = (
                PatchMessageRequestBody.builder()
                .content(json.dumps(card, ensure_ascii=False))
                .build()
            )
            patch_req = (
                PatchMessageRequest.builder()
                .message_id(msg_id)
                .request_body(patch_body)
                .build()
            )
            await asyncio.wait_for(
                asyncio.to_thread(a._client.im.v1.message.patch, patch_req),
                timeout=_API_TIMEOUT,
            )
            logger.info("[Card] Finalized response card %s with indigo header", msg_id)
        except asyncio.TimeoutError:
            logger.warning("[Card] Response card finalize timed out (%ds)", _API_TIMEOUT)
        except Exception as exc:
            logger.warning("[Card] Failed to finalize response card: %s", exc)

    # -----------------------------------------------------------------
    # Rendering helpers
    # -----------------------------------------------------------------

    @staticmethod
    def _build_footer_elements(
        *,
        duration: Optional[float],
        model: Optional[str],
        input_tokens: Optional[int],
        output_tokens: Optional[int],
        tool_calls: Optional[int] = None,
        bash_calls: Optional[int] = None,
    ) -> List[Dict]:
        """Build card elements for a runtime-stats footer.

        Returns an empty list if no data is available so callers can
        unconditionally extend the elements list.
        """
        parts: List[str] = []
        if duration is not None:
            parts.append(f"⏱ {duration:.1f}s")
        if model:
            parts.append(f"\U0001f916 {model}")
        if tool_calls is not None and tool_calls > 0:
            if bash_calls:
                parts.append(f"\U0001f527 {tool_calls} calls · bash ×{bash_calls}")
            else:
                parts.append(f"\U0001f527 {tool_calls} calls")
        in_h = _humanize_tokens(input_tokens)
        out_h = _humanize_tokens(output_tokens)
        if in_h or out_h:
            parts.append(f"↑{in_h or '0'} ↓{out_h or '0'} tokens")
        if not parts:
            return []
        content = " · ".join(parts)
        return [{
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": content,
                "text_size": "notation",
                "text_color": "grey",
            },
        }]

    @staticmethod
    def _render_progress_entries(
        entries: List[Dict], truncated: bool = False
    ) -> List[Dict]:
        elements: List[Dict] = []

        if truncated:
            elements.append({
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": "Showing latest updates only.",
                    "text_size": "notation",
                    "text_color": "grey",
                },
            })

        for entry in entries:
            entry_type = entry.get("type", "")
            if entry_type == "thinking":
                text = entry.get("text", "")
                if text:
                    safe = text.replace("`", "'")
                    elements.append({
                        "tag": "div",
                        "text": {
                            "tag": "plain_text",
                            "content": f"\U0001f4ad {safe}",
                            "text_size": "notation",
                            "text_color": "grey",
                        },
                    })
            elif entry_type == "tool_use":
                tool = entry.get("tool", "?")
                preview = entry.get("preview", "")
                safe_tool = tool.replace("`", "'")
                # Use text_tag colored labels matching cc-connect style
                content = f"<text_tag color='blue'>Tool</text_tag> `{safe_tool}`"
                body = _format_tool_input(tool, preview)
                if body:
                    content += "\n" + body
                elements.append({"tag": "markdown", "content": content})
            elif entry_type == "error":
                text = entry.get("text", "")
                if text:
                    safe = _preprocess_feishu_markdown(
                        _sanitize_markdown_urls(text)
                    )
                    content = f"<text_tag color='red'>Error</text_tag>\n{safe}"
                    elements.append({"tag": "markdown", "content": content})

        # Add hr separators between entries
        if elements:
            separated: List[Dict] = []
            for i, elem in enumerate(elements):
                separated.append(elem)
                if i < len(elements) - 1:
                    separated.append({"tag": "hr"})
            elements = separated

        if not elements:
            elements = [{"tag": "markdown", "content": " "}]
        return elements
