---
name: feishu-card-patch-upstream
description: Hermes 上游更新后，为 feishu-card-progress 插件重新打补丁。在 `cd ~/.hermes/hermes-agent && git pull` 之后使用。
version: 2.0.0
author: Novence
---

# Feishu Card Progress — 上游补丁指南

Hermes 源码是 git clone（`~/.hermes/hermes-agent/`，remote: `origin` → `NousResearch/hermes-agent`）。
插件自身（`feishu-card-progress/`）是独立文件，不会冲突。

**只需要 1 个补丁**，其余功能全部通过插件 monkey-patching 实现。

## 为什么需要这个补丁

`run.py` 在 `show_reasoning=true` 时会把 reasoning 拼到 response 正文前面：
```
💭 **Reasoning:**
```
{reasoning}
```

{actual_response}
```

插件需要在 `_patched_send` 中剥离这个前缀。但 response 本身也可能包含 `` ``` `` 代码块，
导致字符串匹配（`rfind`）找错边界，把 response 的代码块也剥掉。
实测：当 response 含代码块时，剥离失败率约 30-40%。

**根本解决**：让 run.py 在 card 模式下不拼 reasoning（插件已在进度卡片中显示）。

## 唯一补丁: run.py — 跳过 reasoning 拼接

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

## 补丁步骤

```bash
cd ~/.hermes/hermes-agent

# 1. 拉取上游
git pull origin main

# 2. 检查补丁是否还在
grep -n 'FEISHU_PROGRESS_STYLE' gateway/run.py

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

插件是完全自包含的，除了 run.py 这 1 个补丁外，不依赖任何上游代码修改。
