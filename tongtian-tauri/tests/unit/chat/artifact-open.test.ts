/**
 * 产物打开方式的分支行为（设置页「预览方式」的真实消费方）
 *
 * - 应用内预览：不调用系统打开通道
 * - 系统默认程序：调用 fileAPI.openExternal；成功不弹预览
 * - 系统打开失败：返回原路径 + 明确说明，调用方回退应用内预览（不静默失败）
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const { openExternal } = vi.hoisted(() => ({ openExternal: vi.fn() }));

vi.mock('../../../src/renderer/lib/api', () => ({
  fileAPI: { openExternal },
}));

import { openArtifact } from '../../../src/renderer/lib/artifact-open';

describe('openArtifact', () => {
  beforeEach(() => {
    openExternal.mockReset();
  });

  it('应用内预览（以及未知档位）直接返回路径，不碰系统打开通道', async () => {
    await expect(openArtifact('C:/work/report.md', '应用内预览')).resolves.toEqual({
      previewPath: 'C:/work/report.md',
      error: '',
    });
    await expect(openArtifact('C:/work/report.md', '未知档位')).resolves.toEqual({
      previewPath: 'C:/work/report.md',
      error: '',
    });
    expect(openExternal).not.toHaveBeenCalled();
  });

  it('系统默认程序打开成功：不返回预览路径，也不报错', async () => {
    openExternal.mockResolvedValue(undefined);
    await expect(openArtifact('C:/work/report.docx', '系统默认程序')).resolves.toEqual({
      previewPath: null,
      error: '',
    });
    expect(openExternal).toHaveBeenCalledWith('C:/work/report.docx');
  });

  it('系统打开失败：保留原路径并给出可读原因，供调用方回退预览', async () => {
    openExternal.mockRejectedValue(new Error('当前运行通道没有实现该功能'));
    const result = await openArtifact('C:/work/report.docx', '系统默认程序');
    expect(result.previewPath).toBe('C:/work/report.docx');
    expect(result.error).toContain('当前运行通道没有实现该功能');
    expect(result.error).toContain('已改为应用内预览');
  });
});
