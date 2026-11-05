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
- 密码框拒绝输入，`browser_press` 不带序号时也先确认页面聚焦的不是密码框（按键清单里有
  Backspace / Delete / 空格，蒙着眼发按键等于改输入框）；输入正文、页面正文与截图内容不写入审计；
- 页面快照用可见 DOM（不是截图），因此不需要视觉模型；截图工具只在视觉模型下注册；
- 每个任务有快照/操作/截屏次数上限，防止模型在循环里反复刷新或连点。

D-36 在这一层加的三件事（都不需要桌面壳改动，今天就能用）：

- **DOM 分级**：`browser_snapshot(level=...)` 四档（minimal / interactive / structured / full）。
  默认 interactive 与整改前逐字一致，`full` 才给正文之外的骨架、链接与字段名——「复杂页面点错」
  多半是模型只能在全量与截断之间二选一，分级让它先取结构再按需展开；四档的每一项上限都由
  既有常量导出（`MAX_ELEMENTS` / `MAX_PAGE_TEXT`），不另立预算；
- **录制 → 语义步骤 → 草案技能**：`browser_record(start)` 之后的 open/click/type/select/scroll
  记成**语义**步骤（元素靠标签认，不记坐标），`browser_record(save, skill_id=…)` 把流程写成草案
  技能（走技能注册表的 `add_draft` 通道，**不确认就不进执行面**）。录进去的是
  「往『搜索』框输入 {{搜索-3}}」这类占位符（`_record`），默认**不含任何输入正文**，
  像密码/密钥/验证码的字段永远不落盘也不代填（`_looks_secret` + `_step_value`）；
  `browser_replay` 按标签逐步重演，每一步都复用既有那几个工具——放行、审计、预算、密码框拒绝
  这些性质与「人肉一步步做」完全一致，占位符可在回放时用 `inputs` 换值，这是「改一个输入可回放」；
- **等待**：`browser_wait` 只轮询页面自己的就绪状态与地址，不读页面内容、不动页面，
  因此不需要新的网关动作；上限 `MAX_WAIT_SECONDS`，等不到就如实回「仍在加载」。

还没做的两件（受桌面壳约束，引擎这里只能如实标注）：**悬停/按键/执行脚本/标签页**与**命名配置目录**。
引擎侧的实现都在（`_hover_tool` / `_press_tool` / `_evaluate_tool` / `_tab_tool`，注册判据是
`has_feature`），但只有 Rust 的 `browser::features()` 显式说「支持」才会注册——`is_known_action`
不认识这些动作时，「每次询问」档下每次都会被网关拒绝，注册出来就是个必被拒绝的按钮；
缺的那一半由 `notes()` 与 `gated_actions()` 说清。标签页另有一处硬约束：网关只给**一个**页面级
WebSocket 地址，`/ensure` 已带上 `tabId`，但 Rust 不按它取回连接地址时
`browser_tab(switch)` 会发现 targetId 没变并直接报错，不假装切换成功。

回环网关依赖不属于可 Mock 的外部服务：离线自测通过替换 `_gate_post` 模拟。
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Any

import data_paths as dp

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
# 回放与录用的规模上限：录的是**语义步骤**，一条流程几十步已经是真实网页的上限，
# 超出的部分只截不报错（截断会在步骤数里说出来，不让模型以为录全了）。
MAX_RECORD_STEPS = MAX_ELEMENTS
MAX_REPLAY_STEPS = MAX_ELEMENTS
# 等待工具的上限：等的是页面自己变就绪，不是替模型把任务挂住
MAX_WAIT_SECONDS = 10.0
MAX_OUTLINE_ITEMS = MAX_ELEMENTS // 2
MAX_LINK_ITEMS = MAX_ELEMENTS

POLICIES = ("ask_every_time", "auto", "disabled")
POLICY_LABELS = {
    "ask_every_time": "每次询问（系统确认框）",
    "auto": "自动执行（不再逐次确认）",
    "disabled": "已禁用",
}

# ── DOM 分级（D-36）───────────────────────────────────────────────
# 四档的差别是「这一档要给模型看什么」，不是「能不能看」：元素序号四档都分配，
# 所以任何一档快照之后都能直接点击/输入，不必先切档重来。
SNAPSHOT_LEVELS = ("minimal", "interactive", "structured", "full")
SNAPSHOT_LEVEL_LABELS = {
    "minimal": "极简：只有页面位置、滚动状态与可交互元素的文字标签（不含页面正文）",
    "interactive": "交互（默认）：可交互元素带坐标与状态，正文按上限截断",
    "structured": "结构：语义骨架（标题层级/地标/列表与表格）+ 交互元素，正文只给开头",
    "full": "完整：正文、链接清单、表单字段名、标题骨架与全部元素细节",
}
# 每档的元素上限、正文上限、附带的段落。数值全部由既有上限导出，不另立预算：
# 分级要省的是**同一页面**的上下文，省多少由既有常量决定才有解释力。
SNAPSHOT_LEVEL_PROFILES: dict[str, dict[str, Any]] = {
    "minimal": {"elements": MAX_ELEMENTS // 4, "pageText": 0, "geometry": False,
                "outline": False, "links": False, "fields": False, "detail": False},
    "interactive": {"elements": MAX_ELEMENTS, "pageText": MAX_PAGE_TEXT, "geometry": True,
                    "outline": False, "links": False, "fields": False, "detail": False},
    "structured": {"elements": MAX_ELEMENTS, "pageText": MAX_PAGE_TEXT // 3, "geometry": False,
                   "outline": True, "links": False, "fields": True, "detail": False},
    "full": {"elements": MAX_ELEMENTS, "pageText": MAX_PAGE_TEXT, "geometry": True,
             "outline": True, "links": True, "fields": True, "detail": True},
}

