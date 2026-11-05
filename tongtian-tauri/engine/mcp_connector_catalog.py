"""连接器目录（D-39：MCP 连接器生态与接入模式，引擎侧注册表）

台账 D-39 的痛点是「连接器市场可安装，但每个服务仍要手工填配置」：用户不知道该填
哪些字段、哪些是密钥、OAuth 要什么 scope、回调往哪填。本模块把这件事变成
**一条目录记录 + 一份表单字段清单**：界面按 `form_fields()` 渲染输入框，按
`build_spec()` 生成运行期 spec，全程不需要用户写 JSON。

三种接入模式（与台账方案同一口径，internal 后置）：

- `stdio`：本机拉起进程（官方参考服务器多为 `npx` / `uvx` 一条命令）；
- `remote`：服务商托管的远程端点（streamable HTTP / SSE），鉴权可以是
  OAuth 2.1（PKCE，由 Rust 侧 `mcp/oauth.rs` 执行）、Bearer 令牌或 URL 里的 key；
- `gateway`：企业自建 MCP 网关，一个端点聚合多个上游。工具名按约定带上游前缀
  （`上游__工具`），目录据此把 allowlist 写成 `上游/*` 这类范围。

**事实的边界要如实标出来**（这是本模块最重要的一条口径）：

- `verified` 字段记录这条记录里的端点/包名是**怎么确认的**：`"doc"` 表示 2026-11-04
  由公开文档核对（见 `docs`），`"unverified"` 表示按既有资料填写、**接入时必须以服务商
  当前文档复核**。不假装核对过没核对的东西。
- `scopes` 只在有明确文档时填；远程 OAuth 的 scope 常由服务端动态下发，这种情况
  `scopesFromServer=True`，界面就该留空而不是编一个。
- 本目录只是**模板**：没有真实凭据、没有联网握手，所以任何连接器能不能真的连上、
  OAuth 能不能真的拿到令牌，**真机未验收**。工具白名单里的建议工具名同理——
  以服务商实际 `tools/list` 返回为准。

纯数据 + 纯函数：不读盘、不起进程、不碰网络，因此可以在任何环境自测。
"""

from __future__ import annotations

from typing import Any

CATEGORIES = ("办公", "知识", "协作", "开发")
MODES = ("stdio", "remote", "gateway")
AUTH_TYPES = ("none", "url_key", "env_token", "bearer", "oauth_pkce")
TRANSPORTS = ("stdio", "streamable_http", "sse")

# 允许的占位符长度上限：字段值最终进 spec / 命令行，过长通常是贴错了东西
MAX_FIELD_CHARS = 512
MAX_FIELDS_PER_CONNECTOR = 24


def _s(value: Any) -> str:
    return str(value or "").strip()


def _entry(
    *,
    id: str,
    name: str,
    category: str,
    mode: str,
    transport: str,
    summary: str,
    docs: str,
    verified: str,
    icon: str,
    command: str = "",
    args: tuple[str, ...] = (),
    url: str = "",
    auth: str = "none",
    scopes: tuple[str, ...] = (),
    scopes_from_server: bool = False,
    callback_note: str = "",
    fields: tuple[dict, ...] = (),
    optional_args: dict | None = None,
    headers: dict | None = None,
    allowlist: tuple[str, ...] = (),
    gateway_note: str = "",
    timeout_secs: float = 60.0,
) -> dict:
    return {
        "id": id,
        "name": name,
        "category": category,
        "mode": mode,
        "transport": transport,
        "summary": summary,
        "docs": docs,
        "verified": verified,
        "icon": icon,
        "command": command,
        "args": list(args),
        "url": url,
        "auth": auth,
        "scopes": list(scopes),
        "scopesFromServer": scopes_from_server,
        "callbackNote": callback_note,
        "fields": [dict(f) for f in fields],
        "optionalArgs": {k: [str(p) for p in v] for k, v in (optional_args or {}).items()},
        "headers": dict(headers or {}),
        "suggestedAllowlist": list(allowlist),
        "gatewayNote": gateway_note,
        "timeoutSecs": timeout_secs,
    }


