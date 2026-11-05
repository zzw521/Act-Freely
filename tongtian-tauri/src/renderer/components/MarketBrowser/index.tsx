/**
 * 在线市场浏览器（技能 / 专家套件共用）
 *
 * 事实来源：
 * - 索引地址由用户在窗口内配置，落 settings_kv（market.sources），不在代码里写死市场
 * - 列表来自 Rust 拉取并校验后的索引；下载地址不进入渲染层，安装只传 itemId
 * - Rust 侧强制：远程 HTTPS、本机回环才允许 HTTP、禁重定向、SHA-256 必校验、大小上限
 * - 浏览器通道如实回报 supported=false，不展示假条目、不伪造安装成功
 */

import React, { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Dialog,
  DialogContent,
  DialogTitle,
  Divider,
  IconButton,
  Snackbar,
  TextField,
  Typography,
} from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import RefreshIcon from '@mui/icons-material/Refresh';
import SettingsOutlinedIcon from '@mui/icons-material/SettingsOutlined';
import DownloadIcon from '@mui/icons-material/Download';
import SyncIcon from '@mui/icons-material/Sync';
import UpgradeIcon from '@mui/icons-material/SystemUpdateAlt';
import { marketAPI } from '../../lib/api';
import { errorTextOf } from '../../../shared/error-messages';
import type {
  MarketInstalledItem,
  MarketInstalledStatus,
  MarketItemBase,
  MarketList,
  MarketPublisher,
  MarketSourceConfig,
  MarketUpdateItem,
  MarketUpdateReport,
} from '../../../shared/contracts';

export type MarketKind = 'skill' | 'suite';

export interface MarketBrowserProps {
  open: boolean;
  kind: MarketKind;
  onClose: () => void;
  /** 安装成功后回调（页面刷新自己的已安装列表） */
  onInstalled?: () => void;
}

const KIND_TEXT: Record<MarketKind, { title: string; label: string; noun: string }> = {
  skill: { title: '在线技能市场', label: '技能市场索引地址', noun: '技能' },
  suite: { title: '在线套件市场', label: '套件市场索引地址', noun: '套件' },
};

