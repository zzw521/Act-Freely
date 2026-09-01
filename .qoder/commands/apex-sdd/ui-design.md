---
description: Generate UI design specification — color system, typography, component states, spacing — from interaction prototype, with optional Figma source via figma-context-mcp. Optional phase before build, for 前后端应用 only.
---

Invoke the `frontend-ui-designer` skill.

Read the interaction prototype and flow documentation produced by `/split`. If the user provides a Figma file URL (or `fileKey:nodeId`), the skill will additionally call `figma-context-mcp` to extract authoritative design tokens directly from the source file.

## Inputs

**必选**

- `docs/prd/prd-{业务}-交互流程说明.md`
- `docs/prd/prd-{业务}-交互原型/`（可运行的 Vite 交互原型工程）

**可选**

- Figma file URL（支持两种格式，二选一）或 `fileKey:nodeId`，由用户在 prompt 中传入。提供后将以 Figma 为权威设计源。
  - Design 模式链接（新版 Figma 默认）：`https://www.figma.com/design/{fileKey}/{name}?node-id={nodeId}`
  - File 模式链接（老版）：`https://www.figma.com/file/{fileKey}/{name}?node-id={nodeId}`
  - Dev Mode 链接（带 `?m=dev`）与 Design 模式等效，可直接使用
  - `node-id` 中的 `-` 与 `:` 等价（如 `361-109357` ≡ `361:109357`）

## Figma MCP 配置（可选，仅当使用 Figma 输入时需要）

本命令使用社区主流的 [Framelink Figma-Context-MCP](https://github.com/GLips/Figma-Context-MCP)，通过 `npx` 即装即用，仅需 Figma Personal Access Token。

在项目根 `.mcp.json` 注册 server：

```json
{
  "mcpServers": {
    "figma": {
      "command": "npx",
      "args": ["-y", "figma-developer-mcp", "--stdio"],
      "env": {
        "FIGMA_API_KEY": "<your-figma-personal-access-token>"
      }
    }
  }
}
```

> **Token 获取**：Figma → Settings → Personal access tokens → Generate new token（仅需 `File content` 读权限）。建议通过环境变量或本地 `.env` 注入，**不要提交到仓库**。

## 工作流分支

### 分支 A：提供了 Figma URL（权威源模式）

1. 解析 Figma URL，提取 `fileKey` 与可选 `nodeId`
2. 通过 figma-context-mcp 抽取 layout 树 / styles / components / 关键资源
3. 沉淀 design tokens：色彩（主/辅/语义）、字体（family/size/weight/line-height）、间距、圆角、阴影、动效曲线
4. 与 `docs/prd/prd-{业务}-交互原型/` 做交叉一致性校验，差异 ≥ 5% 时记录到「设计差异」附录
5. 生成 UI 规范文档；在文档顶部「数据来源」字段记录 Figma file URL + 抓取时间戳

### 分支 B：未提供 Figma URL（原型逆推模式，默认）

1. 分析可运行原型（`docs/prd/prd-{业务}-交互原型/`）以提取视觉模式
2. 引用交互流程图获取页面级布局与状态流转
3. 生成结构化 UI 规范，覆盖色彩 token、字体、间距、组件变体、交互状态

## Output

保存至 `docs/spec/`：

- `spec-{业务}-UI规范文档.md` — 色彩 token、字体规范、间距系统、组件变体状态、交互动效规范
  - 文档顶部新增「数据来源」字段：`Figma | 原型工程`，若为 Figma 则附 file URL 与抓取时间戳
- 分支 A 时附产物：`docs/spec/assets/{业务}/` — Figma 导出的图标 / 插图资源（仅在使用 Figma 输入时生成）

## Quality Gate

- 色彩 token 完整覆盖主/辅/语义色
- 组件变体覆盖 default / hover / active / disabled 四态
- 与交互原型视觉一致性 ≥ 90%
- **使用 Figma 输入时**：UI 规范 token 与 Figma 源文件一致性 ≥ 95%（颜色 HEX、字号、间距像素值精确匹配）

## Invocation

> 无 Figma：`/apex-sdd:ui-design 业务：{业务}`
> 有 Figma（design 链接）：`/apex-sdd:ui-design 业务：{业务}，figma:https://www.figma.com/design/{fileKey}/{name}?node-id=361-109357`
> 有 Figma（file 链接）：`/apex-sdd:ui-design 业务：{业务}，figma:https://www.figma.com/file/{fileKey}/{name}?node-id=1:2`

## Next step

Run `/apex-sdd:plan` to break down all spec documents (including UI spec) into implementation tasks.

> Suggested prompt: `/apex-sdd:plan 业务：{业务}，输入：docs/spec/ 下的全套设计文档（后端模块设计文档 + 前端模块设计文档 + API契约文档 + DDL设计文档 + UI规范文档）`