def _field(
    key: str,
    label: str,
    *,
    secret: bool = False,
    required: bool = True,
    placeholder: str = "",
    hint: str = "",
) -> dict:
    """一个输入框：界面按它渲染，用户不需要知道背后是 env 还是 header 还是 URL 参数"""
    return {
        "key": key,
        "label": label,
        "secret": secret,
        "required": required,
        "placeholder": placeholder,
        "hint": hint,
    }


# ── 内置目录（≥8，覆盖办公 / 知识 / 协作） ──────────────────────
CONNECTORS: tuple[dict, ...] = (
    _entry(
        id="filesystem",
        name="本地文件",
        category="办公",
        mode="stdio",
        transport="stdio",
        summary="把指定目录暴露给模型读写（官方参考服务器）",
        docs="https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem",
        verified="doc",
        icon="folder",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-filesystem", "{root}"),
        fields=[
            _field(
                "root",
                "允许访问的目录",
                required=True,
                placeholder="D:/工作文档",
                hint="只有列出的目录可读写；多个目录请建多条连接器，别图省事给整盘",
            )
        ],
        allowlist=("read_file", "write_file", "list_directory", "search_files"),
        timeout_secs=60.0,
    ),
    _entry(
        id="memory",
        name="知识图谱记忆",
        category="知识",
        mode="stdio",
        transport="stdio",
        summary="实体/关系型长期记忆（官方参考服务器）",
        docs="https://github.com/modelcontextprotocol/servers/tree/main/src/memory",
        verified="doc",
        icon="brain",
        command="npx",
        args=("-y", "@modelcontextprotocol/server-memory"),
        allowlist=("create_entities", "create_relations", "search_nodes", "read_graph"),
    ),
    _entry(
        id="fetch",
        name="网页抓取",
        category="知识",
        mode="stdio",
        transport="stdio",
        summary="抓取网页并转 Markdown（官方参考服务器）",
        docs="https://github.com/modelcontextprotocol/servers/tree/main/src/fetch",
        verified="doc",
        icon="globe",
        command="uvx",
        args=("mcp-server-fetch",),
        allowlist=("fetch",),
    ),
    _entry(
        id="git",
        name="本地仓库",
        category="开发",
        mode="stdio",
        transport="stdio",
        summary="仓库状态、日志与差异（官方参考服务器）",
        docs="https://github.com/modelcontextprotocol/servers/tree/main/src/git",
        verified="doc",
        icon="git-branch",
        command="uvx",
        args=("mcp-server-git", "--repository", "{repo}"),
        fields=[
            _field(
                "repo",
                "仓库路径",
                required=True,
                placeholder="D:/code/demo",
                hint="留空则由服务器自行决定可访问范围，不建议",
            )
        ],
        allowlist=("git_status", "git_diff_unstaged", "git_log"),
    ),
    _entry(
        id="playwright",
        name="浏览器自动化",
        category="开发",
        mode="stdio",
        transport="stdio",
        summary="Playwright 驱动的页面操作（官方 MCP 包）",
        docs="https://github.com/microsoft/playwright-mcp",
        verified="doc",
        icon="browser",
        command="npx",
        args=("-y", "@playwright/mcp@latest"),
        fields=[
            _field(
                "profile",
                "浏览器用户数据目录",
                required=False,
                placeholder="留空＝每次全新会话",
                hint="填了会留存登录态：请放在受控目录，并在共享机器上留空",
            )
        ],
        optional_args={"profile": ["--user-data-dir", "{profile}"]},
        allowlist=("browser_navigate", "browser_snapshot", "browser_click", "browser_type"),
    ),
    _entry(
        id="amap-maps",
        name="高德地图",
        category="知识",
        mode="remote",
        transport="sse",
        summary="地理编码、路径与周边检索（服务商托管 SSE 端点，URL 带 key）",
        docs="https://lbs.amap.com/",
        verified="unverified",
        icon="map",
        url="https://mcp.amap.com/sse?key={key}",
        auth="url_key",
        fields=[
            _field(
                "key",
                "高德 Web 服务 Key",
                secret=True,
                required=True,
                placeholder="控制台申请的高德 Web 服务 Key",
                hint="密钥会出现在 URL 里：日志与快照只留指纹，不打印原值",
            )
        ],
        allowlist=("maps_geo", "maps_search_around", "maps_direction_driving"),
    ),
    _entry(
        id="github",
        name="GitHub",
        category="协作",
        mode="remote",
        transport="streamable_http",
        summary="仓库、议题与代码评审（GitHub 官方托管远程服务器）",
        docs="https://docs.github.com/copilot/customizing-copilot/extending-github-copilot-with-mcp",
        verified="doc",
        icon="code",
        url="https://api.githubcopilot.com/mcp/",
        auth="oauth_pkce",
        scopes_from_server=True,
        callback_note="远程 OAuth 由服务商下发授权与令牌端点，无需手工填回调",
        fields=[
            _field(
                "pat",
                "个人访问令牌（可留空走 OAuth）",
                secret=True,
                required=False,
                placeholder="ghp_… 或留空，点「一键授权」",
                hint="填了就以 Bearer 头接入；留空则走一键授权（PKCE），令牌刷新与撤销由桌面端负责",
            )
        ],
        allowlist=("get_issue", "list_issues", "search_code", "pull_request_read"),
    ),
    _entry(
        id="notion",
        name="Notion",
        category="知识",
        mode="remote",
        transport="streamable_http",
        summary="页面与数据库检索（服务商托管远程服务器）",
        docs="https://developers.notion.com/",
        verified="unverified",
        icon="note",
        url="https://mcp.notion.com/mcp",
        auth="oauth_pkce",
        scopes_from_server=True,
        callback_note="授权端点由服务商经 OAuth 元数据下发；接入前请按服务商当前文档核对地址",
        allowlist=("search", "fetch", "query_database"),
    ),
    _entry(
        id="linear",
        name="Linear",
        category="协作",
        mode="remote",
        transport="sse",
        summary="议题与项目（服务商托管 SSE 端点）",
        docs="https://linear.app/docs",
        verified="unverified",
        icon="ticket",
        url="https://mcp.linear.app/sse",
        auth="oauth_pkce",
        scopes_from_server=True,
        callback_note="接入前按服务商文档核对端点与授权方式",
        allowlist=("get_issues", "get_issue", "create_issue"),
    ),
    _entry(
        id="slack",
        name="Slack",
        category="协作",
        mode="stdio",
        transport="stdio",
        summary="消息与频道（官方 MCP 包，令牌从环境变量注入）",
        docs="https://api.slack.com/",
        verified="unverified",
        icon="message",
        command="uvx",
        args=("slack-mcp-server", "--transport", "stdio"),
        auth="env_token",
        fields=[
            _field(
                "botToken",
                "Bot 令牌（xoxb-…）",
                secret=True,
                placeholder="xoxb-…",
                hint="以 SLACK_BOT_TOKEN 注入子进程，不进命令行参数",
            ),
            _field(
                "userToken",
                "用户令牌（xoxc-…，可留空）",
                secret=True,
                required=False,
                placeholder="xoxc-…",
                hint="读取你已加入的频道内容时需要；留空则只有 Bot 视角",
            ),
            _field("teamId", "工作区 ID", placeholder="T00000000", hint="以 SLACK_TEAM_ID 注入"),
        ],
        allowlist=("slack_get_channels_list", "slack_get_channel_history"),
    ),
    _entry(
        id="context7",
        name="框架文档检索",
        category="知识",
        mode="stdio",
        transport="stdio",
        summary="库/框架的最新文档与代码示例（官方 MCP 包）",
        docs="https://context7.com/",
        verified="unverified",
        icon="book",
        command="npx",
        args=("-y", "@upstash/context7-mcp"),
        auth="env_token",
        fields=[
            _field(
                "apiKey",
                "API Key（可留空用免费额度）",
                secret=True,
                required=False,
                placeholder="留空＝匿名额度，容易限流",
                hint="以 CONTEXT7_API_KEY 注入",
            )
        ],
        allowlist=("resolve-library-id", "get-library-docs"),
    ),
    _entry(
        id="enterprise-gateway",
        name="自建 MCP 网关",
        category="办公",
        mode="gateway",
        transport="streamable_http",
        summary="企业内一个端点聚合多个上游连接器",
        docs="https://modelcontextprotocol.io/specification/2025-06-18/basic/transports",
        verified="doc",
        icon="gateway",
        url="https://{host}/mcp",
        auth="bearer",
        callback_note="网关通常由自建身份体系签发令牌；撤销后网关会回 401，引擎据此判定失效",
        fields=[
            _field("host", "网关地址", placeholder="mcp.internal.example.com"),
            _field(
                "token",
                "访问令牌",
                secret=True,
                placeholder="网关签发的 Bearer 令牌",
                hint="以 Authorization: Bearer 头注入，不进日志与快照",
            ),
        ],
        headers={"Authorization": "Bearer {token}"},
        gateway_note=(
            "聚合端点的工具名一般带上游前缀（如 jira__search）。白名单可写成 "
            "「上游/*」只放行某个上游；工具清单以网关实际返回为准。"
        ),
    ),
)

