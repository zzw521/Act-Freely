# 服务化部署与命令行

本文只记录**实际能跑通**的部署方式，并明确哪些部分尚未在本仓库开发机验证。
桌面版（Tauri）不需要 Docker，也不经过这里的令牌流程。

## 验证状态（2026-10-22）

**已在 Windows 开发机验证**

- `engine/server.py --web-dir <dist>`：前端构建产物与 `/api/*` 同端口托管，API 不被静态站点覆盖。
- `TT_ENGINE_TOKEN`：`/api/*` 缺失或错误令牌返回 401 `ENGINE_UNAUTHORIZED`；`Authorization: Bearer` 与 `x-tt-engine-token` 两种头都可用；`/health` 与静态文件不要求令牌。
- 非回环绑定准入：`--host 0.0.0.0` 必须同时设置 `TT_ALLOW_REMOTE_BIND=1` 与非空 `TT_ENGINE_TOKEN`，否则进程拒绝启动。
- `scripts/tt-cli.mjs` 的 `health` / `chat` / `doctor`：真实 HTTP/SSE、令牌只读环境变量、拒绝 `--token`。
- 网页令牌输入框：401 时提示输入，令牌只写当前标签页 `sessionStorage`。
- 以上由 `tests/integration/engine_service_cli_selftest.py`、`tests/unit/deploy/tt-cli.test.ts`、`tests/unit/deploy/engine-service-auth.test.ts` 覆盖。

**未验证**

- `deploy/Dockerfile` 的构建与运行：开发机没有 Docker，容器层（基础镜像、`pip install`、卷权限）未实测封版。
- 真实反向代理 / TLS / 公网链路与 SSE 长连接的代理行为。
- 真实厂商模型经 CLI/服务化模式的对话质量与长任务表现。

未验证的部分不应当作“已经可上线”；如果容器构建失败，以引擎直跑方式为准，不要把未跑通的镜像当成承诺。

## 一、本地：引擎 + 静态前端（已推荐用于自测）

```bash
cd tongtian-tauri
npm ci
# 必须显式关闭 Mock，否则构建产物仍是 Mock 通道，不会调用引擎
VITE_MOCK=false npm run build
python -m pip install -r engine/requirements.txt

# 回环地址可不开令牌；打开 http://127.0.0.1:18000
python engine/server.py --host 127.0.0.1 --port 18000 \
  --work-dir ./sessions --web-dir ./dist
```

模型配置有两种方式：页面里在「模型设置」新增（写入 `{TONGTIANWORK_HOME 或 ~/.tongtianwork}/models.json`），或设置引擎环境变量 `TT_API_KEY` / `TT_PROVIDER` / `TT_MODEL`。页面方式不会把密钥回传到界面。

## 二、服务化：令牌 + 静态前端

```bash
# 令牌由部署方的密钥管理生成并保存，不要写进仓库、镜像或命令行参数
export TT_ENGINE_TOKEN='<由密钥管理系统注入>'
export TT_ALLOW_REMOTE_BIND=1

python engine/server.py --host 0.0.0.0 --port 18000 \
  --work-dir /data/work --web-dir ./dist
```

打开 `http://<主机>:18000`。首次 API 请求会因 401 弹出「引擎需要服务令牌」，输入
`TT_ENGINE_TOKEN` 即可；令牌只保存在当前标签页 `sessionStorage`，关闭标签页失效，
不写 `localStorage`、不写 URL、不进构建产物。

若由反向代理统一注入 `x-tt-engine-token`，页面不会看到 401，也就不需要输入；此时
必须保证代理不会把该头透传给未授权调用方，并在代理层终止 TLS。

**安全边界**

- 这里没有用户体系：一个令牌就是全部权限。任何持有令牌的人都能读写 `/data/work`、调用已启用的工具。只应在可信网络内使用，不要直接暴露到公网。
- 服务化模式没有桌面专属能力：终端 AI+、电脑操控、浏览器 AI+、远程桌面、应用内 Hook 与审计视图、Tauri 文件对话框/系统应用打开、任务栏角标、独立任务窗口都依赖桌面壳，页面会如实标注不可用。
- 引擎不是操作系统级沙箱；模型调用会把任务内容发送给所选模型服务商。`run_command` / `fetch_url` 仍受工作区沙箱显式授权与白名单约束，未授权时不注册。
- `/health` 与静态资源不需要令牌；不要在这些路径上放敏感内容。

## 三、命令行（已实现）

```bash
# 令牌只从环境变量读取，--token 会被明确拒绝
export TT_ENGINE_URL='http://127.0.0.1:18000'
export TT_ENGINE_TOKEN='<令牌>'   # 回环且未开令牌时可不设

node scripts/tt-cli.mjs doctor
node scripts/tt-cli.mjs health --json
node scripts/tt-cli.mjs chat "汇总本周待办并写入 report.md" --work-dir ./work
```

`chat` 会流式打印 plan / step / step_done / deliver / done；引擎报错或流提前结束时
以非 0 退出，不把半截结果当成功。CLI 不提供终端、电脑操控、远程桌面等桌面能力。

## 四、Docker（模板，未验证）

```bash
cd tongtian-tauri
docker build -f deploy/Dockerfile -t tongtian-office:dev .

# TT_ENGINE_TOKEN 必须提供；缺令牌时引擎会拒绝绑定 0.0.0.0 并退出
docker run --rm -p 18000:18000 \
  -e TT_ENGINE_TOKEN='<令牌>' \
  -v tt-data:/data \
  tongtian-office:dev
```

- 镜像内以非 root 用户运行，`/data` 是唯一需要持久化的目录：`models.json`、意识文件、IM 凭据、检查点、`work/`。
- 镜像使用 `engine/requirements.txt`（跨平台直接依赖），**不是** `engine/requirements.lock.txt`；后者是 Windows 3.12 embeddable 运行时的随包锁表，Linux 容器不能照用。
- 构建期固定 `VITE_MOCK=false`；若自行改 Dockerfile 构建前端，必须保留这个设置。
- 没有 Docker 环境时请直接使用第二节的引擎直跑方式；不要把未构建过的镜像写进交付承诺。

## 五、数据与升级

- 升级前备份 `/data`（或 `TONGTIANWORK_HOME` 指向的目录）与工作目录；本仓库的迁移只做增量新增，不删除已有数据。
- 不要用 `rm -rf` 清理服务化数据目录；确需清理时先停引擎、确认路径、移入回收站/备份。
- 更换 `TT_ENGINE_TOKEN` 后，所有已打开的页面需要刷新并重新输入；CLI 重新读取环境变量即可。
- 反向代理需要为 SSE 关闭响应缓冲（例如 Nginx `proxy_buffering off`），否则对话事件可能被攒到最后才到。
