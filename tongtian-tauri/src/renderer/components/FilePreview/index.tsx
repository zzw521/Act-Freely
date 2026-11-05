/**
 * 通天办公 - 产物文件内容预览
 *
 * 向引擎 /api/files/content 拉取文件内容并按类型渲染：
 * - text/html  → iframe 渲染
 * - markdown   → Markdown 渲染
 * - 其他文本   → 等宽文本
 * 加载/错误状态显式呈现，不静默失败。
 * 引擎未运行导致的失败额外给一个「启动引擎并重试」入口——原提示让用户「启动引擎」
 * 却没有任何按钮可点，这里补上；同时提示可以用系统默认应用打开（那条路不依赖引擎）。
 *
 * 内容体与取数 hook 单独导出，供右侧任务栏内嵌预览与弹窗两种壳复用。
 */

import React, { useCallback, useEffect, useState } from 'react';
import { Alert, Box, Button, CircularProgress, Dialog, DialogContent, DialogTitle, IconButton, Tooltip, Typography } from '@mui/material';
import CloseIcon from '@mui/icons-material/Close';
import OpenInNewIcon from '@mui/icons-material/OpenInNew';
import Markdown from 'react-markdown';
import {
  ensureEngineStarted,
  isEngineNotRunningError,
  resolveEngineBaseUrl,
} from '../../lib/engine-endpoint';
import { engineFetch } from '../../lib/engine-http';
import { fileAPI } from '../../lib/api';
import { errorTextOf } from '../../../shared/error-messages';
import { useI18n } from '../../i18n';

export interface FilePreviewData {
  fileName: string;
  mime: string;
  content: string;
}

export type FileContentState = 'idle' | 'loading' | 'ok' | 'error';

/** 错误态里「启动引擎并重试」那一条的接线；不传就只显示原因 */
export interface FilePreviewEngineAction {
  /** 上一次失败是否为 ENGINE_NOT_RUNNING */
  needsEngineStart: boolean;
  /** 启动进行中（按钮禁用并显示进度） */
  starting: boolean;
  /** 启动引擎本身失败的原因（与预览失败分开显示） */
  engineErr: string;
  /** 拉起引擎并重取正文 */
  onStart: () => void;
}

/**
 * 按路径取文件正文：null 表示未选中文件（idle）。
 *
 * 取数走引擎 `/api/files/content`，而端点只回报**已在跑**的实例——引擎没起来时
 * 这里拿到的是 `ENGINE_NOT_RUNNING`。那种情况下不能只留一句红字：提示里写的
 * 「或启动引擎后重试」在本应用里原本没有任何入口，所以这里把它做成真按钮，
 * 由 `ensureEngineStarted`（命令侧幂等）拉起当前工作空间的引擎后重取一次。
 */
