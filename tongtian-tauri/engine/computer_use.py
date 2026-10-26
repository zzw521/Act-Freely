"""执行面 P1/P2：电脑操控（Windows 截屏 / 键鼠）

Rust 随 `runtime.computerUse` 下发平台能力、用户档位与一次性回环网关：

    computerUse = {
        supported, platform, features, policy, persisted,
        gateUrl, gateToken,          # 仅在可用时出现
        gateError, error,            # 不可用时说明原因
    }

引擎只做「请求」：截屏与键鼠注入都在桌面壳（Rust）里执行，策略门禁、系统确认框
与审计也都在那一侧。因此本模块不直接调用任何系统 API，也拿不到绕开档位的能力。

边界：
- 仅 Windows 有执行器；非 Windows 如实说明并**不注册工具**；
- 档位为「禁用」时不注册；「每次询问」由 Rust 弹系统确认框，用户拒绝即拒绝；
- 当前模型没有视觉能力时不注册：截屏进不了模型，盲操作没有意义（如实说明原因）；
- 坐标以最近一次 `screen_capture` 的截图像素为准，这里换算成真实屏幕坐标
  （截图会等比缩小到最长边 1600，且多显示器时虚拟桌面原点可能为负）；
- Win 键组合与任务管理器快捷键由 Rust 在解析阶段拒绝；
- 每个任务有截屏/操作次数上限，防止模型在循环里反复截屏或连点；
- 审计由 Rust 记录，不记输入正文与截图内容。

回环网关依赖不属于可 Mock 的外部服务：离线自测通过替换 `_gate_post` 模拟。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

MAX_CAPTURES = 20
MAX_ACTIONS = 60
MAX_TYPE_CHARS = 4000
MAX_GATE_BYTES = 8 * 1024 * 1024
# 每次询问档位会弹系统确认框，要留给用户足够时间；超时过早会导致「界面已超时、
# 动作随后才执行」的重复操作风险
GATE_TIMEOUT = 120

POLICIES = ("ask_every_time", "auto", "disabled")
POLICY_LABELS = {
    "ask_every_time": "每次询问（系统确认框）",
    "auto": "自动执行（不再逐次确认）",
    "disabled": "已禁用",
}


def _brief(text: str, limit: int = 200) -> str:
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


def _gate_post(
    url: str, token: str, payload: dict, timeout: int = GATE_TIMEOUT
) -> tuple[dict | None, str]:
    """POST 到本机网关。返回 (响应 JSON, 错误文案)；错误文案不含 token。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-tt-computer-token": token,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_GATE_BYTES + 1)
    except urllib.error.HTTPError as exc:
        raw = exc.read(MAX_GATE_BYTES + 1)
        message = f"HTTP {exc.code}"
        try:
            parsed = json.loads(raw.decode("utf-8", errors="replace"))
            if isinstance(parsed, dict) and parsed.get("error"):
                message = str(parsed["error"])
        except Exception:  # noqa: BLE001
            pass
        return None, message
    except Exception as exc:  # noqa: BLE001
        return None, f"本机电脑操控网关请求失败：{_brief(exc, 200)}"
    if len(raw) > MAX_GATE_BYTES:
        return None, f"网关响应超过 {MAX_GATE_BYTES} 字节，已拒绝读取"
    try:
        data = json.loads(raw.decode("utf-8", errors="replace"))
    except Exception as exc:  # noqa: BLE001
        return None, f"网关返回了无法解析的响应：{_brief(exc, 120)}"
    if not isinstance(data, dict):
        return None, "网关响应不是 JSON 对象"
    if data.get("ok") is not True:
        return None, str(data.get("error") or "网关拒绝了请求")
    return data, ""