_INDEX = {_s(e["id"]).lower(): e for e in CONNECTORS}


def entries(category: str = "") -> list[dict]:
    """目录视图（界面/车道直接用）；可按分类过滤，未知分类返回空而不是假装全给"""
    want = _s(category)
    if want and want not in CATEGORIES:
        return []
    rows = [e for e in CONNECTORS if not want or e["category"] == want]
    return [dict(e) for e in sorted(rows, key=lambda e: (e["category"], e["id"]))]


def get(connector_id: str) -> dict | None:
    return _INDEX.get(_s(connector_id).lower())


def form_fields(connector_id: str) -> list[dict]:
    """要用户填什么（中文标签 + 是否密钥 + 提示）；这就是「免手写 JSON」的正面"""
    entry = get(connector_id)
    if not entry:
        return []
    return [dict(f) for f in entry["fields"]]


def _substitute(text: str, values: dict, *, keep_unresolved: bool = False) -> str:
    out = text
    for key, value in values.items():
        out = out.replace("{" + key + "}", str(value))
    if not keep_unresolved:
        # 剩下没被填掉的 {x} 一律抹净：命令行里留个花括号是哑弹，URL 里留它是 404
        for key in _placeholder_keys(out):
            out = out.replace("{" + key + "}", "")
    return out