export function useFileContent(path: string | null) {
  const { t: tr } = useI18n();
  const [state, setState] = useState<FileContentState>('idle');
  const [data, setData] = useState<FilePreviewData | null>(null);
  const [errMsg, setErrMsg] = useState('');
  /** 上一次失败是否为「引擎未运行」：决定错误态里给不给「启动引擎并重试」 */
  const [needsEngineStart, setNeedsEngineStart] = useState(false);
  /** 启动引擎进行中（按钮禁用，避免连点堆进程；命令本身也幂等） */
  const [startingEngine, setStartingEngine] = useState(false);
  /** 启动引擎失败的原因（与预览失败分开：起不来不等于文件读不到） */
  const [engineErr, setEngineErr] = useState('');
  /** 重试计数：变化即重跑一次取数，路径没变也能再读（引擎刚起来的场景） */
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    if (!path) {
      setState('idle');
      setData(null);
      setErrMsg('');
      setNeedsEngineStart(false);
      setEngineErr('');
      return;
    }
    let cancelled = false;
    setState('loading');
    setData(null);
    setErrMsg('');
    setNeedsEngineStart(false);
    setEngineErr('');
    (async () => {
      try {
        // 按当前工作空间解析真实端口：第二个实例可能在 18001，不能写死 18000
        const baseUrl = await resolveEngineBaseUrl();
        const resp = await engineFetch(`${baseUrl}/api/files/content?path=${encodeURIComponent(path)}`);
        const json = (await resp.json()) as FilePreviewData & { ok?: boolean; message?: string };
        if (cancelled) return;
        if (!resp.ok || json.ok === false) {
          throw new Error(json.message || tr('打开失败（HTTP {status}）', { status: resp.status }));
        }
        setData({ fileName: json.fileName, mime: json.mime, content: json.content });
        setState('ok');
      } catch (e) {
        if (cancelled) return;
        setErrMsg(errorTextOf(e, tr('打开文件失败')));
        setNeedsEngineStart(isEngineNotRunningError(e));
        setState('error');
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [path, attempt]);

  /** 重新取一次正文（引擎已在跑、只想刷新时用） */
  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  /** 拉起当前工作空间的引擎后重取正文；失败就把原因如实留在按钮下方 */
  const startEngineAndRetry = useCallback(async () => {
    if (startingEngine) return;
    setStartingEngine(true);
    setEngineErr('');
    try {
      await ensureEngineStarted();
      setAttempt((n) => n + 1);
    } catch (e) {
      setEngineErr(errorTextOf(e, tr('引擎启动失败')));
    } finally {
      setStartingEngine(false);
    }
  }, [startingEngine, tr]);

  return {
    state,
    data,
    errMsg,
    needsEngineStart,
    startingEngine,
    engineErr,
    reload,
    startEngineAndRetry,
    /** 直接喂给 FilePreviewBody 的接线（两种壳共用，避免各自拼字段拼出分叉） */
    engineAction: {
      needsEngineStart,
      starting: startingEngine,
      engineErr,
      onStart: () => void startEngineAndRetry(),
    } satisfies FilePreviewEngineAction,
  };
}

/** 内容体：加载 / 错误 / HTML / Markdown / 纯文本。外层负责给高度与留白 */
export const FilePreviewBody: React.FC<{
  state: FileContentState;
  data: FilePreviewData | null;
  errMsg: string;
  /** 引擎未运行时的补救入口；不传则错误态只显示原因 */
  engine?: FilePreviewEngineAction;
}> = ({ state, data, errMsg, engine }) => {
  const { t: tr } = useI18n();
  if (state === 'loading' || state === 'idle') {
    return (
      <Box sx={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1.5 }}>
        <CircularProgress size={20} sx={{ color: 'var(--tt-accent-violet)' }} />
        <Typography sx={{ fontSize: '13px', color: 'var(--tt-text-4)' }}>{tr('正在打开文件…')}</Typography>
      </Box>
    );
  }
  if (state === 'error' || !data) {
    return (
      <Box
        sx={{
          flex: 1,
          display: 'flex',
          flexDirection: 'column',
          alignItems: 'center',
          justifyContent: 'center',
          gap: 1.25,
          px: 2,
        }}
      >
        <Typography sx={{ fontSize: '13px', color: 'var(--tt-danger)', textAlign: 'center' }}>
          {errMsg}
        </Typography>
        {engine?.needsEngineStart && (
          <>
            <Button
              size="small"
              variant="outlined"
              onClick={engine.onStart}
              disabled={engine.starting}
              startIcon={engine.starting ? <CircularProgress size={14} /> : undefined}
              sx={{ fontSize: '12.5px' }}
            >
              {engine.starting ? tr('正在启动引擎…') : tr('启动引擎并重试')}
            </Button>
            {/* 引擎起不来时把启动失败的原因单独说清楚，别和「打开文件失败」混成一句 */}
            {engine.engineErr && (
              <Alert severity="error" sx={{ fontSize: '12px', maxWidth: 420 }}>
                {engine.engineErr}
              </Alert>
            )}
          </>
        )}
        <Typography sx={{ fontSize: '12px', color: 'var(--tt-text-4)', textAlign: 'center' }}>
          {tr('也可以点右上角用系统默认应用打开（这条路不需要引擎）')}
        </Typography>
      </Box>
    );
  }
  if (data.mime === 'text/html') {
    return (
      <Box
        component="iframe"
        srcDoc={data.content}
        sandbox=""
        sx={{
          /* 亮色孤岛：被预览的 HTML 是磁盘上的独立文档，自带白底语义，翻暗只会让它没写背景的元素糊掉 */
          flex: 1,
          minHeight: 0,
          width: '100%',
          border: 'none',
          borderRadius: '10px',
          background: '#fff',
        }}
        title={data.fileName}
      />
    );
  }
  if (data.mime === 'text/markdown') {
    return (
      <Box className="markdown-body" sx={{ flex: 1, minHeight: 0, overflow: 'auto', color: 'var(--tt-text-2)', pb: 2 }}>
        <Markdown>{data.content}</Markdown>
      </Box>
    );
  }
  return (
    <Box
      component="pre"
      sx={{
        flex: 1,
        minHeight: 0,
        overflow: 'auto',
        m: 0,
        p: 1.5,
        borderRadius: '10px',
        background: 'var(--tt-chat-code-bg)',
        fontSize: '12.5px',
        lineHeight: 1.6,
        color: 'var(--tt-text-2)',
        fontFamily: "'SF Mono', Consolas, monospace",
        whiteSpace: 'pre-wrap',
        wordBreak: 'break-word',
      }}
    >
      {data.content}
    </Box>
  );
};

interface FilePreviewProps {
  /** 文件绝对路径（null 时关闭） */
  path: string | null;
  onClose: () => void;
}

/** 独立弹窗壳（保留给需要模态预览的场景） */
const FilePreview: React.FC<FilePreviewProps> = ({ path, onClose }) => {
  const { t: tr } = useI18n();
  const { state, data, errMsg, engineAction } = useFileContent(path);
  /** 用系统默认应用打开的失败说明（与预览失败分开：预览不了不等于文件打不开） */
  const [openErr, setOpenErr] = useState('');

  useEffect(() => {
    setOpenErr('');
  }, [path]);

  /** 用系统默认应用打开：docx/pdf/pptx/图片等此弹窗预览不了的产物走这里 */
  const handleOpenExternal = async () => {
    if (!path) return;
    setOpenErr('');
    try {
      await fileAPI.openExternal(path);
    } catch (e) {
      setOpenErr(errorTextOf(e, tr('打开失败')));
    }
  };

  return (
    <Dialog open={Boolean(path)} onClose={onClose} maxWidth="md" fullWidth PaperProps={{ sx: { borderRadius: '16px', height: '80vh' } }}>
      <DialogTitle sx={{ display: 'flex', alignItems: 'center', pb: 1 }}>
        <Typography sx={{ fontSize: '15px', fontWeight: 700, flexGrow: 1, wordBreak: 'break-all' }}>
          {data?.fileName ?? (path ? path.split(/[/\\]/).pop() : tr('文件预览'))}
        </Typography>
        <Tooltip title={tr('用系统默认应用打开')}>
          <span>
            <IconButton
              size="small"
              aria-label={tr('用系统默认应用打开')}
              onClick={() => void handleOpenExternal()}
              disabled={!path}
              sx={{ color: 'var(--tt-text-4)', mr: 0.5 }}
            >
              <OpenInNewIcon sx={{ fontSize: 18 }} />
            </IconButton>
          </span>
        </Tooltip>
        <IconButton size="small" aria-label={tr('关闭预览')} onClick={onClose} sx={{ color: 'var(--tt-text-4)' }}>
          <CloseIcon sx={{ fontSize: 18 }} />
        </IconButton>
      </DialogTitle>
      <DialogContent sx={{ pt: 0, display: 'flex', flexDirection: 'column' }}>
        {openErr && (
          <Alert severity="error" sx={{ mb: 1, fontSize: '12.5px' }} onClose={() => setOpenErr('')}>
            {openErr}
          </Alert>
        )}
        <FilePreviewBody state={state} data={data} errMsg={errMsg} engine={engineAction} />
      </DialogContent>
    </Dialog>
  );
};

export default FilePreview;
