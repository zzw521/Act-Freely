#!/usr/bin/env python3
"""
Contract Drift Detection Script
用于检测模块间 API 契约版本漂移。

Usage:
    python drift-detection.py --module payment-service --format json
    python drift-detection.py --all --format text
    python drift-detection.py --topology --format mermaid
"""

import argparse
import glob
import json
import os
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import yaml
except ImportError:
    print("❌ 请先安装 PyYAML: pip install PyYAML")
    sys.exit(1)


# =============================================================================
# 常量
# =============================================================================

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# scripts/ → contract-governance/ → apex-sdd/ → skills/ → .qoder/ → <project-root>
DEFAULT_PROJECT_ROOT = os.path.abspath(os.path.join(SCRIPT_DIR, '..', '..', '..', '..', '..'))

EXIT_OK = 0
EXIT_BREAKING_DRIFT = 1
EXIT_FILE_ERROR = 2


# =============================================================================
# 辅助函数：semver 解析与比较
# =============================================================================

def parse_semver(version_str: str) -> Tuple[int, int, int]:
    """解析 semver 字符串为 (major, minor, patch) 元组。

    支持带 'v' 前缀的版本号（如 v1.2.3 → 1.2.3）。
    解析失败时返回 (0, 0, 0)。
    """
    version_str = str(version_str).strip().lstrip('v')
    parts = version_str.split('.')
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
        patch = int(parts[2]) if len(parts) > 2 else 0
        return (major, minor, patch)
    except (ValueError, IndexError):
        return (0, 0, 0)


def compare_versions(pinned: str, actual: str) -> str:
    """比较 pinned_version 与 actual_version，返回 drift 状态。

    规则:
      - major 不同 → breaking_drift
      - minor 不同（actual > pinned）→ minor_drift
      - 完全相同 → aligned
    """
    p = parse_semver(pinned)
    a = parse_semver(actual)

    if p == a:
        return 'aligned'

    if p[0] != a[0]:
        return 'breaking_drift'

    # major 相同，检查 minor
    if a[1] > p[1] or (a[1] == p[1] and a[2] > p[2]):
        return 'minor_drift'

    # actual < pinned 的情况也视为 minor_drift（回退场景）
    return 'minor_drift'


# =============================================================================
# deps.yaml 扫描与解析
# =============================================================================

def find_all_deps_files(project_root: str) -> List[str]:
    """扫描项目中所有 deps.yaml 文件路径。

    搜索范围: server/*/deps.yaml, web/*/deps.yaml
    """
    deps_files = []
    for pattern in ['server/*/deps.yaml', 'web/*/deps.yaml']:
        deps_files.extend(glob.glob(os.path.join(project_root, pattern)))
    return sorted(deps_files)


def load_deps_yaml(filepath: str) -> Optional[Dict[str, Any]]:
    """读取并解析 deps.yaml 文件。

    文件不存在或解析失败返回 None。
    """
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except (yaml.YAMLError, IOError) as e:
        print(f"⚠️ 解析 deps.yaml 失败: {filepath} — {e}", file=sys.stderr)
        return None


def load_api_contract_version(contract_file: str, project_root: str) -> Optional[str]:
    """读取 API 契约 YAML 的 info.version 字段。

    Args:
        contract_file: 相对于项目根目录的路径
        project_root: 项目根目录绝对路径

    Returns:
        版本号字符串，读取失败返回 None
    """
    full_path = os.path.join(project_root, contract_file)
    if not os.path.exists(full_path):
        return None
    try:
        with open(full_path, 'r', encoding='utf-8') as f:
            spec = yaml.safe_load(f)
        if not isinstance(spec, dict):
            return None
        return spec.get('info', {}).get('version')
    except (yaml.YAMLError, IOError) as e:
        print(f"⚠️ 读取 API 契约失败: {full_path} — {e}", file=sys.stderr)
        return None


# =============================================================================
# Drift 检测核心逻辑
# =============================================================================

def check_module_drift(
    module_name: str,
    project_root: str,
) -> Dict[str, Any]:
    """对指定模块执行 drift 检测。

    Returns:
        {
            "module": str,
            "results": [{"provider": str, "pinned_version": str, "actual_version": str, "status": str}],
            "overall_status": "aligned" | "minor_drift" | "breaking_drift"
        }
    """
    deps_files = find_all_deps_files(project_root)
    target_deps = None

    for df in deps_files:
        data = load_deps_yaml(df)
        if data and data.get('metadata', {}).get('module_name') == module_name:
            target_deps = (df, data)
            break

    if target_deps is None:
        return {
            'module': module_name,
            'results': [],
            'overall_status': 'aligned',
            'error': f'模块 {module_name} 未找到 deps.yaml',
        }

    _, deps_data = target_deps
    return _analyze_drift(deps_data, project_root)


