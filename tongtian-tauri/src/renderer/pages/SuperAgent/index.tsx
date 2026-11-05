/**
 * 通天办公 - 主动候选（缺陷 D-46 · 影子模式）
 *
 * 这一页回答的问题是：**在没有谁交代的情况下，Agent 有没有依据动手，以及为什么没动。**
 * 它是 D-46 影子段唯一的产出界面——四件事各自独立，界面上必须能分开看清：
 *
 * - **事件入口**：登记了哪些环境事件源、有没有在读取（游标走到哪、上次失败原因）。
 *   登记 ≠ 在读取 ≠ 已授权，三件事各自都要显式做一次，界面上不合并成一个开关。
 * - **主动候选**：判定为「本可接单」的事件。这里**没有「采纳并开工」按钮**：影子模式
 *   只记账与研判，任何执行都要人另外在会话里正常发起（D-47 工单层与 D-48 执行体隔离
 *   落地前，Rust 侧连 `auto` 档都拒绝写入）。页尾的「导出 JSON / CSV」把这份清单落成文件，
 *   文件里带着档位、策略版本与「没有一条被执行」的声明——导出的还是清单，不是工单。
 *   候选行下方另有一层「模型研判」：问模型「这条值不值得现在开工」，结论同样只是记录，
 *   它也是这一页唯一会花模型钱的动作，所以预算在策略里、闸门在 Rust 侧，界面只能点一次。
 * - **准入策略**：谁能通过哪个入口开工。`auto` 在这里显示为不可选并写明缺哪一段，
 *   而不是留一个按下去只会报错的开关。
 * - **判定留痕**：包含阴性记录——「为什么没做」和「做了什么」同样要能回看。
 *
 * 数据通道（Mock 开关规范）：
 * - Tauri 容器：全部读本机事件账本（Rust `superagent` 模块），后台巡检循环随应用存活，
 *   页面订阅 `superagent-event` 只在有事时刷新
 * - 浏览器直连引擎 / 开发 Mock：`lib/superagent-local.ts` 如实拒绝
 *   （`SUPERAGENT_DESKTOP_ONLY`），这里显示拒绝原因，不编造一份看起来像账本的清单
 * - 所有显示名（入口类型、强度、档位、巡检状态）都由 Rust 给，与判定理由同源；
 *   前端不自己翻译，避免出现第二套口径
 * - 数字（今日研判条数、事件数、候选数）一律来自台账概览，前端不自己数
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Typography,
  Button,
  Switch,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  TextField,
  Select,
  MenuItem,
  IconButton,
} from '@mui/material';
import RadarIcon from '@mui/icons-material/Radar';
import RefreshIcon from '@mui/icons-material/Refresh';
import PsychologyIcon from '@mui/icons-material/Psychology';
import DownloadIcon from '@mui/icons-material/Download';
import AddOutlinedIcon from '@mui/icons-material/AddOutlined';
import InfoOutlinedIcon from '@mui/icons-material/InfoOutlined';
import WarningAmberIcon from '@mui/icons-material/WarningAmber';
import CloseIcon from '@mui/icons-material/Close';
import { errorTextOf } from '../../../shared/error-messages';
import type {
  SuperagentAssessSummary,
  SuperagentCandidate,
  SuperagentDecision,
  SuperagentExportResult,
  SuperagentPolicyDoc,
  SuperagentPolicyVersion,
  SuperagentPolicyView,
  SuperagentPumpSummary,
  SuperagentRule,
  SuperagentSource,
  SuperagentSourceKind,
  SuperagentStats,
  SuperagentTrigger,
} from '../../../shared/contracts';
import { superagentAPI } from '../../lib/api';
import { formatLocalDateTime } from '../../lib/schedule-local';
import { useAppStore } from '../../stores/useAppStore';

type TabKey = 'candidates' | 'sources' | 'policy' | 'decisions';

const TABS: Array<[TabKey, string]> = [
  ['candidates', '主动候选'],
  ['sources', '事件入口'],
  ['policy', '准入策略'],
  ['decisions', '判定留痕'],
];

/** 逗号分隔的名单 ↔ 数组（策略里的名单就是字符串数组，界面上不换行更好填） */
const toList = (text: string): string[] =>
  text
    .split(',')
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
const fromList = (list: string[]): string => list.join(', ');

/**
 * 各入口的实例含义与配置项（与 Rust `sources.rs` 的读法一致）。
 * 只列真正需要的字段：`schedule` / `condition` 用 everyMs 或 atMs，`workspace_file` 用 dir。
 */
const INSTANCE_HINTS: Record<string, string> = {
  project_event: '填具体项目 id（不许整类放行，一个项目一条入口）',
  im_inbound: '填具体会话 id（本段只登记不读取）',
  schedule: '本机唯一入口，可留空',
  condition: '本机唯一入口，可留空',
  workspace_file: '本机唯一入口，可留空',
};

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

