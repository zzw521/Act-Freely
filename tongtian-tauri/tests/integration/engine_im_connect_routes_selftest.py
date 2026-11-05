#!/usr/bin/env python3
"""
connect 车道三条 IM 端点的**端点面**自测（缺陷 D-41 的「界面看得见的事实」那一半）

验的是 HTTP 这一层：路径会不会被 server.py 的 `/api/im/{kind}/…` 通配吞掉、
策略读写的拒绝形状（业务拒绝 = 200 + ok=false + 中文，意外 = 500 + 错误码）、
能力面是否真的把「引擎今天会做的动作」原样下发、流量面能不能看见冷却与挡下的条数、
以及这三条端点有没有把凭据或消息内容带出去。

六道闸的决策与记账在 `engine_im_runtime_selftest.py`（假通道、不联网），
各家的协议事实在 `engine_im_<kind>_selftest.py`；本族不重跑那些，只补 HTTP 形状。

分段：
1. 登记与通配：三条路径都在车道清单里、都是字面段、被通配件包围照样命中车道
2. policies GET：作用域链、生效值、出厂默认、shape（枚举/标签/上下界）与文件隔离
3. policies POST：落到哪个作用域、立刻生效、改粗不影响细、drop 只删那一条
4. policies POST 的拒绝：未知键、非法取值、越界数字、坏作用域、空 patch、坏 JSON、无 scope 的 drop
5. 读写炸掉：IM_POLICY_UNREADABLE / IM_POLICY_WRITE_FAILED 各回 500，不把意外包装成 200
6. capabilities：逐实例一行、False 一律带中文原因、顺序稳定、未接入的带原因
7. traffic：逐实例读数、totals 求和、429 冷却与限流条数在 note 里数得出来
8. 不泄露：三条端点的响应里既没有凭据也没有消息文本

数据隔离：TT_DATA_HOME 与 WORK_DIR 指向临时目录；策略文件与附件根都不落 ~/.tongtianwork。
必须用 `./vendor/python/python.exe -X utf8` 跑；缺 fastapi 时整族 SKIP 且 rc=0。

**假端点绿不等于真平台能用**：真实 IM 凭据、真实群里的 @、真实 429 一律未验收。
用法: ./vendor/python/python.exe -X utf8 tests/integration/engine_im_connect_routes_selftest.py
"""

import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_TMP = Path(tempfile.mkdtemp(prefix="tt-im-routes-"))
os.environ["TT_DATA_HOME"] = str(_TMP / "home")
os.environ["WORK_DIR"] = str(_TMP / "work")
sys.path.insert(0, str(PROJECT_ROOT / "engine"))
sys.path.insert(0, str(PROJECT_ROOT / "tests" / "integration"))

ENGINE_MODULES = {path.stem for path in (PROJECT_ROOT / "engine").glob("*.py")}


def import_gate(exc: BaseException, suite: str) -> None:
    """只在**真缺第三方依赖**时优雅 SKIP；我们自己的引擎文件导入断了必须大声红。

    `except Exception` 一把吞过一次（D-39）：引擎侧一次装配错就把整族报成
    「SKIP + rc=0」，几十项自测瞬间变 0 项还全绿——那正是「假绿写进真台账」的形状。
    """
    root = exc
    for _ in range(12):
        nxt = root.__cause__ or root.__context__
        if nxt is None:
            break
        root = nxt
    missing = getattr(root, "name", "") or ""
    if isinstance(root, ModuleNotFoundError) and missing not in ENGINE_MODULES:
        print(f"SKIP: {suite} unavailable（缺第三方依赖 {missing or root}）")
        sys.exit(0)
    print(f"[FAIL] {suite} 起不来：引擎侧导入断了，不是依赖缺失 — {type(root).__name__}: {root}")
    print("[FAIL] 整族未运行（拒绝把上面的断裂报成 SKIP）")
    sys.exit(1)


try:
    import im_channels as imch
    import im_media as imedia
    import im_policy as ipol
    import routes_connect as rc
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "im connect routes selftest")

try:
    # 六条真实通道模块：只 import 就完成注册（`@register`），能力面与流量面才有行可读。
    # 本族不联网、不建连接，拿的是模块上的能力位与实例台账。
    import dingtalk_channel  # noqa: F401
    import feishu_channel  # noqa: F401
    import slack_channel  # noqa: F401
    import wechat_channel  # noqa: F401
    import wecom_channel  # noqa: F401
    import yuanbao_channel  # noqa: F401
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "im connect routes selftest")

try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
except Exception as exc:  # noqa: BLE001
    import_gate(exc, "im connect routes selftest")

FAILURES = []

SECRET_APP_ID = "cli_SECRET_APP_ID_9f3a"
SECRET_APP_SECRET = "SECRET-APP-SECRET-DO-NOT-LEAK"
SECRET_TOKEN = "SECRET-WS-TOKEN-abc123"