# 桌面壳没在 `features` 里说「支持」的动作，引擎一律不注册：这些动作要走网关的
# `is_known_action` 白名单与确认框文案（`describe_action`），Rust 不认识它们时
# 「每次询问」会直接拒绝，界面画出一个必被拒绝的按钮比没有按钮更糟。
FEATURE_ONLY_ACTIONS = {
    "hover": ("hover", "悬停到元素上（会触发菜单、工具提示等页面副作用）"),
    "press": ("press", "按下键盘按键（Tab / Escape / 方向键等）"),
    "evaluate": ("evaluate", "在当前页面执行一段 JavaScript 并回传结果"),
    "tabs": ("tabs", "标签页：列出 / 新建 / 切换 / 关闭"),
}
# 能力位 → 引擎侧工具名（`tabs` 的工具叫 browser_tab，其余同名单数）
FEATURE_TOOL_NAMES = {
    "hover": "browser_hover",
    "press": "browser_press",
    "evaluate": "browser_evaluate",
    "tabs": "browser_tab",
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


def _response_text(resp) -> str:
    """取工具响应里的文字（回放要据此判断每一步的真实结果，不重新拼一份）"""
    parts: list[str] = []
    for block in (getattr(resp, "content", None) or []):
        if isinstance(block, dict):
            if block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        else:
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
    return "\n".join(parts)


def _ambiguity(found: dict) -> str:
    """按标签定位撞上同名元素时把风险说出来：回放取的是最靠上的一个，未必是录制时那一个"""
    try:
        count = int((found or {}).get("matches") or 1)
    except Exception:  # noqa: BLE001
        return ""
    return f"（页面上有 {count} 个同名元素，取最靠上的一个）" if count > 1 else ""


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
  const LEVEL = '__LEVEL__';
  const wantOutline = LEVEL === 'structured' || LEVEL === 'full';
  const wantLinks = LEVEL === 'full';
  const wantFields = LEVEL === 'structured' || LEVEL === 'full';
  const wantDetail = LEVEL === 'full';
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
    const item = {
      i: idx, kind, label: label.slice(0, 120),
      disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
      x: Math.round(r.left), y: Math.round(r.top),
      w: Math.round(r.width), h: Math.round(r.height),
      inView: r.top >= 0 && r.left >= 0 && r.bottom <= innerHeight && r.right <= innerWidth
    };
    if (wantDetail) {
      // full 才带的细节：模型按这些字段判断「点的是哪一条」，代价是每行更长
      const href = el.tagName.toLowerCase() === 'a' ? clean(el.getAttribute('href')) : '';
      const name = clean(el.getAttribute('name') || '');
      const role = clean(el.getAttribute('role') || '');
      if (href) item.href = href.slice(0, 200);
      if (name) item.name = name.slice(0, 80);
      if (role) item.role = role;
      item.id = clean(el.id || '').slice(0, 80);
      item.depth = (() => { let d = 0, p = el; while (p && p !== document.body && d < 20) { p = p.parentElement; d++; } return d; })();
    }
    elements.push(item);
  });
  const outline = [];
  if (wantOutline) {
    const seen = new Set();
    document.querySelectorAll('h1,h2,h3,h4,h5,h6,nav,main,header,footer,aside,form,table,ul,ol').forEach((el) => {
      if (outline.length >= __MAX_OUTLINE__) return;
      if (!visible(el)) return;
      const tag = el.tagName.toLowerCase();
      const r = el.getBoundingClientRect();
      let text = '';
      if (tag.startsWith('h')) {
        text = clean(el.innerText).slice(0, 100);
      } else {
        text = clean(el.getAttribute('aria-label') || el.getAttribute('name') || el.getAttribute('caption') || '');
      }
      const key = tag + '|' + text + '|' + Math.round(r.top);
      if (seen.has(key)) return;
      seen.add(key);
      outline.push({ tag, text, top: Math.round(r.top), items: (tag === 'ul' || tag === 'ol' || tag === 'table')
        ? el.querySelectorAll('li,tr').length : 0 });
    });
  }
  const links = [];
  if (wantLinks) {
    document.querySelectorAll('a[href]').forEach((el) => {
      if (links.length >= __MAX_LINKS__) return;
      if (!visible(el)) return;
      const href = clean(el.getAttribute('href'));
      if (!href || href.startsWith('#') || href.startsWith('javascript')) return;
      links.push({ text: clean(el.innerText).slice(0, 80), href: href.slice(0, 200) });
    });
  }
  const fields = [];
  if (wantFields) {
    document.querySelectorAll('input,textarea,select,[contenteditable="true"]').forEach((el) => {
      if (fields.length >= __MAX_ELEMENTS__) return;
      if (!visible(el)) return;
      const tag = el.tagName.toLowerCase();
      const type = (el.getAttribute('type') || '').toLowerCase();
      // 字段清单只描述「这里要填什么」，永远不含当前值：值可能已经是用户敲进去的密码或隐私
      fields.push({
        kind: tag === 'input' ? 'input[' + (type || 'text') + ']' : tag,
        label: clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                     el.getAttribute('name') || '').slice(0, 120),
        password: tag === 'input' && type === 'password',
        required: !!(el.required || el.getAttribute('aria-required') === 'true'),
        top: Math.round(el.getBoundingClientRect().top)
      });
    });
  }
  const body = document.body ? document.body.innerText : '';
  return {
    url: location.href,
    title: document.title,
    level: LEVEL,
    text: clean(body).slice(0, __MAX_PAGE_TEXT__),
    elements,
    outline,
    links,
    fields,
    // 被上限截掉的部分要如实说出来，否则模型会把「前 80 个」当成「页面上只有 80 个」
    truncated: {
      elements: Math.max(0, nodes.length - elements.length),
      outline: outline.length >= __MAX_OUTLINE__,
      links: links.length >= __MAX_LINKS__
    },
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

# 按**文字标签**找元素：返回与 RESOLVE_JS 同形状的字段，并把 data-tt-idx 设成 1，
# 这样解析与点击/输入走的是既有那条已过网关的路径（回放不另开一条通路）。
# 定位靠标签不靠坐标：草案换到别的页面、别的窗口尺寸仍能点到同一个控件。
FIND_LABEL_JS = r"""
(() => {
  const want = String(__LABEL__ || '').replace(/\s+/g, ' ').trim();
  const sel = '__SELECTOR__';
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  document.querySelectorAll('[data-tt-idx]').forEach((e) => e.removeAttribute('data-tt-idx'));
  if (!want) return { error: '按标签定位需要非空的 label' };
  const visible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const st = getComputedStyle(el);
    if (st.display === 'none' || st.visibility === 'hidden' || st.visibility === 'collapse') return false;
    if (parseFloat(st.opacity || '1') === 0) return false;
    return true;
  };
  const labelOf = (el) => {
    const tag = el.tagName.toLowerCase();
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    if (tag === 'input' && (type === 'checkbox' || type === 'radio')) {
      return clean(el.getAttribute('aria-label') || el.getAttribute('name') || '');
    }
    return clean(el.getAttribute('aria-label') || el.getAttribute('placeholder') ||
                 el.getAttribute('title') || el.innerText || '');
  };
  const key = want.toLowerCase();
  let best = null, bestRank = 9, matches = 0;
  document.querySelectorAll(sel).forEach((el) => {
    if (!visible(el)) return;
    const t = labelOf(el).toLowerCase();
    if (!t) return;
    let rank = 4;
    if (t === key) rank = 0;
    else if (t.startsWith(key) || key.startsWith(t)) rank = 1;
    else if (t.includes(key) || key.includes(t)) rank = 2;
    if (rank < bestRank) { bestRank = rank; best = el; matches = 1; }
    else if (rank === bestRank) matches += 1;
  });
  if (!best) {
    return { error: '页面上找不到文字标签「' + want + '」对应的可交互元素：页面可能改版、还没加载或这一段本来不在当前页' };
  }
  best.setAttribute('data-tt-idx', '1');
  best.scrollIntoView({ block: 'center', inline: 'center' });
  const r = best.getBoundingClientRect();
  if (r.width < 2 || r.height < 2) return { error: '元素当前不可见' };
  const tag = best.tagName.toLowerCase();
  const type = (best.getAttribute('type') || 'text').toLowerCase();
  const kind = tag === 'input' ? 'input[' + type + ']' : (best.isContentEditable ? 'contenteditable' : tag);
  const editable = tag === 'textarea' ||
    (tag === 'input' && ['text','search','email','url','tel','number','date',
                         'datetime-local','month','week','time','password'].includes(type)) ||
    best.isContentEditable;
  return {
    x: r.left + r.width / 2, y: r.top + r.height / 2,
    w: r.width, h: r.height,
    kind, label: labelOf(best).slice(0, 100), matches,
    disabled: !!best.disabled || best.getAttribute('aria-disabled') === 'true',
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

# 聚焦到某个序号的元素（按键前先定位；找不到就如实回 error，不默默发给页面）
FOCUS_JS = r"""
(() => {
  const el = document.querySelector('[data-tt-idx="__INDEX__"]');
  if (!el) return { error: '元素序号已失效，请重新调用 browser_snapshot' };
  el.scrollIntoView({ block: 'center', inline: 'center' });
  try { el.focus(); } catch (e) { /* 部分元素 focus() 会抛，指针事件仍然有效 */ }
  return { ok: true, tag: el.tagName.toLowerCase() };
})()
"""

# 页面当前聚焦的元素：browser_press(index=0) 不带序号，按键会落到「页面自己聚焦的那个元素」，
# 引擎不看一眼就发 Backspace / Delete 等于蒙着眼睛改输入框——密码框尤其不能这样碰。
# 只取标签名、type 与一个属性名，不读正文：这份信息进审计与确认框文案，不进回显。
ACTIVE_JS = r"""
(() => {
  const el = document.activeElement;
  if (!el || el === document.body || el === document.documentElement) return { none: true };
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  return {
    tag,
    type,
    isPassword: tag === 'input' && type === 'password',
    label: String(el.getAttribute('aria-label') || el.getAttribute('placeholder') || '').trim().slice(0, 60),
  };
})()
"""

# 执行脚本的包装：把任意返回值收敛成「可 JSON 化的值 + 类型 + 截断标记」。
# 不做筛选式白名单——页面的 JS 本来就在页面自己的来源里跑，能读到的只有页面自己的东西；
# 引擎侧要守的是回传体量与「这一步必须过网关」。
EVALUATE_JS = r"""
(async () => {
  const cap = __MAX_RESULT__;
  let value;
  try {
    value = eval(__EXPR__);
    if (value && typeof value.then === 'function') value = await value;
  } catch (e) {
    return { error: String((e && e.message) || e).slice(0, 300) };
  }
  const type = value === null ? 'null' : (Array.isArray(value) ? 'array' : typeof value);
  if (value instanceof Node) {
    const tag = value.nodeName.toLowerCase();
    const text = String(value.textContent || '').replace(/\s+/g, ' ').trim().slice(0, cap);
    return { type: 'node:' + tag, value: text, truncated: (value.textContent || '').length > text.length };
  }
  let out;
  try { out = JSON.stringify(value); } catch (e) { out = String(value); }
  out = String(out === undefined ? '' : out);
  return { type, value: out.slice(0, cap), truncated: out.length > cap };
})()
"""

# 录制里的一步**只认标签**：坐标会随窗口尺寸、字体、页面重排失效，把坐标抄进技能
# 就是造一个「今天能跑、明天点歪」的回放器（这正是 D-36 描述的点错来源）。
REPLAY_ACTIONS = ("open", "click", "type", "select", "scroll")
KEY_CODES = {
    "enter": ("Enter", 13), "tab": ("Tab", 9), "escape": ("Escape", 27), "esc": ("Escape", 27),
    "backspace": ("Backspace", 8), "delete": ("Delete", 46), "space": (" ", 32),
    "arrowup": ("ArrowUp", 38), "arrowdown": ("ArrowDown", 40),
    "arrowleft": ("ArrowLeft", 37), "arrowright": ("ArrowRight", 39),
    "pageup": ("PageUp", 33), "pagedown": ("PageDown", 34), "home": ("Home", 36), "end": ("End", 35),
}
# 像密钥的字段名：这类字段即使档位是「记住本次输入」也不落盘（密码框本身已经被拒绝，
# 这里挡的是 API Key、口令、验证码这类没写成 type=password 的字段）
SECRET_LABEL_RE = re.compile(
    r"(密码|口令|密钥|令牌|私钥|助记词|验证码|password|passwd|secret|token|api[_-]?key|private[_-]?key|mnemonic|otp|cvv|cvc)",
    re.IGNORECASE,
)


def _looks_secret(label: str) -> bool:
    return bool(SECRET_LABEL_RE.search(str(label or "")))


def normalize_level(raw: object) -> tuple[str, str]:
    """快照档位归一：非法值不猜默认，回错误文案（模型据此改用四档之一）"""
    level = str(raw or "").strip().lower()
    if not level:
        return "interactive", ""
    if level in ("min", "lite", "简单"):
        level = "minimal"
    elif level in ("interact", "默认", "default"):
        level = "interactive"
    elif level in ("structure", "semantic", "结构"):
        level = "structured"
    elif level in ("all", "everything", "完整"):
        level = "full"
    if level not in SNAPSHOT_LEVELS:
        return "", (
            f"快照档位「{raw}」不认识；可选 {' / '.join(SNAPSHOT_LEVELS)}"
            f"（默认 {SNAPSHOT_LEVELS[1]}）"
        )
    return level, ""


def _placeholder(index: int, label: str) -> str:
    """录进去的输入位是一个占位符，不是正文：技能文件里出现「用户实际敲了什么」就是泄漏面"""
    clean = re.sub(r"[^0-9A-Za-z一-鿿_-]", "", str(label or ""))[:16]
    return "{{" + (f"{clean}-{index}" if clean else f"输入{index}") + "}}"


def normalize_replay_steps(raw: object) -> tuple[list[dict], str, str]:
    """把模型给的步骤清单归一成可执行序列。

    返回 `(steps, 错误文案, 警告文案)`。错误只在「这一步根本没法执行」时给；
    多出来的、认不出的字段一律忽略并计数（模型抄草案时总会带点解释文字，
    为一句话整个拒跑等于让它重抄一遍）。
    """
    items = raw
    if isinstance(raw, dict):
        items = raw.get("steps")
    if not isinstance(items, list) or not items:
        return [], "steps 要是一个非空数组，形如 " \
                    '[{"action":"open","url":"https://…"},{"action":"type","label":"搜索","value":"{{输入1}}"}]', ""
    steps: list[dict] = []
    warnings: list[str] = []
    for pos, item in enumerate(items, start=1):
        if len(steps) >= MAX_REPLAY_STEPS:
            warnings.append(f"步骤多于 {MAX_REPLAY_STEPS} 条，后面的 {len(items) - pos + 1} 条没有执行")
            break
        if not isinstance(item, dict):
            warnings.append(f"第 {pos} 条不是对象，已跳过")
            continue
        action = str(item.get("action") or "").strip().lower()
        if action not in REPLAY_ACTIONS:
            warnings.append(f"第 {pos} 条的动作「{action or '（空）'}」不在可回放范围（{ ' / '.join(REPLAY_ACTIONS)}），已跳过")
            continue
        step = {"action": action}
        # `input` / `sensitive` 也在认的键里：草案里的步骤就是这两个键形状，
        # 照抄回来却要丢掉输入位标记，等于把「必须由使用者给值」这一步变成空值
        for key in ("url", "label", "kind", "value", "input", "sensitive",
                    "direction", "amount", "submit"):
            if key in item:
                step[key] = item[key]
        if action == "open" and not str(item.get("url") or "").strip():
            warnings.append(f"第 {pos} 条 open 没有 url，已跳过")
            continue
        if action in ("click", "type", "select") and not str(item.get("label") or "").strip():
            # 没有标签就无法在「页面改版后」认元素——这正是分级与录制要避免的事
            warnings.append(f"第 {pos} 条 {action} 没有 label（要按文字标签认元素，不认坐标），已跳过")
            continue
        if action == "type" and "value" not in item:
            step["value"] = ""
        steps.append(step)
    if not steps:
        return [], "可用的回放步骤为 0 条：" + ("；".join(warnings) if warnings else "步骤里没有一条认得的动作"), ""
    return steps, "", "；".join(warnings)


def render_snapshot(data: dict, level: str, page_url: str) -> tuple[list[str], int]:
    """按档位渲染快照。返回（若干行，页面正文的字数）。

    `interactive` 的输出与 D-36 之前逐字一致——分级不能改变默认档的文案形状，
    否则既有自测与已经习惯这段格式的模型都会误判。字数取**渲染出来的**正文长度，
    审计里记的才是模型真正吃进去的量。
    """
    profile = SNAPSHOT_LEVEL_PROFILES[level]
    text = str(data.get("text") or "")
    cap = int(profile.get("pageText") or 0)
    if cap and len(text) > cap:
        text = text[:cap]
    elements = data.get("elements") if isinstance(data.get("elements"), list) else []
    lines = [
        f"页面：{data.get('title') or '（无标题）'}",
        f"URL：{page_url}",
    ]
    if level != "interactive":
        lines.append(f"档位：{level}（{SNAPSHOT_LEVEL_LABELS[level]}）")
    lines.append(
        f"滚动位置：y={data.get('scrollY', 0)} / 页面高度 {data.get('scrollHeight', 0)}，"
        f"视口 {((data.get('viewport') or {}).get('w') or 0)}x"
        f"{((data.get('viewport') or {}).get('h') or 0)}"
    )
    if level == "minimal":
        lines.append("可见文本：本档不返回页面正文（只给元素标签，用来定位再决定要不要换档）")
    elif text:
        lines.append("可见文本：\n" + text)
    else:
        lines.append("可见文本：没有可读取的文字（可能是画布、视频或尚未加载）")

    outline = data.get("outline") if isinstance(data.get("outline"), list) else []
    if profile.get("outline") and outline:
        lines.append("页面结构：")
        for node in outline:
            if not isinstance(node, dict):
                continue
            tag = str(node.get("tag") or "")
            body = str(node.get("text") or "").strip()
            count = node.get("items") or 0
            head = f"· {tag}" + (f"「{body}」" if body else "")
            if count:
                head += f"（{count} 项）"
            lines.append(head + f" y={node.get('top', 0)}")
        trunc = (data.get("truncated") or {}).get("outline")
        if trunc:
            lines.append(f"结构清单在上限 {MAX_OUTLINE_ITEMS} 条处截断，页面比这更长。")

    fields = data.get("fields") if isinstance(data.get("fields"), list) else []
    if profile.get("fields") and fields:
        lines.append("表单字段（只列名称与类型，不含已填写的值）：")
        for node in fields:
            if not isinstance(node, dict):
                continue
            bits = [str(node.get("kind") or "")]
            if node.get("label"):
                bits.append(f"「{node['label']}」")
            if node.get("password"):
                bits.append("密码框（引擎拒绝代填）")
            if node.get("required"):
                bits.append("必填")
            lines.append(f"- {' '.join(bits)} y={node.get('top', 0)}")

    if elements:
        lines.append("可交互元素（点击/输入用序号）：")
        for el in elements:
            if not isinstance(el, dict):
                continue
            label = str(el.get("label") or "").strip()
            tag = str(el.get("kind") or "")
            disabled = "（不可用）" if el.get("disabled") else ""
            shown = f"「{label}」" if label else "（无文字）"
            head = f"[{el.get('i')}] {tag} {shown}"
            if profile.get("geometry"):
                head += f" ({el.get('x', 0)},{el.get('y', 0)} {el.get('w', 0)}x{el.get('h', 0)})"
            extras: list[str] = []
            if profile.get("detail"):
                if el.get("href"):
                    extras.append(f"指向 {el['href']}")
                if el.get("name"):
                    extras.append(f"name={el['name']}")
                if el.get("role"):
                    extras.append(f"role={el['role']}")
                if el.get("id"):
                    extras.append(f"id={el['id']}")
                extras.append(f"层级 {el.get('depth', 0)}")
            lines.append(head + disabled + ("（" + "，".join(extras) + "）" if extras else ""))
        lines.append(
            "序号只在本次快照后有效；操作或页面变化后请重新 browser_snapshot。"
        )
        trunc = (data.get("truncated") or {}).get("elements") or 0
        if trunc:
            lines.append(f"另有 {trunc} 个可交互元素超过本档上限（{profile.get('elements')}）未列出。")
    else:
        lines.append(
            "可交互元素：未发现。如果页面是画布/图片界面，需要用 browser_screenshot 观察。"
        )

    links = data.get("links") if isinstance(data.get("links"), list) else []
    if profile.get("links") and links:
        lines.append("页面链接：")
        for node in links:
            if not isinstance(node, dict):
                continue
            lines.append(f"- {str(node.get('text') or '（无文字）')} → {str(node.get('href') or '')}")
        if (data.get("truncated") or {}).get("links"):
            lines.append(f"链接清单在 {MAX_LINK_ITEMS} 条处截断。")

    if data.get("iframes"):
        lines.append(
            f"提示：页面有 {data['iframes']} 个 iframe，其内部内容不在本次快照中。"
        )
    return lines, len(text)


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
    # D-36：录制中的语义步骤（内存态，`browser_record` 的 save 才落盘成草案）与当前标签页标识
    _recording: bool = False
    _steps: list = field(default_factory=list)
    _record_truncated: bool = False
    _record_skipped: int = 0
    _record_saved: str = ""
    _tab_id: str = ""
    _session_target: str = ""
    _last_level: str = ""

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

    def has_feature(self, name: str) -> bool:
        """桌面壳有没有说「这个动作我认识」：只有显式 True 才算，缺键就是没有"""
        return self.ready and self.features.get(name) is True

    def gated_actions(self) -> list[str]:
        """已就绪时，因网关不认动作而不能注册的那几件（按 FEATURE_ONLY_ACTIONS 的顺序）"""
        return [name for name in FEATURE_ONLY_ACTIONS if not self.has_feature(name)]

    def notes(self) -> list[str]:
        if self.ready:
            lines = [
                f"浏览器 AI+ 已启用（{POLICY_LABELS.get(self.policy, self.policy)}）："
                f"使用 {self.browser or '本机浏览器'} 的独立配置目录，只读/操作受控窗口，"
                "只允许 http/https；每次操作按档位确认并写入审计"
            ]
            if not self.vision:
                lines.append("当前模型没有视觉能力：浏览器截图工具未注册，页面理解走 DOM 快照")
            gated = self.gated_actions()
            if gated:
                # 说清楚「为什么没有悬停/按键/标签页」：这些动作要网关先认识，否则每次都会被拒绝
                lines.append(
                    "以下浏览器动作本轮不可用（桌面壳的浏览器网关还没有登记它们，"
                    f"「每次询问」档下会每次被拒）：{'、'.join(gated)}"
                )
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
        level_line = (
            "- browser_snapshot 的 level：minimal（只看有哪些可点）/ interactive（默认，带坐标）"
            "/ structured（先看标题与表单骨架）/ full（正文+链接+字段名）。"
            "**先 minimal 或 structured 定位，再按需 full**；四档都会重新分配元素序号。\n"
        )
        record_line = (
            "- browser_record：把本轮做过的浏览器操作存成**草案技能**（默认不启用，需人工确认）；"
            "步骤按元素文字标签记录，输入内容记成占位符（形如 {{搜索-1}}），不落盘任何输入正文。\n"
            "- browser_replay：按步骤清单重演录制流程，每步仍走同一个放行网关；"
            "用 inputs 传「占位符 → 本次要用的值」来替换录制时的输入位——这就是「改一个输入再跑一遍」。\n"
            "- browser_wait：等待页面就绪（最多 {wait} 秒），只轮询状态，不读页面内容。\n"
        ).replace("{wait}", f"{MAX_WAIT_SECONDS:g}")
        gated = self.gated_actions()
        enabled = [FEATURE_TOOL_NAMES[n] for n in FEATURE_ONLY_ACTIONS if self.has_feature(n)]
        extra_line = (
            "- 本机网关已登记这些浏览器动作，可以直接用：" + "、".join(enabled) + "。\n"
            if enabled
            else ""
        )
        gated_line = (
            f"- 本轮没有的浏览器动作：{'、'.join(gated)}（页面里需要悬停展开菜单时，"
            "改用 browser_click 点触发元素；需要按键时改用 browser_type(submit=true)）。\n"
            if gated
            else ""
        )
        return (
            "\n\n## 浏览器 AI+（用户已启用）\n"
            "- browser_open：在受控浏览器中打开 http/https 页面。"
            "受控浏览器使用独立配置目录，不带你日常浏览器的登录态。\n"
            "- browser_snapshot：读取当前页面的可见文字与可交互元素序号（[1]、[2]…）。"
            "**点击/输入必须先快照，再按序号操作**；页面变化后序号会失效，需要重新快照。\n"
            + level_line +
            "- browser_click：按序号点击元素（真实鼠标事件，会触发展开、跳转、提交等副作用）。\n"
            "- browser_type：按序号向输入框输入文本（clear=true 时先清空）；密码框会被拒绝；"
            "下拉框用 text 匹配选项。\n"
            "- browser_scroll：滚动页面，取回新的滚动位置。\n"
            + vision_line + record_line + extra_line + gated_line +
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
        # 标签页切换靠这里带出去的 tabId：今天的桌面壳不认这个键也没关系——它回的还是同一个页面，
        # browser_tab(switch) 会发现 targetId 没变并如实说「桌面半边没接」，不会假装切成功了。
        payload: dict = {}
        if self._tab_id:
            payload["tabId"] = self._tab_id
        data, err = await self._post("/ensure", payload, ENSURE_TIMEOUT)
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
        self._session_target = str(data.get("targetId") or "")
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

    # ── 录制（D-36）─────────────────────────────────────────────

    def _record(
        self,
        action: str,
        *,
        kind: str = "",
        label: str = "",
        url: str = "",
        value: str = "",
        direction: str = "",
        submit: bool = False,
        amount: int = 0,
    ) -> None:
        """把一次成功的操作记成**语义步骤**（只有开着录制时才记）

        三条不能退让的规矩：
        1. 输入正文不落盘也不留在内存里——只留一个占位符，回放时必须由使用者给值；
        2. 像密码/令牌的字段连占位符都标 `sensitive`，回放时拒绝代填（见 `_replay_inputs`）；
        3. 记的是标签不是坐标：坐标随窗口与重排失效，抄进技能就得到一个今天能跑、明天点歪的录像。
        """
        if not self._recording:
            return
        if len(self._steps) >= MAX_RECORD_STEPS:
            self._record_truncated = True
            return
        number = len(self._steps) + 1
        step: dict[str, Any] = {"n": number, "action": action}
        if url:
            # 查询串与片段里常见 token / 搜索词，草案是要落盘给人读的文件
            step["url"] = _audit_url(url)
        if kind:
            step["kind"] = kind
        if label:
            step["label"] = label[:120]
        if direction:
            step["direction"] = direction
        if amount:
            step["amount"] = int(amount)
        if submit:
            step["submit"] = True
        if action in ("type", "select"):
            secret = _looks_secret(f"{label} {kind}")
            step["input"] = (
                f"{{{{需要用户亲自输入-{label or kind}-{number}}}}}" if secret else _placeholder(number, label)
            )
            if secret:
                step["sensitive"] = True
            # `value` 参数存在只是为了让调用点说明「这一步确实带了正文」：这里刻意不保存它
            step["hasValue"] = bool(value)
        self._steps.append(step)

    def _record_skip(self, action: str) -> None:
        """录到了但进不了草案的动作（悬停/按键/脚本）只计数，不写成步骤。

        草案的价值是「换一页也能跑」，把只有当前页面才成立的脚本调用抄进去，
        下次回放出来的是一个看起来能跑、实际点不开的东西；宁可显式说「这里缺一步」。
        """
        if self._recording:
            self._record_skipped += 1

    def _draft_body(self, steps: list[dict]) -> str:
        """草案正文：步骤 + 输入位表 + 明确「不含任何输入正文」"""
        lines = [
            "## 浏览器流程（录制生成，未经人工确认）",
            "",
            f"- 共 {len(steps)} 步，元素按**文字标签**定位（不保存坐标）",
            "- 本文件不含任何输入正文；带 `输入位` 的步骤在回放时需要使用者给值",
            "",
            "### 步骤",
            "",
        ]
        inputs: list[str] = []
        for step in steps:
            action = str(step.get("action") or "")
            bits: list[str] = []
            if step.get("url"):
                bits.append(f"地址 {step['url']}")
            if step.get("label"):
                bits.append(f"元素「{step['label']}」")
            if step.get("kind"):
                bits.append(f"类型 {step['kind']}")
            if step.get("direction"):
                screen = f" {step['amount']} 屏" if step.get("amount") else ""
                bits.append(f"方向 {step['direction']}{screen}")
            if step.get("input"):
                bits.append(f"输入位 {step['input']}")
                inputs.append(
                    f"- {step['input']}：第 {step['n']} 步"
                    + ("（敏感字段：请由用户亲自输入，回放不会代填）" if step.get("sensitive") else "")
                )
            if step.get("submit"):
                # 不按 Enter 的录制回放出来是「填完不提交」，和录的时候不一样
                bits.append("输入后按 Enter")
            head = f"{step['n']}. {action}"
            lines.append(f"{head}：" + "，".join(bits) if bits else head)
        if inputs:
            lines += ["", "### 需要使用者提供的输入位", ""] + inputs
        if self._record_truncated:
            lines += ["", f"> 注意：录制在第 {MAX_RECORD_STEPS} 步被截断（单次录制上限），后面的操作没有记下来。"]
        if self._record_skipped:
            lines += [
                "",
                f"> 注意：录制期间有 {self._record_skipped} 个动作（悬停/按键/脚本）没有写进草案——"
                "草案只存可回放的语义步骤。若流程依赖这些动作，回放前需要人工补上等价的一步。",
            ]
        # 落盘的 JSON 必须能被 `browser_replay` 直接吃：`n` / `hasValue` 是录制侧的记账字段，
        # 留在步骤里只会让模型照抄一份认不出的键
        replayable = [
            {k: v for k, v in step.items() if k not in ("n", "hasValue")}
            for step in steps
        ]
        lines += [
            "",
            "### 回放方式",
            "",
            "把下面的步骤交给 `browser_replay`（`steps` 参数），并用 `inputs` 填输入位；"
            "**改一个输入位就能在别的页面上重跑**，元素靠文字标签重新定位，不靠坐标。"
            "每一步仍会按当前档位过网关确认。",
            "",
            "```json",
            json.dumps(replayable, ensure_ascii=False, indent=2),
            "```",
        ]
        return "\n".join(lines)

    def _replay_inputs(self, raw: object) -> dict[str, str]:
        """使用者给的输入位取值：只收字符串，长度与条数都按既有上限"""
        if not isinstance(raw, dict):
            return {}
        out: dict[str, str] = {}
        for key, value in raw.items():
            name = str(key or "").strip()
            if not name or not isinstance(value, str):
                continue
            out[name] = value[:MAX_TYPE_CHARS]
        return out

    def _step_value(self, step: dict, inputs: dict[str, str]) -> tuple[str, str]:
        """这一步要输入什么：`(值, 错误文案)`。敏感字段永远拒绝代填。

        敏感判定不靠草案里那个 `sensitive` 标记——模型手抄步骤时一定会把它抄丢，
        所以这里按**当前这一步的标签**重新认一遍：标签像密钥就拒，不管来源是录制还是手写。
        """
        label = str(step.get("label") or "")
        kind = str(step.get("kind") or "")
        if step.get("sensitive") or _looks_secret(f"{label} {kind}"):
            return "", (
                f"「{label or kind}」像密码/密钥一类字段：录制里没有保存内容，回放也不会代填；"
                "请由用户亲自输入"
            )
        want = str(step.get("input") or "")
        value = str(step.get("value") or "")
        marker = ""
        if want.startswith("{{") and want.endswith("}}"):
            marker = want
        elif value.startswith("{{") and value.endswith("}}"):
            # 模型常把草案里的 `{{搜索-1}}` 原样抄进 `value`：按占位符去找值，
            # 而不是把花括号本身敲进页面（那会搜出「{{搜索-1}}」这个字符串）
            marker = value
        if marker:
            if marker in inputs:
                return inputs[marker], ""
            return "", (
                f"输入位 {marker} 还没有值：录制里没有保存正文，"
                "请在 inputs 里给出这一步要用的内容"
            )
        return (value or want), ""

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
        toolkit.register_tool_function(self._wait_tool(emit, task_id))
        toolkit.register_tool_function(self._record_tool(emit, task_id))
        toolkit.register_tool_function(self._replay_tool(emit, task_id))
        # 网关认识才注册：这些动作要走 `/approve` 的 `is_known_action` 白名单，
        # Rust 不认识它们时「每次询问」会每次都拒——注册出来是个必被拒绝的按钮
        if self.has_feature("hover"):
            toolkit.register_tool_function(self._hover_tool(emit, task_id))
        if self.has_feature("press"):
            toolkit.register_tool_function(self._press_tool(emit, task_id))
        if self.has_feature("evaluate"):
            toolkit.register_tool_function(self._evaluate_tool(emit, task_id))
        if self.has_feature("tabs"):
            toolkit.register_tool_function(self._tab_tool(emit, task_id))
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
                self._record("open", url=target)
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
        async def browser_snapshot(level: str = "interactive"):
            """读取当前页面的可见文字与可交互元素序号，可按档位分级。

            元素序号只在本次快照后有效；点击、输入或页面跳转后需要重新快照。
            返回内容不包含输入框里的值（密码等敏感内容不会进入模型）。

            Args:
                level (`str`, optional):
                    快照档位，默认 interactive。minimal 最省（只有元素标签与位置，不含
                    页面正文，适合先扫一遍再决定）；interactive 给元素坐标与可见正文；
                    structured 给标题层级/地标/表单字段名与简短正文；full 在正文之外再加
                    链接清单与每个元素的 href/name/role/id。四档都会重新分配元素序号。
            """
            tool = "browser_snapshot"
            lvl, err = normalize_level(level)
            if err:
                self._audit(emit, task_id, tool, self._page_target(), "denied", err)
                return _text_response(f"错误：页面快照未执行。{err}")
            limit = self._budget(self._snapshots, MAX_SNAPSHOTS, "页面快照")
            if limit:
                return _text_response(f"错误：{limit}")
            try:
                page = await self._current_page()
                target = str(page.get("url") or self._page_target())
                await self._approve("snapshot", target, f"档位 {lvl}", emit, task_id, tool)
                self._snapshots += 1
                profile = SNAPSHOT_LEVEL_PROFILES[lvl]
                js = (
                    SNAPSHOT_JS.replace("__SELECTOR__", INTERACTIVE_SELECTOR)
                    .replace("__LEVEL__", lvl)
                    .replace("__MAX_ELEMENTS__", str(int(profile["elements"])))
                    .replace("__MAX_PAGE_TEXT__", str(int(profile["pageText"])))
                    .replace("__MAX_OUTLINE__", str(MAX_OUTLINE_ITEMS))
                    .replace("__MAX_LINKS__", str(MAX_LINK_ITEMS))
                )
                data = await self._eval(js) or {}
                if not isinstance(data, dict):
                    raise ToolError("页面快照返回了无法解析的结果")
                self._page_url = str(data.get("url") or target)
                self._last_level = lvl
                lines, text_chars = render_snapshot(data, lvl, self._page_url)
                elements = data.get("elements") if isinstance(data.get("elements"), list) else []
                self._audit(
                    emit, task_id, tool, self._page_url, "ok",
                    f"{lvl} 快照：元素 {len(elements)} 个，文本 {text_chars} 字",
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
                self._record("click", kind=str(el.get("kind") or ""),
                             label=str(el.get("label") or ""))
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
                    self._record("select", kind="select",
                                 label=str(el.get("label") or ""), value=body)
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
                self._record("type", kind=str(el.get("kind") or ""),
                             label=str(el.get("label") or ""), value=body,
                             submit=bool(submit))
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
                self._record("scroll", direction=d, amount=n)
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

    # ── D-36：等待 / 录制 / 回放 ────────────────────────────────

    def _wait_tool(self, emit, task_id: str):
        async def browser_wait(seconds: float = 1.0):
            """等待页面自己变成就绪状态；只轮询加载状态，不读取页面内容。

            Args:
                seconds (`float`, optional):
                    最长等待秒数，默认 1，最多 10。到点仍未就绪也会正常返回并说明
                    当前状态——「还没好」不是失败，需要继续等可以再调一次。
            """
            tool = "browser_wait"
            try:
                want = float(seconds)
            except Exception:  # noqa: BLE001
                self._audit(emit, task_id, tool, self._page_target(), "denied", "seconds 非数字")
                return _text_response("错误：等待未执行。seconds 必须是数字")
            if want <= 0:
                return _text_response("错误：等待未执行。seconds 必须大于 0")
            capped = want > MAX_WAIT_SECONDS
            if capped:
                want = MAX_WAIT_SECONDS
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            self._actions += 1
            loop = asyncio.get_running_loop()
            started = loop.time()
            deadline = started + want
            state: dict = {}
            error = ""
            while True:
                try:
                    state = await self._current_page()
                except ToolError as exc:
                    error = str(exc)
                    break
                if str(state.get("ready") or "") in ("interactive", "complete"):
                    break
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(0.2)
            elapsed = max(0.0, loop.time() - started)
            if error:
                self._audit(emit, task_id, tool, self._page_target(), "error", error)
                return _text_response(f"错误：等待失败。{error}")
            ready = str(state.get("ready") or "")
            self._page_url = str(state.get("url") or self._page_url)
            self._audit(
                emit, task_id, tool, self._page_url, "ok",
                f"等待 {want:g}s（实耗 {elapsed:.2f}s），ready={ready or '未知'}",
            )
            if ready in ("interactive", "complete"):
                head = f"页面已就绪（{ready}），等待 {elapsed:.2f} 秒。"
            else:
                head = (
                    f"等了 {elapsed:.2f} 秒，页面仍是「{ready or '未知状态'}」。"
                    "这不是失败：先 browser_snapshot 看看现有内容是否已经够用。"
                )
            tail = (
                f"（请求的 {seconds} 秒超过上限，已按 {MAX_WAIT_SECONDS:g} 秒执行）"
                if capped else ""
            )
            return _text_response(head + tail)

        return browser_wait

    def _record_tool(self, emit, task_id: str):
        async def browser_record(
            action: str = "status",
            skill_id: str = "",
            title: str = "",
            description: str = "",
        ):
            """录制浏览器操作流程，并把它存成**草案技能**（未经人工确认不会进执行面）。

            Args:
                action (`str`, optional):
                    start 开始录制 / status 查看进度 / preview 只看草案文字（不写文件）/
                    save 存成草案技能 / cancel 丢弃本轮录制。默认 status。
                skill_id (`str`, optional):
                    save 必填：草案标识，只能用字母、数字、点、下划线、横线，最长 64 个字符。
                title (`str`, optional):
                    草案显示名，默认用 skill_id。
                description (`str`, optional):
                    草案一句话说明，默认取正文开头。
            """
            tool = "browser_record"
            act = str(action or "status").strip().lower()
            if act in ("peek", "看", "进度"):
                act = "status"
            if act in ("预览", "show"):
                act = "preview"
            if act not in ("start", "status", "preview", "save", "cancel"):
                self._audit(emit, task_id, tool, "", "denied", f"未知动作 {act}")
                return _text_response(
                    "错误：录制操作未执行。action 只能是 start / status / preview / save / cancel"
                )
            if act == "start":
                if self._recording:
                    return _text_response(
                        f"录制已在进行中：已记 {len(self._steps)} 步。"
                        "先 save 或 cancel，再重新 start。"
                    )
                self._recording = True
                self._steps = []
                self._record_truncated = False
                self._record_skipped = 0
                self._audit(emit, task_id, tool, "", "ok", "开始录制浏览器流程")
                return _text_response(
                    "已开始录制。接下来的 browser_open / browser_click / browser_type / "
                    "browser_scroll 会记成**语义步骤**：元素只记文字标签（不记坐标），"
                    "输入内容只记占位符（正文既不落盘也不留存）。"
                    "录完用 browser_record(action='preview') 先看，再 save。"
                )
            if act == "cancel":
                was, count = self._recording, len(self._steps)
                self._recording = False
                self._steps = []
                self._record_truncated = False
                self._record_skipped = 0
                self._audit(emit, task_id, tool, "", "ok", f"放弃录制（原 {count} 步）")
                return _text_response(
                    f"已放弃本轮录制（{'录制中' if was else '本来没在录'}，丢弃 {count} 步），没有写任何文件。"
                )
            if act == "status":
                marks = [
                    str(s.get("input")) for s in self._steps
                    if s.get("input") and str(s.get("input")).startswith("{{")
                ]
                lines = [
                    f"录制状态：{'进行中' if self._recording else '未开始'}",
                    f"已记步骤：{len(self._steps)} 步（上限 {MAX_RECORD_STEPS}）"
                    + ("，**已触顶截断**" if self._record_truncated else ""),
                    f"未进草案的动作：{self._record_skipped} 个（悬停/按键/脚本这类只在当前页面成立的步骤）",
                    f"输入位：{('、'.join(marks)) if marks else '无'}",
                    f"上次存为：{self._record_saved or '（还没存过）'}",
                ]
                return _text_response("\n".join(lines))
            if act == "preview":
                if not self._steps:
                    return _text_response(
                        "还没有可预览的步骤：先 browser_record(action='start')，再把流程走一遍。"
                    )
                return _text_response(
                    "草案预览（尚未写文件；确认无误后用 browser_record(action='save', skill_id=…) 落盘）：\n\n"
                    + self._draft_body(self._steps)
                )
            # save
            if not self._steps:
                self._audit(emit, task_id, tool, "", "denied", "没有步骤可存")
                return _text_response("错误：没有录制到任何步骤，不写草案。")
            clean_id = str(skill_id or "").strip()
            if not clean_id:
                return _text_response("错误：save 需要 skill_id（草案标识），否则会写出一个没人认领的文件。")
            try:
                import skill_registry_runtime as drafts
            except Exception as exc:  # noqa: BLE001
                self._audit(emit, task_id, tool, "", "error", f"草案模块不可用：{_brief(exc)}")
                return _text_response(f"错误：技能草案模块不可用，未写入。{_brief(exc)}")
            body = self._draft_body(self._steps)
            entry, notice = drafts.add_draft(
                dp.engine_home(), clean_id, body,
                title=str(title or ""), description=str(description or ""),
                origin="浏览器录制",
            )
            if entry is None:
                self._audit(emit, task_id, tool, f"skill_pool/drafts/{clean_id}", "denied", notice)
                return _text_response(f"错误：草案未写入。{notice}")
            self._recording = False
            self._record_saved = str(getattr(getattr(entry, "source", None), "path", "") or "")
            self._audit(
                emit, task_id, tool, f"skill_pool/drafts/{clean_id}", "ok",
                f"草案 {clean_id}：{len(self._steps)} 步，输入位 "
                f"{sum(1 for s in self._steps if s.get('input'))} 个（不含任何输入正文）",
            )
            # 确认文案取自 add_draft 自己：技能池怎么说就怎么转述，引擎不再编一份「已启用」
            return _text_response(
                f"{notice}\n"
                f"共 {len(self._steps)} 步，元素按文字标签定位，输入位是占位符——文件里没有任何输入正文。\n"
                "本轮想直接验证可复现性，可以把草案里的步骤交给 browser_replay，"
                "用 inputs 换掉输入位再跑一遍。"
            )

        return browser_record

    async def _find_label(self, label: str) -> dict:
        js = (
            FIND_LABEL_JS.replace("__SELECTOR__", INTERACTIVE_SELECTOR)
            .replace("__LABEL__", json.dumps(str(label or ""), ensure_ascii=False))
        )
        data = await self._eval(js)
        if not isinstance(data, dict):
            raise ToolError("按标签定位返回了无法解析的结果")
        if data.get("error"):
            raise ToolError(str(data["error"]))
        return data

    def _replay_tool(self, emit, task_id: str):
        async def browser_replay(steps: list, inputs: dict | None = None):
            """按步骤清单重演一段浏览器流程（通常来自 browser_record 存的草案）。

            Args:
                steps (`list`):
                    步骤对象数组，每项形如 {"action":"open","url":"https://…"}、
                    {"action":"click","label":"提交"}、
                    {"action":"type","label":"搜索","value":"阿里云"}、
                    {"action":"select","label":"城市","value":"北京"}、
                    {"action":"scroll","direction":"down","amount":1}。
                    元素靠**文字标签**定位（不认坐标），动作只认
                    open / click / type / select / scroll。
                inputs (`dict`, optional):
                    「占位符 → 本次要用的值」，用来替换草案里的输入位，
                    例如 {"{{搜索-1}}": "阿里云"}。敏感字段即使给了值也不会代填。
            """
            tool = "browser_replay"
            plan, err, warn = normalize_replay_steps(steps)
            if err:
                self._audit(emit, task_id, tool, self._page_target(), "denied", err)
                return _text_response(f"错误：回放未开始。{err}")
            values = self._replay_inputs(inputs)
            # 逐步走既有工具，而不是另写一条通路：放行、审计、预算、密码框拒绝
            # 这些性质必须和「人肉一步步做」完全一致
            runners = {
                "open": self._open_tool(emit, task_id),
                "click": self._click_tool(emit, task_id),
                "type": self._type_tool(emit, task_id),
                "scroll": self._scroll_tool(emit, task_id),
            }
            lines: list[str] = [f"回放开始：{len(plan)} 步（每步仍按当前档位过网关）"]
            if warn:
                lines.append(f"注意：{warn}")
            stopped = ""
            for pos, step in enumerate(plan, start=1):
                action = str(step.get("action") or "")
                if action == "open":
                    url = str(step.get("url") or "")
                    out = _response_text(await runners["open"](url))
                    label = f"打开 {_audit_url(url)}"
                elif action == "click":
                    name = str(step.get("label") or "")
                    try:
                        found = await self._find_label(name)
                        out = _response_text(await runners["click"](1))
                    except ToolError as exc:
                        found, out = {}, f"错误：{exc}"
                    label = f"点击「{name}」" + _ambiguity(found)
                elif action in ("type", "select"):
                    name = str(step.get("label") or "")
                    value, verr = self._step_value(step, values)
                    if verr:
                        found, out = {}, f"错误：{verr}"
                        label = f"{'选择' if action == 'select' else '输入'}「{name}」（需要输入位）"
                    else:
                        try:
                            found = await self._find_label(name)
                            out = _response_text(
                                await runners["type"](1, value, True, bool(step.get("submit")))
                            )
                        except ToolError as exc:
                            found, out = {}, f"错误：{exc}"
                        label = (
                            f"{'选择' if action == 'select' else '输入'}「{name}」"
                            f"{len(value)} 个字符" + _ambiguity(found)
                        )
                else:  # scroll
                    direction = str(step.get("direction") or "down")
                    amount = step.get("amount") or 1
                    try:
                        n = int(amount)
                    except Exception:  # noqa: BLE001
                        n = 1
                    out = _response_text(await runners["scroll"](direction, max(1, min(10, n))))
                    label = f"滚动 {direction} {n} 屏"
                if out.startswith("错误："):
                    stopped = out
                    lines.append(f"{pos}. {label} → 失败：{out[len('错误：'):]}")
                    break
                lines.append(f"{pos}. {label} → 完成")
            if stopped:
                done = len(lines) - 2 - (1 if warn else 0)
                return _text_response(
                    "\n".join(lines)
                    + f"\n\n回停在第 {min(pos + 1, len(plan))} 步（已完成 {max(0, done)} 步）。"
                    + "这一步需要的页面条件没满足——草案是录制当时的页面，改版或换页都可能让它对不上；"
                    "请人工确认后重新录制，或补齐 inputs。"
                )
            self._audit(
                emit, task_id, tool, self._page_target(), "ok",
                f"回放 {len(plan)} 步全部完成",
            )
            return _text_response("\n".join(lines) + f"\n\n回放完成：{len(plan)} 步全部走通。")

        return browser_replay

    # ── D-36：网关认识才注册的四件 ──────────────────────────────

    def _hover_tool(self, emit, task_id: str):
        async def browser_hover(index: int):
            """把鼠标悬停到当前页面的一个元素上（会触发菜单、提示等页面副作用）。

            Args:
                index (`int`):
                    browser_snapshot 返回的元素序号。
            """
            tool = "browser_hover"
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            target = self._page_target()
            try:
                el = await self._resolve(index)
                target = self._page_url or target
                detail = f"{el.get('kind')}「{el.get('label') or '无文字'}」"
                await self._approve("hover", target, detail, emit, task_id, tool)
                self._actions += 1
                x = float(el.get("x") or 0)
                y = float(el.get("y") or 0)
                for offset in (0.0, 1.0):
                    await self._cdp_call(
                        "Input.dispatchMouseEvent",
                        {"type": "mouseMoved", "x": x + offset, "y": y,
                         "button": "none", "clickCount": 0},
                    )
                await asyncio.sleep(0.25)
                self._record_skip("hover")
                self._audit(emit, task_id, tool, target, "ok", detail)
                return _text_response(
                    f"已悬停 [{index}] {detail}。菜单或提示可能已经展开——"
                    "要确认结果请重新 browser_snapshot。"
                )
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：悬停未执行。{exc}")

        return browser_hover

    def _press_tool(self, emit, task_id: str):
        async def browser_press(key: str, index: int = 0):
            """向当前页面按下键盘按键（配合 Tab 逐步聚焦表单时使用）。

            Args:
                key (`str`):
                    按键名，只认有限集合：enter / tab / escape / backspace / delete /
                    space / arrowup / arrowdown / arrowleft / arrowright / pageup /
                    pagedown / home / end。
                index (`int`, optional):
                    先把焦点落到 browser_snapshot 的这个序号上，默认 0 表示不动焦点、
                    直接发给页面当前聚焦的元素（引擎会先看一眼聚焦的是什么元素，
                    落在密码框上就拒绝发送按键）。
            """
            tool = "browser_press"
            name = str(key or "").strip().lower().replace(" ", "").replace("-", "")
            if name not in KEY_CODES:
                self._audit(emit, task_id, tool, self._page_target(), "denied", f"按键 {key}")
                return _text_response(
                    f"错误：按键未执行。「{key}」不在可发送的按键里；可用：{' / '.join(sorted(set(KEY_CODES)))}"
                )
            key_label, vk = KEY_CODES[name]
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            target = self._page_target()
            try:
                focused = ""
                if index:
                    el = await self._resolve(index)
                    if el.get("isPassword"):
                        raise ToolError("密码框不接受浏览器 AI+ 的按键输入，请由用户亲自操作")
                    data = await self._eval(FOCUS_JS.replace("__INDEX__", str(int(index))))
                    if isinstance(data, dict) and data.get("error"):
                        raise ToolError(str(data["error"]))
                    focused = f"（先聚焦 [{index}]"
                    focused += f" {el.get('kind')}「{el.get('label') or '无文字'}」" if el.get("label") else ""
                    focused += "）"
                else:
                    # 不带序号 = 发给页面自己聚焦的那个元素；不看一眼就发按键，
                    # 等于可能把 Backspace / Delete 送进一个密码框
                    probe = await self._eval(ACTIVE_JS)
                    if isinstance(probe, dict):
                        if probe.get("isPassword"):
                            raise ToolError(
                                "当前聚焦的是密码框：按键（含 Backspace / Delete / 空格）"
                                "不接受浏览器 AI+ 的输入，请由用户亲自操作"
                            )
                        if not probe.get("none"):
                            name = str(probe.get("label") or "")
                            focused = (
                                f"（发给页面当前聚焦的 {probe.get('tag') or '元素'}"
                                f"{'「' + name + '」' if name else ''}）"
                            )
                target = self._page_url or target
                detail = f"按键 {key_label}{focused}"
                await self._approve("press", target, detail, emit, task_id, tool)
                self._actions += 1
                for typ in ("keyDown", "keyUp"):
                    await self._cdp_call(
                        "Input.dispatchKeyEvent",
                        {"type": typ, "key": key_label, "code": key_label,
                         "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk},
                    )
                await asyncio.sleep(0.2)
                self._record_skip("press")
                self._audit(emit, task_id, tool, target, "ok", detail)
                return _text_response(
                    f"已按下 {key_label}{focused}。页面状态可能已变化，请重新 browser_snapshot 确认。"
                )
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：按键未执行。{exc}")

        return browser_press

    def _evaluate_tool(self, emit, task_id: str):
        async def browser_evaluate(expression: str):
            """在当前页面执行一段 JavaScript 并回传结果（只在本机受控页面里跑）。

            Args:
                expression (`str`):
                    要执行的 JavaScript 表达式，最长 4000 字符。可以是返回 Promise 的表达式，
                    结果会被 JSON 化并按上限截断。脚本能读到的只有该页面自己的数据。
            """
            tool = "browser_evaluate"
            expr = str(expression or "").strip()
            if not expr:
                return _text_response("错误：脚本未执行。缺少 expression")
            if len(expr) > MAX_TYPE_CHARS:
                return _text_response(f"错误：脚本未执行。表达式最长 {MAX_TYPE_CHARS} 个字符")
            limit = self._budget(self._actions, MAX_ACTIONS, "浏览器操作")
            if limit:
                return _text_response(f"错误：{limit}")
            target = self._page_target()
            try:
                page = await self._current_page()
                target = str(page.get("url") or target)
                detail = f"执行脚本 {len(expr)} 字符（表达式不进审计）"
                await self._approve("evaluate", target, detail, emit, task_id, tool)
                self._actions += 1
                data = await self._eval(
                    EVALUATE_JS.replace("__EXPR__", json.dumps(expr, ensure_ascii=False))
                    .replace("__MAX_RESULT__", str(MAX_PAGE_TEXT))
                )
                if not isinstance(data, dict):
                    raise ToolError("脚本返回了无法解析的结果")
                if data.get("error"):
                    self._audit(emit, task_id, tool, target, "error", f"脚本抛错：{data['error']}")
                    return _text_response(f"脚本执行报错：{data['error']}")
                self._record_skip("evaluate")
                value = str(data.get("value") or "")
                self._audit(emit, task_id, tool, target, "ok", f"{detail}，回传 {len(value)} 字")
                tail = "（结果超过上限，已截断）" if data.get("truncated") else ""
                return _text_response(
                    f"脚本结果（类型 {data.get('type') or '未知'}）：\n{value}{tail}"
                )
            except ToolError as exc:
                if not isinstance(exc, DeniedError):
                    self._audit(emit, task_id, tool, target, "error", str(exc))
                return _text_response(f"错误：脚本未执行。{exc}")

        return browser_evaluate

    def _tab_tool(self, emit, task_id: str):
        async def browser_tab(action: str = "list", tab_id: str = "", url: str = ""):
            """管理受控浏览器的标签页：列出 / 新建 / 切换 / 关闭。

            Args:
                action (`str`, optional):
                    list 列出当前标签页 / new 新建 / switch 切到 tab_id / close 关闭 tab_id。
                    默认 list。
                tab_id (`str`, optional):
                    switch / close 必填，取 list 返回的标识。
                url (`str`, optional):
                    new 时打开的地址，只允许 http/https。
            """
            tool = "browser_tab"
            act = str(action or "list").strip().lower()
            if act not in ("list", "new", "switch", "close"):
                return _text_response(
                    "错误：标签页操作未执行。action 只能是 list / new / switch / close"
                )
            ident = str(tab_id or "").strip()
            if act in ("switch", "close") and not ident:
                return _text_response(f"错误：标签页操作未执行。{act} 需要 tab_id")
            target = self._page_target()
            try:
                detail = f"{act}" + (f" {ident}" if ident else "")
                await self._approve("tabs", target, detail, emit, task_id, tool)
                self._actions += 1
            except ToolError as exc:
                return _text_response(f"错误：标签页操作未执行。{exc}")
            payload: dict = {"action": act}
            if ident:
                payload["tabId"] = ident
            if act == "new":
                page, uerr = normalize_url(url)
                if uerr:
                    self._audit(emit, task_id, tool, "", "denied", uerr)
                    return _text_response(f"错误：新建标签页未执行。{uerr}")
                payload["url"] = page
            data, err = await self._post("/tabs", payload, ENSURE_TIMEOUT)
            if err:
                self._audit(emit, task_id, tool, target, "denied", err)
                return _text_response(
                    f"错误：桌面壳的浏览器网关没有实现标签页接口（{err}）。"
                    "能力位说它支持 tabs，但 `/tabs` 这条路还没接上——"
                    "这一步需要桌面半边落地，引擎不会假装成功。"
                )
            if act in ("switch", "new"):
                got = str(data.get("tabId") or "") or ident
                self._tab_id = got
                await self._reset_session()
                cdp = await self._ensure_cdp()  # 带 tabId 重新取页面地址
                if act == "switch" and self._session_target and ident and ident not in self._session_target:
                    # 网关收了 tabId 但返回的还是同一个页面：说明桌面半边只认「一个页面」
                    self._audit(
                        emit, task_id, tool, target, "error",
                        f"switch 后 targetId 未变（{self._session_target}）",
                    )
                    return _text_response(
                        f"错误：切换标签页没有生效——网关返回的仍是原来的页面（目标 {self._session_target}，"
                        f"你要的是 {ident}）。桌面半边没有按 tabId 取回连接地址，引擎不假装切过去了。"
                    )
                _ = cdp
            state = await self._current_page()
            self._page_url = str(state.get("url") or self._page_url)
            tabs = data.get("tabs") if isinstance(data.get("tabs"), list) else []
            self._audit(
                emit, task_id, tool, self._page_url, "ok",
                f"{act} 完成，标签页 {len(tabs)} 个" if tabs else f"{act} 完成",
            )
            lines = [f"标签页操作 {act} 已完成。"]
            if tabs:
                lines.append("当前标签页：")
                for item in tabs:
                    if not isinstance(item, dict):
                        continue
                    mark = "（当前）" if item.get("active") else ""
                    lines.append(
                        f"- {item.get('id') or '（无标识）'} {_audit_url(str(item.get('url') or ''))}"
                        f" {str(item.get('title') or '')}{mark}"
                    )
            else:
                lines.append(f"当前页面：{self._page_url or '（未知）'}")
            return _text_response("\n".join(lines))

        return browser_tab
