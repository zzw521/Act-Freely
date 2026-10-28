"""平台层配置与开关（单用户本地运行，不读数据库）

配置根：`TT_PLATFORM_HOME`，缺省为 `{TT_DATA_HOME|~/.tongtianwork}/agent-platform`。
目录约定：

    agent-platform/
      platform.yaml        # enabled / defaultSkill / schedules
      skills/*.yaml|json   # 技能包，一文件一个技能或 {"skills": [...]}
      mcp_catalog.yaml     # MCP 注册表，{"servers": [...]}

内置技能与目录在 `agent_platform/builtin/`，用户目录同名技能按 id 覆盖内置项。
开关：`TT_PLATFORM=1` 或 platform.yaml 的 `enabled: true`。默认关闭，原有启动路径不受影响。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRUTHY = {"1", "true", "yes", "on"}

DEFAULT_SKILL = "general"


class ConfigError(Exception):
    """`CODE: 原因`，与引擎其他接口同一口径"""

    def __init__(self, code: str, detail: str = ""):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def data_home() -> Path:
    """平台数据根；`TT_PLATFORM_HOME` 优先，其次跟随引擎的 `TT_DATA_HOME`。"""
    explicit = str(os.environ.get("TT_PLATFORM_HOME") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    base = str(os.environ.get("TT_DATA_HOME") or "").strip()
    root = Path(base).expanduser() if base else (Path.home() / ".tongtianwork")
    return root / "agent-platform"


def bundled_dir() -> Path:
    return Path(__file__).resolve().parent / "builtin"


def load_structured(path: Path) -> Any:
    """读取 yaml/yml/json。PyYAML 缺失时只拒绝 YAML，不假装读到了空配置。"""
    suffix = path.suffix.lower()
    text = path.read_text(encoding="utf-8")
    if suffix == ".json":
        return json.loads(text)
    if suffix in (".yaml", ".yml"):
        try:
            import yaml
        except Exception as exc:  # noqa: BLE001
            raise ConfigError(
                "YAML_UNAVAILABLE", f"读取 {path.name} 需要 PyYAML：{exc}"
            ) from None
        return yaml.safe_load(text)
    raise ConfigError("CONFIG_FORMAT", f"不支持的配置文件格式：{path.name}")


@dataclass
class PlatformConfig:
    enabled: bool = False
    home: Path = field(default_factory=data_home)
    default_skill: str = DEFAULT_SKILL
    max_parallel: int = 2
    schedules: list[dict] = field(default_factory=list)
    source: str = "默认（未启用）"
    errors: list[str] = field(default_factory=list)

    @property
    def skills_dir(self) -> Path:
        return self.home / "skills"

    @property
    def mcp_catalog_path(self) -> Path:
        return self.home / "mcp_catalog.yaml"

    @property
    def workspace_dir(self) -> Path:
        return self.home / "workspace"

    def view(self) -> dict:
        return {
            "enabled": self.enabled,
            "home": str(self.home),
            "skillsDir": str(self.skills_dir),
            "mcpCatalog": str(self.mcp_catalog_path),
            "defaultSkill": self.default_skill,
            "maxParallel": self.max_parallel,
            "schedules": len(self.schedules),
            "source": self.source,
            "errors": list(self.errors),
        }


def _env_truthy(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None or not str(raw).strip():
        return None
    return str(raw).strip().lower() in TRUTHY


def load_config() -> PlatformConfig:
    """读取平台配置；结构错误只记 errors，不让检查类命令整条不可用。"""
    home = data_home()
    cfg = PlatformConfig(home=home)
    path = home / "platform.yaml"
    if not path.exists():
        path = home / "platform.json"
    env_enabled = _env_truthy("TT_PLATFORM")
    if path.exists():
        cfg.source = str(path)
        try:
            raw = load_structured(path)
        except Exception as exc:  # noqa: BLE001
            cfg.errors.append(f"平台配置读取失败，按未启用处理：{exc}")
            raw = {}
        if raw is not None and not isinstance(raw, dict):
            cfg.errors.append("平台配置格式非法（根节点必须是对象），按未启用处理")
            raw = {}
        raw = raw or {}
        if "enabled" in raw:
            cfg.enabled = raw.get("enabled") is True or str(raw.get("enabled")).strip().lower() in TRUTHY
        default_skill = str(raw.get("defaultSkill") or "").strip()
        if default_skill:
            cfg.default_skill = default_skill
        try:
            cfg.max_parallel = max(1, min(8, int(raw.get("maxParallel") or 2)))
        except (TypeError, ValueError):
            cfg.errors.append("maxParallel 不是整数，按 2 执行")
        schedules = raw.get("schedules")
        if isinstance(schedules, list):
            cfg.schedules = [s for s in schedules if isinstance(s, dict)]
        elif schedules is not None:
            cfg.errors.append("schedules 必须是数组，已忽略")
    if env_enabled is not None:
        cfg.enabled = env_enabled
        cfg.source = f"环境变量 TT_PLATFORM={os.environ.get('TT_PLATFORM')}"
    return cfg
