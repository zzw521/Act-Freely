/**
 * 用量总览与报表 —— `usage:stats` / `usage:report` 的唯一消费方
 *
 * - 总览条：周期内 Token / 调用 / 活跃模型 / 最近使用 / 平均耗时 / 按币种成本
 * - 预算档位：日 / 月两档各自的档位、剩余与恢复方式（缺陷 D-14，判定见 shared/quota-gate）
 * - 报表：点「生成报表」才拉取 `usage:report`（按日趋势 + 模型明细 + 汇总），可复制成文本
 * - 口径：台账记录引擎上报的真实 token、端到端耗时与按调用时单价估算的成本；
 *   未配置单价的调用标为「未计价」，不同币种不合并（见 usage-local.ts）
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@mui/material';
import { ContentCopy as CopyIcon, Assessment as ReportIcon, Refresh as RefreshIcon, CloudUpload as UploadIcon } from '@mui/icons-material';
import { useModelUsageStore } from '../../stores/useModelUsageStore';
import { fmtCosts, fmtLatency } from '../../lib/usage-local';
import { api } from '../../lib/api';
import { errorTextOf } from '../../../shared/error-messages';
import {
  BUDGET_SCOPE_LABEL,
  budgetEntrySummaryZh,
  recoveryHintZh,
} from '../../../shared/quota-gate';

const PERIOD_LABEL = { daily: '今日', monthly: '本月', total: '总计' } as const;

const TIER_CHIP_COLOR = { notify: 'default', degrade: 'warning', queue: 'warning', block: 'error' } as const;

function fmtNum(n: number): string {
  return n.toLocaleString('en-US');
}

function fmtTime(ms: number): string {
  if (!ms) return '—';
  return new Date(ms).toLocaleString('zh-CN', { hour12: false });
}

function Metric({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Box sx={{ minWidth: 108 }}>
      <Typography variant="caption" color="text.secondary" display="block">
        {label}
      </Typography>
      <Typography variant="body1" sx={{ fontWeight: 600 }}>
        {value}
      </Typography>
      {hint && (
        <Typography variant="caption" color="text.secondary" display="block">
          {hint}
        </Typography>
      )}
    </Box>
  );
}

const UsageOverviewPanel: React.FC = () => {
  const period = useModelUsageStore((s) => s.period);
  const overview = useModelUsageStore((s) => s.overview);
  const overviewError = useModelUsageStore((s) => s.overviewError);
  const loadOverview = useModelUsageStore((s) => s.loadOverview);
  const budget = useModelUsageStore((s) => s.budget);
  const budgetError = useModelUsageStore((s) => s.budgetError);
  const loadBudget = useModelUsageStore((s) => s.loadBudget);
  const report = useModelUsageStore((s) => s.report);
  const reportLoading = useModelUsageStore((s) => s.reportLoading);
  const reportError = useModelUsageStore((s) => s.reportError);
  const loadReport = useModelUsageStore((s) => s.loadReport);
  const reportMarkdown = useModelUsageStore((s) => s.reportMarkdown);

  const [showText, setShowText] = useState(false);
  const [copyHint, setCopyHint] = useState<string | null>(null);
  const [uploading, setUploading] = useState(false);
  const [uploadHint, setUploadHint] = useState<{
    severity: 'success' | 'info' | 'error';
    text: string;
  } | null>(null);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview, period]);

  // 预算判的是「今日 + 本月」两档，与页面选中的周期无关，只挂载时读一次
  useEffect(() => {
    void loadBudget();
  }, [loadBudget]);

  const maxDayTokens = report?.days.reduce((m, d) => Math.max(m, d.tokensUsed), 0) ?? 0;

  const handleCopy = useCallback(async () => {
    const md = reportMarkdown();
    if (!md) return;
    try {
      await navigator.clipboard.writeText(md);
      setCopyHint('报表文本已复制到剪贴板');
    } catch {
      // 剪贴板不可用（如 WebView 未授权）：不静默失败，改由下方文本区手动选择
      setCopyHint('复制失败，请在「查看文本」中手动选择复制');
    }
  }, [reportMarkdown]);

  // D-43：把桌面台账推到服务端合并视图；结果如实展示，Mock/未登录会明确说明
  const handleUpload = useCallback(async () => {
    setUploading(true);
    setUploadHint(null);
    try {
      const result = await api.usage.upload();
      setUploadHint({
        severity: result.uploaded ? 'success' : 'info',
        text: result.uploaded
          ? `${result.message}（设备 ${result.deviceId.slice(0, 8)}）`
          : result.message,
      });
    } catch (e) {
      setUploadHint({ severity: 'error', text: errorTextOf(e, '用量上报失败') });
    } finally {
      setUploading(false);
    }
  }, []);

  const totals = overview?.totals;

  return (
    <Paper sx={{ mb: 3, p: 2.5, borderRadius: '12px' }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 3, alignItems: 'center' }}>
        <Chip size="small" label={`${PERIOD_LABEL[period]}用量`} color="primary" variant="outlined" />
        {overviewError ? (
          <Typography variant="body2" color="error.main">
            {overviewError}
            <Button size="small" onClick={() => void loadOverview()} startIcon={<RefreshIcon />}>
              重试
            </Button>
          </Typography>
        ) : totals ? (
          <>
            <Metric label="Token 合计" value={fmtNum(totals.tokensUsed)} />
            <Metric label="输入 / 输出" value={`${fmtNum(totals.promptTokens)} / ${fmtNum(totals.completionTokens)}`} />
            <Metric label="调用次数" value={fmtNum(totals.calls)} />
            <Metric label="活跃模型" value={String(totals.activeModels)} />
            <Metric
              label="平均耗时"
              value={fmtLatency(totals.avgLatencyMs, totals.latencySamples ?? 0)}
              hint={(totals.latencySamples ?? 0) > 0 ? `${fmtNum(totals.latencySamples!)} 次样本` : '暂无样本'}
            />
            <Metric
              label="成本"
              value={fmtCosts(totals.costs, totals.pricedCalls ?? 0)}
              hint={(totals.unpricedCalls ?? 0) > 0 ? `${fmtNum(totals.unpricedCalls!)} 次未计价` : undefined}
            />
            <Metric label="用量最高" value={overview?.topModel ?? '—'} />
            <Metric label="最近使用" value={fmtTime(totals.lastUsedAtMs)} />
          </>
        ) : (
          <Typography variant="body2" color="text.secondary">
            正在读取用量台账…
          </Typography>
        )}
        <Box sx={{ flex: 1 }} />
        <Button
          size="small"
          variant="outlined"
          startIcon={
            uploading ? <CircularProgress size={16} color="inherit" /> : <UploadIcon />
          }
          disabled={uploading}
          onClick={() => void handleUpload()}
        >
          {uploading ? '上报中…' : '上报用量'}
        </Button>
        <Button
          size="small"
          variant="outlined"
          startIcon={
            reportLoading ? (
              <CircularProgress size={16} color="inherit" />
            ) : (
              <ReportIcon />
            )
          }
          disabled={reportLoading}
          onClick={() => void loadReport()}
        >
          {reportLoading ? '生成中…' : '生成报表'}
        </Button>
      </Box>

      <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1.5 }}>
        口径：按「模型 × UTC 日」累加引擎上报的真实 token 与耗时；成本 = Token × 调用时配置的单价，
        未配置单价的调用标为「未计价」而非 0 元，不同币种不合并。
      </Typography>

      {uploadHint && (
        <Alert
          severity={uploadHint.severity}
          sx={{ mt: 1.5 }}
          onClose={() => setUploadHint(null)}
        >
          {uploadHint.text}
        </Alert>
      )}

      {/* 预算档位（缺陷 D-14）：日 / 月两档各自的档位、剩余与恢复方式 */}
      {budgetError ? (
        <Alert severity="warning" sx={{ mt: 1.5 }}>
          {budgetError}
          <Button size="small" onClick={() => void loadBudget()} startIcon={<RefreshIcon />}>
            重试
          </Button>
        </Alert>
      ) : budget ? (
        <Box sx={{ mt: 1.5, display: 'flex', flexDirection: 'column', gap: 0.75 }}>
          {budget.verdict.unlimited ? (
            <Typography variant="caption" color="text.secondary">
              未设 Token 预算（设置 →「模型与用量」可配日 / 月上限）。
            </Typography>
          ) : (
            budget.verdict.entries.map((entry) => (
              <Box key={entry.scope} sx={{ display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
                <Chip
                  size="small"
                  variant="outlined"
                  color={TIER_CHIP_COLOR[entry.snapshot.action]}
                  label={
                    entry.percent < 70
                      ? `${BUDGET_SCOPE_LABEL[entry.scope]}预算未达预警线`
                      : `${BUDGET_SCOPE_LABEL[entry.scope]}预算档位 ${entry.snapshot.tier}%`
                  }
                />
                <Typography variant="body2" color="text.secondary">
                  {budgetEntrySummaryZh(entry)}
                </Typography>
                {(entry.snapshot.action === 'queue' || entry.snapshot.action === 'block') && (
                  <Typography variant="caption" color="warning.main" display="block">
                    恢复方式：{recoveryHintZh(entry.snapshot.action, entry.scope)}
                  </Typography>
                )}
              </Box>
            ))
          )}
          {budget.verdict.unreadable.length > 0 && (
            <Typography variant="caption" color="warning.main">
              {budget.verdict.unreadable.map((s) => `${BUDGET_SCOPE_LABEL[s]}用量`).join('、')}
              读取失败，本轮未判定该档（提交按放行处理）。
            </Typography>
          )}
          <Typography variant="caption" color="text.secondary">
            这里的判定由应用侧预算闸门执行；日额度与桌面端 Rust 硬停台账共用同一数值，月额度只在本层判定。
          </Typography>
        </Box>
      ) : null}

      {reportError && (
        <Alert severity="error" sx={{ mt: 2 }}>
          {reportError}
        </Alert>
      )}

      {report && (
        <Box sx={{ mt: 2 }}>
          <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 1 }}>
            <Typography variant="subtitle2">按日趋势</Typography>
            <Typography variant="caption" color="text.secondary">
              生成于 {fmtTime(report.generatedAtMs)}
            </Typography>
            <Box sx={{ flex: 1 }} />
            <Button size="small" onClick={() => setShowText((v) => !v)}>
              {showText ? '收起文本' : '查看文本'}
            </Button>
            <Button size="small" startIcon={<CopyIcon />} onClick={() => void handleCopy()}>
              复制报表
            </Button>
          </Box>

          {report.days.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              该周期暂无用量记录。
            </Typography>
          ) : (
            <Box sx={{ display: 'flex', gap: 1, alignItems: 'flex-end', height: 72, mb: 1 }}>
              {report.days.map((d) => (
                <Box
                  key={d.day}
                  title={
                    `${d.day}：${fmtNum(d.tokensUsed)} token · ${d.calls} 次调用 · ${d.models} 个模型` +
                    ` · ${fmtLatency(d.avgLatencyMs, d.latencySamples ?? 0)} · ${fmtCosts(d.costs, d.pricedCalls ?? 0)}`
                  }
                  sx={{ flex: 1, textAlign: 'center' }}
                >
                  <Box
                    sx={{
                      height: Math.max(4, Math.round((d.tokensUsed / (maxDayTokens || 1)) * 52)),
                      bgcolor: 'primary.main',
                      borderRadius: '4px 4px 0 0',
                      opacity: 0.85,
                    }}
                  />
                  <Typography variant="caption" color="text.secondary" display="block">
                    {d.day.slice(5)}
                  </Typography>
                </Box>
              ))}
            </Box>
          )}

          {report.models.length > 0 && (
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>模型</TableCell>
                  <TableCell align="right">输入 Token</TableCell>
                  <TableCell align="right">输出 Token</TableCell>
                  <TableCell align="right">合计</TableCell>
                  <TableCell align="right">调用</TableCell>
                  <TableCell align="right">平均耗时</TableCell>
                  <TableCell align="right">成本</TableCell>
                  <TableCell align="right">最近使用</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {report.models.map((m) => (
                  <TableRow key={m.modelName}>
                    <TableCell>{m.displayName || m.modelName}</TableCell>
                    <TableCell align="right">{fmtNum(m.promptTokens)}</TableCell>
                    <TableCell align="right">{fmtNum(m.completionTokens)}</TableCell>
                    <TableCell align="right">{fmtNum(m.tokensUsed)}</TableCell>
                    <TableCell align="right">{m.calls}</TableCell>
                    <TableCell align="right">{fmtLatency(m.avgLatencyMs, m.latencySamples ?? 0)}</TableCell>
                    <TableCell align="right">{fmtCosts(m.costs, m.pricedCalls ?? 0)}</TableCell>
                    <TableCell align="right">{fmtTime(m.lastUsedAtMs)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}

          {(report.totals.unpricedCalls ?? 0) > 0 && (
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
              有 {fmtNum(report.totals.unpricedCalls!)} 次调用未配置输入/输出单价，未计入成本。
            </Typography>
          )}

          {copyHint && (
            <Typography variant="caption" color="text.secondary" display="block" sx={{ mt: 1 }}>
              {copyHint}
            </Typography>
          )}
          {showText && (
            <Box
              component="pre"
              sx={{
                mt: 1,
                p: 1.5,
                maxHeight: 280,
                overflow: 'auto',
                whiteSpace: 'pre-wrap',
                fontSize: 12,
                lineHeight: 1.6,
                bgcolor: 'action.hover',
                borderRadius: '8px',
                userSelect: 'text',
              }}
            >
              {reportMarkdown()}
            </Box>
          )}
        </Box>
      )}
    </Paper>
  );
};

export default UsageOverviewPanel;
