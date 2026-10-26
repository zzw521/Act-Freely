"""执行面 P2：浏览器 AI+（受控浏览器会话）

Rust 随 `runtime.browserUse` 下发浏览器、档位与一次性回环网关：

    browserUse = {
        supported, platform, browser, features, policy, persisted,
        running, profileDir,
        gateUrl, gateToken,          # 仅在可用时出现
        gateError, error,            # 不可用时说明原因
    }

引擎只做「请求 + CDP 操作」：浏览器进程、专用配置目录、策略门禁、系统确认框
与审计都在桌面壳（Rust）里。引擎通过网关拿到**页面级** WebSocket 地址，再用
Chrome DevTools Protocol 执行快照/点击/输入；拿不到浏览器进程或其他标签页的能力。

边界：
- 只允许 http/https；file/javascript/data/chrome 等协议在引擎与 Rust 两侧都会拒绝；
- 档位为「禁用」时不注册工具；「每次询问」每次操作都由 Rust 弹系统确认框；
- 只操作受控浏览器自己的独立配置目录，不碰用户日常浏览器的 Cookie 与登录态；
- 密码框拒绝输入；输入正文、页面正文与截图内容不写入审计；
- 页面快照用可见 DOM（不是截图），因此不需要视觉模型；截图工具只在视觉模型下注册；
- 每个任务有快照/操作/截屏次数上限，防止模型在循环里反复刷新或连点。

回环网关依赖不属于可 Mock 的外部服务：离线自测通过替换 `_gate_post` 模拟。
"""

from __future__ import annotations

import asyncio
import json
import urllib.parse
from dataclasses import dataclass, field

MAX_ACTIONS = 80
MAX_SNAPSHOTS = 40
MAX_CAPTURES = 20
MAX_TYPE_CHARS = 4000
MAX_ELEMENTS = 80
MAX_PAGE_TEXT = 6000
MAX_GATE_BYTES = 8 * 1024 * 1024
MAX_URL_CHARS = 4096
# 「每次询问」会弹系统确认框，要留给用户足够时间
GATE_TIMEOUT = 120
ENSURE_TIMEOUT = 45
CDP_TIMEOUT = 25

POLICIES = ("ask_every_time", "auto", "disabled")
POLICY_LABELS = {
    "ask_every_time": "每次询问（系统确认框）",
    "auto": "自动执行（不再逐次确认）",
    "disabled": "已禁用",
}

# 只暴露有限的交互元素：页面上的 div 点击区不少，但逐个编号会撑爆上下文
INTERACTIVE_SELECTOR = (
    'a[href],button,input,textarea,select,summary,'
    '[role="button"],[role="link"],[role="checkbox"],[role="radio"],'
    '[role="switch"],[role="tab"],[role="menuitem"],[role="combobox"],'
    '[role="textbox"],[contenteditable="true"],[onclick]'
)


class ToolError(Exception):
    """工具执行失败：文案直接回给模型，不包含令牌等敏感信息。"""


class DeniedError(ToolError):
    """放行网关拒绝了本次动作：审计已在 _approve 里记为 denied，不再重复记 error。"""


def _brief(text: object, limit: int = 240) -> str:
    one = " ".join(str(text or "").split())
    return one if len(one) <= limit else one[:limit] + "…"


def _loopback_gate(url: str) -> bool:
    """只接受本机回环 HTTP 地址：token 不应被发往任何远端"""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:  # noqa: BLE001
        return False
    return (
        parts.scheme == "http"
        and parts.hostname == "127.0.0.1"
        and bool(parts.port)
    )


def _loopback_ws(url: str) -> bool:
    """页面 WebSocket 必须是本机回环，且落在 devtools/page 端点上。"""
    try:
        parts = urllib.parse.urlsplit(str(url or "").strip())
    except Exception:  # noqa: BLE001
        return False
    return (
        parts.scheme == "ws"
        and parts.hostname in ("127.0.0.1", "localhost")
        and bool(parts.port)
        and parts.path.startswith("/devtools/page/")
    )


def _audit_url(raw: str) -> str:
    """审计/日志里的 URL：去掉查询串与片段（可能含令牌、搜索词），只留协议/主机/路径。"""
    v = str(raw or "").strip()
    cut = len(v)
    for sep in ("?", "#"):
        pos = v.find(sep)
        if pos >= 0:
            cut = min(cut, pos)
    out = v[:cut]
    return out if len(out) <= 200 else out[:200] + "…"


