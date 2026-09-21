#!/usr/bin/env python
"""生成 engine/requirements.lock.txt —— 随包 Python 运行时的依赖锁定表。

为什么需要锁文件：engine/requirements.txt 只声明 6 个直接依赖，pip 会把传递依赖
解析到最新版。实测 mcp 2.x 移除了 streamablehttp_client，直接导致 agentscope 1.0.20
导入失败——即"开发机能跑、随包运行时报 ImportError"。因此随包运行时必须安装
传递闭包冻结后的整表。

用法（必须在"实测可用"的解释器里执行；只允许整表重生成，不要手改单行）：
    python scripts/gen-requirements-lock.py            # 重新生成锁文件
    python scripts/gen-requirements-lock.py --check    # 校验锁文件与当前环境是否一致（CI 用，不一致退出 1）

注意：锁文件与解释器环境强相关（本工程按 CPython 3.12 / win_amd64 冻结），
      换 Python 小版本或换平台后必须重新生成。
"""
from __future__ import annotations

import argparse
import sys
from importlib.metadata import PackageNotFoundError, distribution, requires, version
from pathlib import Path

from packaging.requirements import Requirement

ROOT = Path(__file__).resolve().parent.parent
REQ_FILE = ROOT / "engine" / "requirements.txt"
LOCK_FILE = ROOT / "engine" / "requirements.lock.txt"

# 引导工具：随包运行时不需要，pip 由 prepare-python.sh 单独引导并在打包前剔除
TOOLING = {"pip", "setuptools", "wheel"}

HEADER = """\
# 引擎随包依赖锁定清单（Python 3.12 win_amd64 embeddable 运行时专用）
# 来源：开发机实测可用环境（engine/server.py 在该环境正常启动）按 engine/requirements.txt
#       的 {direct_count} 个直接依赖做传递闭包后冻结，含 Windows-only 条件依赖
# 用法：scripts/prepare-python.sh 以 --target 装入 vendor/python/Lib/site-packages
# 维护：只允许用 scripts/gen-requirements-lock.py 整表重生成，不要手改单行；
#       mcp 2.x 改名了 streamablehttp_client，与 agentscope 1.0.20 不兼容即为浮版本实例
"""


def read_direct_requirements() -> list[str]:
    """读取直接依赖名（忽略注释与空行）"""
    names: list[str] = []
    for raw in REQ_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.append(Requirement(line).name)
    if not names:
        sys.exit(f"错误：{REQ_FILE} 中未解析到任何依赖")
    return names


def base_dependencies(name: str) -> list[str]:
    """某个已安装分发包的"不带 extras"依赖名列表。

    闭包规则（刻意保守，与锁文件头"含 Windows-only 条件依赖"一致）：

    - 跳过 extra 条件依赖：不请求 extras 时它们不会被安装，纳入闭包只会锁进无用包。
    - 不评估 python_version / sys_platform 等标记：这些包在实测可用环境里是"已安装且
      引擎能正常启动"的组成部分（如 exceptiongroup 对 anyio 虽只声明 python<3.11，
      装上也仅为几十 KB），宁可多锁也不冒"运行时缺件"的风险——浮版本导致的
      mcp 改名事故才是真正要防的问题。
    """
    out: list[str] = []
    for raw in requires(name) or []:
        req = Requirement(raw)
        if req.marker is not None and "extra" in str(req.marker):
            continue
        out.append(req.name)
    return out


def transitive_closure(roots: list[str]) -> dict[str, str]:
    """传递闭包 → {分发包名: 已安装版本}，包名与版本都取自环境本身。

    只沿"目标已安装"的边继续展开：锁文件的职责是冻结一个实测可用的环境，
    因此"实际装了什么"就是事实来源。引导工具（pip/setuptools/wheel）不算依赖——
    随包运行时不需要它们，pip 由 prepare-python.sh 单独引导并在打包前剔除。
    """
    def installed(name: str) -> tuple[str, str] | None:
        try:
            dist = distribution(name)
        except PackageNotFoundError:
            return None
        # 用安装元数据里的 Name，保留 PyJWT / typing_extensions / python-multipart 的原始写法
        return (dist.metadata["Name"] or name, version(name))

    seen: dict[str, str] = {}
    seen_keys: set[str] = set()
    for root in roots:
        found = installed(root)
        if found is None:
            sys.exit(
                f"错误：当前解释器缺少直接依赖 {root!r}。请先 pip install -r engine/requirements.txt，"
                "或在实测可用的环境里执行本脚本。"
            )

    queue = list(roots)
    while queue:
        name = queue.pop()
        found = installed(name)
        if found is None or name.lower() in TOOLING:
            continue
        real_name, ver = found
        if real_name.lower() in seen_keys:
            continue
        seen_keys.add(real_name.lower())
        seen[real_name] = ver  # 登记原始大小写，去重键另用小写
        queue.extend(base_dependencies(real_name))
    return seen


def render() -> str:
    direct = read_direct_requirements()
    pins = transitive_closure(direct)
    # 大小写不敏感排序：PyJWT / SQLAlchemy 这类大写名与 pyjwt / sqlalchemy 排在一起
    lines = [f"{name}=={ver}" for name, ver in sorted(pins.items(), key=lambda kv: kv[0].lower())]
    return HEADER.format(direct_count=len(direct)) + "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="生成/校验引擎依赖锁定表")
    parser.add_argument("--check", action="store_true", help="只校验，不写文件；不一致时退出 1")
    args = parser.parse_args()

    content = render()
    pin_count = content.count("==")

    if args.check:
        current = LOCK_FILE.read_text(encoding="utf-8") if LOCK_FILE.exists() else ""
        if current == content:
            print(f"✓ {LOCK_FILE.name} 与当前环境一致（{pin_count} 个包）")
            return 0
        print(
            f"✗ {LOCK_FILE.name} 与当前环境不一致：锁文件会误导随包安装，"
            "请执行 python scripts/gen-requirements-lock.py 重新生成",
            file=sys.stderr,
        )
        return 1

    LOCK_FILE.write_text(content, encoding="utf-8", newline="\n")
    print(f"✓ 已写入 {LOCK_FILE}（{pin_count} 个包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())