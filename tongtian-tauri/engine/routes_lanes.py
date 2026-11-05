"""#164 引擎路由分片的唯一声明处（W4 领地）。

engine/server.py 有 8955 行、61 个路由装饰器，是所有引擎窗口的公共咽喉：
七个窗口同时打开它，后写的会把先写的静默覆盖（本仓库不是 git 仓库）。
所以新的引擎端点一律写进 engine/routes_<domain>.py，由本文件集中声明顺序、
挂载与互相遮蔽检查；server.py 里只在末尾留一次调用点，之后不再被打开。

三条口径：
- 车道里的路由只在 ``attach()`` 之后才挂上 app，所以**既有路由永远优先**：
  同名路径由 server.py 里的原件赢。这不是兜底，是刻意定的规则，
  因为 D-07.4 真机演练踩过「通配路由吞掉字面路径」——那条 mock 永远测不出来。
- 每个车道声明自己的路径前缀，越界即拒绝挂载（防止 A 车道占掉 B 车道的名字）。
- 车道不许 ``import server``：server.py 既可能被当作 ``__main__`` 直接跑，
  也可能被自测当 ``server`` 模块导入，两种身份同时存在就会有两份会话表。
  引擎主模块由 :func:`engine` 提供，注册前取到会如实报错。
"""

from __future__ import annotations

from typing import Any, Iterable

# 车道名 → 负责窗口与卡号（交棒清单同表，改这里要同步改 docs/并行实施/交棒清单.md）
LANE_OWNERS = {
    "knowledge": "W2（#137 D-09 混合检索 / #143 D-38 技能注册表 / #138 D-26 产物版本历史）",
    "artifact": "W4（#144 产物与发布 / #159 更新清单校验与失败回滚）",
    "workspace": "W3（#157 D-29 工作区后端 / #140 D-13 快照接力 / #151 D-32 设计令牌）",
    "exec": "W4（#165 D-14.5 / #150 D-48 prepare_fn 与按剩余窗口取页 / 沙箱）",
    "connect": "W5（#147 D-39 MCP / #146 D-41 IM / #139 D-30 定时 / #145 D-36 浏览器）",
    "orchestrate": "W6（#148 D-34 团队协作 / #149 D-40.5 成员检查点 / #156 D-10 工作流）",
    "voice": "W7（#160 D-11 语音链路；分工文档按六车道拆，语音是第七条，"
             "目的是让 W7 不必挤进别人的文件）",
}

# 每个车道允许占用的路径前缀。新增前缀 = 提分片给 W4，别自己扩。
LANE_PREFIXES = {
    "knowledge": ("/api/knowledge", "/api/skills", "/api/artifacts"),
    "artifact": ("/api/files", "/api/artifact", "/api/market", "/api/update"),
    "workspace": ("/api/workspace", "/api/snapshot", "/api/storage"),
    "exec": ("/api/exec", "/api/approval", "/api/staging", "/api/persona"),
    "connect": ("/api/mcp", "/api/im", "/api/schedule", "/api/browser", "/api/channel"),
    "orchestrate": ("/api/subagents", "/api/experts", "/api/workflow", "/api/checkpoint"),
    "voice": ("/api/audio", "/api/voice", "/api/asr", "/api/tts"),
}

LANE_MODULE_NAMES = (
    "routes_knowledge",
    "routes_artifact",
    "routes_workspace",
    "routes_exec",
    "routes_connect",
    "routes_orchestrate",
    "routes_voice",
)


class LaneError(RuntimeError):
    """分片声明不合法（越界前缀、路径遮蔽、重复挂载）——引擎宁可不起也不带着半套路由跑。"""


_ENGINE: Any = None


def bind(engine_module: Any) -> None:
    global _ENGINE
    _ENGINE = engine_module


def engine() -> Any:
    """车道里取引擎主模块（会话表、鉴权、检查点等既有能力都在这里）。"""
    if _ENGINE is None:
        raise LaneError("车道在 server.py 注册面运行前就被调用：engine() 只有装配完成后才可用")
    return _ENGINE


def _methods_of(route) -> set[str]:
    raw = getattr(route, "methods", None) or set()
    return {m.upper() for m in raw if m.upper() != "HEAD"}


def _is_literal(path: str) -> bool:
    return "{" not in path


def _prefix_ok(path: str, prefix: str) -> bool:
    """前缀按段对齐，不按字符对齐。

    `/api/artifact` 与 `/api/artifacts` 是两条车道的名字（产物面 vs 版本历史面）；
    纯 startswith 会让 artifact 车道把 knowledge 车道的 `/api/artifacts/history` 认成本家，
    越界检查当场失效。
    """
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def check_lane(module) -> None:
    """车道声明自检：名字、归属、前缀都得对上；对不上就是有人把端点写错了地方。"""
    lane = getattr(module, "LANE", None)
    if lane not in LANE_PREFIXES:
        raise LaneError(f"{module.__name__}: LANE={lane!r} 不在登记的车道里（{', '.join(LANE_PREFIXES)}）")
    if not hasattr(module, "router"):
        raise LaneError(f"{module.__name__}: 缺 router = APIRouter()")
    for route in module.router.routes:
        if not any(_prefix_ok(route.path, p) for p in LANE_PREFIXES[lane]):
            raise LaneError(
                f"{lane} 车道里的 {route.path} 越界：本车道只登记了 "
                f"{', '.join(LANE_PREFIXES[lane])}；换车道或提分片给 W4"
            )


def _shadow_of(app, route) -> str:
    """已有路由是否会吞掉这条字面路径（含通配段匹配，D-07.4 那类缺陷的形态）。"""
    if not _is_literal(route.path):
        return ""
    for existing in app.routes:
        pattern = getattr(existing, "path_regex", None)
        if pattern is None:
            continue
        existing_methods = _methods_of(existing)
        route_methods = _methods_of(route)
        if existing_methods and route_methods and not (existing_methods & route_methods):
            continue
        if pattern.match(route.path):
            if getattr(existing, "path", "") == route.path:
                return f"与已有路由完全重名：{existing.path}"
            return f"被已有路由 {existing.path} 吞掉（字面路径永远轮不到）"
    return ""


def attach(app, engine_module, lane_names: Iterable[str] = LANE_MODULE_NAMES) -> list[dict]:
    """按声明顺序把各车道挂上 app，返回挂载报告（车道、端点数、路径）。

    server.py 只在末尾调一次；任何遮蔽/越界都会在这里抛，不会带病启动。
    """
    import importlib

    bind(engine_module)
    report: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for module_name in lane_names:
        module = importlib.import_module(module_name)
        check_lane(module)
        for route in module.router.routes:
            for method in _methods_of(route) or {"*"}:
                key = (method, route.path)
                if key in seen:
                    raise LaneError(f"{getattr(module, 'LANE', module_name)} 车道内部重复挂载：{key}")
                seen.add(key)
            shadow = _shadow_of(app, route)
            if shadow:
                raise LaneError(
                    f"{getattr(module, 'LANE', module_name)} 车道的 {route.path} 挂不上：{shadow}"
                )
        app.include_router(module.router)
        report.append({
            "lane": getattr(module, "LANE", module_name),
            "owner": LANE_OWNERS.get(getattr(module, "LANE", ""), "未登记"),
            "endpoints": len(module.router.routes),
            "paths": sorted({r.path for r in module.router.routes}),
        })
    return report