function formatSize(bytes: number | null): string | null {
  if (bytes === null || !Number.isFinite(bytes) || bytes <= 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function shortFingerprint(fingerprint: string | null): string {
  if (!fingerprint) return '未知';
  return fingerprint.length > 16 ? `${fingerprint.slice(0, 16)}…` : fingerprint;
}

function publisherLabel(item: MarketItemBase): string {
  return item.publisherName || item.publisherId || '未知发布者';
}

/** 更新状态措辞：只报「比得出的结论」，比不了时如实说比不了（不写「已最新」） */
const STATUS_TEXT: Record<MarketInstalledStatus, { label: string; tone: 'ok' | 'warn' | 'bad' }> = {
  upToDate: { label: '已是最新', tone: 'ok' },
  updateAvailable: { label: '可更新', tone: 'warn' },
  ahead: { label: '本机版本更高', tone: 'ok' },
  unknownVersion: { label: '版本无法比较', tone: 'warn' },
  delisted: { label: '索引里已下架', tone: 'warn' },
  revoked: { label: '已被撤销', tone: 'bad' },
};

function formatWhen(ms: number | null): string {
  if (!ms || !Number.isFinite(ms)) return '从未';
  const d = new Date(ms);
  if (Number.isNaN(d.getTime())) return '未知';
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const TONE_VAR: Record<'ok' | 'warn' | 'bad', { bg: string; fg: string }> = {
  ok: { bg: 'var(--tt-chip-teal)', fg: 'var(--tt-teal)' },
  warn: { bg: 'var(--tt-chip-warning)', fg: 'var(--tt-warning)' },
  bad: { bg: 'var(--tt-chip-danger)', fg: 'var(--tt-danger)' },
};

const StatusChip: React.FC<{ tone: 'ok' | 'warn' | 'bad'; label: string; title?: string }> = ({
  tone,
  label,
  title,
}) => (
  <Chip
    size="small"
    label={label}
    title={title}
    sx={{
      height: 18,
      fontSize: '10.5px',
      bgcolor: TONE_VAR[tone].bg,
      color: TONE_VAR[tone].fg,
      maxWidth: 240,
      '& .MuiChip-label': { overflow: 'hidden', textOverflow: 'ellipsis' },
    }}
  />
);

const MarketBrowser: React.FC<MarketBrowserProps> = ({ open, kind, onClose, onInstalled }) => {
  const text = KIND_TEXT[kind];
  const [config, setConfig] = useState<MarketSourceConfig | null>(null);
  const [configLoading, setConfigLoading] = useState(false);
  const [urlDraft, setUrlDraft] = useState('');
  const [editingSource, setEditingSource] = useState(false);
  const [saving, setSaving] = useState(false);
  const [list, setList] = useState<MarketList<MarketItemBase> | null>(null);
  const [listing, setListing] = useState(false);
  const [listError, setListError] = useState<string | null>(null);
  const [installingId, setInstallingId] = useState<string | null>(null);
  /** 正在升级的条目 id（单条升级）；与 installingId 分开，避免两条流水线互相顶掉 */
  const [upgradingId, setUpgradingId] = useState<string | null>(null);
  const [upgradingAll, setUpgradingAll] = useState(false);
  const [trustBusy, setTrustBusy] = useState<string | null>(null);
  const [checking, setChecking] = useState(false);
  const [report, setReport] = useState<MarketUpdateReport | null>(null);
  const [installedRows, setInstalledRows] = useState<MarketInstalledItem[]>([]);
  const [lastCheckAtMs, setLastCheckAtMs] = useState<number | null>(null);
  const [toast, setToast] = useState<{ severity: 'success' | 'error' | 'info'; text: string } | null>(null);

  const indexUrl = kind === 'skill' ? config?.skillIndexUrl ?? null : config?.suiteIndexUrl ?? null;

  const loadList = useCallback(async () => {
    setListing(true);
    setListError(null);
    try {
      const next =
        kind === 'skill'
          ? await marketAPI.listSkills()
          : await marketAPI.listSuites();
      setList(next as MarketList<MarketItemBase>);
    } catch (e) {
      setList(null);
      setListError(errorTextOf(e, '市场索引读取失败'));
    } finally {
      setListing(false);
    }
  }, [kind]);

  const loadConfig = useCallback(async () => {
    setConfigLoading(true);
    try {
      const next = await marketAPI.config();
      setConfig(next);
      const url = kind === 'skill' ? next.skillIndexUrl : next.suiteIndexUrl;
      setUrlDraft(url ?? '');
      setEditingSource(!url);
      if (next.supported && url) {
        await loadList();
      } else {
        setList(null);
      }
    } catch (e) {
      setConfig(null);
      setList(null);
      setListError(errorTextOf(e, '市场配置读取失败'));
    } finally {
      setConfigLoading(false);
    }
  }, [kind, loadList]);

  const loadInstalled = useCallback(async () => {
    try {
      const next = await marketAPI.installedList();
      setInstalledRows(next.supported ? next.items : []);
      setLastCheckAtMs(next.supported ? next.lastCheckAtMs : null);
    } catch {
      // 本机清单读不到不影响市场浏览：留空列表，检查更新时再如实报错
      setInstalledRows([]);
      setLastCheckAtMs(null);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    setToast(null);
    setListError(null);
    setReport(null);
    void loadConfig();
    void loadInstalled();
  }, [open, loadConfig, loadInstalled]);

  const handleUpdateCheck = async () => {
    if (checking) return;
    setChecking(true);
    try {
      const next = await marketAPI.updateCheck();
      setReport(next);
      await loadInstalled();
      if (next.supported) setLastCheckAtMs(next.checkedAtMs);
      if (!next.supported) {
        setToast({ severity: 'info', text: '当前通道没有市场清单与下载能力' });
      } else if (next.summary.revoked > 0) {
        setToast({
          severity: 'error',
          text: `发现 ${next.summary.revoked} 个已撤销条目，已禁用并移出执行面`,
        });
      } else if (next.summary.updateAvailable > 0) {
        setToast({ severity: 'success', text: `有 ${next.summary.updateAvailable} 个条目可以更新` });
      } else {
        setToast({ severity: 'success', text: '没有可用更新（已检查的来源均为最新或未标注版本）' });
      }
      // 逐来源的读取失败单独显示：不能因为撤销清单没读到就把整份条目列表换成错误
      if (next.errors.length > 0) {
        setToast({
          severity: 'error',
          text: `部分来源没读到：${next.errors.join('；')}`,
        });
      }
    } catch (e) {
      setToast({ severity: 'error', text: errorTextOf(e, '检查更新失败') });
    } finally {
      setChecking(false);
    }
  };

  const handleSaveSource = async () => {
    if (saving) return;
    setSaving(true);
    try {
      const value = urlDraft.trim();
      const patch =
        kind === 'skill'
          ? { skillIndexUrl: value ? value : null }
          : { suiteIndexUrl: value ? value : null };
      const next = await marketAPI.setConfig(patch);
      setConfig(next);
      setUrlDraft((kind === 'skill' ? next.skillIndexUrl : next.suiteIndexUrl) ?? '');
      setEditingSource(false);
      if (value) {
        await loadList();
      } else {
        setList(null);
      }
      setToast({ severity: 'success', text: value ? '市场索引地址已保存' : '已清除市场索引地址' });
    } catch (e) {
      setToast({ severity: 'error', text: errorTextOf(e, '市场索引地址保存失败') });
    } finally {
      setSaving(false);
    }
  };

  const handleInstall = async (item: MarketItemBase) => {
    if (installingId || upgradingId || upgradingAll) return;
    setInstallingId(item.id);
    try {
      if (kind === 'skill') {
        await marketAPI.installSkill(item.id);
      } else {
        await marketAPI.installSuite(item.id);
      }
      setToast({ severity: 'success', text: `已安装${text.noun}「${item.name}」` });
      onInstalled?.();
      // 刷新 installed 标记与本机清单（索引可能较大，但安装后的状态必须取真实值）
      await loadList();
      await loadInstalled();
    } catch (e) {
      setToast({ severity: 'error', text: errorTextOf(e, `${text.noun}安装失败`) });
    } finally {
      setInstallingId(null);
    }
  };

  /**
   * 升级单条：校验链与安装同一条（撤销闸门、SHA-256、验签一道不少），落盘换成
   * 「旧版本进回收目录 + 新版本原子落位」，失败时 Rust 会把旧版本放回并把两条路径都回传。
   * 未安装的条目不在这里升（页面也不会给按钮）：Rust 侧直接拒为 SKILL_NOT_FOUND。
   */
  const handleUpgrade = async (item: { id: string; name: string }) => {
    if (installingId || upgradingId) return;
    setUpgradingId(item.id);
    try {
      const row =
        kind === 'skill'
          ? await marketAPI.upgradeSkill(item.id)
          : await marketAPI.upgradeSuite(item.id);
      setToast({
        severity: 'success',
        text: `已升级「${item.name}」${row.marketVersion ? `到 ${row.marketVersion}` : ''}；旧版本已移入回收目录${row.enabled === false ? '（该条目仍是禁用状态，需要时请在技能页手动启用）' : ''}`,
      });
      onInstalled?.();
      await loadList();
      await loadInstalled();
    } catch (e) {
      setToast({ severity: 'error', text: errorTextOf(e, `${text.noun}升级失败`) });
    } finally {
      setUpgradingId(null);
    }
  };

  /** 一键升级：只处理检查更新判定为可更新的条目，单条失败不牵连其余 */
  const handleUpgradeAll = async () => {
    if (upgradingAll || installingId || upgradingId) return;
    setUpgradingAll(true);
    try {
      const next = await marketAPI.upgradeAll();
      if (!next.supported) {
        setToast({ severity: 'info', text: '当前通道没有市场下载与落盘能力' });
      } else if (next.attempted === 0) {
        setToast({ severity: 'info', text: '没有可升级的条目（本机清单里没有判定为新版本的条目）' });
      } else if (next.failed === 0) {
        setToast({ severity: 'success', text: `已升级 ${next.upgraded} 个条目，旧版本都在回收目录里` });
      } else {
        const failedNames = next.items
          .filter((row) => !row.ok)
          .map((row) => `${row.name || row.id}：${row.error ?? '未知原因'}`);
        setToast({
          severity: 'error',
          text: `成功 ${next.upgraded} 个、失败 ${next.failed} 个（失败的仍停在原版本）：${failedNames.join('；')}`,
        });
      }
      onInstalled?.();
      await loadList();
      await loadInstalled();
    } catch (e) {
      setToast({ severity: 'error', text: errorTextOf(e, '批量升级失败') });
    } finally {
      setUpgradingAll(false);
    }
  };

  const handleTrust = async (publisher: MarketPublisher, trusted: boolean) => {
    if (trustBusy) return;
    setTrustBusy(publisher.id);
    try {
      if (trusted) {
        await marketAPI.trustRemove({ publisherId: publisher.id });
      } else {
        await marketAPI.trustAdd({
          publisherId: publisher.id,
          publicKey: publisher.publicKey,
          name: publisher.name,
        });
      }
      await loadList();
      setToast({
        severity: 'success',
        text: trusted
          ? `已取消信任「${publisher.name || publisher.id}」`
          : `已信任「${publisher.name || publisher.id}」，安装时会校验发布者签名`,
      });
    } catch (e) {
      setToast({ severity: 'error', text: errorTextOf(e, '发布者信任更新失败') });
    } finally {
      setTrustBusy(null);
    }
  };

  const unsupported = config !== null && !config.supported;
  const items = list?.items ?? [];
  const publishers = list?.publishers ?? [];
  const signaturePolicy = list?.signaturePolicy ?? null;
  const installedHere = installedRows.filter((row) => row.kind === kind);
  /** 检查更新结果里查本条目的版本差距（没检查过就是 undefined，页面据此不编造状态） */
  const updateOf = (id: string): MarketUpdateItem | undefined =>
    report?.items.find((row) => row.kind === kind && row.id === id);
  /** 本轮检查更新判定「可更新」且属于当前类别的条目：全部升级只吃这一份，不自己猜版本 */
  const pendingUpgrades = (report?.items ?? []).filter(
    (row) => row.kind === kind && row.status === 'updateAvailable',
  );

  return (
    <>
      <Dialog
        open={open}
        onClose={() => {
          if (!installingId && !upgradingId && !upgradingAll && !saving && !trustBusy) onClose();
        }}
        maxWidth="md"
        fullWidth
        PaperProps={{ sx: { borderRadius: '16px' } }}
      >
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1, pr: 6 }}>
          <Typography sx={{ fontSize: '17px', fontWeight: 700, flexGrow: 1 }}>{text.title}</Typography>
          <IconButton
            size="small"
            aria-label={`关闭${text.title}`}
            onClick={onClose}
            disabled={Boolean(installingId) || saving || Boolean(trustBusy)}
            sx={{ position: 'absolute', right: 12, top: 12 }}
          >
            <CloseIcon fontSize="small" />
          </IconButton>
        </DialogTitle>
        <DialogContent sx={{ pt: '4px !important' }}>
          {configLoading ? (
            <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
              <CircularProgress size={26} />
            </Box>
          ) : unsupported ? (
            <Alert severity="warning" sx={{ fontSize: '13px' }}>
              当前运行通道没有市场下载与落盘能力，请在桌面版任我行中使用在线市场。
            </Alert>
          ) : (
            <Box>
              {/* 索引源配置 */}
              <Box
                sx={{
                  display: 'flex',
                  alignItems: 'center',
                  gap: 1,
                  mb: 2,
                  px: 1.5,
                  py: 1,
                  borderRadius: '10px',
                  background: 'var(--tt-surface-2)',
                  border: '1px solid var(--tt-border)',
                }}
              >
                <SettingsOutlinedIcon sx={{ fontSize: 16, color: 'var(--tt-text-2)' }} />
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>索引源</Typography>
                  <Typography
                    sx={{
                      fontSize: '12.5px',
                      color: indexUrl ? 'var(--tt-text-2)' : 'var(--tt-warning)',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                      whiteSpace: 'nowrap',
                    }}
                  >
                    {indexUrl ?? '未配置（填写市场索引 JSON 地址后才能拉取条目）'}
                  </Typography>
                </Box>
                <Button
                  size="small"
                  onClick={() => setEditingSource((v) => !v)}
                  sx={{ textTransform: 'none', fontSize: '12px', color: 'var(--tt-accent)', flexShrink: 0 }}
                >
                  {editingSource ? '收起' : '配置'}
                </Button>
                <Button
                  size="small"
                  startIcon={<RefreshIcon sx={{ fontSize: 15 }} />}
                  disabled={!indexUrl || listing}
                  onClick={() => void loadList()}
                  sx={{ textTransform: 'none', fontSize: '12px', color: 'var(--tt-accent)', flexShrink: 0 }}
                >
                  刷新
                </Button>
                <Button
                  size="small"
                  startIcon={checking ? <CircularProgress size={13} sx={{ color: 'var(--tt-accent)' }} /> : <SyncIcon sx={{ fontSize: 15 }} />}
                  disabled={
                    checking ||
                    (!indexUrl && installedRows.length === 0) ||
                    Boolean(installingId) ||
                    Boolean(upgradingId) ||
                    upgradingAll ||
                    Boolean(trustBusy)
                  }
                  title={
                    !indexUrl && installedRows.length === 0
                      ? '先配置索引地址，或等本机出现已安装条目'
                      : '比对已安装清单：发现新版本并应用市场的撤销'
                  }
                  onClick={() => void handleUpdateCheck()}
                  sx={{ textTransform: 'none', fontSize: '12px', color: 'var(--tt-accent)', flexShrink: 0 }}
                >
                  {checking ? '检查中…' : '检查更新'}
                </Button>
                <Button
                  size="small"
                  startIcon={
                    upgradingAll ? (
                      <CircularProgress size={13} sx={{ color: 'var(--tt-accent)' }} />
                    ) : (
                      <UpgradeIcon sx={{ fontSize: 15 }} />
                    )
                  }
                  disabled={
                    upgradingAll ||
                    checking ||
                    Boolean(installingId) ||
                    Boolean(upgradingId) ||
                    pendingUpgrades.length === 0
                  }
                  title={
                    pendingUpgrades.length === 0
                      ? '先「检查更新」，有可更新条目时这里才会亮'
                      : `把 ${pendingUpgrades.length} 个可更新条目升到索引当前版本（校验链与安装一致，旧版本进回收目录）`
                  }
                  onClick={() => void handleUpgradeAll()}
                  sx={{ textTransform: 'none', fontSize: '12px', color: 'var(--tt-accent)', flexShrink: 0 }}
                >
                  {upgradingAll
                    ? '升级中…'
                    : pendingUpgrades.length > 0
                      ? `全部升级 ${pendingUpgrades.length}`
                      : '全部升级'}
                </Button>
              </Box>

              {editingSource && (
                <Box sx={{ mb: 2 }}>
                  <TextField
                    fullWidth
                    size="small"
                    label={text.label}
                    value={urlDraft}
                    onChange={(e) => setUrlDraft(e.target.value)}
                    placeholder="https://market.example.com/skills.json（本机调试可用 http://127.0.0.1:8080/...）"
                    sx={{ '& .MuiOutlinedInput-root': { borderRadius: '10px' } }}
                  />
                  <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, mt: 1 }}>
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', flexGrow: 1 }}>
                      远程索引只接受 HTTPS；下载内容会按 SHA-256 校验，带签名的条目还会校验 Ed25519 发布者签名。
                    </Typography>
                    <Button
                      variant="contained"
                      size="small"
                      disabled={saving}
                      onClick={() => void handleSaveSource()}
                      sx={{
                        bgcolor: 'var(--tt-cta-fill)',
                        color: 'var(--tt-cta-ink)',
                        borderRadius: '9px',
                        textTransform: 'none',
                        fontSize: '12px',
                        '&:hover': { bgcolor: 'var(--tt-cta-fill-hover)' },
                      }}
                    >
                      {saving ? '保存中…' : '保存并拉取'}
                    </Button>
                  </Box>
                </Box>
              )}

              {(publishers.length > 0 || signaturePolicy === 'required') && (
                <Box
                  sx={{
                    mb: 1.5,
                    px: 1.5,
                    py: 1.2,
                    borderRadius: '10px',
                    border: '1px solid var(--tt-border)',
                    background: 'var(--tt-surface-2)',
                  }}
                >
                  <Typography sx={{ fontSize: '12.5px', fontWeight: 700, color: 'var(--tt-text-2)' }}>
                    发布者信任
                  </Typography>
                  <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.25 }}>
                    {signaturePolicy === 'required'
                      ? '该索引要求所有条目带发布者签名；未信任的发布者不能安装。'
                      : '签名可选；信任后安装会校验 Ed25519 签名，发布者换钥需要重新确认。'}
                  </Typography>
                  {publishers.length === 0 ? (
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-warning)', mt: 0.75 }}>
                      索引没有声明任何发布者，当前条目均为未签名。
                    </Typography>
                  ) : (
                    publishers.map((publisher) => (
                      <Box
                        key={publisher.id}
                        sx={{
                          display: 'flex',
                          alignItems: 'center',
                          gap: 1,
                          py: 0.9,
                          mt: 0.5,
                          borderTop: '1px solid var(--tt-border)',
                        }}
                      >
                        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                          <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-2)' }}>
                            {publisher.name || publisher.id}
                          </Typography>
                          <Typography
                            sx={{
                              fontSize: '10.5px',
                              color: 'var(--tt-text-4)',
                              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {publisher.id} · 指纹 {shortFingerprint(publisher.fingerprint)}
                          </Typography>
                          {publisher.keyChanged && (
                            <Typography sx={{ fontSize: '10.5px', color: 'var(--tt-warning)', mt: 0.2 }}>
                              公钥与本机已信任的不一致；请先移除旧信任，再核对新公钥后信任。
                            </Typography>
                          )}
                        </Box>
                        <Chip
                          size="small"
                          label={
                            publisher.trusted
                              ? '已信任'
                              : publisher.keyChanged
                                ? '公钥已变化'
                                : '未信任'
                          }
                          sx={{
                            height: 20,
                            fontSize: '10.5px',
                            flexShrink: 0,
                            bgcolor: publisher.trusted ? 'var(--tt-chip-teal)' : publisher.keyChanged ? 'var(--tt-chip-warning)' : 'var(--tt-chip-neutral)',
                            color: publisher.trusted ? 'var(--tt-teal)' : publisher.keyChanged ? 'var(--tt-warning)' : 'var(--tt-text-3)',
                          }}
                        />
                        {publisher.trusted ? (
                          <Button
                            size="small"
                            disabled={trustBusy === publisher.id}
                            onClick={() => void handleTrust(publisher, true)}
                            sx={{ textTransform: 'none', fontSize: '11.5px', color: 'var(--tt-danger)', flexShrink: 0 }}
                          >
                            取消信任
                          </Button>
                        ) : publisher.keyChanged ? (
                          <Button
                            size="small"
                            disabled={trustBusy === publisher.id}
                            onClick={() => void handleTrust(publisher, true)}
                            sx={{ textTransform: 'none', fontSize: '11.5px', color: 'var(--tt-warning)', flexShrink: 0 }}
                          >
                            移除旧信任
                          </Button>
                        ) : (
                          <Button
                            size="small"
                            disabled={trustBusy === publisher.id}
                            onClick={() => void handleTrust(publisher, false)}
                            sx={{ textTransform: 'none', fontSize: '11.5px', color: 'var(--tt-accent)', flexShrink: 0 }}
                          >
                            {trustBusy === publisher.id ? '保存中…' : '信任'}
                          </Button>
                        )}
                      </Box>
                    ))
                  )}
                </Box>
              )}

              {(installedHere.length > 0 || report) && (
                <Box
                  sx={{
                    mb: 1.5,
                    px: 1.5,
                    py: 1.2,
                    borderRadius: '10px',
                    border: '1px solid var(--tt-border)',
                    background: 'var(--tt-surface-2)',
                  }}
                >
                  <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
                    <Typography sx={{ fontSize: '12.5px', fontWeight: 700, color: 'var(--tt-text-2)' }}>
                      本机已安装{text.noun}
                    </Typography>
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', flexGrow: 1 }}>
                      上次检查 {formatWhen(lastCheckAtMs)}
                    </Typography>
                    {report && (
                      <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-3)' }}>
                        可更新 {report.summary.updateAvailable} · 已撤销 {report.summary.revoked} · 已下架{' '}
                        {report.summary.delisted}
                      </Typography>
                    )}
                  </Box>

                  {report && report.errors.length > 0 && (
                    <Alert severity="warning" sx={{ mt: 0.75, py: 0.25, '& .MuiAlert-message': { fontSize: '11.5px' } }}>
                      {report.errors.join('；')}
                    </Alert>
                  )}

                  {installedHere.length === 0 ? (
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                      本机清单里还没有{kind === 'skill' ? '技能' : '套件'}（清单为空不代表市场没有条目，只代表这台机器没装过）。
                    </Typography>
                  ) : (
                    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5, mt: 0.75 }}>
                      {installedHere.map((row) => {
                        const st = report?.items.find((c) => c.kind === row.kind && c.id === row.id);
                        const tone = st ? STATUS_TEXT[st.status] : null;
                        return (
                          <Box key={`${row.kind}:${row.id}`} sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexWrap: 'wrap' }}>
                            <Typography sx={{ fontSize: '12px', fontWeight: 600, color: 'var(--tt-text-1)' }}>
                              {row.name || row.id}
                            </Typography>
                            <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)' }}>
                              {row.installedVersion ? `本机 ${row.installedVersion}` : '本机版本未知'}
                              {st?.latestVersion ? ` · 索引 ${st.latestVersion}` : ''}
                              {row.provenance === 'scanned' ? ' · 本机扫描补齐' : ''}
                            </Typography>
                            {row.revoked ? (
                              <StatusChip
                                tone="bad"
                                label={`已撤销${row.revokedReason ? `：${row.revokedReason}` : ''}`}
                                title="已被市场撤销：已转入禁用且不会进入执行面；启用开关也无法重新打开"
                              />
                            ) : row.disabled ? (
                              <StatusChip tone="warn" label="已禁用" title="禁用中，本轮不会进入执行面" />
                            ) : tone ? (
                              <StatusChip
                                tone={tone.tone}
                                label={tone.label}
                                title={
                                  st?.status === 'updateAvailable'
                                    ? `索引提供 ${st.latestVersion ?? '新版本'}；升级走与安装相同的校验链，旧版本移入回收目录`
                                    : st?.status === 'delisted'
                                      ? '本轮读取的索引里已没有这个条目（另一来源可能仍有，未据此禁用）'
                                      : undefined
                                }
                              />
                            ) : null}
                          </Box>
                        );
                      })}
                    </Box>
                  )}
                </Box>
              )}

              <Divider sx={{ my: 1.5 }} />

              {listing ? (
                <Box sx={{ display: 'flex', justifyContent: 'center', py: 6 }}>
                  <CircularProgress size={26} />
                </Box>
              ) : listError ? (
                <Alert severity="error" sx={{ fontSize: '12.5px' }}>
                  {listError}
                </Alert>
              ) : !indexUrl ? (
                <Box sx={{ py: 4, textAlign: 'center' }}>
                  <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
                    先填写{text.noun}市场索引地址，再拉取可安装条目。
                  </Typography>
                </Box>
              ) : items.length === 0 ? (
                <Box sx={{ py: 4, textAlign: 'center' }}>
                  <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)' }}>
                    索引已连接，但里面没有{text.noun}条目。
                  </Typography>
                  {list && (
                    <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                      拉取自 {list.indexUrl}
                    </Typography>
                  )}
                </Box>
              ) : (
                <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
                  {items.map((item) => {
                    const size = formatSize(item.sizeBytes);
                    const busy = installingId === item.id;
                    const blockedByTrust = item.signed && (!item.publisherTrusted || item.publisherKeyChanged);
                    return (
                      <Box
                        key={item.id}
                        sx={{
                          display: 'flex',
                          gap: 1.5,
                          alignItems: 'center',
                          p: 1.5,
                          borderRadius: '12px',
                          border: '1px solid var(--tt-border)',
                          '&:hover': { borderColor: 'var(--tt-border)' },
                        }}
                      >
                        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                          <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
                            <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: 'var(--tt-text-1)' }}>
                              {item.name}
                            </Typography>
                            {item.version && (
                              <Chip
                                size="small"
                                label={item.version}
                                sx={{ height: 18, fontSize: '10.5px', bgcolor: 'var(--tt-chip-neutral)', color: 'var(--tt-text-2)' }}
                              />
                            )}
                            {item.author && (
                              <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>{item.author}</Typography>
                            )}
                            {item.signed ? (
                              <Chip
                                size="small"
                                label={
                                  item.publisherTrusted
                                    ? `已签名 · ${publisherLabel(item)}`
                                    : item.publisherKeyChanged
                                      ? `公钥已变化 · ${publisherLabel(item)}`
                                      : `未信任 · ${publisherLabel(item)}`
                                }
                                sx={{
                                  height: 18,
                                  fontSize: '10.5px',
                                  bgcolor: item.publisherTrusted ? 'var(--tt-chip-teal)' : item.publisherKeyChanged ? 'var(--tt-chip-warning)' : 'var(--tt-chip-neutral)',
                                  color: item.publisherTrusted ? 'var(--tt-teal)' : item.publisherKeyChanged ? 'var(--tt-warning)' : 'var(--tt-text-3)',
                                }}
                              />
                            ) : (
                              <Chip
                                size="small"
                                label="未签名"
                                sx={{ height: 18, fontSize: '10.5px', bgcolor: 'var(--tt-chip-neutral)', color: 'var(--tt-text-4)' }}
                              />
                            )}
                            {item.revoked && (
                              <StatusChip
                                tone="bad"
                                label={`已撤销${item.revokedReason ? `：${item.revokedReason}` : ''}`}
                                title="市场已撤销该条目：安装会被拒绝；本机已装的对应版本会转禁用且不再进入执行面"
                              />
                            )}
                            {!item.revoked &&
                              item.installed &&
                              updateOf(item.id)?.status === 'updateAvailable' && (
                                <StatusChip
                                  tone="warn"
                                  label={`可更新到 ${updateOf(item.id)?.latestVersion ?? '新版本'}`}
                                  title="索引里有更新的版本：点右侧「升级」，校验链与安装完全一致，旧版本移入回收目录后换成新版"
                                />
                              )}
                          </Box>
                          {item.description && (
                            <Typography
                              sx={{
                                fontSize: '12px',
                                color: 'var(--tt-text-2)',
                                mt: 0.35,
                                display: '-webkit-box',
                                WebkitLineClamp: 2,
                                WebkitBoxOrient: 'vertical',
                                overflow: 'hidden',
                              }}
                            >
                              {item.description}
                            </Typography>
                          )}
                          <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.35 }}>
                            {item.id}
                            {size ? ` · ${size}` : ''}
                          </Typography>
                        </Box>
                        {item.revoked ? (
                          <Box
                            title={item.revokedReason ?? '市场未给出原因'}
                            sx={{
                              px: 1.25,
                              py: 0.35,
                              borderRadius: '8px',
                              background: 'var(--tt-chip-danger)',
                              flexShrink: 0,
                              maxWidth: 150,
                            }}
                          >
                            <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: 'var(--tt-danger)' }}>
                              不可安装
                            </Typography>
                            <Typography
                              sx={{
                                fontSize: '10.5px',
                                color: 'var(--tt-danger)',
                                overflow: 'hidden',
                                textOverflow: 'ellipsis',
                                whiteSpace: 'nowrap',
                              }}
                            >
                              {item.revokedReason ?? '已被市场撤销'}
                            </Typography>
                          </Box>
                        ) : item.installed ? (
                          updateOf(item.id)?.status === 'updateAvailable' ? (
                            <Button
                              size="small"
                              variant="outlined"
                              startIcon={
                                upgradingId === item.id ? (
                                  <CircularProgress size={13} sx={{ color: 'var(--tt-accent)' }} />
                                ) : (
                                  <UpgradeIcon sx={{ fontSize: 15 }} />
                                )
                              }
                              disabled={Boolean(installingId) || Boolean(upgradingId) || upgradingAll}
                              title="校验链与安装完全一致；旧版本移入回收目录后再换成索引当前版本"
                              onClick={() => void handleUpgrade({ id: item.id, name: item.name })}
                              sx={{
                                textTransform: 'none',
                                fontSize: '12px',
                                fontWeight: 600,
                                borderRadius: '9px',
                                flexShrink: 0,
                                color: 'var(--tt-accent)',
                                borderColor: 'var(--tt-accent)',
                              }}
                            >
                              {upgradingId === item.id ? '升级中…' : '升级'}
                            </Button>
                          ) : (
                            <Box sx={{ px: 1.25, py: 0.35, borderRadius: '8px', background: 'var(--tt-chip-teal)', flexShrink: 0 }}>
                              <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: 'var(--tt-teal)' }}>
                                已安装
                              </Typography>
                            </Box>
                          )
                        ) : (
                          <Button
                            size="small"
                            variant="contained"
                            startIcon={busy ? <CircularProgress size={13} sx={{ color: 'var(--tt-cta-ink)' }} /> : <DownloadIcon sx={{ fontSize: 15 }} />}
                            disabled={Boolean(installingId) || Boolean(upgradingId) || upgradingAll || blockedByTrust}
                            title={blockedByTrust ? '请先在「发布者信任」中确认并信任该发布者' : undefined}
                            onClick={() => void handleInstall(item)}
                            sx={{
                              bgcolor: 'var(--tt-cta-fill)',
                              color: 'var(--tt-cta-ink)',
                              borderRadius: '9px',
                              textTransform: 'none',
                              fontSize: '12px',
                              fontWeight: 600,
                              flexShrink: 0,
                              '&:hover': { bgcolor: 'var(--tt-cta-fill-hover)' },
                            }}
                          >
                            {busy ? '安装中…' : blockedByTrust ? '先信任发布者' : '安装'}
                          </Button>
                        )}
                      </Box>
                    );
                  })}
                  {list && (
                    <Typography sx={{ fontSize: '11px', color: 'var(--tt-text-4)', mt: 0.5 }}>
                      共 {items.length} 项 · 拉取自 {list.indexUrl}
                    </Typography>
                  )}
                </Box>
              )}
            </Box>
          )}
        </DialogContent>
      </Dialog>
      <Snackbar
        open={!!toast}
        autoHideDuration={toast?.severity === 'error' ? 6000 : 3000}
        onClose={() => setToast(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'center' }}
      >
        <Alert severity={toast?.severity ?? 'info'} onClose={() => setToast(null)} sx={{ fontSize: '12.5px' }}>
          {toast?.text}
        </Alert>
      </Snackbar>
    </>
  );
};

export default MarketBrowser;
