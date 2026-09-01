// vitest.config.ts — 单元测试基建（流 G0）
// 独立于 vite.config.ts（后者 root=src/renderer 仅服务渲染进程构建），
// 测试覆盖 src/shared 契约/纯逻辑层，include tests/unit/**

import { defineConfig } from 'vitest/config';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      '@shared': path.resolve(__dirname, 'src/shared'),
      '@renderer': path.resolve(__dirname, 'src/renderer'),
    },
  },
  test: {
    include: ['tests/unit/**/*.test.ts', 'tests/integration/**/*.integration.test.ts'],
    environment: 'node',
    globals: false,
    testTimeout: 30_000,
    coverage: {
      provider: 'v8',
      include: ['src/shared/**/*.ts'],
    },
  },
});
