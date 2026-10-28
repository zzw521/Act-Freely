"""通天 Agent 平台层（Octop 四个设计思想的增量落地）

- message:   统一消息结构体与总线（入口收敛、通道隔离）
- skills:    YAML 技能包（prompt / 工具白名单 / 上下文策略 / 热加载）
- mcp_catalog: MCP Catalog 注册表 + 客户端池 + 技能级工具白名单
- adapters:  ChannelAdapter 基类与 CLI / IM / 定时适配器
- dispatcher: 调度层，复用 engine/server.py 的存量 Agent 主循环

默认关闭：TT_PLATFORM=1 或 platform.yaml `enabled: true`。原有启动方式不受影响。
"""

from .config import PlatformConfig, load_config
from .message import AgentMessage, AgentReply, MessageBus
from .skills import Skill, SkillRegistry, parse_skill
from .mcp_catalog import McpCatalog, McpCatalogError, McpClientPool, parse_server
from .dispatcher import AgentDispatcher
from .adapters import ChannelAdapter, ChannelGateway, CliAdapter, ImAdapter, ScheduleAdapter

__all__ = [
    "PlatformConfig",
    "load_config",
    "AgentMessage",
    "AgentReply",
    "MessageBus",
    "Skill",
    "SkillRegistry",
    "parse_skill",
    "McpCatalog",
    "McpCatalogError",
    "McpClientPool",
    "parse_server",
    "AgentDispatcher",
    "ChannelAdapter",
    "ChannelGateway",
    "CliAdapter",
    "ImAdapter",
    "ScheduleAdapter",
]
