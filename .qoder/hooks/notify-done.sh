#!/bin/bash
# Qoder Hook: Desktop notification when Agent finishes
# Event: Stop
# Sends a macOS notification when the agent completes its task

osascript -e 'display notification "Agent 已完成任务" with title "Qoder" sound name "Glass"'
exit 0