const SuperAgentPage: React.FC = () => {
  const showNotification = useAppStore((s) => s.showNotification);

  const [tab, setTab] = useState<TabKey>('candidates');
  const [stats, setStats] = useState<SuperagentStats | null>(null);
  const [sources, setSources] = useState<SuperagentSource[]>([]);
  const [candidates, setCandidates] = useState<SuperagentCandidate[]>([]);
  const [decisions, setDecisions] = useState<SuperagentDecision[]>([]);
  const [view, setView] = useState<SuperagentPolicyView | null>(null);
  const [versions, setVersions] = useState<SuperagentPolicyVersion[]>([]);
  const [draft, setDraft] = useState<SuperagentPolicyDoc | null>(null);
  const [pump, setPump] = useState<SuperagentPumpSummary | null>(null);
  /** 最近一次导出的落点：路径、条数、影子模式声明都由 Rust 给，界面只转述 */
  const [lastExport, setLastExport] = useState<SuperagentExportResult | null>(null);
  const [exporting, setExporting] = useState(false);
  /** 最近一次「立即研判」的摘要：数字与跳过原因都由 Rust 给，界面不自己数 */
  const [lastAssess, setLastAssess] = useState<SuperagentAssessSummary | null>(null);
  const [assessing, setAssessing] = useState(false);
  /** 当前通道读不到账本时的中文原因（浏览器/Mock 如实显示，不静默空列表） */
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [detail, setDetail] = useState<{ triggerId: string; row: SuperagentTrigger | null; error: string | null } | null>(
    null,
  );

  // 登记入口弹窗：类型 / 实例 / 备注 + 该类型需要的配置项
  const [registerOpen, setRegisterOpen] = useState(false);
  const [regKind, setRegKind] = useState<SuperagentSourceKind | string>('project_event');
  const [regInstance, setRegInstance] = useState('');
  const [regLabel, setRegLabel] = useState('');
  const [regDir, setRegDir] = useState('');
  const [regEveryMinutes, setRegEveryMinutes] = useState('');
  const [regAtText, setRegAtText] = useState('');

  const load = useCallback(async () => {
    setBusy(true);
    try {
      const [nextStats, nextSources, nextCandidates, nextDecisions, nextView] = await Promise.all([
        superagentAPI.stats(),
        superagentAPI.listSources(),
        superagentAPI.listCandidates(50),
        superagentAPI.listDecisions(50),
        superagentAPI.getPolicy(),
      ]);
      setStats(nextStats);
      setSources(nextSources);
      setCandidates(nextCandidates);
      setDecisions(nextDecisions);
      setView(nextView);
      // 草稿只在没有人工编辑时跟着权威策略走，避免覆盖用户正在填的表单
      setDraft((current) => current ?? nextView.doc);
      setUnavailable(null);
    } catch (err: unknown) {
      setUnavailable(errorTextOf(err, '主动候选台账读取失败'));
    } finally {
      setBusy(false);
    }
  }, []);

  useEffect(() => {
    void load();
    // 后台巡检只在「有事发生」时投事件；投了就重新读一遍权威数据，界面不做本地推算
    const unlisten = superagentAPI.onPump((summary) => {
      setPump(summary);
      void load();
    });
    return unlisten;
  }, [load]);

  const handlePumpNow = async () => {
    setBusy(true);
    try {
      const summary = await superagentAPI.pumpNow();
      setPump(summary);
      await load();
      if (summary.skipped) {
        showNotification(summary.skippedLabel ?? '本轮巡检已跳过', 'info');
      }
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '巡检失败'), 'error');
    } finally {
      setBusy(false);
    }
  };

  /**
   * 对存量候选补一轮模型研判（第二层）。
   *
   * 这是这一页唯一会花模型钱的动作，所以要人明确点：后台循环里它跟着策略预算自己跑，
   * 「立即巡检一轮」则完全不碰模型。按钮不猜结果——结论、条数、跳过原因都从摘要里读。
   */
  const handleAssessNow = async () => {
    setAssessing(true);
    try {
      const summary = await superagentAPI.assessNow();
      setLastAssess(summary);
      await load();
      if (summary.skipped) {
        showNotification(summary.skipReason ?? '这一轮模型研判已跳过', 'info');
      } else if (summary.attempted === 0) {
        showNotification('没有待研判的候选，模型没有被打扰', 'info');
      } else {
        showNotification(
          `模型研判 ${summary.attempted} 条：值得 ${summary.worth} · 不值得 ${summary.notWorth} · 拿不准 ${summary.unsure}` +
            (summary.failed > 0 ? ` · 失败 ${summary.failed}` : '') +
            (summary.superseded > 0 ? ` · 因策略升版作废 ${summary.superseded}` : '') +
            `（${summary.note}）`,
          'success',
        );
      }
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '模型研判失败'), 'error');
    } finally {
      setAssessing(false);
    }
  };

  /**
   * 导出候选清单（验收项③「能导出候选清单与理由」）。
   * 文件由 Rust 落在本机运行时目录的 exports 下，一次一个新文件不覆盖；
   * 界面只转述路径与条数，不自己拼路径、也不把清单内容搬到浏览器下载。
   */
  const handleExport = async (format: 'json' | 'csv') => {
    setExporting(true);
    try {
      const result = await superagentAPI.exportCandidates(format);
      setLastExport(result);
      showNotification(
        result.count === 0
          ? `已导出 0 条候选（${result.format.toUpperCase()}）：账本里确实没有「本可接单」的事件，文件里带着这份说明`
          : `已导出 ${result.count} 条候选（${result.format.toUpperCase()}）：${result.path}`,
        'success',
      );
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '候选清单导出失败'), 'error');
    } finally {
      setExporting(false);
    }
  };

  const handleToggleSource = async (source: SuperagentSource, enabled: boolean) => {
    try {
      const row = await superagentAPI.setSourceEnabled(source.id, enabled);
      setSources((list) => list.map((item) => (item.id === row.id ? row : item)));
      await load();
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '入口读取开关设置失败'), 'error');
    }
  };

  const buildConfig = (): Record<string, unknown> => {
    if (regKind === 'workspace_file') {
      const dir = regDir.trim();
      return dir ? { dir } : {};
    }
    if (regKind === 'schedule' || regKind === 'condition') {
      const minutes = Number(regEveryMinutes);
      const atMs = regAtText ? new Date(regAtText).getTime() : 0;
      if (Number.isFinite(minutes) && minutes > 0) return { everyMs: Math.round(minutes * 60_000) };
      if (Number.isFinite(atMs) && atMs > 0) return { atMs };
      return {};
    }
    return {};
  };

  const handleRegister = async () => {
    const instance = regInstance.trim();
    const config = buildConfig();
    try {
      const row = await superagentAPI.registerSource({
        kind: regKind,
        instance,
        label: regLabel.trim(),
        config,
      });
      setRegisterOpen(false);
      setRegInstance('');
      setRegLabel('');
      setRegDir('');
      setRegEveryMinutes('');
      setRegAtText('');
      setSources((list) => {
        const exists = list.some((item) => item.id === row.id);
        return exists ? list.map((item) => (item.id === row.id ? row : item)) : [...list, row];
      });
      // 登记只是第三件事的第一件：不开读取、不给授权就一条候选都不会有
      showNotification('入口已登记（新入口默认不读取）。要产生候选还需：开启读取 + 在准入策略里授权', 'info');
      await load();
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '事件入口登记失败'), 'error');
    }
  };

  const handleSavePolicy = async () => {
    if (!draft) return;
    setBusy(true);
    try {
      const outcome = await superagentAPI.savePolicy(draft);
      const nextView = await superagentAPI.getPolicy();
      setView(nextView);
      setDraft(nextView.doc);
      setVersions(await superagentAPI.listPolicyVersions(20));
      await load();
      showNotification(
        outcome.changed ? `策略已保存为第 ${outcome.version} 版` : `策略内容与第 ${outcome.version} 版一致，未新增版本`,
        'success',
      );
      // 升版的后果单独说一句：改策略不是没有代价的动作，重排了几条要让人看见（数字由 Rust 数）
      if (outcome.requeueNote) {
        showNotification(outcome.requeueNote, 'info');
      }
    } catch (err: unknown) {
      showNotification(errorTextOf(err, '准入策略保存失败'), 'error');
    } finally {
      setBusy(false);
    }
  };

  const openDetail = async (triggerId: string) => {
    setDetail({ triggerId, row: null, error: null });
    try {
      setDetail({ triggerId, row: await superagentAPI.getTrigger(triggerId), error: null });
    } catch (err: unknown) {
      setDetail({ triggerId, row: null, error: errorTextOf(err, '事件读取失败') });
    }
  };

  const patchDraft = (patch: Partial<SuperagentPolicyDoc>) => {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  };
  const patchSource = (index: number, patch: Partial<SuperagentPolicyDoc['sources'][number]>) => {
    setDraft((current) => {
      if (!current) return current;
      const list = current.sources.map((item, i) => (i === index ? { ...item, ...patch } : item));
      return { ...current, sources: list };
    });
  };
  const patchRule = (sourceIndex: number, ruleIndex: number, patch: Partial<SuperagentRule>) => {
    setDraft((current) => {
      if (!current) return current;
      const source = current.sources[sourceIndex];
      if (!source) return current;
      const rules = source.rules.map((rule, i) => (i === ruleIndex ? { ...rule, ...patch } : rule));
      const list = current.sources.map((item, i) => (i === sourceIndex ? { ...item, rules } : item));
      return { ...current, sources: list };
    });
  };
  const addRule = (sourceIndex: number) => {
    setDraft((current) => {
      if (!current) return current;
      const source = current.sources[sourceIndex];
      if (!source) return current;
      const rule: SuperagentRule = {
        id: `rule-${source.kind.replace(/[^a-z0-9]+/gi, '-')}-${source.rules.length + 1}`,
        enabled: false,
        kinds: [],
        actors: [],
        textAny: [],
        threadAny: [],
        minChars: 0,
      };
      const list = current.sources.map((item, i) =>
        i === sourceIndex ? { ...item, rules: [...item.rules, rule] } : item,
      );
      return { ...current, sources: list };
    });
  };

  const readableKinds = useMemo(() => view?.sourceKinds ?? [], [view]);
  const runningKindLabel = useMemo(
    () => (view ? view.modeLabel : stats?.modeLabel ?? ''),
    [view, stats],
  );
  /**
   * 最近一轮模型研判：手动点的优先，其次是后台循环投来的那一轮。
   * 两者同形状（同一个 `assess::run` 产出），所以这里不需要第二套口径。
   */
  const assessSummary = lastAssess ?? pump?.assessment ?? null;

  return (
    <Box sx={{ height: 'calc(100vh - 104px)', overflowY: 'auto', pr: 1 }}>
      {/* 头部：标题 + 档位 + 立即巡检 */}
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.25 }}>
        <RadarIcon sx={{ fontSize: 22, color: 'var(--tt-teal)', mt: 0.5 }} />
        <Box sx={{ minWidth: 0, flexGrow: 1 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
            <Typography sx={{ fontSize: '20px', fontWeight: 700, color: 'var(--tt-text-1)' }}>主动候选</Typography>
            {runningKindLabel && (
              <Box component="span" sx={{ ...chipSx, background: 'rgba(5, 150, 105, 0.10)', color: 'var(--tt-success)' }}>
                {runningKindLabel}
              </Box>
            )}
          </Box>
          <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-4)', mt: 0.5 }}>
            把环境事件记进账本并按策略研判；影子模式下一条都不会自动执行，要开工请在会话里正常发起。
          </Typography>
        </Box>
        <Box sx={{ display: 'flex', gap: 1, flexShrink: 0 }}>
          <Button
            variant="outlined"
            size="small"
            startIcon={<RefreshIcon sx={{ fontSize: 15 }} />}
            onClick={() => void handlePumpNow()}
            disabled={busy}
            sx={{
              color: 'var(--tt-text-2)',
              borderColor: 'var(--tt-border)',
              borderRadius: '10px',
              textTransform: 'none',
              fontSize: '12.5px',
            }}
          >
            立即巡检一轮
          </Button>
          <Button
            variant="outlined"
            size="small"
            startIcon={<PsychologyIcon sx={{ fontSize: 15 }} />}
            onClick={() => void handleAssessNow()}
            disabled={assessing || busy}
            sx={{
              color: 'var(--tt-text-2)',
              borderColor: 'var(--tt-border)',
              borderRadius: '10px',
              textTransform: 'none',
              fontSize: '12.5px',
            }}
          >
            立即研判存量候选
          </Button>
          <Button
            variant="outlined"
            size="small"
            startIcon={<AddOutlinedIcon sx={{ fontSize: 15 }} />}
            onClick={() => setRegisterOpen(true)}
            sx={{
              color: 'var(--tt-text-2)',
              borderColor: 'var(--tt-border)',
              borderRadius: '10px',
              textTransform: 'none',
              fontSize: '12.5px',
            }}
          >
            登记事件入口
          </Button>
        </Box>
      </Box>

      {/* 通道提示：读不到本机账本时把原因说清楚，不显示成「暂无数据」 */}
      {unavailable && (
        <Box
          sx={{
            mt: 2,
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

      {/* 台账概览：数字全部来自 stats，前端不自己数 */}
      {stats && (
        <Box
          sx={{
            mt: 2,
            px: 2,
            py: 1.25,
            borderRadius: '12px',
            background: 'rgba(59, 130, 246, 0.06)',
            border: '1px solid rgba(59, 130, 246, 0.15)',
          }}
        >
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1.25, alignItems: 'center' }}>
            {[
              ['策略版本', `v${stats.policyVersion}`],
              ['入口', `${stats.enabledSources}/${stats.sources} 在读取`],
              ['事件', `${stats.triggers}`],
              ['判定', `${stats.decisions}`],
              ['本可接单', `${stats.candidates}`],
              ['今日研判', `${stats.decidedToday}/${stats.dailyEvalCap}`],
              // 第二层单独三个数：今天问了几次模型、当前队列里还欠几条没问、被策略升版重排过几条
              ['模型研判', `${stats.assessedToday}/${stats.assessCap}`],
              ['待研判', `${stats.assessPending}`],
              ['策略重排', `${stats.requeued}`],
            ].map(([label, value]) => (
              <Box key={label} component="span" sx={chipSx}>
                <Typography component="span" sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
                  {label}
                </Typography>
                <Typography component="span" sx={{ fontSize: '11.5px', fontWeight: 700, color: 'var(--tt-text-2)' }}>
                  {value}
                </Typography>
              </Box>
            ))}
          </Box>
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-info)', mt: 0.75 }}>{stats.note}</Typography>
        </Box>
      )}

      {/* 最近一轮巡检的逐入口结果（后台循环与手动巡检共用同一份摘要） */}
      {pump && pump.sources.length > 0 && (
        <Box sx={{ mt: 1.5, display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          {pump.sources.map((run) => (
            <Box key={run.sourceId} component="span" sx={chipSx}>
              <Typography component="span" sx={{ fontSize: '11.5px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
                {run.kindLabel}
              </Typography>
              <Typography component="span" sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
                {run.instance || '本机'} · {run.statusLabel}
              </Typography>
              {run.error && (
                <Typography component="span" sx={{ fontSize: '11.5px', color: 'var(--tt-warning)', maxWidth: 320 }}>
                  {run.error}
                </Typography>
              )}
            </Box>
          ))}
        </Box>
      )}

      {/* 最近一轮模型研判（第二层）：只报「问过几次、判成什么、有没有花钱的必要」，没有任何执行 */}
      {assessSummary && (
        <Box
          sx={{
            mt: 1.5,
            px: 2,
            py: 1.25,
            borderRadius: '12px',
            background: 'rgba(124, 58, 237, 0.06)',
            border: '1px solid rgba(124, 58, 237, 0.16)',
          }}
        >
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
            <PsychologyIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />
            <Typography sx={{ fontSize: '12px', fontWeight: 700, color: 'var(--tt-text-2)' }}>
              模型研判（第二层）
            </Typography>
            {assessSummary.model && (
              <Box component="span" sx={chipSx}>
                模型 {assessSummary.model}
              </Box>
            )}
            <Box component="span" sx={chipSx}>
              预算 {assessSummary.capUsedToday}/{assessSummary.assessCap}
            </Box>
            {assessSummary.skipped ? (
              <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-warning)', flexGrow: 1 }}>
                本轮跳过：{assessSummary.skipReason}
              </Typography>
            ) : (
              [
                ['attempted', `问了 ${assessSummary.attempted} 条`],
                ['written', `写回 ${assessSummary.written} 条`],
                ['worth', `值得 ${assessSummary.worth}`],
                ['notWorth', `不值得 ${assessSummary.notWorth}`],
                ['unsure', `拿不准 ${assessSummary.unsure}`],
                ['failed', `失败 ${assessSummary.failed}`],
                ['blocked', `配额阻断 ${assessSummary.blocked}`],
                ['unavailable', `模型不可用 ${assessSummary.unavailable}`],
              ].map(([key, label]) => (
                <Box key={key} component="span" sx={chipSx}>
                  {label}
                </Box>
              ))
            )}
          </Box>
          {assessSummary.lastError && (
            <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-warning)', mt: 0.5, wordBreak: 'break-all' }}>
              最近一次失败：{assessSummary.lastError}
            </Typography>
          )}
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>{assessSummary.note}</Typography>
        </Box>
      )}

      {/* 四个视图 */}
      <Box
        role="tablist"
        aria-label="主动候选视图切换"
        sx={{ display: 'flex', gap: 3, mt: 3, borderBottom: '1px solid var(--tt-border)' }}
      >
        {TABS.map(([key, label], idx, arr) => (
          <Box
            key={key}
            role="tab"
            aria-selected={tab === key}
            tabIndex={tab === key ? 0 : -1}
            onClick={() => setTab(key)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ' || event.key === 'Spacebar') {
                event.preventDefault();
                setTab(key);
                return;
              }
              if (['ArrowLeft', 'ArrowUp', 'ArrowRight', 'ArrowDown'].includes(event.key)) {
                event.preventDefault();
                const step = event.key === 'ArrowLeft' || event.key === 'ArrowUp' ? -1 : 1;
                setTab(arr[(idx + step + arr.length) % arr.length][0]);
              }
            }}
            sx={{
              pb: 1,
              cursor: 'pointer',
              fontSize: '13.5px',
              fontWeight: 600,
              color: tab === key ? 'var(--tt-text-1)' : 'var(--tt-text-4)',
              borderBottom: tab === key ? '2px solid var(--tt-text-1)' : '2px solid transparent',
            }}
          >
            {label}
          </Box>
        ))}
      </Box>

      <Box sx={{ maxWidth: 980, mt: 2 }}>
        {tab === 'candidates' && (
          <Box>
            {/* 导出清单（验收项③）：影子模式的交付物就是这份文件，里面自带档位、策略版本与「没有一条被执行」的声明 */}
            <Box
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1,
                flexWrap: 'wrap',
                mb: 1.25,
                px: 1.5,
                py: 1,
                borderRadius: '12px',
                border: '1px solid var(--tt-border)',
                bgcolor: 'var(--tt-surface)',
              }}
            >
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', flexGrow: 1, minWidth: 220 }}>
                导出的是账本里已有的候选与判定理由，不重读工作区、也不重新研判一次。
              </Typography>
              <Box sx={{ display: 'flex', gap: 0.75, flexShrink: 0 }}>
                <Button
                  variant="outlined"
                  size="small"
                  startIcon={<DownloadIcon sx={{ fontSize: 15 }} />}
                  onClick={() => void handleExport('json')}
                  disabled={exporting}
                  sx={{
                    color: 'var(--tt-text-2)',
                    borderColor: 'var(--tt-border)',
                    borderRadius: '10px',
                    textTransform: 'none',
                    fontSize: '12.5px',
                  }}
                >
                  导出 JSON
                </Button>
                <Button
                  variant="outlined"
                  size="small"
                  startIcon={<DownloadIcon sx={{ fontSize: 15 }} />}
                  onClick={() => void handleExport('csv')}
                  disabled={exporting}
                  sx={{
                    color: 'var(--tt-text-2)',
                    borderColor: 'var(--tt-border)',
                    borderRadius: '10px',
                    textTransform: 'none',
                    fontSize: '12.5px',
                  }}
                >
                  导出 CSV
                </Button>
              </Box>
            </Box>
            {lastExport && (
              <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mb: 1.25, wordBreak: 'break-all' }}>
                上次导出 {lastExport.count} 条（{lastExport.format.toUpperCase()}）：{lastExport.path}
                <br />
                {lastExport.note}
              </Typography>
            )}
            {candidates.length === 0 && (
              <EmptyBox
                title="还没有「本可接单」的候选"
                lines={[
                  '候选只在同时满足三件事时才会出现：入口已登记、读取已开启、策略里有这个入口的启用规则。',
                  '也可以点「立即巡检一轮」手动跑一遍，看每个入口这一轮读到了什么。',
                ]}
              />
            )}
            {candidates.map((item) => (
              <Box
                key={item.decisionId}
                sx={{
                  bgcolor: 'var(--tt-surface)',
                  border: '1px solid var(--tt-border)',
                  borderRadius: '14px',
                  px: 2,
                  py: 1.5,
                  mb: 1,
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                  <Box component="span" sx={chipSx}>
                    {item.sourceKindLabel}
                  </Box>
                  <Box component="span" sx={chipSx}>
                    {item.strengthLabel}
                  </Box>
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', flexGrow: 1 }}>
                    {item.sourceLabel || item.sourceInstance || item.sourceKind} · {formatLocalDateTime(item.decidedAtMs)}
                  </Typography>
                  <Button
                    size="small"
                    onClick={() => void openDetail(item.triggerId)}
                    sx={{ color: 'var(--tt-text-2)', fontSize: '12px', textTransform: 'none' }}
                  >
                    看这条事件
                  </Button>
                </Box>
                <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: 'var(--tt-text-1)', mt: 1 }}>
                  {item.text || '（这条事件没有正文）'}
                </Typography>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', mt: 0.5, lineHeight: 1.6 }}>
                  {item.reason}
                </Typography>
                <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                  依据：{item.basis} · 命中规则 {item.matchedRules.length > 0 ? item.matchedRules.join('、') : '无'} ·
                  策略 v{item.policyVersion}
                </Typography>
                {/* 第二层（模型研判）：显示名全部来自 Rust，前端不翻；空 assessStatus 就是「还没问过模型」 */}
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap', mt: 0.75 }}>
                  <Box
                    component="span"
                    sx={{
                      ...chipSx,
                      background: item.assessVerdict
                        ? 'rgba(124, 58, 237, 0.10)'
                        : 'var(--tt-chip-neutral)',
                      color: item.assessVerdict ? 'var(--tt-text-2)' : 'var(--tt-text-4)',
                    }}
                  >
                    模型研判：{item.assessStatusLabel}
                    {item.assessVerdictLabel ? ` · ${item.assessVerdictLabel}` : ''}
                  </Box>
                  {item.assessModel && (
                    <Typography component="span" sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
                      {item.assessModel} · {item.assessTokens} tokens
                    </Typography>
                  )}
                  {/* 策略升版重排过就直说：判定依据的版本和现在排队的版本不是一个数，文案由 Rust 给 */}
                  {item.requeueLabel && (
                    <Typography component="span" sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
                      {item.requeueLabel}
                    </Typography>
                  )}
                </Box>
                {item.assessReason && (
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', mt: 0.25 }}>
                    模型理由：{item.assessReason}
                  </Typography>
                )}
                {item.assessError && (
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-warning)', mt: 0.25 }}>
                    研判未完成：{item.assessError}
                  </Typography>
                )}
              </Box>
            ))}
          </Box>
        )}

        {tab === 'sources' && (
          <Box>
            {sources.length === 0 && (
              <EmptyBox
                title='还没有登记任何事件入口'
                lines={['点右上角「登记事件入口」。第一验建议用项目事件：填一个你账号下的真实项目 id。']}
              />
            )}
            {sources.map((source) => (
              <Box
                key={source.id}
                sx={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 1.5,
                  bgcolor: 'var(--tt-surface)',
                  border: '1px solid var(--tt-border)',
                  borderRadius: '14px',
                  px: 2,
                  py: 1.5,
                  mb: 1,
                }}
              >
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                    <Box component="span" sx={chipSx}>
                      {source.kindLabel}
                    </Box>
                    <Typography sx={{ fontSize: '13px', fontWeight: 600, color: 'var(--tt-text-1)' }} noWrap>
                      {source.label || source.instance || '本机入口'}
                    </Typography>
                    {!source.enabled && (
                      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>未开启读取</Typography>
                    )}
                  </Box>
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                    游标 {source.cursor} · 更新于 {formatLocalDateTime(source.updatedAtMs)}
                  </Typography>
                  {source.lastError && (
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-warning)', mt: 0.5 }}>
                      上次失败：{source.lastError}
                    </Typography>
                  )}
                </Box>
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }}>
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)' }}>读取</Typography>
                  <Switch
                    size="small"
                    checked={source.enabled}
                    onChange={(event) => void handleToggleSource(source, event.target.checked)}
                    inputProps={{ 'aria-label': `${source.kindLabel} ${source.instance || '本机'} 是否读取` }}
                  />
                </Box>
              </Box>
            ))}
            <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 1 }}>
              这里的开关只管「读不读」；「能不能因此开工」在准入策略里，两者分开做，取消授权不会停掉读取，反之也一样。
            </Typography>
          </Box>
        )}

        {tab === 'policy' && draft && view && (
          <Box>
            <Box
              sx={{
                bgcolor: 'var(--tt-surface)',
                border: '1px solid var(--tt-border)',
                borderRadius: '14px',
                px: 2,
                py: 1.75,
                mb: 1.5,
              }}
            >
              <Typography sx={{ fontSize: '13px', fontWeight: 700, color: 'var(--tt-text-1)' }}>巡检档位</Typography>
              <Box sx={{ display: 'flex', gap: 1, mt: 1, flexWrap: 'wrap' }}>
                {(['off', 'observe'] as const).map((mode) => (
                  <Button
                    key={mode}
                    size="small"
                    variant={draft.acceptMode === mode ? 'contained' : 'outlined'}
                    onClick={() => patchDraft({ acceptMode: mode })}
                    sx={{
                      borderRadius: '10px',
                      textTransform: 'none',
                      fontSize: '12.5px',
                      color: draft.acceptMode === mode ? undefined : 'var(--tt-text-2)',
                      borderColor: 'var(--tt-border)',
                      bgcolor: draft.acceptMode === mode ? 'var(--tt-cta-fill)' : undefined,
                      '&:hover': { bgcolor: draft.acceptMode === mode ? 'var(--tt-cta-fill-hover)' : undefined },
                    }}
                  >
                    {mode === 'off' ? '关闭（不研判）' : '影子模式（研判照做，零自动执行）'}
                  </Button>
                ))}
                <Box component="span" sx={{ ...chipSx, alignSelf: 'center' }}>
                  自动接单：暂不可用 · {view.autoBlockedBy}
                </Box>
              </Box>
              <TextField
                size="small"
                type="number"
                label="每日研判上限（条）"
                value={draft.dailyEvalCap}
                onChange={(event) => patchDraft({ dailyEvalCap: Number(event.target.value) || 0 })}
                sx={{ mt: 1.5, width: 210 }}
                inputProps={{ min: 1, max: 2000, 'aria-label': '每日研判上限' }}
              />
              <TextField
                size="small"
                type="number"
                label="模型研判预算（条/日）"
                value={draft.assessMaxPerDay ?? ''}
                onChange={(event) =>
                  patchDraft({
                    // 留空 = 不写这一字段，由 Rust 补缺省；0 是「显式关闭第二层」，两者含义不同
                    assessMaxPerDay: event.target.value === '' ? undefined : Number(event.target.value) || 0,
                  })
                }
                sx={{ mt: 1.5, ml: 1.5, width: 220 }}
                inputProps={{ min: 0, max: 2000, 'aria-label': '模型研判预算' }}
              />
              <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.75, lineHeight: 1.6 }}>
                上限是成本闸门：到量之后事件照常入账，判定本轮跳过。留空或填 0 会回到默认的 200 条。
                <br />
                模型研判预算管第二层「今天最多问几次模型」，默认 50 条、填 0 就是关掉这一层；
                这一层只写「值不值得开工」的判断，不动任何手。
              </Typography>
            </Box>

            <Typography sx={{ fontSize: '13px', fontWeight: 700, color: 'var(--tt-text-1)', mb: 1 }}>
              入口授权（{draft.sources.length} 条）
            </Typography>
            {draft.sources.length === 0 && (
              <EmptyBox
                title='策略里还没有任何入口授权'
                lines={[
                  '缺省策略是「影子模式 + 零授权」：研判逻辑都在，但没有任何人、任何入口被允许开工。',
                  '先在「事件入口」登记并开启读取，再回到这里给对应入口勾选授权并写一条启用规则。',
                ]}
              />
            )}
            {draft.sources.map((source, sourceIndex) => (
              <Box
                key={`${source.kind}/${source.instance}`}
                sx={{
                  bgcolor: 'var(--tt-surface)',
                  border: '1px solid var(--tt-border)',
                  borderRadius: '14px',
                  px: 2,
                  py: 1.5,
                  mb: 1,
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                  <Box component="span" sx={chipSx}>
                    {(view.sourceKinds.find((k) => k.kind === source.kind)?.label) ?? source.kind}
                  </Box>
                  <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)', flexGrow: 1 }} noWrap>
                    {source.instance || '本机'}
                  </Typography>
                  <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)' }}>参与准入</Typography>
                  <Switch
                    size="small"
                    checked={source.enabled}
                    onChange={(event) => patchSource(sourceIndex, { enabled: event.target.checked })}
                    inputProps={{ 'aria-label': `${source.kind} ${source.instance || '本机'} 参与准入` }}
                  />
                </Box>
                {!readableKinds.find((k) => k.kind === source.kind)?.readable && (
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-warning)', mt: 0.5 }}>
                    这类事件源本段只登记不读取，给了授权也不会产生候选。
                  </Typography>
                )}
                <Box sx={{ display: 'flex', gap: 1, mt: 1, flexWrap: 'wrap' }}>
                  <TextField
                    size="small"
                    label="可直接指派的人（逗号分隔）"
                    defaultValue={fromList(source.allowUsers)}
                    onBlur={(event) => patchSource(sourceIndex, { allowUsers: toList(event.target.value) })}
                    sx={{ minWidth: 260, flexGrow: 1 }}
                    inputProps={{ 'aria-label': '可直接指派的人' }}
                  />
                  <TextField
                    size="small"
                    label="不接单的消息来自（逗号分隔）"
                    defaultValue={fromList(source.bots)}
                    onBlur={(event) => patchSource(sourceIndex, { bots: toList(event.target.value) })}
                    sx={{ minWidth: 240, flexGrow: 1 }}
                    inputProps={{ 'aria-label': '不接单的消息来自' }}
                  />
                </Box>
                <Box sx={{ mt: 1 }}>
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                    <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
                      规则（{source.rules.length}）
                    </Typography>
                    <Button
                      size="small"
                      startIcon={<AddOutlinedIcon sx={{ fontSize: 14 }} />}
                      onClick={() => addRule(sourceIndex)}
                      sx={{ color: 'var(--tt-text-2)', fontSize: '12px', textTransform: 'none' }}
                    >
                      加一条
                    </Button>
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
                      只有启用的规则能给出「主动推断」
                    </Typography>
                  </Box>
                  {source.rules.map((rule, ruleIndex) => (
                    <Box
                      key={`${source.kind}/${source.instance}/${rule.id}`}
                      sx={{ mt: 1, p: 1.25, borderRadius: '12px', border: '1px dashed var(--tt-border)' }}
                    >
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)', flexGrow: 1 }}>
                          {rule.id}
                        </Typography>
                        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-2)' }}>启用</Typography>
                        <Switch
                          size="small"
                          checked={rule.enabled}
                          onChange={(event) => patchRule(sourceIndex, ruleIndex, { enabled: event.target.checked })}
                          inputProps={{ 'aria-label': `启用规则 ${rule.id}` }}
                        />
                      </Box>
                      <Box sx={{ display: 'flex', gap: 1, mt: 0.75, flexWrap: 'wrap' }}>
                        <TextField
                          size="small"
                          label="事件类型"
                          defaultValue={fromList(rule.kinds)}
                          onBlur={(event) => patchRule(sourceIndex, ruleIndex, { kinds: toList(event.target.value) })}
                          sx={{ minWidth: 180, flexGrow: 1 }}
                          inputProps={{ 'aria-label': `规则 ${rule.id} 的事件类型` }}
                        />
                        <TextField
                          size="small"
                          label="发起人"
                          defaultValue={fromList(rule.actors)}
                          onBlur={(event) => patchRule(sourceIndex, ruleIndex, { actors: toList(event.target.value) })}
                          sx={{ minWidth: 160, flexGrow: 1 }}
                          inputProps={{ 'aria-label': `规则 ${rule.id} 的发起人` }}
                        />
                        <TextField
                          size="small"
                          label="正文命中词"
                          defaultValue={fromList(rule.textAny)}
                          onBlur={(event) => patchRule(sourceIndex, ruleIndex, { textAny: toList(event.target.value) })}
                          sx={{ minWidth: 200, flexGrow: 1 }}
                          inputProps={{ 'aria-label': `规则 ${rule.id} 的正文命中词` }}
                        />
                        <TextField
                          size="small"
                          label="会话命中词"
                          defaultValue={fromList(rule.threadAny)}
                          onBlur={(event) => patchRule(sourceIndex, ruleIndex, { threadAny: toList(event.target.value) })}
                          sx={{ minWidth: 160, flexGrow: 1 }}
                          inputProps={{ 'aria-label': `规则 ${rule.id} 的会话命中词` }}
                        />
                        <TextField
                          size="small"
                          type="number"
                          label="最少字数"
                          defaultValue={rule.minChars}
                          onBlur={(event) =>
                            patchRule(sourceIndex, ruleIndex, { minChars: Number(event.target.value) || 0 })
                          }
                          sx={{ width: 120 }}
                          inputProps={{ min: 0, 'aria-label': `规则 ${rule.id} 的最少字数` }}
                        />
                      </Box>
                      <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                        留空表示不限；命中词不区分大小写，最长 64 字。
                      </Typography>
                    </Box>
                  ))}
                </Box>
              </Box>
            ))}

            <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mt: 1 }}>
              <Button
                variant="contained"
                onClick={() => void handleSavePolicy()}
                disabled={busy}
                sx={{
                  bgcolor: 'var(--tt-cta-fill)',
                  borderRadius: '10px',
                  textTransform: 'none',
                  fontSize: '13px',
                  fontWeight: 600,
                  px: 2,
                  '&:hover': { bgcolor: 'var(--tt-cta-fill-hover)' },
                }}
              >
                保存策略
              </Button>
              <Button
                variant="outlined"
                onClick={() => setDraft(view.doc)}
                sx={{ color: 'var(--tt-text-2)', borderColor: 'var(--tt-border)', borderRadius: '10px', textTransform: 'none', fontSize: '12.5px' }}
              >
                放弃本地改动
              </Button>
              <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
                当前生效 v{view.version}；内容没变不新增版本；变了就新增一版——旧判定原样留着可追溯，
                还没研判的候选按新版重排、下一轮会再问一次模型，已研判过的不重判（不为同一个判断再花一次钱）。
              </Typography>
            </Box>

            <Typography sx={{ fontSize: '13px', fontWeight: 700, color: 'var(--tt-text-1)', mt: 3, mb: 1 }}>
              版本历史
            </Typography>
            {versions.length === 0 && (
              <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>
                还没有可读的版本历史（保存一次策略或打开本页后再看）。
              </Typography>
            )}
            {versions.map((item) => (
              <Box key={item.version} component="span" sx={{ ...chipSx, mr: 1, mb: 1 }}>
                v{item.version} · {item.modeLabel} · {item.source} · {item.digest} ·{' '}
                {formatLocalDateTime(item.createdAtMs)}
              </Box>
            ))}
          </Box>
        )}

        {tab === 'decisions' && (
          <Box>
            {decisions.length === 0 && (
              <EmptyBox title='还没有判定留痕' lines={['巡检到事件并跑过 gate 之后，这里会同时记下阳性与阴性。']} />
            )}
            {decisions.map((item) => (
              <Box
                key={item.id}
                sx={{
                  bgcolor: 'var(--tt-surface)',
                  border: '1px solid var(--tt-border)',
                  borderRadius: '14px',
                  px: 2,
                  py: 1.25,
                  mb: 1,
                }}
              >
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                  <Box component="span" sx={chipSx}>
                    {item.strengthLabel}
                  </Box>
                  <Box component="span" sx={chipSx}>
                    {item.wouldAccept ? '本可接单' : '未接单'}
                  </Box>
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', flexGrow: 1 }}>
                    策略 v{item.policyVersion} · {formatLocalDateTime(item.decidedAtMs)}
                  </Typography>
                  <Button
                    size="small"
                    onClick={() => void openDetail(item.triggerId)}
                    sx={{ color: 'var(--tt-text-2)', fontSize: '12px', textTransform: 'none' }}
                  >
                    看事件
                  </Button>
                </Box>
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)', mt: 0.5, lineHeight: 1.6 }}>
                  {item.reason}
                </Typography>
                <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>依据：{item.basis}</Typography>
              </Box>
            ))}
          </Box>
        )}
      </Box>

      {/* 登记事件入口 */}
      <Dialog open={registerOpen} onClose={() => setRegisterOpen(false)} maxWidth="xs" fullWidth>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>登记一个事件入口</DialogTitle>
        <DialogContent sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, pt: '8px !important' }}>
          <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)' }}>
            登记后新入口默认不读取，也不会自动获得授权——这是三件独立的事，都需要你显式做一次。
          </Typography>
          <Select
            size="small"
            value={regKind}
            onChange={(event) => setRegKind(event.target.value as string)}
            aria-label="事件入口类型"
            sx={{ fontSize: '13px' }}
          >
            {readableKinds.map((item) => (
              <MenuItem key={item.kind} value={item.kind} sx={{ fontSize: '13px' }}>
                {item.label}
                {item.readable ? '' : '（本段只登记不读取）'}
              </MenuItem>
            ))}
            {readableKinds.length === 0 && (
              <MenuItem value={regKind} sx={{ fontSize: '13px' }}>
                {regKind}
              </MenuItem>
            )}
          </Select>
          <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
            {INSTANCE_HINTS[regKind] ?? '按上面的类型填写具体对象'}
          </Typography>
          <TextField
            size="small"
            label="实例（项目 id / 会话 id，本机入口可留空）"
            value={regInstance}
            onChange={(event) => setRegInstance(event.target.value)}
            inputProps={{ 'aria-label': '事件入口实例' }}
          />
          <TextField
            size="small"
            label="备注名（可选）"
            value={regLabel}
            onChange={(event) => setRegLabel(event.target.value)}
            inputProps={{ 'aria-label': '事件入口备注名' }}
          />
          {(regKind === 'schedule' || regKind === 'condition') && (
            <>
              <TextField
                size="small"
                type="number"
                label="间隔（分钟，最小 1）"
                value={regEveryMinutes}
                onChange={(event) => setRegEveryMinutes(event.target.value)}
                inputProps={{ min: 1, 'aria-label': '定时间隔分钟' }}
              />
              <TextField
                size="small"
                type="datetime-local"
                label="或一次性到点时间（留空则按间隔）"
                value={regAtText}
                onChange={(event) => setRegAtText(event.target.value)}
                InputLabelProps={{ shrink: true }}
                inputProps={{ 'aria-label': '一次性到点时间' }}
              />
            </>
          )}
          {regKind === 'workspace_file' && (
            <TextField
              size="small"
              label="要巡检的目录（绝对路径）"
              value={regDir}
              onChange={(event) => setRegDir(event.target.value)}
              inputProps={{ 'aria-label': '工作区目录' }}
            />
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <Button onClick={() => setRegisterOpen(false)} sx={{ color: 'var(--tt-text-2)', fontSize: '13px' }}>
            取消
          </Button>
          <Button onClick={() => void handleRegister()} sx={{ color: 'var(--tt-teal)', fontSize: '13px', fontWeight: 600 }}>
            登记
          </Button>
        </DialogActions>
      </Dialog>

      {/* 事件详情：只展示账本里已有的字段，不额外拉取上游内容 */}
      <Dialog open={Boolean(detail)} onClose={() => setDetail(null)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontSize: '15px', fontWeight: 700 }}>事件详情</DialogTitle>
        <DialogContent sx={{ pt: '8px !important' }}>
          {detail?.error && (
            <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-warning)' }}>{detail.error}</Typography>
          )}
          {!detail?.row && !detail?.error && (
            <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-4)' }}>读取中…</Typography>
          )}
          {detail?.row && (
            <Box component="pre" sx={{
              m: 0,
              p: 1.5,
              borderRadius: '12px',
              bgcolor: 'var(--tt-surface)',
              border: '1px solid var(--tt-border)',
              fontSize: '12px',
              lineHeight: 1.6,
              color: 'var(--tt-text-2)',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              fontFamily: 'inherit',
            }}>
              {[
                `事件标识：${detail.row.externalId}`,
                `上游类型：${detail.row.kind}`,
                `会话键：${detail.row.threadKey || '（无）'}`,
                `发起人：${detail.row.actor || '（无）'}`,
                `是否点名本应用：${detail.row.addressedAgent ? '是' : '否（正文里的 @ 不算）'}`,
                `到达时间：${formatLocalDateTime(detail.row.arrivedAtMs)}`,
                '',
                '正文（入库时已截断）：',
                detail.row.text || '（空）',
                '',
                '载荷（只存定位用的最小字段）：',
                JSON.stringify(detail.row.payload, null, 2),
              ].join('\n')}
            </Box>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, pb: 2 }}>
          <IconButton onClick={() => setDetail(null)} aria-label="关闭事件详情" size="small">
            <CloseIcon sx={{ fontSize: 16 }} />
          </IconButton>
        </DialogActions>
      </Dialog>
    </Box>
  );
};

/** 空态：说清楚「为什么是空的」和「下一步做什么」，不留一句「暂无数据」 */
const EmptyBox: React.FC<{ title: string; lines: string[] }> = ({ title, lines }) => (
  <Box sx={{ border: '1px dashed var(--tt-border)', borderRadius: '14px', p: 4 }}>
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75 }}>
      {lines.length > 0 && <InfoOutlinedIcon sx={{ fontSize: 15, color: 'var(--tt-text-4)' }} />}
      <Typography sx={{ fontSize: '13.5px', fontWeight: 600, color: 'var(--tt-text-2)' }}>{title}</Typography>
    </Box>
    {lines.map((line) => (
      <Typography key={line} sx={{ fontSize: '12px', color: 'var(--tt-text-4)', mt: 0.75, lineHeight: 1.7 }}>
        {line}
      </Typography>
    ))}
  </Box>
);

export default SuperAgentPage;