def normalize_url(raw: str) -> tuple[str, str]:
    """只允许 http/https；返回 (url, 错误文案)。"""
    v = str(raw or "").strip()
    if not v:
        return "", "打开页面需要提供 URL"
    if len(v) > MAX_URL_CHARS:
        return "", f"URL 超过 {MAX_URL_CHARS} 个字符"
    if any(c.isspace() or ord(c) < 0x20 for c in v):
        return "", "URL 不能包含空白或控制字符"
    lower = v.lower()
    if not (lower.startswith("http://") or lower.startswith("https://")):
        return "", (
            "受控浏览器只允许打开 http/https 页面"
            "（已拒绝 file / javascript / data / chrome 等协议）"
        )
    rest = v.split("://", 1)[1]
    host = rest.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    host = host.rsplit("@", 1)[-1]
    if not host:
        return "", "URL 缺少主机名"
    return v, ""


async def _gate_post(url: str, token: str, payload: dict, timeout: int = GATE_TIMEOUT) -> tuple[dict | None, str]:
    """POST 到本机浏览器网关。返回 (响应 JSON, 错误文案)；错误文案不含 token。"""
    try:
        import aiohttp
    except Exception as exc:  # noqa: BLE001
        return None, f"引擎缺少 aiohttp，无法连接本机浏览器网关：{_brief(exc)}"
    headers = {
        "Content-Type": "application/json",
        "x-tt-browser-token": token,
    }
    try:
        timeout_cfg = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=timeout_cfg) as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                raw = await resp.read()
                if len(raw) > MAX_GATE_BYTES:
                    return None, f"网关响应超过 {MAX_GATE_BYTES} 字节，已拒绝读取"
                try:
                    data = json.loads(raw.decode("utf-8", errors="replace"))
                except Exception as exc:  # noqa: BLE001
                    return None, f"网关返回了无法解析的响应（HTTP {resp.status}）：{_brief(exc, 120)}"
                if not isinstance(data, dict):
                    return None, "网关响应不是 JSON 对象"
                if resp.status != 200 or data.get("ok") is not True:
                    return None, str(data.get("error") or f"网关拒绝了请求（HTTP {resp.status}）")
                return data, ""
    except Exception as exc:  # noqa: BLE001
        return None, f"本机浏览器网关请求失败：{_brief(exc, 200)}"


def _text_response(text: str):
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    return ToolResponse(content=[TextBlock(type="text", text=text)])


# ── CDP 客户端 ─────────────────────────────────────────────────

class CDP:
    """最小 CDP 客户端：只做命令/响应，不消费事件流。"""

    def __init__(self, ws_url: str):
        self.ws_url = ws_url
        self._session = None
        self._ws = None
        self._id = 0
        self._lock = asyncio.Lock()

    @property
    def connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    async def connect(self) -> None:
        import aiohttp

        self._session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=CDP_TIMEOUT)
        )
        try:
            self._ws = await self._session.ws_connect(
                self.ws_url,
                max_msg_size=64 * 1024 * 1024,
                heartbeat=20,
            )
        except Exception:
            await self.close()
            raise

    async def close(self) -> None:
        ws, session = self._ws, self._session
        self._ws = None
        self._session = None
        if ws is not None:
            try:
                await ws.close()
            except Exception:  # noqa: BLE001
                pass
        if session is not None:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    async def call(self, method: str, params: dict | None = None, timeout: float = CDP_TIMEOUT) -> dict:
        if self._ws is None or self._ws.closed:
            raise ToolError("受控浏览器连接已断开")
        async with self._lock:
            self._id += 1
            mid = self._id
            await self._ws.send_json({"id": mid, "method": method, "params": params or {}})
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    raise TimeoutError(f"CDP {method} 超时")
                msg = await self._ws.receive(timeout=remaining)
                import aiohttp

                if msg.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(msg.data)
                    except Exception:  # noqa: BLE001
                        continue
                    if data.get("id") != mid:
                        continue
                    if "error" in data:
                        err = data.get("error") or {}
                        raise ToolError(
                            f"浏览器拒绝执行 {method}：{_brief(err.get('message') or err)}"
                        )
                    return data.get("result") or {}
                if msg.type in (
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.ERROR,
                ):
                    raise ToolError("受控浏览器连接已断开")

    async def eval_js(self, expression: str, timeout: float = CDP_TIMEOUT):
        result = await self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": False,
                "userGesture": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            detail = result["exceptionDetails"].get("text") or "页面脚本执行失败"
            raise ToolError(_brief(detail, 200))
        return (result.get("result") or {}).get("value")


# ── 页面脚本 ───────────────────────────────────────────────────

