/**
 * 通天办公 - Hooks 管理页 (对标 QwenWork Hooks)
 *
 * 设计:
 * - 左侧: 12 类事件分组列表（对齐 QwenWork Hooks 事件模型）
 * - 右侧: 选中事件的 Hook 列表 + 表单编辑器 + 试运行
 *
 * 执行面口径（页面文案必须与之一致，不能宣称「配了就会跑」）:
 * - 配置 ≠ 执行：命令需在本页「放行」后，才会由本机在真实任务里拉起；
 * - 12 类事件里只有 10 类在当前执行链路有真实触发点，另外 2 类只能配置与试运行；
 * - 删除某条配置的同时，它的放行一并作废。
 */

import React, { useState, useEffect, useCallback } from 'react';
import {
  Box,
  Typography,
  Button,
  TextField,
  MenuItem,
} from '@mui/material';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import AddIcon from '@mui/icons-material/Add';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import {
  getHooks,
  setHooks,
  testHook,
  getHooksStatus,
  grantHook,
  revokeHook,
} from '../../lib/alignment-api';
import { errorTextOf } from '../../../shared/error-messages';
import { useAppStore } from '../../stores/useAppStore';
import type { HookConfigStore, HookEventName, HooksStatus } from '../../../shared/contracts';

// ── 事件定义（对齐 QwenWork 12 类事件） ───────────────────────────

interface HookEventDef {
  name: string;
  desc: string;
  blockable: boolean;
  matcherLabel: string;
  /** 当前执行链路里是否真的有触发节点（与 Rust FIRED_EVENTS 一致，由契约测试锁定） */
  fired: boolean;
}

const hookEvents: HookEventDef[] = [
  { name: 'SessionStart', desc: '每轮任务开始时触发', blockable: false, matcherLabel: '会话来源', fired: true },
  { name: 'SessionEnd', desc: '每轮任务结束时触发（含取消/回滚）', blockable: false, matcherLabel: '结束原因', fired: true },
  { name: 'UserPromptSubmit', desc: '用户提交 Prompt 后、Agent 处理前触发', blockable: false, matcherLabel: '—', fired: true },
  { name: 'PreToolUse', desc: '工具执行前触发，exit 2 可阻止本次工具执行', blockable: true, matcherLabel: '工具名', fired: true },
  { name: 'PostToolUse', desc: '工具执行成功后触发', blockable: false, matcherLabel: '工具名', fired: true },
  { name: 'PostToolUseFailure', desc: '工具执行失败后触发', blockable: false, matcherLabel: '工具名', fired: true },
  { name: 'Stop', desc: 'Agent 完成响应后触发；exit 2 表示「别停」，但当前执行流不会续跑', blockable: true, matcherLabel: '—', fired: true },
  { name: 'SubagentStart', desc: '子 Agent 启动时触发（父 Agent 派发本地子任务）', blockable: false, matcherLabel: 'Agent 类型', fired: true },
  { name: 'SubagentStop', desc: '子 Agent 完成时触发，exit 2 可让其继续（最多续跑 1 次）', blockable: true, matcherLabel: 'Agent 类型', fired: true },
  { name: 'PreCompact', desc: '上下文压缩前触发（历史超过 20 条被截断时）', blockable: false, matcherLabel: '触发方式', fired: true },
  { name: 'Notification', desc: '权限请求/结果通知时触发', blockable: false, matcherLabel: '通知类型', fired: false },
  { name: 'PermissionRequest', desc: '工具需用户授权时触发', blockable: false, matcherLabel: '工具名', fired: false },
];

// ── Hook 条目模型 ─────────────────────────────────────────────────

interface HookItem {
  id: string;
  event: string;
  matcher: string;
  command: string;
  timeout: number;
}

// HookConfigStore 与 QwenWork settings.json 同构：挂载即存在、卸载即移除，无 enabled 字段。
// 因此页面不做「内置模板预置」——持久层为空就是空，避免把不存在的脚本当成用户配置保存并执行。