def check(name: str, cond: bool, detail: str = "") -> None:
    status = "PASS" if cond else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILURES.append(name)


def section(title: str) -> None:
    print(f"\n--- {title} ---")


def make_client() -> TestClient:
    """把车道路由挂到一个最小应用上，并**故意后挂 server.py 那类通配件**。

    server.py 里先有 `/api/im/{kind}/config`、`/api/im/{kind}/login/start` 这些三段通配，
    车道是后挂的；D-07.4 那次真机演练就是被通配吞掉了字面路径（mock 永远测不出），
    所以这里把通配件摆出来，验字面路径确实命中的是车道而不是通配。
    """
    app = FastAPI()
    app.include_router(rc.router)

    @app.get("/api/im/{kind}/config")
    async def _swallow_get(kind: str):  # pragma: no cover - 命中即测试失败
        return {"swallowed": f"get:{kind}"}

    @app.post("/api/im/{kind}/{action}")
    async def _swallow_post(kind: str, action: str):  # pragma: no cover
        return {"swallowed": f"post:{kind}/{action}"}

    @app.get("/api/{a}/{b}")
    async def _swallow_two(a: str, b: str):  # pragma: no cover
        return {"swallowed": f"two:{a}/{b}"}

    return TestClient(app)


CLIENT = make_client()


def row_of(rows: list[dict], instance_key: str) -> dict:
    return next((r for r in rows if r.get("instanceKey") == instance_key), {})


def clean_runtime(kind: str, channel_id: str) -> imch.ChannelRuntime:
    """取一个实例并把它的账擦干净，好让每条断言数的都是本段自己造出来的数"""
    runtime = imch.runtime_of(kind, channel_id)
    runtime.governor.clear()
    for key in runtime.governor.counters:
        runtime.governor.counters[key] = 0
    runtime.governor.last_reason = ""
    for key in runtime.counters:
        runtime.counters[key] = 0
    runtime.last_skip = None
    runtime.send_note = ""
    runtime.typing_state = ""
    return runtime


# ── 1. 登记与通配 ─────────────────────────────────────────────────


def phase_registration() -> None:
    section("1. 三条路径登记在车道里，且不被通配吞掉")
    im_paths = [p for p in rc.CONNECT_ENDPOINT_PATHS if p.startswith("/api/im/")]
    check("车道清单里有 policies / capabilities / traffic 三条 IM 端点",
          set(im_paths) == {"/api/im/policies", "/api/im/capabilities", "/api/im/traffic"},
          str(im_paths))
    check("三条都是字面段（不含 { } 占位）", not any("{" in p for p in im_paths), str(im_paths))
    check("都是 /api/im/<名> 两段形状（三段会被既有通配吃掉）",
          all(len(p.strip("/").split("/")) == 3 for p in im_paths), str(im_paths))
    for path in ("/api/im/policies", "/api/im/capabilities", "/api/im/traffic"):
        body = CLIENT.get(path).json()
        check(f"GET {path} 命中的是车道而不是通配件",
              "swallowed" not in body and body.get("ok") is True, str(body)[:160])
    written = CLIENT.post("/api/im/policies", json={"scope": "oc-swallow", "patch": {"burst": 2}})
    check("POST /api/im/policies 也没被 /api/im/{kind}/{action} 吞",
          "swallowed" not in written.json(), str(written.json())[:160])
    CLIENT.post("/api/im/policies", json={"scope": "oc-swallow", "drop": True})
    check("通配件本身还活着（不是把整张路由表搞坏才通过的）",
          CLIENT.get("/api/im/dingtalk/config").json().get("swallowed") == "get:dingtalk",
          str(CLIENT.get("/api/im/dingtalk/config").json())[:120])


# ── 2. policies GET ───────────────────────────────────────────────