SNAPSHOT_JS = r"""
(() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-tt-idx]').forEach((e) => e.removeAttribute('data-tt-idx'));
  const sel = '__SELECTOR__';
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse') return false;
    if (parseFloat(st.opacity || '1') === 0) return false;
    return true;
  };
  const nodes = [];
  document.querySelectorAll(sel).forEach((el) => {
    if (visible(el)) nodes.push(el);
  });
  nodes.sort((a, b) => {
    const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
    return (ra.top - rb.top) || (ra.left - rb.left);
  });
  const elements = [];
  nodes.slice(0, __MAX_ELEMENTS__).forEach((el, i) => {
    const idx = i + 1;
    el.setAttribute('data-tt-idx', String(idx));
    const r = el.getBoundingClientRect();
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    let kind = tag;
    if (tag === 'input') kind = 'input[' + type + ']';
    else if (el.isContentEditable) kind = 'contenteditable';
    else if (el.getAttribute('role')) kind = 'role=' + el.getAttribute('role');
    let label = clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                      el.getAttribute('title') || el.innerText || '');
    if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
      label = clean(el.getAttribute('aria-label') || el.getAttribute('name') || '');
    }
    elements.push({
      i: idx, kind, label: label.slice(0, 120),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height),
      inView: r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth
    });
  });
  const body = document.body ? document.body.innerText : '';
  return {
    url: location.href,
    title: document.title,
    text: clean(body).slice(0, __MAX_PAGE_TEXT__),
    elements,
    scrollX: Math.round(scrollX),
    scrollY: Math.round(scrollY),
    viewport: { w: innerWidth, h: innerHeight },
    scrollHeight: Math.max(
      document.documentElement ? document.documentElement.scrollHeight : 0,
      document.body ? document.body.scrollHeight : 0
    ),
    iframes: document.querySelectorAll('iframe').length
  };
})()
"""

RESOLVE_JS = r"""
(() => {
  const idx = __INDEX__;
  const el = document.querySelector('[data-tt-idx="' + idx + '"]');
  if (!el) return { error: '元素序号已失效，请重新调用 browser_snapshot' };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  const r = el.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return { error: '元素当前不可见' };
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  const kind = tag === 'input' ? 'input[' + type + ']' : (el.isContentEditable ? 'contenteditable' : tag);
  let label = String(el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                     el.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 100);
  if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
    label = String(el.getAttribute('aria-label') || el.getAttribute('name') || '').trim().slice(0, 100);
  }
  const editable = tag === 'textarea' ||
    (tag === 'input' && ['text','search','email','url','tel','number','date',
                         'datetime-local','month','week','time','password'].includes(type)) ||
    el.isContentEditable;
  return {
    x: r.left + r.width / 2, y: r.top + r.height / 2,
    w: r.width, h: r.height,
    kind, label,
    disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true',
    editable,
    isPassword: tag === 'input' && type === 'password',
    isSelect: tag === 'select'
  };
})()
"""

SELECT_ALL_JS = r"""
(() => {
  const el = document.querySelector('[data-tt-idx="__INDEX__"]');
  if (!el) return false;
  el.focus();
  const tag = el.tagName.toLowerCase();
  if (tag === 'input' || tag === 'textarea') {
    try { el.select(); } catch (e) { return false; }
    return true;
  }
  if (el.isContentEditable) {
    const range = document.createRange();
    range.selectNodeContents(el);
    const sel = window.getSelection();
    sel.removeAllRanges();
    sel.addRange(range);
    return true;
  }
  return false;
})()
"""

SELECT_OPTION_JS = r"""
(() => {
  const idx = __INDEX__;
  const want = __WANT__;
  const el = document.querySelector('[data-tt-idx="' + idx + '"]');
  if (!el) return { error: '元素序号已失效，请重新调用 browser_snapshot' };
  if (el.tagName.toLowerCase() !== 'select') return { error: '目标元素不是下拉框' };
  const wantText = String(want).trim();
  const options = Array.from(el.options || []);
  const opt = options.find((o) => o.textContent.trim() === wantText || o.value === wantText);
  if (!opt) {
    const sample = options.slice(0, 20).map((o) => o.textContent.trim()).join(' / ');
    return { error: '没有匹配的选项。可用选项：' + (sample || '（空）') };
  }
  el.value = opt.value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  return { ok: true, label: opt.textContent.trim() };
})()
"""

SCROLL_STATE_JS = r"""
(() => ({
  y: Math.round(scrollY),
  h: innerHeight,
  total: Math.max(
    document.documentElement ? document.documentElement.scrollHeight : 0,
    document.body ? document.body.scrollHeight : 0
  )
}))()
"""


