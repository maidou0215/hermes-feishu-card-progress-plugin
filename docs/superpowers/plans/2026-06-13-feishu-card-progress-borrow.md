# feishu-card-progress 借鉴优化（footer / think 过滤 / PATCH 锁）实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 借鉴 hermes-feishu-streaming-card 的高 ROI 能力，给现有 monkey-patch 插件补三件事：完成态卡片加运行统计 footer、对最终回复兜底剥离 `<think>` 标签、给 PATCH 路径加 per-chat 串行锁 + stale-drop。

**Architecture:** 全部在现有 monkey-patch 架构内完成，不引入新进程或新依赖。三件事互相独立、可独立提交。Footer 数据通过新增的 `_agent_ref` 模块级变量（在 `_patched_agent_setattr` 钩到 `tool_progress_callback` 设置时捕获）从 `AIAgent` 实例上读取；`<think>` 过滤作为 `_patched_send` 的预处理步骤；PATCH 锁以 per-chat `asyncio.Lock` + 单调 sequence 实现 stale-drop。

**Tech Stack:** Python 3.9+，标准库 `asyncio`/`re`/`time`/`unittest`（不引入 pytest），Hermes 插件 SDK，`lark_oapi`（已有）。

---

## File Structure

| 文件 | 责任 | 改动 |
|---|---|---|
| `card_handler.py` | 卡片状态机与渲染 | **改** `FeishuCardHandler`：加 `_turn_start_times`、`_patch_locks`、`_progress_seq`、`_last_sent_seq`；改 `on_processing_start/complete`、`on_tool_started`、`update_entries`、`on_thinking`、`_patch_progress_card`、`_update_progress_card_completed`、`_update_progress_card_failed`；新增 `_bump_seq`、`_build_footer_elements` |
| `__init__.py` | monkey-patch 装载 | **改**：新增模块级 `_agent_ref`；改 `_patched_agent_setattr` 捕获 agent；改 `_patched_on_processing_complete` 把 `_agent_ref` 透传给 handler；新增 `_THINK_BLOCK_RE` + `_THINK_TAG_RE`，在 `_patched_send` 里剥离 |
| `tests/test_card_handler.py` | 纯函数/逻辑单测 | **新建**：stdlib `unittest`，覆盖 footer 渲染、think 过滤、seq stale-drop |
| `tests/__init__.py` | 包标识 | **新建**（空文件） |
| `README.md` | 用户文档 | **改**：在「功能」里加 footer / think 过滤；在「与 cc-connect 对比」表加新行 |
| `CHANGELOG.md` | 版本记录 | **改**：加 `v1.4.0` 条目 |
| `plugin.yaml` | 版本号 | **改**：`1.3.0` → `1.4.0` |

---

## Task 1: 准备测试基础设施

**Files:**
- Create: `tests/__init__.py`（空文件）
- Create: `tests/test_card_handler.py`

- [ ] **Step 1: 创建空 `tests/__init__.py`**

```bash
touch /Users/Novence/Develop/feishu-card-progress/tests/__init__.py
```

- [ ] **Step 2: 创建 `tests/test_card_handler.py` 占位**

```python
"""Unit tests for feishu-card-progress card_handler.py pure logic.

Uses stdlib unittest only — no pytest dependency. Run with:

    python -m unittest tests.test_card_handler -v
"""

import unittest


class TestPlaceholder(unittest.TestCase):
    """Removed once real tests land in later tasks."""

    def test_placeholder(self):
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 验证占位测试可被发现并跑通**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler -v
```
Expected: `OK` + `test_placeholder ... ok`，无 ImportError。

- [ ] **Step 4: 提交**

```bash
git add tests/__init__.py tests/test_card_handler.py
git commit -m "$(cat <<'EOF'
test: scaffold stdlib unittest harness for card_handler pure logic

No pytest dependency. Later tasks populate this file with real tests
for footer rendering, <think> stripping, and PATCH seq stale-drop.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: `<think>` 标签兜底过滤

**Why first:** 三件事里最独立、最小。先做以验证测试 harness 工作。

**Files:**
- Modify: `__init__.py`（新增 `_THINK_BLOCK_RE`、`_THINK_TAG_RE`、`_strip_think_tags`；改 `_patched_send`）
- Test: `tests/test_card_handler.py`（暂时把 `_strip_think_tags` 测试放在这里，因为函数在 `__init__.py`）

- [ ] **Step 1: 写失败测试 — 在 `tests/test_card_handler.py` 顶部加 import 和测试类**

把 `tests/test_card_handler.py` 的内容整体替换为：

```python
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