def phase_policies_get() -> None:
    section("2. 读策略：链、生效值、默认与 shape")
    resp = CLIENT.get("/api/im/policies")
    check("GET 回 200", resp.status_code == 200, str(resp.status_code))
    body = resp.json()
    check("ok 为真", body.get("ok") is True)
    check("没问作用域时链是空的（只剩全局默认）", body.get("chain") == [], str(body.get("chain")))
    check("生效值是一份完整策略（每个出厂键都在）",
          all(k in (body.get("policy") or {}) for k in ipol.POLICY_DEFAULTS), str(body.get("policy")))
    check("出厂默认与引擎常量同源", body.get("defaults") == dict(ipol.POLICY_DEFAULTS))
    shape = body.get("shape") or {}
    check("shape 给出枚举选项（界面不在前端抄第二份）",
          set(shape.get("enums") or {}) >= {"groupActivation", "dmActivation", "groupContext",
                                            "groupMedia", "dmMedia"}, str(shape.get("enums")))
    check("枚举取值与引擎常量逐个一致",
          shape["enums"]["groupActivation"] == list(ipol.ACTIVATIONS)
          and shape["enums"]["groupMedia"] == list(ipol.MEDIA_MODES))
    check("每个枚举项都带中文标签",
          all(labels.get(v) for k, labels in (shape.get("labels") or {}).items()
              for v in (shape.get("enums") or {}).get(k, [])), str(shape.get("labels"))[:200])
    check("数字项带上界下界与默认值",
          set(shape.get("bounds") or {}) == set(ipol.NUMERIC_BOUNDS)
          and all({"min", "max", "label", "default"} <= set(v) for v in shape["bounds"].values()),
          str(shape.get("bounds"))[:200])
    check("上下界的默认值就是出厂默认",
          all(v["default"] == ipol.POLICY_DEFAULTS[k] for k, v in shape["bounds"].items()))
    check("暂存上限随 shape 下发（不在前端写死数字）",
          shape.get("contextLimits", {}).get("maxMessages") == ipol.MAX_CONTEXT_MESSAGES
          and shape["contextLimits"]["maxChars"] == ipol.MAX_CONTEXT_CHARS)
    check("作用域链的方向用中文写清（会话→实例→类型→默认）",
          "会话" in shape.get("scopeNote", "") and "全局默认" in shape.get("scopeNote", ""),
          shape.get("scopeNote", ""))

    q = CLIENT.get("/api/im/policies",
                   params={"kind": "dingtalk", "channelId": "corpA", "conversationId": "oc_1"}).json()
    check("带作用域时链由细到粗：会话 → 实例 → 类型",
          q.get("chain") == ["oc_1", "dingtalk:corpA", "dingtalk"], str(q.get("chain")))
    check("同类型多实例的链按实例分开（两条企业互不覆盖）",
          CLIENT.get("/api/im/policies", params={"channelId": "corpB", "kind": "dingtalk"}).json()["chain"]
          == ["dingtalk:corpB", "dingtalk"])
    check("只给 kind 不给实例时链里不重复出现 kind",
          CLIENT.get("/api/im/policies", params={"channelId": "dingtalk", "kind": "dingtalk"}).json()["chain"]
          == ["dingtalk"])
    scoped = CLIENT.get("/api/im/policies", params={"scope": "oc_explicit"}).json()
    check("显式 scope 不在链里时插到最前（设置页要能预览任意作用域）",
          scoped["chain"][0] == "oc_explicit", str(scoped["chain"]))
    check("响应里不带任何凭据或消息内容",
          "token" not in str(q).lower() and "credentials" not in q, str(q)[:160])


# ── 3. policies POST ──────────────────────────────────────────────


