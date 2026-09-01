import React, { useState } from 'react';
import {
  Card,
  CardContent,
  Button,
  Typography,
  List,
  ListItem,
  ListItemText,
  ListItemIcon,
  Chip,
  Box,
  Divider,
  Alert,
  TextField,
  Accordion,
  AccordionSummary,
  AccordionDetails,
} from '@mui/material';
import ExpandMoreIcon from '@mui/icons-material/ExpandMore';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import CancelIcon from '@mui/icons-material/Cancel';
import PendingIcon from '@mui/icons-material/Pending';
import PlayArrowIcon from '@mui/icons-material/PlayArrow';
import { Plan, Step, PlanStatus, StepType, StepStatus } from '../../../shared/types';

interface PlanViewProps {
  plan: Plan;
  /** 审阅动作：approve/reject/modify（两阶段协议转移 #9/#10/#11） */
  onReview: (action: 'approve' | 'reject' | 'modify', patch?: Partial<Step>[]) => void;
  onModify?: (stepId: string, changes: Partial<Step>) => void;
}

const PlanView: React.FC<PlanViewProps> = ({ plan, onReview, onModify }) => {
  const [expandedStep, setExpandedStep] = useState<string | null>(null);
  const [modifyNotes, setModifyNotes] = useState<Record<string, string>>({});

  const getStatusIcon = (status: StepStatus) => {
    switch (status) {
      case StepStatus.SUCCESS:
        return <CheckCircleIcon color="success" />;
      case StepStatus.FAILED:
        return <CancelIcon color="error" />;
      case StepStatus.RUNNING:
        return <PendingIcon color="warning" />;
      default:
        return <PendingIcon sx={{ color: 'text.disabled' }} />;
    }
  };

  const getStepTypeLabel = (type: StepType): string => {
    const labels: Record<StepType, string> = {
      [StepType.LLM_CALL]: 'AI 调用',
      [StepType.FILE_READ]: '读取文件',
      [StepType.FILE_WRITE]: '写入文件',
      [StepType.FILE_CONVERT]: '格式转换',
      [StepType.FILE_ORGANIZE]: '文件整理',
      [StepType.USER_CONFIRM]: '用户确认',
      [StepType.SKILL_CALL]: '技能调用',
      [StepType.MCP_TOOL]: 'MCP 工具',
      [StepType.WORKSPACE_DELEGATE]: '工作区委派',
    };
    return labels[type] || type;
  };

  const handleApprove = () => {
    onReview('approve');
  };

  const handleReject = () => {
    onReview('reject');
  };

  /** 提交修改（modify：携带 patch 重新生成计划，交互补设计 §5.2，不允许新增步骤） */
  const handleModify = () => {
    const patch = Object.entries(modifyNotes)
      .filter(([_, note]) => note.trim())
      .map(([stepId, note]) => ({
        id: stepId,
        name: `[修改] ${note}`,
      })) as Partial<Step>[];
    if (patch.length === 0) return;
    onReview('modify', patch);
  };

  const hasModifyNotes = Object.values(modifyNotes).some((n) => n.trim());

  const totalSteps = plan.steps.length;
  const completedSteps = plan.steps.filter((s: Step) => s.status === StepStatus.SUCCESS).length;
  const failedSteps = plan.steps.filter((s: Step) => s.status === StepStatus.FAILED).length;

  return (
    <Card variant="outlined" sx={{ mb: 3 }}>
      <CardContent>
        {/* 计划头部 */}
        <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mb: 2 }}>
          <Typography variant="h6" component="div">
            📋 执行计划
          </Typography>
          <Chip
            label={plan.status}
            size="small"
            color={plan.reviewed ? 'success' : 'default'}
            variant="outlined"
          />
        </Box>

        {/* 统计信息 */}
        <Box sx={{ display: 'flex', gap: 2, mb: 2, flexWrap: 'wrap' }}>
          <Chip label={`总步骤: ${totalSteps}`} size="small" />
          <Chip label={`已完成: ${completedSteps}`} size="small" color="success" variant="outlined" />
          {failedSteps > 0 && (
            <Chip label={`失败: ${failedSteps}`} size="small" color="error" variant="outlined" />
          )}
          <Chip label={`完成率: ${totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : 0}%`} size="small" />
        </Box>

        <Divider sx={{ my: 2 }} />

        {/* 步骤列表 */}
        <List sx={{ maxHeight: 400, overflow: 'auto' }}>
          {plan.steps.map((step, index) => (
            <React.Fragment key={step.id}>
              <Accordion
                expanded={expandedStep === step.id}
                onChange={() => setExpandedStep(expandedStep === step.id ? null : step.id)}
                disableGutters
              >
                <AccordionSummary expandIcon={<ExpandMoreIcon />}>
                  <Box sx={{ display: 'flex', alignItems: 'center', width: '100%' }}>
                    <ListItemIcon sx={{ minWidth: 40 }}>
                      {getStatusIcon(step.status)}
                    </ListItemIcon>
                    <Box sx={{ ml: 1, flex: 1 }}>
                      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                        <Typography variant="body2" fontWeight="medium">
                          {index + 1}. {step.name}
                        </Typography>
                        <Chip
                          label={getStepTypeLabel(step.type)}
                          size="small"
                          variant="outlined"
                          sx={{ fontSize: '0.7rem', height: 20 }}
                        />
                      </Box>
                      {step.progress && (
                        <Typography variant="caption" color="text.secondary" sx={{ mt: 0.5 }}>
                          {step.progress.substring(0, 100)}...
                        </Typography>
                      )}
                    </Box>
                    <Chip
                      label={step.status}
                      size="small"
                      color={
                        step.status === StepStatus.SUCCESS
                          ? 'success'
                          : step.status === StepStatus.FAILED
                          ? 'error'
                          : step.status === StepStatus.RUNNING
                          ? 'warning'
                          : 'default'
                      }
                      variant="filled"
                      sx={{ ml: 1, fontSize: '0.7rem', height: 22 }}
                    />
                  </Box>
                </AccordionSummary>
                <AccordionDetails>
                  <Typography variant="caption" color="text.secondary" display="block">
                    输入参数: {JSON.stringify(step.input, null, 2).substring(0, 200)}...
                  </Typography>
                  {step.result && (
                    <Box sx={{ mt: 1 }}>
                      <Typography variant="caption" fontWeight="medium">
                        结果:
                      </Typography>
                      <Typography variant="caption" color="text.secondary" display="block">
                        {step.result.substring(0, 500)}
                      </Typography>
                    </Box>
                  )}
                  {onModify && modifyNotes[step.id] !== undefined && (
                    <TextField
                      fullWidth
                      size="small"
                      label="修改意见"
                      value={modifyNotes[step.id]}
                      onChange={(e) => setModifyNotes({ ...modifyNotes, [step.id]: e.target.value })}
                      sx={{ mt: 1 }}
                    />
                  )}
                </AccordionDetails>
              </Accordion>
              {index < plan.steps.length - 1 && <Divider />}
            </React.Fragment>
          ))}
        </List>

        {/* 操作按钮 */}
        {plan.status === PlanStatus.PENDING_REVIEW && (
          <Box sx={{ display: 'flex', gap: 2, mt: 3, justifyContent: 'flex-end' }}>
            <Button
              variant="outlined"
              color="error"
              onClick={handleReject}
              startIcon={<CancelIcon />}
            >
              返回修改
            </Button>
            <Button
              variant="outlined"
              color="warning"
              onClick={handleModify}
              disabled={!hasModifyNotes}
            >
              提交修改
            </Button>
            <Button
              variant="contained"
              color="success"
              onClick={handleApprove}
              startIcon={<CheckCircleIcon />}
              disabled={plan.reviewed}
            >
              批准并执行
            </Button>
          </Box>
        )}

        {/* 警告信息 */}
        {failedSteps > 0 && (
          <Alert severity="error" sx={{ mt: 2 }}>
            有 {failedSteps} 个步骤执行失败，请检查错误后重试或回滚。
          </Alert>
        )}
      </CardContent>
    </Card>
  );
};

export default PlanView;