def check_all_drift(project_root: str) -> List[Dict[str, Any]]:
    """对所有模块执行 drift 检测。"""
    deps_files = find_all_deps_files(project_root)
    reports = []

    for df in deps_files:
        data = load_deps_yaml(df)
        if data is None:
            continue
        module_name = data.get('metadata', {}).get('module_name', os.path.basename(os.path.dirname(df)))
        report = _analyze_drift(data, project_root)
        report['module'] = module_name
        report['deps_file'] = df
        reports.append(report)

    return reports


def _analyze_drift(deps_data: Dict[str, Any], project_root: str) -> Dict[str, Any]:
    """分析单个 deps.yaml 的 drift 状态。"""
    results = []
    overall_status = 'aligned'
    has_breaking = False
    has_minor = False

    consumes = deps_data.get('consumes', [])
    for entry in consumes:
        contract_file = entry.get('contract_file')
        pinned_version = entry.get('pinned_version')
        provider = entry.get('provider', 'unknown')

        # 跳过无法检测的条目（contract_file 为 null 或 pinned_version 为 null）
        if not contract_file or contract_file == 'null' or not pinned_version or pinned_version == 'null':
            results.append({
                'provider': provider,
                'pinned_version': pinned_version or 'N/A',
                'actual_version': 'N/A',
                'status': 'skipped',
            })
            continue

        actual_version = load_api_contract_version(contract_file, project_root)
        if actual_version is None:
            results.append({
                'provider': provider,
                'pinned_version': pinned_version,
                'actual_version': 'READ_ERROR',
                'status': 'error',
            })
            has_minor = True
            continue

        status = compare_versions(pinned_version, actual_version)
        results.append({
            'provider': provider,
            'pinned_version': pinned_version,
            'actual_version': actual_version,
            'status': status,
        })

        if status == 'breaking_drift':
            has_breaking = True
        elif status == 'minor_drift':
            has_minor = True

    if has_breaking:
        overall_status = 'breaking_drift'
    elif has_minor:
        overall_status = 'minor_drift'

    return {
        'results': results,
        'overall_status': overall_status,
    }


# =============================================================================
# Topology 生成
# =============================================================================

DEPENDENCY_TYPE_LABEL_MAP = {
    'rest_api': 'REST',
    'event_consumer': 'MQ',
    'event_publisher': 'MQ',
    'external': 'External',
}


def generate_topology(project_root: str) -> str:
    """生成 Mermaid 依赖拓扑图。"""
    deps_files = find_all_deps_files(project_root)
    edges = []

    for df in deps_files:
        data = load_deps_yaml(df)
        if data is None:
            continue

        module_name = data.get('metadata', {}).get('module_name', os.path.basename(os.path.dirname(df)))
        consumes = data.get('consumes', [])

        for entry in consumes:
            provider = entry.get('provider', 'unknown')
            dep_type = entry.get('dependency_type', 'rest_api')
            label = DEPENDENCY_TYPE_LABEL_MAP.get(dep_type, dep_type)
            edges.append((module_name, provider, label))

    # 生成 Mermaid 图
    lines = ['graph TD']
    for consumer, provider, label in edges:
        lines.append(f'    {consumer} -->|{label}| {provider}')

    return '\n'.join(lines)


# =============================================================================
# 输出格式化
# =============================================================================

def format_json_report(reports: List[Dict[str, Any]]) -> str:
    """将检测报告格式化为 JSON。"""
    return json.dumps(reports, ensure_ascii=False, indent=2)


