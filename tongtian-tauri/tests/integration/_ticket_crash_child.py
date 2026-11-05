"""杀进程重启演练的子进程（由 engine_ticket_crash_drill.py 调用）

在**真实进程**里跑两轮 worker 回合，第二轮的外发 dispatch 卡住不返回，父进程
在这时刻意把本进程杀掉——于是磁盘上留下一个「已提交、已租约占坑、但从未有了结」
的在途记录，正是崩溃重启要面对的形状。父进程接着以新进程重放同一条账本。

用法：python _ticket_crash_child.py <markers_dir> <ticket_id> <assignment_id>
数据根由父进程通过 TONGTIANWORK_HOME 传入，本脚本不自己清目录。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "engine"))

import ticket_dispatch_runtime as tdr  # noqa: E402
import ticket_outbox_runtime as tor  # noqa: E402
import ticket_recovery_runtime as trr  # noqa: E402
import ticket_worker_runtime as twr  # noqa: E402


def main() -> None:
    markers = Path(sys.argv[1])
    ticket_id = sys.argv[2]
    assignment_id = sys.argv[3]

    def mark(name: str, body: str = "") -> None:
        (markers / name).write_text(body, encoding="utf-8")

    registry = twr.TicketWorkerRegistry()
    outbox = tor.TicketOutbox()
    ledger = trr.ReadLedger()
    watchdog = trr.JobWatchdog()

    def send_dispatch(record):
        text = record["payload"]["text"]
        mark(f"sent-{text}", str(record["id"]))
        if text == "B":
            # 已经占了租约、已经提交，正在往外发——父进程在这里杀我
            mark("blocked-in-dispatch", str(record["id"]))
            time.sleep(600)
        return True

    script = [
        {
            "ok": True,
            "outcome": "running",
            "messages": [{"role": "assistant", "content": "A 已发出"}],
            "sends": [{"kind": "relay", "sessionKey": "sess-crash", "payload": "A", "dedupeKey": "dedupe-A"}],
            "jobs": [],
            "reason": "第一轮发完，任务还没结束",
        },
        {
            "ok": True,
            "outcome": "running",
            "messages": [
                {"role": "assistant", "content": "A 已发出"},
                {"role": "assistant", "content": "B 正在发"},
            ],
            "sends": [{"kind": "reply", "sessionKey": "sess-crash", "payload": "B", "dedupeKey": "dedupe-B"}],
            "jobs": [],
            "reason": "第二轮发到一半",
        },
    ]
    cursor = {"i": 0}

    def execute_turn(snapshot, prompt, history):
        result = script[cursor["i"]]
        cursor["i"] += 1
        return dict(result)

    dispatcher = tdr.TicketDispatcher(
        registry=registry,
        outbox=outbox,
        ledger=ledger,
        watchdog=watchdog,
        execute_turn=execute_turn,
        send_dispatch=send_dispatch,
    )

    first = dispatcher.wake_and_run(ticket_id=ticket_id, title="崩溃演练单", assignment_id=assignment_id, message="第一条唤醒")
    mark("round1-done", "ok" if first.get("ok") else "fail")

    dispatcher.wake_and_run(ticket_id=ticket_id, assignment_id=assignment_id, message="第二条唤醒")
    mark("round2-done", "永远不该出现")


if __name__ == "__main__":
    main()
