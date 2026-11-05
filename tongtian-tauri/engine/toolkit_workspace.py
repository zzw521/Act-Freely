"""workspace 工具车道（W3）：把工作区后端（#157）接到模型的工具面。

开关：``workspace_backend``。**目前没接线**（``ToolkitContext.workspace_backend`` 缺省 None
⇒ 本车道一件都不注册，与今日行为一致）。点亮方式写在 `docs/并行实施/157.md` §3，一句话：
W4 在 `build_toolkit` 的 ``extra`` 里补 ``workspace_backend`` 键，值是**本轮的落点选择现场**，
本车道认三种形态（只认这三种，认不到就整条车道不注册并留话）：

* ``WorkspaceBackend`` 实例 —— 装配现场已经按本轮 runtime 解析好了，直接用它；
* ``dict`` —— 本轮的 runtime bundle（含 ``workspaceBackend`` 段），车道按它现取后端；
* 有 ``current()`` 的对象（清单/注册表）—— 按活动后端取。

三种形态都要配上 ``ctx.sandbox``（``SandboxPolicy``）才判定：车道**不从工具参数里收档位**，
档位只来自本轮策略。理由与 D-45 一致 —— 能自己声明「这次按完全访问判」的工具参数，
等于把用户在下拉框里选的档位变成模型可以改写东西的地方。

工具名（都在开关后面，一个都不进常驻清单；契约见
`tests/unit/extensions/builtin-tools-contract.test.ts`：常驻清单里每个 id 都要有 Rust 实现）：
``workspace_backend_status`` / ``_switch`` / ``_list`` / ``_read`` / ``_write`` / ``_delete`` / ``_exec``。
其中 ``_switch`` / ``_write`` / ``_delete`` / ``_exec`` 是副作用件，要进
`engine/side_effect_registry.py` 与 TS 那份 ``SIDE_EFFECT_TOOLS``（两份都归 W4，本卡只提分片请求）——
否则检查点重放会把「已经在远端落过的写」再落一遍。

判定与审计都不在这一层重做：三段判定（后端边界 / 沙箱 / 档位）在后端运行时里，
审计由 ``WorkspaceBackend._op`` 落 JSONL（只落字节数与指纹）。车道只做「把结论说给模型听」，
并且**保留来源**：档位拒绝与沙箱拒绝是两句话，模型据此告诉用户去哪儿改，而不是笼统回「不行」。
"""

from __future__ import annotations

from typing import Any

import toolkit_lanes as tl
import workspace_backend_runtime as wb

LANE = "workspace"
SWITCHES = tl.SWITCH_BOARD["workspace"]

#: 车道注册的工具名（只有这一处声明，测试与分片都从这里取）。
WORKSPACE_TOOLS = (
    "workspace_backend_status",
    "workspace_backend_switch",
    "workspace_backend_list",
    "workspace_backend_read",
    "workspace_backend_write",
    "workspace_backend_delete",
    "workspace_backend_exec",
)

#: 回给模型的文本预算：工具输出要进上下文，后端列表一条能带回整个桶的键。
_MAX_TEXT_CHARS = 6000


def resolve_backend(handle: Any, ctx: tl.ToolkitContext):
    """按三种认得的形态取本轮后端；认不到回 None（调用方如实报错，不猜本地目录）。"""
    if isinstance(handle, wb.WorkspaceBackend):
        backend = handle
    elif isinstance(handle, dict):
        backend = wb.active_backend(ctx.sandbox, runtime=handle)
    else:
        current = getattr(handle, "current", None)
        backend = current() if callable(current) else None
    if backend is None:
        return None
    backend.bind_policy(ctx.sandbox)
    return backend


def _text(payload: Any) -> str:
    import json

    try:
        text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    except Exception:  # noqa: BLE001  回话格式不该决定工具成败
        text = str(payload)
    if len(text) > _MAX_TEXT_CHARS:
        return text[:_MAX_TEXT_CHARS] + f"\n…（回话超 {len(text) - _MAX_TEXT_CHARS} 字符已截断；要完整清单请缩小 path 或用分页参数）"
    return text


def _unavailable(ctx: tl.ToolkitContext) -> dict[str, Any]:
    return {
        "ok": False,
        "code": wb.CODE_BACKEND_REQUIRED,
        "message": (
            "本轮没有解析出工作区后端（句柄给的是空清单，或句柄里没有能认得的形态）。"
            f"清单里已装配的是：{'、'.join(wb.REGISTRY.ids()) or '（空）'}；"
            "本车道不会退回「按本机工作目录假装成功」——那会把远程落点上的事说成做过了"
        ),
        "activeBackendId": wb.REGISTRY.active_id(),
    }


