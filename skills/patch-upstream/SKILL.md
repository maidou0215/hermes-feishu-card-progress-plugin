---
name: feishu-card-patch-upstream
description: Hermes 上游更新后，为 feishu-card-progress 插件重新打补丁。在 `cd ~/.hermes/hermes-agent && git pull` 之后使用。
version: 5.0.0
author: Novence
---

# Feishu Card Progress — 上游补丁指南

Hermes 源码是 git clone（`~/.hermes/hermes-agent/`，remote: `origin` → `NousResearch/hermes-agent`）。
插件自身（`feishu-card-progress/`）是独立文件，不会冲突。

**需要 1 个补丁**，其余功能全部通过插件 monkey-patching 实现。

## 补丁: feishu.py — 剥离 root_id 防止自动创建话题

**文件**: `gateway/platforms/feishu.py`
**冲突风险**: 低（单行改动，在消息处理逻辑中）

搜索 `root_id` 结合 `thread_id` 的位置，找到类似：

```python
thread_id = getattr(message, "thread_id", None) or getattr(message, "root_id", None) or None
```

以及：

```python
reply_to_message_id = (
    getattr(message, "parent_id", None)
    or getattr(message, "upper_message_id", None)
    or getattr(message, "root_id", None)
    or None
)
```

去掉两处 `root_id` 引用：

```python
thread_id = getattr(message, "thread_id", None) or None
```

```python
reply_to_message_id = (
    getattr(message, "parent_id", None)
    or getattr(message, "upper_message_id", None)
    or None
)
```

**原因**: Hermes 用 `root_id` 作为 `thread_id` 的 fallback，导致引用回复时自动创建话题。插件也通过 monkey-patch `_on_message_event` 在运行时清除 root_id 作为双重保障。

**验证**: `grep -n 'root_id' gateway/platforms/feishu.py | grep -i 'thread_id\|reply_to'` 应无结果。

## 补丁步骤

```bash
cd ~/.hermes/hermes-agent

# 1. 拉取上游
git pull origin main

# 2. 检查补丁是否还在
grep -n 'root_id' gateway/platforms/feishu.py | grep -i 'thread_id\|reply_to'
# 应无结果（如果补丁还在）

# 3. 如果被覆盖，重新应用上面的补丁

# 4. 重启 gateway（不要用 --all，按 profile 重启）
hermes gateway restart
```

## 为什么其他补丁不需要

| 功能 | 为什么不需要上游补丁 |
|------|----------------------|
| Reasoning 处理 | 插件 monkey-patch `AIAgent._build_assistant_message` 自行提取 + `send()` 中 fallback 剥离前缀 |
| Reply chain 截断 | 当前 500 字符截断未造成实际问题 |
| on_thinking 方法 | 插件直接调用 `handler.on_thinking()`，不经过 adapter 基类 |
| 环境变量读取 | 插件直接 `os.environ.get("FEISHU_PROGRESS_STYLE")` 读取 |
| Interactive 卡片文本提取 | 插件 monkey-patch `_build_get_message_request` + `_extract_text_from_raw_content` |
| 最终回复 header | `on_processing_complete` 后 retroactively patch 已发送的消息 |

插件是完全自包含的，除了 `feishu.py` 这 1 个补丁外，不依赖任何上游代码修改。