def phase_policies_write() -> None:
    section("3. 写策略：落哪、生效、隔离、删除")
    check("策略文件在临时根里（不写进存量 ~/.tongtianwork）",
          str(ipol.policy_path()).startswith(str(_TMP)), str(ipol.policy_path()))
    before = dict(ipol.load_store().get("defaults") or {})

    resp = CLIENT.post("/api/im/policies", json={"scope": "oc_room7", "patch": {"groupActivation": "always"}})
    body = resp.json()
    check("写一个会话作用域回 200 + ok", resp.status_code == 200 and body.get("ok") is True, str(body)[:160])
    check("回显刚存的那一份", body.get("stored") == {"groupActivation": "always"}, str(body.get("stored")))
    check("作用域清单里数得出这一条", "oc_room7" in (body.get("scopes") or {}), str(body.get("scopes")))
    eff = CLIENT.get("/api/im/policies", params={"conversationId": "oc_room7"}).json()["policy"]
    check("保存即生效：该会话的群内触发变成每条都处理", eff["groupActivation"] == "always", str(eff))
    other = CLIENT.get("/api/im/policies", params={"conversationId": "oc_other"}).json()["policy"]
    check("别的会话不受牵连（仍是出厂默认）",
          other["groupActivation"] == ipol.POLICY_DEFAULTS["groupActivation"], str(other))
    check("同一作用域第二次写是合并（不把第一次设过的那项悄悄抹回默认）",
          CLIENT.post("/api/im/policies", json={"scope": "oc_room7", "patch": {"burst": 2}}).json()["stored"]
          == {"groupActivation": "always", "burst": 2})

    kind_write = CLIENT.post("/api/im/policies", json={"scope": "slack", "patch": {"dmMedia": "off"}}).json()
    check("按通道类型写也收（scope 命中已登记的 kind）",
          kind_write.get("ok") is True and kind_write["stored"] == {"dmMedia": "off"}, str(kind_write)[:160])
    check("类型作用域对它下面的实例生效",
          CLIENT.get("/api/im/policies", params={"channelId": "slack:w1", "kind": "slack"}).json()["policy"]["dmMedia"]
          == "off")
    finer = CLIENT.post("/api/im/policies", json={"scope": "C123", "patch": {"dmMedia": "on"}}).json()
    check("改粗的不影响已经单独配过的细作用域",
          CLIENT.get("/api/im/policies", params={"conversationId": "C123", "kind": "slack"}).json()["policy"]["dmMedia"]
          == "on", str(finer)[:120])

    inst = CLIENT.post("/api/im/policies",
                       json={"scope": "dingtalk:multiA", "patch": {"groupActivation": "always"}}).json()
    check("写实例作用域用的是 instance_key 形状（类型:实例标识）",
          inst.get("ok") is True and inst["stored"] == {"groupActivation": "always"}, str(inst)[:160])
    check("该实例的链读到刚写的那一份",
          CLIENT.get("/api/im/policies", params={"channelId": "multiA", "kind": "dingtalk"}).json()["policy"]
          ["groupActivation"] == "always")
    check("同类型另一个实例没被带上（多实例互不影响）",
          CLIENT.get("/api/im/policies", params={"channelId": "multiB", "kind": "dingtalk"}).json()["policy"]
          ["groupActivation"] == ipol.POLICY_DEFAULTS["groupActivation"])
    check("别的类型上同名实例也没被带上（裸标识跨类型撞车已封死）",
          CLIENT.get("/api/im/policies", params={"channelId": "multiA", "kind": "feishu"}).json()["policy"]
          ["groupActivation"] == ipol.POLICY_DEFAULTS["groupActivation"])
    CLIENT.post("/api/im/policies", json={"scope": "dingtalk:multiA", "drop": True})
    check("drop 掉实例作用域后该实例回到类型/默认（不留隐性放宽）",
          CLIENT.get("/api/im/policies", params={"channelId": "multiA", "kind": "dingtalk"}).json()["policy"]
          ["groupActivation"] == ipol.POLICY_DEFAULTS["groupActivation"])

    dropped = CLIENT.post("/api/im/policies", json={"scope": "C123", "drop": True}).json()
    check("drop 只删那一条", dropped.get("dropped") is True and "C123" not in dropped.get("scopes", {})
          and "oc_room7" in dropped.get("scopes", {}), str(dropped)[:200])
    check("删掉的会话回到粗作用域的值",
          CLIENT.get("/api/im/policies", params={"conversationId": "C123", "kind": "slack"}).json()["policy"]["dmMedia"]
          == "off")
    CLIENT.post("/api/im/policies", json={"scope": "oc_room7", "drop": True})
    CLIENT.post("/api/im/policies", json={"scope": "slack", "drop": True})
    check("清理后回到干净的一份默认（不留残渣）",
          ipol.load_store().get("scopes") == {} and ipol.load_store()["defaults"] == before,
          str(ipol.load_store())[:200])

    glob = CLIENT.post("/api/im/policies", json={"scope": "", "patch": {"maxMessagesPerMinute": 20}}).json()
    check("空 scope 写的是全局默认",
          glob.get("ok") is True and glob.get("scope") == ""
          and glob.get("stored", {}).get("maxMessagesPerMinute") == 20, str(glob)[:160])
    check("全局那一份回显的是整份默认（不是只有刚改的那项）",
          set(glob.get("stored") or {}) == set(ipol.POLICY_DEFAULTS), str(sorted(glob.get("stored") or {})))
    check("全局默认改了之后每个作用域都跟着变",
          CLIENT.get("/api/im/policies", params={"conversationId": "anything"}).json()["policy"]["maxMessagesPerMinute"]
          == 20)
    CLIENT.post("/api/im/policies", json={"scope": "", "patch": before})
    check("写回原默认后彻底复原（本族不改真实配置）",
          ipol.load_store()["defaults"] == before, str(ipol.load_store()["defaults"])[:200])


# ── 4. policies POST 的拒绝形状 ───────────────────────────────────


