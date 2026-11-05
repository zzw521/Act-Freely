/**
 * Token 预算设置（缺陷 D-14）
 *
 * 此前 Rust 侧有配额闸门、引擎也会因额度返回 429，但界面上既看不到额度、
 * 也配不了预算——用户只在「任务突然交不出」时才发现超了。本卡片补上配置入口：
 * - 日 / 月两档预算，填 0 即该档不限；保存即写入 settings_kv，下一次提交立刻生效（无需重启）
 * - 当场显示按用量台账判出的档位与剩余，让「配了多少」和「现在是什么状态」并排看见
 *
 * 口径必须说白：日额度会同步写入桌面端 Rust 的配额台账（`quota-ledger.json`，
 * 保存即生效、重启后保留），下一次提交由 Rust 按同一额度硬停；月额度只在渲染层
 * 提交前判定。两档都依据引擎上报的真实 Token（按 UTC 日/月分桶，跨日自动重置）。
 * 单任务级预算（缺陷 D-14.1）也写进同一份台账，按 task_id 分别累计：用满后执行面
 * 停在下一轮模型调用之前，任务转入「预算暂停」——它不会到点自动重置，调高后要在
 * 任务上点「继续」唤回（D-14.2）；这条拦停对桌面内置执行体和 Python 引擎都生效。
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  TextField,
  Typography,
} from '@mui/material';
import RefreshOutlinedIcon from '@mui/icons-material/RefreshOutlined';
import { useAppSettings } from '../../lib/use-app-settings';
import { readBudgetVerdict } from '../../lib/budget-gate';
import { getSettings } from '../../lib/alignment-api';
import { api } from '../../lib/api';
import { BUDGET_SCOPE_LABEL, TIER_CONSEQUENCE_ZH, formatUsagePercent } from '../../../shared/quota-gate';
import { errorTextOf } from '../../../shared/error-messages';
import type { BudgetSettings } from '../../../shared/contracts';
import type { BudgetRead } from '../../lib/budget-gate';

const DIGITS_ONLY = /^\d{0,12}$/;

function fmtNum(n: number): string {
  return n.toLocaleString('en-US');
}

const TIER_COLOR = { notify: 'default', degrade: 'warning', queue: 'warning', block: 'error' } as const;

const TokenBudgetPanel: React.FC = () => {
  const [appSettings, updateAppSettings] = useAppSettings();
  const saved = appSettings.budget;
  const [dailyDraft, setDailyDraft] = useState('');
  const [monthlyDraft, setMonthlyDraft] = useState('');
  const [perTaskDraft, setPerTaskDraft] = useState('');
  const [read, setRead] = useState<BudgetRead | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [savedMsg, setSavedMsg] = useState('');

  // 以已保存值为草稿基线：外部（另一处设置写入）改动后不用等刷新
  useEffect(() => {
    setDailyDraft(String(saved?.dailyTokens ?? 200000));
    setMonthlyDraft(String(saved?.monthlyTokens ?? 0));
    setPerTaskDraft(String(saved?.perTaskTokens ?? 0));
  }, [saved?.dailyTokens, saved?.monthlyTokens, saved?.perTaskTokens]);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const settings = await getSettings().catch(() => undefined);
      setRead(await readBudgetVerdict((period) => api.usage.stats(period), settings));
    } catch (e) {
      setError(errorTextOf(e, '预算读取失败'));
      setRead(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const draftNumbers = (): { daily: number; monthly: number; perTask: number } | null => {
    if (
      !DIGITS_ONLY.test(dailyDraft.trim()) ||
      !DIGITS_ONLY.test(monthlyDraft.trim()) ||
      !DIGITS_ONLY.test(perTaskDraft.trim())
    ) {
      return null;
    }
    return {
      daily: Number(dailyDraft.trim() || '0'),
      monthly: Number(monthlyDraft.trim() || '0'),
      perTask: Number(perTaskDraft.trim() || '0'),
    };
  };

  const invalid = draftNumbers() === null;

  const handleSave = async () => {
    const nums = draftNumbers();
    if (!nums) return;
    setError(null);
    // 三档一起写：预算对象是整体覆盖的，少写一档等于把那一档清成默认值
    const payload: BudgetSettings = {
      dailyTokens: nums.daily,
      monthlyTokens: nums.monthly,
      perTaskTokens: nums.perTask,
    };
    await updateAppSettings({ budget: payload });
    setSavedMsg('预算已保存：下一次提交起按新额度判定，无需重启应用。');
    void load();
    setTimeout(() => setSavedMsg(''), 5000);
  };

  return (
    <Box
      sx={{
        mt: 2,
        bgcolor: 'var(--tt-surface)',
        border: '1px solid var(--tt-border)',
        borderRadius: '14px',
        p: 3,
      }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, mb: 0.5 }}>
        <Typography sx={{ fontSize: '15px', fontWeight: 600, color: 'var(--tt-text-1)' }}>Token 预算</Typography>
        <Box sx={{ flex: 1 }} />
        <Button
          size="small"
          startIcon={loading ? <CircularProgress size={14} color="inherit" /> : <RefreshOutlinedIcon />}
          onClick={() => void load()}
          disabled={loading}
          sx={{ color: 'var(--tt-text-2)', fontSize: '12px' }}
        >
          重新读取用量
        </Button>
      </Box>
      <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', lineHeight: 1.7 }}>
        按日 / 月 / 单任务三档设上限（填 0 表示该档不限）；到 70% / 85% / 95% / 100% 四档依次提示、降级、排队、拒绝新提交。
        日、月两档管的是「提交闸口」，单任务档管的是「已经在跑的任务能烧多少」。
      </Typography>

      <Box sx={{ display: 'flex', gap: 2, mt: 2, flexWrap: 'wrap', alignItems: 'flex-start' }}>
        {[
          {
            key: 'dailyTokens',
            label: `${BUDGET_SCOPE_LABEL.daily}预算（Token）`,
            value: dailyDraft,
            setValue: setDailyDraft,
            helper: '0 = 不限；跨 UTC 日自动重置',
          },
          {
            key: 'monthlyTokens',
            label: `${BUDGET_SCOPE_LABEL.monthly}预算（Token）`,
            value: monthlyDraft,
            setValue: setMonthlyDraft,
            helper: '0 = 不限；下月 1 日（UTC）自动重置',
          },
          {
            key: 'perTaskTokens',
            label: '单任务预算（Token）',
            value: perTaskDraft,
            setValue: setPerTaskDraft,
            helper: '0 = 不限；用满即在下一轮模型调用前停住，不会自动重置',
          },
        ].map((field) => (
          <TextField
            key={field.key}
            size="small"
            label={field.label}
            value={field.value}
            onChange={(e) => field.setValue(e.target.value)}
            error={!DIGITS_ONLY.test(field.value.trim())}
            helperText={!DIGITS_ONLY.test(field.value.trim()) ? '只能填非负整数（0 表示不限）' : field.helper}
            sx={{ width: 240 }}
            inputProps={{ inputMode: 'numeric', maxLength: 12 }}
          />
        ))}
        <Button
          variant="contained"
          onClick={() => void handleSave()}
          disabled={invalid}
          sx={{ borderRadius: '10px', px: 2.5, fontSize: '13px', fontWeight: 600, height: 40 }}
        >
          保存预算
        </Button>
      </Box>

      {savedMsg && (
        <Alert severity="success" sx={{ mt: 2 }}>
          {savedMsg}
        </Alert>
      )}
      {error && (
        <Alert severity="error" sx={{ mt: 2 }}>
          {error}
        </Alert>
      )}

      {read && (
        <Box sx={{ mt: 2 }}>
          {read.verdict.unlimited ? (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)' }}>
              日 / 月两档预算都没设（或都为 0）：提交前不做预算判定，新任务不受额度限制。
              单任务档不在这份判定里——它按 task_id 记在 Rust 的同一份台账上，用满时任务自己会停下来。
            </Typography>
          ) : (
            read.verdict.entries.map((entry) => (
              <Box
                key={entry.scope}
                sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap', mt: 0.75 }}
              >
                <Chip
                  size="small"
                  color={TIER_COLOR[entry.snapshot.action]}
                  label={`${BUDGET_SCOPE_LABEL[entry.scope]} ${formatUsagePercent(entry.percent)}%`}
                />
                <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-2)' }}>
                  已用 {fmtNum(entry.usedTokens)} / {fmtNum(entry.quotaTokens)}，剩 {fmtNum(entry.remaining)} ——{' '}
                  {TIER_CONSEQUENCE_ZH[entry.snapshot.action]}
                </Typography>
              </Box>
            ))
          )}
          {read.verdict.unreadable.length > 0 && (
            <Typography sx={{ fontSize: '12px', color: 'var(--tt-warning)', mt: 0.75 }}>
              {read.verdict.unreadable
                .map((s) => `${BUDGET_SCOPE_LABEL[s]}用量读取失败`)
                .join('；')}
              ：该档本轮未判定，提交按放行处理。
            </Typography>
          )}
          {Object.keys(read.errors).length > 0 && (
            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.5 }}>
              {Object.values(read.errors)[0]}
            </Typography>
          )}
        </Box>
      )}

      <Alert severity="info" sx={{ mt: 2 }}>
        <Typography sx={{ fontSize: '12px', lineHeight: 1.7 }}>
          口径：日额度会同步写入桌面端 Rust 的配额台账（保存即生效，重启后仍保留），下一次提交由 Rust 按同一额度硬停；
          月额度只在应用层提交前判定。两档都依据用量台账里引擎上报的真实 Token（按 UTC 日 / 月分桶，跨日自动重置）。
          单任务预算同样同步进 Rust 台账并按任务分别累计（保存即生效，重启后仍保留）：某任务用满后，桌面内置执行体和
          Python 引擎都会在下一轮模型调用之前停住，任务转入「预算暂停」，用量照常计入当日总量。这一档不会到点自动重置，
          调高上限后要在任务上点「继续」唤回——不点就不会自己恢复，也可以直接取消任务。
        </Typography>
      </Alert>
    </Box>
  );
};

export default TokenBudgetPanel;
