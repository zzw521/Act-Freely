"""扩展声明面（D-08）：把「装了的东西声明了什么能力」摊成一份可核对的清单。

为什么要有这一份：市场装进来的扩展（技能 / 套件指令 / MCP 连接器 / Hooks 脚本）最终都落在
同一条链上——要么把正文交给模型，要么声明一个要跑的程序。用户装的时候看不见自己给了什么权限，
出事后也无处对账。这份清单的职责就是把「声明了什么 ⇒ 现在能不能用 ⇒ 为什么」一次摊清，
让界面能原样展示，也让验收能逐条打勾。

口径（与实现同源，不写在文档里空转）：
- 数据只来自桌面端随请求下发的 `runtime`。**不接受调用方给目录**，不去盘上扫扩展包，
  所以这份清单不会把「本机装了但本轮没进执行面」的东西说成可用。
- 正文不外泄：SKILL.md 内容、连接器 env / headers 的**值**、Hook 命令的完整参数都不进清单，
  只留名字、数量与首个程序名。清单是给界面看的，不是给模型抄的。
- 「未声明即不可用」在这里是可核对的：模型能驱动的执行面只有 `run_command`（命令白名单）、
  `run_shell`（显式授权）与 `fetch_url`（主机白名单），三者的注册判定读的是
  `tool_grants.ToolGrantPolicy.registration_plan()`——扩展声明得再响，工具没注册就是点不通，
  界面与注册走的是同一个判定，不存在两套说法。
- 容器（Windows AppContainer）这一层与终端 / MCP / ACP / Hooks 同源：受控执行也进同一只容器，
  容器起不来时执行面**不注册**而不是注册后拒绝。

已知边界如实写在 `limits` 里（引擎看不到 Hook 已放行命令的正文、SKILL.md 的
`toolsAllow` / `toolsDeny` 到不了引擎、出网只有整组开关、非 Windows 平台没有系统级隔离）。
"""

from __future__ import annotations

import threading
from pathlib import Path

import runtime_context as rc
import tool_grants as tg

# 声明词表（界面的图例与契约都按这几个 id 走，不再自造说法）
DECLARED = {
    "instructions": "正文交给模型（技能 / 套件指令）",
    "executable": "声明了一个要跑的程序",
    "auto_run": "不等模型点头，事件一到就执行",
    "network": "要出网",
    "env": "携带环境变量（只列名字，值不外泄）",
    "external_agent": "把活儿外派给另一个 Agent 进程",
}

MAX_ENTRIES = 200            # 清单条数上限：一台机器装几百个扩展时界面仍然可读，超出如实说明
MAX_NAME_CHARS = 80          # 单条名字的长度上限
MAX_UNGRANTED_COMMANDS = 20  # 未放行 Hook 命令逐条列出的上限

_lock = threading.Lock()
_last: dict | None = None


def _name(raw: object, fallback: str = "") -> str:
    text = str(raw or "").strip() or fallback
    return text[:MAX_NAME_CHARS]


def _program_of(command: object, args: object = None) -> str:
    """只取「要跑哪个程序」：绝对路径取文件名，裸命令名原样，拿不到就留空"""
    raw = str(command or "").strip()
    if not raw:
        first = ""
        if isinstance(args, list) and args:
            first = str(args[0] or "").strip()
        if not first or Path(first).is_absolute() or "/" in first or "\\" in first:
            return ""
        raw = first
    return _name(Path(raw.replace("\\", "/")).name, "")


def _entry(**kwargs) -> dict:
    item = {
        "id": "",
        "name": "",
        "kind": "",
        "source": "",
        "suite": "",
        "declares": [],
        "executable": "",
        "usable": False,
        "reason": "",
        "container": "n/a",
    }
    item.update({k: v for k, v in kwargs.items() if k in item})
    return item


# ── 各类扩展面的摊取 ────────────────────────────────────────────

