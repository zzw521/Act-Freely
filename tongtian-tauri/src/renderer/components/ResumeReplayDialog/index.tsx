/**
 * 续跑前的重放预览（D-15）。
 *
 * 检查点续跑不是「回到我点的那一步」，而是回到最后一次模型调用前的快照，
 * 引擎再按「工具名 + 输入指纹」决定哪些已成功的写入调用不重做。
 * 界面把这份近似摊开给用户看，列出会重做的写操作并要求勾选确认后才发起。
 */

import React, { useEffect, useState } from 'react';
import {
  Box,
  Typography,
  Chip,
  Divider,
  Dialog,
  DialogTitle,
  DialogContent,
  DialogActions,
  Button,
  Alert,
  FormControlLabel,
  Checkbox,
} from '@mui/material';
import type { ChatEvent } from '../../../shared/types';
import { buildReplayPlan, confirmWarningZh, replayRiskLabel } from '../../../shared/step-replay';
import { useI18n } from '../../i18n';

interface ResumeReplayDialogProps {
  open: boolean;
  /** 锚点所在轮次的完整事件流：预览从这里还原步骤序列 */
  events: ChatEvent[];
  /** 本次续跑的锚点步骤；空串表示本轮已没有步骤记录，只能裸续跑 */
  anchorStepId: string;
  onClose: () => void;
  onConfirm: (anchorStepId: string) => void;
}

const ResumeReplayDialog: React.FC<ResumeReplayDialogProps> = ({ open, events, anchorStepId, onClose, onConfirm }) => {
  const { t: tr } = useI18n();
  const [acknowledged, setAcknowledged] = useState(false);
  // 每次打开都重新要求勾选：确认只对它当时看到的那份清单有效
  useEffect(() => {
    if (open) setAcknowledged(false);
  }, [open]);

  const plan = open ? buildReplayPlan(events, anchorStepId) : null;

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle>{tr('续跑前先确认将要重做的步骤')}</DialogTitle>
      <DialogContent dividers>
        <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-2)', mb: 1 }}>
          {plan?.summaryZh}
        </Typography>
        <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.75 }}>
          {(plan?.steps ?? []).map((step) => (
            <Box key={`${step.stepId}-${step.order}`} sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
              <Chip
                size="small"
                label={
                  step.kind === 'other'
                    ? tr('内部步骤')
                    : step.willReplay
                      ? tr('会重做')
                      : tr('不重做')
                }
                color={step.kind === 'other' ? 'default' : step.willReplay ? 'warning' : 'success'}
                sx={{ fontSize: '11px', height: 20, flexShrink: 0 }}
              />
              <Box sx={{ minWidth: 0 }}>
                <Typography sx={{ fontSize: '12.5px', color: 'var(--tt-text-2)' }}>
                  {step.order + 1}. {step.label}
                  {step.kind === 'tool' ? `（${replayRiskLabel(step.risk)}）` : ''}
                </Typography>
                <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>{step.note}</Typography>
              </Box>
            </Box>
          ))}
        </Box>
        <Divider sx={{ my: 1.25 }} />
        <Typography sx={{ fontSize: '11.5px', color: 'var(--tt-text-4)' }}>
          {plan?.caveatZh}
        </Typography>
        {plan?.needsConfirm && (
          <Alert severity="warning" sx={{ mt: 1.25 }}>
            <Typography sx={{ fontSize: '12.5px' }}>
              {confirmWarningZh(plan.confirmTools)}
            </Typography>
            <FormControlLabel
              control={
                <Checkbox
                  size="small"
                  checked={acknowledged}
                  onChange={(_event, checked) => setAcknowledged(checked)}
                />
              }
              label={
                <Typography sx={{ fontSize: '12.5px' }}>{tr('我已知悉上面列出的操作会再执行一次')}</Typography>
              }
            />
          </Alert>
        )}
      </DialogContent>
      <DialogActions>
        <Button onClick={onClose} sx={{ textTransform: 'none' }}>
          {tr('取消')}
        </Button>
        <Button
          variant="contained"
          disabled={Boolean(plan?.needsConfirm) && !acknowledged}
          onClick={() => {
            if (!plan) return;
            onConfirm(plan.anchorStepId);
          }}
          sx={{ textTransform: 'none' }}
        >
          {tr('从检查点续跑')}
        </Button>
      </DialogActions>
    </Dialog>
  );
};

export default ResumeReplayDialog;
