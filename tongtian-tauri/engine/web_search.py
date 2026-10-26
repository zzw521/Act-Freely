"""联网搜索（P0）：受控的网页检索工具

配置来自进程环境（开发期可写项目根 `.env`）：

    TT_SEARCH_PROVIDER  searxng | tavily | serper
    TT_SEARCH_URL       searxng 必填（自建实例的 /search 地址）；其余可覆盖默认端点
    TT_SEARCH_API_KEY   tavily / serper 必填
    TT_SEARCH_MOCK      true 时返回本地占位结果，不发真实网络请求（开发规范）

授权不来自配置，而来自 runtime.sandbox 的显式联网授权：

* 未授权、白名单为空、搜索主机不在白名单 → **不注册工具**，原因作为可见说明下发；
* 真实请求沿用 ToolGrantPolicy 的 SSRF 校验与逐跳重定向复核；
* 每次调用发 `tool_audit`（action=network），target 只记主机与查询长度，不记查询正文。

铁律：搜索结果只是搜索源给的摘要，不是原文；没有结果、请求失败、未授权都必须
作为可读事实返回，不能静默返回空列表让模型自由发挥。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from tool_grants import ToolGrantPolicy, USER_AGENT, _CheckedRedirect, _brief

SEARCH_TIMEOUT = 15
MAX_SEARCH_BYTES = 1024 * 1024
MAX_QUERY_CHARS = 300
MAX_HITS = 10
DEFAULT_HITS = 5
MAX_SNIPPET_CHARS = 360
PROVIDERS = ("searxng", "tavily", "serper")
DEFAULT_ENDPOINTS = {
    "tavily": "https://api.tavily.com/search",
    "serper": "https://google.serper.dev/search",
}
MOCK_ENDPOINT = "https://search.mock.local/search"


def _env_bool(name: str) -> bool:
    return str(os.environ.get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def _host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(str(url or "").strip()).hostname or "").lower()
    except Exception:  # noqa: BLE001
        return ""


def _one_line(value, limit: int) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _clean_url(value) -> str:
    raw = str(value or "").strip()
    try:
        parts = urllib.parse.urlsplit(raw)
    except Exception:  # noqa: BLE001
        return ""
    if parts.scheme.lower() not in ("http", "https"):
        return ""
    if not parts.hostname or parts.username or parts.password:
        return ""
    return raw


def _normalize_rows(rows, title_key: str, url_key: str, snippet_key: str) -> list[dict]:
    out: list[dict] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = _clean_url(row.get(url_key))
        if not url:
            continue
        title = _one_line(row.get(title_key), 200) or _host_of(url) or url
        out.append({
            "title": title,
            "url": url,
            "snippet": _one_line(row.get(snippet_key), MAX_SNIPPET_CHARS),
        })
    return out


def parse_tavily(payload) -> list[dict]:
    rows = payload.get("results") if isinstance(payload, dict) else None
    return _normalize_rows(rows, "title", "url", "content")


def parse_serper(payload) -> list[dict]:
    rows = payload.get("organic") if isinstance(payload, dict) else None
    return _normalize_rows(rows, "title", "link", "snippet")


def parse_searxng(payload) -> list[dict]:
    rows = payload.get("results") if isinstance(payload, dict) else None
    return _normalize_rows(rows, "title", "url", "content")


PARSERS = {
    "tavily": parse_tavily,
    "serper": parse_serper,
    "searxng": parse_searxng,
}


def _mock_results(query: str) -> list[dict]:
    quoted = urllib.parse.quote(_one_line(query, 80), safe="")
    return [
        {
            "title": f"Mock 搜索结果 {i}：{_one_line(query, 40)}",
            "url": f"https://example.com/mock/{quoted}/{i}",
            "snippet": "这是本地 Mock 搜索返回的占位内容，用于开发自测，不代表真实网页。",
        }
        for i in (1, 2)
    ]


def _http_payload(
    method: str,
    url: str,
    headers: dict,
    data: bytes | None,
    validate: Callable[[str], str],
):
    """发一次请求并解析 JSON；重定向逐跳复核，响应体设为上限"""
    opener = urllib.request.build_opener(_CheckedRedirect(validate))
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=SEARCH_TIMEOUT) as resp:
            status = getattr(resp, "status", 200)
            content_type = resp.headers.get("Content-Type", "") or ""
            final_url = resp.geturl()
            validate(final_url)
            raw = resp.read(MAX_SEARCH_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"HTTP {exc.code}（{_brief(exc.reason, 80)}）") from exc
    if len(raw) > MAX_SEARCH_BYTES:
        raise RuntimeError(f"响应超过 {MAX_SEARCH_BYTES} 字节")
    mime = content_type.split(";", 1)[0].strip().lower()
    if mime and "json" not in mime and not mime.startswith("text/"):
        raise RuntimeError(f"响应类型 {mime} 不是 JSON/文本")
    charset = "utf-8"
    for piece in content_type.split(";")[1:]:
        key, _, value = piece.strip().partition("=")
        if key.lower() == "charset" and value:
            charset = value.strip().strip('"') or "utf-8"
    try:
        text = raw.decode(charset, errors="replace")
    except LookupError:
        text = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError("搜索源返回的不是合法 JSON") from exc
    return status, payload


@dataclass
class WebSearchRuntime:
    provider: str = ""
    endpoint: str = ""
    api_key: str = ""
    grants: ToolGrantPolicy | None = None
    mock: bool = False
    error: str = ""

    @classmethod
    def from_env(cls, grants: ToolGrantPolicy | None = None) -> "WebSearchRuntime":
        if _env_bool("TT_SEARCH_MOCK"):
            return cls(provider="mock", endpoint=MOCK_ENDPOINT, grants=grants, mock=True)
        provider = str(os.environ.get("TT_SEARCH_PROVIDER") or "").strip().lower()
        endpoint = str(os.environ.get("TT_SEARCH_URL") or "").strip()
        api_key = str(os.environ.get("TT_SEARCH_API_KEY") or "").strip()
        if not provider:
            if endpoint:
                provider = "searxng"
            else:
                return cls(grants=grants)
        if provider not in PROVIDERS:
            return cls(
                provider=provider,
                endpoint=endpoint,
                grants=grants,
                error=f"未知搜索源「{provider}」（支持：{'、'.join(PROVIDERS)}）",
            )
        endpoint = endpoint or DEFAULT_ENDPOINTS.get(provider, "")
        if not endpoint:
            return cls(
                provider=provider,
                grants=grants,
                error="搜索源 searxng 需要配置 TT_SEARCH_URL（自建实例的 /search 地址）",
            )
        if provider in ("tavily", "serper") and not api_key:
            return cls(
                provider=provider,
                endpoint=endpoint,
                grants=grants,
                error=f"搜索源 {provider} 需要配置 TT_SEARCH_API_KEY",
            )
        return cls(
            provider=provider,
            endpoint=endpoint,
            api_key=api_key,
            grants=grants,
        )

    @property
    def configured(self) -> bool:
        return bool(self.provider) and not self.error

    @property
    def ready(self) -> bool:
        return self.configured and self.denial() is None

    def denial(self) -> str | None:
        """为什么不能检索：未配置 / 未授权 / 主机不在白名单 / 策略读取失败"""
        if self.error:
            return self.error
        if not self.provider:
            return "未配置搜索源（TT_SEARCH_PROVIDER）"
        if self.grants is None:
            return "没有沙箱运行时策略，联网搜索不可用"
        if self.grants.error:
            return f"沙箱策略读取失败：{_brief(self.grants.error)}"
        # Mock 不发网络请求，但仍要求显式授权，保证开发/生产同一套注册条件
        if self.mock:
            try:
                parts = urllib.parse.urlsplit(self.endpoint)
            except Exception:  # noqa: BLE001
                return "搜索端点无法解析"
            if parts.scheme.lower() not in ("http", "https"):
                return "搜索端点只允许 http/https"
            if not self.grants.network_enabled:
                return "用户未授权联网获取（联网工具默认关闭）"
            host = _host_of(self.endpoint)
            if not host:
                return "搜索端点缺少主机名"
            if host not in self.grants.network_allowlist:
                allowed = "、".join(self.grants.network_allowlist[:8]) or "（空）"
                return f"搜索主机「{host}」不在联网白名单内（允许：{allowed}）"
            return None
        return self.grants.network_denial(self.endpoint)

    def notes(self) -> list[str]:
        if self.error:
            return [f"网页搜索未启用：{self.error}"]
        if not self.provider:
            return []
        denial = self.denial()
        if denial:
            return [f"网页搜索未注册：{denial}"]
        if self.mock:
            return ["网页搜索使用 Mock 结果（开发自测，不发真实网络请求）"]
        return []

    def system_hint(self) -> str:
        if not self.ready:
            return ""
        if self.mock:
            return (
                "\n\n## 网页搜索（Mock 开发模式）\n"
                "- web_search 返回本地占位结果，不是真实网页；可以验证调用流程，"
                "不能把结果当作事实引用。"
            )
        host = _host_of(self.endpoint) or self.provider
        return (
            "\n\n## 网页搜索（用户已授权联网）\n"
            f"- web_search：通过 {self.provider} 检索公开网页，返回标题、链接与摘要；"
            f"仅访问已授权主机 {host}。结果里的链接若要读全文，目标主机同样必须在联网白名单内，"
            "否则就用摘要回答并说明打不开原文。\n"
            "- 引用网页信息时保留结果中的 URL（形如 [来源: https://...]）；"
            "摘要不是原文，不要把摘要说成原文，也不要编造没有出现在结果里的链接。"
        )

    def _request(self, query: str, top_k: int):
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if self.provider == "searxng":
            sep = "&" if "?" in self.endpoint else "?"
            url = self.endpoint + sep + urllib.parse.urlencode({"q": query, "format": "json"})
            method, data = "GET", None
        elif self.provider == "tavily":
            url, method = self.endpoint, "POST"
            data = json.dumps({
                "api_key": self.api_key,
                "query": query,
                "max_results": top_k,
                "include_answer": False,
                "include_raw_content": False,
            }).encode("utf-8")
            headers["Content-Type"] = "application/json"
        else:  # serper
            url, method = self.endpoint, "POST"
            data = json.dumps({"q": query, "num": top_k}).encode("utf-8")
            headers["Content-Type"] = "application/json"
            headers["X-API-KEY"] = self.api_key

        def validate(next_url: str) -> str:
            if self.grants is None:
                raise PermissionError("缺少联网授权")
            err = self.grants.network_denial(next_url)
            if err:
                raise PermissionError(err)
            return next_url

        return _http_payload(method, url, headers, data, validate)

    def search(self, query: str, top_k: int = DEFAULT_HITS) -> dict:
        provider = self.provider or "（未配置）"
        raw = " ".join(str(query or "").split())
        if not raw:
            return {"provider": provider, "results": [], "notes": ["检索词为空"], "outcome": "denied"}
        text = raw[:MAX_QUERY_CHARS]
        denial = self.denial()
        if denial:
            return {"provider": provider, "results": [], "notes": [denial], "outcome": "denied"}
        try:
            k = int(top_k)
        except Exception:  # noqa: BLE001
            k = DEFAULT_HITS
        k = min(max(k, 1), MAX_HITS)
        if self.mock:
            return {
                "provider": provider,
                "results": _mock_results(text)[:k],
                "notes": ["当前是 Mock 搜索：结果为本地占位内容，不是真实网页"],
                "outcome": "ok",
            }
        try:
            _status, payload = self._request(text, k)
            rows = PARSERS[self.provider](payload)[:k]
        except PermissionError as exc:
            return {
                "provider": provider,
                "results": [],
                "notes": [f"搜索请求被拒绝：{_brief(exc, 200)}"],
                "outcome": "denied",
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "provider": provider,
                "results": [],
                "notes": [f"搜索失败：{_brief(exc, 200)}"],
                "outcome": "failed",
            }
        notes = [] if rows else ["搜索源没有返回可用结果"]
        return {"provider": provider, "results": rows, "notes": notes, "outcome": "ok"}

    def install(
        self,
        toolkit,
        emit: Callable[[str, dict], None] | None = None,
        task_id: str = "",
    ) -> None:
        if not self.ready:
            return

        def web_search(query: str, top_k: int = DEFAULT_HITS):
            """检索公开网页，返回带链接的标题与摘要。

            结果只是搜索源给的摘要；需要核对原文时用 fetch_url 打开结果链接
            （目标主机同样必须在用户的联网白名单内）。

            Args:
                query (`str`):
                    检索词或问题，中英文均可。
                top_k (`int`, optional):
                    返回结果数，默认 5，最多 10。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            result = self.search(query, top_k)
            rows = result.get("results") or []
            notes = result.get("notes") or []
            host = _host_of(self.endpoint) or self.provider
            target = f"{host}（查询 {len(str(query or ''))} 字）"
            self._audit(
                emit, task_id, target, str(result.get("outcome") or "failed"),
                f"provider={self.provider} results={len(rows)}",
            )
            if not rows:
                body = "没有检索到网页结果。\n" + (
                    "说明：" + "；".join(notes) if notes else ""
                )
                return ToolResponse(content=[TextBlock(type="text", text=body)])
            parts = [
                f"网页搜索（{self.provider}）：「{_one_line(query, 80)}」共 {len(rows)} 条结果："
            ]
            for i, row in enumerate(rows, 1):
                parts.append(
                    f"{i}. {row['title']}\n{row['url']}\n{row.get('snippet') or '（无摘要）'}"
                )
            if self.mock:
                parts.append(
                    "当前是 Mock 搜索：以上链接与摘要均为本地占位内容，"
                    "不得作为事实引用，也不能当作已访问的真实网页。"
                )
            else:
                parts.append(
                    "引用时保留结果里的链接（形如 [来源: https://...]）；摘要不是原文，"
                    "核对原文请用 fetch_url（目标主机需在联网白名单）。"
                )
            if notes:
                parts.append("说明：" + "；".join(notes))
            return ToolResponse(content=[TextBlock(type="text", text="\n\n".join(parts))])

        toolkit.register_tool_function(web_search)

    def _audit(
        self,
        emit: Callable[[str, dict], None] | None,
        task_id: str,
        target: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        if emit is None:
            return
        try:
            emit("tool_audit", {
                "workspaceId": getattr(self.grants, "workspace_id", ""),
                "taskId": task_id,
                "tool": "web_search",
                "action": "network",
                "target": _brief(target, 200),
                "outcome": outcome,
                "detail": _brief(detail, 240),
            })
        except Exception:  # noqa: BLE001  审计失败不能打断检索本身
            pass