def format_text_report(reports: List[Dict[str, Any]]) -> str:
    """将检测报告格式化为人类可读的表格。"""
    sections = []

    for report in reports:
        module = report.get('module', 'unknown')
        overall = report.get('overall_status', 'unknown')
        results = report.get('results', [])
        error = report.get('error')

        status_emoji = {
            'aligned': '✅',
            'minor_drift': '⚠️',
            'breaking_drift': '❌',
        }.get(overall, '❓')

        status_label = {
            'aligned': 'PASS',
            'minor_drift': 'WARN',
            'breaking_drift': 'BLOCK',
        }.get(overall, 'UNKNOWN')

        lines = []
        lines.append('## Contract Governance Check')
        lines.append('')
        lines.append(f'Module: {module}')
        lines.append(f'Status: {status_label}')
        lines.append('')

        if error:
            lines.append(f'Error: {error}')
            lines.append('')
            sections.append('\n'.join(lines))
            continue

        if not results:
            lines.append('No dependencies to check.')
            lines.append('')
            sections.append('\n'.join(lines))
            continue

        # 表格
        lines.append('| Provider | Pinned | Actual | Status |')
        lines.append('|----------|--------|--------|--------|')

        for r in results:
            status_str = {
                'aligned': '✅ aligned',
                'minor_drift': '⚠️ minor drift',
                'breaking_drift': '❌ breaking drift',
                'skipped': '⏭️ skipped',
                'error': '❗ read error',
            }.get(r['status'], f"? {r['status']}")
            lines.append(f"| {r['provider']} | {r['pinned_version']} | {r['actual_version']} | {status_str} |")

        lines.append('')
        lines.append(f'Overall: {status_emoji} {status_label}')
        lines.append('')
        sections.append('\n'.join(lines))

    return '\n'.join(sections)


# =============================================================================
# 主入口
# =============================================================================

def main():
    parser = argparse.ArgumentParser(
        description='Contract Drift Detection — 检测模块间 API 契约版本漂移',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
示例:
  # 检测指定模块
  python drift-detection.py --module payment-service --format json

  # 全量扫描
  python drift-detection.py --all --format text

  # 生成依赖拓扑图
  python drift-detection.py --topology --format mermaid
        '''
    )

    # 互斥的运行模式
    mode_group = parser.add_mutually_exclusive_group(required=True)
    mode_group.add_argument('--module', metavar='NAME',
                            help='检测指定模块的契约漂移')
    mode_group.add_argument('--all', action='store_true',
                            help='检测所有模块的契约漂移')
    mode_group.add_argument('--topology', action='store_true',
                            help='生成依赖拓扑图')

    parser.add_argument('--format', choices=['json', 'text', 'mermaid'],
                        default='json',
                        help='输出格式（默认: json）')
    parser.add_argument('--project-root', default=None,
                        help='项目根目录（默认: 脚本相对路径推算）')
    parser.add_argument('--output', default=None,
                        help='输出文件路径（默认: stdout）')

    args = parser.parse_args()

    # 确定项目根目录
    project_root = args.project_root or DEFAULT_PROJECT_ROOT
    if not os.path.isdir(project_root):
        print(f"❌ 项目根目录不存在: {project_root}", file=sys.stderr)
        sys.exit(EXIT_FILE_ERROR)

    # ---- topology 模式 ----
    if args.topology:
        mermaid_str = generate_topology(project_root)

        if args.format == 'mermaid' or args.format == 'text':
            output = mermaid_str
        elif args.format == 'json':
            output = json.dumps({
                'topology': mermaid_str,
                'format': 'mermaid',
            }, ensure_ascii=False, indent=2)
        else:
            output = mermaid_str

        if args.output:
            os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
            with open(args.output, 'w', encoding='utf-8') as f:
                f.write(output)
            print(f'✅ 拓扑图已写入: {args.output}')
        else:
            print(output)

        sys.exit(EXIT_OK)

    # ---- drift 检测模式 ----
    if args.module:
        report = check_module_drift(args.module, project_root)
        report['module'] = args.module
        reports = [report]
    else:
        reports = check_all_drift(project_root)

    # 判断全局退出码
    global_status = 'aligned'
    for report in reports:
        if report.get('overall_status') == 'breaking_drift':
            global_status = 'breaking_drift'
            break
        if report.get('overall_status') == 'minor_drift':
            global_status = 'minor_drift'

    # 格式化输出
    if args.format == 'json':
        output = format_json_report(reports)
    elif args.format == 'text':
        output = format_text_report(reports)
    elif args.format == 'mermaid':
        # mermaid 格式仅用于 topology 模式，此处降级为 text
        output = format_text_report(reports)
    else:
        output = format_json_report(reports)

    # 写入文件或 stdout
    if args.output:
        os.makedirs(os.path.dirname(args.output) or '.', exist_ok=True)
        with open(args.output, 'w', encoding='utf-8') as f:
            f.write(output)
        print(f'✅ 报告已写入: {args.output}')
    else:
        print(output)

    # 退出码
    if global_status == 'breaking_drift':
        sys.exit(EXIT_BREAKING_DRIFT)
    sys.exit(EXIT_OK)


if __name__ == '__main__':
    main()
