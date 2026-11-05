/**
 * 产物预览的「引擎未启动」补救入口（源码契约）
 *
 * 背景：预览正文走引擎 `/api/files/content`，而端点只回报已在跑的实例。
 * 引擎不在时旧实现只留一句红字，那句话让用户「或启动引擎后重试」——可应用里
 * 原本没有任何启动引擎的入口（`ensureEngineStarted` 只接在 IM 频道页），
 * 于是提示成了一个点不到的动作。本文件钉住补好的接线：
 * - 失败按码分类，只有 ENGINE_NOT_RUNNING 才给按钮（别的错不许伪装成能重试）；
 * - 按钮真的走 `ensureEngineStarted`，成功后重取正文，失败单独报原因；
 * - 启动进行中禁用，连点不堆进程；
 * - 右侧面板与弹窗两种壳都接上，不许只修一边。
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const read = (rel: string) =>
  readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');

const preview = () => read('../../../src/renderer/components/FilePreview/index.tsx');
const monitor = () => read('../../../src/renderer/components/TaskMonitor/index.tsx');

describe('预览失败分类与补救入口', () => {
  it('只有 ENGINE_NOT_RUNNING 才点亮按钮：按码判，不拿中文文案当条件', () => {
    const src = preview();
    expect(src).toContain('isEngineNotRunningError(e)');
    expect(src).toContain('setNeedsEngineStart(');
    // 分支不许落在文案上（文案由 errorTextOf 生成，改一个字就把重试能力弄没）
    expect(src).not.toMatch(/needsEngineStart[^;]*尚未启动/);
  });

  it('按钮接的是 ensureEngineStarted，起来后靠 attempt 重取正文', () => {
    const src = preview();
    expect(src).toContain('await ensureEngineStarted()');
    expect(src).toContain('setAttempt((n) => n + 1)');
    expect(src).toContain('}, [path, attempt]);');
  });

  it('启动进行中禁用，避免连点各起一个引擎', () => {
    const src = preview();
    expect(src).toContain('if (startingEngine) return;');
    expect(src).toContain('disabled={engine.starting}');
  });

  it('启动失败单独报原因，不和「打开文件失败」混成一句', () => {
    const src = preview();
    expect(src).toContain('setEngineErr(errorTextOf(e, tr');
    expect(src).toContain('{engine.engineErr && (');
    // 预览失败本身仍走统一文案
    expect(src).toContain('setErrMsg(errorTextOf(e, tr(');
  });

  it('按钮与说明文案都走 i18n，不各写一套', () => {
    const src = preview();
    expect(src).toContain("{ t: tr } = useI18n()");
    expect(src).toContain("tr('启动引擎并重试')");
    expect(src).toContain("tr('正在启动引擎…')");
    expect(src).toContain('tr(\'也可以点右上角用系统默认应用打开（这条路不需要引擎）\')');
  });

  it('引擎没起来时仍给出不需要引擎的退路：系统默认应用那条通道保留在两种壳里', () => {
    expect(preview()).toContain('fileAPI.openExternal(path)');
    expect(monitor()).toContain('handleOpenExternal()');
  });

  it('右侧面板与弹窗两种壳共用同一份接线（不许只修一边）', () => {
    expect(preview()).toContain('<FilePreviewBody state={state} data={data} errMsg={errMsg} engine={engineAction} />');
    expect(monitor()).toContain('engine={preview.engineAction}');
  });

  it('取数入口不变：仍是 engineFetch + resolveEngineBaseUrl，不写死地址', () => {
    const src = preview();
    expect(src).toContain('engineFetch(');
    expect(src).toContain('resolveEngineBaseUrl()');
    expect(src).not.toMatch(/127\.0\.0\.1:18000/);
    expect(src).not.toMatch(/\bawait fetch\(|\breturn fetch\(/);
  });
});

describe('文档口径跟着事实走', () => {
  it('engine-endpoint 不再声称「只给 IM 频道用」', () => {
    const src = read('../../../src/renderer/lib/engine-endpoint.ts');
    expect(src).toContain('export function isEngineNotRunningError');
    expect(src).toContain('用户主动点');
    expect(src).not.toContain('的页面调用（IM 频道）');
  });

  it('tauri-api 的按需启动注释把预览算进去', () => {
    const src = read('../../../src/renderer/lib/tauri-api.ts');
    expect(src).toContain('产物预览');
  });
});
