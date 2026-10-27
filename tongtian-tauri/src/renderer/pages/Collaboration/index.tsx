/**
 * 通天办公 - 项目协作页（跨用户 / 跨机器）
 *
 * 数据真值只有一个来源：账号服务的项目快照（Rust `collab_*` command 代理）。
 * 页面不做本地乐观缓存，所有变更成功后重新拉快照；轮询间隔 10s。
 *
 * 边界（界面如实说明）：
 * - 共享：项目元数据、成员与角色、任务、评论、结果摘要、产物文件名
 * - 不共享：对话、产物正文、技能、凭据、运行时状态
 * - 跨机器执行由任务负责人在自己电脑上显式点「在本机执行」，没有 Agent 自动接力
 */

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Avatar,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogActions,
  DialogContent,
  DialogTitle,
  Divider,
  FormControl,
  IconButton,
  InputLabel,
  MenuItem,
  Select,
  Snackbar,
  Stack,
  Tab,
  Tabs,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import AddIcon from '@mui/icons-material/Add';
import ContentCopyIcon from '@mui/icons-material/ContentCopy';
import DeleteOutlineIcon from '@mui/icons-material/DeleteOutline';
import GroupsIcon from '@mui/icons-material/Groups';
import PersonAddIcon from '@mui/icons-material/PersonAdd';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import RefreshIcon from '@mui/icons-material/Refresh';
import SwapHorizIcon from '@mui/icons-material/SwapHoriz';
import LogoutIcon from '@mui/icons-material/Logout';
import TaskAltIcon from '@mui/icons-material/TaskAlt';
import RateReviewIcon from '@mui/icons-material/RateReview';
import AutorenewIcon from '@mui/icons-material/Autorenew';
import RadioButtonUncheckedIcon from '@mui/icons-material/RadioButtonUnchecked';
import { errorTextOf } from '../../../shared/error-messages';
import type {
  CollabProject,
  CollabRole,
  CollabSnapshot,
  CollabStatus,
  CollabTask,
  CollabTaskPatch,
  CollabTaskStatus,
} from '../../../shared/contracts';
import { api } from '../../lib/api';
import {
  bindCollabRun,
  discardPendingReport,
  pendingReports,
  retryPendingReport,
  type CollabPendingReport,
} from '../../lib/collab-run';
import { useAuthStore } from '../../stores/useAuthStore';
import { useWorkbenchStore } from '../../store/workbenchStore';

// ── 展示口径 ────────────────────────────────────────────────────────

const ROLE_RANK: Record<CollabRole, number> = { viewer: 1, member: 2, admin: 3, owner: 4 };
const ROLE_LABEL: Record<CollabRole, string> = {
  owner: '所有者',
  admin: '管理员',
  member: '成员',
  viewer: '只读',
};

const STATUS_META: Record<
  CollabTaskStatus,
  { label: string; fg: string; bg: string; icon: React.ElementType }
> = {
  todo: { label: '待处理', fg: '#4B5563', bg: '#EEF0F2', icon: RadioButtonUncheckedIcon },
  doing: { label: '进行中', fg: '#B45309', bg: '#FEF3C7', icon: AutorenewIcon },
  review: { label: '待验收', fg: '#1D4ED8', bg: '#DBEAFE', icon: RateReviewIcon },
  done: { label: '已完成', fg: '#047857', bg: '#D1FAE5', icon: TaskAltIcon },
};

const STATUS_ORDER: CollabTaskStatus[] = ['todo', 'doing', 'review', 'done'];