@dataclass
class BrowserUse:
    task_id: str = ""
    supported: bool = False
    platform: str = ""
    browser: str = ""
    policy: str = "disabled"
    features: dict = field(default_factory=dict)
    gate_url: str = ""
    gate_token: str = ""
    vision: bool = False
    reason: str = ""
    error: str = ""
    runtime_present: bool = False
    _cdp: CDP | None = None
    _page_url: str = ""
    _actions: int = 0
    _snapshots: int = 0
    _captures: int = 0
    _seq: int = 0

    @classmethod
    def from_runtime(
        cls,
        runtime: dict | None,
        *,
        vision: bool,
        task_id: str = "",
    ) -> "BrowserUse":
        block = (runtime or {}).get("browserUse") if isinstance(runtime, dict) else None
        if not isinstance(block, dict):
            return cls(
                task_id=task_id,
                vision=vision,
                reason="未收到浏览器 AI+ 能力信息（桌面壳可能过旧）",
            )
        platform = str(block.get("platform") or "").strip()
        supported = block.get("supported") is True
        policy = str(block.get("policy") or "disabled").strip()
        browser = str(block.get("browser") or "").strip()
        features = block.get("features") if isinstance(block.get("features"), dict) else {}
        error = str(block.get("error") or "").strip()
        gate_error = str(block.get("gateError") or "").strip()
        gate_url = str(block.get("gateUrl") or "").strip()
        gate_token = str(block.get("gateToken") or "").strip()

        reason = ""
        if error:
            reason = f"浏览器能力读取失败：{_brief(error, 300)}"
        elif not supported:
            reason = "本机没有找到 Microsoft Edge 或 Google Chrome，浏览器 AI+ 不可用"
        elif policy not in POLICIES:
            reason = f"浏览器档位非法「{policy}」，已按禁用处理"
        elif policy == "disabled":
            reason = "浏览器 AI+ 已禁用"
        elif gate_error:
            reason = gate_error
        elif not _loopback_gate(gate_url) or not gate_token:
            reason = "本机浏览器网关未启动或地址无效，执行器不可用"
        return cls(
            task_id=task_id,
            supported=supported,
            platform=platform,
            browser=browser,
            policy=policy,
            features=features,
            gate_url=gate_url,
            gate_token=gate_token,
            vision=vision,
            reason=reason,
            error=error,
            runtime_present=True,
        )

    @property
    def ready(self) -> bool:
        return self.runtime_present and not self.reason

    def notes(self) -> list[str]:
        if self.ready:
            lines = [
                f"浏览器 AI+ 已启用（{POLICY_LABELS.get(self.policy, self.policy)}）："
                f"使用 {self.browser or '本机浏览器'} 的独立配置目录，只读/操作受控窗口，"
                "只允许 http/https；每次操作按档位确认并写入审计"
            ]
            if not self.vision:
                lines.append("当前模型没有视觉能力：浏览器截图工具未注册，页面理解走 DOM 快照")
            return lines
        if self.runtime_present and (
            self.error or (self.supported and self.policy != "disabled")
        ):
            return [f"浏览器 AI+ 未启用：{self.reason}"]
        return []

    def system_hint(self) -> str:
        if not self.ready:
            return ""
        vision_line = (
            "- browser_screenshot：截取当前网页画面（仅在需要看布局/画布时使用）。\n"
            if self.vision
            else "- 当前模型没有视觉能力，不能截图；请依赖 browser_snapshot 的文字与元素序号。\n"
        )
        return (
            "\n\n## 浏览器 AI+（用户已启用）\n"
            "- browser_open：在受控浏览器中打开 http/https 页面。"
            "受控浏览器使用独立配置目录，不带你日常浏览器的登录态。\n"
            "- browser_snapshot：读取当前页面的可见文字与可交互元素序号（[1]、[2]…）。"
            "**点击/输入必须先快照，再按序号操作**；页面变化后序号会失效，需要重新快照。\n"
            "- browser_click：按序号点击元素（真实鼠标事件，会触发展开、跳转、提交等副作用）。\n"
            "- browser_type：按序号向输入框输入文本（clear=true 时先清空）；密码框会被拒绝；"
            "下拉框用 text 匹配选项。\n"
            "- browser_scroll：滚动页面，取回新的滚动位置。\n"
            + vision_line +
            f"- 当前档位：{POLICY_LABELS.get(self.policy, self.policy)}。"
            "「每次询问」每次操作都会弹系统确认框，用户拒绝时动作不会执行。\n"
            "- 不要绕过页面流程批量提交表单；涉及支付、验证码、密码、删除等不可逆操作时先向用户确认。"
        )

    # ── 网关与 CDP ──────────────────────────────────────────────

    async def _post(self, path: str, payload: dict, timeout: int = GATE_TIMEOUT):
        if not self.ready:
            return None, self.reason or "浏览器 AI+ 不可用"
        return await _gate_post(
            self.gate_url.rstrip("/") + path,
            self.gate_token,
            {**payload, "taskId": self.task_id},
            timeout,
        )

    async def _ensure_cdp(self) -> CDP:
        if self._cdp is not None and self._cdp.connected:
            return self._cdp
        await self._reset_session()
        data, err = await self._post("/ensure", {}, ENSURE_TIMEOUT)
        if err:
            raise ToolError(err)
        ws_url = str(data.get("wsUrl") or "")
        if not _loopback_ws(ws_url):
            raise ToolError("浏览器网关返回的页面地址不是本机回环 devtools 地址，已拒绝连接")
        cdp = CDP(ws_url)
        try:
            await cdp.connect()
            await cdp.call("Page.enable")
            await cdp.call("Runtime.enable")
        except Exception as exc:
            await cdp.close()
            raise ToolError(f"无法连接受控浏览器页面：{_brief(exc)}") from exc
        # 下载一律拒绝：受控浏览器只用于阅读与操作页面，不应悄悄往磁盘写文件
        try:
            await cdp.call("Page.setDownloadBehavior", {"behavior": "deny"})
        except Exception:  # noqa: BLE001  旧版本不支持该命令时不影响主流程
            pass
        self._cdp = cdp
        self._page_url = str(data.get("url") or "")
        return cdp

    async def _reset_session(self) -> None:
        cdp = self._cdp
        self._cdp = None
        if cdp is not None:
            await cdp.close()

    async def _cdp_call(self, method: str, params: dict | None = None) -> dict:
        cdp = await self._ensure_cdp()
        try:
            return await cdp.call(method, params)
        except Exception as exc:
            await self._reset_session()
            raise ToolError(
                f"受控浏览器连接中断：{_brief(exc)}。"
                "如窗口已被关闭，请重新 browser_open。"
            ) from exc

    async def _eval(self, expression: str):
        cdp = await self._ensure_cdp()
        try:
            return await cdp.eval_js(expression)
        except Exception as exc:
            await self._reset_session()
            raise ToolError(f"页面脚本执行失败：{_brief(exc)}") from exc

    async def _approve(
        self,
        action: str,
        target: str,
        detail: str,
        emit,
        task_id: str,
        tool: str,
    ) -> dict:
        data, err = await self._post(
            "/approve",
            {"action": action, "target": target, "detail": detail},
        )
        if err:
            self._audit(emit, task_id, tool, target, "denied", err)
            raise DeniedError(err)
        return data

    def _audit(
        self,
        emit,
        task_id: str,
        tool: str,
        target: str,
        outcome: str,
        detail: str = "",
    ) -> None:
        if emit is None:
            return
        self._seq += 1
        try:
            emit("tool_audit", {
                "taskId": task_id,
                "tool": tool,
                "action": "browser",
                "target": _audit_url(target),
                "outcome": outcome,
                "detail": _brief(detail, 300),
                "seq": self._seq,
            })
        except Exception:  # noqa: BLE001  审计事件发不出去不应中断工具本身
            pass

    def _budget(self, counter: int, limit: int, what: str) -> str:
        if counter >= limit:
            return f"本次任务{what}次数已达上限 {limit}，请基于已有信息继续或向用户说明进展。"
        return ""

    def _page_target(self) -> str:
        return self._page_url or ""

    async def _current_page(self) -> dict:
        return await self._eval(
            "({url: location.href, title: document.title, ready: document.readyState})"
        ) or {}

    async def _wait_ready(self, url: str, timeout: float = 20.0) -> dict:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        last: dict = {}
        while loop.time() < deadline:
            try:
                last = await self._current_page()
            except ToolError:
                await asyncio.sleep(0.3)
                continue
            self._page_url = str(last.get("url") or url)
            if str(last.get("ready") or "") in ("interactive", "complete"):
                return last
            await asyncio.sleep(0.25)
        last["url"] = self._page_url or url
        last["ready"] = "timeout"
        return last

    # ── 工具注册 ────────────────────────────────────────────────

    def install(self, toolkit, emit=None, task_id: str = "") -> None:
        """就绪才注册；未就绪时工具与提示词同时缺席，原因由 notes() 摊开"""
        if not self.ready:
            return
        toolkit.register_tool_function(self._open_tool(emit, task_id))
        toolkit.register_tool_function(self._snapshot_tool(emit, task_id))
        toolkit.register_tool_function(self._click_tool(emit, task_id))
        toolkit.register_tool_function(self._type_tool(emit, task_id))
        toolkit.register_tool_function(self._scroll_tool(emit, task_id))
        if self.vision:
            toolkit.register_tool_function(self._screenshot_tool(emit, task_id))

    def _open_tool(self, emit, task_id: str):
        async def browser_open(url: str):
            """在受控浏览器中打开一个网页。

            Args:
                url (`str`):
                    要打开的完整地址，必须以 http:// 或 https:// 开头。
                    受控浏览器使用独立配置目录，不会带上你日常浏览器的登录态。
            """
            tool = "browser_open"
            target, err = normalize_url(url)
            if err:
                self._audit(emit, task_id, tool, str(url or ""), "denied", err)
                return _text_response(f"错误：页面未打开。{err}")
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            try:
                await self._approve("open", target, "", emit, task_id, tool)
                self._actions += 1
                result = await self._cdp_call("Page.navigate", {"url": target})
                if result.get("errorText"):
                    self._audit(emit, task_id, tool, target, "error", str(result["errorText"]))
                    return _text_response(
                        f"错误：页面打开失败。{result['errorText']}"
                    )
                page = await self._wait_ready(target)
                self._page_url = str(page.get("url") or target)
                ready = page.get("ready")
                note = "页面已加载" if ready != "timeout" else "页面仍在加载"
                self._audit(
                    emit, task_id, tool, target, "ok",
                    f"{note}（{page.get('title') or '无标题'}）",
                )
                return _text_response(
                    f"已打开：{self._page_url}\n标题：{page.get('title') or '（无标题）'}\n{note}。"
                    "下一步用 browser_snapshot 查看页面文字与可交互元素序号。"
                )
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：页面未打开。{exc}")

        return browser_open

    def _snapshot_tool(self, emit, task_id: str):
        async def browser_snapshot():
            """读取当前页面的可见文字与可交互元素序号。

            元素序号只在本次快照后有效；点击、输入或页面跳转后需要重新快照。
            返回内容不包含输入框里的值（密码等敏感内容不会进入模型）。
            """
            tool = "browser_snapshot"
            limit = self._budget(self._snapshots, MAX_SNAPSHOTS, "页面快照")
            if limit:
                return _text_response(f"错误：{limit}")
            try:
                page = await self._current_page()
                target = str(page.get("url") or self._page_target())
                await self._approve("snapshot", target, "", emit, task_id, tool)
                self._snapshots += 1
                js = (
                    SNAPSHOT_JS.replace("__SELECTOR__", INTERACTIVE_SELECTOR)
                    .replace("__MAX_ELEMENTS__", str(MAX_ELEMENTS))
                    .replace("__MAX_PAGE_TEXT__", str(MAX_PAGE_TEXT))
                )
                data = await self._eval(js) or {}
                if not isinstance(data, dict):
                    raise ToolError("页面快照返回了无法解析的结果")
                self._page_url = str(data.get("url") or target)
                text = str(data.get("text") or "")
                elements = data.get("elements") if isinstance(data.get("elements"), list) else []
                lines = [
                    f"页面：{data.get('title') or '（无标题）'}",
                    f"URL：{self._page_url}",
                    f"滚动位置：y={data.get('scrollY', 0)} / 页面高度 {data.get('scrollHeight', 0)}，"
                    f"视口 {((data.get('viewport') or {}).get('w') or 0)}x"
                    f"{((data.get('viewport') or {}).get('h') or 0)}",
                ]
                if text:
                    lines.append("可见文本：\n" + text)
                else:
                    lines.append("可见文本：没有可读取的文字（可能是画布、视频或尚未加载）")
                if elements:
                    lines.append("可交互元素（点击/输入用序号）：")
                    for el in elements:
                        if not isinstance(el, dict):
                            continue
                        label = str(el.get("label") or "").strip()
                        tag = str(el.get("kind") or "")
                        pos = f"({el.get('x', 0)},{el.get('y', 0)} {el.get('w', 0)}x{el.get('h', 0)})"
                        disabled = "（不可用）" if el.get("disabled") else ""
                        shown = f"「{label}」" if label else "（无文字）"
                        lines.append(f"[{el.get('i')}] {tag} {shown} {pos}{disabled}")
                    lines.append(
                        "序号只在本次快照后有效；操作或页面变化后请重新 browser_snapshot。"
                    )
                else:
                    lines.append(
                        "可交互元素：未发现。如果页面是画布/图片界面，需要用 browser_screenshot 观察。"
                    )
                if data.get("iframes"):
                    lines.append(
                        f"提示：页面有 {data['iframes']} 个 iframe，其内部内容不在本次快照中。"
                    )
                self._audit(
                    emit, task_id, tool, self._page_url, "ok",
                    f"元素 {len(elements)} 个，文本 {len(text)} 字",
                )
                return _text_response("\n".join(lines))
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, self._page_target(), "error", str(exc))
                return _text_response(f"错误：页面快照失败。{exc}")

        return browser_snapshot

    async def _resolve(self, index: int) -> dict:
        try:
            idx = int(index)
        except Exception as exc:  # noqa: BLE001
            raise ToolError("index 必须是 browser_snapshot 返回的整数序号") from exc
        data = await self._eval(RESOLVE_JS.replace("__INDEX__", str(idx)))
        if not isinstance(data, dict):
            raise ToolError("元素解析返回了无法解析的结果")
        if data.get("error"):
            raise ToolError(str(data["error"]))
        return data

    async def _dispatch_click(self, x: float, y: float) -> None:
        await self._cdp_call(
            "Input.dispatchMouseEvent",
            {"type": "mouseMoved", "x": x, "y": y, "button": "none", "clickCount": 0},
        )
        await self._cdp_call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1},
        )
        await self._cdp_call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1},
        )

    def _click_tool(self, emit, task_id: str):
        async def browser_click(index: int):
            """点击当前页面上的一个可交互元素。

            Args:
                index (`int`):
                    browser_snapshot 返回的元素序号。点击会触发真实鼠标事件，
                    可能造成跳转、提交表单、发送消息等副作用。
            """
            tool = "browser_click"
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            target = self._page_target()
            try:
                el = await self._resolve(index)
                target = self._page_url or target
                if el.get("disabled"):
                    raise ToolError("目标元素当前不可用（disabled）")
                detail = f"{el.get('kind')}「{el.get('label') or '无文字'}」"
                await self._approve("click", target, detail, emit, task_id, tool)
                self._actions += 1
                await self._dispatch_click(float(el.get("x") or 0), float(el.get("y") or 0))
                await asyncio.sleep(0.35)
                page = await self._current_page()
                self._page_url = str(page.get("url") or target)
                self._audit(emit, task_id, tool, self._page_url, "ok", detail)
                return _text_response(
                    f"已点击 [{index}] {detail}。页面可能已变化，"
                    "下一步操作前请重新 browser_snapshot 确认。"
                )
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：点击未执行。{exc}")

        return browser_click

    async def _press_enter(self) -> None:
        for typ in ("keyDown", "keyUp"):
            await self._cdp_call(
                "Input.dispatchKeyEvent",
                {
                    "type": typ,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                },
            )

    def _type_tool(self, emit, task_id: str):
        async def browser_type(index: int, text: str, clear: bool = True, submit: bool = False):
            """向当前页面的输入框输入文本，或选择下拉框选项。

            Args:
                index (`int`):
                    browser_snapshot 返回的元素序号。
                text (`str`):
                    要输入的文本（最多 4000 字符）；对下拉框是选项文字或 value。
                    内容可能进入页面并随表单提交，密码框会被拒绝。
                clear (`bool`, optional):
                    是否先清空输入框，默认 True；False 表示追加。
                submit (`bool`, optional):
                    输入后是否按 Enter 提交，默认 False。
            """
            tool = "browser_type"
            body = str(text or "")
            if not body:
                self._audit(emit, task_id, tool, self._page_target(), "denied", "缺少 text")
                return _text_response("错误：输入未执行。缺少 text")
            if len(body) > MAX_TYPE_CHARS:
                err = f"单次输入最多 {MAX_TYPE_CHARS} 个字符（收到 {len(body)}）"
                self._audit(emit, task_id, tool, self._page_target(), "denied", err)
                return _text_response(f"错误：输入未执行。{err}")
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            target = self._page_target()
            try:
                el = await self._resolve(index)
                target = self._page_url or target
                if el.get("disabled"):
                    raise ToolError("目标元素当前不可用（disabled）")
                if el.get("isPassword"):
                    raise ToolError(
                        "密码框不通过浏览器 AI+ 输入。请让用户亲自填写密码或使用密码管理器。"
                    )
                if el.get("isSelect"):
                    detail = f"选择选项，候选 {len(body)} 字"
                    await self._approve("select", target, detail, emit, task_id, tool)
                    self._actions += 1
                    result = await self._eval(
                        SELECT_OPTION_JS.replace("__INDEX__", str(int(index)))
                        .replace("__WANT__", json.dumps(body, ensure_ascii=False))
                    )
                    if not isinstance(result, dict) or result.get("error"):
                        raise ToolError(
                            str((result or {}).get("error") if isinstance(result, dict) else "")
                            or "选择下拉选项失败"
                        )
                    label = str(result.get("label") or body)
                    self._audit(emit, task_id, tool, target, "ok", f"选择「{label}」")
                    return _text_response(
                        f"已选择 [{index}] 选项「{label}」。下一步操作前请重新 browser_snapshot。"
                    )
                if not el.get("editable"):
                    raise ToolError(
                        f"[{index}] {el.get('kind')} 不是可输入元素；只能向输入框/文本域输入"
                    )
                detail = f"{el.get('kind')}「{el.get('label') or '无文字'}」输入 {len(body)} 个字符"
                await self._approve("type", target, detail, emit, task_id, tool)
                self._actions += 1
                await self._dispatch_click(float(el.get("x") or 0), float(el.get("y") or 0))
                if clear:
                    await self._eval(SELECT_ALL_JS.replace("__INDEX__", str(int(index))))
                await self._cdp_call("Input.insertText", {"text": body})
                if submit:
                    await self._press_enter()
                await asyncio.sleep(0.3)
                page = await self._current_page()
                self._page_url = str(page.get("url") or target)
                self._audit(emit, task_id, tool, self._page_url, "ok", detail)
                suffix = "，已按 Enter" if submit else ""
                return _text_response(
                    f"已向 [{index}] 输入 {len(body)} 个字符{suffix}。"
                    "下一步操作前请重新 browser_snapshot 确认结果。"
                )
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：输入未执行。{exc}")

        return browser_type

    def _scroll_tool(self, emit, task_id: str):
        async def browser_scroll(direction: str = "down", amount: int = 1):
            """滚动当前页面。

            Args:
                direction (`str`, optional):
                    up 或 down，默认 down。
                amount (`int`, optional):
                    滚动几屏，1-10，默认 1。
            """
            tool = "browser_scroll"
            d = str(direction or "down").strip().lower()
            if d not in ("up", "down"):
                self._audit(emit, task_id, tool, self._page_target(), "denied", "方向非法")
                return _text_response("错误：滚动未执行。direction 只能是 up 或 down")
            try:
                n = int(amount)
            except Exception:  # noqa: BLE001
                return _text_response("错误：滚动未执行。amount 必须是整数")
            if n < 1 or n > 10:
                return _text_response("错误：滚动未执行。amount 必须在 1-10 之间")
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            target = self._page_target()
            try:
                page = await self._current_page()
                target = str(page.get("url") or target)
                detail = f"{'向下' if d == 'down' else '向上'}滚动 {n} 屏"
                await self._approve("scroll", target, detail, emit, task_id, tool)
                self._actions += 1
                metrics = await self._eval(
                    "({w: innerWidth, h: innerHeight, x: Math.round(scrollX), y: Math.round(scrollY)})"
                ) or {}
                x = float(metrics.get("w") or 800) / 2
                y = float(metrics.get("h") or 600) / 2
                delta = int(float(metrics.get("h") or 600) * 0.9 * n) * (1 if d == "down" else -1)
                await self._cdp_call(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseWheel",
                        "x": x,
                        "y": y,
                        "deltaX": 0,
                        "deltaY": delta,
                    },
                )
                await asyncio.sleep(0.35)
                state = await self._eval(SCROLL_STATE_JS) or {}
                self._audit(
                    emit, task_id, tool, target, "ok",
                    f"{detail}，y={state.get('y')}",
                )
                return _text_response(
                    f"已{detail}。当前滚动位置 y={state.get('y', 0)} / 页面高度 {state.get('total', 0)}。"
                    "需要继续操作时请重新 browser_snapshot。"
                )
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：滚动未执行。{exc}")

        return browser_scroll

    def _screenshot_tool(self, emit, task_id: str):
        async def browser_screenshot():
            """截取当前网页的可见画面，并把图片附在工具结果中（仅视觉模型可用）。"""
            tool = "browser_screenshot"
            limit = self._budget(self._captures, MAX_CAPTURES, "浏览器截图")
            if limit:
                return _text_response(f"错误：{limit}")
            target = self._page_target()
            try:
                page = await self._current_page()
                target = str(page.get("url") or target)
                await self._approve("screenshot", target, "", emit, task_id, tool)
                self._captures += 1
                result = await self._cdp_call(
                    "Page.captureScreenshot",
                    {"format": "png", "fromSurface": True, "captureBeyondViewport": False},
                )
                data = str(result.get("data") or "")
                if not data:
                    raise ToolError("浏览器没有返回截图数据")
                metrics = await self._cdp_call("Page.getLayoutMetrics")
                css = (metrics.get("cssLayoutViewport") or {})
                width = int(css.get("clientWidth") or 0)
                height = int(css.get("clientHeight") or 0)
                self._audit(
                    emit, task_id, tool, target, "ok",
                    f"截图 {width}x{height}（画面内容不入审计）",
                )
                from agentscope.message import TextBlock
                from agentscope.tool import ToolResponse

                return ToolResponse(content=[
                    TextBlock(
                        type="text",
                        text=(
                            f"已截取网页画面：{width}x{height}，URL：{target}。"
                            "截图会附在本轮上下文中；页面变化后请重新截图或快照。"
                        ),
                    ),
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": "image/png",
                            "data": data,
                        },
                    },
                ])
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：网页截图失败。{exc}")

        return browser_screenshot