def _placeholder_keys(text: str) -> list[str]:
    keys: list[str] = []
    rest = text
    while "{" in rest and "}" in rest:
        start = rest.index("{")
        end = rest.index("}", start)
        name = rest[start + 1 : end].strip()
        if name and name.isidentifier() and name not in keys:
            keys.append(name)
        rest = rest[end + 1 :]
    return keys


def missing_required(connector_id: str, values: dict) -> list[str]:
    """还缺哪些必填项（中文标签，界面直接摆在按钮旁边）"""
    entry = get(connector_id)
    if not entry:
        return [f"目录里没有连接器「{_s(connector_id) or '（空）'}」，请从目录重新选择"]
    out: list[str] = []
    for field in entry["fields"]:
        if not field["required"]:
            continue
        if not _s((values or {}).get(field["key"])):
            out.append(field["label"])
    return out


def build_spec(
    connector_id: str,
    values: dict | None = None,
    *,
    token: str = "",
    expires_at_ms: int | None = None,
    server_id: str = "",
) -> tuple[dict, list[str]]:
    """目录记录 + 用户填的值 → 运行期 spec（`mcp_runtime.normalize_servers` 认得的形状）。

    返回 `(spec, notes)`：notes 一律中文，写清楚哪些值没填、令牌是刚注入的还是复用的。
    抛错只有一种情况——目录里根本没有这条记录，那属于调用方传错 id。
    """
    entry = get(connector_id)
    if entry is None:
        raise ValueError(f"MCP_CONNECTOR_UNKNOWN: 目录里没有连接器「{_s(connector_id)}」")
    values = values if isinstance(values, dict) else {}
    notes: list[str] = []

    filled: dict[str, str] = {}
    for field in entry["fields"]:
        raw = _s(values.get(field["key"]))
        if len(raw) > MAX_FIELD_CHARS:
            raw = raw[:MAX_FIELD_CHARS]
            notes.append(f"字段「{field['label']}」超过 {MAX_FIELD_CHARS} 字，已截断")
        if raw:
            filled[field["key"]] = raw
    # 桌面端授权回来的令牌也算「这个字段填了」：PKCE 成功后用户不会再手输一遍，
    # 只认 values 就等于把刚拿到的令牌当成缺失必填项。
    if _s(token) and "token" in {f["key"] for f in entry["fields"]} and not filled.get("token"):
        filled["token"] = _s(token)[:MAX_FIELD_CHARS]
    missing = [f["label"] for f in entry["fields"] if f["required"] and f["key"] not in filled]
    if missing:
        raise ValueError(
            "MCP_CONNECTOR_MISSING_FIELD: " + f"连接器「{entry['name']}」还缺 " + "、".join(missing)
        )
    unknown = [k for k in values if k not in {f["key"] for f in entry["fields"]}]
    if unknown:
        notes.append(
            f"这些字段不在「{entry['name']}」的表单里，已忽略：{'、'.join(sorted(unknown)[:8])}"
        )

    spec: dict = {
        "serverId": _s(server_id) or f"catalog-{entry['id']}",
        "name": entry["name"],
        "transport": entry["transport"],
        "catalogId": entry["id"],
        "mode": entry["mode"],
        "timeoutSecs": entry["timeoutSecs"],
    }
    if entry["transport"] == "stdio":
        command = _s(_substitute(entry["command"], filled))
        if not command:
            raise ValueError(f"MCP_CONNECTOR_MISSING_FIELD: 连接器「{entry['name']}」缺启动命令")
        args = [_s(_substitute(a, filled)) for a in entry["args"]]
        # 可选开关：只有用户真填了值才拼出这一段参数，留空就一个 token 都不多
        optional: list[str] = []
        for key, pieces in (entry.get("optionalArgs") or {}).items():
            if _s(filled.get(key)):
                optional.extend(_s(_substitute(p, filled)) for p in pieces)
        spec["command"] = command
        spec["args"] = [a for a in args if a] + [a for a in optional if a]
    else:
        url = _s(_substitute(entry["url"], filled))
        # URL 里没被填掉的占位符说明值本身缺失（例如 key 为空），此时宁可不给 URL
        if not url or "{" in url:
            raise ValueError(f"MCP_CONNECTOR_MISSING_FIELD: 连接器「{entry['name']}」缺服务地址")
        spec["url"] = url

    headers = {
        k: _substitute(v, {**filled, "token": _s(token)})
        for k, v in entry["headers"].items()
    }
    env: dict[str, str] = {}
    auth = entry["auth"]
    bearer = _s(token)
    if auth == "oauth_pkce" or (auth == "bearer" and bearer):
        bearer = bearer or _s(filled.get("pat"))
        if bearer:
            headers["Authorization"] = f"Bearer {bearer}"
            notes.append("已注入访问令牌（Bearer 头）；过期后由桌面端刷新，撤销后立即失效")
        elif auth == "oauth_pkce":
            notes.append(
                f"连接器「{entry['name']}」走一键授权：还没有令牌，"
                "请先在桌面端点「一键授权」完成 PKCE，再连接"
            )
        elif auth == "bearer":
            raise ValueError(
                f"MCP_CONNECTOR_MISSING_FIELD: 连接器「{entry['name']}」需要访问令牌才能接入"
            )
    elif auth == "env_token":
        mapping = {
            "botToken": "SLACK_BOT_TOKEN",
            "userToken": "SLACK_USER_TOKEN",
            "teamId": "SLACK_TEAM_ID",
            "apiKey": "CONTEXT7_API_KEY",
        }
        for key, env_name in mapping.items():
            value = _s(filled.get(key))
            if value:
                env[env_name] = value
        if not env:
            notes.append(f"连接器「{entry['name']}」没有注入任何令牌，很可能连不上")
    if expires_at_ms is not None:
        spec["authExpiresAtMs"] = int(expires_at_ms)
    if headers:
        spec["headers"] = headers
    if env:
        spec["env"] = env
    if entry["mode"] == "gateway":
        notes.append(entry["gatewayNote"])
    if entry["verified"] != "doc":
        notes.append(
            f"「{entry['name']}」的端点/包名以服务商当前文档为准（本目录未联网核验）"
        )
    return spec, notes