# Import the package — register() is NOT called by import, only by Hermes.
# So importing __init__ does not monkey-patch anything.
from feishu_card_progress import _strip_think_tags  # noqa: E402


class TestStripThinkTags(unittest.TestCase):
    def test_removes_closed_think_block(self):
        text = "<think>internal reasoning here</think>visible answer"
        self.assertEqual(_strip_think_tags(text), "visible answer")

    def test_removes_closed_thinking_block(self):
        text = "<thinking>long\nmulti\nline</thinking>answer"
        self.assertEqual(_strip_think_tags(text), "answer")

    def test_case_insensitive(self):
        text = "<THINK>reasoning</THINK>answer"
        self.assertEqual(_strip_think_tags(text), "answer")

    def test_multiline_block(self):
        text = "<think>\nline1\nline2\n</think>\nanswer"
        self.assertEqual(_strip_think_tags(text).strip(), "answer")

    def test_orphan_closing_tag_removed(self):
        text = "answer</think>tail"
        self.assertEqual(_strip_think_tags(text), "answertail")

    def test_orphan_opening_tag_removed(self):
        # No closing tag — strip the bare opening tag, keep text after.
        text = "answer<think>more"
        self.assertEqual(_strip_think_tags(text), "answermore")

    def test_no_tags_unchanged(self):
        text = "plain answer with **markdown** and `code`"
        self.assertEqual(_strip_think_tags(text), text)

    def test_multiple_blocks(self):
        text = "<think>a</think>mid<think>b</think>end"
        self.assertEqual(_strip_think_tags(text), "midend")


if __name__ == "__main__":
    unittest.main()
```

注意：导入 `from feishu_card_progress import ...` 需要包名能被识别。本插件目录名为 `feishu-card-progress`（连字符），Python 不能直接 import。**需要通过 sys.path 加目录后用 `import __init__` 别名导入**。修正写法见 Step 2。

- [ ] **Step 2: 修正测试导入方式 — `__init__.py` 不可以被点号导入（目录名含连字符）**

把测试文件里的 import 段改为使用 `importlib`：

```python
# Replace the `from feishu_card_progress import _strip_think_tags` line with:
import importlib.util

_spec = importlib.util.spec_from_file_location(
    "feishu_card_progress_under_test",
    _REPO_ROOT / "__init__.py",
)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
_strip_think_tags = _mod._strip_think_tags
```

- [ ] **Step 3: 运行测试，确认失败（函数不存在）**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler -v
```
Expected: `AttributeError: module has no attribute '_strip_think_tags'` 或 ImportError。

- [ ] **Step 4: 在 `__init__.py` 实现剥离函数**

在 `__init__.py` 顶部「Progress-text detection」section 之后、「Lazy card-handler accessor」之前，新增：

```python
# ---------------------------------------------------------------------------
# Reasoning tag fallback stripping
# ---------------------------------------------------------------------------
# Some providers (DeepSeek, Qwen, Moonshot) occasionally leak raw
# <think>/<thinking> tags into the answer text instead of routing them
# through the reasoning channel.  We strip both complete blocks and
# orphan tags as a defensive measure before the final response is sent.
_THINK_BLOCK_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>", re.IGNORECASE | re.DOTALL
)
_THINK_TAG_RE = re.compile(r"</?think(?:ing)?>", re.IGNORECASE)


def _strip_think_tags(text: str) -> str:
    """Remove <think>/<thinking> blocks and orphan tags from text.

    Order matters: strip complete blocks first (so their inner content
    disappears), then strip any leftover orphan opening/closing tags.
    """
    if not isinstance(text, str) or not text:
        return text
    text = _THINK_BLOCK_RE.sub("", text)
    text = _THINK_TAG_RE.sub("", text)
    return text
```

- [ ] **Step 5: 在 `_patched_send` 中调用剥离**

定位 `__init__.py` 里 `_patched_send` 函数中 `_REASONING_PREFIX` 处理逻辑之后、`# Track the first response message for this chat` 之前，插入：

```python
    # Defensive: strip any leaked <think>/<thinking> tags from the final
    # response text.  Normally reasoning is routed to the card via
    # on_thinking; this catches models that emit raw tags.
    if isinstance(content, str):
        content = _strip_think_tags(content)
```

