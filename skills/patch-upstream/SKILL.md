---
name: feishu-card-patch-upstream
description: Hermes 上游更新后，为 feishu-card-progress 插件重新打补丁。在 `cd ~/.hermes/hermes-agent && git pull` 之后使用。
version: 1.0.0
author: Novence
---

# Feishu Card Progress — 上游补丁指南

Hermes 源码是 git clone（`~/.hermes/hermes-agent/`，remote: `origin` → `NousResearch/hermes-agent`）。
插件自身（`feishu-card-progress/`）是独立文件，不会冲突。
以下补丁是对 Hermes 核心文件的修改，每次 `git pull` 后需要检查并重新应用。

## 补丁清单

### 补丁 1: run.py — 跳过 reasoning 拼接（FEISHU_PROGRESS_STYLE=card 时）

**文件**: `gateway/run.py`
**冲突风险**: 中（核心流程文件，上游经常改）

找到以下代码段（搜索 `show_reasoning` 和 `Prepend reasoning`）：

```python
            except Exception:
                _show_reasoning_effective = getattr(self, "_show_reasoning", False)
            if _show_reasoning_effective and response:
```

在 `except` 块和 `if` 之间插入：

```python
            # feishu-card-progress plugin handles reasoning in the card — skip prepend
            if os.environ.get("FEISHU_PROGRESS_STYLE", "").lower() == "card":
                _show_reasoning_effective = False
```

**验证**: 搜索 `FEISHU_PROGRESS_STYLE` 应在 `run.py` 中找到这行。

---

### 补丁 2: run_agent.py — Reasoning 提取修复

**文件**: `run_agent.py`
**冲突风险**: 中（agent 循环，上游经常改）

确保 `last_reasoning` 的提取使用 `_extract_reasoning()` 而非 `assistant_message.content`。

搜索 `last_reasoning` 相关代码，确认：
- 使用 `_extract_reasoning(assistant_message)` 提取 thinking tokens
- 不使用 `assistant_message.content` 当 reasoning
- 无结构化 reasoning 时可回退到 content，但仅限 `<think...>` 标签内容

如果上游已改进 reasoning 提取逻辑，此补丁可能不再需要。

**验证**: `grep -n 'last_reasoning\|_extract_reasoning' run_agent.py` 确认提取逻辑正确。

---

### 补丁 3: base.py — on_thinking 默认方法

**文件**: `gateway/platforms/base.py`
**冲突风险**: 低（基类不常改）

在 adapter 基类中添加 `on_thinking` 默认方法。

搜索 `class.*Adapter` 或 `on_processing_complete`，在合适位置添加：

```python
    async def on_thinking(self, chat_id: str, text: str) -> None:
        """Called when reasoning/thinking text is available. Default: no-op."""
        pass
```

如果上游已添加此方法，跳过。

**验证**: `grep -n 'on_thinking' gateway/platforms/base.py` 应有结果。

---

### 补丁 4: config.py — FEISHU_PROGRESS_STYLE 环境变量

**文件**: `gateway/config.py`
**冲突风险**: 低（只加环境变量读取）

添加 `FEISHU_PROGRESS_STYLE` 环境变量的读取。

搜索其他 `FEISHU_` 或环境变量读取的位置，添加：

```python
FEISHU_PROGRESS_STYLE = os.environ.get("FEISHU_PROGRESS_STYLE", "").lower()
```

如果上游已添加此变量或插件通过其他方式读取，跳过。

**验证**: `grep -n 'FEISHU_PROGRESS_STYLE' gateway/config.py` 应有结果。

---

## 补丁步骤

```bash
cd ~/.hermes/hermes-agent

# 1. 暂存当前改动
git stash push -m "feishu-card-progress patches"

# 2. 拉取上游
git pull origin main

# 3. 尝试恢复
git stash pop

# 4. 如果有冲突，逐文件解决后按上面的补丁清单检查
#    确认每个补丁是否还在（stash pop 成功则可能还在）
#    如果被覆盖则重新手动应用

# 5. 验证所有补丁
grep -n 'FEISHU_PROGRESS_STYLE' gateway/run.py        # 补丁 1
grep -n '_extract_reasoning' run_agent.py              # 补丁 2
grep -n 'on_thinking' gateway/platforms/base.py        # 补丁 3
grep -n 'FEISHU_PROGRESS_STYLE' gateway/config.py     # 补丁 4

# 6. 重启 gateway
hermes gateway restart --all
```

## 注意事项

- 补丁 1 是最重要的，没有它 reasoning 会同时出现在卡片和正文里
- 补丁 2 的 `_extract_reasoning` 改造如果上游已改进可以跳过
- 补丁 3、4 冲突风险很低，通常 stash pop 后自动保留
- 插件文件（`feishu-card-progress/`）是独立目录，不受 `git pull` 影响
