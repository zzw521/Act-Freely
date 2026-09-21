import React, { useState } from 'react';
import {
  Box,
  Card,
  CardContent,
  Typography,
  Chip,
  LinearProgress,
  Alert,
  Divider,
  Stack,
  Button,
  IconButton,
  Tooltip,
} from '@mui/material';
import RefreshIcon from '@mui/icons-material/Refresh';
import SecurityIcon from '@mui/icons-material/Security';
import WarningIcon from '@mui/icons-material/Warning';
import CheckCircleIcon from '@mui/icons-material/CheckCircle';
import { DataSensitivity } from '../../../shared/types';
import { errorTextOf } from '../../../shared/error-messages';

interface DataScanResult {
  sensitivity: DataSensitivity;
  confidence: number;
  findings: ScanFinding[];
  summary: string;
}

interface ScanFinding {
  type: string;
  pattern: string;
  severity: 'high' | 'medium' | 'low';
  description: string;
}

interface DataClassificationProps {
  onScan: (text: string) => Promise<DataScanResult>;
  initialText?: string;
  disabled?: boolean;
}

const DataClassification: React.FC<DataClassificationProps> = ({
  onScan,
  initialText,
  disabled,
}) => {
  const [inputText, setInputText] = useState(initialText || '');
  const [scanResult, setScanResult] = useState<DataScanResult | null>(null);
  const [scanning, setScanning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleScan = async () => {
    if (!inputText.trim()) {
      setError('请输入待检测的文本内容');
      return;
    }

    setScanning(true);
    setError(null);
    setScanResult(null);

    try {
      const result = await onScan(inputText);
      setScanResult(result);
    } catch (err) {
      setError(`检测失败: ${errorTextOf(err)}`);
    } finally {
      setScanning(false);
    }
  };

  const getSensitivityChipColor = (sensitivity: DataSensitivity): any => {
    const colors: Record<DataSensitivity, any> = {
      [DataSensitivity.PUBLIC]: 'success',
      [DataSensitivity.INTERNAL]: 'info',
      [DataSensitivity.CONFIDENTIAL]: 'error',
    };
    return colors[sensitivity];
  };

  const getSensitivityLabel = (sensitivity: DataSensitivity): string => {
    const labels: Record<DataSensitivity, string> = {
      [DataSensitivity.PUBLIC]: '🌐 公开',
      [DataSensitivity.INTERNAL]: '🏢 内部',
      [DataSensitivity.CONFIDENTIAL]: '🔒 机密',
    };
    return labels[sensitivity];
  };

  const getSeverityColor = (severity: 'high' | 'medium' | 'low'): any => {
    const colors: Record<'high' | 'medium' | 'low', any> = {
      high: 'error',
      medium: 'warning',
      low: 'info',
    };
    return colors[severity];
  };

  const wordCount = inputText.split(/\s+/).filter(Boolean).length;
  const charCount = inputText.length;

  return (
    <Box>
      {/* 输入区域 */}
      <Card variant="outlined" sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 1 }}>
            📝 文本内容
          </Typography>
          <Box
            component="textarea"
            value={inputText}
            onChange={(e) => setInputText(e.target.value)}
            placeholder="请输入需要检测数据分级的文本内容..."
            sx={{
              width: '100%',
              minHeight: 120,
              px: 2,
              py: 1.5,
              border: '1px solid',
              borderColor: 'divider',
              borderRadius: 1,
              fontSize: 'body2.fontSize',
              fontFamily: 'inherit',
              resize: 'vertical',
              '&:focus': {
                borderColor: 'primary.main',
                boxShadow: '0 0 0 2px rgba(63, 167, 244, 0.2)',
              },
            }}
          />
          <Box sx={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', mt: 1 }}>
            <Typography variant="caption" color="text.secondary">
              {wordCount} 词 / {charCount} 字符
            </Typography>
            <Button
              variant="contained"
              size="small"
              startIcon={scanning ? <LinearProgress sx={{ width: 80 }} /> : <SecurityIcon />}
              onClick={handleScan}
              disabled={disabled || scanning || !inputText.trim()}
              sx={{ fontSize: '0.8rem', height: 32 }}
            >
              {scanning ? '检测中...' : '开始检测'}
            </Button>
          </Box>
        </CardContent>
      </Card>

      {/* 错误提示 */}
      {error && (
        <Alert severity="error" onClose={() => setError(null)} sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {/* 检测结果 */}
      {scanResult && (
        <Card variant="outlined" sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="subtitle2" fontWeight="medium" sx={{ mb: 2 }}>
              🔍 检测结果
            </Typography>

            {/* 敏感度评级 */}
            <Stack direction="row" spacing={2} sx={{ mb: 2 }}>
              <Card variant="outlined" sx={{ flex: 1 }}>
                <CardContent>
                  <Typography variant="caption" color="text.secondary">
                    数据分级
                  </Typography>
                  <Box sx={{ mt: 0.5 }}>
                    <Chip
                      label={getSensitivityLabel(scanResult.sensitivity)}
                      color={getSensitivityChipColor(scanResult.sensitivity)}
                      sx={{ fontSize: '0.9rem', height: 32 }}
                    />
                  </Box>
                </CardContent>
              </Card>
              <Card variant="outlined" sx={{ flex: 1 }}>
                <CardContent>
                  <Typography variant="caption" color="text.secondary">
                    置信度
                  </Typography>
                  <Box sx={{ mt: 0.5 }}>
                    <LinearProgress
                      variant="determinate"
                      value={scanResult.confidence * 100}
                      sx={{
                        height: 8,
                        borderRadius: 4,
                        '& .MuiLinearProgress-bar': {
                          backgroundColor: getSensitivityChipColor(scanResult.sensitivity),
                        },
                      }}
                    />
                    <Typography variant="caption" sx={{ mt: 0.5 }}>
                      {(scanResult.confidence * 100).toFixed(1)}%
                    </Typography>
                  </Box>
                </CardContent>
              </Card>
            </Stack>

            {/* 摘要 */}
            <Alert
              severity={
                scanResult.sensitivity === DataSensitivity.CONFIDENTIAL
                  ? 'error'
                  : scanResult.sensitivity === DataSensitivity.INTERNAL
                  ? 'info'
                  : 'success'
              }
              icon={<SecurityIcon />}
              sx={{ mb: 2 }}
            >
              {scanResult.summary}
            </Alert>

            <Divider sx={{ my: 2 }} />

            {/* 详细发现 */}
            {scanResult.findings.length > 0 && (
              <Box>
                <Typography variant="subtitle2" sx={{ mb: 1 }}>
                  ⚠️ 检测到敏感信息
                </Typography>
                <Stack spacing={1}>
                  {scanResult.findings.map((finding, idx) => (
                    <Card key={idx} variant="outlined" sx={{ borderLeft: `4px solid` }}>
                      <CardContent sx={{ py: 1.5, px: 2 }}>
                        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                          <Chip
                            label={finding.type}
                            size="small"
                            color={getSeverityColor(finding.severity)}
                            sx={{ fontSize: '0.65rem', height: 20 }}
                          />
                          <Typography variant="body2" fontWeight="medium">
                            匹配模式: {finding.pattern}
                          </Typography>
                        </Box>
                        <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
                          {finding.description}
                        </Typography>
                      </CardContent>
                    </Card>
                  ))}
                </Stack>
              </Box>
            )}

            {scanResult.findings.length === 0 && (
              <Alert severity="success" icon={<CheckCircleIcon />} sx={{ mt: 2 }}>
                未检测到敏感信息，该文本可标记为公开级别。
              </Alert>
            )}
          </CardContent>
        </Card>
      )}
    </Box>
  );
};

export default DataClassification;