def _skill_entries(runtime: dict) -> list[dict]:
    """技能与套件指令：正文交给模型，本身不直接执行代码

    `runtime_context.normalize_skills` 已经滤过 `scanBlocked`（被安全扫描判高风险的正文
    根本不进执行面），这里再读一次同一份判定，把「被拦」的条目按不可用列出——
    界面要能回答「我装的那个技能为什么没反应」。
    """
    out: list[dict] = []
    raw = (runtime or {}).get("skills")
    if not isinstance(raw, list):
        return out
    rows = rc.normalize_skills(runtime)
    blocked_ids = {
        str(item.get("id") or "").strip()
        for item in raw
        if isinstance(item, dict) and item.get("scanBlocked") is True
    }
    for row in rows:
        sid = row["id"]
        blocked = sid in blocked_ids
        if blocked:
            usable, reason = False, "安全扫描判定为高风险，正文未进入执行面（未声明的能力不可用）"
        elif row["error"]:
            usable, reason = False, f"未提供正文：{row['error']}"
        else:
            usable = True
            reason = (
                "正文只交给模型阅读，扩展本身不执行代码；"
                "它指挥模型用的执行面是 run_command / run_shell，二者是否可用见 execution"
            )
        out.append(_entry(
            id=sid,
            name=_name(row["name"], sid),
            kind="suite_command" if row["kind"] == "command" else "skill",
            source=_name(row["source"], "installed"),
            suite=_name(row["suite"]),
            declares=["instructions"],
            usable=usable,
            reason=reason,
        ))
    return out


def _mcp_entries(runtime: dict) -> list[dict]:
    """MCP 连接器：stdio 类声明了一个要跑的程序，http 类声明了出网"""
    out: list[dict] = []
    block = (runtime or {}).get("mcp")
    if isinstance(block, dict) and block.get("error"):
        out.append(_entry(
            id="mcp:table", name="MCP 连接器表", kind="mcp",
            declares=["executable", "network"],
            reason=f"连接器清单读不出来：{str(block['error'])[:200]}",
        ))
        return out
    if not isinstance(block, list):
        return out
    container = tg.container_state()
    for row in block:
        if not isinstance(row, dict):
            continue
        sid = _name(row.get("serverId"), "mcp")
        transport = str(row.get("transport") or "").strip().lower() or "stdio"
        tools = row.get("tools") if isinstance(row.get("tools"), list) else []
        env_names = [
            _name(k) for k in sorted((row.get("env") or {}).keys())[:12]
        ] if isinstance(row.get("env"), dict) else []
        declares = []
        executable = ""
        if transport in ("stdio", ""):
            executable = _program_of(row.get("command"))
            if executable:
                declares.append("executable")
        if transport not in ("stdio", "") or executable:
            # stdio 起的服务器自己也要联网（拉包、调远端 API），http 类更是直接出网
            declares.append("network")
        if env_names:
            declares.append("env")
        in_container = "in" if container["enabled"] and container["ready"] and executable else "out"
        out.append(_entry(
            id=f"mcp:{sid}",
            name=_name(row.get("name"), sid),
            kind="mcp",
            source="mcp",
            declares=declares or ["network"],
            executable=executable,
            usable=True,
            reason=(
                f"已连接（桌面端握过手）才会下发到引擎，presence 即授权；声明工具 {len(tools)} 个"
                + (f"，环境变量名：{'、'.join(env_names)}（值不下发）" if env_names else "")
                + (
                    "；启动命令在系统隔离容器内执行"
                    if in_container == "in" else
                    "；启动命令未进容器（本机未启用系统隔离）"
                    if not container["enabled"] else ""
                )
            ),
            container=in_container,
        ))
    return out