/** HookItem[] → HookConfigStore（持久化结构，对齐 QwenWork settings.json） */
function toConfigStore(items: HookItem[]): HookConfigStore {
  const hooks: HookConfigStore['hooks'] = {};
  items.forEach((h) => {
    const ev = h.event as HookEventName;
    const groups = hooks[ev] ?? [];
    let group = groups.find((g) => (g.matcher ?? '*') === (h.matcher || '*'));
    if (!group) {
      group = { matcher: h.matcher || '*', hooks: [] };
      groups.push(group);
    }
    group.hooks.push({ type: 'command', command: h.command, timeout: h.timeout });
    hooks[ev] = groups;
  });
  return { hooks };
}

/** HookConfigStore → HookItem[] */
function fromConfigStore(store: HookConfigStore): HookItem[] {
  const items: HookItem[] = [];
  let seq = 0;
  Object.entries(store.hooks ?? {}).forEach(([event, groups]) => {
    (groups ?? []).forEach((g) => {
      (g.hooks ?? []).forEach((h) => {
        items.push({
          id: `h-load-${++seq}`,
          event,
          matcher: g.matcher ?? '*',
          command: h.command,
          timeout: h.timeout ?? 60,
        });
      });
    });
  });
  return items;
}

// ── 主页面 ────────────────────────────────────────────────────────

