"""MCP 连接器错误的中文口径（D-39「中文错误映射」）。

为什么单独一个文件：连接器的坏法只有那么十来种，但报错原文来自三家 SDK、
若干 HTTP 库和一堆自研服务器，同一件「端口没人听」可能写成 `ConnectionRefusedError`、
`connection refused`、`10061`、`连接被拒绝`。常驻枢纽（`mcp_session_runtime`）、
注册链路（`mcp_runtime`）和桌面端健康面要的是**同一句人话**，所以分类规则只在这里有一份。

三条不变量：

1. **只翻译，不粉饰**。分类给出的是「这句中文对应的事实」，服务方原话一律跟在后面；
   认不出来就明说「原因见服务方原话」，绝不硬套一个类别把问题说成已经定位。
2. **原文只可能是异常文本**。绝不把 spec 里的 env / headers / 令牌拼进文案——
   这些值会原样进事件流和快照，泄露一次就是凭据泄露。
3. **「要重新授权」的判定比分类更保守**：只有明确的鉴权特征（含裸状态码要带上下文）
   才算鉴权失败。判错方向的代价不对称——把网络故障说成「你去重新授权」会让用户白折腾，
   而把鉴权失败说成网络故障只是少了个引导。
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Tuple

# 鉴权特征：英文是各 SDK 的常见措辞，中文是网关与桌面端自己抛的那几句。
# 只在这张表里加，别再在调用方补一份——两份表会各自漂移，最后变成「有时提示重新授权」。
AUTH_MARKERS = (
    "unauthorized",
    "forbidden",
    "authentication failed",
    "invalid token",
    "invalid api key",
    "invalid_api_key",
    "token expired",
    "expired token",
    "token is expired",
    "missing scope",
    "insufficient scope",
    "未授权",
    "无权限",
    "认证失败",
    "授权已过期",
    "令牌过期",
    "令牌失效",
    "需要重新授权",
)

# 裸状态码只在带上下文的写法里算数：`\b401\b` 会把端口号 401、URL 里的 401 也算成鉴权失败
AUTH_STATUS_RE = re.compile(
    r"(?:http|status|状态码|error\s*code|code)\D{0,6}\b(?:401|403)\b|\b(?:401|403)\b\D{0,12}"
    r"(?:unauthorized|forbidden)",
    re.IGNORECASE,
)

# 鉴权里再分一刀：403 是「拿到了令牌但范围不够」，让用户去改授权范围而不是重输令牌
_FORBIDDEN_MARKERS = ("forbidden", "insufficient scope", "missing scope", "权限不足", "无权限")

# 其它类别：(类别, 中文事实, 用户下一步)。按顺序匹配，先命中的算数，所以
# 「启动命令找不到」要排在「端口没人听」前面——npx 拉不到包时那句报错里也带 refused。
_RULES: Tuple[Tuple[str, str, str, Tuple[str, ...]], ...] = (
    (
        "not_found",
        "启动命令找不到：这台机器上没有连接器要的可执行程序",
        "先在终端装好它（例如 npm 安装对应的 MCP server 包），或在桌面端改正启动命令",
        ("program not found", "no such file or directory", "no such file", "file not found",
         "filenotfounderror", "not recognized as an internal or external command",
         "不是内部或外部命令", "can't execute", "cannot find", "executable file not found",
         "找不到命令", "系统找不到指定的文件", "winerror 2", "没有这个文件", "npx 无法",
         "command not found"),
    ),
    (
        "refused",
        "端口没人听：地址是对的，但那个端口上没有服务在跑",
        "确认服务已经启动、端口没写错；远程连接器还要确认服务商给的接入地址是否过期",
        ("connection refused", "econnrefused", "connection aborted", "10061",
         "no connection could be made", "连接被拒绝", "拒绝连接", "无法连接", "连接不上"),
    ),
    (
        "dns",
        "域名解析不了：这台机器问不到那个地址在哪",
        "检查网络与代理设置；离线时远程连接器一定连不上，本地 stdio 连接器不受影响",
        ("getaddrinfo failed", "getaddrinfo", "name resolution", "nodename nor servname",
         "no such host", "11001", "server could not be found", "domain not found",
         "域名解析", "无法解析主机", "网络不可达", "network is unreachable", "network unreachable"),
    ),
    (
        "timeout",
        "超时：连接器在时限内没有答完",
        "多半是服务方慢或卡住；可以在桌面端把这个连接器的超时调长，或先关掉它",
        ("timed out", "timeout", "timeouterror", "read timeout", "deadline exceeded",
         "context deadline", "请求超时", "连接超时", "响应超时"),
    ),
    (
        "tls",
        "加密通道没建起来：证书或协议版本对不上",
        "常见于走了代理或自签证书；确认代理没有中间人拦截，或按服务商要求配置证书",
        ("ssl", "certificate", "cert", "tls", "handshake failure", "证书", "证书已过期",
         "unable to verify"),
    ),
    (
        "crashed",
        "服务方进程提前退出：连接建立到一半就对上了断口",
        "看连接器自己的日志（stdio 连接器一般打在 stderr）；反复退出的话先停用这个连接器",
        ("process exited", "exit code", "broken pipe", "closed before", "eof occurred",
         "end of file", "stream closed", "peer closed connection", "connection closed",
         "子进程已退出", "进程已退出", "管道已结束", "connection reset"),
    ),
    (
        "protocol",
        "协议对不上：对方回的不是合法的 MCP / JSON-RPC",
        "确认接入地址指向 MCP 端点（不是普通 API 根路径），或把服务商的接入文档发我核对",
        ("invalid json", "json parse", "parse error", "not a valid", "method not found",
         "method not implemented", "invalid request", "unexpected content type",
         "not acceptable", "protocol version", "协议"),
    ),
    (
        "rate_limited",
        "被限流：对方说请求太快了",
        "不用手工干预，常驻枢纽会自动退避重试；还在限流就降低这个连接器的使用频率",
        ("too many requests", "rate limit", "ratelimit", "retry after", "限流",
         "请求过于频繁"),
    ),
)

#: 需要「去重新授权」的类别（其余类别靠退避重试自愈）
REAUTH_KINDS = ("auth", "forbidden")

#: 认不出来时用的类别：仍然如实说「原因见服务方原话」
UNKNOWN_KIND = "unknown"

_SENTENCES = {kind: (fact, next_step) for kind, fact, next_step, _markers in _RULES}
_SENTENCES["auth"] = ("未授权：令牌无效、已过期或已被撤销", "请在桌面端重新授权（或换一枚有效令牌）后重试")
_SENTENCES["forbidden"] = (
    "权限不够：连上了，但当前授权的范围不允许做这件事",
    "请在桌面端重新授权并勾上需要的权限范围（scope）",
)
_SENTENCES[UNKNOWN_KIND] = ("连接器没能连上：原因见服务方原话", "照着原话排查；改好配置后会立刻放行")

ALL_KINDS = tuple(_SENTENCES)


def _text(raw: Any) -> str:
    """把异常（或纯字符串）压成一行小写文本用于匹配"""
    if isinstance(raw, BaseException):
        # TimeoutError 的 str() 是空的：类别要靠类型补，不靠这段文本
        text = f"{type(raw).__name__}: {raw}"
    else:
        text = str(raw or "")
    return " ".join(text.split()).lower()


def is_auth_text(text: str) -> bool:
    """这段文本是否是「鉴权失败」——判定只此一份，分类与退避都读它"""
    if any(marker in text for marker in AUTH_MARKERS):
        return True
    return bool(AUTH_STATUS_RE.search(text))


def needs_reauth(exc: BaseException) -> bool:
    """这条错误是否意味着「要重新授权」而不是「连不上」"""
    return is_auth_text(_text(exc))


def classify(raw: Any) -> str:
    """给一条错误定类别（机读键，供健康面着色与桌面端选文案）

    TimeoutError / asyncio.TimeoutError 的文本是空的，所以先看类型再匹配文本。
    """
    text = _text(raw)
    if is_auth_text(text):
        return "forbidden" if any(m in text for m in _FORBIDDEN_MARKERS) else "auth"
    if isinstance(raw, BaseException) and isinstance(raw, TimeoutError):
        return "timeout"
    for kind, _sentence, _advice, markers in _RULES:
        if any(marker in text for marker in markers):
            return kind
    return UNKNOWN_KIND


def sentence(kind: str) -> str:
    """这句话的中文事实描述（不带原话）"""
    return _SENTENCES.get(kind, _SENTENCES[UNKNOWN_KIND])[0]


def advice(kind: str) -> str:
    """这句话的下一步（不带原话）"""
    return _SENTENCES.get(kind, _SENTENCES[UNKNOWN_KIND])[1]


def describe(kind: str) -> str:
    """中文事实 + 下一步（不含原话）：给只需要一句文案的调用方"""
    fact, next_step = _SENTENCES.get(kind, _SENTENCES[UNKNOWN_KIND])
    return f"{fact}（{next_step}）"


def brief(raw: Any, limit: int = 200) -> str:
    """服务方原话压成一行；空文本（TimeoutError 那种）就不硬造一句"""
    if isinstance(raw, BaseException):
        text = str(raw)
    else:
        text = str(raw or "")
    text = " ".join(text.split())
    if len(text) > limit:
        return text[:limit] + "…"
    return text


def explain(raw: Any, *, limit: int = 200) -> str:
    """一条错误 → 「中文事实（下一步）｜服务方原话：xxx」

    原话永远留着：翻译可能不准，但用户和服务商对话时靠的是那句话。
    """
    kind = classify(raw)
    out = describe(kind)
    original = brief(raw, limit)
    if original:
        out = f"{out}｜服务方原话：{original}"
    return out


def kinds_matching(texts: Iterable[str]) -> dict:
    """批量分类（自测与诊断台用）：{原文: 类别}，只走纯函数，不碰任何连接"""
    return {str(t): classify(str(t)) for t in texts or []}