def reconcile(servers: list[dict] | None) -> dict:
    """把运行期连接器清单与目录对账（台账方案里「统一 Rust / Python 的连接器注册表」这一步）。

    只做识别与说明，不改任何配置：命中目录的行标出 `catalogId` 与还缺什么，
    没命中的行如实标 `manual`（手工配置，不是坏东西）。
    """
    rows: list[dict] = []
    unmatched = 0
    for spec in servers or []:
        if not isinstance(spec, dict):
            continue
        cid = _s(spec.get("catalogId")).lower()
        server_id = _s(spec.get("serverId"))
        entry = get(cid) if cid else None
        if entry is None:
            # 桌面侧的连接器没有 catalogId：按命令/URL 反查目录（同一条官方包不该被当成两个连接器）
            command = _s(spec.get("command"))
            url = _s(spec.get("url"))
            for candidate in CONNECTORS:
                if command and command == candidate["command"] and _args_overlap(
                    spec.get("args"), candidate["args"]
                ):
                    entry = candidate
                    break
                if url and _bare_url(url) and _bare_url(candidate["url"]) and url.startswith(
                    _bare_url(candidate["url"])
                ):
                    entry = candidate
                    break
        if entry is None:
            unmatched += 1
            rows.append(
                {
                    "serverId": server_id,
                    "name": _s(spec.get("name")) or server_id or "连接器",
                    "matched": False,
                    "source": "manual",
                }
            )
            continue
        rows.append(
            {
                "serverId": server_id,
                "name": _s(spec.get("name")) or entry["name"],
                "matched": True,
                "catalogId": entry["id"],
                "category": entry["category"],
                "mode": entry["mode"],
                "auth": entry["auth"],
                "verified": entry["verified"],
                "suggestedAllowlist": list(entry["suggestedAllowlist"]),
                "enabled": spec.get("enabled") is not False,
                "status": _s(spec.get("status")) or "unknown",
            }
        )
    return {"rows": rows, "catalogTotal": len(CONNECTORS), "manual": unmatched}