def phase_policies_reject() -> None:
    section("4. 表单里会遇到的拒绝：200 + ok=false + 中文")
    cases = (
        ({"scope": "oc_x", "patch": {"groupActivation": "sometimes"}}, "IM_POLICY_INVALID", "非法枚举取值"),
        ({"scope": "oc_x", "patch": {"maxMessagesPerMinute": 9999}}, "IM_POLICY_INVALID", "越界数字"),
        ({"scope": "oc_x", "patch": {"dedupeSeconds": -1}}, "IM_POLICY_INVALID", "负数窗口"),
        ({"scope": "oc_x", "patch": {"groupMedia": "yes"}}, "IM_POLICY_INVALID", "非 on/off 的媒体档"),
        ({"scope": "oc_x", "patch": {"whoKnowsThisKey": 1}}, "IM_POLICY_INVALID", "未知策略项"),
        ({"scope": "../etc", "patch": {"burst": 2}}, "IM_POLICY_INVALID", "带 .. 的作用域"),
        ({"scope": "a/b", "patch": {"burst": 2}}, "IM_POLICY_INVALID", "带分隔符的作用域"),
        ({"scope": "x" * 121, "patch": {"burst": 2}}, "IM_POLICY_INVALID", "超长作用域"),
        ({"scope": "oc_x", "patch": {}}, "IM_POLICY_INVALID", "空 patch"),
        ({"scope": "oc_x"}, "IM_POLICY_INVALID", "没带 patch"),
        ({"patch": {"burst": 2}}, "IM_POLICY_INVALID", "没带 scope 的写（落到全局默认）"),
        ({"scope": "oc_x", "drop": True}, "", "drop 一条不存在的作用域也接得住"),
    )
    for payload, code, label in cases:
        resp = CLIENT.post("/api/im/policies", json=payload)
        body = resp.json()
        if label.startswith("drop"):
            check(label, resp.status_code == 200 and body.get("ok") is True, str(body)[:160])
            continue
        if label.startswith("没带 scope 的写"):
            # 空 scope 是合法的全局默认写法：这里只验它没有被当错误，随后复原
            saved = dict(ipol.load_store().get("defaults") or {})
            check(label, body.get("ok") is True and body.get("scope") == "", str(body)[:160])
            CLIENT.post("/api/im/policies", json={"scope": "", "patch": saved})
            continue
        check(f"{label}：HTTP 200 而不是 500", resp.status_code == 200, str(resp.status_code))
        check(f"{label}：ok=false 且错误码是 {code}",
              body.get("ok") is False and body.get("code") == code, str(body)[:160])
        check(f"{label}：message 是中文且说清改法", any("\u4e00" <= ch <= "\u9fff" for ch in body.get("message", ""))
              and len(body.get("message", "")) >= 6, str(body)[:160])
        if label in ("非法枚举取值", "越界数字", "非 on/off 的媒体档"):
            check(f"{label}：把 shape 一起回给表单（可选项不用前端猜）",
                  bool(body.get("shape", {}).get("enums")), str(body)[:160])

    bad_json = CLIENT.post("/api/im/policies", content=b"this is not json",
                           headers={"content-type": "application/json"})
    check("坏 JSON 按缺参拒绝，不炸 500",
          bad_json.status_code == 200 and bad_json.json().get("code") == "IM_POLICY_INVALID",
          str(bad_json.json())[:160])
    list_body = CLIENT.post("/api/im/policies", json=["burst", 2])
    check("顶层不是对象的请求体同样按缺参拒绝",
          list_body.status_code == 200 and list_body.json().get("ok") is False, str(list_body.json())[:160])
    no_scope_drop = CLIENT.post("/api/im/policies", json={"drop": True})
    check("drop 不带 scope 被拒（全局默认不许从这里删）",
          no_scope_drop.json().get("ok") is False
          and "scope" in no_scope_drop.json().get("message", ""), str(no_scope_drop.json())[:160])
    check("以上拒绝都没留下任何作用域", ipol.load_store().get("scopes") == {}, str(ipol.load_store())[:160])


# ── 5. 读写炸掉 ───────────────────────────────────────────────────


def phase_policies_failure() -> None:
    section("5. 读不出来 / 写不进去：500 + 错误码，不包装成业务拒绝")
    real_load, real_save, real_drop = ipol.load_store, ipol.save_policy, ipol.drop_scope

    def boom_load():
        raise OSError("disk gone")

    ipol.load_store = boom_load
    try:
        resp = CLIENT.get("/api/im/policies")
        check("策略表读不出来回 500", resp.status_code == 500, str(resp.status_code))
        check("错误码是 IM_POLICY_UNREADABLE", resp.json().get("code") == "IM_POLICY_UNREADABLE")
        check("意外也带中文与原始异常类型",
              "策略表读不出来" in resp.json().get("message", "")
              and "OSError" in resp.json().get("message", ""), str(resp.json())[:200])
    finally:
        ipol.load_store = real_load

    def boom_save(patch, scope=""):
        raise OSError("read-only volume")

    ipol.save_policy = boom_save
    try:
        resp = CLIENT.post("/api/im/policies", json={"scope": "oc_boom", "patch": {"burst": 2}})
        check("写入被文件系统拒绝回 500", resp.status_code == 500, str(resp.status_code))
        check("错误码是 IM_POLICY_WRITE_FAILED", resp.json().get("code") == "IM_POLICY_WRITE_FAILED")
        check("中文说明里带上真实原因的类型",
              "策略写入失败" in resp.json().get("message", "")
              and "OSError" in resp.json().get("message", ""), str(resp.json())[:200])
    finally:
        ipol.save_policy = real_save

    def boom_drop(scope):
        raise OSError("disk gone")

    ipol.drop_scope = boom_drop
    try:
        resp = CLIENT.post("/api/im/policies", json={"scope": "oc_boom", "drop": True})
        check("删除失败同样回 500 带 IM_POLICY_WRITE_FAILED",
              resp.status_code == 500 and resp.json().get("code") == "IM_POLICY_WRITE_FAILED",
              str(resp.json())[:200])
    finally:
        ipol.drop_scope = real_drop

    check("换回真实现后读写正常（monkeypatch 没留下副作用）",
          CLIENT.get("/api/im/policies").json().get("ok") is True)


