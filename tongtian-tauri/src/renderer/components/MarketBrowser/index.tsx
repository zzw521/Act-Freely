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
import { marketAPI } from '../../lib/api';
import { errorTextOf } from '../../../shared/error-messages';
import type {
  MarketItemBase,
  MarketList,
  MarketPublisher,
  MarketSourceConfig,
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
  const [trustBusy, setTrustBusy] = useState<string | null>(null);
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

  useEffect(() => {
    if (!open) return;
    setToast(null);
    setListError(null);
    void loadConfig();
  }, [open, loadConfig]);

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
    if (installingId) return;
    setInstallingId(item.id);
    try {
      if (kind === 'skill') {
        await marketAPI.installSkill(item.id);
      } else {
        await marketAPI.installSuite(item.id);
      }
      setToast({ severity: 'success', text: `已安装${text.noun}「${item.name}」` });
      onInstalled?.();
      // 刷新 installed 标记（索引可能较大，但安装后的状态必须取真实值）
      await loadList();
    } catch (e) {
      setToast({ severity: 'error', text: errorTextOf(e, `${text.noun}安装失败`) });
    } finally {
      setInstallingId(null);
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

  return (
    <>
      <Dialog
        open={open}
        onClose={() => {
          if (!installingId && !saving && !trustBusy) onClose();
        }}
        maxWidth="md"
        fullWidth
        PaperProps={{ sx: { borderRadius: '16px' } }}
      >
        <DialogTitle sx={{ display: 'flex', alignItems: 'center', gap: 1, pr: 6 }}>
          <Typography sx={{ fontSize: '17px', fontWeight: 700, flexGrow: 1 }}>{text.title}</Typography>
          <IconButton
            size="small"
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
              当前运行通道没有市场下载与落盘能力，请在桌面版通天办公中使用在线市场。
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
                  background: '#F8FAFC',
                  border: '1px solid #EEF0F2',
                }}
              >
                <SettingsOutlinedIcon sx={{ fontSize: 16, color: '#6B7280' }} />
                <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                  <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>索引源</Typography>
                  <Typography
                    sx={{
                      fontSize: '12.5px',
                      color: indexUrl ? '#374151' : '#B45309',
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
                  sx={{ textTransform: 'none', fontSize: '12px', color: '#4F46E5', flexShrink: 0 }}
                >
                  {editingSource ? '收起' : '配置'}
                </Button>
                <Button
                  size="small"
                  startIcon={<RefreshIcon sx={{ fontSize: 15 }} />}
                  disabled={!indexUrl || listing}
                  onClick={() => void loadList()}
                  sx={{ textTransform: 'none', fontSize: '12px', color: '#4F46E5', flexShrink: 0 }}
                >
                  刷新
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
                    <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', flexGrow: 1 }}>
                      远程索引只接受 HTTPS；下载内容会按 SHA-256 校验，带签名的条目还会校验 Ed25519 发布者签名。
                    </Typography>
                    <Button
                      variant="contained"
                      size="small"
                      disabled={saving}
                      onClick={() => void handleSaveSource()}
                      sx={{
                        bgcolor: '#111827',
                        borderRadius: '9px',
                        textTransform: 'none',
                        fontSize: '12px',
                        '&:hover': { bgcolor: '#4F46E5' },
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
                    border: '1px solid #EEF0F2',
                    background: '#FCFCFD',
                  }}
                >
                  <Typography sx={{ fontSize: '12.5px', fontWeight: 700, color: '#374151' }}>
                    发布者信任
                  </Typography>
                  <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.25 }}>
                    {signaturePolicy === 'required'
                      ? '该索引要求所有条目带发布者签名；未信任的发布者不能安装。'
                      : '签名可选；信任后安装会校验 Ed25519 签名，发布者换钥需要重新确认。'}
                  </Typography>
                  {publishers.length === 0 ? (
                    <Typography sx={{ fontSize: '11.5px', color: '#B45309', mt: 0.75 }}>
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
                          borderTop: '1px solid #F1F3F5',
                        }}
                      >
                        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                          <Typography sx={{ fontSize: '12px', fontWeight: 600, color: '#374151' }}>
                            {publisher.name || publisher.id}
                          </Typography>
                          <Typography
                            sx={{
                              fontSize: '10.5px',
                              color: '#9CA3AF',
                              fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                              overflow: 'hidden',
                              textOverflow: 'ellipsis',
                              whiteSpace: 'nowrap',
                            }}
                          >
                            {publisher.id} · 指纹 {shortFingerprint(publisher.fingerprint)}
                          </Typography>
                          {publisher.keyChanged && (
                            <Typography sx={{ fontSize: '10.5px', color: '#B45309', mt: 0.2 }}>
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
                            bgcolor: publisher.trusted ? '#E6F7F4' : publisher.keyChanged ? '#FEF3C7' : '#F3F4F6',
                            color: publisher.trusted ? '#0D9488' : publisher.keyChanged ? '#B45309' : '#6B7280',
                          }}
                        />
                        {publisher.trusted ? (
                          <Button
                            size="small"
                            disabled={trustBusy === publisher.id}
                            onClick={() => void handleTrust(publisher, true)}
                            sx={{ textTransform: 'none', fontSize: '11.5px', color: '#B91C1C', flexShrink: 0 }}
                          >
                            取消信任
                          </Button>
                        ) : publisher.keyChanged ? (
                          <Button
                            size="small"
                            disabled={trustBusy === publisher.id}
                            onClick={() => void handleTrust(publisher, true)}
                            sx={{ textTransform: 'none', fontSize: '11.5px', color: '#B45309', flexShrink: 0 }}
                          >
                            移除旧信任
                          </Button>
                        ) : (
                          <Button
                            size="small"
                            disabled={trustBusy === publisher.id}
                            onClick={() => void handleTrust(publisher, false)}
                            sx={{ textTransform: 'none', fontSize: '11.5px', color: '#4F46E5', flexShrink: 0 }}
                          >
                            {trustBusy === publisher.id ? '保存中…' : '信任'}
                          </Button>
                        )}
                      </Box>
                    ))
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
                  <Typography sx={{ fontSize: '13px', color: '#6B7280' }}>
                    先填写{text.noun}市场索引地址，再拉取可安装条目。
                  </Typography>
                </Box>
              ) : items.length === 0 ? (
                <Box sx={{ py: 4, textAlign: 'center' }}>
                  <Typography sx={{ fontSize: '13px', color: '#6B7280' }}>
                    索引已连接，但里面没有{text.noun}条目。
                  </Typography>
                  {list && (
                    <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF', mt: 0.5 }}>
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
                          border: '1px solid #EEF0F2',
                          '&:hover': { borderColor: '#D9DCE1' },
                        }}
                      >
                        <Box sx={{ flexGrow: 1, minWidth: 0 }}>
                          <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 1, flexWrap: 'wrap' }}>
                            <Typography sx={{ fontSize: '13.5px', fontWeight: 700, color: '#111827' }}>
                              {item.name}
                            </Typography>
                            {item.version && (
                              <Chip
                                size="small"
                                label={item.version}
                                sx={{ height: 18, fontSize: '10.5px', bgcolor: '#F3F4F6', color: '#6B7280' }}
                              />
                            )}
                            {item.author && (
                              <Typography sx={{ fontSize: '11.5px', color: '#9CA3AF' }}>{item.author}</Typography>
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
                                  bgcolor: item.publisherTrusted ? '#E6F7F4' : item.publisherKeyChanged ? '#FEF3C7' : '#F3F4F6',
                                  color: item.publisherTrusted ? '#0D9488' : item.publisherKeyChanged ? '#B45309' : '#6B7280',
                                }}
                              />
                            ) : (
                              <Chip
                                size="small"
                                label="未签名"
                                sx={{ height: 18, fontSize: '10.5px', bgcolor: '#F3F4F6', color: '#9CA3AF' }}
                              />
                            )}
                          </Box>
                          {item.description && (
                            <Typography
                              sx={{
                                fontSize: '12px',
                                color: '#6B7280',
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
                          <Typography sx={{ fontSize: '11px', color: '#9CA3AF', mt: 0.35 }}>
                            {item.id}
                            {size ? ` · ${size}` : ''}
                          </Typography>
                        </Box>
                        {item.installed ? (
                          <Box sx={{ px: 1.25, py: 0.35, borderRadius: '8px', background: '#E6F7F4', flexShrink: 0 }}>
                            <Typography sx={{ fontSize: '11.5px', fontWeight: 600, color: '#0D9488' }}>
                              已安装
                            </Typography>
                          </Box>
                        ) : (
                          <Button
                            size="small"
                            variant="contained"
                            startIcon={busy ? <CircularProgress size={13} sx={{ color: '#fff' }} /> : <DownloadIcon sx={{ fontSize: 15 }} />}
                            disabled={Boolean(installingId) || blockedByTrust}
                            title={blockedByTrust ? '请先在「发布者信任」中确认并信任该发布者' : undefined}
                            onClick={() => void handleInstall(item)}
                            sx={{
                              bgcolor: '#111827',
                              borderRadius: '9px',
                              textTransform: 'none',
                              fontSize: '12px',
                              fontWeight: 600,
                              flexShrink: 0,
                              '&:hover': { bgcolor: '#4F46E5' },
                            }}
                          >
                            {busy ? '安装中…' : blockedByTrust ? '先信任发布者' : '安装'}
                          </Button>
                        )}
                      </Box>
                    );
                  })}
                  {list && (
                    <Typography sx={{ fontSize: '11px', color: '#B5BAC1', mt: 0.5 }}>
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
