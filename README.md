# hermes-feishu-card-progress-plugin

Hermes Agent 插件 — 将飞书消息体验从纯文本升级为交互式卡片。

参考 [cc-connect](https://github.com/anthropics/cc-connect) 的 Feishu 进度卡片 UI 实现，为 Hermes Feishu gateway 提供一致的卡片体验。

## 效果对比

### Before（Hermes 原始样式）

<img src="assets/before.png" width="480" alt="Hermes 原始文本进度样式">

### After（安装插件后）

<img src="assets/after.png" width="480" alt="安装插件后的交互式卡片样式">

## 功能

### 1. 工具执行进度卡片

原始体验：每执行一个工具，发一条文本消息 "⚙️ bash: ls"，刷屏且不可编辑。

安装后：自动创建一张进度卡片，随工具执行 **实时更新**（Patch API），不再刷屏。

- 卡片 header 实时显示状态：🔵 Running → 🟢 Completed / 🔴 Failed
- 每个工具调用使用 `**Tool**` 粗体标签 + 参数预览
- Bash/Shell 命令自动包装为 ` ```bash ``` ` 代码块
- 超过 10 个工具时自动截断，显示 "Showing latest updates only"
- 无工具调用的会话，卡片静默删除，不留垃圾消息

### 2. Thinking/Reasoning 实时显示

模型的推理过程实时显示在进度卡片中：

- 灰色 notation 文本 + 💭 前缀，与工具条目视觉区分
- 只保留最新一条 reasoning（避免卡片过长）
- 不触发卡片创建（仅工具调用创建卡片，避免竞态）
- 支持 DeepSeek / Qwen / Moonshot / OpenRouter 等多 provider 的 reasoning 格式
- Reasoning 通过两层机制从最终响应中剥离：
  - **主层**: `run.py` 在 `FEISHU_PROGRESS_STYLE=card` 时跳过 reasoning 拼接
  - **兜底**: 插件用 `startswith` + `rfind` 字符串操作剥离残留的 reasoning 前缀

### 3. Markdown 响应增强渲染

原始体验：Hermes 回复用 `post` 格式渲染 Markdown，表格错位、代码块样式粗糙。

安装后：所有含 Markdown 语法的响应自动使用 **Schema 2.0 卡片**渲染：

- 表格对齐更精确（超过 5 行数据自动分页，避免被飞书静默丢弃）
- 代码块与文本分离为独立元素，互不干扰
- 链接渲染更美观
- 无多余 header，直接展示内容

### 4. 网关重启容错

- 活跃卡片 ID 持久化到 `feishu_active_cards.json`
- 网关重启后自动清理上次的遗留 "Running" 卡片
- 防止用户看到永远无法完成的进度卡片

### 5. 可配置开关

- 通过 `FEISHU_PROGRESS_STYLE=card` 环境变量激活
- 未设置时插件静默加载，不影响原有行为
- 仅影响飞书 adapter，CLI 模式不受影响

## 安装

### 1. 复制插件目录

```bash
cp -r feishu-card-progress ~/.hermes/plugins/feishu-card-progress
```

### 2. 启用插件

在 profile 的 `config.yaml` 中启用插件：

```yaml
# ~/.hermes/profiles/<your-profile>/config.yaml
plugins:
  enabled:
    - feishu-card-progress
```

### 3. 设置环境变量

在 profile 的 `.env` 文件中添加：

```bash
# ~/.hermes/profiles/<your-profile>/.env
FEISHU_PROGRESS_STYLE=card
```

### 4. Profile 模式额外步骤

如果使用 `--profile` 运行 gateway，需要将插件链接到 profile 的 plugins 目录：

```bash
mkdir -p ~/.hermes/profiles/<your-profile>/plugins
ln -s ~/.hermes/plugins/feishu-card-progress ~/.hermes/profiles/<your-profile>/plugins/
```

> **原因**：`--profile` 模式会将 `HERMES_HOME` 设为 `~/.hermes/profiles/<name>`，
> 插件发现路径也随之变为 `<profile>/plugins/`。符号链接确保 profile 也能找到插件。

### 5. 打上游补丁

插件需要对 Hermes 核心文件打少量补丁（共 4 处，详见 `skills/patch-upstream/SKILL.md`）。

最重要的补丁：在 `gateway/run.py` 中让 card 模式跳过 reasoning 拼接。

```bash
# 让 AI 自动打补丁：读取补丁指南并执行
# 或者手动参考 skills/patch-upstream/SKILL.md
```

### 6. 重启网关

```bash
hermes gateway restart
```

## 上游更新

Hermes 更新（`git pull`）后，核心文件补丁可能被覆盖。使用补丁 skill 重新应用：

```bash
cd ~/.hermes/hermes-agent
git stash push -m "feishu-card-progress patches"
git pull origin main
git stash pop  # 可能有冲突，按 skills/patch-upstream/SKILL.md 解决
```

或直接让 AI 执行：**"Hermes 更新了，帮我重新打 feishu-card 的补丁"**

详细的补丁清单和冲突解决指南见 `skills/patch-upstream/SKILL.md`。

## 工作原理

插件通过 monkey-patching 在运行时扩展 `FeishuAdapter` 和 `Agent`：

| 补丁对象 | 方法 | 行为 |
|----------|------|------|
| FeishuAdapter | `on_processing_start` | 清理遗留卡片，重置状态 |
| FeishuAdapter | `on_processing_complete` | 完成卡片（绿色/红色 + 页脚） |
| FeishuAdapter | `send()` | 拦截首个进度消息 → 创建卡片；剥离残留 reasoning 前缀 |
| FeishuAdapter | `edit_message()` | 拦截后续进度更新 → PATCH 卡片 |
| FeishuAdapter | `_build_outbound_payload` | Markdown 响应使用 Schema 2.0 卡片格式 |
| Agent | `__setattr__` | 拦截 `tool_progress_callback` 赋值，包装 reasoning 事件路由 |
| Agent | `_build_assistant_message` | 拦截 reasoning 提取，路由到卡片 handler |

进度消息通过 emoji 前缀检测（`⚙️`、`🔍` 等），与网关的 `progress_callback` 格式匹配。

Reasoning 事件通过 Agent 层拦截：当 gateway 将 `progress_callback` 赋给
`agent.tool_progress_callback` 时，插件自动包装该回调，在 `reasoning.available`
事件到达 gateway 的过滤逻辑之前将其路由到卡片 handler。由于 agent 在线程池中
运行而 gateway 在主线程事件循环中，使用 `asyncio.run_coroutine_threadsafe`
跨线程调度卡片更新。

### 数据流

```
Agent Thread                    Gateway Event Loop
──────────                      ──────────────────
LLM response
  ├─ reasoning.available ──→ _wrap_progress_callback
  │                         └─→ _handle_reasoning_event
  │                              └─→ asyncio.run_coroutine_threadsafe
  │                                   └─→ handler.on_thinking() ──→ PATCH card
  │
  ├─ tool.started ──→ progress_callback ──→ adapter.send()
  │                    └─→ _patched_send
  │                         └─→ handler.on_tool_started() ──→ Create/PATCH card
  │
  └─ final response ──→ run.py (skip reasoning prepend)
                        └─→ adapter.send()
                             └─→ _patched_send (strip reasoning fallback)
                                  └─→ _patched_build_outbound_payload
                                       └─→ Schema 2.0 card ──→ POST message
```

## 文件结构

```
feishu-card-progress/
├── plugin.yaml                    # 插件清单
├── __init__.py                    # 注册函数 + monkey-patching + 剥离逻辑
├── card_handler.py                # FeishuCardHandler 核心逻辑
├── CHANGELOG.md                   # 修复记录
├── skills/
│   └── patch-upstream/
│       └── SKILL.md               # 上游补丁指南（AI 可读）
├── assets/
│   ├── before.png
│   └── after.png
└── README.md
```

## Reasoning / Thinking

Hermes 原生支持 reasoning 配置：

```yaml
# profile config.yaml
agent:
  reasoning_effort: medium    # 推理深度: low / medium / high

display:
  show_reasoning: true        # CLI 中显示推理过程
```

模型的 reasoning 内容通过多种格式返回：

| 格式 | Provider | 说明 |
|------|----------|------|
| `message.reasoning` | DeepSeek, Qwen | 直接字段 |
| `message.reasoning_content` | Moonshot AI, Novita | 替代字段名 |
| `message.reasoning_details` | OpenRouter (unified) | `{type, summary}` 数组 |

Hermes 的 `_extract_reasoning()` 函数（`run_agent.py`）会依次尝试以上格式，
最后回退到从 `content` 中提取 `<think...</think*>` 标签内容。

Card 模式下，reasoning 只在进度卡片中显示，不出现在最终响应正文里。

## 与 cc-connect 的对比

| 功能 | cc-connect | 本插件 |
|------|-----------|--------|
| 进度卡片 | Schema 2.0 | Schema 2.0 |
| 工具标签 | `<text_tag>` 彩色 | `**粗体**` markdown |
| Reasoning 显示 | 进度卡片内 | 进度卡片内 |
| 流式文本预览 | `streamPreview` 有节流 | 未实现 |
| TodoWrite 格式化 | ✅🔄⏳ 图标 | 未实现 |
| 表格分页 | 无 | 自动分页（>5 行） |
| 网关重启容错 | 无 | 持久化 + 自动清理 |

## 依赖

- Hermes Agent >= 最新版本（需要插件系统支持）
- `lark_oapi` Python SDK（飞书官方 SDK）
- 飞书平台适配器已配置（`FEISHU_APP_ID`、`FEISHU_APP_SECRET`）

## 许

MIT
