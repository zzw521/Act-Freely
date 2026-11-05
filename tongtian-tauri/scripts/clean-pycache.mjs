// 打包前清掉引擎源码目录里的字节码缓存：它们会随 bundle.resources 一起进安装包，
// 既增加体积，也可能带着开发机的绝对路径。
import { readdir, rm } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const targets = ['engine', 'scripts'];

async function clean(dir) {
  let entries;
  try {
    entries = await readdir(dir, { withFileTypes: true });
  } catch {
    return 0;
  }
  let removed = 0;
  for (const entry of entries) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      if (entry.name === '__pycache__') {
        await rm(full, { recursive: true, force: true });
        removed += 1;
      } else {
        removed += await clean(full);
      }
    } else if (entry.isFile() && entry.name.endsWith('.pyc')) {
      await rm(full, { force: true });
      removed += 1;
    }
  }
  return removed;
}

let total = 0;
for (const target of targets) total += await clean(path.join(root, target));
console.log(`[clean-pycache] removed ${total} cache entries`);