具体插入位置参考现有代码（约 `__init__.py:335`，在 `if _last_reasoning_text and content.startswith(...)` 块之后）。

- [ ] **Step 6: 运行测试，确认全部通过**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler -v
```
Expected: 8 个 test 方法全部 ok，`OK`。

- [ ] **Step 7: 手动验证 — 重启 Hermes 触发一次包含 `<think>` 的回答**

不是必须，但建议在有 DeepSeek/Qwen profile 上验证一次。如果暂时没条件，跳过即可（单测已经覆盖逻辑）。

- [ ] **Step 8: 提交**

```bash
git add __init__.py tests/test_card_handler.py
git commit -m "$(cat <<'EOF'
feat: strip leaked <think>/<thinking> tags from final response

Some providers (DeepSeek, Qwen, Moonshot) occasionally emit raw
<think> tags into the answer text instead of routing reasoning through
the proper channel.  Strip both complete blocks and orphan tags in
_patched_send before the response is sent to Feishu.

Borrowed from hermes-feishu-streaming-card's defensive layer.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: PATCH 串行化 + stale-drop

**Why this order:** 独立于 footer；先解决潜在并发竞态，让后续 footer 改动跑在稳态上。

**Files:**
- Modify: `card_handler.py`：`__init__`、`on_tool_started`、`update_entries`、`on_thinking`、`_patch_progress_card`、`_update_progress_card_completed`、`_update_progress_card_failed`
- Test: `tests/test_card_handler.py`

- [ ] **Step 1: 写失败测试 — PATCH 序列号 stale-drop 行为**

在 `tests/test_card_handler.py` 顶部 import 段追加：

```python
import asyncio
from unittest.mock import MagicMock, AsyncMock, patch
```

并在文件末尾、`if __name__ == "__main__":` 之前，新增测试类：

```python
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
        # Stub out the actual Feishu PATCH so the lock is the only
        # thing under test.
        handler._patch_progress_card = AsyncMock()
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
            # Temporarily restore it from module source.
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
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler.TestPatchStaleDrop -v
```
Expected: `AttributeError: 'FeishuCardHandler' object has no attribute '_bump_seq'` 或类似。

- [ ] **Step 3: 修改 `card_handler.py` 的 `__init__` — 增加 lock / seq 字段**

定位 `card_handler.py:86`（`def __init__` 函数体），在 `self._load_stale_cards()` 之前插入：

```python
        self._turn_start_times: Dict[str, float] = {}      # chat_id → monotonic start
        self._patch_locks: Dict[str, "asyncio.Lock"] = {}  # chat_id → PATCH serialization lock
        self._progress_seq: Dict[str, int] = {}            # chat_id → monotonic entry counter
        self._last_sent_seq: Dict[str, int] = {}           # chat_id → last PATCH seq actually sent
```

- [ ] **Step 4: 实现 `_bump_seq` 和 `_get_patch_lock` 辅助方法**

在 `card_handler.py` 中 `_agent_label` property 之后（约 `card_handler.py:101`），新增：

```python
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
```

- [ ] **Step 5: 改 `_patch_progress_card` 签名与实现 — 加 seq 参数、加 lock、加 stale-drop**

把现有 `_patch_progress_card`（`card_handler.py:349-388`）整体替换为：

```python
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
```

- [ ] **Step 6: 在所有 mutation 点 bump seq 并透传**

**6a.** `card_handler.py:209`，`on_tool_started` 中 `entries.append(...)` 之后插入：

```python
        seq = self._bump_seq(chat_id)
```

并把 `await self._patch_progress_card(active_card_id, chat_id, entries)` 改为：

```python
        await self._patch_progress_card(active_card_id, chat_id, entries, seq=seq)
```

**6b.** `card_handler.py:247`，`update_entries` 中 `self._progress_entries[chat_id] = ...` 之后插入：

```python
        seq = self._bump_seq(chat_id)
```

并把 `await self._patch_progress_card(...)` 改为带 `seq=seq`。

**6c.** `card_handler.py:274`，`on_thinking` 中 `entries.append(...)` 之后插入：

```python
        seq = self._bump_seq(chat_id)
```

并把 `await self._patch_progress_card(...)` 改为带 `seq=seq`。

