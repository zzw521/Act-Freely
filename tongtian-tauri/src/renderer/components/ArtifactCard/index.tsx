/**
 * 通天办公 - 产物卡片
 *
 * 消息流与右侧任务栏共用同一张卡片：类型色块图标 + 文件名 + 类型/动作提示，
 * 固定高度、单行省略，避免不同扩展名把列表撑成参差不齐的小 Chip。
 */

import React from 'react';
import { Box, Tooltip, Typography } from '@mui/material';
import ArticleOutlinedIcon from '@mui/icons-material/ArticleOutlined';
import ChevronRightIcon from '@mui/icons-material/ChevronRight';
import CodeOutlinedIcon from '@mui/icons-material/CodeOutlined';
import DescriptionOutlinedIcon from '@mui/icons-material/DescriptionOutlined';
import ImageOutlinedIcon from '@mui/icons-material/ImageOutlined';
import InsertDriveFileOutlinedIcon from '@mui/icons-material/InsertDriveFileOutlined';
import PictureAsPdfOutlinedIcon from '@mui/icons-material/PictureAsPdfOutlined';
import SlideshowOutlinedIcon from '@mui/icons-material/SlideshowOutlined';
import TableChartOutlinedIcon from '@mui/icons-material/TableChartOutlined';

interface ArtifactKind {
  label: string;
  color: string;
  bg: string;
  icon: React.ReactNode;
}

/** 按扩展名给类型色块：同一类文件在消息流与任务栏里长相一致 */
export function artifactKindOf(fileName: string): ArtifactKind {
  const ext = fileName.split('.').pop()?.toLowerCase() ?? '';
  if (['html', 'htm'].includes(ext)) {
    return { label: 'HTML', color: '#C2410C', bg: '#FFF7ED', icon: <CodeOutlinedIcon sx={{ fontSize: 18 }} /> };
  }
  if (['md', 'markdown', 'txt'].includes(ext)) {
    return { label: ext === 'txt' ? 'TXT' : 'Markdown', color: '#2563EB', bg: '#EFF6FF', icon: <ArticleOutlinedIcon sx={{ fontSize: 18 }} /> };
  }
  if (['docx', 'doc'].includes(ext)) {
    return { label: 'Word', color: '#1D4ED8', bg: '#EFF6FF', icon: <DescriptionOutlinedIcon sx={{ fontSize: 18 }} /> };
  }
  if (['xlsx', 'xls', 'csv'].includes(ext)) {
    return { label: 'Excel', color: '#15803D', bg: '#ECFDF5', icon: <TableChartOutlinedIcon sx={{ fontSize: 18 }} /> };
  }
  if (['pptx', 'ppt'].includes(ext)) {
    return { label: 'PPT', color: '#C2410C', bg: '#FFF7ED', icon: <SlideshowOutlinedIcon sx={{ fontSize: 18 }} /> };
  }
  if (ext === 'pdf') {
    return { label: 'PDF', color: '#DC2626', bg: '#FEF2F2', icon: <PictureAsPdfOutlinedIcon sx={{ fontSize: 18 }} /> };
  }
  if (['png', 'jpg', 'jpeg', 'webp', 'gif', 'svg'].includes(ext)) {
    return { label: '图片', color: '#7C3AED', bg: '#F5F3FF', icon: <ImageOutlinedIcon sx={{ fontSize: 18 }} /> };
  }
  return { label: '文件', color: '#475569', bg: '#F1F5F9', icon: <InsertDriveFileOutlinedIcon sx={{ fontSize: 18 }} /> };
}

export interface ArtifactCardProps {
  /** 产物绝对路径 */
  path: string;
  /** 点击回调（消息流里由工作台统一接管：打开右侧面板预览） */
  onClick: (path: string) => void;
  /** 卡片副标题里的动作提示，如「在右侧预览」 */
  hint?: string;
}

const ArtifactCard: React.FC<ArtifactCardProps> = ({ path, onClick, hint }) => {
  const name = path.split(/[\\/]/).filter(Boolean).pop() ?? path;
  const meta = artifactKindOf(name);
  return (
    <Tooltip title={path} placement="top">
      <Box
        component="button"
        type="button"
        onClick={() => onClick(path)}
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          width: '100%',
          minWidth: 0,
          p: 1,
          pl: 1.1,
          borderRadius: '12px',
          border: '1px solid var(--tt-border-soft)',
          background: 'var(--tt-surface)',
          textAlign: 'left',
          fontFamily: 'inherit',
          cursor: 'pointer',
          transition: 'border-color 0.15s ease, box-shadow 0.15s ease, transform 0.15s ease',
          '&:hover': {
            borderColor: '#5EEAD4',
            boxShadow: '0 6px 18px rgba(15, 23, 42, 0.07)',
            transform: 'translateY(-1px)',
          },
          '&:focus-visible': { outline: '2px solid #14B8A6', outlineOffset: 2 },
        }}
      >
        <Box
          sx={{
            width: 34,
            height: 34,
            borderRadius: '9px',
            bgcolor: meta.bg,
            color: meta.color,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            flexShrink: 0,
          }}
        >
          {meta.icon}
        </Box>
        <Box sx={{ minWidth: 0, flex: 1 }}>
          <Typography
            sx={{
              fontSize: '12.5px',
              fontWeight: 600,
              color: 'var(--tt-text-1)',
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {name}
          </Typography>
          <Typography
            sx={{
              fontSize: '11px',
              color: 'var(--tt-text-4)',
              mt: 0.15,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}
          >
            {meta.label}
            {hint ? ` · ${hint}` : ''}
          </Typography>
        </Box>
        <ChevronRightIcon sx={{ fontSize: 16, color: 'var(--tt-text-4)', flexShrink: 0 }} />
      </Box>
    </Tooltip>
  );
};

export default ArtifactCard;
