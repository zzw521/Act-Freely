/**
 * 产物打开方式（设置页「预览方式」的真实消费方）
 *
 * - 应用内预览：返回路径，由调用方挂 FilePreview 弹窗
 * - 系统默认程序：走 fileAPI.openExternal（仅桌面版有该通道）
 *   失败时不假装打开成功：返回错误说明并回退应用内预览，保证用户仍能看到文件
 */

import { fileAPI } from './api';
import { errorTextOf } from '../../shared/error-messages';

export interface ArtifactOpenResult {
  /** 需要在应用内预览的路径；null 表示已交给系统默认应用 */
  previewPath: string | null;
  /** 非空说明系统打开失败，已回退应用内预览 */
  error: string;
}

export async function openArtifact(path: string, previewMode: string): Promise<ArtifactOpenResult> {
  if (previewMode !== '系统默认程序') {
    return { previewPath: path, error: '' };
  }
  try {
    await fileAPI.openExternal(path);
    return { previewPath: null, error: '' };
  } catch (e) {
    return {
      previewPath: path,
      error: `${errorTextOf(e, '系统默认程序未能打开该文件')}；已改为应用内预览`,
    };
  }
}