def register(ctx: tl.ToolkitContext) -> None:
    handle = ctx.workspace_backend
    if handle is None:
        return

    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    def respond(payload: Any) -> ToolResponse:
        return ToolResponse(content=[TextBlock(type="text", text=_text(payload))])

    def current_backend():
        return resolve_backend(handle, ctx)

    def workspace_backend_status() -> ToolResponse:
        """看当前工作区落在哪个后端上、这个后端能做什么、还没验收的是哪几件事。

        返回后端清单（id / 类型 / 根 / 只读 / 能力位）、每个后端的探测结论、被拒条目与未验收
        条目。这里只描述落点：模型配置、工具装配、文件访问档位都不在其中，切换后端也不改它们。
        """
        snapshot = wb.list_backends()
        status = wb.runtime_status()
        return respond(
            {
                "ok": True,
                "activeBackendId": snapshot.get("activeBackendId"),
                "backends": snapshot.get("backends"),
                "rejected": snapshot.get("rejected"),
                "unverified": status.get("unverified"),
                "credentialPolicy": status.get("credentialPolicy"),
                "note": snapshot.get("note"),
            }
        )

    def workspace_backend_switch(backend_id: str) -> ToolResponse:
        """把工作区落点切到清单里的另一个后端（不搬任何数据）。

        只换「这次操作落在哪个后端」；文件搬运、格式转换、历史迁移都不在这条工具里，
        需要的话要显式读出来再写过去。切完会回「改了什么 / 没改什么」。
        切到配置没通过校验的后端会被拒并给出原因——那类后端压根没装进清单。
        """
        return respond(wb.REGISTRY.switch(backend_id))

    def workspace_backend_list(path: str = "") -> ToolResponse:
        """列当前工作区后端里某个目录下的对象（路径一律相对后端根，不写本机盘符也不写 ..）。"""
        backend = current_backend()
        if backend is None:
            return respond(_unavailable(ctx))
        return respond(backend.list_dir(path, ctx=_op_context(ctx, "list")))

    def workspace_backend_read(file_path: str) -> ToolResponse:
        """读当前工作区后端里的一个文本对象（相对后端根的路径）。

        被拒时回话里带 ``deniedBy``：后端设置、沙箱白名单、文件访问档位是三件不同的事，
        三句不同的话，别把「档位不够」说成「路径越界」。
        """
        backend = current_backend()
        if backend is None:
            return respond(_unavailable(ctx))
        return respond(backend.read(file_path, ctx=_op_context(ctx, "read")))

    def workspace_backend_write(file_path: str, content: str) -> ToolResponse:
        """在当前工作区后端里写一个文本对象（整篇覆盖，不是追加；相对后端根的路径）。

        远程后端的写要求「完全访问」档位，只读后端在任何档位都写不了；被拒时按 ``deniedBy``
        回三句不同的话。想追加用 ``workspace_backend_read`` 读出后合并再写。
        """
        backend = current_backend()
        if backend is None:
            return respond(_unavailable(ctx))
        return respond(backend.write(file_path, content, ctx=_op_context(ctx, "write")))

    def workspace_backend_delete(file_path: str) -> ToolResponse:
        """删除当前工作区后端里的一个对象（相对后端根的路径，不接受根路径本身）。

        删除比写多两道要求：文件访问档位要到「完全访问」，且后端显式开了
        ``options.allowRemoteDelete``。两道少哪一道都会被拒，而且理由不是同一句。
        """
        backend = current_backend()
        if backend is None:
            return respond(_unavailable(ctx))
        return respond(backend.delete(file_path, ctx=_op_context(ctx, "delete")))

    def workspace_backend_exec(command: str) -> ToolResponse:
        """在当前工作区后端上起一条命令（只有 docker / ssh 两类后端承载得了）。

        docker 那一档的重数（内存/CPU/进程数/网络）按后端配置进 ``docker run`` 命令行；
        本地目录、数据库、对象存储后端会直接回「不承载命令执行」，不会空跑装作成功。
        """
        backend = current_backend()
        if backend is None:
            return respond(_unavailable(ctx))
        return respond(backend.run(command, ctx=_op_context(ctx, "exec")))

    def _op_context(tool_ctx: tl.ToolkitContext, op: str) -> wb.OpContext:
        return wb.OpContext(task_id=tool_ctx.task_id, tool=f"toolkit:workspace_backend_{op}")

    # 显式点名，不用 locals()：WORKSPACE_TOOLS 与函数名一旦漂移要当场炸在这里，
    # 而不是「少注册一件、模型看不见、界面以为是后端坏了」。
    tools = {
        "workspace_backend_status": workspace_backend_status,
        "workspace_backend_switch": workspace_backend_switch,
        "workspace_backend_list": workspace_backend_list,
        "workspace_backend_read": workspace_backend_read,
        "workspace_backend_write": workspace_backend_write,
        "workspace_backend_delete": workspace_backend_delete,
        "workspace_backend_exec": workspace_backend_exec,
    }
    missing = sorted(set(WORKSPACE_TOOLS) - set(tools))
    if missing:
        raise tl.ToolkitLaneError(f"workspace 车道声明了工具却没实现：{'、'.join(missing)}")
    for name in WORKSPACE_TOOLS:
        ctx.toolkit.register_tool_function(tools[name])