- [ ] **Step 7: 让 `_update_progress_card_completed` 和 `_update_progress_card_failed` 使用同一个 lock**

这两个方法是终态（不参与 seq drop），但需要与进行中的 PATCH 互斥，避免覆盖。在两个方法的 `try:` 之后第一行插入：

```python
            async with self._get_patch_lock(chat_id):
```

并把后续 `from lark_oapi...` 到 `await asyncio.wait_for(...)` 整段缩进 +4 空格。注意保持原有 `except` 块不变。

参考结构（completed，约 `card_handler.py:390-440`）：

```python
    async def _update_progress_card_completed(
        self, card_message_id: str, chat_id: str
    ) -> None:
        a = self._a
        if not a._client:
            return
        try:
            async with self._get_patch_lock(chat_id):
                # ... existing body indented +4 spaces ...
                entries = self._progress_entries.get(chat_id, [])
                # ... rest of card build + patch ...
                self._last_sent_seq[chat_id] = self._progress_seq.get(chat_id, 0)
        except asyncio.TimeoutError:
            logger.warning("[Card] Completed card update timed out (%ds)", _API_TIMEOUT)
        except Exception as exc:
            logger.warning("[Card] Completed card update error: %s", exc)
```

同样对 `_update_progress_card_failed` 应用（约 `card_handler.py:442-493`）。

- [ ] **Step 8: 运行所有测试**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler -v
```
Expected: 所有 test 通过（`TestStripThinkTags` 8 个 + `TestPatchStaleDrop` 3 个）+ 之前的 placeholder 已被替换。

- [ ] **Step 9: 手动验证 — 重启 Hermes，触发一个有 ≥3 个 tool call 的对话，观察卡片无内容回退**

```bash
hermes gateway restart <profile>
```

在飞书发消息触发 tool 密集型任务（例如要求执行多次 bash）。验证：卡片内容随 tool 推进单调增长，不出现"内容回退到旧快照"现象。

- [ ] **Step 10: 提交**

```bash
git add card_handler.py tests/test_card_handler.py
git commit -m "$(cat <<'EOF'
fix: serialize per-chat card PATCHes and drop stale snapshots

Add asyncio.Lock per chat_id around _patch_progress_card and the
completed/failed finalizers.  Add a monotonic seq counter bumped on
every _progress_entries mutation; patches with seq older than the
last sent seq are dropped inside the lock.

Fixes the "old snapshot overwrites new content" race when multiple
Hermes callbacks fire in rapid succession.  Mirrors issue #31 from
hermes-feishu-streaming-card.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Footer 运行统计

**Files:**
- Modify: `__init__.py`：新增模块级 `_agent_ref`；改 `_patched_agent_setattr` 捕获 agent；改 `_patched_on_processing_complete` 透传 agent 给 handler
- Modify: `card_handler.py`：`on_processing_start` 记 start time；`on_processing_complete` 签名加 `agent`；新增 `_build_footer_elements`
- Test: `tests/test_card_handler.py`

- [ ] **Step 1: 写失败测试 — footer 渲染逻辑**

在 `tests/test_card_handler.py` import 段之后、`TestPlaceholder` 之前，把 placeholder 类删除，并在文件末尾追加：

```python
class TestFooterRender(unittest.TestCase):
    """Verify footer element structure for completed cards."""

    def _load_handler_cls(self):
        spec = importlib.util.spec_from_file_location(
            "card_handler_under_test",
            _REPO_ROOT / "card_handler.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod.FeishuCardHandler, mod

    def test_footer_includes_duration_model_tokens(self):
        cls, _ = self._load_handler_cls()
        # _build_footer_elements is a static method.
        elements = cls._build_footer_elements(
            duration=12.3,
            model="claude-sonnet-4-6",
            input_tokens=1234,
            output_tokens=5678,
        )
        # Should be a non-empty list of card element dicts.
        self.assertIsInstance(elements, list)
        self.assertGreater(len(elements), 0)
        # Render to a single string for assertions on content.
        rendered = json.dumps(elements, ensure_ascii=False)
        self.assertIn("12.3", rendered)            # duration (1 decimal)
        self.assertIn("claude-sonnet-4-6", rendered)  # model
        self.assertIn("1.2k", rendered)            # input tokens (humanized)
        self.assertIn("5.7k", rendered)            # output tokens (humanized)

    def test_footer_omits_missing_fields(self):
        cls, _ = self._load_handler_cls()
        elements = cls._build_footer_elements(
            duration=None, model=None,
            input_tokens=None, output_tokens=None,
        )
        # With no data, footer should be empty (no element emitted).
        self.assertEqual(elements, [])

    def test_footer_handles_zero_tokens(self):
        cls, _ = self._load_handler_cls()
        elements = cls._build_footer_elements(
            duration=0.5, model="test-model",
            input_tokens=0, output_tokens=0,
        )
        rendered = json.dumps(elements, ensure_ascii=False)
        self.assertIn("0.5", rendered)
        # 0 tokens — we still render "0" so the user sees the turn was free.
        self.assertIn("0", rendered)
```

