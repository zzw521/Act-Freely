---
kind: frontend_style
name: MUI + Tailwind 双轨样式体系与设计令牌
category: frontend_style
scope:
    - '**'
source_files:
    - tongtian-tauri/src/renderer/theme/tokens.ts
    - tongtian-tauri/src/renderer/theme/theme.ts
    - tongtian-tauri/src/renderer/styles/globals.css
    - tongtian-tauri/tailwind.config.js
    - tongtian-tauri/postcss.config.js
    - tongtian-tauri/package.json
---

通天办公前端采用 **Material-UI (MUI) v5** 作为核心组件库，配合 **Tailwind CSS v3** 进行原子化布局与全局样式扩展，并通过独立的 **设计令牌（Design Tokens）** 文件统一管理色彩、圆角、阴影、字体、间距与断点等视觉规范。整体风格遵循「品牌绿主色 + 石板灰中性色 + 柔和阴影 + 大圆角」的 AionUi 风格指南。

### 1. 使用的系统与工具
- **组件库**: `@mui/material` + `@mui/icons-material`，通过 `createTheme` 定制主题，覆盖按钮、卡片、输入框、对话框、标签、顶栏、抽屉、选项卡、表格、提示条、工具提示、菜单等所有常用组件。
- **CSS 框架**: `tailwindcss` + `autoprefixer` + `postcss`，用于原子类与全局层样式。
- **样式引擎**: `@emotion/react` + `@emotion/styled`，为 MUI 提供 CSS-in-JS 支持。
- **构建工具**: Vite 5，PostCSS 8，TypeScript 5。

### 2. 核心文件与位置
- `src/renderer/theme/tokens.ts` — 设计令牌集中定义：primary/secondary/accent 三色阶、语义色（success/warning/error/info）、中性灰阶、背景色、圆角系统、阴影系统、间距系统、字体族、字号、字重、断点、过渡动画。
- `src/renderer/theme/theme.ts` — MUI 主题工厂函数 `createAppTheme(mode)`，根据 light/dark 模式动态生成完整 Theme 对象，并导出 `lightTheme`、`darkTheme` 及响应式版本。
- `src/renderer/styles/globals.css` — Tailwind 入口，使用 `@layer base/components/utilities` 组织全局基础样式、自定义组件类（`.btn-primary`、`.btn-secondary`、`.card`、`.input-field`）以及 Markdown 渲染样式（`.markdown-body`）。
- `tailwind.config.js` — Tailwind 主题扩展：colors（primary/secondary/accent/surface）、borderRadius、boxShadow 与 content 扫描路径。
- `postcss.config.js` — PostCSS 插件链：tailwindcss → autoprefixer。
- `package.json` — 依赖声明确认 MUI、Emotion、Tailwind、Vite、TypeScript 等版本。

### 3. 架构与约定
- **双轨样式策略**：MUI 负责业务组件的视觉一致性（按钮、表单、卡片、对话框等），Tailwind 负责页面布局、间距、排版与全局基础样式；两者通过 tokens.ts 共享设计变量，避免硬编码颜色值。
- **主题模式切换**：通过 `createAppTheme('light' | 'dark')` 生成两套主题，背景、文字、边框、阴影均按模式动态取值，实现完整的明暗主题支持。
- **组件样式约定**：
  - 按钮统一使用 `disableElevation: true`、`textTransform: 'none'`、`borderRadius: '10px'`，强调色为主品牌绿。
  - 卡片使用 `borderRadius: '16px'` + 细边框 + 柔和阴影。
  - 输入框使用 `variant: 'outlined'`、`size: 'medium'`、`borderRadius: '12px'`。
  - 顶栏使用 `backdropFilter: 'blur(12px)'` 毛玻璃效果。
- **Markdown 内容样式**：在 `.markdown-body` 中统一定义标题、列表、代码块、引用、表格、链接等排版规则，确保聊天消息内嵌内容的视觉一致性。
- **滚动条定制**：全局 `::-webkit-scrollbar` 样式，6px 宽度、半透明轨道、圆角滑块，hover 时加深。

### 4. 约定与约束
- **颜色使用约束**：所有颜色必须从 `tokens.ts` 中的 `primaryColors`、`secondaryColors`、`accentColors`、`semanticColors` 引入，禁止在组件中硬编码十六进制颜色值。
- **圆角层级**：小组件 10px、中组件 12px、大卡片 16px、超大 20px，通过 `borderRadius` 常量统一管理。
- **阴影层级**：使用 `shadows.sm/md/lg/xl/2xl` 常量，禁止随意编写 box-shadow。
- **字体系统**：优先使用 PingFang SC / Hiragino Sans GB / Microsoft YaHei，英文回退到系统字体栈。
- **响应式断点**：统一使用 tokens.ts 中定义的 breakpoints（sm: 640px, md: 768px, lg: 1024px, xl: 1280px, 2xl: 1536px）。
- **组件样式覆盖**：所有 MUI 组件的样式修改必须通过 `styleOverrides` 或 `sx` prop 完成，禁止直接修改全局 CSS 类名。
- **Tailwind 内容扫描**：仅扫描 `./src/renderer/**/*.{js,ts,jsx,tsx,html}`，避免打包无用样式。
- **主题扩展点**：新增设计变量应优先添加到 `tokens.ts`，再由 `theme.ts` 消费，保持单一数据源。