/**
 * 应用快照与浏览器上下文接力（缺陷 D-13 · 任务 #140）
 *
 * 这个面板只做三件界面必须做的事：
 * 1. **把引擎的口径原样摊开**——上限、状态机、三档分级、四类拒绝来源、未验收清单，
 *    全部读自 `/api/snapshot/status`；前端不另立一套「支持什么」的清单。
 * 2. **让用户在正文离开本机之前看一眼**——预览与上送载荷由引擎同一个函数产出，
 *    这里显示什么，确认后就发什么。
 * 3. **把闸说出来**——机密级没有绕过开关（连「带上截图」都改不了结论），内部敏感必须
 *    显式勾选确认；取消即销毁，之后确认也捞不回来。
 *
 * 三条边界，界面上不许含糊：
 * - **本面板不截屏**。正文与图片由调用方交进来（真实屏幕捕获、前台应用识别、全局快捷键
 *   都在桌面壳里，workspace 车道的 IPC 未接线 ⇒ 真机采集未验收）；
 * - **正文不留副本**。预览文本只活在组件的内存态里，不写 localStorage、不进日志，
 *   取消/确认/换一份草稿时立刻清掉；
 * - **接力不碰浏览器**。这里只问「这次能不能复用已授权会话」，开/切/关标签页归 W5 的
 *   `/api/browser`，Cookie 与登录态本模块既不持有也不导出。
 *
 * 样式沿用全局 CSS 变量令牌（`var(--tt-*)`）与 MUI 主题，不引入第二套颜色来源。
 */

import React, { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Alert,
  Box,
  Button,
  Chip,
  CircularProgress,
  Divider,
  FormControlLabel,
  Checkbox,
  List,
  ListItem,
  ListItemText,
  TextField,
  Tooltip,
  Typography,
} from '@mui/material';
import {
  RELAY_DENIAL_LABELS,
  SENSITIVITY_LABELS,
  STATUS_LABELS,
  labelOf,
  type RelayGrantRow,
  type SnapshotDraftRow,
  type SnapshotPreview,
  type SnapshotStatusView,
} from '../../../shared/snapshot-contract';
import { errorTextOf } from '../../../shared/error-messages';
import { KitCard, KitChip, KitEmptyNote, KitFieldRow, KitRow, KitSectionTitle, type KitChipTone } from '../DesignKit';
import {
  cancelSnapshot,
  captureSnapshot,
  confirmSnapshot,
  decideRelay,
  getRelayStatus,
  getSnapshotPreview,
  getSnapshotStatus,
  grantRelay,
  isDesktopOnlyRejection,
  isSnapshotApiError,
  listPendingSnapshots,
  revokeRelay,
  snapshotUnverifiedViews,
} from '../../lib/snapshot-api';

/**
 * 三档分级 → 组件库色调：这一层只做「引擎的档位」到「库里的档」的映射，
 * 具体哪块底配哪支字由 DesignKit 成对给（底与字拆开写就会在某个皮肤下糊掉）。
 */
const SENSITIVITY_TONE: Record<string, KitChipTone> = {
  public: 'success',
  internal: 'warning',
  confidential: 'danger',
};

function sensitivityTone(value: string | undefined): KitChipTone {
  return SENSITIVITY_TONE[value ?? ''] ?? 'neutral';
}

/** 引擎的句子是给人看的，代码留在 code 里；界面把两句分开摆，不互相覆盖 */
function failText(error: unknown, fallback: string): string {
  if (isSnapshotApiError(error)) return error.message;
  return errorTextOf(error, fallback);
}

function failCode(error: unknown): string {
  return isSnapshotApiError(error) ? error.code : '';
}

interface AppSnapshotProps {
  /** 快照与接力都按工作区/会话隔离；不传就用引擎的缺省口径 */
  workspaceId?: string;
  session?: string;
}

