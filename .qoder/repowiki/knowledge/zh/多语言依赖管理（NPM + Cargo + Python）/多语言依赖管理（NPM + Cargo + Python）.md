---
kind: dependency_management
name: 多语言依赖管理（NPM + Cargo + Python）
category: dependency_management
scope:
    - '**'
source_files:
    - tongtian-tauri/package.json
    - tongtian-tauri/package-lock.json
    - tongtian-tauri/src-tauri/Cargo.toml
    - tongtian-tauri/src-tauri/Cargo.lock
    - .tools/probe-agentscope.ps1
    - .tools/probe-qwenpaw.ps1
---

本项目为 Tauri 2.x + Rust 后端 + React 前端 + Python Agent 引擎的桌面应用，依赖管理采用多包管理器并存的方式：

1. **Node.js 依赖**：通过 `package.json` 声明依赖，使用 `package-lock.json`（lockfileVersion 3）锁定版本，npm 作为包管理器。依赖分为运行时依赖（React、MUI、Zustand、Tauri API 等）和开发依赖（Vite、TypeScript、Vitest、TailwindCSS 等），未使用 vendoring 策略。

2. **Rust 依赖**：通过 `src-tauri/Cargo.toml` 声明，使用 `Cargo.lock` 锁定版本。核心依赖包括 tauri 2.x、tokio、reqwest、rusqlite、serde 等，release 配置启用了 LTO、strip 等优化选项。

3. **Python 依赖**：Agent 引擎代码位于 `tongtian-tauri/src/agent/` 目录下，但仓库中未发现 `requirements.txt`、`pyproject.toml`、`Pipfile` 或 `poetry.lock` 等标准依赖声明文件。Python 依赖通过 `.tools/` 目录下的 PowerShell 脚本间接探测（如 `probe-agentscope.ps1` 使用 `python -m pip list` 检查 agentscope、openai、dashscope 等包），表明 Python 环境可能通过外部方式安装而非项目内声明。

4. **构建工具链依赖**：`.tools/` 目录包含 JDK8、Maven 的一键下载与配置脚本（`download-env.ps1`、`setup-env.ps1`、`check-m2.ps1`），以及 Rust 环境设置脚本（`setup-rust.ps1`），用于 Windows 本地开发环境的快速搭建。

5. **忽略规则**：`.gitignore` 明确忽略 `node_modules/`、`dist/`、`src-tauri/target/` 等构建产物和依赖目录，符合各语言包管理器的常规实践。

当前 Python 依赖管理缺乏标准化的声明文件，是该项目在依赖管理方面最明显的缺失点。