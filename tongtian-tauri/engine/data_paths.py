"""引擎侧数据根与数据文件路径的唯一解析处（D-43 存储归属）。

三套存储各有归属：桌面 SQLite（`tongtian.db`）只由 Rust 主进程打开，服务端
`market.db` 只由服务端进程打开，引擎只写本模块解析出的目录。过去这段规则在
检查点、意识目录、models.json、IM 凭据、平台目录里各抄了一份，改一处就会漏掉
其余；现在引擎侧只有这一处，其余模块一律从这里取路径。

环境变量优先级：`TONGTIANWORK_HOME`（与 Rust `awareness::dir()` 同规则）
→ `TT_DATA_HOME`（历史别名，IM 与平台目录先用过）→ `~/.tongtianwork`。
"""

from __future__ import annotations

import os
from pathlib import Path

HOME_ENV = "TONGTIANWORK_HOME"
LEGACY_HOME_ENV = "TT_DATA_HOME"

CHECKPOINTS_DIRNAME = "checkpoints"
AWARENESS_DIRNAME = "awareness"
USAGE_DIRNAME = "usage"
FILE_TRASH_DIRNAME = "file-trash"
WRITE_STAGING_DIRNAME = "write-staging"
TICKET_WORKERS_DIRNAME = "ticket-workers"
TICKET_OUTBOX_FILENAME = "ticket_outbox.json"
TICKET_READS_FILENAME = "ticket_reads.json"
TICKET_INBOX_FILENAME = "ticket_inbox.json"
IM_CONFIG_FILENAME = "im_channels.json"
MODELS_CONFIG_FILENAME = "models.json"
USAGE_LEDGER_FILENAME = "model-usage.jsonl"
CRASH_DIRNAME = "crash-reports"
TELEMETRY_DIRNAME = "telemetry"
TELEMETRY_CONSENT_FILENAME = "consent.json"
TELEMETRY_EVENTS_FILENAME = "events.jsonl"


def _env_dir(name: str) -> Path | None:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return None
    return Path(raw).expanduser()


def engine_home() -> Path:
    """引擎数据根；每次调用现读环境变量，测试可整体重定向到临时目录。"""
    for name in (HOME_ENV, LEGACY_HOME_ENV):
        override = _env_dir(name)
        if override is not None:
            return override
    return Path.home() / ".tongtianwork"


def checkpoints_dir() -> Path:
    return engine_home() / CHECKPOINTS_DIRNAME


def awareness_main_dir() -> Path:
    return engine_home() / AWARENESS_DIRNAME / "main"


def models_config_file() -> Path:
    return engine_home() / MODELS_CONFIG_FILENAME


def im_config_file() -> Path:
    return engine_home() / IM_CONFIG_FILENAME


def usage_dir() -> Path:
    return engine_home() / USAGE_DIRNAME


def file_trash_dir() -> Path:
    """删除文件工具的回收目录：先复制到这里再删原件，保证「完全访问」也不是不可恢复删除。"""
    return engine_home() / FILE_TRASH_DIRNAME


def write_staging_dir() -> Path:
    """写暂存区（缺陷 D-03.5）：模型要写的文件先落这里，界面批准后才合并进目标路径。

    只解析路径；暂存、diff、过期判定与合并的语义在 `write_staging_runtime`
    （不在这里留第二套规则）。这个目录必须留在数据根里而不是工作目录里——
    工作目录是模型能自己写的地方，把「待审批的内容」放那儿等于让审批对象被被审批者改。"""
    return engine_home() / WRITE_STAGING_DIRNAME


def ticket_workers_dir() -> Path:
    """工单级执行体的独立工作目录根（缺陷 D-48.1）。

    每个工单一个 `worker-<工单id摘要>` 子目录；本模块只解析路径，目录的创建与
    prepare 语义在 ticket_worker_runtime（不在这里留第二套规则）。"""
    return engine_home() / TICKET_WORKERS_DIRNAME


def ticket_outbox_file() -> Path:
    """工单外发的耐久副作用队列账本（缺陷 D-48.2）。

    只解析路径；入队、提交后才执行、保序与退避的语义在 ticket_outbox_runtime
    （不在这里留第二套规则）。"""
    return engine_home() / TICKET_OUTBOX_FILENAME


def ticket_reads_file() -> Path:
    """「消息是否已被会话读进历史」的按步骤落账账本（缺陷 D-48.3）。

    只解析路径；落账、查重与重启补查的语义在 ticket_recovery_runtime
    （不在这里留第二套规则）。"""
    return engine_home() / TICKET_READS_FILENAME


def ticket_inbox_file() -> Path:
    """工单唤醒的**耐久收件箱**日志（缺陷 D-48.6）。

    只解析路径；投递即落盘、被喂进本轮会话才销账、崩溃后由补查侧读回的语义在
    `ticket_inbox_runtime`（不在这里留第二套规则）。它与 `ticket_reads.json` 是
    两条不同的事实：**这条日志记「引擎收到过但没有进会话」，那份账本记「进过会话」**——
    两条都在盘上，重启后才谈得上不依赖任何内存件回答「唤醒丢没丢」。"""
    return engine_home() / TICKET_INBOX_FILENAME


def usage_ledger_file() -> Path:
    return usage_dir() / USAGE_LEDGER_FILENAME


def crash_dir() -> Path:
    """崩溃现场的留存根（缺陷 D-28 的后半，#141）。

    只解析路径；现场怎么写、脱敏哪几类、去重与上限、导出与删除的语义都在
    `crash_runtime`（不在这里留第二套规则）。放数据根而不是工作目录的理由和
    `write_staging_dir` 同一条：工作目录是模型能写的地方，「引擎自己崩过的证据」
    不该被被观察对象随手改掉或清掉。"""
    return engine_home() / CRASH_DIRNAME


def telemetry_dir() -> Path:
    """可选遥测的本地根（缺陷 D-28 的前半，#141）：同意状态与待发送缓冲。

    只解析路径；默认关、同意要确认词、字段白名单、缓冲上限与「没有出口」的如实
    回话都在 `telemetry_runtime`（不在这里留第二套规则）。
    """
    return engine_home() / TELEMETRY_DIRNAME


def telemetry_consent_file() -> Path:
    """同意状态文件：**这份文件是唯一的权威**，界面开关与 runtime 都只读它。"""
    return telemetry_dir() / TELEMETRY_CONSENT_FILENAME


def telemetry_events_file() -> Path:
    """本地事件缓冲（JSONL 追加）：同意关掉时整份删除，不留「先收了再说」。"""
    return telemetry_dir() / TELEMETRY_EVENTS_FILENAME
