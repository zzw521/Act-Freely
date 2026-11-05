/**
 * 通天办公 - 等你的工单（缺陷 D-47.5）
 *
 * 这一页是 D-47 工单层在桌面上的唯一入口，它只回答一个问题：**现在有哪些事卡在等人，
 * 人能做的那几个动作分别是什么。** 判定口径全部在服务端（`tongtian-server/app/tickets.py`），
 * 这一层不做第二套：
 *
 * - 「等你」只由结构化字段判定：`stageLabel`、`needsHuman`、`attention.kindLabel` 都来自
 *   服务端视图，前端不读正文猜情绪，也不自己翻译 stage。
 * - 列表默认只列需要人动手的三种 stage（等你回答 / 等你验收 / 等人处理）——健康态不占版面，
 *   这是本层的核心取舍：Agent 越多，人越不该盯着看。关掉开关可以看全部 stage。
 * - 人在这一层只有四个动作：**回答**（answer 类问题）、**验收**（review 类问题：通过=收口、
 *   退回=重排队且授权不变）、**扩大范围**（mandate/amend，追加式改版，活着的执行体会被结束
 *   退回排队）、**叫停**（全系统唯一撤权动作，之后该单不再接受任何写入）。
 *   报告、交回、领取、已读是执行体的动作，页面上不出现——人不替执行体记账。
 * - 扇出只补视野不给授权：订阅与上下文条目如实展示（条数与游标来自服务端），
 *   这里没有任何入口能把「看到的消息」变成授权。
 *
 * 数据通道：
 * - 桌面端：`api.collab.ticket*` → Rust 桥接（collab/tickets.rs）→ 账号服务；
 *   服务端错误码由 `errorTextOf` 换成「为什么被拒、下一步做什么」，原文留在括号里。
 * - 浏览器 / Mock：Rust 桥不存在，collab-local 对 `collab:` 前缀泛化拒绝
 *   （COLLAB_UNSUPPORTED），这里如实显示拒绝原因，不编造一份看起来像工单的清单。
 * - 列表是跨项目视角；项目事件订阅（D-06 watch）是按项目的通道，这一页不开第二条订阅，
 *   刷新靠手动与动作完成后的回读。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Typography,
  Button,
  Switch,
  FormControlLabel,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  CircularProgress,
} from '@mui/material';
import InboxIcon from '@mui/icons-material/Inbox';
import RefreshIcon from '@mui/icons-material/Refresh';
import StopCircleIcon from '@mui/icons-material/StopCircle';
import EditNoteIcon from '@mui/icons-material/EditNote';
import SendIcon from '@mui/icons-material/Send';
import CheckCircleOutlineIcon from '@mui/icons-material/CheckCircleOutline';
import UndoIcon from '@mui/icons-material/Undo';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import CloseIcon from '@mui/icons-material/Close';
import { errorTextOf } from '../../../shared/error-messages';
import type {
  AgentAttention,
  AgentTicketDetailResp,
  AgentTicketListItem,
  AgentTicketStage,
} from '../../../shared/contracts';
import { api } from '../../lib/api';
import { formatLocalDateTime } from '../../lib/schedule-local';
import { useAppStore } from '../../stores/useAppStore';

const chipSx = {
  display: 'inline-flex',
  alignItems: 'center',
  gap: 0.5,
  px: 1,
  py: 0.35,
  borderRadius: '8px',
  background: 'var(--tt-chip-neutral)',
  fontSize: '11.5px',
  color: 'var(--tt-text-2)',
} as const;

/** stage → 色调。文案用服务端的 stageLabel，这里只决定颜色归属 */
const STAGE_TONE: Record<AgentTicketStage, string> = {
  awaiting_answer: 'rgba(217, 119, 6, 0.10)',
  awaiting_review: 'rgba(5, 150, 105, 0.10)',
  blocked: 'rgba(220, 38, 38, 0.10)',
  working: 'rgba(59, 130, 246, 0.10)',
  queued: 'var(--tt-chip-neutral)',
  idle: 'var(--tt-chip-neutral)',
  input_undelivered: 'rgba(217, 119, 6, 0.10)',
  closed: 'var(--tt-chip-neutral)',
};
const STAGE_TEXT_TONE: Record<AgentTicketStage, string> = {
  awaiting_answer: 'var(--tt-warning)',
  awaiting_review: 'var(--tt-success)',
  blocked: 'var(--tt-danger)',
  working: 'var(--tt-info)',
  queued: 'var(--tt-text-2)',
  idle: 'var(--tt-text-2)',
  input_undelivered: 'var(--tt-warning)',
  closed: 'var(--tt-text-4)',
};

