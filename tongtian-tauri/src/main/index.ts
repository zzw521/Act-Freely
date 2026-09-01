// ============================================================
// src/main/index.ts — Electron main process entry
// ============================================================

import { app, BrowserWindow, Tray, Menu, nativeImage, shell, type NativeImage } from 'electron';
import path from 'path';
import Logger from './util/logger';
import { registerChatIpc } from './ipc/chat.ipc';
import { registerModelIpc } from './ipc/model.ipc';
import { registerFileIpc } from './ipc/file.ipc';
import { registerWorkspaceIpc } from './ipc/workspace.ipc';
import { registerAlignmentIpc } from './ipc/alignment.ipc';
import SettingsStore from './store/settings-store';

// ── Constants ────────────────────────────────────────────────

const IS_DEV = process.env.NODE_ENV !== 'production';
const DEV_SERVER_URL = 'http://localhost:7173';

const logger = Logger.getInstance();

// ── Window & Tray references ─────────────────────────────────

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let isQuitting = false;

// ── Preload path ─────────────────────────────────────────────

function getPreloadPath(): string {
  if (IS_DEV) {
    return path.join(__dirname, '..', 'preload', 'index.js');
  }
  return path.join(__dirname, '..', 'preload', 'index.js');
}

// ── Window Creation ──────────────────────────────────────────

function createMainWindow(): BrowserWindow {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 800,
    minHeight: 600,
    title: '通天办公',
    show: false,
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      preload: getPreloadPath(),
      spellcheck: false,
    },
    // Frame style
    titleBarStyle: process.platform === 'darwin' ? 'hiddenInset' : 'default',
    // Icon
    icon: path.join(__dirname, '..', '..', 'resources', 'icon.png'),
  });

  // Show window when ready to prevent visual flash
  mainWindow.once('ready-to-show', () => {
    mainWindow?.show();
    logger.info('Main window shown', 'main');
  });

  // Load content
  if (IS_DEV) {
    mainWindow.loadURL(DEV_SERVER_URL);
    // Open DevTools in dev mode
    mainWindow.webContents.openDevTools({ mode: 'detach' });
  } else {
    mainWindow.loadFile(path.join(__dirname, '..', 'renderer', 'index.html'));
  }

  // Handle window close — minimize to tray instead of quitting
  mainWindow.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      mainWindow?.hide();
      logger.info('Window minimized to tray', 'main');
    }
  });

  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  // Security: prevent new windows from being created
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: 'deny' };
  });

  logger.info('Main window created', 'main');
  return mainWindow;
}

// ── System Tray ──────────────────────────────────────────────

function createTray(): void {
  // Create a simple tray icon (in production, use a real icon file)
  const iconPath = path.join(__dirname, '..', '..', 'resources', 'tray-icon.png');
  let trayIcon: NativeImage;

  try {
    trayIcon = nativeImage.createFromPath(iconPath);
  } catch {
    // Fallback: create a simple 16x16 empty icon
    trayIcon = nativeImage.createEmpty();
  }

  tray = new Tray(trayIcon);
  tray.setToolTip('通天办公');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '打开主窗口',
      click: () => {
        if (mainWindow) {
          mainWindow.show();
          mainWindow.focus();
        } else {
          createMainWindow();
        }
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        isQuitting = true;
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);

  tray.on('click', () => {
    if (mainWindow) {
      if (mainWindow.isVisible()) {
        mainWindow.focus();
      } else {
        mainWindow.show();
      }
    }
  });

  logger.info('System tray created', 'main');
}

// ── IPC Handler Registration ─────────────────────────────────

function registerIPCHandlers(): void {
  // 核心业务 IPC
  registerModelIpc();
  registerFileIpc();
  registerWorkspaceIpc();
  // 千问对标整改新增 IPC（settings/hooks/awareness/connector/archive/computer-use）
  registerAlignmentIpc();

  logger.info('IPC handlers registered', 'main');
}

// ── App Lifecycle ────────────────────────────────────────────

app.whenReady().then(() => {
  logger.info('App ready, starting...', 'main');

  registerIPCHandlers();
  createMainWindow();
  // chat IPC 需要主窗口引用用于事件推送
  if (mainWindow) registerChatIpc(mainWindow);
  createTray();

  // 按已保存设置恢复系统行为（开机自启/保持唤醒）
  SettingsStore.getInstance().applyOnStartup();

  app.on('activate', () => {
    // macOS: re-create window when dock icon is clicked
    if (BrowserWindow.getAllWindows().length === 0) {
      createMainWindow();
    } else {
      mainWindow?.show();
    }
  });

  logger.info('App started successfully', 'main');
});

app.on('window-all-closed', () => {
  // On macOS, apps typically stay open until user explicitly quits
  if (process.platform !== 'darwin') {
    // Even on other platforms, we minimize to tray, so don't quit here
    logger.info('All windows closed', 'main');
  }
});

app.on('before-quit', () => {
  isQuitting = true;
  logger.info('App quitting...', 'main');
});

app.on('quit', () => {
  logger.info('App quit', 'main');
  tray?.destroy();
  tray = null;
});

// ── Security: prevent navigation to external URLs ────────────

app.on('web-contents-created', (_event, contents) => {
  contents.on('will-navigate', (event, _url) => {
    event.preventDefault();
  });
});
