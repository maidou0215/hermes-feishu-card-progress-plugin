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


class FeishuCardHandler:
    """Composition-based handler that adds interactive card progress.

    Holds a reference to the FeishuAdapter and delegates API calls
    through it.
    """

    def __init__(self, adapter: Any) -> None:
        self._a = adapter
        self._active_progress_cards: Dict[str, str] = {}    # chat_id → card_msg_id
        self._progress_entries: Dict[str, List[Dict]] = {}  # chat_id → [entries]
        self._completed_chats: set = set()                   # chat_ids that finished
        self._stale_cards: Dict[str, str] = {}               # orphaned cards from previous run
        self._stale_cleanup_done = False
        self._load_stale_cards()

    @property
    def _agent_label(self) -> str:
        return "Hermes"

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
        logger.info("[Card] on_processing_start: chat_id=%s", event.source.chat_id)
        await self._cleanup_stale_cards()

        message_id = event.message_id
        if not message_id:
            return

        chat_id = event.source.chat_id
        self._completed_chats.discard(chat_id)
        self._active_progress_cards.pop(chat_id, None)
        self._progress_entries.pop(chat_id, None)

    async def on_processing_complete(self, event: Any, outcome: Any) -> None:
        a = self._a
        logger.info("[Card] on_processing_complete: outcome=%s chat_id=%s",
                     outcome, event.source.chat_id)

        message_id = event.message_id
        if not message_id:
            return

        chat_id = event.source.chat_id
        self._completed_chats.add(chat_id)

        active_card_id = self._active_progress_cards.get(chat_id)
        entries = self._progress_entries.get(chat_id, [])

        if active_card_id:
            has_tool_entries = any(e.get("type") == "tool_use" for e in entries)
            if not has_tool_entries:
                logger.info("[Card] Deleting empty progress card (no tool entries)")
                await self._delete_message(active_card_id)
            else:
                from gateway.platforms.base import ProcessingOutcome
                if outcome is ProcessingOutcome.FAILURE:
                    await self._update_progress_card_failed(active_card_id, chat_id)
                else:
                    await self._update_progress_card_completed(active_card_id, chat_id)

        self._active_progress_cards.pop(chat_id, None)
        self._progress_entries.pop(chat_id, None)
        self._save_active_cards()

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

        active_card_id = self._active_progress_cards.get(chat_id)
        if not active_card_id:
            # Lazy-create the card
            active_card_id = await self._send_progress_card(chat_id)
            if active_card_id:
                self._active_progress_cards[chat_id] = active_card_id
                self._save_active_cards()
            else:
                return None

        await self._patch_progress_card(active_card_id, chat_id, entries)
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

        active_card_id = self._active_progress_cards.get(chat_id)
        if active_card_id:
            await self._patch_progress_card(
                active_card_id, chat_id, self._progress_entries[chat_id]
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
        # Replace any existing thinking entry (only keep latest)
        entries = [e for e in entries if e.get("type") != "thinking"]
        entries.append({
            "type": "thinking",
            "text": text.strip()[:500],
        })
        self._progress_entries[chat_id] = entries

        # Only patch if a card already exists (created by on_tool_started)
        active_card_id = self._active_progress_cards.get(chat_id)
        if active_card_id:
            await self._patch_progress_card(active_card_id, chat_id, entries)

    # -----------------------------------------------------------------
    # Card creation / patching / finalization
    # -----------------------------------------------------------------

    def _trim_entries(self, entries: List[Dict]) -> tuple:
        if len(entries) <= _MAX_ENTRIES:
            return entries, False
        return entries[-_MAX_ENTRIES:], True

    async def _send_progress_card(self, chat_id: str) -> Optional[str]:
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
            body = a._build_create_message_body(
                receive_id=chat_id,
                msg_type="interactive",
                content=json.dumps(card, ensure_ascii=False),
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
        self, card_message_id: str, chat_id: str, entries: List[Dict]
    ) -> None:
        a = self._a
        if not a._client:
            return
        try:
            trimmed, truncated = self._trim_entries(entries)
            elements = self._render_progress_entries(trimmed, truncated)
            card = {
                "schema": "2.0",
                "config": {"wide_screen_mode": True},
                "header": {
                    "title": {"tag": "plain_text", "content": f"{self._agent_label} · Running"},
                    "template": "blue",
                },
                "body": {"elements": elements},
            }
            card_json = json.dumps(card, ensure_ascii=False)
            logger.debug("[Card] Patching card %s (%d entries)", card_message_id, len(entries))
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
        except asyncio.TimeoutError:
            logger.warning("[Card] Progress card patch timed out (%ds)", _API_TIMEOUT)
        except Exception as exc:
            logger.warning("[Card] Progress card patch error: %s", exc)

    async def _update_progress_card_completed(
        self, card_message_id: str, chat_id: str
    ) -> None:
        a = self._a
        if not a._client:
            return
        try:
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
    # Rendering helpers
    # -----------------------------------------------------------------

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
            elements.append({"tag": "hr"})

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
