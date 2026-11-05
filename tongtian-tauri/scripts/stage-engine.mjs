// 打包用的引擎副本：整目录复制，但排除 __pycache__ 与 *.pyc。
//
// 为什么不能直接映射 ../engine：Tauri 的资源映射只能整目录复制，不提供排除规则；
// 而引擎目录在打包期间仍可能被外部 Python 进程写入字节码缓存（实测打包中途出现过
// engine/__pycache__，并随 MSI/NSIS 一起进了安装包，既增体积又可能带着开发机路径）。
// 因此每次构建先生成一份干净副本，资源的映射指向它。
import { cp, rm, stat } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const srcDir = path.join(root, 'engine');
const dstDir = path.join(root, 'src-tauri', 'staged-engine');

try {
  await stat(path.join(srcDir, 'server.py'));
} catch {
  console.error(`[stage-engine] 缺少 ${path.join(srcDir, 'server.py')}，拒绝生成不完整的引擎副本`);
  process.exit(1);
}

await rm(dstDir, { recursive: true, force: true });
await cp(srcDir, dstDir, {
  recursive: true,
  filter: (src) => {
    const name = path.basename(src);
    if (name === '__pycache__') return false;
    if (name.endsWith('.pyc') || name.endsWith('.pyo')) return false;
    return true;
  },
});
console.log(`[stage-engine] ${path.relative(root, dstDir)} ← engine（已排除字节码缓存）`);