def _hook_entries(runtime: dict) -> list[dict]:
    """Hooks：只有「未放行」的命令正文会到引擎，已放行的由本机网关执行"""
    out: list[dict] = []
    block = (runtime or {}).get("hooks")
    if block is None:
        return out
    if isinstance(block, str):
        import json
        try:
            block = json.loads(block)
        except Exception:  # noqa: BLE001
            block = {"error": "Hooks 配置不是合法 JSON"}
    if not isinstance(block, dict):
        return out
    if block.get("error"):
        out.append(_entry(
            id="hooks:read", name="Hooks 配置", kind="hook",
            declares=["auto_run", "executable"],
            reason=f"读不出来：{str(block['error'])[:200]}",
        ))
        return out
    container = tg.container_state()
    configured = int(block.get("configured") or 0)
    ungranted = [str(c or "").strip() for c in (block.get("ungranted") or []) if str(c or "").strip()]
    granted = max(configured - len(ungranted), 0)
    sandboxed = block.get("sandboxed") is True
    if block.get("gateError"):
        out.append(_entry(
            id="hooks:gate", name="本机 Hook 网关", kind="hook",
            declares=["auto_run", "executable"],
            reason=str(block["gateError"])[:200],
        ))
    if granted:
        reason = f"已放行的 {granted} 条由桌面端本机网关执行，命令正文不下发到引擎"
        if container["enabled"] and not container["ready"]:
            reason += "；本轮系统隔离已启用但容器不可用，网关侧按不放行处理"
        elif container["enabled"]:
            reason += "；已放行的命令在系统隔离容器内执行"
        if isinstance(sandboxed, bool) and sandboxed != bool(container["enabled"] and container["ready"]):
            # 桌面端与引擎对「进没进容器」的说法必须一致，不一致就摊开写，不静默取一个
            reason += (
                f"（注意：桌面端上报 sandboxed={sandboxed}，与引擎本轮容器状态读数不一致，"
                "以实际执行为准，这条差异需要排查）"
            )
        out.append(_entry(
            id="hooks:granted",
            name=f"Hooks（已放行 {granted} 条）",
            kind="hook", source="hooks",
            declares=["auto_run", "executable"],
            usable=True,
            reason=reason,
            container="in" if container["enabled"] and container["ready"] else "out",
        ))
    for index, cmd in enumerate(ungranted[:MAX_UNGRANTED_COMMANDS]):
        out.append(_entry(
            id=f"hooks:ungranted:{index}",
            name=_name(cmd, "未放行的 Hook 命令"),
            kind="hook", source="hooks",
            declares=["auto_run", "executable"],
            executable=_program_of(cmd.split(None, 1)[0] if cmd else ""),
            usable=False,
            reason="授权台账里没有这条命令，Hook 网关拒绝执行（未授权即不注册，不是注册后拒绝）",
        ))
    if len(ungranted) > MAX_UNGRANTED_COMMANDS:
        out.append(_entry(
            id="hooks:ungranted:more",
            name=f"另有 {len(ungranted) - MAX_UNGRANTED_COMMANDS} 条未放行的 Hook 命令未逐条列出",
            kind="hook", source="hooks", declares=["auto_run", "executable"],
            usable=False, reason="逐条列出有上限，超出部分只报数量",
        ))
    return out


def _acp_entries(runtime: dict) -> list[dict]:
    """ACP 外派：用户显式配置了启动命令才注册工具，那条命令本身就是可执行件"""
    out: list[dict] = []
    block = (runtime or {}).get("acp")
    rows = block.get("agents") if isinstance(block, dict) else None
    if not isinstance(rows, list):
        return out
    container = tg.container_state()
    for row in rows:
        if not isinstance(row, dict):
            continue
        aid = _name(row.get("id") or row.get("agentId"), "acp")
        executable = _program_of(row.get("command"))
        out.append(_entry(
            id=f"acp:{aid}",
            name=_name(row.get("name"), aid),
            kind="acp", source="acp",
            declares=["external_agent"] + (["executable"] if executable else []),
            executable=executable,
            usable=bool(row.get("command")),
            reason=(
                "把整段任务外派给另一个 Agent 进程，那条启动命令在系统隔离容器内执行"
                if container["enabled"] and container["ready"] and executable else
                "配置了启动命令才会注册委派工具；未配置就不进执行面"
            ),
            container="in" if container["enabled"] and container["ready"] and executable else "out",
        ))
    return out


def _execution(runtime: dict) -> dict:
    """模型真正能点的执行面：与 `install()` 同一份判定"""
    policy = tg.ToolGrantPolicy.from_runtime(runtime)
    plan = policy.registration_plan()
    return {
        "runCommand": plan["run_command"],
        "runShell": plan["run_shell"],
        "fetchUrl": plan["fetch_url"],
        "allowlist": policy.exec_allowlist[:16],
        "hosts": policy.network_allowlist[:16],
        "container": plan["container"],
        "network": tg.sl_network(),
    }