const AppSnapshot: React.FC<AppSnapshotProps> = ({ workspaceId, session }) => {
  const [view, setView] = useState<SnapshotStatusView | null>(null);
  const [preview, setPreview] = useState<SnapshotPreview | null>(null);
  const [pending, setPending] = useState<SnapshotDraftRow[]>([]);
  const [grants, setGrants] = useState<RelayGrantRow[]>([]);
  const [draftText, setDraftText] = useState('');
  const [appLabel, setAppLabel] = useState('');
  const [hostInput, setHostInput] = useState('');
  const [busy, setBusy] = useState('');
  const [error, setError] = useState<{ text: string; code: string } | null>(null);
  const [notice, setNotice] = useState('');
  const [includeImage, setIncludeImage] = useState(false);
  const [ackInternal, setAckInternal] = useState(false);
  const [desktopOnly, setDesktopOnly] = useState(false);
  const [loaded, setLoaded] = useState(false);

  const reload = useCallback(async () => {
    setError(null);
    setDesktopOnly(false);
    try {
      const [status, relayStatus, list] = await Promise.all([
        getSnapshotStatus(workspaceId),
        getRelayStatus(workspaceId),
        listPendingSnapshots({ session }, workspaceId),
      ]);
      setView(status);
      setGrants(relayStatus.grants ?? []);
      setPending(list.items ?? []);
      setLoaded(true);
    } catch (e) {
      if (isDesktopOnlyRejection(e)) setDesktopOnly(true);
      setError({ text: failText(e, '快照口径读取失败'), code: failCode(e) });
    }
  }, [session, workspaceId]);

  useEffect(() => {
    void reload();
    // 引擎重启后草稿会全没（正文只在进程内存里活到 TTL）：一次读取，不轮询
  }, [reload]);

  const run = useCallback(
    async (label: string, action: () => Promise<void>): Promise<void> => {
      setBusy(label);
      setError(null);
      setNotice('');
      try {
        await action();
      } catch (e) {
        setError({ text: failText(e, `${label}失败`), code: failCode(e) });
      } finally {
        setBusy('');
      }
    },
    [],
  );

  const onCapture = () =>
    run('递交采集物', async () => {
      const row = await captureSnapshot({ text: draftText, appLabel, session }, workspaceId);
      // 预览即上送内容：这里不做二次加工，也不留第二份文本
      setPreview(row);
      setAckInternal(false);
      setIncludeImage(false);
    });

  const onOpenDraft = (draftId: string) =>
    run('打开预览', async () => {
      setPreview(await getSnapshotPreview(draftId, workspaceId));
      setAckInternal(false);
    });

  const onConfirm = () =>
    run('确认上送', async () => {
      const row = await confirmSnapshot(
        {
          draftId: preview?.draftId ?? '',
          includeImage,
          acknowledgeInternal: ackInternal,
          session,
        },
        workspaceId,
      );
      // 载荷交给调用方之后，面板不留正文：确认完的这份只剩审计里的一行元数据
      setPreview(null);
      setDraftText('');
      setNotice(row.note || '已确认：上送载荷由引擎产出，本面板不缓存正文。');
      await reload();
    });

  const onCancel = () =>
    run('取消并销毁', async () => {
      const row = await cancelSnapshot({ draftId: preview?.draftId ?? '' }, workspaceId);
      setPreview(null);
      setDraftText('');
      setNotice(row.cancelNote || '已取消：这份草稿连同正文与图片一起销毁，之后再确认也不会捞回来。');
      await reload();
    });

  const onGrant = () =>
    run('开一次接力授权', async () => {
      const hosts = hostInput
        .split(/[\s,]+/)
        .map((item) => item.trim())
        .filter(Boolean);
      const decision = await grantRelay({ workspaceId: workspaceId ?? '', hosts, session }, workspaceId);
      setHostInput('');
      setNotice(decision.message || `已授权 ${decision.grant?.hosts?.length ?? 0} 个域`);
      await reload();
    });

  const onRevoke = (grantId: string) =>
    run('撤销授权', async () => {
      const decision = await revokeRelay({ grantId, reason: '在快照面板手动撤销' }, workspaceId);
      setNotice(decision.message || '已撤销');
      await reload();
    });

  const onDecide = (grantId: string) =>
    run('判定一次复用', async () => {
      const decision = await decideRelay({ grantId, want: 'tabs', session }, workspaceId);
      const who = decision.deniedBy ? labelOf(RELAY_DENIAL_LABELS, decision.deniedBy) : '';
      setNotice(decision.allowed ? '可以复用这条授权里的标签页' : `${who}：${decision.message}`);
    });

  const unverified = useMemo(() => (view ? snapshotUnverifiedViews(view) : { snapshot: [], relay: [] }), [view]);

  const limits = view?.snapshot;

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <KitCard>
        <KitSectionTitle title="应用快照（D-13）" />
        <Typography variant="body2" sx={{ color: 'var(--tt-text-3)' }}>
          触发 → 预览 → 确认 → 上送：预览文本与上送载荷由引擎同一个函数产出，
          这里看到什么就发什么。本面板不截屏，正文与截图由调用方交进来。
        </Typography>
        {limits && (
          <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mt: 1.5 }}>
            <KitChip>{`待确认 ${limits.pending} / 内存中 ${limits.drafts}`}</KitChip>
            <KitChip>{`上送正文上限 ${limits.maxUplinkTextChars} 字`}</KitChip>
            <KitChip>{`图片上限 ${Math.round(limits.maxImageBytes / 1024 / 1024)} MiB`}</KitChip>
            <KitChip>{`TTL ${limits.ttlMinutes} 分钟`}</KitChip>
          </Box>
        )}
        {desktopOnly && (
          <Alert severity="warning" sx={{ mt: 1.5 }}>
            快照只对本机桌面壳开放：当前身份不是桌面本机，引擎不会把屏幕内容与域名清单交给远程调用方。
          </Alert>
        )}
      </KitCard>

      {error && (
        <Alert severity="error" onClose={() => setError(null)}>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
            {error.code && <Chip size="small" label={error.code} />}
            <Typography variant="body2">{error.text}</Typography>
          </Box>
        </Alert>
      )}
      {notice && (
        <Alert severity="info" onClose={() => setNotice('')}>
          {notice}
        </Alert>
      )}

      <KitCard>
        <KitSectionTitle title="1 · 采集物" />
        <TextField
          multiline
          minRows={3}
          fullWidth
          size="small"
          label="正文（由你或调用方交进来，不是本面板截的屏）"
          value={draftText}
          onChange={(e) => setDraftText(e.target.value)}
          inputProps={{ maxLength: limits?.maxUplinkTextChars }}
        />
        <Box sx={{ display: 'flex', gap: 1, mt: 1, alignItems: 'center', flexWrap: 'wrap' }}>
          <TextField
            size="small"
            label="来源应用名（可留空）"
            value={appLabel}
            onChange={(e) => setAppLabel(e.target.value)}
            sx={{ width: 220 }}
          />
          <Button
            variant="contained"
            size="small"
            onClick={() => void onCapture()}
            disabled={Boolean(busy) || !draftText.trim()}
            startIcon={busy === '递交采集物' ? <CircularProgress size={14} /> : undefined}
          >
            触发分级与预览
          </Button>
          <Typography variant="caption" sx={{ color: 'var(--tt-text-4)' }}>
            {draftText.length} / {limits?.maxUplinkTextChars ?? '—'} 字
          </Typography>
        </Box>
      </KitCard>

      {preview && (
        <KitCard>
          <KitSectionTitle
            title="2 · 上送前可见"
            right={
              <KitChip tone={sensitivityTone(preview.classification?.sensitivity)}>
                {labelOf(SENSITIVITY_LABELS, preview.classification?.sensitivity ?? '')}
              </KitChip>
            }
          />
          <Box
            sx={{
              p: 1.5,
              borderRadius: '10px',
              bgcolor: 'var(--tt-surface-2)',
              border: '1px solid var(--tt-border-soft)',
              whiteSpace: 'pre-wrap',
              fontFamily: 'ui-monospace, Consolas, monospace',
              fontSize: '0.82rem',
              color: 'var(--tt-text-2)',
              maxHeight: 220,
              overflow: 'auto',
            }}
          >
            {preview.preview}
          </Box>
          <Box sx={{ display: 'flex', gap: 1, mt: 1, flexWrap: 'wrap', alignItems: 'center' }}>
            <Chip size="small" label={labelOf(STATUS_LABELS, preview.status)} variant="outlined" />
            <Chip size="small" label={`${preview.textChars} 字`} variant="outlined" />
            {preview.truncated && <Chip size="small" label="已按上限截断" color="warning" />}
            {preview.hasImage && (
              <Chip size="small" label={`图片 ${Math.round((preview.imageBytes ?? 0) / 1024)} KB（默认不上送）`} variant="outlined" />
            )}
            {preview.redactionMode && preview.redactionMode !== 'off' && (
              <Chip size="small" label={`脱敏 ${preview.redactionMode}`} variant="outlined" />
            )}
          </Box>
          {(preview.classification?.findings ?? []).length > 0 && (
            <Box sx={{ mt: 1 }}>
              {preview.classification.findings.map((finding, index) => (
                <Typography key={`${finding.kind}-${index}`} variant="caption" sx={{ display: 'block', color: 'var(--tt-text-3)' }}>
                  · {finding.description || finding.kind}（{finding.pattern}）
                </Typography>
              ))}
            </Box>
          )}
          {!preview.wouldUplink && (
            <Alert severity="error" sx={{ mt: 1.5 }}>
              这份被引擎判为机密，确认也不会发出去——机密级没有绕过开关，勾选「带上截图」改不了结论。
            </Alert>
          )}
          {preview.needsAck && (
            <Alert severity="warning" sx={{ mt: 1.5 }}>
              内部敏感需要显式确认才允许上送；不勾选时确认会原样退回这条要求。
            </Alert>
          )}
          <Box sx={{ display: 'flex', gap: 2, mt: 1.5, flexWrap: 'wrap', alignItems: 'center' }}>
            <FormControlLabel
              control={<Checkbox checked={includeImage} onChange={(e) => setIncludeImage(e.target.checked)} size="small" />}
              label={<Typography variant="body2">带上截图（图片无法本地检测，风险由确认人承担）</Typography>}
            />
            <FormControlLabel
              control={<Checkbox checked={ackInternal} onChange={(e) => setAckInternal(e.target.checked)} size="small" />}
              label={<Typography variant="body2">我确认这份内部敏感可以离开本机</Typography>}
            />
          </Box>
          <Box sx={{ display: 'flex', gap: 1, mt: 1.5 }}>
            <Button
              size="small"
              variant="outlined"
              color="error"
              onClick={() => void onCancel()}
              disabled={Boolean(busy)}
            >
              取消并销毁
            </Button>
            <Button
              size="small"
              variant="contained"
              onClick={() => void onConfirm()}
              disabled={Boolean(busy) || !preview.wouldUplink}
              startIcon={busy === '确认上送' ? <CircularProgress size={14} /> : undefined}
            >
              确认上送
            </Button>
          </Box>
          <Typography variant="caption" sx={{ display: 'block', mt: 1, color: 'var(--tt-text-4)' }}>
            {preview.storageNote}
          </Typography>
        </KitCard>
      )}

      <KitCard>
        <KitSectionTitle
          title="待确认清单（只有元数据，列表不是预览）"
          right={
            <Tooltip title="引擎只把待确认的那几份给这里；已确认/已取消/已过期的墓碑要看全量请显式带 status=all">
              <Button size="small" onClick={() => void reload()} disabled={Boolean(busy)}>
                刷新
              </Button>
            </Tooltip>
          }
        />
        {!loaded ? (
          <KitEmptyNote title="正在读取…" lines={[]} />
        ) : pending.length === 0 ? (
          <KitEmptyNote
            title="没有待确认的快照草稿"
            lines={['引擎重启或超过 TTL 后草稿会消失——正文只活在引擎进程内存里，不落盘。']}
          />
        ) : (
          <List dense sx={{ '& .MuiListItem-root': { px: 0 } }}>
            {pending.map((row) => (
              <ListItem key={row.draftId} disableGutters divider sx={{ py: 0.75 }}>
                <ListItemText
                  primary={
                    <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                      <Typography variant="body2">{row.appLabel || '未标注来源'}</Typography>
                      <Chip size="small" label={`${row.textChars} 字`} variant="outlined" />
                      <KitChip tone={sensitivityTone(row.classification?.sensitivity)}>
                        {labelOf(SENSITIVITY_LABELS, row.classification?.sensitivity ?? '')}
                      </KitChip>
                    </Box>
                  }
                  secondary={<KitFieldRow label="状态" value={`${labelOf(STATUS_LABELS, row.status)} · 清单不含正文`} />}
                />
                <Button size="small" onClick={() => void onOpenDraft(row.draftId)} disabled={Boolean(busy)}>
                  看预览
                </Button>
              </ListItem>
            ))}
          </List>
        )}
      </KitCard>

      <KitCard>
        <KitSectionTitle title="3 · 浏览器上下文接力（只判定，不操作浏览器）" />
        <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
          <TextField
            size="small"
            label="授权域名（空格或逗号分隔）"
            value={hostInput}
            onChange={(e) => setHostInput(e.target.value)}
            sx={{ width: 300 }}
            helperText={`引擎上限 ${view?.relay.maxHostsPerGrant ?? '—'} 个域 / 条；指向日常浏览器目录的授权开不出来`}
          />
          <Button size="small" variant="outlined" onClick={() => void onGrant()} disabled={Boolean(busy) || !hostInput.trim()}>
            开一次授权
          </Button>
        </Box>
        {grants.length === 0 ? (
          <Box sx={{ mt: 1.5 }}>
            <KitEmptyNote
              title="当前没有接力授权"
              lines={['这里没有 Cookie、没有令牌、没有完整路径：只有域名、配置目录指纹与结论。']}
            />
          </Box>
        ) : (
          <List dense sx={{ '& .MuiListItem-root': { px: 0 } }}>
            {grants.map((row) => (
              <ListItem key={row.grantId} disableGutters divider sx={{ py: 0.75 }}>
                <ListItemText
                  primary={
                    <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
                      <Typography variant="body2" sx={{ fontFamily: 'ui-monospace, Consolas, monospace' }}>
                        {row.grantId}
                      </Typography>
                      <Chip size="small" label={row.expired ? '已过期' : row.revoked ? '已撤销' : '有效'} variant="outlined" />
                      <Chip size="small" label={`${row.hosts.length} 个域`} variant="outlined" />
                      <Chip size="small" label={`指纹 ${row.profileFingerprint || '—'}`} variant="outlined" />
                    </Box>
                  }
                  secondary={
                    <>
                      {row.hosts.join(' · ') || '（还没有域名）'}
                      {row.tabCount > 0 && ` · 已登记 ${row.tabCount} 个标签页：${row.tabs.map((tab) => tab.host).join(' · ')}`}
                      {row.revokeNote && <Box component="span" sx={{ display: 'block', color: 'var(--tt-danger)' }}>撤销：{row.revokeNote}</Box>}
                    </>
                  }
                />
                <Box sx={{ display: 'flex', gap: 1 }}>
                  <Button size="small" onClick={() => void onDecide(row.grantId)} disabled={Boolean(busy)}>
                    判定
                  </Button>
                  <Button size="small" color="error" onClick={() => void onRevoke(row.grantId)} disabled={Boolean(busy)}>
                    撤销
                  </Button>
                </Box>
              </ListItem>
            ))}
          </List>
        )}
      </KitCard>

      <KitCard>
        <KitSectionTitle title="口径与未验收（读自引擎，前端不另立清单）" />
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', mb: 1 }}>
          {(view?.snapshot.sensitivityLevels ?? []).map((item) => (
            <KitChip key={item.value} tone={sensitivityTone(item.value)}>
              {item.label}
            </KitChip>
          ))}
          <Divider orientation="vertical" flexItem sx={{ mx: 0.5 }} />
          {(view?.relay.denialSources ?? []).map((item) => (
            <Chip key={item.value} size="small" variant="outlined" label={item.label} />
          ))}
        </Box>
        {[...unverified.snapshot, ...unverified.relay].map((item, index) => (
          <KitRow key={index} align="flex-start">
            <Typography variant="caption" sx={{ color: 'var(--tt-text-3)' }}>
              未验收 · {item}
            </Typography>
          </KitRow>
        ))}
      </KitCard>
    </Box>
  );
};

// 待确认清单只由父组件读一次：列表面不回正文，多读一次只会多一份元数据副本。

export default AppSnapshot;