并在 `tests/test_card_handler.py` 顶部加：

```python
import json
```

- [ ] **Step 2: 运行测试，确认失败**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler.TestFooterRender -v
```
Expected: `AttributeError: type object 'FeishuCardHandler' has no attribute '_build_footer_elements'`。

- [ ] **Step 3: 实现 token humanize 辅助 + `_build_footer_elements` 静态方法**

在 `card_handler.py` 顶部模块级辅助区（`_format_tool_input` 之后，约 `card_handler.py:77`），新增：

```python
def _humanize_tokens(n: Optional[int]) -> str:
    """1234 → '1.2k', 5678900 → '5.7M', None → ''."""
    if n is None:
        return ""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)
```

然后在 `FeishuCardHandler` 类的 `_render_progress_entries` 静态方法之前（约 `card_handler.py:570`），新增静态方法：

```python
    @staticmethod
    def _build_footer_elements(
        *,
        duration: Optional[float],
        model: Optional[str],
        input_tokens: Optional[int],
        output_tokens: Optional[int],
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
```

- [ ] **Step 4: 运行 footer 单测，确认通过**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler.TestFooterRender -v
```
Expected: 3 个 test 全部 ok。

- [ ] **Step 5: 在 `__init__.py` 增加模块级 `_agent_ref`**

在 `__init__.py` 的「Cross-thread state for reasoning interception」section 内（约 `__init__.py:263-265`），新增：

```python
_agent_ref: Any = None        # run_agent.AIAgent (captured in _patched_agent_setattr)
```

- [ ] **Step 6: 在 `_patched_agent_setattr` 捕获 agent 实例**

定位 `__init__.py` 里 `_patched_agent_setattr` 函数（约 `__init__.py:741-744`），整体替换为：

```python
        def _patched_agent_setattr(self_agent, name, value):
            global _agent_ref
            # tool_progress_callback is set late in agent init, after
            # session state and model are configured — capture the ref here.
            if name == "tool_progress_callback":
                _agent_ref = self_agent
            if name == "tool_progress_callback" and value is not None:
                value = _wrap_progress_callback(value)
            _orig_setattr(self_agent, name, value)
```

- [ ] **Step 7: 改 `_patched_on_processing_complete` 透传 agent 给 handler**

定位 `__init__.py:291-296`，整体替换为：

```python
async def _patched_on_processing_complete(self, event, outcome) -> None:
    """Wrap original on_processing_complete + card finalization."""
    handler = _get_card_handler(self)
    await handler.on_processing_complete(event, outcome, agent=_agent_ref)
    # Call original (removes Typing reaction, adds failure reaction)
    await _orig_on_processing_complete(self, event, outcome)
```

- [ ] **Step 8: 改 `card_handler.py` 的 `on_processing_start` — 记录开始时间**

定位 `card_handler.py:150-160`，在 `self._completed_chats.discard(chat_id)` 之前插入：

```python
        import time
        self._turn_start_times[chat_id] = time.monotonic()
```

- [ ] **Step 9: 改 `card_handler.py` 的 `on_processing_complete` — 签名 + 计算 duration + 透传 footer**

定位 `card_handler.py:162-191`，整体替换为：

```python
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

        if active_card_id:
            has_tool_entries = any(e.get("type") == "tool_use" for e in entries)
            if not has_tool_entries:
                logger.info("[Card] Deleting empty progress card (no tool entries)")
                await self._delete_message(active_card_id)
            else:
                if outcome is ProcessingOutcome.FAILURE:
                    await self._update_progress_card_failed(active_card_id, chat_id)
                else:
                    await self._update_progress_card_completed(
                        active_card_id, chat_id,
                        duration=duration, model=model,
                        input_tokens=input_tokens, output_tokens=output_tokens,
                    )

        self._active_progress_cards.pop(chat_id, None)
        self._progress_entries.pop(chat_id, None)
        self._progress_seq.pop(chat_id, None)
        self._last_sent_seq.pop(chat_id, None)
        self._save_active_cards()

        # Add response header to the final response message to distinguish
        # it from the green "Completed" progress card above.
        if self._response_header and active_card_id and outcome is not ProcessingOutcome.FAILURE:
            await self._finalize_response_card(chat_id)
```

- [ ] **Step 10: 改 `_update_progress_card_completed` — 接收 footer 参数并在 hr 之前插入**

把 Task 3 Step 7 改过的 `_update_progress_card_completed` 整体替换为：

```python
    async def _update_progress_card_completed(
        self, card_message_id: str, chat_id: str,
        *, duration: Optional[float] = None,
        model: Optional[str] = None,
        input_tokens: Optional[int] = None,
        output_tokens: Optional[int] = None,
    ) -> None:
        a = self._a
        if not a._client:
            return
        try:
            async with self._get_patch_lock(chat_id):
                entries = self._progress_entries.get(chat_id, [])
                trimmed, truncated = self._trim_entries(entries)
                elements = self._render_progress_entries(trimmed, truncated)
                footer = self._build_footer_elements(
                    duration=duration, model=model,
                    input_tokens=input_tokens, output_tokens=output_tokens,
                )
                if footer:
                    elements.append({"tag": "hr"})
                    elements.extend(footer)
                else:
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
```

- [ ] **Step 11: 运行所有测试，确认无回归**

Run:
```bash
cd /Users/Novence/Develop/feishu-card-progress && python -m unittest tests.test_card_handler -v
```
Expected: 所有测试通过（TestStripThinkTags 8 + TestPatchStaleDrop 3 + TestFooterRender 3 = 14 个）。

- [ ] **Step 12: 手动验证 — 重启 Hermes，触发一次 tool 调用对话，确认 footer 显示**

```bash
hermes gateway restart <profile>
```

在飞书发消息触发 tool 调用（例如「列一下当前目录文件」）。完成卡片应显示类似：

> ⏱ 4.2s · 🤖 claude-sonnet-4-6 · ↑1.2k ↓320 tokens

如果 model 字段为空，检查 Hermes 该 profile 的 agent 是否设置了 `model` 属性；如果 token 为空，检查 `session_input_tokens` / `session_output_tokens` 是否在该 Hermes 版本上存在（属性名可能不同，必要时调整 Step 9 的 getattr key）。

- [ ] **Step 13: 提交**

```bash
git add card_handler.py __init__.py tests/test_card_handler.py
git commit -m "$(cat <<'EOF'
feat: add runtime stats footer to completed progress cards

Capture the AIAgent instance when tool_progress_callback is set, read
session_input_tokens / session_output_tokens / model on
on_processing_complete, and render a footer on the green 'Completed'
card showing duration, model, and humanized token counts.

Duration is tracked via time.monotonic() between on_processing_start
and on_processing_complete.

Borrowed from hermes-feishu-streaming-card's footer_fields feature.

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 文档与版本号更新

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `plugin.yaml`

- [ ] **Step 1: 更新 `README.md` 的功能列表**

在 `README.md` 现有「功能」列表中（约 `README.md:13-21`），在「**重启容错**」之前插入三行：

```markdown
- **运行统计 Footer** — 完成态卡片自动展示耗时、模型、token 用量（如 `⏱ 4.2s · 🤖 claude-sonnet-4-6 · ↑1.2k ↓320 tokens`）
- **`<think>` 标签兜底** — DeepSeek/Qwen/Moonshot 等模型偶发泄漏的 `<think>`/`<thinking>` 标签自动剥离
- **PATCH 并发安全** — per-chat 锁 + 序号 stale-drop，避免 tool 密集场景下旧快照覆盖新内容
```

- [ ] **Step 2: 更新 `README.md` 的对比表**

在 `README.md` 的「与 cc-connect 对比」表（约 `README.md:104-112`）里，在「TodoWrite 图标」行之前追加三行：

```markdown
| 运行统计 footer | — | ⏱🤖↑↓ |
| `<think>` 兜底过滤 | — | ✓ |
| PATCH 串行 + stale-drop | — | ✓ |
```

- [ ] **Step 3: 更新 `CHANGELOG.md`**

打开 `CHANGELOG.md`，在最顶部新增一个条目（如果文件用 `## [Unreleased]` 风格，新增 `## [1.4.0] - 2026-06-13`；如果是按版本倒序的纯列表，直接顶部加）：

```markdown
## [1.4.0] - 2026-06-13

### Added
- 完成态卡片新增运行统计 footer（duration / model / input_tokens / output_tokens）
- `<think>` / `<thinking>` 标签兜底剥离，防止模型原始标签泄漏到最终回复
- 新增 stdlib `unittest` 测试 harness（`tests/test_card_handler.py`），覆盖纯函数与 PATCH 序号 stale-drop

### Fixed
- 多线程回调并发 PATCH 同一卡片时的内容回退竞态（per-chat lock + monotonic seq stale-drop）
```

如果 `CHANGELOG.md` 当前格式不是 Keep a Changelog 风格，参考文件现有格式调整。

- [ ] **Step 4: 更新 `plugin.yaml` 版本号**

把 `plugin.yaml` 第 2 行 `version: 1.3.0` 改为 `version: 1.4.0`。

- [ ] **Step 5: 提交**

```bash
git add README.md CHANGELOG.md plugin.yaml
git commit -m "$(cat <<'EOF'
docs: bump to v1.4.0 — footer, <think> strip, PATCH lock

Co-Authored-By: Claude Opus 4.7 <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**1. Spec coverage:**
- Footer 统计（duration/model/tokens）→ Task 4 ✓
- `<think>` 兜底过滤 → Task 2 ✓
- PATCH 串行 + stale-drop → Task 3 ✓
- 测试基础设施 → Task 1 ✓
- 文档/版本 → Task 5 ✓
- 调研中识别但**有意不做**的项（流式 answer 预览、approval/clarify 按钮、附件摘要、长 code block 切分、doctor CLI、多 bot）→ 不在 plan 里，符合「先做高 ROI 三项」的共识 ✓

**2. Placeholder scan:**
- 全部 step 都有具体代码或具体命令，无 TBD/TODO/"add error handling"
- 测试代码是真实可运行的，不是 "Write tests for the above"
- `hermes gateway restart <profile>` 中的 `<profile>` 是用户运行时填入的真实 profile 名，不是 plan 占位

**3. Type consistency:**
- `_bump_seq` / `_get_patch_lock` / `_last_sent_seq` / `_progress_seq` 在 Task 3 定义、Task 4 使用，命名一致 ✓
- `_build_footer_elements` 在 Task 4 Step 3 定义为 staticmethod，Task 4 Step 1 测试通过 `cls._build_footer_elements` 调用，签名一致 ✓
- `_patch_progress_card` 在 Task 3 Step 5 改为 keyword-only `seq` 参数，Task 3 Step 6 三处调用都用了 `seq=seq` ✓
- `on_processing_complete` 在 Task 4 Step 7 (`__init__.py`) 调用 `handler.on_processing_complete(event, outcome, agent=_agent_ref)`，Task 4 Step 9 (`card_handler.py`) 签名为 `(self, event, outcome, *, agent=None)`，匹配 ✓
- `_update_progress_card_completed` 在 Task 4 Step 9 调用时传 `duration=`, `model=`, `input_tokens=`, `output_tokens=`，Task 4 Step 10 签名一致 ✓
- `_humanize_tokens` 模块级辅助在 Task 4 Step 3 定义，`_build_footer_elements` 内部调用，命名一致 ✓

**4. 风险点（已知，写入 plan 让实施者知情）:**
- Task 4 Step 9 的 `getattr(agent, "session_input_tokens", None)` 依赖 Hermes `AIAgent` 暴露该属性。Explore agent 报告该属性位于 `agent/conversation_loop.py:1921-1922`，但若 Hermes 版本字段名变化，footer 会缺 token 字段（不会崩，因为 `getattr` 有默认值）。手动验证步骤已要求检查这一点。
- Task 3 Step 7 把 `_update_progress_card_completed/failed` 整段缩进 +4 空格容易出错；Task 4 Step 10 又会重写 `_update_progress_card_completed`。实施者可选择在 Task 3 直接用 Task 4 Step 10 的最终形态，但本 plan 选择分两步走以便每步可独立验证。

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-06-13-feishu-card-progress-borrow.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?
