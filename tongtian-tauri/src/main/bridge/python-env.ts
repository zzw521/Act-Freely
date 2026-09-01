// ============================================================
// src/main/bridge/python-env.ts — Agent Python environment
// ============================================================

import { execFile } from 'child_process';
import path from 'path';
import fs from 'fs';
import { app } from 'electron';

// ── Types ────────────────────────────────────────────────────

interface PythonInfo {
  path: string;
  version: string;
}

interface AgentInfo {
  installed: boolean;
  version?: string;
}

// ── Helpers ──────────────────────────────────────────────────

function getBaseDir(): string {
  try {
    return path.join(app.getPath('userData'));
  } catch {
    const home = process.env.APPDATA
      || process.env.HOME
      || path.join(process.env.USERPROFILE || '', '.tongtian');
    return path.join(home, '.tongtian');
  }
}

/**
 * Detect Python installation on the system.
 * Tries 'python3', 'python', and common Windows paths.
 */
export async function detectPython(): Promise<PythonInfo | null> {
  const candidates = process.platform === 'win32'
    ? ['python', 'python3', 'py']
    : ['python3', 'python'];

  for (const cmd of candidates) {
    try {
      const version = await getPythonVersion(cmd);
      if (version) {
        return { path: cmd, version };
      }
    } catch {
      continue;
    }
  }

  // Try common Windows paths
  if (process.platform === 'win32') {
    const localAppData = process.env.LOCALAPPDATA || '';
    const windowsPaths = [
      path.join(localAppData, 'Programs', 'Python', 'Python312', 'python.exe'),
      path.join(localAppData, 'Programs', 'Python', 'Python311', 'python.exe'),
      path.join(localAppData, 'Programs', 'Python', 'Python310', 'python.exe'),
      'C:\\Python312\\python.exe',
      'C:\\Python311\\python.exe',
      'C:\\Python310\\python.exe',
    ];

    for (const p of windowsPaths) {
      try {
        if (fs.existsSync(p)) {
          const version = await getPythonVersion(p);
          if (version) {
            return { path: p, version };
          }
        }
      } catch {
        continue;
      }
    }
  }

  return null;
}

function getPythonVersion(pythonPath: string): Promise<string> {
  return new Promise((resolve, reject) => {
    execFile(pythonPath, ['--version'], { timeout: 5000 }, (error, stdout, stderr) => {
      if (error) {
        reject(error);
        return;
      }
      const output = (stdout || stderr).trim();
      const match = output.match(/Python\s+(\d+\.\d+\.\d+)/);
      if (match) {
        resolve(match[1]);
      } else {
        reject(new Error('Could not parse Python version'));
      }
    });
  });
}

/**
 * Detect if Agent is installed in the given Python environment.
 */
export async function detectAgent(pythonPath: string): Promise<AgentInfo> {
  return new Promise((resolve) => {
    execFile(pythonPath, ['-m', 'pip', 'show', 'qwenpaw'], { timeout: 10000 }, (error, stdout) => {
      if (error) {
        resolve({ installed: false });
        return;
      }

      const versionMatch = stdout.match(/Version:\s*(.+)/);
      resolve({
        installed: true,
        version: versionMatch ? versionMatch[1].trim() : undefined,
      });
    });
  });
}

/**
 * Get the workspace directory path for a given workspace ID.
 */
export function getWorkspaceDir(workspaceId: string): string {
  return path.join(getBaseDir(), 'workspaces', workspaceId);
}

/**
 * Ensure the workspace directory exists, creating it if necessary.
 */
export async function ensureWorkspaceDir(workspaceId: string): Promise<string> {
  const dir = getWorkspaceDir(workspaceId);
  await fs.promises.mkdir(dir, { recursive: true });
  return dir;
}

/**
 * Get the command arguments to start Agent.
 */
export function getAgentStartCommand(
  pythonPath: string,
  port: number,
  workDir: string
): string[] {
  return [
    pythonPath,
    '-m', 'qwenpaw',
    '--port', String(port),
    '--work-dir', workDir,
  ];
}
