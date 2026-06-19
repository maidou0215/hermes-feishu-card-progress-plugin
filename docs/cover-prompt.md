# README 封面 Banner 生图提示词

> README 顶部的 `readme-cover.png` 用 AI 生图工具生成。本文档保存提示词，供随时复用/调整。
> 注意：AI 画不出准确的飞书卡片 UI（会失真），所以提示词走「概念氛围」路线；真实效果靠 `assets/showcase.png` 体现。

## 英文提示词（推荐喂 DALL·E / Midjourney）

> A premium hero banner for an open-source developer tool. Concept: real-time streaming chat cards on Feishu/Lark messenger. Composition: a single floating glassmorphic chat card hovering over a deep navy-to-electric-blue gradient background; the card carries an abstract live progress indicator with glowing checkmarks and a spinner, plus a thin metrics footer line with tiny icons (clock, robot, wrench, arrows). Luminous data streams and particles flow around the card suggesting real-time updates. Feishu brand blue (#3370FF) with turquoise and cyan accents, soft neon glow, dark mode, clean minimalist aesthetic, subtle depth of field, cinematic lighting, ultra-detailed, 16:9, no text or minimal elegant typography. Professional GitHub project cover.

## 中文提示词（喂即梦 / 国产工具）

> 开源开发者工具的产品封面横幅。主题：飞书/Lark 实时流式聊天卡片。构图：一张悬浮的玻璃拟态聊天卡片漂浮在深藏青到电光蓝的渐变背景上，卡片上有抽象的实时进度指示器（发光勾选、加载动画）和一行细窄的指标页脚（时钟/机器人/扳手/箭头小图标）。卡片周围有流动的光带和粒子暗示实时更新。飞书品牌蓝 #3370FF 配青绿点缀，柔和霓虹辉光，深色模式，极简科技美学，电影感打光，16:9，无文字或极少优雅排版。

## 生成后处理

1. 裁成宽高比 **3:1 或 16:9**，导出宽度 ≥ 1600px。
2. 命名为 `assets/readme-cover.png`。
3. 若生图带了乱码文字，用 Figma / PS 删掉文字层，标题交给 README 的 emoji + 纯文本。
4. 替换 README.md / README.en.md 顶部的 banner 占位注释。