def _limits(execution: dict) -> list[str]:
    """已知的说不圆之处——写出来比留白强"""
    container = execution["container"]
    out = [
        "扩展声明的能力不会自己获得权限：技能与套件正文只能指挥模型使用已注册的工具，"
        "未注册的工具连工具面都进不去。",
        "SKILL.md 的 toolsAllow / toolsDeny 到不了引擎（桌面端下发时没带这两个字段），"
        "所以「这个技能只准用某几个工具」目前无法按技能粒度强制，只能按整轮沙箱策略拦。",
        "已放行的 Hook 命令正文留在桌面端 SQLite，引擎只拿到条数与未放行清单，"
        "界面上这一类只能按条展示。",
        "出网是整组开关（容器层面只有 internetClient 一组能力），"
        "按进程 / 按域名的出域白名单还没做；fetch_url 的主机白名单管的是模型直接联网的工具，"
        "不约束连接器自己发的请求。",
    ]
    if not container["enabled"]:
        out.append("本机未启用系统隔离：受控执行只受工作目录、超时与资源限制约束，没有容器这一层。")
    elif not container["ready"]:
        out.append(f"系统隔离已启用但容器不可用：{container['reason']}")
    else:
        out.append("连接器与 Hook 的出网只在容器整组开关上体现，与本轮出网档位"
                   f"（{execution['network']}）一致；容器内连回环地址是否可达需要在真机上验收。")
    return out


# ── 快照 ────────────────────────────────────────────────────────

def observe(runtime: dict | None, task_id: str = "") -> dict:
    """从一份 runtime 摊出声明清单并存为「最近一次读数」；任何意外都不许把任务带崩"""
    snapshot: dict = {
        "observed": True,
        "taskId": str(task_id or ""),
        "extensions": [],
        "execution": {},
        "limits": [],
        "counts": {},
        "truncated": False,
        "error": "",
    }
    try:
        payload = runtime if isinstance(runtime, dict) else {}
        entries: list[dict] = []
        entries += _skill_entries(payload)
        entries += _mcp_entries(payload)
        entries += _hook_entries(payload)
        entries += _acp_entries(payload)
        execution = _execution(payload)
        counts: dict[str, int] = {}
        for item in entries:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        snapshot["counts"] = counts
        snapshot["execution"] = execution
        snapshot["limits"] = _limits(execution)
        if len(entries) > MAX_ENTRIES:
            snapshot["truncated"] = True
            snapshot["limits"] = snapshot["limits"] + [
                f"扩展声明条目超过 {MAX_ENTRIES} 条，仅前 {MAX_ENTRIES} 条进入清单"
            ]
        snapshot["extensions"] = entries[:MAX_ENTRIES]
    except Exception as exc:  # noqa: BLE001  摊清单失败不能影响任务本身
        snapshot["observed"] = False
        snapshot["error"] = f"扩展声明清单生成失败：{type(exc).__name__}"
    global _last
    with _lock:
        _last = snapshot
    return snapshot


def snapshot() -> dict:
    """最近一次读数；本轮还没跑过任务时如实说「没有读数」，不伪造一份空清单"""
    with _lock:
        if _last is not None:
            return _last
    container = tg.container_state()
    return {
        "observed": False,
        "taskId": "",
        "extensions": [],
        "execution": {},
        "counts": {},
        "truncated": False,
        "limits": [
            "还没有一次任务把扩展配置下发到引擎，这份清单只反映引擎进程当前读得到的容器状态。",
            "任务开始后才摊得到的部分（技能 / 连接器 / Hook 声明）不在此列——"
            "引擎不去盘上扫扩展包，避免把「装了但本轮没进执行面」的东西说成可用。",
        ],
        "executionNote": (
            f"容器读数：{'已启用且可用' if container['ready'] else container['reason']}"
        ),
        "container": container,
    }


def reset_for_tests() -> None:
    """自测用：清掉「最近一次读数」，避免用例之间互相串味"""
    global _last
    with _lock:
        _last = None