@dataclass
class ComputerUse:
    task_id: str = ""
    supported: bool = False
    platform: str = ""
    policy: str = "disabled"
    features: dict = field(default_factory=dict)
    gate_url: str = ""
    gate_token: str = ""
    vision: bool = False
    reason: str = ""
    error: str = ""
    runtime_present: bool = False
    _last_shot: dict | None = None
    _captures: int = 0
    _actions: int = 0

    @classmethod
    def from_runtime(
        cls,
        runtime: dict | None,
        *,
        vision: bool,
        task_id: str = "",
    ) -> "ComputerUse":
        block = (runtime or {}).get("computerUse") if isinstance(runtime, dict) else None
        if not isinstance(block, dict):
            return cls(
                task_id=task_id,
                vision=vision,
                reason="未收到电脑操控能力信息（桌面壳可能过旧）",
            )
        platform = str(block.get("platform") or "").strip()
        supported = block.get("supported") is True
        policy = str(block.get("policy") or "disabled").strip()
        features = block.get("features") if isinstance(block.get("features"), dict) else {}
        error = str(block.get("error") or "").strip()
        gate_error = str(block.get("gateError") or "").strip()
        gate_url = str(block.get("gateUrl") or "").strip()
        gate_token = str(block.get("gateToken") or "").strip()

        reason = ""
        if error:
            reason = f"电脑操控能力读取失败：{_brief(error, 300)}"
        elif not supported:
            reason = f"电脑操控仅支持 Windows（当前平台：{platform or '未知'}）"
        elif policy not in POLICIES:
            reason = f"电脑操控档位非法「{policy}」，已按禁用处理"
        elif policy == "disabled":
            reason = "电脑操控已禁用"
        elif gate_error:
            reason = gate_error
        elif not _loopback_gate(gate_url) or not gate_token:
            reason = "本机电脑操控网关未启动或地址无效，执行器不可用"
        elif not vision:
            reason = (
                "当前模型没有视觉能力，截屏无法进入模型，本轮不提供电脑操控工具"
                "（可换用视觉模型，或用 TT_VISION=on 强制尝试）"
            )
        return cls(
            task_id=task_id,
            supported=supported,
            platform=platform,
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
            return [
                f"电脑操控已启用（{POLICY_LABELS.get(self.policy, self.policy)}）："
                "仅本机 Windows 桌面；每次截屏与键鼠操作都由桌面壳执行并写入审计，"
                "Win 键组合与任务管理器快捷键会被拒绝"
            ]
        # 禁用/非 Windows 是用户可预期的事实，不在每个任务里重复播报；
        # 平台支持却没起来（网关、档位损坏、没有视觉能力）才需要解释
        if self.runtime_present and (
            self.error or (self.supported and self.policy != "disabled")
        ):
            return [f"电脑操控未启用：{self.reason}"]
        return []

    def system_hint(self) -> str:
        if not self.ready:
            return ""
        return (
            "\n\n## 电脑操控（用户已启用）\n"
            "- screen_capture：截取整个虚拟桌面（所有显示器）并把画面附在本轮上下文里。"
            "截屏前按档位确认，截屏动作写入审计。\n"
            "- computer_action：点击 / 双击 / 右键 / 移动 / 滚动 / 输入文本 / 按键。"
            "坐标以**最近一次 screen_capture 的截图像素**为准，执行端会换算成真实屏幕坐标；"
            "坐标超出截图范围会被拒绝。\n"
            f"- 当前档位：{POLICY_LABELS.get(self.policy, self.policy)}。"
            "「每次询问」档位每次操作都会弹系统确认框，用户拒绝时动作不会执行。\n"
            "- 先截屏观察界面再操作；操作改变界面后重新截屏确认。"
            "不要发送 Win 键组合或任务管理器快捷键（会被拒绝）。"
        )

    # ── 网关 ────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict) -> tuple[dict | None, str]:
        if not self.ready:
            return None, self.reason or "电脑操控不可用"
        url = self.gate_url.rstrip("/") + path
        return _gate_post(url, self.gate_token, {**payload, "taskId": self.task_id})

    # ── 坐标换算 ────────────────────────────────────────────────

    def remember_shot(self, data: dict) -> dict:
        meta = {
            "width": max(1, int(data.get("width") or 0)),
            "height": max(1, int(data.get("height") or 0)),
            "originalWidth": max(1, int(data.get("originalWidth") or 0)),
            "originalHeight": max(1, int(data.get("originalHeight") or 0)),
            "originX": int(data.get("originX") or 0),
            "originY": int(data.get("originY") or 0),
        }
        self._last_shot = meta
        return meta

    def screen_point(self, x: int, y: int) -> tuple[tuple[int, int] | None, str]:
        """截图像素 → 系统屏幕坐标；坐标越界或还没截过屏时返回原因"""
        meta = self._last_shot
        if not meta:
            return None, "还没有截过屏：请先调用 screen_capture，再按截图像素给出坐标"
        w, h = meta["width"], meta["height"]
        if not (0 <= x < w and 0 <= y < h):
            return None, f"坐标 ({x}, {y}) 超出最近截图范围 {w}x{h}"
        sx = meta["originX"] + round(x * meta["originalWidth"] / w)
        sy = meta["originY"] + round(y * meta["originalHeight"] / h)
        sx = min(max(sx, meta["originX"]), meta["originX"] + meta["originalWidth"] - 1)
        sy = min(max(sy, meta["originY"]), meta["originY"] + meta["originalHeight"] - 1)
        return (sx, sy), ""

    # ── 工具注册 ────────────────────────────────────────────────

    def install(self, toolkit) -> None:
        """就绪才注册；未就绪时工具与提示词同时缺席，原因由 notes() 摊开"""
        if not self.ready:
            return
        toolkit.register_tool_function(self._capture_tool())
        toolkit.register_tool_function(self._action_tool())

    def _capture_tool(self):
        def screen_capture():
            """截取当前整个桌面（所有显示器）的画面，并把图片附在工具结果中。

            画面会直接进入模型上下文，用于判断窗口、按钮与坐标。
            坐标以这张截图的像素为准（截图已按比例缩小到最长边不超过 1600）。
            「每次询问」档位会先弹系统确认框；截屏动作写入审计日志。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            if self._captures >= MAX_CAPTURES:
                return ToolResponse(content=[TextBlock(
                    type="text",
                    text=f"错误：本次任务截屏次数已达上限 {MAX_CAPTURES}，请基于已有画面继续。",
                )])
            self._captures += 1
            data, err = self._post("/screenshot", {})
            if err:
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：截屏未完成。{err}"
                )])
            image = str(data.get("imageBase64") or "")
            mime = str(data.get("mime") or "image/png")
            if not image:
                return ToolResponse(content=[TextBlock(
                    type="text", text="错误：截屏响应里没有图片数据"
                )])
            meta = self.remember_shot(data)
            size_note = f"{meta['width']}x{meta['height']}"
            if meta["width"] != meta["originalWidth"] or meta["height"] != meta["originalHeight"]:
                size_note += f"（原始 {meta['originalWidth']}x{meta['originalHeight']}，已等比缩小）"
            text = (
                f"已截取桌面：{size_note}。"
                "坐标以这张截图的像素为准；点击/移动/滚动前请先看清目标位置。"
            )
            return ToolResponse(content=[
                TextBlock(type="text", text=text),
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": mime, "data": image},
                },
            ])

        return screen_capture

    def _action_tool(self):
        def computer_action(
            action: str,
            x: int = -1,
            y: int = -1,
            amount: int = 0,
            text: str = "",
            keys: str = "",
        ):
            """在桌面上执行一次鼠标或键盘操作。

            Args:
                action (`str`):
                    动作：click（左键单击）/ double_click（左键双击）/ right_click（右键单击）/
                    move（移动鼠标）/ scroll（滚轮）/ type（输入文本）/ key（按键组合）。
                x (`int`, optional):
                    目标横坐标，以最近一次 screen_capture 的截图像素为准。
                    点击、双击、右键、移动必填；滚动可选（给出时先移动过去）。
                y (`int`, optional):
                    目标纵坐标，规则同 x。
                amount (`int`, optional):
                    scroll 的滚动格数，正数向上/向前、负数向下/向后，范围 -20..20。
                text (`str`, optional):
                    type 要输入的文本（最多 4000 字符）。内容会写入当前焦点窗口，
                    也可能含敏感信息；不要输入密码等凭据，除非用户明确要求。
                keys (`str`, optional):
                    key 的按键组合，如 ctrl+a、enter、shift+tab；不允许 Win 键组合。
            """
            from agentscope.message import TextBlock
            from agentscope.tool import ToolResponse

            def fail(message: str):
                return ToolResponse(content=[TextBlock(
                    type="text", text=f"错误：操作未执行。{message}"
                )])

            act = str(action or "").strip().lower()
            payload: dict = {"action": act}
            if act in ("click", "double_click", "right_click", "move"):
                point, err = self.screen_point(int(x), int(y))
                if err:
                    return fail(err)
                payload["x"], payload["y"] = point
            elif act == "scroll":
                try:
                    delta = int(amount)
                except Exception:  # noqa: BLE001
                    return fail("amount 必须是整数")
                if delta == 0 or abs(delta) > 20:
                    return fail("滚动格数必须在 -20 到 20 之间且不为 0")
                payload["amount"] = delta
                if int(x) >= 0 and int(y) >= 0:
                    point, err = self.screen_point(int(x), int(y))
                    if err:
                        return fail(err)
                    payload["x"], payload["y"] = point
            elif act == "type":
                body = str(text or "")
                if not body:
                    return fail("type 动作缺少 text")
                if len(body) > MAX_TYPE_CHARS:
                    return fail(f"单次输入最多 {MAX_TYPE_CHARS} 个字符（收到 {len(body)}）")
                payload["text"] = body
            elif act == "key":
                combo = str(keys or "").strip()
                if not combo:
                    return fail("key 动作缺少 keys（如 ctrl+a）")
                payload["keys"] = combo
            elif not act:
                return fail("缺少 action")
            else:
                return fail(
                    f"不支持的动作「{act}」，可用 click / double_click / right_click / "
                    "move / scroll / type / key"
                )

            if self._actions >= MAX_ACTIONS:
                return fail(f"本次任务操控次数已达上限 {MAX_ACTIONS}，请停下并向用户说明进展。")
            self._actions += 1
            data, err = self._post("/action", payload)
            if err:
                return fail(err)
            done = str(data.get("action") or act)
            return ToolResponse(content=[TextBlock(
                type="text",
                text=f"已执行：{done}。界面可能已变化，下一步操作前请重新 screen_capture 确认。",
            )])

        return computer_action


# ── 工具结果截图 → 模型可消费的图片 ─────────────────────────────

def lift_tool_images(
    msgs: list, limit: int = 2
) -> tuple[list, list[dict], str]:
    """把工具结果里的 base64 截图提升为一条附图片的用户消息。

    AgentScope 的 OpenAI/DashScope formatter 会把工具结果里的 base64 图片落到
    临时文件，并把**本地路径**写进文本；远端模型既读不到路径，也拿不到图。
    这里只取末尾连续工具结果里的最近 `limit` 张，作为用户消息的 ImageBlock
    重新下发，其余截图替换成省略说明。返回 (改写后的消息列表, 图片列表, 说明)。
    """
    latest: list[dict] = []
    for msg in reversed(msgs):
        if getattr(msg, "role", "") != "system":
            break
        content = getattr(msg, "content", None)
        blocks = content if isinstance(content, list) else []
        results = [
            b for b in blocks
            if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        if not results:
            break
        batch: list[dict] = []
        for block in results:
            output = block.get("output")
            if not isinstance(output, list):
                continue
            for item in output:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "image"
                    and isinstance(item.get("source"), dict)
                    and item["source"].get("type") == "base64"
                ):
                    batch.append(item)
        latest = batch + latest
    if not latest:
        return list(msgs), [], ""
    latest = latest[-limit:]
    keep = {id(img) for img in latest}

    from agentscope.message import Msg

    cleaned: list = []
    for msg in msgs:
        content = getattr(msg, "content", None)
        if not isinstance(content, list):
            cleaned.append(msg)
            continue
        changed = False
        new_blocks: list = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                new_blocks.append(block)
                continue
            output = block.get("output")
            if not isinstance(output, list):
                new_blocks.append(block)
                continue
            new_output: list = []
            for item in output:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "image"
                    and isinstance(item.get("source"), dict)
                    and item["source"].get("type") == "base64"
                ):
                    changed = True
                    new_output.append({
                        "type": "text",
                        "text": "（截图已作为图片附加在本轮用户消息中）"
                        if id(item) in keep
                        else "（较早的截图已省略，避免占用上下文）",
                    })
                else:
                    new_output.append(item)
            replaced = dict(block)
            replaced["output"] = new_output
            new_blocks.append(replaced)
        cleaned.append(
            Msg(name=msg.name, content=new_blocks, role=msg.role)
            if changed else msg
        )
    return cleaned, latest, "latest"


class ToolImageFormatter:
    """formatter 包装：先把电脑操控截图提升为用户消息图片，再交给原 formatter。"""

    def __init__(self, base, max_images: int = 2, lead: str | None = None):
        self.base = base
        self.max_images = max_images
        self.lead = lead or (
            "以下是电脑操控刚刚截取的屏幕画面（按时间从旧到新；"
            "坐标以最后一张截图的像素为准）："
        )

    async def format(self, msgs, *args, **kwargs):
        cleaned, images, _ = lift_tool_images(list(msgs), self.max_images)
        if images:
            from agentscope.message import Msg

            content = [{"type": "text", "text": self.lead}, *images]
            cleaned = [*cleaned, Msg(name="user", content=content, role="user")]
        return await self.base.format(cleaned, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.base, name)
