"""D-13（#140）浏览器上下文接力：**受控会话内**的标签页与登录态复用边界。

台账 D-13 的方案第二句是「浏览器接力先做受控会话内的标签页与登录态复用，明确隔离边界」。
本模块只落**边界与授权判定**这一半，不做任何浏览器动作：

- 谁能在哪个受控浏览器里接哪些标签页、复用哪些域的登录态、什么时候作废 —— 在这里判；
- CDP 连接、开/切/关标签页、快照与点击一律在 ``engine/browser_use.py`` 与 W5 的
  ``/api/browser`` 车道里（本模块**不调用**它，只借用它的 URL 归一，避免第二套协议白名单）；
- 引擎没有 OS 权限，也拿不到浏览器进程：**标签页与采集现场由调用方交上来**，
  本模块负责回答「这件事允不允许」，不负责「这件事怎么做」，也不假装自己枚举过标签页。

三条不许越过的隔离边界（写死在判定里，各有独立 code 与独立文案）：

1. **不碰用户日常浏览器。** 接力只能复用受控浏览器自己的独立配置目录；任何指向
   日常浏览器配置目录（Chrome / Edge 的 ``User Data``、Firefox 的 ``Profiles`` 等）的
   请求一律拒，且理由点名「不碰用户日常浏览器的 Cookie 与登录态」——这句与
   ``browser_use`` 文件头那条边界同源，两处不许各说一套。
2. **跨工作区不复用。** 授权是**按工作区 + 域**给的；换一个工作区问同一个域，
   答案是不行，需要新的一次授权。
3. **登录态本身永不进这里。** 只记「哪个域在授权集合里」与配置目录的指纹，
   **不记 Cookie、不记令牌、不记表单正文**；审计里也只有域名与计数。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

try:  # URL 归一复用执行面那一份（只允许 http/https），不另立第二套协议白名单
    import browser_use as bu
except Exception:  # noqa: BLE001
    bu = None  # type: ignore[assignment]

try:
    import data_paths as dp
except Exception:  # noqa: BLE001
    dp = None  # type: ignore[assignment]


# ── 上限与默认存活 ──────────────────────────────────────────────

RELAY_TTL_MINUTES = 60
MAX_GRANTS = 32
MAX_HOSTS_PER_GRANT = 32
MAX_TABS_PER_GRANT = 40
MAX_TAB_TITLE_CHARS = 120

# ── 拒绝来源：四类，各有各的文案（不许合并成一句）────────────────

SOURCE_CONTROLLED_BOUNDARY = "controlled_boundary"
SOURCE_GRANT = "grant"
SOURCE_BROWSER_POLICY = "browser_policy"
SOURCE_SESSION = "session"

SOURCE_LABELS = {
    SOURCE_CONTROLLED_BOUNDARY: "受控浏览器边界（不碰日常浏览器）",
    SOURCE_GRANT: "本次接力授权（工作区 / 域 / 有效期 / 撤销）",
    SOURCE_BROWSER_POLICY: "浏览器档位",
    SOURCE_SESSION: "受控会话现场（浏览器没在跑就没有可接力的东西）",
}

CODE_DAILY_PROFILE = "RELAY_DAILY_BROWSER_PROFILE"
CODE_PROFILE_UNKNOWN = "RELAY_PROFILE_NOT_MATCHING_SESSION"
CODE_NO_SESSION = "RELAY_NO_CONTROLLED_SESSION"
CODE_POLICY_DISABLED = "RELAY_BROWSER_POLICY_DISABLED"
CODE_NOT_GRANTED = "RELAY_NOT_GRANTED"
CODE_HOST_NOT_GRANTED = "RELAY_HOST_NOT_IN_GRANT"
CODE_WORKSPACE_MISMATCH = "RELAY_WORKSPACE_MISMATCH"
CODE_GRANT_EXPIRED = "RELAY_GRANT_EXPIRED"
CODE_GRANT_REVOKED = "RELAY_GRANT_REVOKED"
CODE_GRANT_NOT_FOUND = "RELAY_GRANT_NOT_FOUND"
CODE_TABS_OVERFLOW = "RELAY_TAB_LIMIT"
#: 一次授权给的域名数超限：与「标签页超限」不是一件事，不许共用一枚 code
CODE_HOSTS_OVERFLOW = "RELAY_HOST_LIMIT"
CODE_BAD_URL = "RELAY_URL_REJECTED"
#: 调用方要的东西不在这三件里：这是「参数没给对」（400），不是「没授权」（403），不借 NOT_GRANTED 那枚
CODE_WANT_INVALID = "RELAY_WANT_INVALID"

#: 日常浏览器配置目录的特征片段（大小写不敏感，按平台分隔符归一后再比）。
#: 命中即拒——这条是「明确隔离边界」里最硬的一句。
DAILY_PROFILE_MARKERS = (
    "user data",  # Chrome / Edge / Chromium
    "google/chrome",
    "microsoft/edge",
    "mozilla/firefox",
    "profiles",  # Firefox 的 Profiles 目录
    "safari",
    "opera software",
    "browser_vpn",
)

# ── 结论 ────────────────────────────────────────────────────────


@dataclass
class RelayDecision:
    """一次接力请求的判定。``source`` 决定界面选哪句文案，``code`` 决定审计怎么归因。"""

    allowed: bool = False
    reason: str = ""
    source: str = ""
    code: str = ""
    grant_id: str = ""
    profile_dir: str = ""
    host: str = ""

    def to_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "deniedBy": self.source,
            "deniedByLabel": SOURCE_LABELS.get(self.source, self.source) if not self.allowed else "",
            "code": self.code,
            "grantId": self.grant_id,
            "host": self.host,
        }


def _brief(text: object, limit: int = 160) -> str:
    flat = " ".join(str(text or "").split())
    # 省略号也占一位：说好「最长 limit 字」就不能交付 limit+1 字
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)] + "…"


def normalize_profile(raw: object) -> str:
    """把配置目录路径归一成可比较的小写形态（反斜杠换正斜杠，压掉重复分隔符）。

    只做「同一台机器上同一路径的两种写法要判成同一个」这一件事；
    ``C:/x//y`` 与 ``c:/x/y`` 归一后相等， scheme 前缀也会一起压（本函数只用于本地路径）。
    """
    value = str(raw or "").strip().replace("\\", "/").lower()
    while "//" in value:
        value = value.replace("//", "/")
    return value.rstrip("/")


def daily_profile_hit(profile_dir: str) -> str:
    """返回命中的日常浏览器特征片段（没命中回空串）。"""
    normalized = normalize_profile(profile_dir)
    for marker in DAILY_PROFILE_MARKERS:
        if marker in normalized:
            return marker
    return ""


def profile_fingerprint(profile_dir: str) -> str:
    """配置目录指纹：审计与回话里只出现这个，不出现完整路径（路径本身带用户名）。"""
    return hashlib.sha256(normalize_profile(profile_dir).encode("utf-8")).hexdigest()[:12]


def host_of(url: str) -> tuple[str, str]:
    """从 URL 取主机名；协议白名单复用 ``browser_use.normalize_url``（只放 http/https）。"""
    target = str(url or "").strip()
    if bu is not None:
        _clean, error = bu.normalize_url(target)
        if error:
            return "", error
        if not target.lower().startswith(("http://", "https://")):
            return "", "受控浏览器只允许打开 http/https 页面"
        rest = target.split("://", 1)[1]
    else:
        # browser_use 缺件时仍然不能放行任意协议：宁可拒掉也不开第二套白名单
        if not target.lower().startswith(("http://", "https://")):
            return "", "受控浏览器只允许 http/https 页面（URL 归一件不可用，已按最保守处理）"
        rest = target.split("://", 1)[1]
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1].split(":", 1)[0].strip(".").lower()
    if not host:
        return "", "URL 缺少主机名"
    return host, ""


# ── 授权 ────────────────────────────────────────────────────────


@dataclass
class RelayGrant:
    """一次「用户授权受控浏览器会话可被接力」的现场。"""

    grant_id: str
    workspace_id: str
    profile_dir: str
    hosts: list[str] = field(default_factory=list)
    task_ids: list[str] = field(default_factory=list)
    tabs: list[dict] = field(default_factory=list)
    created_at: float = 0.0
    expires_at: float = 0.0
    revoked: bool = False
    revoke_note: str = ""
    #: 本次授权允许的复用范围：只到「同域复用登录态 / 复用已登记的标签页」，
    #: 不含新的浏览器动作（那些归 W5）
    reuse_login: bool = True
    reuse_tabs: bool = True

    @property
    def active(self) -> bool:
        return not self.revoked

    def is_expired(self, now: float) -> bool:
        return now >= self.expires_at

    def to_row(self, *, now: float = 0.0) -> dict:
        return {
            "grantId": self.grant_id,
            "workspaceId": self.workspace_id,
            "profileFingerprint": profile_fingerprint(self.profile_dir),
            "hosts": list(self.hosts),
            "taskIds": list(self.task_ids),
            "tabCount": len(self.tabs),
            "tabs": list(self.tabs),
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "expired": self.is_expired(now) if now else False,
            "revoked": self.revoked,
            "revokeNote": self.revoke_note,
            "reuseLogin": self.reuse_login,
            "reuseTabs": self.reuse_tabs,
            "note": (
                "这里只有域名与配置目录指纹：Cookie、令牌、表单正文一概不记，"
                "登录态复用指的是「受控 profile 里已经有」，不是「本模块持有」"
            ),
        }


class RelayRegistry:
    """接力授权的进程内登记表 + 决策审计（与快照同一口径：审计不含正文与凭据）。"""

    def __init__(
        self,
        *,
        clock: Callable[[], float] | None = None,
        ttl_minutes: int = RELAY_TTL_MINUTES,
    ) -> None:
        self._grants: dict[str, RelayGrant] = {}
        self._clock = clock or time.time
        self.ttl_minutes = max(1, int(ttl_minutes))
        self._decisions: list[dict] = []

    # 现场

    @staticmethod
    def session_view(runtime: dict | None) -> dict:
        """从 ``runtime.browserUse`` 里读受控会话现场（不猜、不补默认值）。"""
        block = (runtime or {}).get("browserUse") if isinstance(runtime, dict) else None
        if not isinstance(block, dict):
            # 缺段也要回**同一组键**：调用方直接索引，键不齐就是 KeyError 崩在「没有会话」这条正常路径上
            return {
                "present": False,
                "running": False,
                "persisted": False,
                "policy": "",
                "browser": "",
                "profileDir": "",
                "profileFingerprint": "",
                "reason": "本轮 runtime 没有 browserUse 段：没有受控浏览器会话",
            }
        profile = str(block.get("profileDir") or "").strip()
        return {
            "present": bool(profile),
            "running": bool(block.get("running")),
            "persisted": bool(block.get("persisted")),
            "policy": str(block.get("policy") or "").strip(),
            "browser": _brief(block.get("browser") or "", 60),
            "profileDir": profile,
            "profileFingerprint": profile_fingerprint(profile) if profile else "",
            "reason": "" if profile else "runtime.browserUse 没有 profileDir：不知道可接力的配置目录是哪个",
        }

    def _policy_denial(self, policy: str) -> RelayDecision | None:
        if policy == "disabled":
            return RelayDecision(
                allowed=False,
                source=SOURCE_BROWSER_POLICY,
                code=CODE_POLICY_DISABLED,
                reason=(
                    "浏览器档位为「已禁用」：这一档不注册任何浏览器能力，"
                    "接力自然无来源（改档位请去「设置 → 电脑与浏览器」，不在本模块）"
                ),
            )
        if policy not in ("ask_every_time", "auto"):
            return RelayDecision(
                allowed=False,
                source=SOURCE_BROWSER_POLICY,
                code=CODE_POLICY_DISABLED,
                reason=f"浏览器档位读数「{_brief(policy, 40) or '空'}」不在受支持表里，按禁用处理",
            )
        return None

    # 授权

    def grant(
        self,
        *,
        runtime: dict | None = None,
        workspace_id: str = "",
        hosts: list[str] | None = None,
        task_id: str = "",
        profile_dir: str = "",
    ) -> tuple[RelayDecision, dict]:
        """开一次接力授权。授权本身也要过边界：日常浏览器配置目录不配被授权。"""
        session = self.session_view(runtime)
        target_profile = profile_dir or session["profileDir"]
        denial = self._boundary_denial(target_profile)
        if denial is not None:
            self._record("grant", denial, workspace_id=workspace_id)
            return denial, {}
        view = session
        if target_profile and view["profileDir"] and normalize_profile(target_profile) != normalize_profile(
            view["profileDir"]
        ):
            denial = RelayDecision(
                allowed=False,
                source=SOURCE_CONTROLLED_BOUNDARY,
                code=CODE_PROFILE_UNKNOWN,
                reason=(
                    "请求指定的配置目录与本轮受控会话的目录不是同一个（指纹对不上）："
                    "接力只跟着受控会话走，不跟着请求里的路径走"
                ),
            )
            self._record("grant", denial, workspace_id=workspace_id)
            return denial, {}
        if not view["present"]:
            denial = RelayDecision(
                allowed=False,
                source=SOURCE_SESSION,
                code=CODE_NO_SESSION,
                reason=view["reason"] + "；没有受控会话就没有可接力的登录态，授权不成立",
            )
            self._record("grant", denial, workspace_id=workspace_id)
            return denial, {}
        policy_denial = self._policy_denial(view["policy"])
        if policy_denial is not None:
            self._record("grant", policy_denial, workspace_id=workspace_id)
            return policy_denial, {}

        cleaned: list[str] = []
        for raw in hosts or []:
            host, error = self._host_from_text(str(raw or ""))
            if error:
                denial = RelayDecision(
                    allowed=False,
                    source=SOURCE_GRANT,
                    code=CODE_BAD_URL,
                    reason=f"授权域名「{_brief(raw, 60)}」不合法：{error}",
                )
                self._record("grant", denial, workspace_id=workspace_id)
                return denial, {}
            if host not in cleaned:
                cleaned.append(host)
        if not cleaned:
            denial = RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_NOT_GRANTED,
                reason="这次授权没有给出任何域名：接力必须按域授权，不给域就等于不授权",
            )
            self._record("grant", denial, workspace_id=workspace_id)
            return denial, {}
        if len(cleaned) > MAX_HOSTS_PER_GRANT:
            denial = RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_HOSTS_OVERFLOW,
                reason=f"一次授权最多 {MAX_HOSTS_PER_GRANT} 个域名，本次给了 {len(cleaned)} 个",
            )
            self._record("grant", denial, workspace_id=workspace_id)
            return denial, {}

        now = self._clock()
        row = RelayGrant(
            grant_id=uuid.uuid4().hex[:12],
            workspace_id=str(workspace_id or "")[:80],
            profile_dir=str(view["profileDir"]),
            hosts=cleaned,
            task_ids=[str(task_id)[:64]] if task_id else [],
            created_at=now,
            expires_at=now + self.ttl_minutes * 60.0,
        )
        self._grants[row.grant_id] = row
        self._evict_overflow()
        decision = RelayDecision(
            allowed=True,
            source="",
            code="",
            grant_id=row.grant_id,
            profile_dir=row.profile_dir,
            reason=f"已授权 {len(cleaned)} 个域名的接力，存活 {self.ttl_minutes} 分钟",
        )
        self._record("grant", decision, workspace_id=row.workspace_id)
        return decision, row.to_row(now=now)

    @staticmethod
    def _host_from_text(raw: str) -> tuple[str, str]:
        """授权项既接受纯域名也接受 URL，两种写法最后都走**同一个** host 归一。

        裸域名先补 ``http://`` 再进白名单：早期版本对「不含 ``://``」直接放行整串，
        ``javascript:alert(1)`` 这类协议串会被当成域名收进授权集合 —— 授权清单里出现
        一个不是域名的串，界面上却写着「已授权 N 个域名」，那是假账。
        """
        value = str(raw or "").strip().lower()
        if not value:
            return "", "授权项为空"
        if "://" not in value:
            if any(char.isspace() for char in value):
                return "", "授权项只能是域名或完整 URL"
            if "/" in value or "?" in value or "#" in value:
                return "", "授权项按域名给，不给路径（带路径会被静默丢掉，不如一开始就拒）"
            if ":" in value:
                return "", "授权项里出现冒号：按协议或端口处理，域名不该有这两种写法"
            value = f"http://{value}"
        return host_of(value)

    def _boundary_denial(self, profile_dir: str) -> RelayDecision | None:
        marker = daily_profile_hit(profile_dir)
        if marker:
            return RelayDecision(
                allowed=False,
                source=SOURCE_CONTROLLED_BOUNDARY,
                code=CODE_DAILY_PROFILE,
                profile_dir=profile_dir,
                reason=(
                    f"请求指向的路径命中日常浏览器配置目录特征「{marker}」，一律拒绝："
                    "接力只复用受控浏览器自己的独立配置目录，**不碰用户日常浏览器的 "
                    "Cookie 与登录态**（与 browser_use 那条边界同一句口径）"
                ),
            )
        return None

    # 判定

    def decide(
        self,
        *,
        url: str = "",
        grant_id: str = "",
        workspace_id: str = "",
        runtime: dict | None = None,
        want: str = "tabs",
    ) -> RelayDecision:
        """这次能不能复用已授权会话里的标签页 / 登录态。"""
        host, url_error = host_of(url) if url else ("", "")
        if url and url_error:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_BAD_URL,
                host="",
                reason=f"目标地址没过协议白名单：{url_error}",
            )
        row = self.get(grant_id)
        if row is None:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_GRANT_NOT_FOUND,
                host=host,
                reason=(
                    "没有这条接力授权（id 不存在或从没授成功）。接力不会因为「用户以前授权过别的"
                    "会话」就自动放行，必须按这一条来"
                ),
            )
        denial = self._boundary_denial(row.profile_dir)
        if denial is not None:
            denial.host = host
            return denial
        if row.revoked:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_GRANT_REVOKED,
                grant_id=row.grant_id,
                host=host,
                reason=f"这条授权已撤销（{row.revoke_note or '未注明原因'}）：撤销立即生效，不看剩余有效期",
            )
        if row.is_expired(self._clock()):
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_GRANT_EXPIRED,
                grant_id=row.grant_id,
                host=host,
                reason=f"这条授权已过期（存活 {self.ttl_minutes} 分钟）：需要用户重新授一次",
            )
        if row.workspace_id and str(workspace_id or "") != row.workspace_id:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_WORKSPACE_MISMATCH,
                grant_id=row.grant_id,
                host=host,
                reason=(
                    "跨工作区不复用：这条授权是给工作区"
                    f"「{_brief(row.workspace_id, 40)}」的，本轮问的是"
                    f"「{_brief(workspace_id, 40) or '未声明工作区'}」；"
                    "换工作区要用户再授一次，不做静默放大"
                ),
            )
        if host and host not in row.hosts:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_HOST_NOT_GRANTED,
                grant_id=row.grant_id,
                host=host,
                reason=(
                    f"域名「{_brief(host, 60)}」不在本次授权集合里（已授 "
                    f"{len(row.hosts)} 个域）：授权按域给，不给就是不给"
                ),
            )
        if want == "login" and not row.reuse_login:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_NOT_GRANTED,
                grant_id=row.grant_id,
                host=host,
                reason="本次授权没有开放登录态复用（只授了标签页接力）",
            )
        if want == "tabs" and not row.reuse_tabs:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_NOT_GRANTED,
                grant_id=row.grant_id,
                host=host,
                reason="本次授权没有开放标签页接力（只授了登录态复用）",
            )

        session = self.session_view(runtime)
        if runtime is not None:
            if not session["present"]:
                return RelayDecision(
                    allowed=False,
                    source=SOURCE_SESSION,
                    code=CODE_NO_SESSION,
                    grant_id=row.grant_id,
                    host=host,
                    reason=session["reason"] + "；授权还在，但现场没了",
                )
            if normalize_profile(session["profileDir"]) != normalize_profile(row.profile_dir):
                return RelayDecision(
                    allowed=False,
                    source=SOURCE_CONTROLLED_BOUNDARY,
                    code=CODE_PROFILE_UNKNOWN,
                    grant_id=row.grant_id,
                    host=host,
                    reason=(
                        "本轮受控会话的配置目录与被授权的那份不是同一个（指纹对不上）："
                        "授权跟着原来那份目录，不跟着新目录走"
                    ),
                )
            if session["policy"] not in ("ask_every_time", "auto"):
                # 判定这一步自己说一句：与「开授权时就被档位拒」同源但不是同一件事，两句都指向界面不同的提示
                return RelayDecision(
                    allowed=False,
                    source=SOURCE_BROWSER_POLICY,
                    code=CODE_POLICY_DISABLED,
                    grant_id=row.grant_id,
                    host=host,
                    reason=(
                        f"授权本身还在，但本轮浏览器档位读数是「{_brief(session['policy'], 40) or '空'}」，"
                        "按禁用处理：这一档不注册任何浏览器能力，接力自然无来源"
                        "（改档位去「设置 → 电脑与浏览器」，不在本模块）"
                    ),
                )
            if not session["running"]:
                return RelayDecision(
                    allowed=False,
                    source=SOURCE_SESSION,
                    code=CODE_NO_SESSION,
                    grant_id=row.grant_id,
                    host=host,
                    reason="受控浏览器当前不在运行：没有活着的会话可以接力（授权本身仍然有效）",
                )

        return RelayDecision(
            allowed=True,
            grant_id=row.grant_id,
            profile_dir=row.profile_dir,
            host=host,
            reason=(
                f"可以复用：域「{_brief(host, 60) or '未指定'}」在本次授权集合里，"
                "受控会话在线（本模块只判边界，动作在浏览器执行侧）"
            ),
        )

    def register_tabs(self, grant_id: str, tabs: list[dict] | None) -> tuple[RelayDecision, dict]:
        """把调用方（桌面壳 / W5）交上来的标签页登记进授权，供后续按域判定与界面展示。"""
        row = self.get(grant_id)
        if row is None:
            return (
                RelayDecision(
                    allowed=False,
                    source=SOURCE_GRANT,
                    code=CODE_GRANT_NOT_FOUND,
                    reason="没有这条接力授权，标签页无处可登记",
                ),
                {},
            )
        incoming = []
        for item in tabs or []:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "")
            host = ""
            if raw_url:
                host, error = host_of(raw_url)
                if error:
                    return (
                        RelayDecision(
                            allowed=False,
                            source=SOURCE_GRANT,
                            code=CODE_BAD_URL,
                            grant_id=row.grant_id,
                            reason=f"标签页地址不合法：{error}",
                        ),
                        {},
                    )
            if host and host not in row.hosts:
                return (
                    RelayDecision(
                        allowed=False,
                        source=SOURCE_GRANT,
                        code=CODE_HOST_NOT_GRANTED,
                        grant_id=row.grant_id,
                        host=host,
                        reason=(
                            f"标签页所在域「{_brief(host, 60)}」不在本次授权集合里："
                            "登记也要按授权走，不然登记本身就成了越界"
                        ),
                    ),
                    {},
                )
            incoming.append(
                {
                    "tabId": _brief(item.get("tabId") or "", 64),
                    "host": host,
                    "title": _brief(item.get("title") or "", MAX_TAB_TITLE_CHARS),
                    "active": bool(item.get("active")),
                }
            )
        if len(incoming) > MAX_TABS_PER_GRANT:
            return (
                RelayDecision(
                    allowed=False,
                    source=SOURCE_GRANT,
                    code=CODE_TABS_OVERFLOW,
                    grant_id=row.grant_id,
                    reason=f"一条授权最多登记 {MAX_TABS_PER_GRANT} 个标签页，本次交了 {len(incoming)} 个",
                ),
                {},
            )
        row.tabs = incoming
        decision = RelayDecision(
            allowed=True,
            grant_id=row.grant_id,
            reason=f"已登记 {len(incoming)} 个标签页（只留域名与标题摘要，不留 URL 查询串）",
        )
        self._record("register_tabs", decision, workspace_id=row.workspace_id)
        return decision, row.to_row(now=self._clock())

    def add_task(self, grant_id: str, task_id: str) -> RelayDecision:
        row = self.get(grant_id)
        if row is None:
            return RelayDecision(
                allowed=False,
                source=SOURCE_GRANT,
                code=CODE_GRANT_NOT_FOUND,
                reason="没有这条接力授权",
            )
        token = str(task_id or "")[:64]
        if token and token not in row.task_ids:
            row.task_ids.append(token)
        return RelayDecision(allowed=True, grant_id=row.grant_id, reason=f"任务 {token} 已并入这条接力")

    def revoke(self, grant_id: str, *, reason: str = "") -> tuple[RelayDecision, dict]:
        row = self.get(grant_id)
        if row is None:
            return (
                RelayDecision(
                    allowed=False,
                    source=SOURCE_GRANT,
                    code=CODE_GRANT_NOT_FOUND,
                    reason="没有这条接力授权：撤销不回溯，也不会因为撤销而放行别的会话",
                ),
                {},
            )
        row.revoked = True
        row.revoke_note = _brief(reason or "用户撤销", 120)
        decision = RelayDecision(
            allowed=True,
            grant_id=row.grant_id,
            reason=f"已撤销（{row.revoke_note}）：撤销立即生效，界面与审计都按这条走",
        )
        self._record("revoke", decision, workspace_id=row.workspace_id)
        return decision, row.to_row(now=self._clock())

    def revoke_workspace(self, workspace_id: str, *, reason: str = "") -> list[str]:
        revoked = []
        for row in self._grants.values():
            if row.workspace_id == str(workspace_id or "") and not row.revoked:
                self.revoke(row.grant_id, reason=reason or "按工作区整体撤销")
                revoked.append(row.grant_id)
        return revoked

    # 读取与内部件

    def get(self, grant_id: str) -> RelayGrant | None:
        return self._grants.get(str(grant_id or "").strip())

    def list(self, *, workspace_id: str = "") -> list[dict]:
        now = self._clock()
        rows = [
            row.to_row(now=now)
            for row in self._grants.values()
            if not workspace_id or row.workspace_id == workspace_id
        ]
        return sorted(rows, key=lambda item: item["createdAt"], reverse=True)

    def _evict_overflow(self) -> None:
        if len(self._grants) <= MAX_GRANTS:
            return
        ordered = sorted(self._grants.values(), key=lambda item: item.created_at)
        for row in ordered[: len(ordered) - MAX_GRANTS]:
            row.revoked = True
            row.revoke_note = f"接力授权总数超过 {MAX_GRANTS} 条，最早的一条按撤销处理"

    def _audit_file(self):
        if dp is None:
            return None
        try:
            folder = Path(dp.engine_home()) / "snapshots"
            folder.mkdir(parents=True, exist_ok=True)
            return folder / "relay-decisions.jsonl"
        except Exception:  # noqa: BLE001
            return None

    def _record(self, action: str, decision: RelayDecision, *, workspace_id: str = "") -> None:
        """接力审计：只有域名、指纹与结论，**没有 Cookie、没有令牌、没有 URL 查询串**。"""
        row = {
            "ts": round(self._clock(), 3),
            "action": action,
            "allowed": decision.allowed,
            "code": decision.code,
            "deniedBy": decision.source,
            "grantId": decision.grant_id,
            "workspaceId": str(workspace_id or "")[:80],
            "host": decision.host,
            "profileFingerprint": profile_fingerprint(decision.profile_dir) if decision.profile_dir else "",
            "reason": _brief(decision.reason, 240),
        }
        self._decisions.append(row)
        target = self._audit_file()
        if target is not None:
            try:
                with target.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            except Exception:  # noqa: BLE001
                pass

    def decisions(self, *, grant_id: str = "", action: str = "", limit: int = 50, offset: int = 0) -> dict:
        rows = [
            row
            for row in self._decisions
            if (not grant_id or row.get("grantId") == grant_id) and (not action or row.get("action") == action)
        ]
        total = len(rows)
        page = rows[offset : offset + limit]
        return {
            "total": total,
            "items": page,
            "hasMore": offset + len(page) < total,
            "note": "" if page or total == 0 else "这一页没有条目（过滤后为空，或 offset 超过了总数）",
        }

    def status(self) -> dict:
        now = self._clock()
        return {
            "grants": len(self._grants),
            "active": sum(1 for row in self._grants.values() if not row.revoked and not row.is_expired(now)),
            "ttlMinutes": self.ttl_minutes,
            "maxGrants": MAX_GRANTS,
            "maxHostsPerGrant": MAX_HOSTS_PER_GRANT,
            "maxTabsPerGrant": MAX_TABS_PER_GRANT,
            "denialSources": [{"value": key, "label": value} for key, value in SOURCE_LABELS.items()],
            "codes": [
                CODE_DAILY_PROFILE,
                CODE_PROFILE_UNKNOWN,
                CODE_NO_SESSION,
                CODE_POLICY_DISABLED,
                CODE_NOT_GRANTED,
                CODE_HOST_NOT_GRANTED,
                CODE_WORKSPACE_MISMATCH,
                CODE_GRANT_EXPIRED,
                CODE_GRANT_REVOKED,
                CODE_GRANT_NOT_FOUND,
                CODE_TABS_OVERFLOW,
                CODE_HOSTS_OVERFLOW,
                CODE_BAD_URL,
                CODE_WANT_INVALID,
            ],
            "dailyProfileMarkers": list(DAILY_PROFILE_MARKERS),
            "unverified": [
                "本模块不采集标签页、不读 Cookie、不发起 CDP：现场由调用方交上来，"
                "真实受控浏览器会话下的接力行为未验收（桌面壳 IPC 未接线）",
                "「复用登录态」指的是受控 profile 里已有那份登录态仍可用，"
                "本模块既不持有也不导出它，因此没有跨机同步登录态这回事",
                "日常浏览器目录的识别靠路径特征，不保证覆盖所有便携版/第三方壳（"
                "命中即拒；未命中也不等于放行——仍要受控会话现场与授权两条都成立）",
            ],
            "note": "浏览器动作（开/切/关标签页、点击、输入）不在本模块，归 W5 的 /api/browser 车道",
        }

    def clear(self) -> None:
        self._grants.clear()
        self._decisions.clear()


REGISTRY = RelayRegistry()


def reset_registry(**kwargs: Any) -> RelayRegistry:
    """测试与装配用的整体重置。"""
    global REGISTRY
    REGISTRY = RelayRegistry(**kwargs)
    return REGISTRY
