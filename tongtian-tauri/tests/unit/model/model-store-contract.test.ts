/**
 * 模型配置数据源统一契约（任务 #39）
 *
 * 旧实现三套台账互不相认：设置页读写 localStorage（含明文 Key）、输入框另读一份，
 * 聊天提交带 apiKey 字符串。现在统一为：
 * - 渲染层只认 api.model.*；清单视图不含明文密钥，只有 hasApiKey
 * - 设置页不再写 localStorage，只保留一次性的旧缓存迁移
 * - 对话只传 modelId / modelBinding，端点与密钥由数据源一侧解析
 * - Mock 通道的 create/update/delete/setDefault 与真实通道同一语义
 */

import { describe, it, expect, beforeAll } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const settings = () => read('../../../src/renderer/components/ModelSettings/index.tsx');
const form = () => read('../../../src/renderer/components/ModelForm/index.tsx');
const hub = () => read('../../../src/renderer/pages/ModelHub/index.tsx');
const chatInput = () => read('../../../src/renderer/components/ChatInput/index.tsx');
const chatModels = () => read('../../../src/renderer/lib/chat-models.ts');

describe('设置页：真实数据源 + 旧缓存一次性迁移', () => {
  it('增删改/默认/测试全部走 api.model.*，不再自己维护 localStorage 台账', () => {
    const src = settings();
    expect(src).toContain('api.model.create(');
    expect(src).toContain('api.model.update(');
    expect(src).toContain('api.model.remove(');
    expect(src).toContain('api.model.setDefault(');
    expect(src).toContain('api.model.test(');
    expect(src).not.toContain('localStorage.setItem');
    expect(src).not.toContain('seedModels');
  });

  it('旧 tt-custom-models 只在导入成功后清理；占位种子不导入', () => {
    const src = settings();
    expect(src).toContain("const LEGACY_STORAGE_KEY = 'tt-custom-models';");
    expect(src).toContain('localStorage.removeItem(LEGACY_STORAGE_KEY)');
    expect(src).toContain("modelName.toLowerCase() === 'auto'");
    expect(src).toContain('if (!name || !baseUrl || !modelName');
    // 有失败时保留旧缓存，不静默丢用户配置
    expect(src).toContain('if (result.failed === 0)');
  });

  it('密钥留空=保留，不回传也不显示明文', () => {
    const src = settings();
    expect(src).toContain('editing?.hasApiKey');
    expect(src).toContain("if (apiKey.trim()) payload.apiKey = apiKey.trim();");
    expect(src).toContain('id: editing?.id,');
    // 写操作后清对话模型缓存，下一轮立即读到新清单
    expect(src).toContain('invalidateChatModels();');
  });

  it('数据源说明如实区分桌面端 / 引擎 / Mock，不宣称重启生效', () => {
    const src = settings();
    expect(src).toContain('modelStoreInfo()');
    expect(src).not.toContain('修改完成后重启应用生效');
    expect(src).toContain('下一轮对话立即生效');
    expect(chatModels()).toContain("title: '本机 SQLite + 系统密钥库'");
    expect(chatModels()).toContain("title: 'Mock 内存台账'");
    expect(chatModels()).toContain("title: '引擎 models.json'");
  });
});

describe('表单与输入框：同一份清单', () => {
  it('编辑已存模型时用 hasApiKey 显示占位，不再依赖空的 apiKeyEncrypted', () => {
    const src = form();
    expect(src).toContain('initialData?.hasApiKey');
    expect(src).not.toContain('initialData?.apiKeyEncrypted');
  });

  it('输入框从 api.model.list 读已保存模型，不再读 localStorage', () => {
    const src = chatInput();
    expect(src).toContain('loadChatModels(true)');
    expect(src).toContain('savedId: m.id');
    expect(src).toContain('modelSourceLabel(m.source)');
    expect(src).not.toContain('tt-custom-models');
  });

  it('ModelHub 编辑时占位符不写回密钥', () => {
    const src = hub();
    expect(src).toContain("data.apiKey === '●●●●●●●●' ? undefined : data.apiKey");
    expect(src).toContain('id: modelId,');
  });
});

// ── 行为：Mock 通道与真实通道同一语义 ───────────────────────────

(globalThis as Record<string, unknown>).window =
  (globalThis as Record<string, unknown>).window ?? {};
type WindowAPI = {
  tongtianAPI?: {
    invoke: (channel: string, ...args: unknown[]) => Promise<unknown>;
  };
};
const win = globalThis.window as WindowAPI;

interface MockModelView {
  id: string;
  name: string;
  modelName: string;
  isDefault: boolean;
  hasApiKey: boolean;
  apiKey?: string;
}

beforeAll(async () => {
  await import('../../../src/renderer/lib/mock-api');
});

describe('Mock 模型台账：id 入参不再被当成整个对象', () => {
  async function list(): Promise<MockModelView[]> {
    return (await win.tongtianAPI!.invoke('model:list')) as MockModelView[];
  }

  it('setDefault 接收 { id } 并保证默认唯一', async () => {
    await win.tongtianAPI!.invoke('model:setDefault', { id: 'mock-model-2' });
    const models = await list();
    const defaults = models.filter((m) => m.isDefault);
    expect(defaults.map((m) => m.id)).toEqual(['mock-model-2']);
  });

  it('create 只留 hasApiKey，明文密钥不进入清单', async () => {
    const created = (await win.tongtianAPI!.invoke('model:create', {
      name: '自测模型',
      source: 'openai_compat',
      baseUrl: 'https://mock.example/v1',
      apiKey: 'sk-mock-secret',
      modelName: 'mock-selftest',
      contextWindow: 4096,
    })) as MockModelView;
    expect(created.hasApiKey).toBe(true);
    expect(created.apiKey).toBeUndefined();
    expect(JSON.stringify(await list())).not.toContain('sk-mock-secret');
  });

  it('update 不传 apiKey 保留原标记，传空串清除', async () => {
    const models = await list();
    const target = models.find((m) => m.modelName === 'mock-selftest')!;
    const renamed = (await win.tongtianAPI!.invoke('model:update', {
      id: target.id,
      name: '自测模型改名',
    })) as MockModelView;
    expect(renamed.name).toBe('自测模型改名');
    expect(renamed.hasApiKey).toBe(true);

    const cleared = (await win.tongtianAPI!.invoke('model:update', {
      id: target.id,
      apiKey: '',
    })) as MockModelView;
    expect(cleared.hasApiKey).toBe(false);
  });

  it('delete 接收 { id }；重复删除如实报 MODEL_NOT_FOUND', async () => {
    const before = await list();
    const target = before.find((m) => m.modelName === 'mock-selftest')!;
    await win.tongtianAPI!.invoke('model:delete', { id: target.id });
    expect((await list()).some((m) => m.id === target.id)).toBe(false);
    await expect(
      win.tongtianAPI!.invoke('model:delete', { id: target.id }),
    ).rejects.toThrow('MODEL_NOT_FOUND');
  });

  it('未知 id 的 setDefault 不静默成功', async () => {
    await expect(
      win.tongtianAPI!.invoke('model:setDefault', { id: 'no-such-model' }),
    ).rejects.toThrow('MODEL_NOT_FOUND');
  });
});
