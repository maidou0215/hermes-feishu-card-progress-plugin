---
name: feishu-card-patch-upstream
description: Hermes 上游更新后，为 feishu-card-progress 插件重新打补丁。在 `cd ~/.hermes/hermes-agent && git pull` 之后使用。
version: 3.0.0
author: Novence
---

# Feishu Card Progress — 上游补丁指南

Hermes 源码是 git clone（`~/.hermes/hermes-agent/`，remote: `origin` → `NousResearch/hermes-agent`）。
插件自身（`feishu-card-progress/`）是独立文件，不会冲突。

**需要 2 个补丁**，其余功能全部通过插件 monkey-patching 实现。

## 补丁 1: run.py — 跳过 reasoning 拼接

**文件**: `gateway/run.py`
**冲突风险**: 中（核心流程文件）

找到以下代码段（搜索 `_show_reasoning_effective` 或 `Prepend reasoning`）：

```python
            except Exception:
                _show_reasoning_effective = getattr(self, "_show_reasoning", False)
            if _show_reasoning_effective and response:
```

在 `except` 块和 `if` 之间插入 2 行：

```python
            # feishu-card-progress plugin handles reasoning in the card — skip prepend
            if os.environ.get("FEISHU_PROGRESS_STYLE", "").lower() == "card":
                _show_reasoning_effective = False
```

**验证**: `grep -n 'FEISHU_PROGRESS_STYLE' gateway/run.py` 应有结果。

## 补丁 2: run.py — Reply chain 不截断引用内容

**文件**: `gateway/run.py`
**冲突风险**: 低（单行改动）

搜索 `reply_to_text[:500]`，找到：

```python
            reply_snippet = event.reply_to_text[:500]
```

替换为（去掉 `[:500]`）：

```python
            reply_snippet = event.reply_to_text
```

**原因**: 插件增强了 interactive 卡片的文本提取（通过 `card_msg_content_type=raw_card_content` API 参数），卡片内容可能很长。上游的 500 字符截断会丢失大部分内容。cc-connect 也不截断 reply chain。

**验证**: `grep -n 'reply_to_text\[:500\]' gateway/run.py` 应无结果。

## 补丁步骤

```bash
cd ~/.hermes/hermes-agent

# 1. 拉取上游
git pull origin main

# 2. 检查补丁是否还在
grep -n 'FEISHU_PROGRESS_STYLE' gateway/run.py          # 补丁 1
grep -n 'reply_to_text\[:500\]' gateway/run.py           # 补丁 2（应无结果）

# 3. 如果被覆盖，重新应用上面的补丁

# 4. 重启 gateway
hermes gateway restart --all
```

## 为什么其他补丁不需要

| 功能 | 为什么不需要上游补丁 |
|------|----------------------|
| Reasoning 提取 | 插件 monkey-patch `AIAgent._build_assistant_message` 自行提取 |
| on_thinking 方法 | 插件直接调用 `handler.on_thinking()`，不经过 adapter 基类 |
| 环境变量读取 | 插件直接 `os.environ.get("FEISHU_PROGRESS_STYLE")` 读取 |
| Interactive 卡片文本提取 | 插件 monkey-patch `_build_get_message_request`（加 API 参数）和 `_extract_text_from_raw_content`（解析 `json_card` 格式） |
| Reply chain 截断 | 补丁 2 已处理 |

插件是完全自包含的，除了 run.py 这 2 个补丁外，不依赖任何上游代码修改。
