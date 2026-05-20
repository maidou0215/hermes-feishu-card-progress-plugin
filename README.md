# feishu-card-progress

Hermes 飞书插件 — 工具执行进度卡片 + Schema 2.0 响应渲染。

参考 [cc-connect](https://github.com/anthropics/cc-connect) 的 Feishu 进度卡片 UI 实现。

## 效果

<img src="assets/before.png" width="400" alt="Before"> <img src="assets/after.png" width="400" alt="After">

## 功能

- **进度卡片** — 自动创建、实时 Patch 更新，不再刷屏。Header 状态：🔵 Running → 🟢 Completed / 🔴 Failed
- **Thinking 显示** — 灰色 💭 notation，支持 DeepSeek/Qwen/Moonshot/OpenRouter 等多 provider
- **Schema 2.0 渲染** — Markdown 响应自动转为交互式卡片，表格/代码块/链接格式更精确
- **Reply Chain 增强** — 引用卡片消息时提取实际文本内容，不再显示 `[Interactive message]`
- **root_id 自动清除** — 防止引用回复自动创建话题，Hermes 更新后不会复发
- **重启容错** — 活跃卡片 ID 持久化，重启后自动清理遗留卡片

## 安装

```bash
# 1. 复制插件
cp -r feishu-card-progress ~/.hermes/plugins/feishu-card-progress

# 2. Profile 模式需要额外链接
ln -s ~/.hermes/plugins/feishu-card-progress ~/.hermes/profiles/<profile>/plugins/

# 3. 启用插件（profile config.yaml）
# plugins:
#   enabled:
#     - feishu-card-progress

# 4. 打上游补丁（2 处，均在 gateway/run.py）
# 详见 skills/patch-upstream/SKILL.md

# 5. 重启
hermes gateway restart
```

## 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `FEISHU_PROGRESS_STYLE` | — | 设为 `card` 激活插件，未设置则静默加载 |
| `FEISHU_PROGRESS_GREEN_HEADER` | `false` | 设为 `true` 给有进度卡片的最终回复加绿色 "Hermes · Completed" 头部 |

## 上游更新

Hermes `git pull` 后检查 2 处补丁是否被覆盖：

```bash
cd ~/.hermes/hermes-agent && git pull origin main
grep -n 'FEISHU_PROGRESS_STYLE' gateway/run.py  # 应有结果
grep -n 'reply_to_text\[:500\]' gateway/run.py   # 应无结果
```

或直接让 AI 执行：**"Hermes 更新了，帮我重新打 feishu-card 的补丁"**

详见 `skills/patch-upstream/SKILL.md`。

## Monkey-patch 列表

| 方法 | 行为 |
|------|------|
| `on_processing_start` | 清理遗留卡片，重置状态 |
| `on_processing_complete` | 完成卡片（绿/红 header + 页脚） |
| `_on_message_event` | 清除 `root_id`，防止引用回复自动创建话题 |
| `send()` | 拦截进度消息 → 创建卡片；标记最终回复 |
| `edit_message()` | 拦截进度更新 → PATCH 卡片 |
| `_build_outbound_payload` | Schema 2.0 卡片渲染 + 可选绿色头部 |
| `_build_get_message_request` | 增加 `card_msg_content_type=raw_card_content` 参数 |
| `_extract_text_from_raw_content` | 解析 interactive 卡片，提取引用文本 |
| `Agent.__setattr__` | 包装 `tool_progress_callback`，路由 reasoning 事件 |
| `Agent._build_assistant_message` | 拦截 reasoning 提取 → 卡片 |

## 与 cc-connect 对比

| 功能 | cc-connect | 本插件 |
|------|-----------|--------|
| 进度卡片 | Schema 2.0 | Schema 2.0 |
| Reasoning 显示 | 卡片内 | 卡片内 |
| 网关重启容错 | 无 | 持久化 + 自动清理 |
| 流式文本预览 | 有 | — |
| TodoWrite 图标 | 有 | — |

## 依赖

- Hermes Agent（需要插件系统支持）
- `lark_oapi` SDK
- 飞书平台已配置（`FEISHU_APP_ID` / `FEISHU_APP_SECRET`）

## 许

MIT