const HooksManager: React.FC = () => {
  const [selectedEvent, setSelectedEvent] = useState('PreToolUse');
  const [hooks, setHooksState] = useState<HookItem[]>([]);
  const [editing, setEditing] = useState<HookItem | null>(null);
  const [testResult, setTestResult] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [status, setStatus] = useState<HooksStatus | null>(null);
  const [busyCommand, setBusyCommand] = useState('');
  const showNotification = useAppStore((s) => s.showNotification);

  const refreshStatus = useCallback(() => {
    getHooksStatus()
      .then(setStatus)
      // 执行面概览读不到时页面照常可用，但不能假装「全部已放行」
      .catch(() => setStatus(null));
  }, []);

  // 从持久层加载 Hooks 配置（真实 IPC：sqlite hook_configs + hook_grants；浏览器通道为同口径本地台账）
  useEffect(() => {
    getHooks()
      .then((store) => setHooksState(fromConfigStore(store)))
      .catch((err: unknown) => showNotification(errorTextOf(err, 'Hook 配置读取失败'), 'error'))
      .finally(() => setLoaded(true));
    refreshStatus();
  }, [showNotification, refreshStatus]);

  // 持久化当前 hooks 状态（校验失败必须可见：静默丢弃会让用户以为已保存）
  const persist = (items: HookItem[]) => {
    setHooks(toConfigStore(items))
      .then((res) => {
        if (res?.revoked?.length) {
          showNotification(
            `配置里已删除的命令一并撤销放行（${res.revoked.length} 条）：${res.revoked.join('；')}`,
            'info',
          );
        }
      })
      .catch((err: unknown) => {
        showNotification(errorTextOf(err, 'Hook 配置保存失败'), 'error');
      })
      .finally(refreshStatus);
  };

  const grantedOf = (command: string): boolean =>
    Boolean(status?.grants.some((g) => g.command === command.trim()));

  const runsOf = (command: string): number =>
    status?.grants.find((g) => g.command === command.trim())?.runCount ?? 0;

  const handleGrant = async (item: HookItem) => {
    const command = item.command.trim();
    if (!command) {
      showNotification('命令为空，无需放行（空命令永远不会触发任何脚本）', 'error');
      return;
    }
    setBusyCommand(command);
    try {
      await grantHook(item.event as HookEventName, command);
      showNotification(`已放行：${command} 会在真实任务的「${item.event}」节点由本机执行`, 'success');
    } catch (err: unknown) {
      showNotification(errorTextOf(err, 'Hook 放行失败'), 'error');
    } finally {
      setBusyCommand('');
      refreshStatus();
    }
  };

  const handleRevoke = async (item: HookItem) => {
    const command = item.command.trim();
    setBusyCommand(command);
    try {
      await revokeHook(command);
      showNotification(`已撤销放行：真实任务里不再执行「${item.event}」的 ${command}（配置仍保留）`, 'info');
    } catch (err: unknown) {
      showNotification(errorTextOf(err, 'Hook 撤销放行失败'), 'error');
    } finally {
      setBusyCommand('');
      refreshStatus();
    }
  };

  const eventDef = hookEvents.find((e) => e.name === selectedEvent)!;
  const eventHooks = hooks.filter((h) => h.event === selectedEvent);

  const handleAdd = () => {
    const item: HookItem = {
      id: `h${Date.now()}`,
      event: selectedEvent,
      matcher: '*',
      command: '',
      timeout: 60,
    };
    const next = [...hooks, item];
    setHooksState(next);
    setEditing(item);
  };

  const handleSave = () => {
    if (!editing) return;
    if (!editing.command.trim()) {
      showNotification('Hook 命令不能为空（留空不会触发任何脚本）', 'error');
      return;
    }
    const next = hooks.map((h) => (h.id === editing.id ? editing : h));
    setHooksState(next);
    setEditing(null);
    persist(next);
  };

  const handleTest = async () => {
    if (!editing) return;
    setTestResult('执行中…');
    try {
      const r = await testHook(editing.event as HookEventName, editing.command);
      setTestResult(
        `exit code: ${r.exitCode}${r.timedOut ? ' (超时)' : ''}\nstdout: ${r.stdout || '(空)'}\nstderr: ${r.stderr || '(空)'}\n→ ${
          r.exitCode === 0 ? 'Hook 执行成功，未阻塞' : r.exitCode === 2 ? 'Hook 阻塞（exit 2）' : 'Hook 执行出错'
        }`,
      );
    } catch (e) {
      setTestResult(errorTextOf(e, 'Hook 试运行失败'));
    }
  };

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', display: 'flex', flexDirection: 'column' }}>
      <Typography sx={{ fontSize: '20px', fontWeight: 700, color: '#111827' }}>Hooks</Typography>
      <Typography sx={{ fontSize: '13px', color: '#9CA3AF', mt: 0.5 }}>
        在执行的关键节点插入自定义逻辑。脚本通过 stdin 接收 JSON，exit code 2 表示阻塞（仅支持阻塞的事件生效）。
        命令需在本页「放行」后才会由本机在真实任务里执行——放行前只保存配置，不会拉起进程。
      </Typography>

      {status && (
        <Box
          sx={{
            mt: 1.5,
            p: 1.5,
            borderRadius: '12px',
            bgcolor: status.mountedUnfired.length || status.gateError ? '#FFFBEB' : '#F0FDF4',
            border: `1px solid ${status.mountedUnfired.length || status.gateError ? '#FDE68A' : '#BBF7D0'}`,
          }}
        >
          <Typography sx={{ fontSize: '12px', color: '#374151', lineHeight: 1.7 }}>
            当前 {status.configured} 条配置，其中 {status.ungranted.length} 条未放行（真实任务里不会执行）。
            {status.configured > 0 && status.ungranted.length === 0
              ? ' 已配置的命令均已放行。'
              : ''}
          </Typography>
          {status.mountedUnfired.length > 0 && (
            <Typography sx={{ fontSize: '12px', color: '#92400E', mt: 0.5, lineHeight: 1.7 }}>
              这些事件在当前执行链路没有触发点，只能配置与试运行，真实任务里不会执行：
              {status.mountedUnfired.join('、')}
            </Typography>
          )}
          {status.gateError && (
            <Typography sx={{ fontSize: '12px', color: '#92400E', mt: 0.5, lineHeight: 1.7 }}>
              {status.gateError}
            </Typography>
          )}
        </Box>
      )}

      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', gap: 3, mt: 2 }}>
        {/* 左侧事件列表 */}
        <Box sx={{ width: 220, flexShrink: 0, overflowY: 'auto' }}>
          {hookEvents.map((ev) => {
            const isActive = selectedEvent === ev.name;
            const count = hooks.filter((h) => h.event === ev.name).length;
            return (
              <Box
                key={ev.name}
                onClick={() => {
                  setSelectedEvent(ev.name);
                  setEditing(null);
                  setTestResult(null);
                }}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1,
                  px: 1.5,
                  py: 0.9,
                  mb: 0.25,
                  borderRadius: '10px',
                  cursor: 'pointer',
                  background: isActive ? '#E6F7F4' : 'transparent',
                  '&:hover': { background: isActive ? '#E6F7F4' : 'rgba(15, 23, 42, 0.04)' },
                }}
              >
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Typography
                    sx={{
                      fontSize: '12.5px',
                      fontWeight: isActive ? 600 : 400,
                      color: isActive ? '#0D9488' : '#374151',
                    }}
                  >
                    {ev.name}
                  </Typography>
                  {!ev.fired && (
                    <Typography sx={{ fontSize: '10.5px', color: '#C2913A' }}>无触发点</Typography>
                  )}
                </Box>
                {count > 0 && (
                  <Typography sx={{ fontSize: '11px', color: '#9CA3AF' }}>{count}</Typography>
                )}
              </Box>
            );
          })}
        </Box>

        {/* 右侧内容 */}
        <Box sx={{ flex: 1, minWidth: 0, overflowY: 'auto', pr: 1, maxWidth: 760 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mb: 1.5 }}>
            <Box>
              <Typography sx={{ fontSize: '16px', fontWeight: 700, color: '#111827' }}>
                {eventDef.name}
              </Typography>
              <Typography sx={{ fontSize: '12px', color: '#9CA3AF' }}>
                {eventDef.desc}
                {eventDef.blockable ? ' · 支持阻塞(exit 2)' : ''} · matcher 匹配：{eventDef.matcherLabel}
              </Typography>
              {!eventDef.fired && (
                <Typography sx={{ fontSize: '12px', color: '#B45309', mt: 0.25 }}>
                  该事件在当前执行链路没有触发点 · 可配置与试运行，但真实任务不会执行
                </Typography>
              )}
            </Box>
            <Box sx={{ flexGrow: 1 }} />
            <Button
              size="small"
              startIcon={<AddIcon sx={{ fontSize: 16 }} />}
              onClick={handleAdd}
              sx={{ bgcolor: '#111827', color: '#fff', fontSize: '12px', '&:hover': { bgcolor: '#1F2937' } }}
            >
              新增 Hook
            </Button>
          </Box>

          {/* Hook 列表 */}
          {loaded && eventHooks.length === 0 && !editing && (
            <Box sx={{ border: '1px dashed #E5E7EB', borderRadius: '14px', p: 4, textAlign: 'center' }}>
              <Typography sx={{ fontSize: '13px', color: '#9CA3AF' }}>
                该事件暂无 Hook 配置 · 未配置则不会执行任何脚本
              </Typography>
            </Box>
          )}
          {eventHooks.map((h) =>
            editing?.id === h.id ? null : (
              <Box
                key={h.id}
                onClick={() => {
                  setEditing({ ...h });
                  setTestResult(null);
                }}
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1.5,
                  bgcolor: '#FFFFFF',
                  border: '1px solid #E5E7EB',
                  borderRadius: '14px',
                  px: 2,
                  py: 1.5,
                  mb: 1,
                  cursor: 'pointer',
                  '&:hover': { borderColor: '#14B8A6' },
                }}
              >
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#111827' }}>
                      matcher: {h.matcher || '*'}
                    </Typography>
                    {grantedOf(h.command) ? (
                      <Box
                        sx={{
                          px: 0.75,
                          py: 0.1,
                          borderRadius: '6px',
                          bgcolor: '#DCFCE7',
                          color: '#15803D',
                          fontSize: '11px',
                          fontWeight: 600,
                        }}
                      >
                        已放行{runsOf(h.command) ? ` · 已执行 ${runsOf(h.command)} 次` : ''}
                      </Box>
                    ) : (
                      <Box
                        sx={{
                          px: 0.75,
                          py: 0.1,
                          borderRadius: '6px',
                          bgcolor: '#FEF3C7',
                          color: '#B45309',
                          fontSize: '11px',
                          fontWeight: 600,
                        }}
                      >
                        待放行
                      </Box>
                    )}
                  </Box>
                  <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }} noWrap>
                    {h.command || '(未配置命令)'} · 超时 {h.timeout}s
                  </Typography>
                  <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.25 }}>
                    {grantedOf(h.command)
                      ? '真实任务命中该节点时由本机执行'
                      : eventDef.fired
                        ? '未放行：配置已保存，但真实任务不会执行该命令'
                        : '该事件无触发点：放行也不会执行，仅可试运行'}
                  </Typography>
                </Box>
                {h.command.trim() && (
                  <Button
                    size="small"
                    disabled={busyCommand === h.command.trim() || !eventDef.fired}
                    onClick={(e) => {
                      e.stopPropagation();
                      if (grantedOf(h.command)) void handleRevoke(h);
                      else void handleGrant(h);
                    }}
                    sx={{
                      fontSize: '12px',
                      color: grantedOf(h.command) ? '#6B7280' : '#0D9488',
                      borderColor: grantedOf(h.command) ? '#E5E7EB' : '#14B8A6',
                      border: '1px solid',
                      borderRadius: '8px',
                      px: 1.25,
                      py: 0.4,
                    }}
                  >
                    {grantedOf(h.command) ? '撤销放行' : '放行'}
                  </Button>
                )}
                <DeleteOutlineIcon
                  sx={{ fontSize: 17, color: '#9CA3AF', '&:hover': { color: '#EF4444' } }}
                  onClick={(e) => {
                    e.stopPropagation();
                    const next = hooks.filter((x) => x.id !== h.id);
                    setHooksState(next);
                    persist(next);
                  }}
                />
              </Box>
            ),
          )}

          {/* 编辑器 */}
          {editing && (
            <Box sx={{ bgcolor: '#FFFFFF', border: '1px solid #14B8A6', borderRadius: '14px', p: 2.5, mt: 1 }}>
              <Typography sx={{ fontSize: '13px', fontWeight: 700, color: '#111827', mb: 1.5 }}>
                编辑 Hook
              </Typography>
              <TextField
                select
                size="small"
                fullWidth
                label="matcher（不填或 * 匹配全部，支持 A|B 与正则）"
                value={editing.matcher}
                onChange={(e) => setEditing({ ...editing, matcher: e.target.value })}
                sx={{ mb: 1, '& .MuiInputBase-input': { fontSize: '13px' } }}
              >
                <MenuItem value="*">*（全部）</MenuItem>
                <MenuItem value="Bash">Bash</MenuItem>
                <MenuItem value="Write | Edit">Write | Edit</MenuItem>
                <MenuItem value="mcp__.*">mcp__.*（正则）</MenuItem>
              </TextField>
              <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mb: 1.5 }}>
                正则按「包含」匹配（不锚定起止）；工具类事件匹配工具名，其余事件匹配
                source / reason / trigger。改命令文本等同于换一条脚本，需重新放行。
              </Typography>
              <TextField
                size="small"
                fullWidth
                label="command（shell 命令 / 脚本路径）"
                value={editing.command}
                onChange={(e) => setEditing({ ...editing, command: e.target.value })}
                sx={{ mb: 1.5, '& .MuiInputBase-input': { fontSize: '13px', fontFamily: 'Consolas, monospace' } }}
              />
              <Box sx={{ display: 'flex', gap: 1.5, alignItems: 'center' }}>
                <TextField
                  size="small"
                  type="number"
                  label="超时(秒)"
                  value={editing.timeout}
                  onChange={(e) => setEditing({ ...editing, timeout: Number(e.target.value) || 60 })}
                  sx={{ width: 120, '& .MuiInputBase-input': { fontSize: '13px' } }}
                />
                <Button size="small" startIcon={<PlayArrowIcon sx={{ fontSize: 15 }} />} onClick={handleTest} sx={{ color: '#0D9488', fontSize: '12px' }}>
                  试运行
                </Button>
                <Box sx={{ flexGrow: 1 }} />
                <Button size="small" onClick={() => setEditing(null)} sx={{ color: '#6B7280', fontSize: '12px' }}>
                  取消
                </Button>
                <Button
                  size="small"
                  variant="contained"
                  onClick={handleSave}
                  sx={{ bgcolor: '#111827', fontSize: '12px', '&:hover': { bgcolor: '#1F2937' } }}
                >
                  保存
                </Button>
              </Box>
              <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 1 }}>
                试运行会立即在本机执行该命令（不等放行），用于验证脚本本身；真实任务是否执行只由「放行」决定。
              </Typography>
              {testResult && (
                <Box
                  component="pre"
                  sx={{
                    mt: 1.5,
                    p: 1.5,
                    bgcolor: '#F9FAFB',
                    border: '1px solid #E5E7EB',
                    borderRadius: '8px',
                    fontSize: '12px',
                    color: '#374151',
                    fontFamily: 'Consolas, monospace',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {testResult}
                </Box>
              )}
            </Box>
          )}
        </Box>
      </Box>
    </Box>
  );
};

export default HooksManager;