const timeText = (ms: number) =>
  new Date(ms).toLocaleString('zh-CN', { hour12: false }).replace(/\//g, '-');

const basename = (path: string) => path.replace(/\\/g, '/').split('/').pop() ?? path;

function RoleChip({ role }: { role: CollabRole }) {
  const palette: Record<CollabRole, { fg: string; bg: string; border: string }> = {
    owner: { fg: '#B45309', bg: '#FEF3C7', border: '#FDE68A' },
    admin: { fg: '#4338CA', bg: '#E0E7FF', border: '#C7D2FE' },
    member: { fg: '#0F766E', bg: '#CCFBF1', border: '#99F6E4' },
    viewer: { fg: '#4B5563', bg: '#F3F4F6', border: '#E5E7EB' },
  };
  const c = palette[role];
  return (
    <Chip
      size="small"
      label={ROLE_LABEL[role]}
      sx={{ height: 21, fontSize: '11px', color: c.fg, bgcolor: c.bg, border: `1px solid ${c.border}` }}
    />
  );
}

function StatusChip({ status }: { status: CollabTaskStatus }) {
  const meta = STATUS_META[status];
  const Icon = meta.icon;
  return (
    <Chip
      size="small"
      icon={<Icon sx={{ fontSize: 13, color: `${meta.fg} !important` }} />}
      label={meta.label}
      sx={{ height: 22, fontSize: '11px', color: meta.fg, bgcolor: meta.bg }}
    />
  );
}

// ── 页面 ────────────────────────────────────────────────────────────

const Collaboration: React.FC = () => {
  const navigate = useNavigate();
  const loggedIn = useAuthStore((s) => s.loggedIn);
  const mockLogin = useAuthStore((s) => s.mock);
  const authBusy = useAuthStore((s) => s.busy);
  const authLogin = useAuthStore((s) => s.login);
  const authInit = useAuthStore((s) => s.init);
  const myUid = useAuthStore((s) => s.profile?.uid ?? '');

  const [status, setStatus] = useState<CollabStatus | null>(null);
  const [statusError, setStatusError] = useState('');
  const [projects, setProjects] = useState<CollabProject[]>([]);
  const [listLoading, setListLoading] = useState(false);
  const [listError, setListError] = useState('');
  const [activeId, setActiveId] = useState<string | null>(null);
  const [snapshot, setSnapshot] = useState<CollabSnapshot | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState('');
  const [tab, setTab] = useState(0);
  const [openTaskId, setOpenTaskId] = useState<string | null>(null);
  const [busy, setBusy] = useState('');
  const [toast, setToast] = useState<{ text: string; severity: 'success' | 'error' | 'info' } | null>(
    null,
  );
  const [pending, setPending] = useState<CollabPendingReport[]>(() => pendingReports());

  // 新建项目
  const [createOpen, setCreateOpen] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  // 接受邀请
  const [acceptOpen, setAcceptOpen] = useState(false);
  const [inviteToken, setInviteToken] = useState('');
  // 邀请成员
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState('');
  const [inviteRole, setInviteRole] = useState<CollabRole>('member');
  const [inviteCode, setInviteCode] = useState('');
  // 新建任务
  const [taskOpen, setTaskOpen] = useState(false);
  const [taskTitle, setTaskTitle] = useState('');
  const [taskDetail, setTaskDetail] = useState('');
  const [taskAssignee, setTaskAssignee] = useState('');
  // 评论
  const [commentBody, setCommentBody] = useState('');
  // 二次确认
  const [confirm, setConfirm] = useState<{
    title: string;
    body: string;
    danger?: boolean;
    run: () => Promise<void>;
  } | null>(null);

  const activeIdRef = useRef<string | null>(null);

  useEffect(() => {
    activeIdRef.current = activeId;
  }, [activeId]);

  const loadStatus = useCallback(async () => {
    setStatusError('');
    try {
      const result = await api.collab.status();
      setStatus(result);
      return result;
    } catch (err) {
      setStatusError(errorTextOf(err, '协作服务状态查询失败'));
      return null;
    }
  }, []);

  const loadProjects = useCallback(async (selectId?: string) => {
    setListLoading(true);
    setListError('');
    try {
      const resp = await api.collab.listProjects();
      setProjects(resp.projects);
      setActiveId((current) => {
        const next = selectId ?? current;
        if (next && resp.projects.some((p) => p.id === next)) return next;
        return resp.projects[0]?.id ?? null;
      });
    } catch (err) {
      setListError(errorTextOf(err, '项目列表加载失败'));
    } finally {
      setListLoading(false);
    }
  }, []);

  const loadDetail = useCallback(async (projectId: string, silent = false) => {
    if (!silent) setDetailLoading(true);
    try {
      const snap = await api.collab.detail(projectId);
      // 切换项目后旧请求可能晚到：只接受当前选中项目的快照
      if (activeIdRef.current === projectId && snap.project.id === projectId) {
        setSnapshot(snap);
        setDetailError('');
      }
    } catch (err) {
      if (activeIdRef.current === projectId) {
        setDetailError(errorTextOf(err, '项目详情加载失败'));
      }
    } finally {
      if (!silent) setDetailLoading(false);
    }
  }, []);

  useEffect(() => {
    void authInit();
  }, [authInit]);

  useEffect(() => {
    void (async () => {
      const result = await loadStatus();
      if (result?.supported && result.serverValid && result.loggedIn) {
        await loadProjects();
      }
    })();
  }, [loadStatus, loadProjects]);

  useEffect(() => {
    if (activeId) void loadDetail(activeId);
    else setSnapshot(null);
  }, [activeId, loadDetail]);

  // 轮询快照：仅页面可见时拉取，避免后台空转
  useEffect(() => {
    if (!activeId || !status?.supported || !status.serverValid || !status.loggedIn) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState === 'visible') void loadDetail(activeId, true);
    }, 10_000);
    return () => window.clearInterval(timer);
  }, [activeId, status, loadDetail]);

  const refreshAll = async () => {
    setToast(null);
    const result = await loadStatus();
    if (result?.supported && result.serverValid && result.loggedIn) {
      await loadProjects();
      if (activeId) await loadDetail(activeId);
      setPending(pendingReports());
    }
  };

  const run = async (label: string, fn: () => Promise<void>, after?: () => Promise<void>) => {
    setBusy(label);
    try {
      await fn();
      if (after) await after();
    } catch (err) {
      setToast({ text: errorTextOf(err, '操作失败'), severity: 'error' });
    } finally {
      setBusy('');
    }
  };

  const project = snapshot?.project ?? projects.find((p) => p.id === activeId) ?? null;
  const myRole = project?.myRole ?? 'viewer';
  const canManage = ROLE_RANK[myRole] >= ROLE_RANK.admin;
  const isOwner = myRole === 'owner';
  const openTask = useMemo(
    () => snapshot?.tasks.find((t) => t.id === openTaskId) ?? null,
    [snapshot, openTaskId],
  );
  const activeMembers = snapshot?.members ?? [];
  const assignableMembers = activeMembers.filter((m) => m.role !== 'viewer');

  const showToast = (text: string, severity: 'success' | 'info' = 'success') =>
    setToast({ text, severity });

  // ── 动作 ──────────────────────────────────────────────────────────

  const submitCreateProject = () =>
    run(
      'create-project',
      async () => {
        if (!newName.trim()) throw new Error('COLLAB_INVALID_INPUT: 请填写项目名称');
        const snap = await api.collab.createProject(newName.trim(), newDesc.trim());
        setCreateOpen(false);
        setNewName('');
        setNewDesc('');
        showToast(`项目已创建：${snap.project.name}`);
        await loadProjects(snap.project.id);
        setSnapshot(snap);
      },
    );

  const submitAcceptInvite = () =>
    run('accept-invite', async () => {
      if (!inviteToken.trim()) throw new Error('COLLAB_INVALID_INPUT: 请填写邀请码');
      const snap = await api.collab.acceptInvite(inviteToken.trim());
      setAcceptOpen(false);
      setInviteToken('');
      showToast(`已加入项目：${snap.project.name}`);
      await loadProjects(snap.project.id);
      setSnapshot(snap);
    });

  const submitInvite = () =>
    run('create-invite', async () => {
      if (!project) return;
      if (!inviteEmail.trim()) throw new Error('COLLAB_INVALID_INPUT: 请填写受邀邮箱');
      const resp = await api.collab.createInvite(project.id, inviteEmail.trim(), inviteRole);
      setInviteCode(resp.inviteCode);
      setInviteEmail('');
      await loadDetail(project.id);
      setPending(pendingReports());
    });

  const submitTask = () =>
    run('create-task', async () => {
      if (!project) return;
      if (!taskTitle.trim()) throw new Error('COLLAB_INVALID_INPUT: 请填写任务标题');
      await api.collab.createTask(project.id, taskTitle.trim(), taskDetail.trim(), taskAssignee || null);
      setTaskOpen(false);
      setTaskTitle('');
      setTaskDetail('');
      setTaskAssignee('');
      await loadDetail(project.id);
      showToast('任务已创建');
    });

  const patchTask = (task: CollabTask, patch: Omit<CollabTaskPatch, 'version'>, label: string) =>
    run(label, async () => {
      if (!project) return;
      try {
        await api.collab.updateTask(project.id, task.id, { ...patch, version: task.version });
      } catch (err) {
        // 冲突时先拉最新版本，避免用户拿着旧版本反复重试
        await loadDetail(project.id);
        throw err;
      }
      await loadDetail(project.id);
    });

  const submitComment = () =>
    run('comment', async () => {
      if (!project || !openTask) return;
      if (!commentBody.trim()) throw new Error('COLLAB_INVALID_INPUT: 请填写评论内容');
      await api.collab.addComment(project.id, openTask.id, commentBody.trim());
      setCommentBody('');
      await loadDetail(project.id);
    });

  const copyText = async (text: string) => {
    try {
      await navigator.clipboard.writeText(text);
      showToast('邀请码已复制');
    } catch {
      showToast('复制失败，请手动选中邀请码复制', 'info');
    }
  };

  const startOnThisMachine = (task: CollabTask) =>
    run('start-task', async () => {
      if (!project || !myUid) throw new Error('COLLAB_LOGIN_REQUIRED: 请先登录真实账号');
      if (task.assignee && task.assignee.uid !== myUid) {
        throw new Error('COLLAB_INVALID_INPUT: 该任务已指派给其他成员，不能在本机执行');
      }
      const patch: CollabTaskPatch = { version: task.version };
      if (!task.assignee) patch.assigneeUid = myUid;
      if (task.status !== 'doing') patch.status = 'doing';
      let version = task.version;
      if (patch.assigneeUid || patch.status) {
        const resp = await api.collab.updateTask(project.id, task.id, patch);
        version = resp.task.version;
      }
      const store = useWorkbenchStore.getState();
      store.newConversation();
      const convId = useWorkbenchStore.getState().activeId;
      if (!convId) throw new Error('COLLAB_INVALID_INPUT: 无法创建执行会话');
      bindCollabRun({
        conversationId: convId,
        projectId: project.id,
        projectName: project.name,
        taskId: task.id,
        taskTitle: task.title,
        version,
        startedAtMs: Date.now(),
      });
      const goal = [
        `【协作任务】${project.name} / ${task.title}`,
        task.detail ? `任务说明：${task.detail}` : '任务说明：无',
        '请在本机完成该任务。如需产出文件，请写入工作目录，并在最终回复中列出产物文件名与完成情况。',
      ].join('\n');
      void useWorkbenchStore.getState().submitGoal(goal);
      setOpenTaskId(null);
      navigate(`/?conv=${convId}`);
    });

  const changeRole = (uid: string, role: CollabRole) =>
    run('role', async () => {
      if (!project) return;
      await api.collab.updateMemberRole(project.id, uid, role);
      await loadDetail(project.id);
    });

  const askRemoveMember = (uid: string, nickname: string) => {
    if (!project) return;
    setConfirm({
      title: '移除成员',
      body: `确定将「${nickname || uid}」移出项目？其历史任务与评论会保留，未完成任务将取消指派。`,
      danger: true,
      run: async () => {
        await api.collab.removeMember(project.id, uid);
        await loadDetail(project.id);
        showToast('成员已移除');
      },
    });
  };

  const askTransferOwner = (uid: string, nickname: string) => {
    if (!project) return;
    setConfirm({
      title: '转让所有权',
      body: `确定把项目「${project.name}」的所有权转让给「${nickname || uid}」？转让后你将成为普通成员。`,
      run: async () => {
        await api.collab.transferOwner(project.id, uid);
        await loadDetail(project.id);
        await loadProjects(project.id);
        showToast('所有权已转让');
      },
    });
  };

  const askLeave = () => {
    if (!project) return;
    setConfirm({
      title: '退出项目',
      body: `确定退出项目「${project.name}」？退出后将无法再查看该项目的任务与评论。`,
      danger: true,
      run: async () => {
        await api.collab.leave(project.id);
        setActiveId(null);
        setSnapshot(null);
        await loadProjects();
        showToast('已退出项目');
      },
    });
  };

  const retryReport = (row: CollabPendingReport) =>
    run('retry-report', async () => {
      const result = await retryPendingReport(row.id);
      setPending(pendingReports());
      if (result.ok) showToast('待回报结果已同步');
      else throw new Error(result.error ?? '结果回报失败');
    });

  const canExecute = (task: CollabTask) => {
    if (!myUid || task.status === 'done' || ROLE_RANK[myRole] < ROLE_RANK.member) return false;
    if (task.assignee) return task.assignee.uid === myUid;
    // 未指派任务只有管理员/创建者能把自己指上去（普通成员改不了负责人）
    return canManage || task.createdBy.uid === myUid;
  };

  // ── 渲染 ──────────────────────────────────────────────────────────

  if (statusError) {
    return (
      <PageShell title="项目协作" onRefresh={refreshAll} busy={false}>
        <Alert severity="error" sx={{ borderRadius: '12px' }}>
          {statusError}
        </Alert>
      </PageShell>
    );
  }

  if (!status) {
    return (
      <Box sx={{ height: '100%', display: 'grid', placeItems: 'center' }}>
        <CircularProgress size={28} />
      </Box>
    );
  }

  if (!status.supported) {
    return (
      <PageShell title="项目协作" onRefresh={refreshAll} busy={false}>
        <Alert severity="info" sx={{ borderRadius: '12px' }}>
          当前运行通道无法访问协作服务（需要桌面端系统凭据库与账号服务直连）。
          请在桌面版任我行中使用项目协作；浏览器/Mock 通道不会伪造项目与任务数据。
        </Alert>
      </PageShell>
    );
  }

  if (!status.serverValid) {
    return (
      <PageShell title="项目协作" onRefresh={refreshAll} busy={false}>
        <Alert severity="error" sx={{ borderRadius: '12px' }}>
          协作服务地址不合法：{status.baseUrl || '未配置'}。
          远程地址必须使用 HTTPS；本机调试可使用 http://127.0.0.1 或 http://localhost。
          默认连接综合服务端 http://127.0.0.1:8000，可用环境变量 TT_AUTH_SERVER_URL 覆盖。
        </Alert>
      </PageShell>
    );
  }

  if (!status.loggedIn) {
    return (
      <PageShell title="项目协作" onRefresh={refreshAll} busy={false}>
        <Alert severity={mockLogin && loggedIn ? 'warning' : 'info'} sx={{ borderRadius: '12px' }}>
          <Typography sx={{ fontSize: '13px', fontWeight: 600, mb: 0.5 }}>
            {mockLogin && loggedIn ? '当前是 Mock 登录，项目协作需要真实账号' : '请先登录真实账号'}
          </Typography>
          <Typography sx={{ fontSize: '12.5px', color: 'inherit' }}>
            {mockLogin && loggedIn
              ? 'Mock 用户只存在于本机，无法加入真实项目。请设置 TT_AUTH_MOCK=false 后重启应用，再完成网页验证登录。'
              : '协作数据保存在综合服务端中，需要真实登录态才能读写；请确认已启动 tongtian-server（默认 127.0.0.1:8000）。'}
          </Typography>
          {!mockLogin && (
            <Button
              size="small"
              variant="contained"
              sx={{ mt: 1.5, textTransform: 'none' }}
              disabled={authBusy}
              onClick={() =>
                void authLogin().then(async (ok) => {
                  if (ok) {
                    const result = await loadStatus();
                    if (result?.loggedIn) await loadProjects();
                  } else {
                    setToast({ text: useAuthStore.getState().error ?? '登录失败', severity: 'error' });
                  }
                })
              }
            >
              网页验证登录
            </Button>
          )}
        </Alert>
      </PageShell>
    );
  }

  return (
    <PageShell title="项目协作" onRefresh={refreshAll} busy={listLoading || detailLoading}>
      <Alert severity="info" icon={false} sx={{ borderRadius: '12px', py: 0.5, mb: 2 }}>
        <Typography sx={{ fontSize: '12.5px' }}>
          跨用户 / 跨机器协作：只共享项目、任务、评论、结果摘要与产物文件名，不上传对话与文件内容。
          任务由负责人在自己电脑上点「在本机执行」，执行完成后自动回报结果；没有 Agent 之间的自动接力。
        </Typography>
      </Alert>

      <Box sx={{ display: 'grid', gridTemplateColumns: { xs: '1fr', md: '300px minmax(0, 1fr)' }, gap: 2 }}>
        {/* ── 左：项目列表 + 待回报 ── */}
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
          <Stack direction="row" spacing={1}>
            <Button
              fullWidth
              size="small"
              variant="contained"
              startIcon={<AddIcon />}
              onClick={() => setCreateOpen(true)}
              sx={{ textTransform: 'none', borderRadius: '10px' }}
            >
              新建项目
            </Button>
            <Tooltip title="接受邀请">
              <Button
                size="small"
                variant="outlined"
                startIcon={<PersonAddIcon />}
                onClick={() => setAcceptOpen(true)}
                sx={{ textTransform: 'none', borderRadius: '10px', whiteSpace: 'nowrap' }}
              >
                邀请码
              </Button>
            </Tooltip>
          </Stack>

          {listError && (
            <Alert severity="error" sx={{ borderRadius: '10px', fontSize: '12px' }}>
              {listError}
            </Alert>
          )}

          <Box
            sx={{
              border: '1px solid var(--tt-border-soft)',
              borderRadius: '14px',
              bgcolor: 'var(--tt-surface)',
              overflow: 'hidden',
            }}
          >
            {listLoading && projects.length === 0 ? (
              <Box sx={{ p: 3, textAlign: 'center' }}>
                <CircularProgress size={20} />
              </Box>
            ) : projects.length === 0 ? (
              <Box sx={{ p: 2.5, textAlign: 'center', color: 'var(--tt-text-4)' }}>
                <GroupsIcon sx={{ fontSize: 32, mb: 0.5, opacity: 0.6 }} />
                <Typography sx={{ fontSize: '12.5px' }}>还没有项目</Typography>
                <Typography sx={{ fontSize: '11.5px' }}>
                  新建项目，或用邀请码加入其他人的项目
                </Typography>
              </Box>
            ) : (
              projects.map((p, index) => {
                const selected = p.id === activeId;
                return (
                  <Box
                    key={p.id}
                    onClick={() => setActiveId(p.id)}
                    sx={{
                      px: 1.5,
                      py: 1.25,
                      cursor: 'pointer',
                      borderTop: index === 0 ? 'none' : '1px solid var(--tt-border-soft)',
                      bgcolor: selected ? 'rgba(99,102,241,0.07)' : 'transparent',
                      '&:hover': { bgcolor: 'rgba(99,102,241,0.05)' },
                    }}
                  >
                    <Stack direction="row" alignItems="center" spacing={0.75}>
                      <Typography
                        noWrap
                        sx={{ flex: 1, fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }}
                      >
                        {p.name}
                      </Typography>
                      <RoleChip role={p.myRole} />
                    </Stack>
                    <Stack direction="row" spacing={1} sx={{ mt: 0.5, color: 'var(--tt-text-4)' }}>
                      <Typography sx={{ fontSize: '11px' }}>{p.memberCount} 人</Typography>
                      <Typography sx={{ fontSize: '11px' }}>待处理 {p.taskCounts.todo}</Typography>
                      <Typography sx={{ fontSize: '11px' }}>待验收 {p.taskCounts.review}</Typography>
                      <Typography sx={{ fontSize: '11px' }}>已完成 {p.taskCounts.done}</Typography>
                    </Stack>
                  </Box>
                );
              })
            )}
          </Box>

          {pending.length > 0 && (
            <Box
              sx={{
                border: '1px solid #FDE68A',
                borderRadius: '14px',
                bgcolor: '#FFFBEB',
                p: 1.5,
              }}
            >
              <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: '#92400E', mb: 0.75 }}>
                待回报结果（{pending.length}）
              </Typography>
              <Stack spacing={1}>
                {pending.map((row) => (
                  <Box key={row.id}>
                    <Typography sx={{ fontSize: '12px', color: '#78350F' }}>
                      {row.projectName} / {row.taskTitle}
                    </Typography>
                    <Typography sx={{ fontSize: '11px', color: '#B45309', wordBreak: 'break-all' }}>
                      {row.error}
                    </Typography>
                    <Stack direction="row" spacing={0.75} sx={{ mt: 0.5 }}>
                      <Button
                        size="small"
                        variant="outlined"
                        disabled={busy === 'retry-report'}
                        onClick={() => void retryReport(row)}
                        sx={{ textTransform: 'none', minWidth: 0, py: 0, fontSize: '11.5px' }}
                      >
                        重试
                      </Button>
                      <Button
                        size="small"
                        disabled={busy === 'retry-report'}
                        onClick={() => {
                          discardPendingReport(row.id);
                          setPending(pendingReports());
                        }}
                        sx={{ textTransform: 'none', minWidth: 0, py: 0, fontSize: '11.5px', color: '#92400E' }}
                      >
                        丢弃
                      </Button>
                    </Stack>
                  </Box>
                ))}
              </Stack>
            </Box>
          )}
        </Box>

        {/* ── 右：项目详情 ── */}
        <Box
          sx={{
            border: '1px solid var(--tt-border-soft)',
            borderRadius: '14px',
            bgcolor: 'var(--tt-surface)',
            minHeight: 420,
            display: 'flex',
            flexDirection: 'column',
            overflow: 'hidden',
          }}
        >
          {!activeId ? (
            <Box sx={{ flex: 1, display: 'grid', placeItems: 'center', color: 'var(--tt-text-4)' }}>
              <Box sx={{ textAlign: 'center' }}>
                <GroupsIcon sx={{ fontSize: 40, opacity: 0.5 }} />
                <Typography sx={{ fontSize: '13px', mt: 1 }}>选择或创建一个项目</Typography>
              </Box>
            </Box>
          ) : detailError && !snapshot ? (
            <Box sx={{ p: 2 }}>
              <Alert severity="error" sx={{ borderRadius: '10px' }}>
                {detailError}
              </Alert>
            </Box>
          ) : !snapshot || snapshot.project.id !== activeId ? (
            <Box sx={{ flex: 1, display: 'grid', placeItems: 'center' }}>
              <CircularProgress size={24} />
            </Box>
          ) : (
            <>
              <Box sx={{ p: 2, pb: 1 }}>
                <Stack direction="row" alignItems="flex-start" spacing={1}>
                  <Box sx={{ flex: 1, minWidth: 0 }}>
                    <Stack direction="row" alignItems="center" spacing={1}>
                      <Typography sx={{ fontSize: '16px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                        {snapshot.project.name}
                      </Typography>
                      <RoleChip role={snapshot.project.myRole} />
                      {detailLoading && <CircularProgress size={13} />}
                    </Stack>
                    <Typography
                      sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.25, wordBreak: 'break-word' }}
                    >
                      {snapshot.project.description || '暂无项目说明'}
                    </Typography>
                  </Box>
                  <Button
                    size="small"
                    startIcon={<AddIcon />}
                    variant="outlined"
                    onClick={() => setTaskOpen(true)}
                    disabled={ROLE_RANK[myRole] < ROLE_RANK.member || busy !== ''}
                    sx={{ textTransform: 'none', whiteSpace: 'nowrap', borderRadius: '9px' }}
                  >
                    新建任务
                  </Button>
                </Stack>
                {detailError && (
                  <Alert severity="warning" sx={{ mt: 1, borderRadius: '9px', fontSize: '12px' }}>
                    刷新失败：{detailError}（当前显示上次成功的数据）
                  </Alert>
                )}
              </Box>
              <Tabs
                value={tab}
                onChange={(_, value) => setTab(value)}
                sx={{ px: 1.5, minHeight: 36, borderBottom: '1px solid var(--tt-border-soft)' }}
              >
                <Tab label={`任务 ${snapshot.tasks.length}`} sx={{ textTransform: 'none', minHeight: 36, fontSize: '12.5px' }} />
                <Tab label={`成员 ${snapshot.members.length}`} sx={{ textTransform: 'none', minHeight: 36, fontSize: '12.5px' }} />
                {canManage && (
                  <Tab label="邀请" sx={{ textTransform: 'none', minHeight: 36, fontSize: '12.5px' }} />
                )}
              </Tabs>

              <Box sx={{ flex: 1, overflow: 'auto', p: 1.5 }}>
                {tab === 0 && (
                  <Box
                    sx={{
                      display: 'grid',
                      gridTemplateColumns: { xs: '1fr', lg: 'repeat(4, minmax(0, 1fr))' },
                      gap: 1.25,
                      alignItems: 'start',
                    }}
                  >
                    {STATUS_ORDER.map((statusKey) => {
                      const meta = STATUS_META[statusKey];
                      const rows = snapshot.tasks.filter((t) => t.status === statusKey);
                      return (
                        <Box key={statusKey}>
                          <Stack direction="row" alignItems="center" spacing={0.75} sx={{ mb: 0.75 }}>
                            <Box sx={{ width: 7, height: 7, borderRadius: '50%', bgcolor: meta.fg }} />
                            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
                              {meta.label}
                            </Typography>
                            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                              {rows.length}
                            </Typography>
                          </Stack>
                          <Stack spacing={1}>
                            {rows.map((task) => (
                              <TaskCard
                                key={task.id}
                                task={task}
                                onClick={() => {
                                  setCommentBody('');
                                  setOpenTaskId(task.id);
                                }}
                              />
                            ))}
                            {rows.length === 0 && (
                              <Box
                                sx={{
                                  border: '1px dashed var(--tt-border-soft)',
                                  borderRadius: '10px',
                                  p: 1.5,
                                  textAlign: 'center',
                                  color: 'var(--tt-text-4)',
                                  fontSize: '11.5px',
                                }}
                              >
                                暂无任务
                              </Box>
                            )}
                          </Stack>
                        </Box>
                      );
                    })}
                  </Box>
                )}

                {tab === 1 && (
                  <Stack spacing={0.75}>
                    {snapshot.members.map((member) => {
                      const isMe = member.uid === myUid;
                      const canRemove =
                        canManage &&
                        member.role !== 'owner' &&
                        !(myRole === 'admin' && member.role === 'admin') &&
                        !isMe;
                      return (
                        <Stack
                          key={member.uid}
                          direction="row"
                          alignItems="center"
                          spacing={1}
                          sx={{
                            p: 1,
                            borderRadius: '10px',
                            border: '1px solid var(--tt-border-soft)',
                          }}
                        >
                          <Avatar sx={{ width: 28, height: 28, fontSize: 12, bgcolor: '#6366F1' }}>
                            {(member.nickname || member.uid).slice(0, 1).toUpperCase()}
                          </Avatar>
                          <Box sx={{ flex: 1, minWidth: 0 }}>
                            <Typography noWrap sx={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
                              {member.nickname || member.uid}
                              {isMe ? '（我）' : ''}
                            </Typography>
                            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                              加入于 {timeText(member.joinedAtMs)}
                            </Typography>
                          </Box>
                          {isOwner && member.role !== 'owner' ? (
                            <FormControl size="small" sx={{ minWidth: 92 }}>
                              <Select
                                value={member.role}
                                onChange={(e) => void changeRole(member.uid, e.target.value as CollabRole)}
                                sx={{ fontSize: '12px', height: 30 }}
                              >
                                <MenuItem value="admin" sx={{ fontSize: '12px' }}>管理员</MenuItem>
                                <MenuItem value="member" sx={{ fontSize: '12px' }}>成员</MenuItem>
                                <MenuItem value="viewer" sx={{ fontSize: '12px' }}>只读</MenuItem>
                              </Select>
                            </FormControl>
                          ) : (
                            <RoleChip role={member.role} />
                          )}
                          {isOwner && !isMe && (
                            <Tooltip title="转让所有权">
                              <IconButton
                                size="small"
                                onClick={() => askTransferOwner(member.uid, member.nickname)}
                              >
                                <SwapHorizIcon sx={{ fontSize: 17 }} />
                              </IconButton>
                            </Tooltip>
                          )}
                          {canRemove && (
                            <Tooltip title="移除成员">
                              <IconButton
                                size="small"
                                onClick={() => askRemoveMember(member.uid, member.nickname)}
                              >
                                <DeleteOutlineIcon sx={{ fontSize: 17, color: '#B91C1C' }} />
                              </IconButton>
                            </Tooltip>
                          )}
                        </Stack>
                      );
                    })}
                    <Divider sx={{ my: 1 }} />
                    <Stack direction="row" justifyContent="flex-end">
                      <Button
                        size="small"
                        color="error"
                        startIcon={<LogoutIcon />}
                        disabled={isOwner}
                        onClick={askLeave}
                        sx={{ textTransform: 'none' }}
                      >
                        退出项目
                      </Button>
                    </Stack>
                    {isOwner && (
                      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', textAlign: 'right' }}>
                        所有者不能直接退出，请先在成员列表转让所有权
                      </Typography>
                    )}
                  </Stack>
                )}

                {tab === 2 && canManage && (
                  <Stack spacing={1.5}>
                    <Stack direction="row" spacing={1} alignItems="flex-start">
                      <TextField
                        size="small"
                        label="受邀邮箱"
                        value={inviteEmail}
                        onChange={(e) => setInviteEmail(e.target.value)}
                        sx={{ flex: 1 }}
                      />
                      <FormControl size="small" sx={{ minWidth: 100 }}>
                        <InputLabel>角色</InputLabel>
                        <Select
                          label="角色"
                          value={inviteRole}
                          onChange={(e) => setInviteRole(e.target.value as CollabRole)}
                        >
                          {isOwner && <MenuItem value="admin">管理员</MenuItem>}
                          <MenuItem value="member">成员</MenuItem>
                          <MenuItem value="viewer">只读</MenuItem>
                        </Select>
                      </FormControl>
                      <Button
                        variant="contained"
                        size="small"
                        startIcon={<PersonAddIcon />}
                        disabled={busy === 'create-invite'}
                        onClick={() => void submitInvite()}
                        sx={{ textTransform: 'none', whiteSpace: 'nowrap', height: 40 }}
                      >
                        生成邀请码
                      </Button>
                    </Stack>
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
                      邀请码 7 天有效、只能使用一次，且必须由受邀邮箱对应的账号接受。服务端只保存摘要。
                    </Typography>
                    <Divider />
                    <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
                      待接受邀请（{snapshot.invites.length}）
                    </Typography>
                    {snapshot.invites.length === 0 ? (
                      <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>
                        暂无待接受邀请
                      </Typography>
                    ) : (
                      snapshot.invites.map((invite) => (
                        <Stack
                          key={invite.id}
                          direction="row"
                          alignItems="center"
                          spacing={1}
                          sx={{ p: 1, borderRadius: '10px', border: '1px solid var(--tt-border-soft)' }}
                        >
                          <Box sx={{ flex: 1, minWidth: 0 }}>
                            <Typography noWrap sx={{ fontSize: '12.5px', color: 'var(--tt-text-1)' }}>
                              {invite.email}
                            </Typography>
                            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                              {ROLE_LABEL[invite.role]} · 过期于 {timeText(invite.expiresAtMs)}
                            </Typography>
                          </Box>
                          <RoleChip role={invite.role} />
                        </Stack>
                      ))
                    )}
                  </Stack>
                )}
              </Box>
            </>
          )}
        </Box>
      </Box>

      {/* ── 新建项目 ── */}
      <Dialog open={createOpen} onClose={() => setCreateOpen(false)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 360 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>新建项目</DialogTitle>
        <DialogContent>
          <Stack spacing={1.5} sx={{ mt: 0.5 }}>
            <TextField
              size="small"
              label="项目名称"
              value={newName}
              onChange={(e) => setNewName(e.target.value)}
              inputProps={{ maxLength: 60 }}
              autoFocus
            />
            <TextField
              size="small"
              label="项目说明（可选）"
              value={newDesc}
              onChange={(e) => setNewDesc(e.target.value)}
              multiline
              minRows={3}
              inputProps={{ maxLength: 2000 }}
            />
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setCreateOpen(false)} sx={{ textTransform: 'none' }}>取消</Button>
          <Button
            variant="contained"
            disabled={busy === 'create-project'}
            onClick={() => void submitCreateProject()}
            sx={{ textTransform: 'none' }}
          >
            创建
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── 接受邀请 ── */}
      <Dialog open={acceptOpen} onClose={() => setAcceptOpen(false)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 380 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>接受项目邀请</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mb: 1.5 }}>
            粘贴项目管理员发给你的邀请码。邀请码一次性使用，且当前登录邮箱必须与受邀邮箱一致。
          </Typography>
          <TextField
            fullWidth
            size="small"
            label="邀请码"
            value={inviteToken}
            onChange={(e) => setInviteToken(e.target.value)}
            autoFocus
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAcceptOpen(false)} sx={{ textTransform: 'none' }}>取消</Button>
          <Button
            variant="contained"
            disabled={busy === 'accept-invite'}
            onClick={() => void submitAcceptInvite()}
            sx={{ textTransform: 'none' }}
          >
            接受邀请
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── 一次性邀请码 ── */}
      <Dialog open={Boolean(inviteCode)} onClose={() => setInviteCode('')} PaperProps={{ sx: { borderRadius: '14px', minWidth: 420 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>邀请码（只显示这一次）</DialogTitle>
        <DialogContent>
          <Alert severity="warning" sx={{ borderRadius: '10px', mb: 1.5, fontSize: '12px' }}>
            关闭后无法再次查看；服务端只保存摘要。请现在复制并发给受邀人。
          </Alert>
          <Stack direction="row" spacing={1}>
            <TextField fullWidth size="small" value={inviteCode} InputProps={{ readOnly: true }} />
            <Button
              variant="outlined"
              startIcon={<ContentCopyIcon />}
              onClick={() => void copyText(inviteCode)}
              sx={{ textTransform: 'none', whiteSpace: 'nowrap' }}
            >
              复制
            </Button>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setInviteCode('')} sx={{ textTransform: 'none' }}>我已保存</Button>
        </DialogActions>
      </Dialog>

      {/* ── 新建任务 ── */}
      <Dialog open={taskOpen} onClose={() => setTaskOpen(false)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 420 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>新建任务</DialogTitle>
        <DialogContent>
          <Stack spacing={1.5} sx={{ mt: 0.5 }}>
            <TextField
              size="small"
              label="任务标题"
              value={taskTitle}
              onChange={(e) => setTaskTitle(e.target.value)}
              inputProps={{ maxLength: 120 }}
              autoFocus
            />
            <TextField
              size="small"
              label="任务说明（可选）"
              value={taskDetail}
              onChange={(e) => setTaskDetail(e.target.value)}
              multiline
              minRows={3}
              inputProps={{ maxLength: 4000 }}
            />
            <FormControl size="small">
              <InputLabel>负责人（可选）</InputLabel>
              <Select
                label="负责人（可选）"
                value={taskAssignee}
                onChange={(e) => setTaskAssignee(e.target.value)}
              >
                <MenuItem value="">暂不指派</MenuItem>
                {assignableMembers.map((member) => (
                  <MenuItem key={member.uid} value={member.uid}>
                    {member.nickname || member.uid}
                    {member.uid === myUid ? '（我）' : ''}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setTaskOpen(false)} sx={{ textTransform: 'none' }}>取消</Button>
          <Button
            variant="contained"
            disabled={busy === 'create-task'}
            onClick={() => void submitTask()}
            sx={{ textTransform: 'none' }}
          >
            创建任务
          </Button>
        </DialogActions>
      </Dialog>

      {/* ── 任务详情 ── */}
      <TaskDialog
        task={openTask}
        snapshot={snapshot}
        myUid={myUid}
        myRole={myRole}
        busy={busy}
        commentBody={commentBody}
        onCommentBody={setCommentBody}
        onClose={() => setOpenTaskId(null)}
        onPatch={(task, patch, label) => void patchTask(task, patch, label)}
        onComment={() => void submitComment()}
        onExecute={(task) => void startOnThisMachine(task)}
        canExecute={openTask ? canExecute(openTask) : false}
      />

      {/* ── 二次确认 ── */}
      <Dialog open={Boolean(confirm)} onClose={() => setConfirm(null)} PaperProps={{ sx: { borderRadius: '14px', minWidth: 360 } }}>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>{confirm?.title}</DialogTitle>
        <DialogContent>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>{confirm?.body}</Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirm(null)} sx={{ textTransform: 'none' }}>取消</Button>
          <Button
            color={confirm?.danger ? 'error' : 'primary'}
            variant="contained"
            onClick={() => {
              const action = confirm;
              setConfirm(null);
              if (action) void run('confirm', action.run);
            }}
            sx={{ textTransform: 'none' }}
          >
            确认
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={Boolean(toast)}
        autoHideDuration={4000}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert
          severity={toast?.severity ?? 'success'}
          onClose={() => setToast(null)}
          sx={{ borderRadius: '10px', fontSize: '12.5px' }}
        >
          {toast?.text}
        </Alert>
      </Snackbar>
    </PageShell>
  );
};

// ── 子组件 ──────────────────────────────────────────────────────────

function PageShell({
  title,
  onRefresh,
  busy,
  children,
}: {
  title: string;
  onRefresh: () => Promise<void>;
  busy: boolean;
  children: React.ReactNode;
}) {
  return (
    <Box sx={{ height: '100%', overflow: 'auto', p: { xs: 2, md: 3 } }}>
      <Stack direction="row" alignItems="center" spacing={1.25} sx={{ mb: 2 }}>
        <Box
          sx={{
            width: 34,
            height: 34,
            borderRadius: '10px',
            display: 'grid',
            placeItems: 'center',
            bgcolor: 'rgba(99,102,241,0.12)',
            color: '#4F46E5',
          }}
        >
          <GroupsIcon sx={{ fontSize: 20 }} />
        </Box>
        <Box sx={{ flex: 1 }}>
          <Typography sx={{ fontSize: '17px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
            {title}
          </Typography>
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
            跨用户 / 跨机器 · 人工触发任务交接
          </Typography>
        </Box>
        <Tooltip title="刷新">
          <span>
            <IconButton size="small" onClick={() => void onRefresh()} disabled={busy}>
              <RefreshIcon sx={{ fontSize: 19 }} />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      {children}
    </Box>
  );
}

function TaskCard({ task, onClick }: { task: CollabTask; onClick: () => void }) {
  return (
    <Box
      onClick={onClick}
      sx={{
        p: 1.25,
        borderRadius: '10px',
        border: '1px solid var(--tt-border-soft)',
        bgcolor: 'var(--tt-surface)',
        cursor: 'pointer',
        '&:hover': { borderColor: 'rgba(99,102,241,0.4)', boxShadow: '0 4px 14px rgba(30,41,59,0.06)' },
      }}
    >
      <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--tt-text-1)', wordBreak: 'break-word' }}>
        {task.title}
      </Typography>
      <Stack direction="row" alignItems="center" spacing={0.5} sx={{ mt: 0.75, flexWrap: 'wrap', gap: 0.5 }}>
        <Chip
          size="small"
          label={task.assignee?.nickname || '未指派'}
          sx={{ height: 20, fontSize: '10.5px', bgcolor: task.assignee ? '#EEF2FF' : '#F3F4F6', color: task.assignee ? '#3730A3' : '#6B7280' }}
        />
        {task.result && (
          <Chip
            size="small"
            label={`${task.result.artifacts.length} 个产物`}
            sx={{ height: 20, fontSize: '10.5px', bgcolor: '#ECFDF5', color: '#047857' }}
          />
        )}
        <Typography sx={{ fontSize: '10.5px', color: 'var(--tt-text-4)', ml: 'auto !important' }}>
          v{task.version}
        </Typography>
      </Stack>
    </Box>
  );
}

function TaskDialog({
  task,
  snapshot,
  myUid,
  myRole,
  busy,
  commentBody,
  onCommentBody,
  onClose,
  onPatch,
  onComment,
  onExecute,
  canExecute,
}: {
  task: CollabTask | null;
  snapshot: CollabSnapshot | null;
  myUid: string;
  myRole: CollabRole;
  busy: string;
  commentBody: string;
  onCommentBody: (value: string) => void;
  onClose: () => void;
  onPatch: (task: CollabTask, patch: Omit<CollabTaskPatch, 'version'>, label: string) => void;
  onComment: () => void;
  onExecute: (task: CollabTask) => void;
  canExecute: boolean;
}) {
  if (!task || !snapshot) return null;
  const rank = ROLE_RANK[myRole];
  const privileged = rank >= ROLE_RANK.admin || task.createdBy.uid === myUid;
  const isAssignee = task.assignee?.uid === myUid;
  const canStatus = privileged || isAssignee;
  const members = snapshot.members.filter((m) => m.role !== 'viewer');
  const comments = snapshot.comments.filter((c) => c.taskId === task.id);
  const meta = STATUS_META[task.status];

  return (
    <Dialog open onClose={onClose} fullWidth maxWidth="sm" PaperProps={{ sx: { borderRadius: '14px' } }}>
      <DialogTitle sx={{ pr: 6 }}>
        <Typography sx={{ fontSize: '15px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
          {task.title}
        </Typography>
        <Stack direction="row" alignItems="center" spacing={0.75} sx={{ mt: 0.5, flexWrap: 'wrap', gap: 0.5 }}>
          <StatusChip status={task.status} />
          <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
            创建者 {task.createdBy.nickname || task.createdBy.uid} · v{task.version} · 更新于 {timeText(task.updatedAtMs)}
          </Typography>
        </Stack>
      </DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', whiteSpace: 'pre-wrap', mb: 2 }}>
          {task.detail || '暂无任务说明'}
        </Typography>

        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2, flexWrap: 'wrap', gap: 1 }}>
          <FormControl size="small" sx={{ minWidth: 120 }}>
            <InputLabel>状态</InputLabel>
            <Select
              label="状态"
              value={task.status}
              disabled={!canStatus || busy !== ''}
              onChange={(e) =>
                onPatch(task, { status: e.target.value as CollabTaskStatus }, 'task-status')
              }
            >
              {STATUS_ORDER.map((key) => (
                <MenuItem key={key} value={key} sx={{ fontSize: '12.5px' }}>
                  {STATUS_META[key].label}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <FormControl size="small" sx={{ minWidth: 150 }}>
            <InputLabel>负责人</InputLabel>
            <Select
              label="负责人"
              value={task.assignee?.uid ?? ''}
              disabled={!privileged || busy !== ''}
              onChange={(e) => onPatch(task, { assigneeUid: e.target.value || null }, 'task-assignee')}
            >
              <MenuItem value="" sx={{ fontSize: '12.5px' }}>未指派</MenuItem>
              {members.map((member) => (
                <MenuItem key={member.uid} value={member.uid} sx={{ fontSize: '12.5px' }}>
                  {member.nickname || member.uid}
                  {member.uid === myUid ? '（我）' : ''}
                </MenuItem>
              ))}
            </Select>
          </FormControl>
          <Box sx={{ flex: 1 }} />
          {canExecute && (
            <Button
              variant="contained"
              size="small"
              startIcon={<PlayArrowIcon />}
              disabled={busy !== ''}
              onClick={() => onExecute(task)}
              sx={{ textTransform: 'none', whiteSpace: 'nowrap', bgcolor: meta.fg }}
            >
              {task.assignee?.uid === myUid ? '在本机执行' : '接手并在本机执行'}
            </Button>
          )}
        </Stack>
        {!canStatus && (
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mb: 1.5 }}>
            只有项目管理员、任务创建者或负责人可以修改状态。
          </Typography>
        )}
        {task.assignee?.uid !== myUid && task.assignee && (
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mb: 1.5 }}>
            该任务由「{task.assignee.nickname || task.assignee.uid}」负责，需由他/她在自己的电脑上执行。
          </Typography>
        )}

        {task.result && (
          <Box sx={{ p: 1.25, borderRadius: '10px', bgcolor: '#F0FDF4', border: '1px solid #BBF7D0', mb: 2 }}>
            <Typography sx={{ fontSize: '12px', fontWeight: 700, color: '#166534', mb: 0.5 }}>
              执行结果
            </Typography>
            <Typography sx={{ fontSize: '12.5px', color: '#14532D', whiteSpace: 'pre-wrap' }}>
              {task.result.summary}
            </Typography>
            {task.result.artifacts.length > 0 && (
              <Stack direction="row" spacing={0.5} sx={{ mt: 0.75, flexWrap: 'wrap', gap: 0.5 }}>
                {task.result.artifacts.map((name) => (
                  <Chip key={name} size="small" label={basename(name)} sx={{ height: 21, fontSize: '10.5px' }} />
                ))}
              </Stack>
            )}
            <Typography sx={{ fontSize: '10.5px', color: '#15803D', mt: 0.75 }}>
              {task.result.byNickname || task.result.by} · {task.result.machine || '未知机器'} ·{' '}
              {timeText(task.result.atMs)}
            </Typography>
          </Box>
        )}

        <Divider sx={{ mb: 1.5 }} />
        <Typography sx={{ fontSize: '12.5px', fontWeight: 600, color: 'var(--tt-text-2)', mb: 1 }}>
          评论（{comments.length}）
        </Typography>
        <Stack spacing={1} sx={{ mb: 1.5 }}>
          {comments.length === 0 && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>还没有评论</Typography>
          )}
          {comments.map((comment) => (
            <Box key={comment.id} sx={{ p: 1, borderRadius: '9px', bgcolor: 'var(--tt-surface-2, #F8FAFC)' }}>
              <Stack direction="row" alignItems="center" spacing={0.75}>
                <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
                  {comment.author.nickname || comment.author.uid}
                </Typography>
                <Typography sx={{ fontSize: '10.5px', color: 'var(--tt-text-4)' }}>
                  {timeText(comment.createdAtMs)}
                </Typography>
              </Stack>
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', whiteSpace: 'pre-wrap', mt: 0.25 }}>
                {comment.body}
              </Typography>
            </Box>
          ))}
        </Stack>
        {rank >= ROLE_RANK.member && (
          <Stack direction="row" spacing={1} alignItems="flex-end">
            <TextField
              fullWidth
              size="small"
              label="写评论"
              value={commentBody}
              onChange={(e) => onCommentBody(e.target.value)}
              multiline
              maxRows={4}
              inputProps={{ maxLength: 2000 }}
            />
            <Button
              variant="outlined"
              disabled={busy === 'comment' || !commentBody.trim()}
              onClick={onComment}
              sx={{ textTransform: 'none', whiteSpace: 'nowrap' }}
            >
              发送
            </Button>
          </Stack>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none' }}>关闭</Button>
      </DialogActions>
    </Dialog>
  );
}

export default Collaboration;