def _bare_url(template: str) -> str:
    """目录里的 URL 模板去掉查询串与占位符尾巴，用于前缀反查"""
    head = _s(template).split("?", 1)[0]
    cut = head.find("{")
    return head[:cut] if cut > 0 else head


def _args_overlap(actual: Any, template: tuple[str, ...]) -> bool:
    """命令行参数的身份判定：只看「包名」那一段。

    `npx` / `uvx` 是最常见的撞车（十几条连接器都从同一个命令起），只有包名能说明
    「这就是目录里那一条」。模板里带占位符的参数（`{root}`）不参与比对。
    """
    identity = ""
    for item in template:
        token = _s(item)
        if not token or token.startswith("-") or _placeholder_keys(token):
            continue
        identity = token
        break
    if not identity:
        return False
    items = [_s(a) for a in (actual or [])]
    return any(identity in item for item in items if item)


# ── 按专家的工具白名单（allowlist） ─────────────────────────────


def tool_names_from(raw: Any) -> list[str]:
    """从 `client.list_tools()` 的返回里提工具名（SDK 对象、dict、裸字符串三种形状都认）。

    白名单要按真实工具名展开成 `enable_funcs`，运行期三处（`mcp_runtime` 注册前、
    `mcp_session_runtime` 探活时、车道的白名单预览）都用这一个口径，避免
    「一处认 dict、另一处只认对象」。裸字符串是最常见的形状：界面缓存的名字数组、
    用户手写的清单都长这样，不认就等于把清单读成空的（少放行，看起来像坏了）。
    """
    names: list[str] = []
    items: Any = raw
    if isinstance(items, (str, bytes)) or not isinstance(items, (list, tuple)):
        # 顶层不是裸清单：SDK 的 `list_tools()` 返回的是结果对象，工具挂在 `.tools` 上；
        # 给一个字符串就当清单会逐字符拆成「工具名」，比返回空的更坏
        nested = None if isinstance(items, (str, bytes)) else getattr(items, "tools", None)
        if nested is None and isinstance(items, dict):
            nested = items.get("tools")
        items = nested if isinstance(nested, (list, tuple)) else []
    for tool in items:
        if isinstance(tool, str):
            name = _s(tool)
        elif isinstance(tool, dict):
            name = _s(tool.get("name"))
        else:
            name = _s(getattr(tool, "name", ""))
        if name and name not in names:
            names.append(name)
    return names


