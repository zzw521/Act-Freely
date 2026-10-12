/**
 * 通天办公 - Hooks 管理页 (对标 QwenWork Hooks)
 *
 * 设计:
 * - 左侧: 12 类事件分组列表（对齐 QwenWork Hooks 事件模型）
 * - 右侧: 选中事件的 Hook 列表 + 表单/JSON 双模编辑器 + 试运行
 * - 内置模板: 拦截危险命令 / 写文件后格式化 / 任务完成通知
 */

import React, { useState, useEffect } from 'react';
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
import { getHooks, setHooks, testHook } from '../../lib/alignment-api';
import { errorTextOf } from '../../../shared/error-messages';
import { useAppStore } from '../../stores/useAppStore';
import type { HookConfigStore, HookEventName } from '../../../shared/contracts';

// ── 事件定义（对齐 QwenWork 12 类事件） ───────────────────────────

interface HookEventDef {
  name: string;
  desc: string;
  blockable: boolean;
  matcherLabel: string;
}

const hookEvents: HookEventDef[] = [
  { name: 'SessionStart', desc: '会话开始时触发', blockable: false, matcherLabel: '会话来源' },
  { name: 'SessionEnd', desc: '会话结束时触发', blockable: false, matcherLabel: '结束原因' },
  { name: 'UserPromptSubmit', desc: '用户提交 Prompt 后、Agent 处理前触发', blockable: false, matcherLabel: '—' },
  { name: 'PreToolUse', desc: '工具执行前触发，可阻止执行', blockable: true, matcherLabel: '工具名' },
  { name: 'PostToolUse', desc: '工具执行成功后触发', blockable: false, matcherLabel: '工具名' },
  { name: 'PostToolUseFailure', desc: '工具执行失败后触发', blockable: false, matcherLabel: '工具名' },
  { name: 'Stop', desc: 'Agent 完成响应后触发，可让其继续', blockable: true, matcherLabel: '—' },
  { name: 'SubagentStart', desc: '子 Agent 启动时触发', blockable: false, matcherLabel: 'Agent 类型' },
  { name: 'SubagentStop', desc: '子 Agent 完成时触发，可让其继续', blockable: true, matcherLabel: 'Agent 类型' },
  { name: 'PreCompact', desc: '上下文压缩前触发', blockable: false, matcherLabel: '触发方式' },
  { name: 'Notification', desc: '权限请求/结果通知时触发', blockable: false, matcherLabel: '通知类型' },
  { name: 'PermissionRequest', desc: '工具需用户授权时触发', blockable: false, matcherLabel: '工具名' },
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
  const showNotification = useAppStore((s) => s.showNotification);

  // 从持久层加载 Hooks 配置（真实 IPC：sqlite hook_configs；浏览器通道为同口径本地台账）
  useEffect(() => {
    getHooks()
      .then((store) => setHooksState(fromConfigStore(store)))
      .catch((err: unknown) => showNotification(errorTextOf(err, 'Hook 配置读取失败'), 'error'))
      .finally(() => setLoaded(true));
  }, [showNotification]);

  // 持久化当前 hooks 状态（校验失败必须可见：静默丢弃会让用户以为已保存）
  const persist = (items: HookItem[]) => {
    setHooks(toConfigStore(items)).catch((err: unknown) => {
      showNotification(errorTextOf(err, 'Hook 配置保存失败'), 'error');
    });
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
        在执行的关键节点插入自定义逻辑。Hooks 是确定性的——事件触发则脚本必执行，不受模型理解影响。
        脚本通过 stdin 接收 JSON，exit code 2 表示阻塞（仅支持阻塞的事件生效）。
      </Typography>

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
                <Typography
                  sx={{
                    fontSize: '12.5px',
                    fontWeight: isActive ? 600 : 400,
                    color: isActive ? '#0D9488' : '#374151',
                    flexGrow: 1,
                  }}
                >
                  {ev.name}
                </Typography>
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
                  <Typography sx={{ fontSize: '13px', fontWeight: 600, color: '#111827' }}>
                    matcher: {h.matcher || '*'}
                  </Typography>
                  <Typography sx={{ fontSize: '12px', color: '#9CA3AF', mt: 0.25 }} noWrap>
                    {h.command || '(未配置命令)'} · 超时 {h.timeout}s
                  </Typography>
                </Box>
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
                value={['*', editing.matcher].includes(editing.matcher) ? editing.matcher : editing.matcher}
                onChange={(e) => setEditing({ ...editing, matcher: e.target.value })}
                sx={{ mb: 1.5, '& .MuiInputBase-input': { fontSize: '13px' } }}
              >
                <MenuItem value="*">*（全部）</MenuItem>
                <MenuItem value="Bash">Bash</MenuItem>
                <MenuItem value="Write | Edit">Write | Edit</MenuItem>
                <MenuItem value="mcp__.*">mcp__.*（正则）</MenuItem>
              </TextField>
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
