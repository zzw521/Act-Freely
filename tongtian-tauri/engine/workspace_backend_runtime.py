"""D-29（#157）：工作区后端抽象 —— 本地 / Docker / PostgreSQL 半边，S3 / COS / SSH 只到适配器。

台账口径（§D-29）：「仅本地目录；无 Docker / SSH 执行器，也无 S3 / COS / PostgreSQL 工作区后端」，
验收三句是 **不换 Agent 配置即可切换工作区**、**远程操作可审计**、**凭据不落配置文件**。
本模块只把这三句做到「本机可测」的形状，凡是真容器、真数据库、真对象存储、真 SSH 主机
都在 ``probe()`` 与自测里如实标 **未验收**（本机没有可用的 Docker daemon，也没有 PG 服务）。

四条口径写在这里，代码与测试都按它来：

1. **凭据只接受引用，不接受字面量**（``env:<变量名>`` / ``keychain:<服务>/<账户>``）。
   配置里出现明文密钥不是「宽容处理」而是当场拒绝 —— 否则一份 settings 导出就把密钥带走了。
   引擎进程里没有钥匙串访问件，所以 ``keychain:`` 引用一律回「未接线 ⇒ 未验收」，
   不假装能取到值。任何快照、日志、审计、IPC 返回体都不带凭据原值，只带指纹前缀。
2. **路径判定复用 D-45 的那一套，不另起规则**。分隔符归一、命名空间前缀剥离、
   分段前缀比对（``sr._norm_path`` / ``sr._rule_hit``）都直接取 ``sandbox_runtime``。
   本机落地的后端（local / docker 的绑定目录）连判定都原样交给
   ``policy.file_denial()``，于是档位与沙箱两段的**文案与优先级**与既有文件工具完全一致。
3. **三段拒绝各有各的措辞与来源**，绝不共用一句：
   - ``backend_boundary``：越出这个后端自己的根前缀（对象存储的 key 前缀、库表的路径列前缀）。
     这句话点名「与文件访问档位、工作区沙箱白名单无关」，因为它改不动档位；
   - ``file_permission``：D-45 的档位拒绝。远程对象路径的这句**不承诺「切到完全访问就能写」**，
     因为「完全访问」这一档管的是本机文件系统，拿它去开对象存储前缀是假承诺；
   - ``sandbox``：沙箱拒绝（黑名单命中 / L0 只读 / 白名单外），沿用 ``sandbox_runtime`` 原句。
   顺序是 边界 → 沙箱 → 档位：后端前缀是配置事实，比任何一轮对话里选的档位都宽不了。
4. **资源与网络限制只在「有地方强制」的地方强制**。Docker 那侧 ``--memory`` / ``--cpus`` /
   ``--pids-limit`` / ``--network`` 是命令行上真能给的重数；PostgreSQL 与对象存储那侧
   本机连接件管不到对端的 statement_timeout、角色配额与桶策略，所以只申报「本层无法强制」
   并写进 ``probe()`` 的未验收清单，不写进「已支持」。

工具面（``engine/toolkit_workspace.py``）与端点面（``engine/routes_workspace.py``）都走 #164 车道，
本文件不 import ``server``，也不自带任何副作用工具名单（名单归 ``side_effect_registry``，由 W4 落表）。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import sandbox_runtime as sr

try:  # data_paths 缺时不影响判定，只是远程审计落不到数据根（会退回内存缓冲并打印）
    import data_paths as dp
except Exception:  # noqa: BLE001
    dp = None  # type: ignore[assignment]


logger = logging.getLogger("workspace_backend")

# ── 后端种类与能力 ──────────────────────────────────────────────

BACKEND_KINDS = ("local", "docker", "postgresql", "s3", "cos", "ssh")

KIND_LABELS = {
    "local": "本地目录",
    "docker": "Docker 容器",
    "postgresql": "PostgreSQL 表",
    "s3": "S3 兼容对象存储",
    "cos": "腾讯云 COS",
    "ssh": "SSH 远程主机",
}

# 能力位：界面与工具面按它决定给不给入口，而不是按「 kind 名字听起来能做」
CAPABILITY_FILE_OPS = "file_ops"        # 列目录 / 读 / 写 / 删
CAPABILITY_EXEC = "exec"                # 在该后端上起命令
CAPABILITY_LIST_ONLY = "list_only"      # 只能列，改不了（当前没有后端落在这位上）

KIND_CAPABILITIES: dict[str, frozenset[str]] = {
    "local": frozenset({CAPABILITY_FILE_OPS}),
    "docker": frozenset({CAPABILITY_FILE_OPS, CAPABILITY_EXEC}),
    "postgresql": frozenset({CAPABILITY_FILE_OPS}),
    "s3": frozenset({CAPABILITY_FILE_OPS}),
    "cos": frozenset({CAPABILITY_FILE_OPS}),
    "ssh": frozenset({CAPABILITY_FILE_OPS, CAPABILITY_EXEC}),
}

# 这两个 kind 的对象真的落在本机文件系统的某个目录里（docker 是绑定挂载），
# 所以它们的越界判定可以整条交给 D-45 的 file_denial，不重新发明。
HOST_BACKED_KINDS = ("local", "docker")

STATUS_VERIFIED = "本机已验"
STATUS_UNVERIFIED = "未验收"

# 本机实测：docker CLI 在，daemon 不在（`docker info` 回 npipe 连不上）。
# 这句只描述本机现场，不代表产品结论 —— 真容器执行仍是 #162 收口批的验收项。
DOCKER_DAEMON_NOTE = (
    "本机 docker CLI 存在但 daemon 未运行（docker info 回 failed to connect to the docker API "
    "at npipe），故容器执行与资源限制只在命令行构造层验证，真容器形态未验收"
)

# ── 拒绝来源（三段各有各的来源，界面按来源选文案，不按字符串猜）──

SOURCE_BACKEND = "backend"
SOURCE_BACKEND_BOUNDARY = "backend_boundary"
SOURCE_FILE_PERMISSION = "file_permission"  # 与 D-45 同名同源，别改名
SOURCE_SANDBOX = "sandbox"

CODE_SPEC_INVALID = "WS_SPEC_INVALID"
CODE_PATH_UNSAFE = "WS_PATH_UNSAFE"
CODE_PATH_OUTSIDE_BACKEND = "WS_PATH_OUTSIDE_BACKEND"
CODE_BACKEND_READONLY = "WS_BACKEND_READONLY"
CODE_TIER_DENIES_REMOTE = "WS_TIER_DENIES_REMOTE_WRITE"
CODE_DELETE_DENIED = "WS_DELETE_DENIED"
CODE_DELETE_NOT_FULL_ACCESS = "WS_DELETE_NOT_FULL_ACCESS"
CODE_CREDENTIAL_LITERAL = "WS_CREDENTIAL_LITERAL_REJECTED"
CODE_CREDENTIAL_UNRESOLVED = "WS_CREDENTIAL_UNRESOLVED"
CODE_DRIVER_MISSING = "WS_DRIVER_MISSING"
CODE_TRANSPORT_MISSING = "WS_TRANSPORT_MISSING"
CODE_LIMIT_INVALID = "WS_LIMIT_INVALID"
CODE_IMAGE_INVALID = "WS_IMAGE_INVALID"
CODE_NETWORK_INVALID = "WS_NETWORK_INVALID"
CODE_CAPABILITY_DENIED = "WS_CAPABILITY_DENIED"
CODE_BACKEND_UNAVAILABLE = "WS_BACKEND_UNAVAILABLE"
CODE_BACKEND_NOT_FOUND = "WS_BACKEND_NOT_FOUND"
CODE_BACKEND_REQUIRED = "WS_BACKEND_REQUIRED"
CODE_DESKTOP_ONLY = "WS_DESKTOP_ONLY"
CODE_INTERNAL = "WS_INTERNAL"

# 密钥字段名：配置里出现这些键时，值必须是一条引用；连私钥**路径**也按引用取，
# 免得配置文件里写死一台机器的目录布局（导出配置就等于导出主机结构）。
_CREDENTIAL_KEYS = (
    "accessKey",
    "accessKeyId",
    "secretKey",
    "secretAccessKey",
    "secretId",
    "sessionToken",
    "password",
    "privateKey",
    "keyPath",
    "token",
    "apiKey",
)

# SSH 的执行类操作按「与 run_shell 同一档」判定：远程起命令和本机起命令是同一类风险，
# 复用 sandbox_runtime 已有的白名单/黑名单/L0 规则，不在此另立一套口径。
EXEC_ASSESSMENT_TOOL = "run_shell"

# 凭据引用的唯一两种形式
CREDENTIAL_REF_SCHEMES = ("env", "keychain")

_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_WIN_DRIVE = re.compile(r"^[a-zA-Z]:")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")  # PG schema / table 标识符
_IMAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/:@]{0,254}$")
_BACKEND_ID = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]{0,63}$")
# 桶名要进 URL path 与签名串，不接受 / ? # 这类会改寻址的字符；
# 首尾必须是字母数字（S3 与 COS 的命名规则都排除首尾连字符/点）。
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]$")
# 远程主机名要拼进 ssh argv 的 target。这里禁首字符是连字符——
# 否则 `-oProxyCommand=...` 会被 ssh 当成选项而不是主机名。
_SSH_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,252}$")

# docker 的 extraArgs 里这些重数不许由配置带进来：它们会把绑定挂载、网络、
# 命名空间隔离整条绕开，等于用「工作区后端」的名义换掉沙箱。
_DOCKER_FORBIDDEN_ARGS = (
    "--privileged",
    "--pid",
    "--pid-limit",
    "--network",
    "--net",
    "--security-opt",
    "--cap-add",
    "--cap-drop",
    "--userns",
    "--device",
    "--device-cgroup-rule",
    "-v",
    "--volume",
    "-m",
    "--memory",
    "--memory-swap",
    "--cpus",
    "--cpu-quota",
    "--pids-limit",
    "--entrypoint",
    "--env",
    "-e",
)

NETWORK_MODES = ("none", "bridge", "host")


# ── 小工具 ──────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _brief(text: object, limit: int = 180) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[: limit - 1] + "…"


def _fingerprint(value: object) -> str:
    """凭据/内容的不可逆短指纹：只用于「同一份没变」的比对与日志，绝不反推原值"""
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()[:12]


def _jsonable(value: Any) -> Any:
    """出得去 JSON 的返回值：Path / tuple / set 这些在 HTTP 层 500 之前就该被换掉"""
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (tuple, list)):
        return [_jsonable(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(_jsonable(v) for v in value)
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


def _utf8_len(text: str) -> int:
    return len(str(text or "").encode("utf-8"))


# ── 凭据引用 ────────────────────────────────────────────────────


@dataclass
class CredentialRef:
    """一条凭据引用；值永远不在对象里长期驻留（resolve 时现取，用完不留副本）"""

    key_name: str = ""
    scheme: str = ""
    name: str = ""
    # 解析出来的值只在本次操作内活着；describe()/snapshot()/审计一律只看这个指纹
    value: str = field(default="", repr=False)

    @property
    def display(self) -> str:
        return f"{self.scheme}:{self.name}" if self.scheme else ""

    def redact(self) -> dict[str, Any]:
        out: dict[str, Any] = {"field": self.key_name, "source": self.display}
        if self.scheme == "env":
            out["resolved"] = bool(self.value)
        return out


def parse_credential_ref(field_name: str, raw: Any) -> tuple[CredentialRef | None, str]:
    """把配置里的一个凭据键读成引用；**明文一律拒绝**。

    返回 (引用或 None, 拒绝原因)。空值不算拒绝（该字段没配），由调用方按
    「缺凭据」处理 —— 缺凭据与写错凭据是两件事，混成一句会让界面说「没配」而用户明明配了。
    """
    text = str(raw or "").strip()
    if not text:
        return None, ""
    if _CONTROL_CHARS.search(text):
        return None, f"凭据字段「{field_name}」含控制字符，按非法引用处理"
    scheme, sep, name = text.partition(":")
    if not sep:
        return None, (
            f"凭据字段「{field_name}」写的是字面量：配置里只接受 "
            f"env:<环境变量名> 或 keychain:<服务>/<账户> 两种引用，"
            f"密钥本体不许进配置文件（导出配置就等于导出密钥）"
        )
    scheme = scheme.strip().lower()
    name = name.strip()
    if scheme not in CREDENTIAL_REF_SCHEMES or not name:
        return None, (
            f"凭据字段「{field_name}」的引用形式不支持（{scheme}:）：只接受 "
            f"{'/'.join(CREDENTIAL_REF_SCHEMES)}:引用名"
        )
    if scheme == "keychain" and "/" not in name:
        return None, f"keychain 引用要写成 keychain:<服务>/<账户>，当前是「{_brief(name)}」"
    return CredentialRef(key_name=field_name, scheme=scheme, name=name), ""


def resolve_credentials(
    refs: list[CredentialRef], *, environ: dict[str, str] | None = None
) -> tuple[dict[str, str], list[str]]:
    """按引用取值。

    env 从进程环境取（**不写回**任何文件）；keychain 在引擎进程里没有访问件 —— 这里如实
    报未接线，而不是去猜一个环境变量名当作「钥匙串已支持」。
    """
    env = os.environ if environ is None else environ
    values: dict[str, str] = {}
    problems: list[str] = []
    for ref in refs:
        if ref.scheme == "env":
            value = str(env.get(ref.name) or "")
            if not value:
                problems.append(
                    f"凭据引用 {ref.display} 未解析：环境变量 {ref.name} 未设置"
                    "（凭据不落配置文件，故只从环境变量取；请在本机环境里配置后重试）"
                )
                continue
            ref.value = value
            values[ref.key_name] = value
        elif ref.scheme == "keychain":
            problems.append(
                f"凭据引用 {ref.display} 未解析：钥匙串条目需由桌面壳下发到引擎，"
                "本车道还没有这条通道（未验收），不会退回读环境变量"
            )
        else:  # pragma: no cover  parse_credential_ref 已挡住
            problems.append(f"凭据字段「{ref.key_name}」引用形式未知（{ref.scheme}）")
    return values, problems


# ── 虚拟路径与后端前缀 ──────────────────────────────────────────


def normalize_virtual_path(raw: object) -> tuple[str, str]:
    """把用户/模型给的对象路径收成「后端前缀之下的相对路径」。

    与 D-45 同源的部分在 :func:`within_backend_prefix`（比较键直接走 ``sr._norm_path`` /
    ``sr._rule_hit``）；这里负责的是「远程后端根本没有本机绝对路径」这条额外事实：
    盘符、UNC、``~``、绝对路径和 ``..`` 一律不收 —— 收了就等于在对象存储前缀外开了个洞。

    返回 (相对路径, 拒绝原因)；原因非空时路径不可用。根目录返回空串（合法）。
    """
    text = str(raw or "").strip()
    if not text or text in (".", "./"):
        return "", ""
    if _CONTROL_CHARS.search(text):
        return "", "对象路径含控制字符，已按非法路径拒绝"
    if _WIN_DRIVE.match(text) or text.startswith("\\\\"):
        return "", (
            f"对象路径要写成后端前缀下的相对路径，当前是本机绝对路径写法（{_brief(text)}）；"
            "远程工作区不承载本机盘符与 UNC 路径"
        )
    if text.startswith("/") or text.startswith("~"):
        return "", (
            f"对象路径不许以 / 或 ~ 开头（{_brief(text)}）：后端根前缀已经定好了落点，"
            "这里只接受前缀之下的相对路径"
        )
    parts: list[str] = []
    for seg in text.replace("\\", "/").split("/"):
        seg = seg.strip()
        if seg in ("", "."):
            continue
        if seg == "..":
            return "", (
                f"对象路径含 ..（{_brief(text)}）：后端不在自身前缀之外做 traversal，"
                "越出前缀请改后端的根前缀配置"
            )
        if len(seg) > 255:
            return "", f"对象路径单段超过 255 字符（{_brief(seg, 60)}…）"
        parts.append(seg)
    return "/".join(parts), ""


def within_backend_prefix(prefix: str, vpath: str) -> bool:
    """后端前缀命中判定：**直接复用 D-45 的比较键与分段前缀口径**（含 ``/**`` 与大小写规则）"""
    if not str(prefix or "").strip():
        return True
    return sr._rule_hit(prefix, vpath)


def backend_host_root(spec: BackendSpec) -> Path | None:
    """本机落地后端（local / docker 的绑定目录）的根；纯远程后端回 None。

    docker 的根是**本机侧**的绑定源目录：容器里看是 ``/workspace``，
    但越界判定发生在把它映射回本机路径之后，所以这里取源目录而不是容器路径。
    """
    if spec.kind not in HOST_BACKED_KINDS:
        return None
    root = str(spec.options.get("dir") or "").strip()
    return Path(root).expanduser().resolve() if root else None


def _strip_backend_prefix(key: str, backend_prefix: str) -> str:
    """把存储侧的完整落点还原成调用方用的相对路径（与 :func:`storage_key` 互逆）"""
    text = str(key or "")
    base = str(backend_prefix or "").strip("/")
    if base and text.startswith(base + "/"):
        return text[len(base) + 1 :]
    return text


def storage_key(spec: BackendSpec, vpath: str) -> str:
    """对象路径 → 后端内的落点键（对象存储的 key / 表后端的 obj_path）。

    ``prefix`` 是**工作区根在远端的落点**，vpath 是相对它的路径 —— 与本地后端的
    ``options.dir + 相对路径``、docker 的 ``绑定目录 + 相对路径`` 同一个口径。
    换后端不换路径这句话才成立；把 prefix 再拼一遍或干脆不拼都是错的。
    """
    base = spec.prefix.strip("/")
    return "/".join(x for x in (base, vpath) if x)


# ── 资源与网络限制 ──────────────────────────────────────────────


@dataclass
class Limits:
    """后端资源与网络限制。

    字段值的**强制点**在 kind 上：docker 那几个真的进 ``docker run`` 命令行；
    数据库与对象存储那两个只能声明，本机连接件强制不了对端配额（见 :meth:`enforce_notes`）。
    """

    memory_mb: int = 512
    cpus: float = 1.0
    pids: int = 128
    network: str = "none"
    timeout_secs: int = 120
    max_write_bytes: int = 2_000_000
    max_list_items: int = 200
    notes: list[str] = field(default_factory=list)

    def enforce_notes(self, kind: str) -> list[str]:
        """如实说明每个重数在这一档后端上到底有没有被强制"""
        if kind == "docker":
            return [
                f"内存上限 {self.memory_mb}MB" + ("（0 = 未设限）" if not self.memory_mb else ""),
                f"CPU 上限 {self.cpus}" + ("（0 = 未设限）" if not self.cpus else ""),
                f"进程数上限 {self.pids}",
                f"容器网络 {self.network}",
                f"单次执行上限 {self.timeout_secs}s（由本层 kill 计时，非 cgroup 强制）",
            ]
        if kind == "local":
            return [
                "本地目录后端不承载算力，无内存/CPU 上限可施；单次写入字节上限 "
                f"{self.max_write_bytes} 由本层强制",
                "本地目录后端不施加网络限制（它不起网络）",
            ]
        if kind == "postgresql":
            return [
                f"单次语句超时 {self.timeout_secs}s 以 SET LOCAL statement_timeout 下发；"
                "若连接方是只读角色或服务端已有更严的设置，实际以服务端为准",
                f"单次返回行数上限 {self.max_list_items}（本层加 LIMIT）",
                "角色配额、连接数与存储配额属数据库服务端，本层无法强制 ⇒ 未验收",
            ]
        return [
            f"单次写入字节上限 {self.max_write_bytes}（本层强制，超限拒绝而不是截断）",
            f"单次列表返回上限 {self.max_list_items} 条（本层加限制参数，超出如实报截断）",
            "对象存储/远程主机的带宽与 QPS 配额属对端策略，本层无法强制 ⇒ 未验收",
        ]


def parse_limits(raw: Any) -> tuple[Limits, list[str]]:
    """非法值一律回落到**更严**的那一侧并说明，绝不因为「没填对」而放开限制"""
    limits = Limits()
    problems: list[str] = []
    if raw is None:
        return limits, problems
    if not isinstance(raw, dict):
        return limits, ["工作区后端的 limits 段不是对象，已按默认（最保守）限制执行"]
    memory = raw.get("memoryMb", raw.get("memory_mb"))
    if memory is not None:
        try:
            value = int(str(memory).strip())
        except (TypeError, ValueError):
            value = -1
        if value < 0 or value > 262_144:
            problems.append(f"内存上限「{_brief(memory)}」非法（允许 0–262144 MB，0 = 未设限），已回落 {limits.memory_mb}MB")
        else:
            limits.memory_mb = value
    cpus = raw.get("cpus")
    if cpus is not None:
        try:
            value = float(str(cpus).strip())
        except (TypeError, ValueError):
            value = -1.0
        if value == 0:
            limits.cpus = 0.0
        elif 0.1 <= value <= 32.0:
            limits.cpus = round(value, 2)
        else:
            problems.append(f"CPU 上限「{_brief(cpus)}」非法（允许 0.1–32，0 = 未设限），已回落 {limits.cpus}")
    pids = raw.get("pids", raw.get("pidsLimit"))
    if pids is not None:
        try:
            value = int(str(pids).strip())
        except (TypeError, ValueError):
            value = -1
        if 1 <= value <= 4096:
            limits.pids = value
        else:
            problems.append(f"进程数上限「{_brief(pids)}」非法（允许 1–4096），已回落 {limits.pids}")
    network = str(raw.get("network") or "").strip().lower()
    if network:
        if network not in NETWORK_MODES:
            problems.append(f"网络模式「{_brief(network)}」未知（允许 none/bridge/host），已回落 {limits.network}")
        elif network == "host":
            limits.network = "host"
            limits.notes.append(
                "网络模式选了 host：容器与本机同网络栈，沙箱的网络边界自此不成立，"
                "请确认这是显式选择"
            )
        else:
            limits.network = network
    timeout = raw.get("timeoutSecs", raw.get("timeout_secs"))
    if timeout is not None:
        try:
            value = int(str(timeout).strip())
        except (TypeError, ValueError):
            value = -1
        if 1 <= value <= 600:
            limits.timeout_secs = value
        else:
            problems.append(f"执行超时「{_brief(timeout)}」非法（允许 1–600 秒），已回落 {limits.timeout_secs}")
    write_bytes = raw.get("maxWriteBytes", raw.get("max_write_bytes"))
    if write_bytes is not None:
        try:
            value = int(str(write_bytes).strip())
        except (TypeError, ValueError):
            value = -1
        if 1 <= value <= 64_000_000:
            limits.max_write_bytes = value
        else:
            problems.append(
                f"单次写入字节上限「{_brief(write_bytes)}」非法（允许 1–64000000），已回落 {limits.max_write_bytes}"
            )
    items = raw.get("maxListItems", raw.get("max_list_items"))
    if items is not None:
        try:
            value = int(str(items).strip())
        except (TypeError, ValueError):
            value = -1
        if 1 <= value <= 2000:
            limits.max_list_items = value
        else:
            problems.append(f"单次列表条数上限「{_brief(items)}」非法（允许 1–2000），已回落 {limits.max_list_items}")
    return limits, problems


# ── 后端配置 ────────────────────────────────────────────────────


@dataclass
class BackendSpec:
    """一个工作区后端的配置事实（不含任何凭据原值）"""

    backend_id: str = ""
    kind: str = "local"
    root_display: str = ""
    prefix: str = ""
    read_only: bool = False
    limits: Limits = field(default_factory=Limits)
    credentials: list[CredentialRef] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    configured: bool = False

    def capabilities(self) -> frozenset[str]:
        return KIND_CAPABILITIES.get(self.kind, frozenset())

    def describe(self) -> dict[str, Any]:
        """给界面/审计/IPC 的形状：**永远没有凭据原值**"""
        return _jsonable(
            {
                "backendId": self.backend_id,
                "kind": self.kind,
                "kindLabel": KIND_LABELS.get(self.kind, self.kind),
                "rootDisplay": self.root_display,
                "prefix": self.prefix,
                "readOnly": self.read_only,
                "hostBacked": self.kind in HOST_BACKED_KINDS,
                "capabilities": sorted(self.capabilities()),
                "limits": {
                    "memoryMb": self.limits.memory_mb,
                    "cpus": self.limits.cpus,
                    "pids": self.limits.pids,
                    "network": self.limits.network,
                    "timeoutSecs": self.limits.timeout_secs,
                    "maxWriteBytes": self.limits.max_write_bytes,
                    "maxListItems": self.limits.max_list_items,
                    "enforced": self.limits.enforce_notes(self.kind),
                },
                "credentials": [ref.redact() for ref in self.credentials],
                "options": {
                    k: (_fingerprint(v) if k in _CREDENTIAL_KEYS else v)
                    for k, v in self.options.items()
                },
                "errors": list(self.errors),
                "warnings": list(self.warnings),
                "configured": self.configured,
            }
        )


def parse_spec(raw: Any) -> BackendSpec:
    """runtime.workspaceBackend 段 → BackendSpec；任何非法字段都进 errors，
    带 errors 的规格在 :func:`build_backend` 处 fail-closed。"""
    if not isinstance(raw, dict):
        return BackendSpec(
            configured=True,
            errors=[f"工作区后端配置段格式非法（{type(raw).__name__}），已按 fail-closed 处理"],
        )
    kind = str(raw.get("kind") or "").strip().lower()
    backend_id = str(raw.get("backendId") or raw.get("backend_id") or "").strip()
    if not backend_id:
        backend_id = f"{kind or 'default'}-default"
    spec = BackendSpec(kind=kind or "local", backend_id=backend_id, configured=True)
    if not _BACKEND_ID.match(backend_id):
        spec.errors.append(
            f"后端 id「{_brief(backend_id)}」非法：只允许字母数字与 _ . -，且不以分隔符开头（它要进目录名与容器名）"
        )
    if kind not in BACKEND_KINDS:
        spec.errors.append(
            f"工作区后端类型「{_brief(kind)}」不支持：本版本认识 {'/'.join(BACKEND_KINDS)}"
        )
    # options 先解析：prefix、根展示名、以及下面各 kind 的校验都要读它。
    # 顺序颠倒过一次，症状是「options.prefix 永远读不到」——配置面两种写法都得认。
    options = raw.get("options")
    if isinstance(options, dict):
        spec.options = dict(options)
    elif options is not None:
        spec.errors.append(
            f"工作区后端 options 段格式非法（{type(options).__name__}），已按 fail-closed 处理"
        )

    prefix, prefix_err = normalize_virtual_path(
        str(raw.get("prefix") or spec.options.get("prefix") or "").strip()
    )
    if prefix_err:
        spec.errors.append(f"后端根前缀非法：{prefix_err}")
    spec.prefix = prefix
    spec.read_only = bool(raw.get("readOnly") or raw.get("read_only"))
    root = str(raw.get("root") or raw.get("rootDisplay") or "").strip()
    spec.limits, limit_problems = parse_limits(raw.get("limits"))
    spec.errors.extend(limit_problems)
    spec.warnings.extend(spec.limits.notes)

    for key, value in list(spec.options.items()):
        if key in _CREDENTIAL_KEYS:
            ref, err = parse_credential_ref(key, value)
            if err:
                spec.errors.append(err)
                spec.options.pop(key, None)
            elif ref is not None:
                spec.credentials.append(ref)
                spec.options.pop(key, None)
    # 顶层直接写凭据键也要抓：配置面比 options 更容易被手改
    for key in _CREDENTIAL_KEYS:
        if key in raw:
            ref, err = parse_credential_ref(key, raw.get(key))
            if err:
                spec.errors.append(err)
            elif ref is not None:
                spec.credentials.append(ref)

    if kind in HOST_BACKED_KINDS:
        # 本机落地后端（本地目录、容器绑定目录的源侧）没有 dir 就没有落点。
        # 这一条必须留在 parse_spec：设置页拿 parse_spec 的结果就能指出缺哪项，
        # 而不是等 build_backend / 构造器才补一句 —— 那句还老把 docker 说成「本地后端」。
        label = KIND_LABELS.get(kind, kind)
        if not str(spec.options.get("dir") or "").strip():
            spec.errors.append(
                f"{label} 后端缺少 options.dir（本机工作目录的绝对路径），无法定位落点"
            )
    if kind == "docker":
        image = str(spec.options.get("image") or "").strip()
        if not image:
            # 缺项与非法项是两句话：设置页据此决定「补一个镜像名」还是「改个写法」，
            # 合成一句「镜像名非法」会让人以为填了个坏名字，其实是一个都没填。
            spec.errors.append("Docker 后端缺少 options.image（容器镜像，形如 python:3.12-slim）")
        elif not _IMAGE.match(image) or ".." in image:
            spec.errors.append(
                f"Docker 后端的镜像名「{_brief(image)}」非法：只允许合法仓库/标签字符，且不许 .."
            )
    if kind == "postgresql":
        host = str(spec.options.get("host") or "").strip()
        dbname = str(spec.options.get("dbname") or "").strip()
        if not host and not dbname:
            # 两个都不写时 libpq 会连到「进程环境里的那个默认目标」——
            # 配置面上看不出来，落库却可能落在另一个库；这比报错更糟，所以按非法处理。
            spec.errors.append(
                "PostgreSQL 后端至少要写 options.host 或 options.dbname：两个都不写时连接件"
                "会按 libpq 默认目标连（本机套接字 + 同名用户库），等于连到一个配置里没写过的库"
            )
        elif not host:
            spec.warnings.append(
                "PostgreSQL 后端未写 options.host：将按 libpq 默认走本机套接字/默认库；"
                "跨机部署请显式写 host（真库形态未验收）"
            )
        for opt in ("schema", "table"):
            value = str(spec.options.get(opt) or "").strip()
            target = value or {"schema": "public", "table": "tt_workspace_object"}[opt]
            if not _IDENT.match(target):
                spec.errors.append(
                    f"PostgreSQL 后端的 {opt} 名「{_brief(value)}」非法：只允许小写字母、数字与下划线，"
                    "且不以数字开头（它要拼进 SQL 标识符，不做转义兜底）"
                )
    if kind in ("s3", "cos"):
        # 对象存储的必填项在配置面就能判，不该等 build_backend 才炸——
        # 设置页要拿 parse_spec 的结果直接把话讲清楚。
        label = KIND_LABELS.get(kind, kind)
        bucket = str(spec.options.get("bucket") or "").strip()
        if not bucket:
            spec.errors.append(f"{label} 后端缺少 options.bucket")
        elif not _BUCKET.match(bucket):
            spec.errors.append(
                f"桶名「{_brief(bucket)}」非法：只允许小写字母、数字、连字符与点，"
                f"长度 3–63（它要进 URL 与签名，不做转义兜底）"
            )
        endpoint = str(spec.options.get("endpoint") or "").strip()
        if endpoint and urllib.parse.urlsplit(endpoint).scheme != "https":
            spec.errors.append(
                f"对象存储端点「{_brief(endpoint)}」不是 HTTPS：凭据与正文不许走明文 HTTP 出网"
            )
        if kind == "cos" and not endpoint:
            spec.errors.append(
                "腾讯云 COS 后端缺少 options.endpoint（形如 https://cos.<region>.myqcloud.com），"
                "本车道不猜厂商域名"
            )
    if kind == "ssh":
        host = str(spec.options.get("host") or "").strip()
        if not host:
            spec.errors.append("SSH 后端缺少 options.host")
        elif not _SSH_HOST.match(host):
            spec.errors.append(
                f"远程主机名「{_brief(host)}」非法：只允许主机名字符与端口分隔冒号，"
                "不许空格与 shell 元字符（它要拼进 ssh 的 argv）"
            )
        port = str(spec.options.get("port") or "22").strip()
        if not port.isdigit() or not 1 <= int(port) <= 65535:
            spec.errors.append(f"SSH 端口「{_brief(port)}」非法：只接受 1–65535 的数字")
        if any(ref.key_name == "password" for ref in spec.credentials):
            spec.errors.append(
                "SSH 后端不接受密码凭据：只支持密钥认证（keyPath 按 env: 引用），"
                "密码进配置或进 argv 都是外泄面"
            )
        if str(spec.options.get("strictHostKeyChecking") or "").strip().lower() in ("no", "false", "0"):
            spec.errors.append(
                "SSH 后端不许关闭主机密钥校验（strictHostKeyChecking=no）：那等于把中间人风险换便利"
            )
        if not any(ref.key_name == "keyPath" for ref in spec.credentials):
            spec.warnings.append(
                "SSH 后端未配置 keyPath（env: 引用）：将依赖远程主机的默认密钥与 ssh-agent，"
                "真主机未验收"
            )
    if spec.errors:
        spec.warnings.append(
            "本后端配置有非法项，所有操作都会先被拒绝（fail-closed）；修好配置再切，不带病跑"
        )
    spec.root_display = root or _default_root_display(spec)
    return spec


def _default_root_display(spec: BackendSpec) -> str:
    if spec.kind == "local":
        return "（未指定本地目录）"
    if spec.kind == "docker":
        return "/workspace（容器内绑定目录）"
    if spec.kind == "postgresql":
        schema = str(spec.options.get("schema") or "public")
        table = str(spec.options.get("table") or "tt_workspace_object")
        return f"{schema}.{table}"
    if spec.prefix:
        return spec.prefix
    return {
        "s3": "s3 桶（未指定前缀）",
        "cos": "COS 存储桶（未指定前缀）",
        "ssh": "远程主机家目录",
    }.get(spec.kind, f"{spec.kind}（未指定根）")


def spec_from_runtime(runtime: Any) -> BackendSpec | None:
    """从一轮请求的 runtime 里取后端选择（``workspaceBackends`` 清单 + ``activeBackendId``）。

    没下发 ⇒ 返回 None，调用方按「本机既有形态」跑：远程后端是**新增**能力，
    不能因为它不在就把原来的本地工作区改成 fail-closed。
    """
    bundle = runtime if isinstance(runtime, dict) else {}
    block = bundle.get("workspaceBackend", bundle.get("workspace_backends"))
    if isinstance(block, str):
        try:
            block = json.loads(block)
        except Exception:  # noqa: BLE001
            return BackendSpec(
                configured=True,
                errors=[f"工作区后端配置不是合法 JSON：{_brief(block)}"],
            )
    if block is None:
        return None
    if isinstance(block, list):
        block = {"backends": block}
    if not isinstance(block, dict):
        return BackendSpec(configured=True, errors=["工作区后端配置段格式非法（不是对象或数组）"])
    backends = block.get("backends")
    active = str(block.get("activeBackendId") or block.get("active") or "").strip()
    if isinstance(backends, list) and backends:
        chosen = None
        for item in backends:
            if isinstance(item, dict) and str(item.get("backendId") or item.get("backend_id") or "") == active:
                chosen = item
                break
        if chosen is None:
            chosen = backends[0] if not active else None
            if chosen is None:
                return BackendSpec(
                    configured=True,
                    errors=[f"要用的后端 id「{_brief(active)}」没在清单里（清单有 {len(backends)} 条）"],
                )
        spec = parse_spec(chosen)
        return spec
    return parse_spec(block)


# ── 三段判定 ────────────────────────────────────────────────────


@dataclass
class AccessDecision:
    """一次路径判定的结论。``source`` 决定界面选哪句文案，``code`` 决定审计怎么归因。"""

    allowed: bool = True
    reason: str = ""
    source: str = ""
    code: str = ""
    rule: str = ""
    vpath: str = ""

    def error(self) -> dict[str, Any]:
        return _jsonable(
            {
                "ok": False,
                "code": self.code or CODE_BACKEND_READONLY,
                "message": self.reason,
                "deniedBy": self.source,
                "rule": self.rule,
                "path": self.vpath,
            }
        )


def decide_access(
    spec: BackendSpec,
    vpath: str,
    *,
    policy: sr.SandboxPolicy | None = None,
    host_path: Path | None = None,
    write: bool = False,
    delete: bool = False,
) -> AccessDecision:
    """三段判定：**后端边界 → 沙箱 → 档位**，三段各有各的句子，不共用文案。

    - 本机落地（``host_path`` 给出，且 kind ∈ HOST_BACKED_KINDS）的沙箱与档位判定**整条**
      交给 ``policy.file_denial()``，文案与既有文件工具逐字一致，不在这里重述；
    - 纯远程（对象存储 / 数据库 / SSH）没有本机路径可比，档位段用自己的句子，
      并且**明确说清「切到完全访问」放开的是远程写这一层，管不到后端根前缀与只读配置**
      —— 那两条是后端设置，不是档位。
    """
    def deny(code: str, reason: str, source: str, *, rule: str = "") -> AccessDecision:
        return AccessDecision(
            allowed=False, reason=reason, source=source, code=code, rule=rule, vpath=vpath
        )

    action_word = "删除" if delete else "写入"

    # 0) 配置本身有问题 ⇒ fail-closed，不做任何「看起来还能用」的降级
    if spec.errors:
        return deny(
            CODE_SPEC_INVALID,
            "工作区后端配置未通过校验，本车道按 fail-closed 拒绝该操作：" + "；".join(spec.errors),
            SOURCE_BACKEND,
        )

    # 1) 后端边界：这一条判的是「路径还在不在这个后端的根里」。
    #
    # ``prefix`` 是工作区根在远端的**落点**（对象存储前缀 / 表前缀 / 远程目录），
    # 操作面给的 vpath 一律是相对它的路径 —— 与本地后端的 ``options.dir`` + 相对路径同口径，
    # 所以「换后端不换路径」成立。越界有两种真实形态：
    #   · 相对路径里混进 ``..``、绝对路径、盘符或 UNC（判定层自查，不只依赖 :func:`normalize_virtual_path`
    #     那一遍收口 —— decide() 也能被直接调，不能假定调用方收过）；
    #   · 本机落地后端的映射结果跑到绑定目录之外（子类映射漂移时才会出现，兜底用）。
    # 这是配置事实，跟这一轮选哪档无关 ⇒ 排在沙箱与档位之前，且句子与档位句、沙箱句各不相同。
    cleaned, escape_err = normalize_virtual_path(vpath)
    if escape_err or cleaned != vpath:
        return deny(
            CODE_PATH_OUTSIDE_BACKEND,
            (
                f"路径「{_brief(vpath)}」越出工作区后端「{spec.backend_id}」"
                f"（{KIND_LABELS.get(spec.kind, spec.kind)}）的根「{spec.prefix or '（整个后端根）'}」，"
                f"后端不承载根之外的对象；这一条与文件访问档位、工作区沙箱白名单都无关 —— "
                f"在对话里切档位改不了它，要换范围请改后端的根前缀配置。"
            ),
            SOURCE_BACKEND_BOUNDARY,
        )
    # 2) 沙箱段
    if policy is not None:
        if policy.error:
            return deny(
                CODE_SPEC_INVALID,
                f"沙箱策略读取失败，已按 fail-closed 拒绝工作区访问：{policy.error}",
                SOURCE_SANDBOX,
            )
        if (write or delete) and policy.execution_level == "L0":
            # 与 sandbox_runtime 同句：L0 是沙箱等级，不是文件档位，别混进档位文案
            return deny(
                CODE_CAPABILITY_DENIED,
                "沙箱为 L0 只读，不允许写入文件",
                SOURCE_SANDBOX,
            )
        for rule in policy.file_blocklist:
            if sr._rule_hit(rule, vpath):
                return deny(
                    CODE_CAPABILITY_DENIED,
                    f"路径命中沙箱文件黑名单（{rule}）",
                    SOURCE_SANDBOX,
                    rule=rule,
                )

    # 3) 后端只读（配置事实，先于档位：只读后端连「完全访问」也写不了）
    if (write or delete) and spec.read_only:
        return deny(
            CODE_BACKEND_READONLY,
            (
                f"工作区后端「{spec.backend_id}」配置为只读，不允许"
                f"{'删除' if delete else '写入'}；要写请先在设置里把该后端切成可写。"
            ),
            SOURCE_BACKEND,
        )

    # 4) 本机落地后端：档位与沙箱整条交回 D-45 的原判定，句子不改写。
    #    D-45 说完才轮到后端自己的边界兜底（子类映射漂移时才会走到这条）。
    if host_path is not None and spec.kind in HOST_BACKED_KINDS and policy is not None:
        denial = policy.file_denial(host_path, write=write, delete=delete, base=policy.work_dir)
        if denial is not None:
            reason, sub_source, rule = denial
            return AccessDecision(
                allowed=False,
                reason=reason,
                source=SOURCE_SANDBOX if sub_source in ("file_blocklist", "file_allowlist", "execution_level", "policy_error") else SOURCE_FILE_PERMISSION,
                code=rule or sub_source,
                rule=rule,
                vpath=vpath,
            )
        host_root = backend_host_root(spec)
        if host_root is not None and not sr._rule_hit(
            sr._norm_path(host_root), sr._norm_path(host_path)
        ):
            return deny(
                CODE_PATH_OUTSIDE_BACKEND,
                (
                    f"路径「{_brief(vpath)}」在本机落到「{_brief(host_path)}」，越出后端"
                    f"「{spec.backend_id}」的绑定目录「{_brief(host_root)}」之外；"
                    "这一条与文件访问档位、工作区沙箱白名单都无关 —— 切档位改不了它，"
                    "要换范围请改后端的根目录配置。"
                ),
                SOURCE_BACKEND_BOUNDARY,
            )
        return AccessDecision(allowed=True, vpath=vpath)

    mode = (policy.file_permission if policy is not None else sr.DEFAULT_FILE_PERMISSION)
    label = sr.FILE_PERMISSION_LABELS.get(mode, mode)
    # 删除先判：删除比写入多一层要求（必须完全访问 + 后端显式开启），
    # 顺序反了会把「未开启远程删除」说成通用的档位句，两件事就混成一句了。
    if delete:
        if mode != "full_access":
            return deny(
                CODE_DELETE_NOT_FULL_ACCESS,
                (
                    f"当前文件访问档位是「{label}」，删除操作需要切到「完全访问」；"
                    f"远程后端「{spec.backend_id}」上的对象未被删除：{vpath}"
                ),
                SOURCE_FILE_PERMISSION,
            )
        if not spec.options.get("allowRemoteDelete"):
            return deny(
                CODE_DELETE_DENIED,
                (
                    f"后端「{spec.backend_id}」（{KIND_LABELS.get(spec.kind, spec.kind)}）未开启远程删除"
                    f"（options.allowRemoteDelete）：文件访问档位到「完全访问」也不会替你打开这一层，"
                    "远程删除不可撤销且没有本机回收站兜底，请在后端配置里显式开启。被拒路径："
                    f"{vpath}"
                ),
                SOURCE_BACKEND,
            )
    if write and mode != "full_access":
        return deny(
            CODE_TIER_DENIES_REMOTE,
            (
                f"当前文件访问档位是「{label}」，这一档的写盘范围只覆盖绑定工作目录与工作区文件白名单；"
                f"工作区后端「{spec.backend_id}」（{KIND_LABELS.get(spec.kind, spec.kind)}）的对象路径不在本机工作目录里，"
                f"所以本次{action_word}未执行。要放开这一层，把档位切到「完全访问」；"
                f"但请注意完全访问管不到后端的根前缀与只读配置 —— 那两条是后端设置，改不了档位。被拒路径：{vpath}"
            ),
            SOURCE_FILE_PERMISSION,
        )
    return AccessDecision(allowed=True, vpath=vpath)


# ── 远程操作审计 ────────────────────────────────────────────────

AUDIT_FILE_NAME = "workspace-remote-audit.jsonl"


def audit_dir() -> Path:
    if dp is None:  # pragma: no cover  取不到数据根时退到进程临时目录，不静默丢审计
        import tempfile

        return Path(tempfile.gettempdir()) / "tongtian-workspace-audit"
    return Path(dp.engine_home()) / "workspaces" / "_remote_audit"


def audit_file() -> Path:
    return audit_dir() / AUDIT_FILE_NAME


def append_audit(entry: dict[str, Any]) -> dict[str, Any]:
    """追加一条远程操作审计。

    永远不带凭据原值与被写内容本体（只带字节数与内容指纹）；写盘失败不静默：
    打日志 + 进内存缓冲，让 ``runtime_status()`` 能把「有几条只活在内存里」如实报出去。
    """
    safe = {k: v for k, v in _jsonable(entry).items() if v is not None}
    _MEMORY_AUDIT.append(safe)
    try:
        target = audit_file()
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(safe, ensure_ascii=False) + "\n")
        safe["persisted"] = True
    except Exception as exc:  # noqa: BLE001
        safe["persisted"] = False
        safe["persistError"] = _brief(exc)
        logger.warning("工作区远程审计落盘失败：%s", exc)
    return safe


_MEMORY_AUDIT: deque[dict[str, Any]] = deque(maxlen=500)


def read_audit(
    *, backend_id: str = "", kind: str = "", outcome: str = "", limit: int = 50, offset: int = 0
) -> dict[str, Any]:
    """倒序读审计（新→旧）；条数与偏移都夹住，越界给明确说法而不是空表。"""
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 50
    size = max(1, min(size, 500))
    try:
        start = int(offset)
    except (TypeError, ValueError):
        start = 0
    start = max(0, start)
    rows: list[dict[str, Any]] = []
    path = audit_file()
    if path.exists():
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:  # noqa: BLE001
            return _jsonable(
                {"ok": False, "code": CODE_INTERNAL, "message": f"审计文件读不出来：{_brief(exc)}", "items": []}
            )
        for line in reversed(lines):
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(item, dict):
                continue
            if backend_id and str(item.get("backendId") or "") != backend_id:
                continue
            if kind and str(item.get("kind") or "") != kind:
                continue
            if outcome and str(item.get("outcome") or "") != outcome:
                continue
            rows.append(item)
    page = rows[start : start + size]
    out: dict[str, Any] = {
        "ok": True,
        "items": page,
        "total": len(rows),
        "offset": start,
        "limit": size,
        "hasMore": start + size < len(rows),
        "auditFile": str(path),
        "inMemoryOnly": sum(1 for item in _MEMORY_AUDIT if not item.get("persisted")),
    }
    if not page and start:
        out["note"] = (
            f"偏移 {start} 之后没有审计条目了（这一页筛完共 {len(rows)} 条），"
            "要回头看最早几条请带 offset=0"
        )
    return _jsonable(out)


# ── 后端异常 ────────────────────────────────────────────────────


class BackendUnavailable(RuntimeError):
    """后端够不着（缺驱动、缺传输件、daemon 没起）——归因 error 而不是 denied"""

    def __init__(self, code: str, message: str, *, unverified: bool = False):
        super().__init__(message)
        self.code = code
        self.unverified = unverified


# ── 后端本体 ────────────────────────────────────────────────────


@dataclass
class OpContext:
    """一次操作的归属信息（谁在什么时候干的），审计按它归因"""

    task_id: str = ""
    principal: str = ""
    tool: str = ""


class WorkspaceBackend:
    """工作区后端基类：路径口径、三段判定与审计在这一层统一，子类只写「怎么够着东西」"""

    kind = ""

    def __init__(
        self,
        spec: BackendSpec,
        *,
        policy: sr.SandboxPolicy | None = None,
        audit: Callable[[dict[str, Any]], None] | None = None,
        environ: dict[str, str] | None = None,
    ):
        self.spec = spec
        self.policy = policy
        self._audit = audit or append_audit
        self._environ = environ

    def bind_policy(self, policy: sr.SandboxPolicy | None) -> None:
        """换一轮对话时把新的 ``SandboxPolicy`` 递进来（档位与沙箱都跟着这一轮走）。

        没有下发沙箱段也不能当成「没有限制」：按 D-45 的同一口径合成一份默认策略
        （档位缺省即最保守的「每次询问」，工作目录取本后端的落点），
        这样「壳没配沙箱」时远程写盘照样受档位约束，与本机文件工具的行为一致。
        """
        if policy is not None:
            self.policy = policy
            return
        if self.kind in HOST_BACKED_KINDS:
            self.policy = sr.SandboxPolicy.from_runtime(None, work_dir=getattr(self, "root", None))
        else:
            self.policy = sr.SandboxPolicy.from_runtime(None)

    # ── 能力与自述 ────────────────────────────────────────────

    def has(self, capability: str) -> bool:
        return capability in self.spec.capabilities()

    def describe(self) -> dict[str, Any]:
        return self.spec.describe()

    def probe(self) -> dict[str, Any]:
        """后端可用性。子类要调 :meth:`_base_probe` 再补自己的现场观察与未验收清单。"""
        return self._base_probe()

    def _base_probe(self, *, extra: dict[str, Any] | None = None) -> dict[str, Any]:
        out: dict[str, Any] = {
            "ok": not self.spec.errors,
            "backendId": self.spec.backend_id,
            "kind": self.kind,
            "kindLabel": KIND_LABELS.get(self.kind, self.kind),
            "configured": self.spec.configured,
            "errors": list(self.spec.errors),
            "warnings": list(self.spec.warnings) + list(self.spec.limits.notes),
            "enforcedLimits": self.spec.limits.enforce_notes(self.kind),
            "credentials": [ref.redact() for ref in self.spec.credentials],
            "status": STATUS_VERIFIED if not self.spec.errors else STATUS_UNVERIFIED,
            "unverified": [],
        }
        if self.spec.errors:
            out["message"] = "配置未通过校验，本后端所有操作都会被拒（fail-closed）"
        else:
            out["message"] = f"配置已解析：{KIND_LABELS.get(self.kind, self.kind)} @ {self.spec.root_display}"
        if extra:
            out.update(extra)
        # 「本机已验」只有在既没配置错、又没有任何未验收项时才成立。
        # 只看 errors 会把「配置合法但真机/真桶没跑过」的后端报成已验，
        # 台账口径就变成自测绿等于实机绿 —— 这是不允许的。
        if out["errors"] or out.get("unverified"):
            out["status"] = STATUS_UNVERIFIED
        return _jsonable(out)

    # ── 路径与判定 ────────────────────────────────────────────

    def resolve_path(
        self, raw: object, *, write: bool = False, delete: bool = False
    ) -> tuple[str, AccessDecision | None]:
        """收成相对对象路径并做三段判定；返回 (vpath, None) 或 ("", decision)

        ``write`` / ``delete`` 必须一路带到 :meth:`decide`：判定不看操作方向的话，
        只读后端照样能写、缺省档位照样能删——这跟 D-45 的档位语义正好相反。
        """
        vpath, err = normalize_virtual_path(raw)
        if err:
            return "", AccessDecision(
                allowed=False,
                reason=err,
                source=SOURCE_BACKEND,
                code=CODE_PATH_UNSAFE,
                vpath=_brief(str(raw or "")),
            )
        decision = self.decide(vpath, write=write, delete=delete, host_path=self.host_path_for(vpath))
        return vpath, decision

    def decide(
        self, vpath: str, *, write: bool = False, delete: bool = False, host_path: Path | None = None
    ) -> AccessDecision:
        return decide_access(
            self.spec, vpath, policy=self.policy, host_path=host_path, write=write, delete=delete
        )

    def host_path_for(self, vpath: str) -> Path | None:
        """本机落地后端把对象路径映射成真实路径；纯远程后端回 None"""
        return None

    def full_display(self, vpath: str) -> str:
        base = self.spec.prefix.strip("/")
        joined = "/".join(x for x in (base, vpath) if x)
        return f"{self.spec.root_display.rstrip('/').rstrip('.')}/{joined}" if joined else self.spec.root_display

    # ── 操作漏斗：判定 → 执行 → 审计 ──────────────────────────

    def _op(
        self,
        op: str,
        raw_path: object,
        run: Callable[[str], dict[str, Any]],
        *,
        write: bool = False,
        delete: bool = False,
        ctx: OpContext | None = None,
        detail: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        ctx = ctx or OpContext()
        vpath, decision = self.resolve_path(raw_path, write=write, delete=delete)
        if decision is not None and not decision.allowed:
            self._emit(op, vpath, "denied", decision, ctx, detail)
            return decision.error()
        try:
            result = run(vpath)
        except BackendUnavailable as exc:
            payload = {
                "ok": False,
                "code": exc.code,
                "message": str(exc),
                "unverified": exc.unverified,
                "path": vpath,
            }
            self._emit(op, vpath, "error", AccessDecision(reason=str(exc), code=exc.code), ctx, detail)
            return _jsonable(payload)
        except Exception as exc:  # noqa: BLE001  子类实现炸了也别把「不知道成没成」说成成功
            payload = {
                "ok": False,
                "code": CODE_INTERNAL,
                "message": f"后端操作失败，且无法确认远端是否已变更：{_brief(exc)}",
                "path": vpath,
            }
            self._emit(op, vpath, "error", AccessDecision(reason=str(exc), code=CODE_INTERNAL), ctx, detail)
            return _jsonable(payload)
        outcome = "ok" if result.get("ok", True) else "error"
        note = dict(detail or {})
        # 审计要能回答「这次落了多少、落成了什么」，又不许带正文本体：
        # 字节数与指纹从操作结果里补，各后端不必各自记得在 detail 里重复一遍。
        if isinstance(result.get("bytes"), int):
            note.setdefault("contentBytes", result["bytes"])
        if result.get("contentFingerprint"):
            note.setdefault("contentFingerprint", result["contentFingerprint"])
        for passthrough in ("key", "objPath", "remotePath", "trashPath", "etag", "returnCode", "from"):
            if result.get(passthrough) not in (None, ""):
                note.setdefault(passthrough, result[passthrough])
        self._emit(op, vpath, outcome, AccessDecision(code=str(result.get("code") or "")), ctx, note)
        result.setdefault("path", vpath)
        result.setdefault("backendId", self.spec.backend_id)
        return _jsonable(result)

    def _emit(
        self,
        op: str,
        vpath: str,
        outcome: str,
        decision: AccessDecision,
        ctx: OpContext,
        detail: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "ts": _now_iso(),
            "epochMs": int(time.time() * 1000),
            "backendId": self.spec.backend_id,
            "kind": self.kind,
            "op": op,
            "path": vpath,
            "display": self.full_display(vpath),
            "outcome": outcome,
            "deniedBy": decision.source or "",
            "code": decision.code or "",
            "reason": _brief(decision.reason, 300) if decision.reason else "",
            "taskId": ctx.task_id or "",
            "principal": ctx.principal or "",
            "tool": ctx.tool or "",
            "readOnlyBackend": self.spec.read_only,
        }
        if detail:
            for key, value in detail.items():
                if key in _CREDENTIAL_KEYS:
                    entry[key] = {"fingerprint": _fingerprint(value)}
                elif key == "content":
                    entry[f"{key}Bytes"] = _utf8_len(value)
                    entry[f"{key}Fingerprint"] = _fingerprint(value)
                else:
                    entry[key] = value
        try:
            self._audit(entry)
        except Exception as exc:  # noqa: BLE001  审计挂了不能改变操作结论，但必须留痕
            logger.warning("工作区审计写入异常（op=%s）：%s", op, exc)

    # ── 子类要实现的五个动作 ──────────────────────────────────

    def list_dir(self, raw_path: object = "", ctx: OpContext | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def read(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def write(self, raw_path: object, content: str, ctx: OpContext | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def delete(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        raise NotImplementedError

    def move(self, src: object, dst: object, ctx: OpContext | None = None) -> dict[str, Any]:
        ctx = ctx or OpContext()
        v_src, d_src = self.resolve_path(src)
        # 目标侧按「要写」预判：只读后端应在动任何内容之前就把话说死，
        # 而不是先把源读出来、写到一半才被 write 的判定拦下。
        v_dst, d_dst = self.resolve_path(dst, write=True)
        if d_src is not None and not d_src.allowed:
            self._emit("move", v_src, "denied", d_src, ctx, {"from": v_src})
            return d_src.error()
        if d_dst is not None and not d_dst.allowed:
            d_dst.vpath = v_dst
            self._emit("move", v_dst, "denied", d_dst, ctx, {"from": v_src})
            return d_dst.error()
        read = self.read(v_src, ctx)
        if not read.get("ok", False):
            return read
        made = self.write(v_dst, str(read.get("content") or ""), ctx)
        if not made.get("ok", False):
            return made
        return self._op(
            "move",
            v_dst,
            lambda _p: {
                "ok": True,
                "message": f"已从 {v_src} 复制为 {v_dst}",
                "movedFrom": v_src,
                "note": "本后端没有原子改名，move 实现为「复制 + 显式删源」；源仍在，删除要走 delete 判定",
            },
            ctx=ctx,
            detail={"from": v_src},
        )

    def exec_denial(self) -> AccessDecision | None:
        """命令执行按 ``run_shell`` 同一条沙箱规则判定（L0 只读、白名单、黑名单、策略读不出）。

        这里刻意不新造「远程执行」这第三种沙箱语义：能在远程起命令和本机起命令是同一家事，
        该被沙箱挡住就得被同一条规则挡住，措辞也沿用 ``sandbox_runtime`` 的原句。
        """
        if self.policy is None:
            self.bind_policy(None)
        denial = self.policy.tool_denial(EXEC_ASSESSMENT_TOOL)
        if denial is None:
            return None
        reason, sub_source, rule = denial
        return AccessDecision(
            allowed=False,
            reason=reason,
            source=SOURCE_SANDBOX,
            code=rule or sub_source,
            rule=rule,
        )

    def run(self, command: str, ctx: OpContext | None = None) -> dict[str, Any]:
        if not self.has(CAPABILITY_EXEC):
            return _jsonable(
                {
                    "ok": False,
                    "code": CODE_CAPABILITY_DENIED,
                    "message": (
                        f"工作区后端「{self.spec.backend_id}」（{KIND_LABELS.get(self.kind, self.kind)}）"
                        f"不承载命令执行；能在远程起命令的后端类型是 docker / ssh"
                    ),
                }
            )
        denial = self.exec_denial()
        if denial is not None:
            self._emit("exec", "", "denied", denial, ctx or OpContext())
            return denial.error()
        return self._exec(command, ctx or OpContext())

    def _exec(self, command: str, ctx: OpContext) -> dict[str, Any]:
        raise NotImplementedError

    def status_payload(self) -> dict[str, Any]:
        return _jsonable(
            {
                "backend": self.describe(),
                "probe": self.probe(),
            }
        )


# ── 本地目录后端 ────────────────────────────────────────────────


class LocalWorkspaceBackend(WorkspaceBackend):
    """本地目录后端：把既有「绑定工作目录」当成清单里的第一个后端，切换语义与远程一致。

    判定整条走 D-45：越界、硬拒清单、删除要「完全访问」这些句子与 ``view_text_file`` /
    ``write_text_file`` 看到的完全一样，本类不重述也不放宽。
    """

    kind = "local"

    def __init__(self, spec: BackendSpec, **deps: Any):
        super().__init__(spec, **deps)
        # options.dir 必填由 parse_spec 判（配置面就能看到）；构造器只负责解析落点。
        # 直接拿一个没 dir 的 spec 装配也不会有静默行为：读写都走 _require_root 明确报错。
        root = str(spec.options.get("dir") or "").strip()
        self.root: Path | None = Path(root).expanduser().resolve() if root else None

    def host_path_for(self, vpath: str) -> Path | None:
        if self.root is None:
            return None
        return (self.root / vpath) if vpath else self.root

    def _require_root(self) -> Path:
        if self.root is None or not self.root.exists():
            raise BackendUnavailable(
                CODE_BACKEND_UNAVAILABLE,
                "本地工作目录不存在或还没绑定（检查后端配置里的 options.dir）",
            )
        return self.root

    def probe(self) -> dict[str, Any]:
        extra: dict[str, Any] = {}
        if self.root is not None:
            extra["rootExists"] = self.root.exists()
            extra["rootIsDir"] = self.root.is_dir()
            if not extra["rootExists"]:
                extra["message"] = f"本地后端指向的目录不存在：{self.root}"
        return self._base_probe(extra=extra)

    def list_dir(self, raw_path: object = "", ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            root = self._require_root()
            target = self.host_path_for(vpath)
            assert target is not None
            if not target.exists():
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"目录不存在：{target}"}
            if not target.is_dir():
                stat = target.stat()
                return {
                    "ok": True,
                    "kind": "file",
                    "path": vpath,
                    "bytes": stat.st_size,
                    "message": "这是一个文件而不是目录，要看内容请用 read",
                }
            limit = self.spec.limits.max_list_items
            entries: list[dict[str, Any]] = []
            truncated = False
            for item in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
                if len(entries) >= limit:
                    truncated = True
                    break
                try:
                    stat = item.stat()
                except OSError:
                    continue
                entries.append(
                    {
                        "name": item.name,
                        "path": "/".join(x for x in (vpath, item.name) if x),
                        "isDir": item.is_dir(),
                        "bytes": 0 if item.is_dir() else stat.st_size,
                        "mtimeMs": int(stat.st_mtime * 1000),
                    }
                )
            out: dict[str, Any] = {
                "ok": True,
                "path": vpath,
                "items": entries,
                "count": len(entries),
                "root": str(root),
            }
            if truncated:
                out["truncated"] = True
                out["note"] = (
                    f"目录条目超过单次上限 {limit} 条，本页只给了前 {limit} 条；"
                    f"要更多请调后端 limits.maxListItems，或进子目录再列"
                )
            return out

        return self._op("list", raw_path, run, ctx=ctx)

    def read(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            target = self.host_path_for(vpath)
            assert target is not None
            self._require_root()
            if not target.is_file():
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"不是可读文件：{target}"}
            size = target.stat().st_size
            if size > self.spec.limits.max_write_bytes:
                return {
                    "ok": False,
                    "code": CODE_LIMIT_INVALID,
                    "message": f"文件 {size} 字节超过后端单次读取上限 {self.spec.limits.max_write_bytes} 字节，未读入",
                }
            text = target.read_text(encoding="utf-8", errors="replace")
            return {
                "ok": True,
                "path": vpath,
                "content": text,
                "bytes": size,
                "contentFingerprint": _fingerprint(text),
            }

        return self._op("read", raw_path, run, ctx=ctx)

    def write(self, raw_path: object, content: str, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            self._require_root()
            target = self.host_path_for(vpath)
            assert target is not None
            if target.is_dir():
                return {"ok": False, "code": CODE_INTERNAL, "message": f"目标是个目录，不能当文件写：{target}"}
            payload = str(content or "")
            size = _utf8_len(payload)
            if size > self.spec.limits.max_write_bytes:
                return {
                    "ok": False,
                    "code": CODE_LIMIT_INVALID,
                    "message": (
                        f"本次写入 {size} 字节超过后端单次上限 {self.spec.limits.max_write_bytes} 字节，"
                        "整篇未落盘（不截断写，避免产出半个文件）"
                    ),
                }
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(payload, encoding="utf-8")
            return {
                "ok": True,
                "path": vpath,
                "bytes": size,
                "contentFingerprint": _fingerprint(payload),
                "hostPath": str(target),
            }

        return self._op("write", raw_path, run, write=True, ctx=ctx, detail={"bytes": _utf8_len(content)})

    def delete(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            self._require_root()
            target = self.host_path_for(vpath)
            assert target is not None
            if not target.exists():
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"要删的东西不存在：{target}"}
            if target.is_dir():
                return {
                    "ok": False,
                    "code": CODE_CAPABILITY_DENIED,
                    "message": f"本后端只承载单文件删除，目录 {target} 未删除（递归删除不在 D-29 范围内）",
                }
            trash: str = ""
            try:
                if dp is not None:
                    trash_dir = Path(dp.file_trash_dir()) / self.spec.backend_id
                    trash_dir.mkdir(parents=True, exist_ok=True)
                    moved = trash_dir / f"{int(time.time() * 1000)}-{target.name}"
                    shutil.copy2(target, moved)
                    trash = str(moved)
            except Exception as exc:  # noqa: BLE001
                logger.warning("本地后端删除前备份失败（不阻断删除，但界面要知道）：%s", exc)
            target.unlink()
            return {
                "ok": True,
                "path": vpath,
                "message": "文件已删除" + ("，删除前已在本机回收目录留一份副本" if trash else "，未留副本（数据根不可写）"),
                "trashPath": trash,
            }

        return self._op("delete", raw_path, run, write=True, delete=True, ctx=ctx)

    def _exec(self, command: str, ctx: OpContext) -> dict[str, Any]:
        return _jsonable(
            {
                "ok": False,
                "code": CODE_CAPABILITY_DENIED,
                "message": "本地目录后端不承载命令执行；要执行请选 docker 或 ssh 后端（终端类工具走既有 run_shell，不在本车道）",
            }
        )


# ── Docker 后端 ────────────────────────────────────────────────


class DockerWorkspaceBackend(LocalWorkspaceBackend):
    """Docker 工作区后端：目录还是本机绑定目录（所以文件判定与本地同一条路），
    额外多一件「在这个目录里起容器命令」，重数（内存/CPU/进程数/网络）进 ``docker run`` 命令行。

    未验收清单：本机 docker CLI 在但 daemon 没起，所以「容器真的按这些重数跑起来」
    是 #162 收口批的事；本类自测钉的是**命令行构造**与**默认值取向**。
    """

    kind = "docker"

    def __init__(self, spec: BackendSpec, **deps: Any):
        self.run_fn: Callable[[list[str], int], tuple[int, str, str]] | None = deps.pop("run_fn", None)
        super().__init__(spec, **deps)
        if not str(spec.options.get("image") or "").strip() and not spec.errors:
            spec.errors.append("Docker 后端缺少 options.image（容器镜像）")
        self.container_root = str(spec.options.get("containerPath") or "/workspace").strip() or "/workspace"

    def host_path_for(self, vpath: str) -> Path | None:
        if self.root is None:
            return None
        return (self.root / vpath) if vpath else self.root

    def cli_available(self) -> bool:
        return shutil.which("docker") is not None

    def probe(self) -> dict[str, Any]:
        extra: dict[str, Any] = {
            "cliFound": self.cli_available(),
            "unverified": [DOCKER_DAEMON_NOTE, "真容器内的挂载可见性与属主映射未在本机验证"],
            "limitsEnforcedByCli": True,
        }
        if not extra["cliFound"]:
            extra["message"] = "本机 PATH 里没有 docker CLI，容器执行不可用（文件半边仍可走绑定目录）"
        elif self.run_fn is None:
            extra["daemonVerified"] = False
        probe = self._base_probe(extra=extra)
        if extra["cliFound"] and self.run_fn is None:
            # CLI 在但 daemon 够不着时，「后端可用」这句话不能替容器说话
            probe["status"] = STATUS_UNVERIFIED
            probe["message"] = str(probe.get("message") or "") + "；容器执行未验收"
        return probe

    def build_run_argv(self, command: str) -> list[str]:
        """构造 ``docker run`` 的 argv（列表形式，不经本机 shell）。

        取向：默认**断网**、默认**非特权**、默认**只读挂载**（后端只读时）；
        配置带进来的高危重数在 :data:`_DOCKER_FORBIDDEN_ARGS` 里，一律丢弃并说明。
        """
        limits = self.spec.limits
        argv: list[str] = ["docker", "run", "--rm"]
        name = f"tt-ws-{self.spec.backend_id}"[:63]
        argv += ["--name", name]
        mode = "ro" if self.spec.read_only else "rw"
        root = str(self.root) if self.root is not None else ""
        if root:
            argv += ["--volume", f"{root}:{self.container_root}:{mode}"]
        argv += ["--workdir", self.container_root]
        if limits.network:
            argv += ["--network", limits.network]
        if limits.memory_mb:
            argv += ["--memory", f"{limits.memory_mb}m"]
        else:
            argv += ["--memory-swap", "0"]  # 0 = 未设内存上限：显式写出来，别让默认值背锅
        if limits.cpus:
            argv += ["--cpus", f"{limits.cpus:g}"]
        if limits.pids:
            argv += ["--pids-limit", str(limits.pids)]
        argv += ["--user", str(spec_user(self.spec))]
        if self.spec.read_only:
            argv += ["--read-only", "--tmpfs", "/tmp:rw,size=64m,noexec"]
        dropped = []
        kept = []
        extras = list(self.spec.options.get("dockerArgs") or [])
        index = 0
        while index < len(extras):
            token = str(extras[index]).strip()
            index += 1
            head = token.split("=", 1)[0]
            if head not in _DOCKER_FORBIDDEN_ARGS and token not in _DOCKER_FORBIDDEN_ARGS:
                kept.append(token)
                continue
            dropped.append(token)
            # 短横后跟独立值的写法（``-v /:/host``、``--network host``）只丢 flag 不丢值，
            # 值会当成位置参数留在 argv 里 —— 等于把绕开边界的那句话照样喂给 docker。
            # 下一个 token 不以短横开头就按「它的值」一起丢；带 ``=`` 的写法已经在同一 token 里。
            if "=" not in token and index < len(extras):
                nxt = str(extras[index]).strip()
                if nxt and not nxt.startswith("-"):
                    dropped.append(nxt)
                    index += 1
        argv += kept
        if dropped:
            self.spec.warnings.append(
                "配置里的这些 docker 重数被丢弃（会绕开挂载/网络/隔离边界）：" + "、".join(dropped[:8])
            )
        argv += [str(self.spec.options.get("image") or ""), "sh", "-c", str(command or "")]
        return argv

    def _exec(self, command: str, ctx: OpContext) -> dict[str, Any]:
        if self.spec.errors:
            decision = AccessDecision(
                allowed=False,
                reason="工作区后端配置未通过校验，容器命令未起（fail-closed）：" + "；".join(self.spec.errors),
                source=SOURCE_BACKEND,
                code=CODE_SPEC_INVALID,
            )
            self._emit("exec", "", "denied", decision, ctx)
            return decision.error()
        argv = self.build_run_argv(command)

        def finish(rc: int, stdout: str, stderr: str) -> dict[str, Any]:
            return {
                "ok": rc == 0,
                "returnCode": rc,
                "stdout": _brief(stdout, 4000) if rc == 0 else stdout[:8000],
                "stderr": _brief(stderr, 2000),
                "argv": argv,
                "network": self.spec.limits.network,
                "resourceLimits": {
                    "memoryMb": self.spec.limits.memory_mb,
                    "cpus": self.spec.limits.cpus,
                    "pids": self.spec.limits.pids,
                },
            }

        def run(_vpath: str) -> dict[str, Any]:
            fn = self.run_fn or _subprocess_run
            rc, out, err = fn(argv, self.spec.limits.timeout_secs)
            return finish(rc, out, err)

        result = self._op("exec", "", run, ctx=ctx, detail={"argv": " ".join(argv[:6])})
        if result.get("ok") is False and not self.run_fn and not self.cli_available():
            result["message"] = str(result.get("message") or "") + "（本机没有 docker CLI）"
            result["unverified"] = True
        return result


def spec_user(spec: BackendSpec) -> str:
    """容器内运行身份：默认非 root；配置写死 root 时如实放行但记账"""
    raw = str(spec.options.get("containerUser") or "").strip()
    if raw.lower() in ("root", "0"):
        spec.warnings.append("容器内以 root 运行（options.containerUser=root），绑定目录的属主权限自此不受限")
        return "0:0"
    if re.match(r"^\d{1,6}(:\d{1,6})?$", raw):
        return raw
    return "10001:10001"


def _subprocess_run(argv: list[str], timeout_secs: int) -> tuple[int, str, str]:
    """默认传输件：真起子进程。列表形式交给 CreateProcess，不经本机 shell。"""
    try:
        proc = subprocess.run(  # noqa: S603
            argv,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1, int(timeout_secs or 120)),
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "docker 命令不存在（未安装或不在 PATH）"
    except subprocess.TimeoutExpired:
        return 124, "", f"容器命令超过 {timeout_secs} 秒未结束，已由本层终止"
    except Exception as exc:  # noqa: BLE001
        return 1, "", f"容器命令未能启动：{_brief(exc)}"
    return int(proc.returncode or 0), proc.stdout or "", proc.stderr or ""


# ── PostgreSQL 后端 ────────────────────────────────────────────

_PG_CREATE = (
    "CREATE TABLE IF NOT EXISTS {table} ("
    "backend_id TEXT NOT NULL, obj_path TEXT NOT NULL, content TEXT NOT NULL, "
    "content_bytes INTEGER NOT NULL, fingerprint TEXT NOT NULL, "
    "updated_at TIMESTAMPTZ NOT NULL DEFAULT now(), "
    "PRIMARY KEY (backend_id, obj_path))"
)

_PG_DRIVER_MODULES = ("psycopg", "psycopg2")


class _PgUnavailable(BackendUnavailable):
    pass


class PostgresWorkspaceBackend(WorkspaceBackend):
    """PostgreSQL 工作区后端：对象路径 + 文本正文存在一张表里。

    **没有新增依赖**：引擎随包运行时里没有 psycopg，也不该为一个后端加一个。
    所以这里只认「注入一个 DB-API 2.0 的 connect 件」，缺注入时如实报缺驱动、不静默换方言。
    自测用的是一个内存假连接（记录 SQL 与参数），钉的是 SQL 形状、绑定参数、前缀过滤与
    fail-closed —— 真库真服务未验收。

    SQL 里唯一允许拼进去的是标识符（schema/table），且先过 ``_IDENT`` 白名单；
    值一律走 ``%s`` 占位与参数元组。
    """

    kind = "postgresql"

    def __init__(self, spec: BackendSpec, **deps: Any):
        self.connect_fn: Callable[[dict[str, str]], Any] | None = deps.pop("connect_fn", None)
        super().__init__(spec, **deps)
        self.schema = str(spec.options.get("schema") or "public").strip() or "public"
        self.table_name = str(spec.options.get("table") or "tt_workspace_object").strip() or "tt_workspace_object"
        if not (_IDENT.match(self.schema) and _IDENT.match(self.table_name)) and not spec.errors:
            spec.errors.append(
                f"表标识符「{self.schema}.{self.table_name}」不合白名单，SQL 不拼可疑标识符"
            )

    @property
    def qualified_table(self) -> str:
        return f'"{self.schema}"."{self.table_name}"'

    def _dsn_fields(self) -> tuple[dict[str, str], list[str]]:
        options = dict(self.spec.options)
        dsn: dict[str, str] = {}
        for key in ("host", "port", "dbname", "user", "sslmode"):
            value = str(options.get(key) or "").strip()
            if value:
                dsn[key] = value
        values, problems = resolve_credentials(self.spec.credentials, environ=self._environ)
        for field_name, value in values.items():
            target = {"password": "password"}.get(field_name, field_name)
            dsn[target] = value
        return dsn, problems

    def _connect(self):
        if self.connect_fn is None:
            found = [name for name in _PG_DRIVER_MODULES if _module_available(name)]
            raise _PgUnavailable(
                CODE_DRIVER_MISSING,
                "PostgreSQL 后端需要 DB-API 2.0 连接件：随包运行时里没有 "
                + "/".join(_PG_DRIVER_MODULES)
                + "，且本车道不为此新增依赖。"
                + ("已装好驱动，但未注入 connect 件（未接线）" if found else "未装驱动")
                + "（真库形态未验收）",
                unverified=True,
            )
        dsn, problems = self._dsn_fields()
        if problems:
            raise _PgUnavailable(CODE_CREDENTIAL_UNRESOLVED, "；".join(problems))
        return self.connect_fn(dsn)

    def probe(self) -> dict[str, Any]:
        dsn, problems = self._dsn_fields()
        extra: dict[str, Any] = {
            "table": f"{self.schema}.{self.table_name}",
            "dsnFields": sorted(k for k in dsn if k != "password"),
            "credentialProblems": problems,
            "driverInjected": self.connect_fn is not None,
            "unverified": [
                "真 PostgreSQL 服务未连接过（本机无库），表存在性、权限与并发行为未验收",
                "服务端角色配额与连接数限制不由本层强制",
            ],
        }
        return self._base_probe(extra=extra)

    def _execute(self, sql: str, params: tuple[Any, ...], *, fetch: bool = False) -> list[Any]:
        """一条语句一个事务；超时用 SET LOCAL 随事务走，不污染连接池里的长会话"""
        conn = self._connect()
        try:
            cursor = conn.cursor()
            timeout = self.spec.limits.timeout_secs
            try:
                if timeout:
                    cursor.execute("SET LOCAL statement_timeout = %s", (int(timeout) * 1000,))
                cursor.execute(sql, params)
                rows = cursor.fetchall() if fetch else []
            finally:
                try:
                    cursor.close()
                except Exception:  # noqa: BLE001
                    pass
            try:
                conn.commit()
            except Exception:  # noqa: BLE001  有些假连接不实现 commit
                pass
            return list(rows)
        finally:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def list_dir(self, raw_path: object = "", ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            self.ensure_table()
            # 表里的 obj_path 存的是「后端根 + 相对路径」的完整落点（与对象存储的 key 同口径），
            # 所以查前缀要查存储键，回给调用方的 path 再把后端根剥掉。
            base = self.spec.prefix.strip("/")
            wanted = storage_key(self.spec, vpath)
            prefix = f"{wanted}/" if wanted else ""
            sql = (
                f"SELECT obj_path, content_bytes, fingerprint, updated_at FROM {self.qualified_table} "
                "WHERE backend_id = %s AND (%s = '' OR obj_path LIKE %s) ORDER BY obj_path LIMIT %s"
            )
            rows = self._execute(
                sql,
                (self.spec.backend_id, prefix, _like_escape(prefix) + "%", int(self.spec.limits.max_list_items)),
                fetch=True,
            )
            items = [
                {
                    "name": str(r[0]).rsplit("/", 1)[-1],
                    "path": _strip_backend_prefix(str(r[0]), base),
                    "bytes": int(r[1] or 0),
                    "fingerprint": str(r[2] or ""),
                }
                for r in rows
            ]
            out: dict[str, Any] = {
                "ok": True,
                "path": vpath,
                "items": items,
                "count": len(items),
                "table": f"{self.schema}.{self.table_name}",
            }
            if len(items) >= self.spec.limits.max_list_items:
                out["truncated"] = True
                out["note"] = f"命中条数到达单次上限 {self.spec.limits.max_list_items}，本页可能不完整"
            return out

        return self._op("list", raw_path, run, ctx=ctx)

    def ensure_table(self) -> None:
        if self.spec.options.get("_tableReady"):
            return
        self._execute(_PG_CREATE.format(table=self.qualified_table), ())
        self.spec.options["_tableReady"] = True

    def read(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            if not vpath:
                return {
                    "ok": False,
                    "code": CODE_PATH_UNSAFE,
                    "message": "表后端要的是对象路径，根不是个文件（列目录请用 list）",
                }
            self.ensure_table()
            rows = self._execute(
                f"SELECT content, content_bytes FROM {self.qualified_table} "
                "WHERE backend_id = %s AND obj_path = %s",
                (self.spec.backend_id, storage_key(self.spec, vpath)),
                fetch=True,
            )
            if not rows:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"表里没有这个对象路径：{storage_key(self.spec, vpath)}"}
            content = str(rows[0][0] or "")
            return {
                "ok": True,
                "path": vpath,
                "content": content,
                "bytes": int(rows[0][1] or _utf8_len(content)),
                "contentFingerprint": _fingerprint(content),
            }

        return self._op("read", raw_path, run, ctx=ctx)

    def write(self, raw_path: object, content: str, ctx: OpContext | None = None) -> dict[str, Any]:
        payload = str(content or "")
        if "\x00" in payload:
            return _jsonable(
                {
                    "ok": False,
                    "code": CODE_PATH_UNSAFE,
                    "message": "表后端只承载不含 NUL 的文本正文（二进制请走对象存储后端），本次未写",
                }
            )

        def run(vpath: str) -> dict[str, Any]:
            if not vpath:
                return {"ok": False, "code": CODE_PATH_UNSAFE, "message": "写对象必须给明确的路径，根不是个对象"}
            self.ensure_table()
            size = _utf8_len(payload)
            if size > self.spec.limits.max_write_bytes:
                return {
                    "ok": False,
                    "code": CODE_LIMIT_INVALID,
                    "message": (
                        f"本次写入 {size} 字节超过后端上限 {self.spec.limits.max_write_bytes} 字节，"
                        "整篇未落库（不截断写）"
                    ),
                }
            self._execute(
                f"INSERT INTO {self.qualified_table} "
                "(backend_id, obj_path, content, content_bytes, fingerprint, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, now()) "
                "ON CONFLICT (backend_id, obj_path) DO UPDATE SET "
                "content = EXCLUDED.content, content_bytes = EXCLUDED.content_bytes, "
                "fingerprint = EXCLUDED.fingerprint, updated_at = now()",
                (self.spec.backend_id, storage_key(self.spec, vpath), payload, size, _fingerprint(payload)),
            )
            return {
                "ok": True,
                "path": vpath,
                "objPath": storage_key(self.spec, vpath),
                "bytes": size,
                "contentFingerprint": _fingerprint(payload),
                "table": f"{self.schema}.{self.table_name}",
            }

        return self._op("write", raw_path, run, write=True, ctx=ctx, detail={"bytes": _utf8_len(payload)})

    def delete(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            if not vpath:
                return {
                    "ok": False,
                    "code": CODE_CAPABILITY_DENIED,
                    "message": "不许整后端清空：删除必须给明确对象路径（批量清理由 DBA 在库侧做）",
                }
            self.ensure_table()
            self._execute(
                f"DELETE FROM {self.qualified_table} WHERE backend_id = %s AND obj_path = %s",
                (self.spec.backend_id, storage_key(self.spec, vpath)),
            )
            return {"ok": True, "path": vpath, "objPath": storage_key(self.spec, vpath), "message": "表里的对象已删除（无本机回收站）"}

        return self._op("delete", raw_path, run, write=True, delete=True, ctx=ctx)

    def _exec(self, command: str, ctx: OpContext) -> dict[str, Any]:
        return _jsonable(
            {
                "ok": False,
                "code": CODE_CAPABILITY_DENIED,
                "message": "数据库后端不承载命令执行（能起命令的是 docker / ssh）",
            }
        )


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def _like_escape(text: str) -> str:
    return str(text or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ── 对象存储：S3 / COS 适配器（只到适配器 + 假端点契约测试）──────


def _urllib_transport(
    method: str, url: str, *, headers: dict[str, str], body: bytes | None, timeout: int
) -> tuple[int, dict[str, str], bytes]:
    """默认传输件（真联网）。自测永远注入假件，不走这条路。"""
    request = urllib.request.Request(url, data=body, method=method)
    for key, value in headers.items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=max(1, timeout)) as response:  # noqa: S310
            return int(response.status), dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return int(exc.code), dict(exc.headers.items() if exc.headers else {}), exc.read() or b""
    except Exception as exc:  # noqa: BLE001
        raise BackendUnavailable(
            CODE_BACKEND_UNAVAILABLE, f"对象存储请求未能完成：{_brief(exc)}（真实凭据与端点未验收）"
        ) from exc


class _ObjectStoreBackend(WorkspaceBackend):
    """对象存储后端公共半边：路径口径、大小上限、审计、分页；签名留给具体 kind。

    这一族**只到适配器**：凭据一律按引用现取，端点与真桶未验收；自测跑在假传输件上，
    假传输件绿不等于真桶能读写。
    """

    kind = ""

    def __init__(self, spec: BackendSpec, **deps: Any):
        self.request_fn: Callable[..., tuple[int, dict[str, str], bytes]] | None = deps.pop(
            "request_fn", None
        )
        super().__init__(spec, **deps)
        self.bucket = str(spec.options.get("bucket") or "").strip()
        self.region = str(spec.options.get("region") or "").strip() or "us-east-1"
        endpoint = str(spec.options.get("endpoint") or "").strip()
        self.endpoint = endpoint.rstrip("/")
        # 桶名 / HTTPS 端点 / COS 必填端点都在 parse_spec 那侧记账（设置页要看得到），
        # 这里只留寻址方式：给了端点走 path-style，没给（且 kind=s3）走虚拟主机式。

    def _transport(self) -> Callable[..., tuple[int, dict[str, str], bytes]]:
        return self.request_fn or _urllib_transport

    def _url(self, key: str, query: dict[str, str] | None = None) -> str:
        """两种寻址分开算，别把桶名拼两遍：

        - 给了 ``options.endpoint``（自建 MinIO / 假端点 / COS 域名）⇒ **path-style**：
          ``{endpoint}/{bucket}/{key}``，签名里的 host 就是 endpoint 的 host；
        - 没给 endpoint ⇒ **virtual-host**：``https://{bucket}.s3.{region}.amazonaws.com/{key}``，
          只有 S3 兼容服务会走这条（COS 必须显式给 endpoint，见构造里的校验）。
        """
        safe_key = "/".join(urllib.parse.quote(seg, safe="") for seg in key.split("/") if seg)
        if self.endpoint:
            parts = urllib.parse.urlsplit(self.endpoint)
            path = f"/{urllib.parse.quote(self.bucket, safe='')}"
            if safe_key:
                path += f"/{safe_key}"
            elif not path.endswith("/"):
                path += "/"
            return parts._replace(path=path, query=urllib.parse.urlencode(query or {})).geturl()
        host = f"{self.bucket}.s3.{self.region}.amazonaws.com"
        path = f"/{safe_key}" if safe_key else "/"
        return (
            urllib.parse.SplitResult("https", host, path, urllib.parse.urlencode(query or {}), "")
        ).geturl()

    def _path_style(self) -> bool:
        """带显式端点（含假端点）时按 path-style 签名，虚拟主机式按桶名前缀域名 —— 两者签的 host 不同"""
        return bool(self.endpoint)

    def probe(self) -> dict[str, Any]:
        values, problems = resolve_credentials(self.spec.credentials, environ=self._environ)
        extra: dict[str, Any] = {
            "bucket": self.bucket,
            "region": self.region,
            "endpoint": self.endpoint,
            "pathStyle": self._path_style(),
            "credentialFields": sorted(values),
            "credentialProblems": problems,
            "transportInjected": self.request_fn is not None,
            "unverified": [
                "真实对象存储凭据与桶未验证过（只跑过假端点契约测试）：不许据此声称已支持对象存储",
                "桶策略、版本化与生命周期不由本层强制",
            ],
        }
        if problems:
            extra["message"] = "凭据未解析，本后端所有读写会被拒（fail-closed）"
        return self._base_probe(extra=extra)

    def _signed_request(
        self, method: str, key: str, *, body: bytes | None = None, query: dict[str, str] | None = None
    ) -> tuple[str, dict[str, str], bytes | None]:
        raise NotImplementedError

    def _guard_credentials(self) -> None:
        values, problems = resolve_credentials(self.spec.credentials, environ=self._environ)
        if problems:
            raise BackendUnavailable(CODE_CREDENTIAL_UNRESOLVED, "；".join(problems))
        self._credential_values = values

    def list_dir(self, raw_path: object = "", ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            self._guard_credentials()
            # S3 的 prefix 是「字符串前缀」，不带尾斜杠会把 docs/a.md 和 docsx.md 一起捞进来。
            # 列一个目录 = 列「这个键 + /」之下的键，根目录就是后端根前缀本身。
            wanted = storage_key(self.spec, vpath)
            full_prefix = f"{wanted}/" if wanted else ""
            query = self._list_query(full_prefix)
            status, headers, payload = self._request("GET", "", query=query)
            if status >= 400:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": _object_error(status, payload)}
            items = self._parse_listing(payload, full_prefix)
            truncated = bool(_xml_flag(payload, "Truncated"))
            out: dict[str, Any] = {"ok": True, "path": vpath, "items": items, "count": len(items)}
            if truncated:
                out["truncated"] = True
                out["note"] = (
                    f"桶侧返回 Truncated=true：{full_prefix or '（全桶）'} 下对象多于一次列表的返回，"
                    "本车道未做 continuation-token 续拉"
                )
            return out

        return self._op("list", raw_path, run, ctx=ctx)

    def _list_query(self, prefix: str) -> dict[str, str]:
        return {"prefix": prefix, "max-keys": str(self.spec.limits.max_list_items), "delimiter": "/"}

    def _parse_listing(self, payload: bytes, prefix: str) -> list[dict[str, Any]]:
        return _parse_s3_listing(payload, self.spec.prefix, prefix)

    def read(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            self._guard_credentials()
            key = self._key(vpath)
            status, headers, payload = self._request("GET", key)
            if status == 404:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"桶里没有这个对象：{key}"}
            if status >= 400:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": _object_error(status, payload)}
            text = payload.decode("utf-8", errors="replace")
            return {
                "ok": True,
                "path": vpath,
                "content": text,
                "bytes": len(payload),
                "etag": str(headers.get("ETag") or headers.get("etag") or "").strip('"'),
                "contentFingerprint": _fingerprint(text),
            }

        return self._op("read", raw_path, run, ctx=ctx)

    def write(self, raw_path: object, content: str, ctx: OpContext | None = None) -> dict[str, Any]:
        payload = str(content or "")
        size = _utf8_len(payload)

        def run(vpath: str) -> dict[str, Any]:
            self._guard_credentials()
            if size > self.spec.limits.max_write_bytes:
                return {
                    "ok": False,
                    "code": CODE_LIMIT_INVALID,
                    "message": (
                        f"本次写入 {size} 字节超过后端上限 {self.spec.limits.max_write_bytes} 字节，"
                        "对象未上传（不截断上传）"
                    ),
                }
            key = self._key(vpath)
            status, headers, body = self._request("PUT", key, data=payload.encode("utf-8"))
            if status >= 400:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": _object_error(status, body)}
            return {
                "ok": True,
                "path": vpath,
                "bytes": size,
                "key": key,
                "etag": str(headers.get("ETag") or headers.get("etag") or "").strip('"'),
                "contentFingerprint": _fingerprint(payload),
            }

        return self._op("write", raw_path, run, write=True, ctx=ctx, detail={"bytes": size})

    def delete(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            self._guard_credentials()
            key = self._key(vpath)
            status, _headers, body = self._request("DELETE", key)
            if status >= 400:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": _object_error(status, body)}
            return {"ok": True, "path": vpath, "key": key, "message": "对象已删除（版本化桶里历史版本不由本层清理）"}

        return self._op("delete", raw_path, run, write=True, delete=True, ctx=ctx)

    def _key(self, vpath: str) -> str:
        return storage_key(self.spec, vpath)

    def _request(
        self, method: str, key: str, *, data: bytes | None = None, query: dict[str, str] | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        url, headers, body = self._signed_request(method, key, body=data, query=query)
        transport = self._transport()
        status, resp_headers, payload = transport(
            method, url, headers=headers, body=body, timeout=self.spec.limits.timeout_secs
        )
        return int(status), resp_headers, payload

    def _exec(self, command: str, ctx: OpContext) -> dict[str, Any]:
        return _jsonable(
            {
                "ok": False,
                "code": CODE_CAPABILITY_DENIED,
                "message": "对象存储后端不承载命令执行（能起命令的是 docker / ssh）",
            }
        )


class S3WorkspaceBackend(_ObjectStoreBackend):
    """S3 兼容适配器：SigV4 签名用标准库现算（不引入 boto3/aioboto3）。

    真桶未验收；自测跑假端点，钉的是「签名字段齐不齐、key 前缀对不对、HTTPS 强制、
    凭据缺失就拒」，不是「厂商端点真的收」。
    """

    kind = "s3"

    def _signed_request(
        self, method: str, key: str, *, body: bytes | None = None, query: dict[str, str] | None = None
    ) -> tuple[str, dict[str, str], bytes | None]:
        payload = body or b""
        values = getattr(self, "_credential_values", {})
        access = str(values.get("accessKeyId") or values.get("accessKey") or "")
        secret = str(values.get("secretAccessKey") or values.get("secretKey") or "")
        token = str(values.get("sessionToken") or values.get("token") or "")
        if not access or not secret:
            raise BackendUnavailable(
                CODE_CREDENTIAL_UNRESOLVED,
                "S3 签名需要 access key 与 secret，两者都只能按 env:/keychain: 引用取到（未验收）",
            )
        url = self._url(key, query)
        parts = urllib.parse.urlsplit(url)
        host = parts.netloc
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted((query or {}).items())
        )
        now = datetime.now(timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        headers = {
            "host": host,
            "x-amz-content-sha256": hashlib.sha256(payload).hexdigest(),
            "x-amz-date": amz_date,
        }
        if token:
            headers["x-amz-security-token"] = token
        signed_list = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(headers.items()))
        canonical_request = "\n".join(
            [
                method.upper(),
                urllib.parse.quote(parts.path or "/", safe="/"),
                canonical_query,
                canonical_headers,
                signed_list,
                headers["x-amz-content-sha256"],
            ]
        )
        scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join(
            ["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()]
        )
        key_bytes = ("AWS4" + secret).encode("utf-8")
        for piece in (date_stamp, self.region, "s3", "aws4_request"):
            key_bytes = hmac.new(key_bytes, piece.encode("utf-8"), hashlib.sha256).digest()
        signature = hmac.new(key_bytes, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={access}/{scope}, "
            f"SignedHeaders={signed_list}, Signature={signature}"
        )
        return url, headers, payload


class CosWorkspaceBackend(_ObjectStoreBackend):
    """腾讯云 COS 适配器：q-sign-algorithm=sha1 签名（标准库 hmac）。同样只到适配器，真桶未验收。"""

    kind = "cos"

    def _signed_request(
        self, method: str, key: str, *, body: bytes | None = None, query: dict[str, str] | None = None
    ) -> tuple[str, dict[str, str], bytes | None]:
        values = getattr(self, "_credential_values", {})
        secret_id = str(values.get("secretId") or values.get("accessKeyId") or values.get("accessKey") or "")
        secret_key = str(values.get("secretKey") or values.get("secretAccessKey") or "")
        token = str(values.get("token") or values.get("sessionToken") or "")
        if not secret_id or not secret_key:
            raise BackendUnavailable(
                CODE_CREDENTIAL_UNRESOLVED,
                "COS 签名需要 SecretId 与 SecretKey，且只能按 env:/keychain: 引用取到（未验收）",
            )
        url = self._url(key, query)
        parts = urllib.parse.urlsplit(url)
        now = int(time.time())
        key_time = f"{now - 60};{now + 900}"
        path_list = parts.path or "/"
        param_list = "&".join(f"{k}={urllib.parse.quote(v, safe='')}" for k, v in sorted((query or {}).items()))
        header_map = {"host": parts.netloc}
        if token:
            header_map["x-cos-security-token"] = token
        header_list = ";".join(sorted(header_map))
        http_string = "\n".join([method.lower(), path_list, param_list, header_list, ""])
        string_to_sign = "\n".join(["sha1", key_time, hashlib.sha1(http_string.encode("utf-8")).hexdigest(), ""])
        sign_key = hmac.new(secret_key.encode("utf-8"), key_time.encode("utf-8"), hashlib.sha1).hexdigest()
        signature = hmac.new(sign_key.encode("utf-8"), string_to_sign.encode("utf-8"), hashlib.sha1).hexdigest()
        header_map["Authorization"] = (
            f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}"
            f"&q-key-time={key_time}&q-header-list={header_list}"
            f"&q-url-param-list={'&'.join(sorted(query or {}))}&q-signature={signature}"
        )
        return url, header_map, body


def _parse_s3_listing(payload: bytes, backend_prefix: str, wanted_prefix: str) -> list[dict[str, Any]]:
    """解析 ListObjects(v1/v2) 的 XML：命名空间容错，字段缺就如实少"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(payload or b"<ListBucketResult/>")
    except ET.ParseError:
        return []

    def strip(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    def child(node, local: str) -> str:
        """按**局部名**取子节点文本：ListBucketResult 带默认命名空间，
        `findtext("Key")` 在 ElementTree 里匹配不到 `{http://...}Key`，
        于是每条都读成空串——列出来的就是一堆没有名字的空条目。"""
        for leaf in node:
            if strip(leaf.tag) == local:
                return (leaf.text or "").strip()
        return ""

    items: list[dict[str, Any]] = []
    base = (backend_prefix or "").strip("/")
    # wanted_prefix 是发给服务端的完整键前缀（含后端根），rel 是剥掉后端根之后的路径
    # —— 两者口径不同，直接比会把每条都滤掉（列空表看着像桶是空的）。
    wanted_rel = _strip_backend_prefix(wanted_prefix or "", base)
    for node in root:
        name = strip(node.tag)
        if name not in ("Contents", "CommonPrefixes"):
            continue
        if name == "CommonPrefixes":
            prefix_text = child(node, "Prefix").strip("/")
            if base and prefix_text.startswith(base):
                rel = prefix_text[len(base) :].lstrip("/")
            else:
                rel = prefix_text
            if rel:
                items.append({"name": rel.rsplit("/", 1)[-1], "path": rel, "isDir": True, "bytes": 0})
            continue
        key_text = child(node, "Key")
        size_text = child(node, "Size") or "0"
        rel = key_text[len(base) :].lstrip("/") if base and key_text.startswith(base) else key_text
        if wanted_rel and not rel.startswith(wanted_rel):
            continue
        if rel.endswith("/"):
            continue
        items.append(
            {
                "name": rel.rsplit("/", 1)[-1],
                "path": rel,
                "isDir": False,
                "bytes": int(size_text) if size_text.isdigit() else 0,
                "etag": child(node, "ETag").strip('"'),
            }
        )
    return items


def _xml_flag(payload: bytes, tag: str) -> str:
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(payload or b"")
    except ET.ParseError:
        return ""
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1] == tag:
            return (node.text or "").strip()
    return ""


def _object_error(status: int, payload: bytes) -> str:
    code = _xml_flag(payload, "Code")
    message = _xml_flag(payload, "Message")
    tail = f"：{code} {message}".rstrip() if code or message else ""
    return f"对象存储返回 HTTP {status}{tail}（真实端点形态未验收）"


# ── SSH 适配器（只到适配器）────────────────────────────────────


class SshWorkspaceBackend(WorkspaceBackend):
    """SSH 远程主机适配器：argv 构造 + 传输件注入，不自己起密码认证。

    边界三条是刻意的：
    - **只认密钥**：密码认证当场拒（``password`` 引用不接单）；
    - **不许关主机密钥校验**：``StrictHostKeyChecking=no`` 一律不拼，配置要求时给警告；
    - **凭据不进命令行**：私钥路径按引用取，argv 里只有路径字符串。
    真主机未验收：自测跑假 ``run_fn``，钉的是 argv 形状与输出解析，不是真能连上。
    """

    kind = "ssh"

    def __init__(self, spec: BackendSpec, **deps: Any):
        self.run_fn: Callable[[list[str], int], tuple[int, str, str]] | None = deps.pop("run_fn", None)
        super().__init__(spec, **deps)
        self.host = str(spec.options.get("host") or "").strip()
        self.user = str(spec.options.get("user") or "").strip()
        self.port = str(spec.options.get("port") or "22").strip()

    def probe(self) -> dict[str, Any]:
        _values, problems = resolve_credentials(self.spec.credentials, environ=self._environ)
        extra: dict[str, Any] = {
            "host": self.host,
            "user": self.user,
            "port": self.port,
            "credentialProblems": problems,
            "transportInjected": self.run_fn is not None,
            "unverified": [
                "真实 SSH 主机未连接过（无凭据、无主机指纹），文件读写与命令执行均未验收",
                "远程主机上的目录权限与 umask 不由本层强制",
            ],
        }
        if problems:
            extra["message"] = "凭据未解析（多为环境变量未设），本后端所有操作会被拒"
        return self._base_probe(extra=extra)

    def build_argv(self, remote_command: str) -> list[str]:
        argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "ConnectTimeout=10",
            "-p",
            self.port or "22",
        ]
        key_ref = next((ref for ref in self.spec.credentials if ref.key_name == "keyPath"), None)
        if key_ref is not None and key_ref.name:
            path = str((self._environ or os.environ).get(key_ref.name) or "").strip()
            if path:
                argv += ["-i", path]
        target = f"{self.user}@{self.host}" if self.user else self.host
        argv += [target, remote_command]
        return argv

    def _run_remote(self, remote_command: str) -> tuple[int, str, str]:
        values, problems = resolve_credentials(
            [r for r in self.spec.credentials if r.scheme == "env"], environ=self._environ
        )
        if problems:
            raise BackendUnavailable(CODE_CREDENTIAL_UNRESOLVED, "；".join(problems))
        argv = self.build_argv(remote_command)
        fn = self.run_fn or _subprocess_run
        return fn(argv, self.spec.limits.timeout_secs)

    def _shq(self, text: str) -> str:
        """单引号包裹，内部单引号按 ' '\\'' 转义 —— 路径永远不经受控字符串外的 shell 解析"""
        return "'" + str(text).replace("'", "'\\''") + "'"

    def _full(self, vpath: str) -> str:
        """远程落点 = 配置里的根 + 相对路径（与其他后端的 :func:`storage_key` 同口径）。

        ``options.root`` 允许写成远程绝对路径（``/srv/tt-ws``）—— 它是**配置面**给的，
        不是模型或对话里的路径；操作面只能用相对路径拼上去，``..`` 在归一那层就没了，
        所以拼不出越界。没配 root 时退回后端前缀，再退回远程登录账号的当前目录。
        """
        base = str(self.spec.options.get("root") or self.spec.prefix or "").strip().rstrip("/")
        if not base:
            return vpath
        return f"{base}/{vpath}" if vpath else base

    def list_dir(self, raw_path: object = "", ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            target = self._full(vpath) or "."
            rc, out, err = self._run_remote(f"ls -la {self._shq(target)}")
            if rc != 0:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"远程列目录失败：{_brief(err)}"}
            items: list[dict[str, Any]] = []
            for line in out.splitlines()[1:]:
                cols = line.split(None, 8)
                if len(cols) < 6:
                    continue
                name = cols[-1].split(" -> ")[0]
                if name in (".", ".."):
                    continue
                items.append(
                    {
                        "name": name,
                        "path": "/".join(x for x in (vpath, name) if x),
                        "isDir": line.startswith("d"),
                        "bytes": int(cols[4]) if cols[4].isdigit() else 0,
                        "mode": cols[0],
                    }
                )
                if len(items) >= self.spec.limits.max_list_items:
                    break
            return {"ok": True, "path": vpath, "items": items, "count": len(items), "host": self.host}

        return self._op("list", raw_path, run, ctx=ctx)

    def read(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            if not vpath:
                return {"ok": False, "code": CODE_PATH_UNSAFE, "message": "根不是个文件，列目录请用 list"}
            rc, out, err = self._run_remote(f"cat {self._shq(self._full(vpath))}")
            if rc != 0:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"远程读取失败：{_brief(err)}"}
            if _utf8_len(out) > self.spec.limits.max_write_bytes:
                return {"ok": False, "code": CODE_LIMIT_INVALID, "message": "远程文件超过本层上限，未取回正文"}
            return {"ok": True, "path": vpath, "content": out, "bytes": _utf8_len(out), "contentFingerprint": _fingerprint(out)}

        return self._op("read", raw_path, run, ctx=ctx)

    def write(self, raw_path: object, content: str, ctx: OpContext | None = None) -> dict[str, Any]:
        payload = str(content or "")
        size = _utf8_len(payload)

        def run(vpath: str) -> dict[str, Any]:
            if not vpath:
                return {"ok": False, "code": CODE_PATH_UNSAFE, "message": "写远程文件必须给明确路径"}
            if size > self.spec.limits.max_write_bytes:
                return {"ok": False, "code": CODE_LIMIT_INVALID, "message": f"本次写入 {size} 字节超过上限 {self.spec.limits.max_write_bytes}，未上传"}
            quoted = self._shq(payload)
            command = f"mkdir -p {self._shq(str(Path(self._full(vpath)).parent))} && printf %s {quoted} > {self._shq(self._full(vpath))}"
            rc, _out, err = self._run_remote(command)
            if rc != 0:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"远程写入失败：{_brief(err)}"}
            return {"ok": True, "path": vpath, "bytes": size, "contentFingerprint": _fingerprint(payload), "host": self.host}

        return self._op("write", raw_path, run, write=True, ctx=ctx, detail={"bytes": size})

    def delete(self, raw_path: object, ctx: OpContext | None = None) -> dict[str, Any]:
        def run(vpath: str) -> dict[str, Any]:
            if not vpath:
                return {"ok": False, "code": CODE_CAPABILITY_DENIED, "message": "不许删根目录"}
            rc, _out, err = self._run_remote(f"rm -f {self._shq(self._full(vpath))}")
            if rc != 0:
                return {"ok": False, "code": CODE_BACKEND_UNAVAILABLE, "message": f"远程删除失败：{_brief(err)}"}
            return {"ok": True, "path": vpath, "message": "远程文件已删除（无回收站，不可撤销）"}

        return self._op("delete", raw_path, run, write=True, delete=True, ctx=ctx)

    def _exec(self, command: str, ctx: OpContext) -> dict[str, Any]:
        def run(_vpath: str) -> dict[str, Any]:
            rc, out, err = self._run_remote(str(command or ""))
            return {
                "ok": rc == 0,
                "returnCode": rc,
                "stdout": out[:8000],
                "stderr": _brief(err, 2000),
                "host": self.host,
                "note": "命令在远程主机以其账号权限执行，资源限制不由本层强制 ⇒ 未验收",
            }

        return self._op("exec", "", run, ctx=ctx)


# ── 装配与切换 ─────────────────────────────────────────────────

_BACKEND_CLASSES: dict[str, type[WorkspaceBackend]] = {
    "local": LocalWorkspaceBackend,
    "docker": DockerWorkspaceBackend,
    "postgresql": PostgresWorkspaceBackend,
    "s3": S3WorkspaceBackend,
    "cos": CosWorkspaceBackend,
    "ssh": SshWorkspaceBackend,
}


def build_backend(
    spec: BackendSpec,
    *,
    policy: sr.SandboxPolicy | None = None,
    deps: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> WorkspaceBackend:
    """按 kind 装配后端。

    ``deps`` 是**测试与接线的注入口**（run_fn / connect_fn / request_fn）：生产接线不注入时
    走默认传输件，缺驱动/缺 CLI 由 ``probe()`` 如实报未验收，而不是拼个必然失败的调用。
    """
    cls = _BACKEND_CLASSES.get(spec.kind)
    if cls is None:
        spec.errors.append(f"后端类型「{spec.kind}」没有装配实现")
        cls = LocalWorkspaceBackend
    kwargs: dict[str, Any] = {"policy": policy, "environ": environ}
    for key, value in (deps or {}).items():
        kwargs[key] = value
    backend = cls(spec, **kwargs)
    backend.bind_policy(policy)
    if spec.errors:
        backend.spec.warnings.append(
            "本后端配置未通过校验：所有读写与执行都会先被拒（fail-closed），修配置后重新装配"
        )
    return backend


@dataclass
class WorkspaceRegistry:
    """工作区后端清单与当前选择。

    切换语义是 D-29 验收第一句：**换后端不改 Agent 配置**。所以 :meth:`switch` 只动
    「当前用哪个后端句柄」，返回体把「没变的东西」逐条列出来（策略对象、模型与工具装配
    都不在本类的管辖内），并且不做任何文件搬运 —— 数据迁移是另一件事，别把它写成切换。
    """

    _backends: dict[str, WorkspaceBackend] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _active: str = ""
    # 配置未通过校验的条目：不注册（切不过去、动不了任何东西），但要留着说明为什么。
    # 只把它从 configure 的返回值里丢掉，设置页下一屏就看不见这条错是从哪来的。
    _rejected: dict[str, list[str]] = field(default_factory=dict)
    audit: Callable[[dict[str, Any]], None] = append_audit

    # ── 装配 ────────────────────────────────────────────────

    def add(self, backend: WorkspaceBackend) -> str:
        spec = backend.spec
        if not spec.backend_id or not _BACKEND_ID.match(spec.backend_id):
            return f"后端 id 非法（{_brief(spec.backend_id)}）"
        self._backends[spec.backend_id] = backend
        if spec.backend_id not in self._order:
            self._order.append(spec.backend_id)
        if not self._active:
            self._active = spec.backend_id
        return ""

    def configure(
        self,
        raw_backends: Any,
        *,
        policy: sr.SandboxPolicy | None = None,
        deps: dict[str, Any] | None = None,
        environ: dict[str, str] | None = None,
        active: str = "",
    ) -> dict[str, Any]:
        """从配置段批量装配；单条坏配置只废那一条并说明，不拖垮整份清单。"""
        items = raw_backends if isinstance(raw_backends, list) else [raw_backends]
        problems: list[str] = []
        added: list[str] = []
        rejected: dict[str, list[str]] = {}
        for item in items:
            if not isinstance(item, dict):
                problems.append(f"后端条目格式非法（{type(item).__name__}），已跳过")
                continue
            spec = parse_spec(item)
            if spec.errors:
                # 坏配置不进清单：它能被切过去就等于把「带病跑」交给用户点。
                rejected[spec.backend_id] = list(spec.errors)
                problems.append(
                    f"后端「{_brief(spec.backend_id)}」配置未通过校验，已跳过：{'；'.join(spec.errors)}"
                )
                continue
            backend = build_backend(spec, policy=policy, deps=deps, environ=environ)
            problem = self.add(backend)
            if problem:
                problems.append(problem)
                continue
            added.append(spec.backend_id)
        # 只留最后一次 configure 的落选名单：它是「这一份配置」的产物，不是历史累加
        self._rejected = rejected
        if active:
            moved = self.switch(active)
            if not moved.get("ok"):
                problems.append(str(moved.get("message")))
        return _jsonable(
            {
                "ok": bool(added),
                "added": added,
                "problems": problems,
                "activeBackendId": self._active,
                "message": (
                    f"已装配 {len(added)} 个工作区后端" + (f"，{len(problems)} 条被拒" if problems else "")
                ),
            }
        )

    # ── 查询 ────────────────────────────────────────────────

    def get(self, backend_id: str) -> WorkspaceBackend | None:
        return self._backends.get(str(backend_id or "").strip())

    def active_id(self) -> str:
        """当前活动后端 id（只读）。给自测与端点面用，别让人去戳私有字段。"""
        return self._active

    def rejected(self, backend_id: str = "") -> Any:
        """配置未通过校验、没装进清单的条目。

        端点面要能回答「我按这个 id 找不到后端，是被拒了还是压根没写」——
        没有这个口子，调用方只能去戳 ``_rejected``，或把被拒说成「不存在」。
        """
        if backend_id:
            return list(self._rejected.get(str(backend_id).strip(), []))
        return [{"backendId": key, "errors": errors} for key, errors in self._rejected.items()]

    def current(self) -> WorkspaceBackend | None:
        return self._backends.get(self._active)

    def ids(self) -> list[str]:
        return list(self._order)

    def snapshot(self) -> dict[str, Any]:
        return _jsonable(
            {
                "ok": True,
                "activeBackendId": self._active,
                "backendCount": len(self._order),
                "backends": [self._backends[i].describe() for i in self._order],
                "probes": {i: self._backends[i].probe() for i in self._order},
                "rejected": self.rejected(),
                "note": (
                    "这里只描述工作区落点与后端能力；模型配置、工具装配与文件访问档位都不在其中，"
                    "切换后端不会改它们"
                ),
            }
        )

    # ── 切换 ────────────────────────────────────────────────

    def switch(self, backend_id: str) -> dict[str, Any]:
        wanted = str(backend_id or "").strip()
        if not wanted:
            return _jsonable(
                {"ok": False, "code": CODE_BACKEND_REQUIRED, "message": "切换工作区后端要给 backendId"}
            )
        backend = self._backends.get(wanted)
        if backend is None:
            if wanted in self._rejected:
                return _jsonable(
                    {
                        "ok": False,
                        "code": CODE_SPEC_INVALID,
                        "message": (
                            f"后端「{_brief(wanted)}」的配置未通过校验，没有装配进来，切不过去："
                            f"{'；'.join(self._rejected[wanted])}"
                        ),
                        "rejectedReasons": list(self._rejected[wanted]),
                    }
                )
            return _jsonable(
                {
                    "ok": False,
                    "code": CODE_BACKEND_NOT_FOUND,
                    "message": (
                        f"没有 id 为「{_brief(wanted)}」的工作区后端（已装配的是"
                        f"{'、'.join(self._order) or '空'}）"
                    ),
                }
            )
        if backend.spec.errors:
            return _jsonable(
                {
                    "ok": False,
                    "code": CODE_SPEC_INVALID,
                    "message": f"后端「{wanted}」配置未通过校验，不能切过去：{'；'.join(backend.spec.errors)}",
                }
            )
        previous = self._active
        self._active = wanted
        self.audit(
            {
                "ts": _now_iso(),
                "backendId": wanted,
                "kind": backend.kind,
                "op": "switch",
                "outcome": "ok",
                "path": "",
                "display": backend.spec.root_display,
                "previousBackendId": previous,
                "agentConfigChanged": False,
            }
        )
        return _jsonable(
            {
                "ok": True,
                "code": "",
                "activeBackendId": wanted,
                "previousBackendId": previous,
                "changed": [
                    {"field": "workspaceBackend", "from": previous, "to": wanted},
                    {"field": "rootDisplay", "to": backend.spec.root_display},
                    {"field": "capabilities", "to": sorted(backend.spec.capabilities())},
                ],
                "unchanged": [
                    "模型配置与路由（本车道不碰）",
                    "工具装配与开关（build_toolkit 的 extra 句柄不变，只是它指向的后端换了）",
                    "文件访问档位与工作区沙箱策略（同一份 SandboxPolicy 对象照样传给新后端）",
                    "会话历史与检查点",
                ],
                "notIncluded": "后端之间的数据搬运不在本卡范围：切换只换落点，不迁文件",
                "message": f"工作区落点已切到「{wanted}」（{KIND_LABELS.get(backend.kind, backend.kind)}）",
            }
        )


REGISTRY = WorkspaceRegistry()


def reset_registry() -> None:
    """自测/重建用：清掉装配现场（含活动 id），不碰审计文件"""
    global REGISTRY
    REGISTRY = WorkspaceRegistry()


def list_backends() -> dict[str, Any]:
    return REGISTRY.snapshot()


def active_backend(
    policy: sr.SandboxPolicy | None = None,
    *,
    runtime: Any = None,
    deps: dict[str, Any] | None = None,
    environ: dict[str, str] | None = None,
) -> WorkspaceBackend | None:
    """本轮要用哪个后端。

    优先级：runtime 里显式指定的后端 → 清单里已装配的活动后端 → None（= 既有本地工作目录形态）。
    指定了但清单里没有、或那份配置没通过校验 ⇒ 如实回 None 并留一条说明，
    不静默造一个「看着像远程其实是空壳」的后端；调用方按本地形态继续跑。
    """
    spec = spec_from_runtime(runtime)
    if spec is None:
        current = REGISTRY.current()
        if current is not None:
            current.bind_policy(policy)
        return current
    found = REGISTRY.get(spec.backend_id)
    if found is not None and found.kind == spec.kind:
        found.bind_policy(policy)
        return found
    backend = build_backend(spec, policy=policy, deps=deps, environ=environ)
    if spec.errors:
        logger.warning(
            "runtime 指定的工作区后端「%s」未装配成功（%s），本轮按本地工作目录形态执行",
            spec.backend_id,
            "；".join(spec.errors),
        )
        return None
    return backend


def runtime_status() -> dict[str, Any]:
    """给端点面的总览：清单、活动后端、审计去向、未验收条目"""
    snapshot = REGISTRY.snapshot()
    unverified: list[str] = []
    for backend_id in REGISTRY.ids():
        backend = REGISTRY.get(backend_id)
        if backend is None:
            continue
        probe = backend.probe()
        unverified.extend(f"{backend_id}：{item}" for item in (probe.get("unverified") or []))
    return _jsonable(
        {
            "ok": True,
            "backends": snapshot.get("backends"),
            "activeBackendId": snapshot.get("activeBackendId"),
            "auditFile": str(audit_file()),
            "auditPersistWarning": sum(1 for item in _MEMORY_AUDIT if not item.get("persisted")),
            "unverified": unverified,
            "kindsKnown": list(BACKEND_KINDS),
            "credentialPolicy": "配置只接受 env:/keychain: 引用；字面量当场拒绝，快照与审计只出指纹",
        }
    )