def normalize_allowlist(raw: Any) -> tuple[list[str] | None, str]:
    """白名单规范化：返回（模式列表或 None，原因）。

    三态必须分清楚，否则「没配」会被读成「全关」或「全开」，两种都是事故：

    - `None` / 缺字段 / 空串 / 格式非法 ⇒ 返回 `None`，表示**不限制**（存量口径不变；
      格式非法时带上原因，不静默）；
    - `[]` ⇒ 用户的显式决定：**一条都不放行**；
    - `["*"]` ⇒ 显式放行全部（内部折算成 `None`）；
    - 其余按工具名或 `上游/*` 存下来，去重去空。
    """
    if raw is None:
        return None, ""
    if isinstance(raw, str):
        # 输入框被清空（空串）＝没配，不是「一条都不放行」：全关必须是显式的空数组，
        # 否则界面上随手删掉一个字符就能把整个连接器关掉。
        if not raw.strip():
            return None, ""
        raw = [part for part in raw.replace("，", ",").split(",")]
    if not isinstance(raw, list):
        return None, "白名单格式非法（既不是数组也不是逗号串），本轮按不限制处理"
    out: list[str] = []
    for item in raw:
        name = _s(item)
        if not name:
            continue
        if name == "*":
            return None, ""
        if name not in out:
            out.append(name)
    return out, ""


def tool_allowed(patterns: list[str] | None, tool_name: str) -> bool:
    """工具是否落在白名单里；`patterns=None`＝不限制，`[]`＝全不放行"""
    name = _s(tool_name)
    if not name:
        return False
    if patterns is None:
        return True
    for pattern in patterns:
        if pattern.endswith("/*"):
            prefix = pattern[:-2]
            if name.startswith(prefix + "__") or name.startswith(prefix + "/"):
                return True
            continue
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
            continue
        if name == pattern:
            return True
    return False


def allowlist_for(spec: dict | None, expert_ref: str = "") -> tuple[list[str] | None, str]:
    """取这条连接器对**当前专家**生效的白名单（`None`＝不限制）。

    优先级：`expertToolAllowlists[专家]` → `allowedTools` → 不限制。
    同一连接器给两个专家不同白名单靠的就是第一条；选中了专家但该专家没有单独配置时
    落到全局那条，没配置才是不限制。
    """
    block = spec if isinstance(spec, dict) else {}
    per_expert = block.get("expertToolAllowlists")
    ref = _s(expert_ref).lower()
    if isinstance(per_expert, dict) and ref:
        for key, value in per_expert.items():
            if _s(key).lower() == ref:
                return normalize_allowlist(value)
    return normalize_allowlist(block.get("allowedTools"))


def describe_allowlist(patterns: list[str] | None, total: int, kept: int) -> str:
    """白名单生效情况的中文说明（进过程事件，让用户知道少掉的工具是被谁挡的）"""
    if patterns is None:
        return f"已接入 {kept} 个工具（未设白名单）"
    if not patterns:
        return (
            f"白名单是空的：{total} 个工具一条都没放行，"
            "本轮这个连接器不提供任何工具（要放开请改白名单）"
        )
    listed = "、".join(patterns[:8]) + ("…" if len(patterns) > 8 else "")
    head = f"按白名单放行 {kept}/{total} 个工具（规则：{listed}）"
    if kept >= total:
        # 全命中时不能说「其余看不见」——根本没有其余，硬说一句假话比少说一句更糟
        return head
    return f"{head}；其余工具本轮模型看不见，不是坏了"