# ── 6. capabilities ───────────────────────────────────────────────


def phase_capabilities() -> None:
    section("6. 能力面：每个实例一行，False 一律带中文原因")
    body = CLIENT.get("/api/im/capabilities").json()
    rows = body.get("rows") or []
    check("回 200 + ok", body.get("ok") is True)
    check("每个登记过的 kind 都有一行", {r["kind"] for r in rows} == set(imch.CHANNEL_CLASSES),
          f"{sorted({r['kind'] for r in rows})} vs {sorted(imch.CHANNEL_CLASSES)}")
    check("行的数量等于实例数量", len(rows) == len(imch.all_runtimes()), f"{len(rows)} vs {len(imch.all_runtimes())}")
    check("kind 清单已排序且去重", body.get("kinds") == sorted({r["kind"] for r in rows}), str(body.get("kinds")))
    check("已接入的排在未接入之前",
          [r["supported"] for r in rows] == sorted([r["supported"] for r in rows], reverse=True),
          str([(r["kind"], r["supported"]) for r in rows]))
    for field, note_field, label in (
        ("mediaIn", "mediaInNote", "收附件"),
        ("mediaOut", "mediaOutNote", "发附件"),
        ("typing", "typingNote", "正在输入"),
    ):
        missing = [r["instanceKey"] for r in rows if not r[field] and not str(r[note_field]).strip()]
        check(f"不支持「{label}」的行都带中文原因", not missing, str(missing))
        lying = [r["instanceKey"] for r in rows
                 if not r[field] and ("已接入" in str(r[note_field]) or f"支持{label}" in str(r[note_field]))]
        check(f"「{label}」为假时说明不许写成已支持（界面照抄这句话）", not lying, str(lying))
    check("每行都写明 @ 判定靠什么证据（或答不出）",
          all(str(r.get("mentionEvidence", "")).strip() for r in rows))
    blind = [r for r in rows if "无法判定" in r["mentionEvidence"]]
    check("答不出被 @ 的通道如实写「无法判定」（至少一条：帧里没有 @ 字段的）",
          bool(blind), str([r["kind"] for r in rows]))
    check("每一条「答不出」都写明默认档下不起任务",
          all(("不起任务" in r["mentionEvidence"] or "拒起任务" in r["mentionEvidence"]) for r in blind),
          str([r["mentionEvidence"][:50] for r in blind]))
    check("每一条「答不出」都给出可执行的改法（改成每条都处理）",
          all("每条都处理" in r["mentionEvidence"] for r in blind),
          str([r["mentionEvidence"][:50] for r in blind]))
    check("能判定与答不出的行都写明了证据来自哪个字段（不是空话）",
          all(any(tok in r["mentionEvidence"] for tok in
                  ("chat_type", "mentions", "isInAtList", "channel_type", "app_mention",
                   "groupCode", "单聊", "文档")) for r in rows),
          str([(r["kind"], r["mentionEvidence"][:24]) for r in rows if
               not any(tok in r["mentionEvidence"] for tok in
                       ("chat_type", "mentions", "isInAtList", "channel_type", "app_mention",
                        "groupCode", "单聊", "文档"))]))
    check("未接入的通道带中文原因，已接入的原因为空",
          all((bool(r["unsupportedReason"]) is False) is bool(r["supported"]) for r in rows))
    check("说明写明能力位来自引擎今天的动作",
          "引擎" in body.get("note", "") and "False" in body.get("note", ""), body.get("note", ""))
    check("行里没有 loginMode 之外的凭据字段",
          all(not {"credentials", "appSecret", "token", "botToken"} & set(r) for r in rows))

    a = clean_runtime("dingtalk", "capA")
    b = clean_runtime("dingtalk", "capB")
    two = CLIENT.get("/api/im/capabilities").json()["rows"]
    row_a, row_b = row_of(two, a.instance_key), row_of(two, b.instance_key)
    check("同类型两个实例各占一行（互不合并）",
          row_a.get("channelId") == "capA" and row_b.get("channelId") == "capB"
          and row_a["instanceKey"] != row_b["instanceKey"], f"{row_a.get('instanceKey')} / {row_b.get('instanceKey')}")
    check("同类型两行的能力位与标签一致（能力属于通道类型，不属于某个实例）",
          row_a["mediaIn"] == row_b["mediaIn"] and row_a["label"] == row_b["label"])
    check("configured 是 False（本族没写过这两条实例的凭据）",
          row_a.get("configured") is False and row_b.get("configured") is False)
    check("没连对端时 running 为 False（不虚报在线）",
          row_a.get("running") is False and row_b.get("running") is False)
    check("实例行按 channel_id 排在同类型内部（清单顺序可复现）",
          [r["instanceKey"] for r in two if r["kind"] == "dingtalk"]
          == sorted([r["instanceKey"] for r in two if r["kind"] == "dingtalk"]),
          str([r["instanceKey"] for r in two if r["kind"] == "dingtalk"]))


