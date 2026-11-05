#!/usr/bin/env python3
"""连接器目录自测（D-39：MCP 连接器生态与远程接入，引擎侧注册表）

锁住三件事：

1. **目录本身站得住**——每条记录的分类/模式/传输/鉴权都在枚举里，字段不重复、
   id 不重复，占位符必须有对应的输入字段（写错一个花括号就是「填了也没用」的哑弹），
   端点与包名一律带 `verified` 口径（`doc` 或 `unverified`，不许出现第三种假装核对过的写法）；
2. **表单到 spec 的链路**——`form_fields` / `missing_required` / `build_spec`：
   用户填的中文标签字段怎么变成运行期 spec，缺必填要抛得清楚，令牌注入走哪一步，
   没被填掉的占位符不能留在命令行或 URL 里；
3. **白名单与对账的纯函数口径**——三态（不限制 / 空 / 具体规则）、`前缀*` 与网关
   `上游/*` 的匹配、按专家优先取哪一份，以及 `reconcile` 认出「这就是目录里那一条」。
4. **与 Rust 下发形状同源**——`for_runtime()` 那十个键名两侧拼写一致、鉴权缺失时
   对侧整条不下发而本侧另有停用/过期闸，新键只按 camelCase 出现；这一节只读源码
   比拼写，不重跑对方的测试。

离线运行：不起进程、不联网。目录只是模板，**任何连接器能否真的连上、OAuth 能否真的
拿到令牌都不在本自测的断言范围内**（真机未验收）。

用法: python tests/integration/engine_mcp_catalog_selftest.py
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

try:
    import mcp_connector_catalog as mcc
except Exception as exc:  # noqa: BLE001
    print(f"SKIP: 连接器目录不可用（{exc}）")
    sys.exit(0)

FAILURES = []


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def _raises(fn, *args, code: str = "", **kwargs) -> str:
    """期望抛错：返回错误文本（空串表示没抛）；给了 code 就顺手校验错误码"""
    try:
        fn(*args, **kwargs)
    except Exception as exc:  # noqa: BLE001
        text = str(exc)
        if code and code not in text:
            return f"__WRONG_CODE__{text}"
        return text
    return ""


# ── 1. 目录数据的自洽 ──────────────────────────────────────────

def data_checks() -> None:
    print("--- 1. 目录数据自洽（不需要联网也能保证它不把用户带沟里） ---")
    all_entries = mcc.entries()
    check("目录条数达到台账要求的 ≥8 个开箱连接器", len(all_entries) >= 8, str(len(all_entries)))
    check("目录不少于四类",
          len({e["category"] for e in all_entries}) >= 4,
          str(sorted({e["category"] for e in all_entries})))
    for want in ("办公", "知识", "协作"):
        check(f"分类「{want}」有连接器可用（台账点名要覆盖的三类）",
              bool(mcc.entries(want)), f"{want} 当前 {len(mcc.entries(want))} 条")
    check("未知分类返回空而不是假装全给", mcc.entries("外星") == [])
    ids = [e["id"] for e in all_entries]
    check("连接器 id 不重复", len(ids) == len(set(ids)), str(ids))
    names = [e["name"] for e in all_entries]
    check("连接器中文名不重复（界面按名字摆卡片）", len(names) == len(set(names)), str(names))
    check("目录按「分类, id」稳定排序（同一份清单两次渲染不跳位）",
          [ (e["category"], e["id"]) for e in all_entries ]
          == sorted((e["category"], e["id"]) for e in all_entries))

    bad_enum = [
        e["id"] for e in all_entries
        if e["category"] not in mcc.CATEGORIES
        or e["mode"] not in mcc.MODES
        or e["transport"] not in mcc.TRANSPORTS
        or e["auth"] not in mcc.AUTH_TYPES
    ]
    check("分类/模式/传输/鉴权全部落在枚举里", not bad_enum, str(bad_enum))
    bad_verified = [e["id"] for e in all_entries if e["verified"] not in ("doc", "unverified")]
    check("每条记录的核验口径只有 doc / unverified 两种", not bad_verified, str(bad_verified))
    check("unverified 是多数口径时也要有 doc 打底（不假装全核对过，也不全没核对过）",
          any(e["verified"] == "doc" for e in all_entries)
          and any(e["verified"] == "unverified" for e in all_entries),
          str([(e["id"], e["verified"]) for e in all_entries]))
    missing_docs = [e["id"] for e in all_entries if not str(e["docs"]).startswith("http")]
    check("每条都给了可核对的文档链接", not missing_docs, str(missing_docs))
    short_summary = [e["id"] for e in all_entries if len(str(e["summary"]).strip()) < 8]
    check("每条都有一句话说清它是干什么的", not short_summary, str(short_summary))

    shape = [
        e["id"] for e in all_entries
        if (e["transport"] == "stdio") != bool(str(e["command"]).strip())
        or bool(str(e["url"]).strip()) == (e["transport"] == "stdio")
    ]
    check("stdio 只给命令、远程只给地址（两者不混写）", not shape, str(shape))
    stdio_headers = [e["id"] for e in all_entries if e["transport"] == "stdio" and e["headers"]]
    check("stdio 记录不带 HTTP 头（起了进程就没有 header 这回事）", not stdio_headers, str(stdio_headers))
    remote_auth = [
        e["id"] for e in all_entries
        if e["transport"] != "stdio" and e["auth"] == "none"
    ]
    check("远程记录都写明鉴权方式（没有「免鉴权的远程端点」这种侥幸）", not remote_auth, str(remote_auth))

    field_issues = [e["id"] for e in all_entries if len(e["fields"]) > mcc.MAX_FIELDS_PER_CONNECTOR]
    check("字段数不超过上限", not field_issues, str(field_issues))
    dup_keys = [
        e["id"] for e in all_entries
        if len({f["key"] for f in e["fields"]}) != len(e["fields"])
    ]
    check("同一条记录里字段 key 不重复", not dup_keys, str(dup_keys))
    no_label = [
        f"{e['id']}/{f['key']}" for e in all_entries for f in e["fields"]
        if not str(f["label"]).strip()
    ]
    check("每个字段都有中文标签（这就是「免手写 JSON」的正面）", not no_label, str(no_label))
    cn_label = [
        f"{e['id']}/{f['key']}" for e in all_entries for f in e["fields"]
        if not any("一" <= ch <= "鿿" for ch in str(f["label"]))
    ]
    check("字段标签是中文（不给用户看 raw key）", not cn_label, str(cn_label))

    def used_tokens(entry: dict) -> set:
        """这条记录里所有会被拼进命令/地址/头的占位符"""
        used = set(mcc._placeholder_keys(entry["command"])) | set(mcc._placeholder_keys(entry["url"]))
        for arg in entry["args"]:
            used |= set(mcc._placeholder_keys(arg))
        for pieces in (entry.get("optionalArgs") or {}).values():
            for piece in pieces:
                used |= set(mcc._placeholder_keys(piece))
        for value in entry["headers"].values():
            used |= set(mcc._placeholder_keys(str(value)))
        return used

    orphan = []
    for e in all_entries:
        declared = {f["key"] for f in e["fields"]} | {"token"}
        dangling = sorted(used_tokens(e) - declared)
        if dangling:
            orphan.append(f"{e['id']}: {dangling}")
    check("命令/参数/URL/头里的占位符都有对应字段", not orphan, str(orphan))
    unused = []
    for e in all_entries:
        used = used_tokens(e)
        if e["auth"] in ("env_token", "oauth_pkce", "bearer"):
            # 令牌类字段不进命令行/URL：它们走 env / Authorization 头，"没被用到"是正常的
            continue
        dead = sorted(f["key"] for f in e["fields"] if f["key"] not in used)
        if dead:
            unused.append(f"{e['id']}: {dead}")
    check("非令牌字段都被命令或地址用到（没有填了白填的字段）", not unused, str(unused))

    sec_ok = all(
        any(f["secret"] for f in e["fields"])
        for e in all_entries
        if e["auth"] in ("url_key", "env_token", "bearer")
    )
    check("要填密钥的连接器把对应字段标成 secret（界面才会打码存储）", sec_ok)
    oauth_no_secret = all(
        not any(f["secret"] for f in e["fields"])
        for e in all_entries
        if e["auth"] == "oauth_pkce" and e["id"] != "github"
    )
    check("一键授权的连接器不要求用户贴令牌（PKCE 由桌面端做）", oauth_no_secret)
    check("三种接入模式都有样本（stdio / remote / gateway）",
          {e["mode"] for e in all_entries} >= {"stdio", "remote", "gateway"},
          str(sorted({e["mode"] for e in all_entries})))
    gateway = mcc.get("enterprise-gateway")
    check("网关记录写明「一个端点聚合多个上游」与工具名前缀口径",
          gateway is not None and "上游" in gateway["gatewayNote"] and "__" in gateway["gatewayNote"],
          str((gateway or {}).get("gatewayNote")))
    check("scope 由服务端下发的记录标了 scopesFromServer（不编造 scope 清单）",
          any(e["scopesFromServer"] for e in all_entries))


# ── 2. 表单 → spec ─────────────────────────────────────────────

def spec_checks() -> None:
    print("--- 2. 表单字段到运行期 spec（用户不写 JSON） ---")
    check("目录里没有的 id 直接拒（不是给个空 spec）",
          _raises(mcc.build_spec, "不存在的连接器", {}, code="MCP_CONNECTOR_UNKNOWN") != "")
    check("get() 认大小写（界面回传的是小写 id）",
          mcc.get("Filesystem") is not None and mcc.get(" filesystem ") is not None)
    check("未知 id 的表单字段是空列表而不是抛错", mcc.form_fields("nope") == [])

    fs_fields = mcc.form_fields("filesystem")
    check("本地文件只有一个必填字段（目录路径）",
          [f["key"] for f in fs_fields] == ["root"] and fs_fields[0]["required"] is True,
          str(fs_fields))
    check("密钥字段标了 secret",
          any(f["key"] == "token" and f["secret"] for f in mcc.form_fields("enterprise-gateway")))
    check("每个字段都带 required/secret 两个布尔（界面渲染不靠猜）",
          all(isinstance(f["required"], bool) and isinstance(f["secret"], bool)
              for e in mcc.entries() for f in e["fields"]))

    check("缺必填时报的是中文标签而不是内部 key",
          "目录" in _raises(mcc.build_spec, "filesystem", {}, code="MCP_CONNECTOR_MISSING_FIELD"))
    check("missing_required 与 build_spec 说的是同一件事",
          mcc.missing_required("filesystem", {}) == [f["label"] for f in fs_fields]
          and mcc.missing_required("filesystem", {"root": "D:/x"}) == [])
    bot_label = [f["label"] for f in mcc.form_fields("slack") if f["key"] == "botToken"][0]
    check("空串等同没填（不能拿一个空格糊弄过去）",
          mcc.missing_required("slack", {"botToken": "  ", "teamId": "T1"}) == [bot_label],
          str(mcc.missing_required("slack", {"botToken": "  ", "teamId": "T1"})))
    check("令牌由桌面端授权回来时也算「必填已满足」（用户不必再手输一遍）",
          mcc.build_spec("enterprise-gateway", {"host": "mcp.corp.example"}, token="t-1")[0]["url"]
          == "https://mcp.corp.example/mcp")
    check("未知连接器的缺失提示指向目录（不是崩在 KeyError）",
          "目录" in " ".join(mcc.missing_required("nope", {})))

    spec, notes = mcc.build_spec("filesystem", {"root": "D:/工作/合同"})
    check("stdio spec 把填的值拼进参数",
          spec["args"][-1] == "D:/工作/合同" and spec["command"] == "npx"
          and spec["transport"] == "stdio", str(spec))
    check("spec 带 catalogId（对账与界面回链都靠它）",
          spec["catalogId"] == "filesystem" and spec["mode"] == "stdio")
    check("没填的可选字段不留花括号残骸（命令行里的 {x} 是哑弹）",
          not any("{" in a for a in spec["args"]), str(spec["args"]))
    check("目录类连接器不需要令牌，notes 里没有授权提示",
          not any("令牌" in n for n in notes), str(notes))

    spec2, _ = mcc.build_spec("filesystem", {"root": "D:/x"}, server_id="srv-9")
    check("给了 serverId 就用它（落库主键不能被目录默认值覆盖）",
          spec2["serverId"] == "srv-9", str(spec2["serverId"]))
    check("没给时用 catalog-<id> 占位（至少能把工具归属到某个连接器）",
          spec["serverId"] == "catalog-filesystem")

    amap, amap_notes = mcc.build_spec("amap-maps", {"key": "abc123"})
    check("URL 型 key 直接拼进地址", amap["url"] == "https://mcp.amap.com/sse?key=abc123", str(amap["url"]))
    check("URL 型 key 不会被同时塞进 env/headers（一份凭据只走一条路）",
          not amap.get("env") and not amap.get("headers"), str(amap))
    check("未核验端点在 notes 里提醒接入时复核",
          any("以服务商当前文档为准" in n for n in amap_notes), str(amap_notes))
    check("doc 级记录不挂复核提醒",
          not any("以服务商当前文档为准" in n for n in mcc.build_spec("memory", {})[1]),
          str(mcc.build_spec("memory", {})[1]))

    slack, slack_notes = mcc.build_spec("slack", {"botToken": "xoxb-1", "teamId": "T1"})
    check("env_token 连接器把令牌落到子进程 env",
          slack["env"] == {"SLACK_BOT_TOKEN": "xoxb-1", "SLACK_TEAM_ID": "T1"}, str(slack.get("env")))
    check("令牌不进命令行参数（避免出现在进程列表与日志里）",
          not any("xoxb" in a for a in slack["args"]), str(slack["args"]))
    bare_spec, bare_notes = mcc.build_spec("context7", {})
    check("没配任何令牌的 env_token 连接器如实说很可能连不上",
          any("没有注入任何令牌" in n for n in bare_notes), str(bare_notes))
    check("这种连接器的 env 只带真正给了的那一项",
          mcc.build_spec("context7", {"apiKey": "ctx-key"})[0].get("env") == {"CONTEXT7_API_KEY": "ctx-key"},
          str(mcc.build_spec("context7", {"apiKey": "ctx-key"})[0].get("env")))
    check("完全没给令牌时干脆不写 env 键（省得运行期以为配过了）",
          "env" not in bare_spec, str(bare_spec))

    pw_off, _ = mcc.build_spec("playwright", {})
    pw_on, _ = mcc.build_spec("playwright", {"profile": "D:/tt/browser-profile"})
    check("可选开关：没填就一个参数都不多拼",
          pw_off["args"] == ["-y", "@playwright/mcp@latest"], str(pw_off["args"]))
    check("可选开关：填了才带上对应 flag（用户看不到 flag 也能用）",
          pw_on["args"][-2:] == ["--user-data-dir", "D:/tt/browser-profile"], str(pw_on["args"]))

    gh_spec, gh_notes = mcc.build_spec("github", {}, token="gho_secret")
    check("OAuth 连接器拿到令牌后落 Authorization 头",
          gh_spec.get("headers", {}).get("Authorization") == "Bearer gho_secret", str(gh_spec))
    check("注入令牌在 notes 里说明刷新与撤销口径",
          any("刷新" in n and "撤销" in n for n in gh_notes), str(gh_notes))
    check("一键授权还没完成时说清下一步（不是「连接失败」）",
          any("一键授权" in n for n in mcc.build_spec("github", {})[1]),
          str(mcc.build_spec("github", {})[1]))
    check("OAuth 连接器允许「先不落令牌」（等 PKCE 回来再连）",
          "Authorization" not in mcc.build_spec("notion", {})[0].get("headers", {}))

    gw, gw_notes = mcc.build_spec("enterprise-gateway", {"host": "mcp.corp.example"}, token="t-1")
    check("网关地址按填的 host 生成", gw["url"] == "https://mcp.corp.example/mcp", str(gw["url"]))
    check("网关的 Bearer 头来自 token 参数",
          gw["headers"]["Authorization"] == "Bearer t-1", str(gw["headers"]))
    check("网关 notes 摊出上游前缀口径（白名单要写成 上游/*）",
          any("上游" in n for n in gw_notes), str(gw_notes))
    check("必填令牌缺失的 bearer 连接器直接拒（不给一个连不上的 spec）",
          _raises(mcc.build_spec, "enterprise-gateway", {"host": "h"}, code="MCP_CONNECTOR_MISSING_FIELD") != "")

    expires_spec, _ = mcc.build_spec("github", {}, token="t", expires_at_ms=1_800_000_000_000)
    check("令牌有效期落进 spec（运行期据此拦截过期令牌）",
          expires_spec["authExpiresAtMs"] == 1_800_000_000_000, str(expires_spec))
    check("没给有效期时不伪造 authExpiresAtMs",
          "authExpiresAtMs" not in mcc.build_spec("github", {}, token="t")[0])

    long_root = "D:/" + "很" * 600
    clipped, clip_notes = mcc.build_spec("filesystem", {"root": long_root})
    check("超长字段截断并说明（不是静默吞掉）",
          len(clipped["args"][-1]) <= mcc.MAX_FIELD_CHARS and any("已截断" in n for n in clip_notes),
          str([len(n) for n in clip_notes]))
    _ignored, ig_notes = mcc.build_spec("filesystem", {"root": "D:/x", "webhookUrl": "http://x"})
    check("表单外的字段被忽略且点名",
          any("不在" in n and "webhookUrl" in n for n in ig_notes), str(ig_notes))
    check("values 传 None / 字符串也不崩（界面可能还没渲染完）",
          mcc.build_spec("memory", None)[0]["transport"] == "stdio"
          and isinstance(mcc.build_spec("memory", "不是字典")[0], dict))


# ── 3. 对账：Rust 清单 ↔ 目录 ──────────────────────────────────

def reconcile_checks() -> None:
    print("--- 3. 统一注册表：把已有连接器与目录对上号 ---")
    empty = mcc.reconcile([])
    check("空清单对账不报错，并报出目录总数",
          empty["rows"] == [] and empty["catalogTotal"] == len(mcc.entries()) and empty["manual"] == 0,
          str(empty))
    check("非字典的行被跳过（脏数据不算手工配置）",
          mcc.reconcile([None, "字符串"])["rows"] == [])

    by_id = mcc.reconcile([{"serverId": "s1", "catalogId": "filesystem", "name": "本地文件"}])
    check("带 catalogId 的行直接命中目录",
          by_id["rows"][0]["matched"] is True and by_id["rows"][0]["catalogId"] == "filesystem",
          str(by_id["rows"]))
    check("命中行带上核验口径（doc / unverified 一路传到界面）",
          by_id["rows"][0]["verified"] == "doc", str(by_id["rows"][0]))

    by_cmd = mcc.reconcile([
        {"serverId": "s2", "name": "手装的内存图谱", "transport": "stdio",
         "command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]}
    ])
    check("没写 catalogId 的按命令 + 包名反查目录（同一条官方包不当成两个连接器）",
          by_cmd["rows"][0]["matched"] is True and by_cmd["rows"][0]["catalogId"] == "memory",
          str(by_cmd["rows"]))
    by_url = mcc.reconcile([
        {"serverId": "s3", "name": "远程搜索", "transport": "streamable_http",
         "url": "https://mcp.notion.com/mcp"}
    ])
    check("远程端点按地址前缀反查目录",
          by_url["rows"][0]["catalogId"] == "notion", str(by_url["rows"]))
    manual = mcc.reconcile([
        {"serverId": "s4", "name": "公司自研", "transport": "stdio",
         "command": "my-mcp", "args": ["--serve"]}
    ])
    check("目录里没有的行如实标 manual（手工配置不是坏东西）",
          manual["rows"][0]["matched"] is False and manual["rows"][0]["source"] == "manual"
          and manual["manual"] == 1, str(manual))
    check("反查不会因为「第一个非选项参数恰好同名」误认亲（只看包名身份）",
          mcc.reconcile([{"serverId": "s5", "command": "docker", "args": ["-y", "-z"]}])["manual"] == 1,
          str(mcc.reconcile([{"serverId": "s5", "command": "docker", "args": ["-y", "-z"]}])))
    row = mcc.reconcile([{"serverId": "s6", "catalogId": "slack", "enabled": False,
                          "status": "connected"}])["rows"][0]
    check("对账行带出停用状态与上次连接结果（界面才有依据）",
          row["enabled"] is False and row["status"] == "connected", str(row))
    check("没标 status 的行说 unknown（不假装连过）",
          mcc.reconcile([{"serverId": "s7", "catalogId": "fetch"}])["rows"][0]["status"] == "unknown")
    check("命中行给出建议白名单（用户可以直接采用）",
          mcc.reconcile([{"serverId": "s8", "catalogId": "git"}])["rows"][0]["suggestedAllowlist"]
          == mcc.get("git")["suggestedAllowlist"])


# ── 4. 工具白名单（按专家放行） ────────────────────────────────

def allowlist_checks() -> None:
    print("--- 4. 工具白名单三态与匹配口径 ---")
    none, why = mcc.normalize_allowlist(None)
    check("没配白名单＝不限制（存量口径不变）", none is None and why == "")
    check("白名单整份是空串（界面清过又保存）＝不限制，不是全关",
          mcc.normalize_allowlist("")[0] is None)
    check("空列表＝一条都不放行（与「没配」必须分清）", mcc.normalize_allowlist([])[0] == [])
    check("只有 * 时折算成不限制（写 * 不等于全关）", mcc.normalize_allowlist(["*"])[0] is None)
    check("列表里混进 * 也算不限制", mcc.normalize_allowlist(["read_file", "*"])[0] is None)
    deduped, _ = mcc.normalize_allowlist(["read_file", " read_file ", "", "write_file"])
    check("去空去重并抹掉首尾空格", deduped == ["read_file", "write_file"], str(deduped))
    comma, _ = mcc.normalize_allowlist("read_file，write_file")
    check("逗号串（含中文逗号）也能读", comma == ["read_file", "write_file"], str(comma))
    bad, bad_why = mcc.normalize_allowlist({"read_file": True})
    check("格式非法时按不限制处理并说明原因（不静默改成全关）",
          bad is None and "格式非法" in bad_why, str((bad, bad_why)))

    check("None 放行任何工具", mcc.tool_allowed(None, "anything"))
    check("空列表一个都不放行", not mcc.tool_allowed([], "anything"))
    check("精确名匹配", mcc.tool_allowed(["get_issue"], "get_issue")
          and not mcc.tool_allowed(["get_issue"], "get_issues"))
    check("前缀 * 匹配", mcc.tool_allowed(["browser_"], "browser_click") is False
          and mcc.tool_allowed(["browser_*"], "browser_click") is True)
    check("网关 上游/* 认 上游__工具", mcc.tool_allowed(["crm/*"], "crm__get_customer") is True)
    check("网关 上游/* 也认 上游/工具（两种命名都见过）",
          mcc.tool_allowed(["crm/*"], "crm/get_customer") is True)
    check("上游/* 不会误放别的上游", not mcc.tool_allowed(["crm/*"], "erp__get_customer"))
    check("上游/* 不会放过没有前缀的裸工具名", not mcc.tool_allowed(["crm/*"], "get_customer"))
    check("空工具名不放行（脏数据不冒充命中）", not mcc.tool_allowed(["a*"], ""))
    check("多条规则是并集", mcc.tool_allowed(["read_file", "search_*"], "search_files") is True)

    check("没配任何东西的 spec＝不限制", mcc.allowlist_for({"serverId": "s"})[0] is None)
    check("连接器级白名单生效",
          mcc.allowlist_for({"allowedTools": ["read_file"]})[0] == ["read_file"])
    spec = {"allowedTools": ["read_file", "write_file"],
            "expertToolAllowlists": {"法务专家": ["read_file"]}}
    check("按专家的配置优先于连接器级（同一连接器给不同专家不同权限）",
          mcc.allowlist_for(spec, "法务专家")[0] == ["read_file"], str(mcc.allowlist_for(spec, "法务专家")))
    check("没单独配置的专家落回连接器级", mcc.allowlist_for(spec, "财务专家")[0] == ["read_file", "write_file"])
    check("没选专家时用连接器级（不给「没专家」放行全部）",
          mcc.allowlist_for(spec, "")[0] == ["read_file", "write_file"])
    check("专家名匹配忽略大小写与空格",
          mcc.allowlist_for({"expertToolAllowlists": {" GitHub ": ["get_issue"]}}, "github")[0]
          == ["get_issue"])
    check("专家级写空列表＝对该专家全关（但别的专家照常用）",
          mcc.allowlist_for({"allowedTools": ["read_file"],
                             "expertToolAllowlists": {"实习生": []}}, "实习生")[0] == []
          and mcc.allowlist_for({"allowedTools": ["read_file"],
                                 "expertToolAllowlists": {"实习生": []}}, "正式")[0] == ["read_file"])
    check("expertToolAllowlists 不是字典时落回连接器级（不崩）",
          mcc.allowlist_for({"allowedTools": ["a"], "expertToolAllowlists": "坏了"}, "x")[0] == ["a"])
    check("传进来的整条 runtime 项不是字典时按不限制处理",
          mcc.allowlist_for(None)[0] is None and mcc.allowlist_for("字符串")[0] is None)

    check("未设白名单的中文说明不谎报拦截",
          mcc.describe_allowlist(None, 3, 3) == "已接入 3 个工具（未设白名单）",
          mcc.describe_allowlist(None, 3, 3))
    check("空白名单说清「一条都没放行」", "一条都没放行" in mcc.describe_allowlist([], 5, 0))
    check("放行比例说清是白名单挡的，不是坏了",
          "1/3" in mcc.describe_allowlist(["read_file"], 3, 1) and "不是坏了" in
          mcc.describe_allowlist(["read_file"], 3, 1), mcc.describe_allowlist(["read_file"], 3, 1))
    check("规则太长只列前 8 条（过程事件不是配置转储）",
          mcc.describe_allowlist([f"t{i}" for i in range(20)], 20, 20).count("…") == 1,
          mcc.describe_allowlist([f"t{i}" for i in range(20)], 20, 20))


# ── 5. 工具清单提取（两处共用一个口径）────────────────────────

def tool_names_checks() -> None:
    print("--- 5. list_tools 结果的取名口径 ---")
    class _T:
        def __init__(self, name):
            self.name = name

    check("SDK 对象形状能取名", mcc.tool_names_from([_T("echo"), _T("now_word")]) == ["echo", "now_word"])
    check("dict 形状也能取名", mcc.tool_names_from([{"name": "echo"}, {"name": "fetch"}]) == ["echo", "fetch"])
    check("混合形状一起认", mcc.tool_names_from([_T("a"), {"name": "b"}]) == ["a", "b"])
    check("重名只留一个（enable_funcs 里有重复项会误导人）",
          mcc.tool_names_from([_T("a"), _T("a")]) == ["a"])
    check("空名与 None 项被丢掉", mcc.tool_names_from([_T(""), _T("  "), {"name": None}, _T("ok")]) == ["ok"])
    check("没有工具就是空列表（不是 None，调用方要能分清「没有」和「没拿到」）",
          mcc.tool_names_from([]) == [] and mcc.tool_names_from(None) == [])
    check("属性缺失的对象不炸（第三方服务器什么形状都有）",
          mcc.tool_names_from([object(), _T("ok")]) == ["ok"])
    # 车道的白名单预览与界面缓存给的是名字数组，不认就等于把清单读成空的（少放行）
    check("裸字符串清单也认（界面缓存的名字数组是最常见的形状）",
          mcc.tool_names_from(["echo", "now_word", " echo "]) == ["echo", "now_word"],
          str(mcc.tool_names_from(["echo", "now_word", " echo "])))
    check("SDK 的结果对象也认（工具挂在 .tools 上，不是裸清单）",
          mcc.tool_names_from(type("Resp", (), {"tools": [_T("a"), {"name": "b"}]})()) == ["a", "b"],
          str(mcc.tool_names_from(type("Resp", (), {"tools": [_T("a")]})())))
    check("dict 包一层 tools 也认", mcc.tool_names_from({"tools": [{"name": "x"}]}) == ["x"])
    check("顶层给一个字符串时不逐字符拆成「工具名」",
          mcc.tool_names_from("echo") == [], str(mcc.tool_names_from("echo")))
    check("顶层给没有 tools 的对象时给空（不当成清单硬 iterate）",
          mcc.tool_names_from(object()) == [] and mcc.tool_names_from({"name": "x"}) == [])


def seam_checks() -> None:
    print("--- 6. 与 Rust 下发形状同源（跨语言钉键名，不重跑对方测试） ---")

    rust_mcp = PROJECT_ROOT / "src-tauri" / "src" / "mcp" / "mod.rs"
    py_runtime = PROJECT_ROOT / "engine" / "mcp_runtime.py"
    if not (rust_mcp.exists() and py_runtime.exists()):
        check("对侧源码可读（读不到就只跑本侧口径）", False, f"{rust_mcp} / {py_runtime}")
        return
    rs = rust_mcp.read_text(encoding="utf-8", errors="replace")
    ps = py_runtime.read_text(encoding="utf-8", errors="replace")

    # Rust 的 `for_runtime()` 是把配置交给引擎的唯一出口：这一串键名就是接口本身。
    # 它改名而 Python 没跟上，表现是「连接器全都不接入」这种最难查的坏法，所以钉住拼写。
    tail = rs.split("pub fn for_runtime", 1)[1]
    # 截到下一个 `pub fn` 之前：这一段才是下发形状本体，后面是探测请求体之类的别的接口
    body = tail.split(chr(10) + "pub fn ", 1)[0]
    for key in ("serverId", "name", "transport", "command", "args", "url",
                "env", "headers", "timeoutSecs", "tools"):
        needle = '"' + key + '"'
        check(f"键名 `{key}` 两侧同源（Rust 下发、引擎读同一拼写）",
              needle in body and needle in ps,
              f"rust={needle in body} py={needle in ps}")
    check("Rust 在鉴权缺失/过期时整条不下发（continue），不是下发一个空令牌",
          "runtime_authorized" in body and "continue" in body, body[:160])
    check("引擎侧另有一道过期与停用闸（不假设对侧永远正确）",
          "authExpiresAtMs" in ps and '"enabled"' in ps)
    # 新键一律 camelCase：任何一侧写成 snake_case 都是静默读不到（=白名单不生效）
    for field in ("allowedTools", "expertToolAllowlists", "authExpiresAtMs", "catalogId"):
        check(f"新键只按 camelCase 出现（没有 `{_snake(field)}` 第二种写法）",
              _snake(field) not in ps and _snake(field) not in rs, _snake(field))
    build = _build_spec_source()
    check("目录 id 只进 `catalogId` 这一个键（不给第二份标识）",
          '"catalogId": entry["id"]' in build and "catalog_id" not in build, build[:160])


def _snake(camel: str) -> str:
    import re as _re

    return _re.sub(r"(?<!^)(?=[A-Z])", "_", camel).lower()


def _build_spec_source() -> str:
    text = (PROJECT_ROOT / "engine" / "mcp_connector_catalog.py").read_text(
        encoding="utf-8", errors="replace"
    )
    start = text.find("def build_spec")
    end = text.find("\ndef ", start + 10)
    return text[start:end if end > 0 else start + 4000]


def main() -> None:
    data_checks()
    spec_checks()
    reconcile_checks()
    allowlist_checks()
    tool_names_checks()
    seam_checks()
    print("-" * 52)
    if FAILURES:
        print(f"engine mcp catalog selftest: {len(FAILURES)} 项失败")
        for name in FAILURES:
            print(f"  - {name}")
        sys.exit(1)
    print("engine mcp catalog selftest: ALL PASS")


if __name__ == "__main__":
    main()