const StageChip: React.FC<{ stage: AgentTicketStage; label: string }> = ({ stage, label }) => (
  <Box component="span" sx={{ ...chipSx, background: STAGE_TONE[stage], color: STAGE_TEXT_TONE[stage] }}>
    {label || stage}
  </Box>
);

const FieldRow: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <Box sx={{ mt: 0.75 }}>
    <Typography component="span" sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mr: 0.75 }}>
      {label}
    </Typography>
    <Typography component="span" sx={{ fontSize: '12px', color: 'var(--tt-text-2)', wordBreak: 'break-word' }}>
      {value}
    </Typography>
  </Box>
);

/** 空态：说清楚「为什么是空的」和「下一步做什么」，不留一句「暂无数据」 */
const EmptyBox: React.FC<{ title: string; lines: string[] }> = ({ title, lines }) => (
  <Box sx={{ border: '1px dashed var(--tt-border)', borderRadius: '14px', p: 4 }}>
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
      <InfoOutlinedIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />
      <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: 'var(--tt-text-2)' }}>{title}</Typography>
    </Box>
    {lines.map((line) => (
      <Typography key={line} sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.75, lineHeight: 1.7 }}>
        {line}
      </Typography>
    ))}
  </Box>
);

const AgentTicketsPage: React.FC = () => {
  const showNotification = useAppStore((s) => s.showNotification);

  const [needsHumanOnly, setNeedsHumanOnly] = useState(true);
  const [tickets, setTickets] = useState<AgentTicketListItem[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AgentTicketDetailResp | null>(null);
  const [listLoading, setListLoading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  /** 通道读不到工单时的中文原因（浏览器/Mock 如实显示，不静默空列表） */
  const [unavailable, setUnavailable] = useState<string | null>(null);
  /** 每个开放问题独立的作答草稿（answer 自由文本；review 的验收注记） */
  const [drafts, setDrafts] = useState<Record<string, string>>({});

  const [stopOpen, setStopOpen] = useState(false);
  const [stopReason, setStopReason] = useState('');
  const [amendOpen, setAmendOpen] = useState(false);
  const [amendBrief, setAmendBrief] = useState('');
  const [amendScope, setAmendScope] = useState('');
  const [amendOutOfScope, setAmendOutOfScope] = useState('');
  const [amendReason, setAmendReason] = useState('');

  const loadList = useCallback(async () => {
    setListLoading(true);
    try {
      const resp = await api.collab.ticketMyTickets({ needsHuman: needsHumanOnly, limit: 100 });
      setTickets(resp.tickets);
      setUnavailable(null);
    } catch (err: unknown) {
      setUnavailable(errorTextOf(err, '工单列表读取失败'));
    } finally {
      setListLoading(false);
    }
    // 这里不清选中项：动作做完工单退出「只列等你的」视角是常态（stage 不再等人），
    // 详情要留在右栏让人看到结果本身，不被列表视角的反向过滤把版面拽回空态。
  }, [needsHumanOnly]);

  const loadDetail = useCallback(async (ticket: AgentTicketListItem | AgentTicketViewLike) => {
    setDetailLoading(true);
    try {
      const resp = await api.collab.ticketDetail({ projectId: ticket.projectId, ticketId: ticket.id });
      setDetail(resp);
      setDrafts({});
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '工单详情读取失败'), 'error');
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }, [showNotification]);

  useEffect(() => {
    void loadList();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [needsHumanOnly]);

  const selectTicket = (ticket: AgentTicketListItem) => {
    setSelectedId(ticket.id);
    void loadDetail(ticket);
  };

  // 动作后回读：列表按当前视角重拉；详情用已持有的工单引用回读——
  // 工单可能因为这次动作退出当前视角（比如答完就不再「等你」了），
  // 但服务端仍是唯一口径，详情里的 stageLabel 会如实说明它现在卡在哪。
  const refreshAfterAction = async () => {
    const ticket = detail?.ticket ?? null;
    await loadList();
    if (ticket) await loadDetail(ticket);
  };

  const openAmend = () => {
    if (!detail?.mandate) return;
    setAmendBrief(detail.mandate.brief);
    setAmendScope(detail.mandate.scope);
    setAmendOutOfScope(detail.mandate.outOfScope);
    setAmendReason('');
    setAmendOpen(true);
  };

  const handleAnswer = async (attention: AgentAttention) => {
    const text = (drafts[attention.id] ?? '').trim();
    if (!text) {
      showNotification('回答不能是空的——空回答会让工单原地回到「等你回答」', 'info');
      return;
    }
    if (!detail) return;
    setBusy(true);
    try {
      const resp = await api.collab.ticketAnswer({
        projectId: detail.ticket.projectId,
        ticketId: detail.ticket.id,
        attentionId: attention.id,
        answer: text,
      });
      showNotification(`已回答；工单现在是「${resp.ticket.stageLabel}」`, 'success');
      await refreshAfterAction();
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '回答提交失败'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleAccept = async (accept: boolean) => {
    if (!detail) return;
    const attention = openReview(detail.attentions);
    const note = ((attention ? drafts[attention.id] : '') ?? '').trim();
    setBusy(true);
    try {
      const resp = await api.collab.ticketAccept({
        projectId: detail.ticket.projectId,
        ticketId: detail.ticket.id,
        accept,
        note: note || undefined,
      });
      showNotification(
        accept
          ? `已验收，工单收口（${resp.ticket.stageLabel}）`
          : `已退回重做，工单回到「${resp.ticket.stageLabel}」；授权没有变`,
        'success',
      );
      await refreshAfterAction();
    } catch (err: unknown) {
      showNotification(errorTextOf(err, accept ? '验收提交失败' : '退回提交失败'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleStop = async () => {
    if (!detail) return;
    setBusy(true);
    try {
      const reason = stopReason.trim();
      const resp = await api.collab.ticketStop({
        projectId: detail.ticket.projectId,
        ticketId: detail.ticket.id,
        reason: reason || undefined,
      });
      setStopOpen(false);
      setStopReason('');
      showNotification(`已叫停：授权吊销、工单收口（${resp.ticket.stageLabel}），此后该单不再接受任何写入`, 'success');
      await refreshAfterAction();
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '叫停失败'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const handleAmend = async () => {
    if (!detail) return;
    const brief = amendBrief.trim();
    if (!brief) {
      showNotification('新范围的交代（brief）不能为空', 'info');
      return;
    }
    setBusy(true);
    try {
      const resp = await api.collab.ticketAmend({
        projectId: detail.ticket.projectId,
        ticketId: detail.ticket.id,
        brief,
        scope: amendScope.trim() || undefined,
        outOfScope: amendOutOfScope.trim() || undefined,
        reason: amendReason.trim() || undefined,
      });
      setAmendOpen(false);
      showNotification(
        `授权已升版（第 ${resp.mandate.seq} 份生效，${resp.mandate.intensityLabel}）；活着的执行体已结束，工单回到「${resp.ticket.stageLabel}」`,
        'success',
      );
      await refreshAfterAction();
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '授权修订失败'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const selected = useMemo(() => tickets.find((t) => t.id === selectedId) ?? null, [tickets, selectedId]);
  const view = detail?.ticket ?? null;
  const openAnswers = useMemo(
    () => (detail ? detail.attentions.filter((a) => a.status === 'open' && a.kind === 'answer') : []),
    [detail],
  );
  const reviewAttention = detail ? openReview(detail.attentions) : null;
  const closed = view?.stage === 'closed';

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', display: 'flex', flexDirection: 'column' }}>
      {/* 头部：标题 + 只看等你处理 + 刷新 */}
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.25, pb: 1 }}>
        <InboxIcon sx={{ fontSize: 22, color: 'var(--tt-teal)', mt: 0.5 }} />
        <Box sx={{ minWidth: 0, flexGrow: 1 }}>
          <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)' }}>等你的工单</Typography>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-4)', mt: 0.5 }}>
            「等你」由服务端按结构化字段判定（等你回答 / 等你验收 / 等人处理）；叫停是全系统唯一撤权动作，改范围走授权改版。
          </Typography>
        </Box>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexShrink: 0 }}>
          <FormControlLabel
            control={
              <Switch
                size="small"
                checked={needsHumanOnly}
                onChange={(event) => setNeedsHumanOnly(event.target.checked)}
                inputProps={{ 'aria-label': '只列需要人处理的工单' }}
              />
            }
            label={
              <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)' }}>
                只列等你的{!needsHumanOnly ? '（已关：显示全部阶段）' : ''}
              </Typography>
            }
            sx={{ m: 0 }}
          />
          <Button
            variant="outlined"
            size="small"
            startIcon={<RefreshIcon sx={{ fontSize: 15 }} />}
            onClick={() => void loadList()}
            disabled={listLoading}
            sx={{
              color: 'var(--tt-text-2)',
              borderColor: 'var(--tt-border)',
              borderRadius: '10px',
              textTransform: 'none',
              fontSize: '12.5px',
            }}
          >
            刷新
          </Button>
        </Box>
      </Box>

      {unavailable && (
        <Box
          sx={{
            mb: 1.5,
            px: 2,
            py: 1,
            borderRadius: '12px',
            background: 'rgba(217, 119, 6, 0.08)',
            border: '1px solid rgba(217, 119, 6, 0.2)',
            display: 'flex',
            alignItems: 'flex-start',
            gap: 1,
          }}
        >
          <WarningAmberIcon sx={{ fontSize: 16, color: 'var(--tt-warning)', mt: 0.25 }} />
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-warning)' }}>{unavailable}</Typography>
        </Box>
      )}

      <Box sx={{ flex: 1, minHeight: 0, display: 'flex', gap: 1.5 }}>
        {/* 左列：列表（跨项目，按更新时间倒序——排序也在服务端） */}
        <Box
          sx={{
            width: { xs: '100%', md: 380 },
            flexShrink: 0,
            overflowY: 'auto',
            pr: { md: 0.5 },
            display: { xs: selected ? 'none' : 'flex', md: 'flex' },
            flexDirection: 'column',
            gap: 1,
          }}
        >
          {tickets.length === 0 && !listLoading && (
            <EmptyBox
              title={needsHumanOnly ? '没有等你处理的工单' : '账本里还没有工单'}
              lines={
                unavailable
                  ? ['工单读取通道不可用，原因见上方提示；这里不编造一份看起来像工单的清单。']
                  : needsHumanOnly
                    ? [
                        '三种「等人」阶段（等你回答 / 等你验收 / 等人处理）都没有开放项——健康态不占版面。',
                        '关掉「只列等你的」可以看到 working / queued / closed 的全部工单。',
                      ]
                    : [
                        '工单由研判或人在项目里发起后才会存在；这一页只负责把卡住的事递到人面前。',
                        '想要「本可接单」的环境事件，去看「主动候选」页（影子模式不建单、不执行）。',
                      ]
              }
            />
          )}
          {tickets.map((ticket) => {
            const active = ticket.id === selectedId;
            return (
              <Box
                key={ticket.id}
                role="button"
                tabIndex={0}
                onClick={() => selectTicket(ticket)}
                onKeyDown={(event) => {
                  if (event.key === 'Enter' || event.key === ' ') {
                    event.preventDefault();
                    selectTicket(ticket);
                  }
                }}
                sx={{
                  p: 1.25,
                  borderRadius: '12px',
                  border: active ? '1px solid var(--tt-teal)' : '1px solid var(--tt-border-soft)',
                  background: active ? 'rgba(5, 150, 105, 0.06)' : 'var(--tt-surface)',
                  cursor: 'pointer',
                  '&:hover': { borderColor: 'var(--tt-teal)' },
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, mb: 0.5 }}>
                  <StageChip stage={ticket.stage} label={ticket.stageLabel} />
                  <Box component="span" sx={chipSx}>{ticket.projectName}</Box>
                  <Box sx={{ flexGrow: 1 }} />
                  {listLoading && <CircularProgress size={12} />}
                </Box>
                <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }} noWrap>
                  {ticket.title}
                </Typography>
                {ticket.attention && (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', mt: 0.5, lineHeight: 1.6 }}>
                    {ticket.attention.kindLabel}：{ticket.attention.question}
                  </Typography>
                )}
                {ticket.mandateBrief && (
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }} noWrap>
                    授权 v{ticket.mandateSeq}：{ticket.mandateBrief}
                  </Typography>
                )}
                <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                  更新于 {formatLocalDateTime(ticket.updatedAtMs)} · {ticket.createdByNickname || ticket.createdBy}
                  {ticket.createdBy === 'agent' ? '（研判发起）' : ''}
                </Typography>
              </Box>
            );
          })}
        </Box>

        {/* 右列：详情与动作 */}
        <Box sx={{ flex: 1, minWidth: 0, overflowY: 'auto', pr: 1 }}>
          {!selected && !detail && (
            <EmptyBox
              title="选一条工单看它卡在哪"
              lines={[
                '左边点一条，这里会给出授权范围、执行体绑定、六态报告与要你做的那个动作。',
                '判定与拒绝都在服务端：这一页只是如实展示与提交动作，不替你判断该不该动。',
              ]}
            />
          )}
          {selected && !view && (
            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, p: 2 }}>
              <CircularProgress size={16} />
              <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-4)' }}>
                {detailLoading ? '读取工单详情…' : '详情还没读到，可点上方「刷新」重试'}
              </Typography>
            </Box>
          )}
          {view && detail && (
            <Box>
              {/* 概要 */}
              <Box
                sx={{
                  p: 1.5,
                  borderRadius: '14px',
                  border: '1px solid var(--tt-border)',
                  background: 'var(--tt-surface)',
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                  <StageChip stage={view.stage} label={view.stageLabel} />
                  <Box component="span" sx={chipSx}>{view.projectName}</Box>
                  {detail.mandate && (
                    <Box component="span" sx={chipSx}>授权 v{detail.mandate.seq} · {detail.mandate.intensityLabel}</Box>
                  )}
                  <Box component="span" sx={chipSx}>第 {view.version} 次改动</Box>
                  <Box sx={{ flexGrow: 1 }} />
                  <Button
                    size="small"
                    startIcon={<EditNoteIcon sx={{ fontSize: 15 }} />}
                    onClick={openAmend}
                    disabled={closed || busy || !detail.mandate}
                    sx={{ textTransform: 'none', fontSize: '12.5px', color: 'var(--tt-text-2)' }}
                  >
                    扩大范围
                  </Button>
                  <Button
                    size="small"
                    startIcon={<StopCircleIcon sx={{ fontSize: 15 }} />}
                    onClick={() => setStopOpen(true)}
                    disabled={closed || busy}
                    sx={{ textTransform: 'none', fontSize: '12.5px', color: 'var(--tt-danger)' }}
                  >
                    叫停
                  </Button>
                </Box>
                <Typography sx={{ fontSize: '15px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 0.75 }}>
                  {view.title}
                </Typography>
                {view.detail && (
                  <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', mt: 0.5, lineHeight: 1.7 }}>
                    {view.detail}
                  </Typography>
                )}
                {closed && (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.75 }}>
                    收口原因：{view.closedReason || '（未填写）'} · 收口于 {formatLocalDateTime(view.closedAtMs)}
                    。此后一切写入（报告、交回、回答、改授权、再领取）都会被服务端拒掉。
                  </Typography>
                )}
                {!view.needsHuman && !closed && (
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-info)', mt: 0.75 }}>
                    这条现在不需要人动手（{view.stageLabel}）；列出来是因为你关掉了「只列等你的」。
                  </Typography>
                )}
              </Box>

              {/* 等你的动作区：问题与答案只围绕 kindLabel 展示，判定口径在服务端 */}
              {(openAnswers.length > 0 || reviewAttention) && (
                <Box
                  sx={{
                    mt: 1.25,
                    p: 1.5,
                    borderRadius: '14px',
                    border: '1px solid rgba(217, 119, 6, 0.25)',
                    background: 'rgba(217, 119, 6, 0.05)',
                  }}
                >
                  <Typography sx={{ fontSize: '13px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                    现在等人的是
                  </Typography>
                  {openAnswers.map((attention) => (
                    <Box key={attention.id} sx={{ mt: 1 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                        <Box component="span" sx={{ ...chipSx, background: 'rgba(217, 119, 6, 0.12)', color: 'var(--tt-warning)' }}>
                          {attention.kindLabel}
                        </Box>
                        <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                          提出于 {formatLocalDateTime(attention.createdAtMs)}
                        </Typography>
                      </Box>
                      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-1)', mt: 0.5, lineHeight: 1.7 }}>
                        {attention.question}
                      </Typography>
                      {attention.reason && (
                        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                          为什么等人：{attention.reason}
                        </Typography>
                      )}
                      {attention.options.length > 0 && (
                        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.75, mt: 0.75 }}>
                          {attention.options.map((option) => (
                            <Box
                              key={option}
                              role="button"
                              tabIndex={0}
                              onClick={() => setDrafts((d) => ({ ...d, [attention.id]: option }))}
                              onKeyDown={(event) => {
                                if (event.key === 'Enter') setDrafts((d) => ({ ...d, [attention.id]: option }));
                              }}
                              sx={{
                                ...chipSx,
                                cursor: 'pointer',
                                border: '1px dashed var(--tt-border)',
                                '&:hover': { borderColor: 'var(--tt-teal)', color: 'var(--tt-teal)' },
                              }}
                            >
                              {option}
                            </Box>
                          ))}
                        </Box>
                      )}
                      <Box sx={{ display: 'flex', gap: 1, mt: 1, alignItems: 'flex-start' }}>
                        <TextField
                          size="small"
                          fullWidth
                          multiline
                          minRows={2}
                          placeholder="回答这句提问（只有这一条路：答完工单回到执行）"
                          value={drafts[attention.id] ?? ''}
                          onChange={(event) => setDrafts((d) => ({ ...d, [attention.id]: event.target.value }))}
                          inputProps={{ 'aria-label': '回答内容' }}
                          sx={{ '& .MuiOutlinedInput-input': { fontSize: '12.5px' } }}
                        />
                        <Button
                          variant="contained"
                          size="small"
                          startIcon={<SendIcon sx={{ fontSize: 15 }} />}
                          onClick={() => void handleAnswer(attention)}
                          disabled={busy}
                          sx={{ textTransform: 'none', borderRadius: '10px', fontSize: '12.5px', mt: 0.25 }}
                        >
                          提交回答
                        </Button>
                      </Box>
                    </Box>
                  ))}
                  {reviewAttention && (
                    <Box sx={{ mt: 1 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
                        <Box component="span" sx={{ ...chipSx, background: 'rgba(5, 150, 105, 0.12)', color: 'var(--tt-success)' }}>
                          {reviewAttention.kindLabel}
                        </Box>
                        <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                          提出于 {formatLocalDateTime(reviewAttention.createdAtMs)}
                        </Typography>
                      </Box>
                      <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-1)', mt: 0.5, lineHeight: 1.7 }}>
                        {reviewAttention.question}
                      </Typography>
                      {reviewAttention.reason && (
                        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                          为什么等你验收：{reviewAttention.reason}
                        </Typography>
                      )}
                      <Box sx={{ display: 'flex', gap: 1, mt: 1, alignItems: 'flex-start' }}>
                        <TextField
                          size="small"
                          fullWidth
                          multiline
                          minRows={2}
                          placeholder="验收注记（可选；退回时建议写清楚差在哪，执行体按原授权继续）"
                          value={drafts[reviewAttention.id] ?? ''}
                          onChange={(event) => setDrafts((d) => ({ ...d, [reviewAttention.id]: event.target.value }))}
                          inputProps={{ 'aria-label': '验收注记' }}
                          sx={{ '& .MuiOutlinedInput-input': { fontSize: '12.5px' } }}
                        />
                      </Box>
                      <Box sx={{ display: 'flex', gap: 1, mt: 1 }}>
                        <Button
                          variant="contained"
                          size="small"
                          startIcon={<CheckCircleOutlineIcon sx={{ fontSize: 15 }} />}
                          onClick={() => void handleAccept(true)}
                          disabled={busy}
                          sx={{ textTransform: 'none', borderRadius: '10px', fontSize: '12.5px' }}
                        >
                          通过验收（收口）
                        </Button>
                        <Button
                          variant="outlined"
                          size="small"
                          startIcon={<UndoIcon sx={{ fontSize: 15 }} />}
                          onClick={() => void handleAccept(false)}
                          disabled={busy}
                          sx={{
                            textTransform: 'none',
                            borderRadius: '10px',
                            fontSize: '12.5px',
                            color: 'var(--tt-text-2)',
                            borderColor: 'var(--tt-border)',
                          }}
                        >
                          退回重做（授权不变）
                        </Button>
                      </Box>
                    </Box>
                  )}
                </Box>
              )}

              {/* 当前授权 */}
              <SectionBox title="当前生效的授权">
                {detail.mandate ? (
                  <Box>
                    <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-1)', lineHeight: 1.7 }}>
                      {detail.mandate.brief}
                    </Typography>
                    <FieldRow label="范围内" value={detail.mandate.scope || '（未写：按交代原文理解）'} />
                    <FieldRow label="范围外" value={detail.mandate.outOfScope || '（未列）'} />
                    <FieldRow
                      label="依据"
                      value={`${detail.mandate.intensityLabel} · ${detail.mandate.sourceKind}${
                        detail.mandate.sourceRef ? ` · ${detail.mandate.sourceRef}` : ''
                      } · 策略 v${detail.mandate.policyVersion}`}
                    />
                    <FieldRow label="状态" value={`${detail.mandate.statusLabel} · 第 ${detail.mandate.seq} 版 · ${formatLocalDateTime(detail.mandate.createdAtMs)}`} />
                    {detail.mandate.status === 'revoked' && (
                      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-danger)', mt: 0.75 }}>
                        吊销于 {formatLocalDateTime(detail.mandate.revokedAtMs)}：{detail.mandate.revokeReason || '（未填写）'}
                        ——只有人的停止能撤权。
                      </Typography>
                    )}
                  </Box>
                ) : (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>
                    这份工单当前没有生效授权（被吊销了）；要开工必须由人重新交代范围。
                  </Typography>
                )}
              </SectionBox>

              {/* 执行体绑定 */}
              <SectionBox title={`执行体绑定（${detail.assignments.length} 次）`}>
                {detail.assignments.length === 0 && (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>还没有执行体领取过这单。</Typography>
                )}
                {detail.assignments
                  .slice()
                  .reverse()
                  .map((assignment) => {
                    const lastRead = assignment.readSteps.reduce((max, step) => Math.max(max, step.seq), 0);
                    return (
                      <Box key={assignment.id} sx={{ py: 0.75, borderTop: '1px solid var(--tt-border-soft)' }}>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                          <Box
                            component="span"
                            sx={{
                              ...chipSx,
                              background:
                                assignment.status === 'live' ? 'rgba(59, 130, 246, 0.10)' : 'var(--tt-chip-neutral)',
                              color: assignment.status === 'live' ? 'var(--tt-info)' : 'var(--tt-text-4)',
                            }}
                          >
                            {assignment.status === 'live' ? '在跑' : '已结束'}
                          </Box>
                          <Typography component="span" sx={{ fontSize: '12px', color: 'var(--tt-text-2)' }}>
                            {assignment.workerId || assignment.id}
                          </Typography>
                          {assignment.machine && (
                            <Typography component="span" sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                              @ {assignment.machine}
                            </Typography>
                          )}
                        </Box>
                        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.25 }}>
                          起 {formatLocalDateTime(assignment.startedAtMs)}
                          {assignment.endedAtMs ? ` · 止 ${formatLocalDateTime(assignment.endedAtMs)}` : ''}
                          {assignment.exitReason ? ` · ${assignment.exitReason}` : ''}
                        </Typography>
                        {assignment.readSteps.length > 0 && (
                          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.25 }}>
                            已读落账 {assignment.readSteps.length} 步，读到第 {lastRead} 号事件{assignment.recoveryNote ? ` · 恢复注记：${assignment.recoveryNote}` : ''}
                          </Typography>
                        )}
                      </Box>
                    );
                  })}
              </SectionBox>

              {/* 六态报告 */}
              <SectionBox title={`执行报告（${detail.reports.length} 份）`}>
                {detail.reports.length === 0 && (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>还没有收到任何报告。</Typography>
                )}
                {detail.reports
                  .slice()
                  .reverse()
                  .map((report) => (
                    <Box key={report.id} sx={{ py: 0.75, borderTop: '1px solid var(--tt-border-soft)' }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                        <Box component="span" sx={chipSx}>{report.kindLabel}</Box>
                        <Typography component="span" sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                          {formatLocalDateTime(report.createdAtMs)}
                        </Typography>
                        {report.handedBack && (
                          <Box component="span" sx={{ ...chipSx, background: 'rgba(217, 119, 6, 0.10)', color: 'var(--tt-warning)' }}>
                            超范围交回
                          </Box>
                        )}
                      </Box>
                      <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-1)', mt: 0.25, lineHeight: 1.6 }}>
                        {report.summary}
                      </Typography>
                      {report.detail && (
                        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.25, lineHeight: 1.6 }}>
                          {report.detail}
                        </Typography>
                      )}
                      {report.artifacts.length > 0 && (
                        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mt: 0.5 }}>
                          {report.artifacts.map((name) => (
                            <Box key={name} component="span" sx={{ ...chipSx, fontFamily: 'ui-monospace, monospace' }}>
                              {name}
                            </Box>
                          ))}
                        </Box>
                      )}
                    </Box>
                  ))}
              </SectionBox>

              {/* 订阅与扇出：只补视野，不给授权 */}
              <SectionBox title={`订阅与扇出上下文（${detail.subscriptions.length} 个订阅 / ${detail.contexts.length} 条已扇出）`}>
                <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.25 }}>
                  订阅带来的消息只补执行体的视野：绕过准入、不产生新授权、不改 stage；要扩大范围请走上方「扩大范围」。
                </Typography>
                {detail.subscriptions.map((subscription) => (
                  <FieldRow
                    key={subscription.id}
                    label={`${subscription.targetKind} · ${subscription.targetRef}`}
                    value={subscription.closedAtMs ? `已关闭 ${formatLocalDateTime(subscription.closedAtMs)}` : `已扇出到第 ${subscription.lastFannedSeq} 号`}
                  />
                ))}
              </SectionBox>
            </Box>
          )}
        </Box>
      </Box>

      {/* 叫停确认：全系统唯一撤权动作 */}
      <Dialog open={stopOpen} onClose={() => setStopOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>叫停这张工单？</DialogTitle>
        <DialogContent sx={{ pt: '4px !important' }}>
          <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', lineHeight: 1.7 }}>
            停止是唯一的撤权动作：授权吊销、工单收口、活着的执行体结束、开放问题撤回；此后该工单不再接受任何写入
            （报告、交回、回答、改授权、再领取都会被服务端拒绝）。这是这张工单的最后一次写入。
          </Typography>
          <TextField
            size="small"
            fullWidth
            multiline
            minRows={2}
            margin="normal"
            placeholder="停止理由（可选，会记进事件）"
            value={stopReason}
            onChange={(event) => setStopReason(event.target.value)}
            inputProps={{ 'aria-label': '停止理由' }}
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setStopOpen(false)} sx={{ color: 'var(--tt-text-2)', fontSize: '13px' }}>
            取消
          </Button>
          <Button onClick={() => void handleStop()} disabled={busy} sx={{ color: 'var(--tt-danger)', fontSize: '13px', fontWeight: 600 }}>
            确认叫停
          </Button>
        </DialogActions>
      </Dialog>

      {/* 扩大范围：授权追加式改版 */}
      <Dialog open={amendOpen} onClose={() => setAmendOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>修订授权（追加新版，旧版留痕）</DialogTitle>
        <DialogContent sx={{ pt: '4px !important', display: 'flex', flexDirection: 'column', gap: 1.25 }}>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', lineHeight: 1.7 }}>
            改范围不是改旧单：旧授权保留、新授权生效，正在跑的执行体会被结束并退回排队——范围变了，它跑的上下文已经不能沿用。
          </Typography>
          <TextField
            size="small"
            fullWidth
            multiline
            minRows={2}
            label="新的交代（brief，必填）"
            value={amendBrief}
            onChange={(event) => setAmendBrief(event.target.value)}
            inputProps={{ 'aria-label': '新授权 brief' }}
          />
          <TextField
            size="small"
            fullWidth
            label="范围内（scope）"
            value={amendScope}
            onChange={(event) => setAmendScope(event.target.value)}
            inputProps={{ 'aria-label': '范围内' }}
          />
          <TextField
            size="small"
            fullWidth
            label="范围外（out of scope）"
            value={amendOutOfScope}
            onChange={(event) => setAmendOutOfScope(event.target.value)}
            inputProps={{ 'aria-label': '范围外' }}
          />
          <TextField
            size="small"
            fullWidth
            label="改动理由（可选，进事件流）"
            value={amendReason}
            onChange={(event) => setAmendReason(event.target.value)}
            inputProps={{ 'aria-label': '修订理由' }}
          />
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setAmendOpen(false)} sx={{ color: 'var(--tt-text-2)', fontSize: '13px' }}>
            取消
          </Button>
          <Button
            onClick={() => void handleAmend()}
            disabled={busy || !amendBrief.trim()}
            sx={{ color: 'var(--tt-teal)', fontSize: '13px', fontWeight: 600 }}
          >
            发布新版授权
          </Button>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

/** 详情分区外壳 */
const SectionBox: React.FC<{ title: string; children: React.ReactNode }> = ({ title, children }) => (
  <Box
    sx={{
      mt: 1.25,
      p: 1.5,
      borderRadius: '14px',
      border: '1px solid var(--tt-border)',
      background: 'var(--tt-surface)',
    }}
  >
    <Typography sx={{ fontSize: '13px', fontWeight: 700, color: 'var(--tt-text-1)' }}>{title}</Typography>
    <Box sx={{ mt: 0.5 }}>{children}</Box>
  </Box>
);

/** 开放中的验收问题（review 类，最多算一条：服务端保证语义） */
function openReview(attentions: AgentAttention[]): AgentAttention | null {
  return attentions.find((a) => a.status === 'open' && a.kind === 'review') ?? null;
}

/** loadDetail 只需要 id 与 projectId 两个字段 */
interface AgentTicketViewLike {
  id: string;
  projectId: string;
}

export default AgentTicketsPage;