# ── 7. traffic ────────────────────────────────────────────────────


def phase_traffic() -> None:
    section("7. 流量面：冷却与挡下的条数要看得见")
    body = CLIENT.get("/api/im/traffic").json()
    rows = body.get("rows") or []
    check("回 200 + ok", body.get("ok") is True)
    check("逐实例一行", len(rows) == len(imch.all_runtimes()), str(len(rows)))
    need = {"kind", "channelId", "instanceKey", "label", "state", "configured", "running",
            "rateLimit", "counters", "lastSkip", "sendNote", "typingState", "dedupeNote",
            "media", "lastError"}
    check("每行都带齐读数键", all(need <= set(r) for r in rows), str([k for k in rows[0]][:5]))
    check("限流读数里有额度与计数",
          all({"perMinute", "burst", "counters", "blockedConversations"} <= set(r["rateLimit"]) for r in rows))
    check("计数键与框架的计数器同名",
          all(set(r["counters"]) == set(imch.all_runtimes()[0].counters) for r in rows))
    check("排重窗口用中文说明", "只处理一次" in rows[0]["dedupeNote"] or "未做重复抑制" in rows[0]["dedupeNote"],
          rows[0]["dedupeNote"])
    check("totals 里有 instances 且等于行数", rows and body["totals"]["instances"] == len(rows))

    quiet = CLIENT.get("/api/im/traffic").json()
    cooled_quiet = sum(int(r["rateLimit"].get("blockedConversations") or 0) for r in quiet["rows"])
    throttled_quiet = sum(int(r["counters"].get("throttled") or 0) for r in quiet["rows"])
    if not cooled_quiet and not throttled_quiet:
        check("没有限流时 note 如实说没有", "没有限流或冷却中的会话" in quiet["note"], quiet["note"])

    rt = clean_runtime("feishu", "trafA")
    rt.governor.penalize(f"{rt.instance_key}|oc_x", 90, why="frequency limit exceeded")
    one = CLIENT.get("/api/im/traffic").json()
    row = row_of(one["rows"], rt.instance_key)
    check("429 之后行里数得出冷却中的会话", row["rateLimit"].get("blockedConversations") == 1, str(row["rateLimit"]))
    check("note 报的冷却会话数不是恒为 0（读的是 blockedConversations）",
          "1 个会话在平台冷却中" in one["note"], one["note"])
    check("note 用中文说清「发送自动暂停」", "发送自动暂停" in one["note"], one["note"])
    check("行里带上引擎此刻的中文状态", rt.instance_key and isinstance(row["sendNote"], str))
    rt.governor.clear()
    rt.counters["throttled"] = 2
    two = CLIENT.get("/api/im/traffic").json()
    check("冷却清掉后不再报会话在冷却",
          row_of(two["rows"], rt.instance_key)["rateLimit"]["blockedConversations"] == 0,
          str(row_of(two["rows"], rt.instance_key)["rateLimit"]))
    check("被本地额度挡下的条数求和进 totals 与 note",
          two["totals"]["throttled"] >= 2 and "另有" in two["note"] and "条回复被逐实例限流挡下" in two["note"],
          f'{two["totals"]} / {two["note"]}')
    check("totals 的每一项都是各行求和",
          all(two["totals"][k] == sum(int(r["counters"].get(k) or 0) for r in two["rows"])
              for k in ("inbound", "runs", "skipped", "deduped", "mediaStored", "mediaRejected")),
          str(two["totals"]))

    rt.counters["inbound"] = 3
    rt.counters["runs"] = 1
    rt.counters["skipped"] = 2
    rt.last_skip = {"reason": "群里没有被 @：按策略不起任务"}
    three = CLIENT.get("/api/im/traffic").json()
    row3 = row_of(three["rows"], rt.instance_key)
    check("被挡下的那条把中文原因原样带出来",
          row3["lastSkip"].get("reason") == "群里没有被 @：按策略不起任务", str(row3["lastSkip"]))
    check("inbound / runs / skipped 逐实例记账",
          row3["counters"]["inbound"] == 3 and row3["counters"]["skipped"] == 2, str(row3["counters"]))
    check("附件用量按实例目录给（files/bytes 是数字不是文本）",
          isinstance(row3["media"]["files"], int) and isinstance(row3["media"]["bytes"], int), str(row3["media"]))
    check("媒体读数标出这是哪个实例的目录",
          row3["media"]["scope"] == imedia.MediaBackend(imedia.media_root())._slug(rt.instance_key),
          str(row3["media"]))
    clean_runtime("feishu", "trafA")
    four = CLIENT.get("/api/im/traffic").json()
    check("清账之后回到平静读数（不留残影）",
          row_of(four["rows"], rt.instance_key)["counters"]["throttled"] == 0)


# ── 8. 不泄露 ─────────────────────────────────────────────────────


def phase_no_leak() -> None:
    section("8. 三条端点都不许把凭据带出去")
    rt = clean_runtime("dingtalk", "leakA")
    imch.save_config("dingtalk:leakA", {"app_key": SECRET_APP_ID, "app_secret": SECRET_APP_SECRET,
                                        "token": SECRET_TOKEN})
    try:
        configured = row_of(CLIENT.get("/api/im/capabilities").json()["rows"], rt.instance_key)
        check("配了凭据的实例在能力面上报 configured=true（但不带值）",
              configured.get("configured") is True, str(configured)[:160])
        blobs = {
            "policies": str(CLIENT.get("/api/im/policies", params={"channelId": "leakA", "kind": "dingtalk"}).json()),
            "capabilities": str(CLIENT.get("/api/im/capabilities").json()),
            "traffic": str(CLIENT.get("/api/im/traffic").json()),
        }
        for name, blob in blobs.items():
            for secret in (SECRET_APP_SECRET, SECRET_TOKEN, SECRET_APP_ID):
                check(f"{name} 不带出凭据明文（{secret[:12]}…）", secret not in blob, blob[:160])
            check(f"{name} 不带 credentials 字段", "credentials" not in blob, blob[:120])
        traffic = CLIENT.get("/api/im/traffic").json()
        row = row_of(traffic["rows"], rt.instance_key)
        check("流量面不带会话台账与消息键（只有状态与计数）",
              not ({"messages", "text", "session", "sessions", "creds", "credentials"} & set(row)),
              str(sorted(row))[:220])
        check("能力面行里同样没有会话内容键",
              not ({"messages", "text", "credentials", "creds"} & set(configured)), str(sorted(configured))[:220])
        check("策略面读的是差异项，不是消息",
              set(CLIENT.get("/api/im/policies").json()) == {"ok", "scope", "chain", "policy",
                                                             "defaults", "scopes", "shape"})
        # 中文状态照样显示：防泄露靠的是不下发内容，不是把状态说明一起抹掉
        rt.send_note = "附件太大没发出去，只发了文本"
        gate = rt.governor.penalize(f"{rt.instance_key}|oc_leak", 45, why="frequency limit exceeded")
        after = row_of(CLIENT.get("/api/im/traffic").json()["rows"], rt.instance_key)
        check("冷却中的中文状态照样下发（不是靠删掉说明来防泄露）",
              "对端限流" in after["rateLimit"]["lastReason"]
              and "暂停发送 45 秒" in after["rateLimit"]["lastReason"], str(after["rateLimit"])[:200])
        check("限流原因里只有对端的措辞，不带我们发出去的回贴正文",
              "附件太大没发出去" not in after["rateLimit"]["lastReason"] and gate.allowed is False)
        check("回贴状态说明照常在行里", after["sendNote"] == "附件太大没发出去，只发了文本", after["sendNote"])
        clean_runtime("dingtalk", "leakA")
    finally:
        imch.drop_config("dingtalk:leakA")
    gone = row_of(CLIENT.get("/api/im/capabilities").json()["rows"], rt.instance_key)
    check("解绑之后 configured 回到 False", gone.get("configured") is False, str(gone)[:160])
    check("解绑后凭据文件里没有这一条", "leakA" not in str(imch.load_config()), str(imch.load_config())[:200])


def main() -> int:
    print("== IM connect 车道端点自测（D-41）==")
    phase_registration()
    phase_policies_get()
    phase_policies_write()
    phase_policies_reject()
    phase_policies_failure()
    phase_capabilities()
    phase_traffic()
    phase_no_leak()
    print(f"\n[DONE] im_connect_routes selftest: {len(FAILURES)} failure(s)")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    sys.exit(main())
