#!/usr/bin/env node
/**
 * 通天办公 · 插件 SDK 命令行
 *
 *   node scripts/plugin-sdk.mjs init ./my-plugin --name "我的套件"
 *   node scripts/plugin-sdk.mjs validate ./my-plugin
 *   node scripts/plugin-sdk.mjs pack ./my-plugin -o my-plugin.zip
 *
 * 规则与桌面端 Rust 预检同源：
 * - plugin.json 必须有非空 name；commands 支持数组字符串 / 数组对象 / 名称→描述三种写法
 * - 指令正文优先 content/prompt/body/instructions，其次 file，最后约定文件
 *   commands/<名称>.md 等；找不到正文记 warning（可安装，但不能取用）
 * - 包内任意 SKILL.md 会作为技能进入执行面；mcpServers/hooks/agents 等字段不会执行
 * - pack 只做 STORE 直存 ZIP（无压缩），保证与安装器解析口径一致
 *
 * 退出码：0 成功（validate 允许 warning）；1 校验失败或参数/IO 错误
 */

import { mkdirSync, readFileSync, readdirSync, statSync, writeFileSync, existsSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const TEMPLATE_DIR = path.join(ROOT, 'src-tauri', 'templates', 'plugin');

const MAX_BODY_FILE_BYTES = 256 * 1024;
const MAX_BODY_CHARS = 20_000;
const MAX_COMMANDS = 100;
const MAX_SKILLS = 50;
const MANIFEST_KEYS = new Set([
  'name',
  'version',
  'author',
  'category',
  'description',
  'commands',
  'skills',
  'dataLinks',
  'license',
]);
const BEHAVIOR_FIELDS = new Set(['mcpservers', 'hooks', 'agents', 'permissions', 'tools']);

function usage(exitCode = 0) {
  const text = `插件 SDK

用法：
  node scripts/plugin-sdk.mjs init <目录> [--name <套件名>]
  node scripts/plugin-sdk.mjs validate <目录> [--json]
  node scripts/plugin-sdk.mjs pack <目录> -o <输出.zip>

说明：
  init      从内置模板生成 plugin.json / commands/hello.md / skills/example/SKILL.md / README.md
  validate  按安装器口径检查目录（不读 ZIP；warnings 不阻断，errors 阻断）
  pack      先校验再打包为 STORE 直存 ZIP，可直接用「添加 → 上传套件」安装
`;
  process.stdout.write(text);
  process.exit(exitCode);
}

function fail(message) {
  process.stderr.write(`错误：${message}\n`);
  process.exit(1);
}

function parseArgs(argv) {
  const positional = [];
  const flags = {};
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (arg === '--name' || arg === '--out' || arg === '-o') {
      const value = argv[i + 1];
      if (!value || value.startsWith('-')) fail(`${arg} 缺少参数值`);
      flags[arg === '-o' ? '--out' : arg] = value;
      i += 1;
    } else if (arg === '--json' || arg === '--help' || arg === '-h') {
      flags[arg] = true;
    } else if (arg.startsWith('-')) {
      fail(`未知参数：${arg}`);
    } else {
      positional.push(arg);
    }
  }
  return { positional, flags };
}

function readJson(file) {
  try {
    return JSON.parse(readFileSync(file, 'utf8'));
  } catch (e) {
    return { __error: e instanceof Error ? e.message : String(e) };
  }
}

function strOf(obj, key) {
  const v = obj?.[key];
  if (typeof v !== 'string') return undefined;
  const t = v.trim();
  return t ? t : undefined;
}

function firstStr(obj, keys) {
  for (const key of keys) {
    const v = strOf(obj, key);
    if (v) return v;
  }
  return undefined;
}

function collectCommands(manifest) {
  const out = [];
  const raw = manifest?.commands;
  if (Array.isArray(raw)) {
    for (const item of raw) {
      if (typeof item === 'string') {
        const name = item.trim();
        if (name) out.push({ name, description: '', content: undefined, file: undefined });
      } else if (item && typeof item === 'object') {
        const name = firstStr(item, ['name', 'command', 'id']);
        if (!name) continue;
        out.push({
          name,
          description: firstStr(item, ['description', 'desc']) ?? '',
          content: firstStr(item, ['content', 'prompt', 'body', 'instructions']),
          file: firstStr(item, ['file', 'path']),
        });
      }
    }
  } else if (raw && typeof raw === 'object') {
    for (const [name, value] of Object.entries(raw)) {
      const key = name.trim();
      if (!key) continue;
      if (typeof value === 'string') {
        out.push({ name: key, description: value.trim(), content: undefined, file: undefined });
      } else if (value && typeof value === 'object') {
        out.push({
          name: key,
          description: firstStr(value, ['description', 'desc']) ?? '',
          content: firstStr(value, ['content', 'prompt', 'body', 'instructions']),
          file: firstStr(value, ['file', 'path']),
        });
      } else {
        out.push({ name: key, description: '', content: undefined, file: undefined });
      }
    }
  }
  return out;
}

function isSafeRelative(p) {
  if (!p || path.isAbsolute(p) || /^[A-Za-z]:/.test(p)) return false;
  const segs = p.replace(/\\/g, '/').split('/').filter(Boolean);
  return segs.length > 0 && segs.every((s) => s !== '..');
}

function commandNameError(name) {
  if (!name) return '指令名为空';
  if ([...name].length > 128) return `指令名超过 128 字（${[...name].length}）`;
  if (name === '.' || name === '..') return '指令名不能是 . 或 ..';
  if (/[/\\:\u0000-\u001f]/.test(name)) return `指令名「${name}」含路径分隔符或控制字符`;
  return null;
}

function walk(dir, base = dir) {
  const out = [];
  for (const name of readdirSync(dir)) {
    const full = path.join(dir, name);
    const stat = statSync(full);
    if (stat.isDirectory()) out.push(...walk(full, base));
    else if (stat.isFile()) out.push(path.relative(base, full).replace(/\\/g, '/'));
  }
  return out;
}

function readBounded(file, rel, errors) {
  const stat = statSync(file);
  if (stat.size > MAX_BODY_FILE_BYTES) {
    errors.push(`正文文件「${rel}」超过 ${MAX_BODY_FILE_BYTES / 1024}KB，安装后读取会失败`);
    return null;
  }
  return readFileSync(file, 'utf8').trim();
}

function validateDir(dir) {
  const errors = [];
  const warnings = [];
  const files = existsSync(dir) && statSync(dir).isDirectory() ? walk(dir) : [];
  if (!files.length) errors.push(`目录不存在或为空：${dir}`);

  const manifestRel = files.find((f) => f === 'plugin.json');
  if (!manifestRel) {
    errors.push('根目录缺少 plugin.json');
    return { ok: false, errors, warnings, manifest: null, commands: [], skillFiles: [] };
  }
  const manifest = readJson(path.join(dir, manifestRel));
  if (manifest.__error) {
    errors.push(`plugin.json 不是合法 JSON：${manifest.__error}`);
    return { ok: false, errors, warnings, manifest: null, commands: [], skillFiles: [] };
  }
  if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
    errors.push('plugin.json 必须是 JSON 对象');
    return { ok: false, errors, warnings, manifest: null, commands: [], skillFiles: [] };
  }

  const name = strOf(manifest, 'name');
  if (!name) errors.push('plugin.json 缺少非空的 name 字段');
  else if ([...name].length > 200) errors.push('套件名超过 200 字');
  if (!strOf(manifest, 'version')) warnings.push('未标注 version，安装后显示「未标注版本」');

  const ignoredFields = Object.keys(manifest).filter((k) => !MANIFEST_KEYS.has(k)).sort();
  const behavior = ignoredFields.filter((k) => BEHAVIOR_FIELDS.has(k.toLowerCase()));
  if (behavior.length) {
    warnings.push(`字段 ${behavior.join(' / ')} 不会被执行：本应用只消费 commands 与包内 SKILL.md`);
  }

  const skillFiles = files.filter((f) => f.toLowerCase().endsWith('skill.md'));
  const declaredSkills = Array.isArray(manifest.skills)
    ? manifest.skills.filter((it) =>
        typeof it === 'string'
          ? Boolean(it.trim())
          : it && typeof it === 'object' && Boolean(firstStr(it, ['name', 'path', 'file'])),
      ).length
    : 0;
  if (manifest.skills !== undefined && !Array.isArray(manifest.skills)) {
    warnings.push('skills 字段不是数组，安装时会被丢弃');
  }
  if (declaredSkills > 0 && skillFiles.length > 0 && declaredSkills !== skillFiles.length) {
    warnings.push(
      `声明了 ${declaredSkills} 个技能，目录内实际找到 ${skillFiles.length} 个 SKILL.md；安装后以实际文件为准`,
    );
  }
  if (skillFiles.length > MAX_SKILLS) {
    warnings.push(`SKILL.md 超过 ${MAX_SKILLS} 个，超出的技能不会进入执行面`);
  }
  for (const s of skillFiles) {
    if (statSync(path.join(dir, s)).size > 512 * 1024) {
      warnings.push(`技能文件「${s}」超过 512KB，安装后会被跳过`);
    }
  }

  const rawCommands = collectCommands(manifest);
  if (rawCommands.length > MAX_COMMANDS) {
    errors.push(`声明的指令过多（${rawCommands.length} 条，上限 ${MAX_COMMANDS}）`);
  }
  const seen = new Set();
  const commands = [];
  for (const cmd of rawCommands.slice(0, MAX_COMMANDS)) {
    const row = { name: cmd.name, description: cmd.description, bodySource: 'missing', bodyChars: 0, error: null };
    const nameError = commandNameError(cmd.name);
    if (nameError) {
      errors.push(nameError);
      row.error = nameError;
      commands.push(row);
      continue;
    }
    const key = cmd.name.toLowerCase();
    if (seen.has(key)) {
      warnings.push(`指令「${cmd.name}」名称重复，只保留第一条`);
      continue;
    }
    seen.add(key);

    let body;
    if (cmd.content) {
      body = cmd.content;
      row.bodySource = 'content';
    } else if (cmd.file) {
      if (!isSafeRelative(cmd.file)) {
        row.error = `正文文件路径不安全：${cmd.file}`;
      } else {
        const rel = cmd.file.replace(/\\/g, '/');
        const full = path.join(dir, rel);
        if (!existsSync(full) || !statSync(full).isFile()) {
          row.error = `插件包内不存在文件「${rel}」`;
        } else {
          body = readBounded(full, rel, errors);
          row.bodySource = 'file';
        }
      }
    } else {
      const candidates = [
        `commands/${cmd.name}.md`,
        `commands/${cmd.name}.txt`,
        `commands/${cmd.name}/PROMPT.md`,
        `prompts/${cmd.name}.md`,
        `prompts/${cmd.name}.txt`,
        `.qoder-plugin/commands/${cmd.name}.md`,
        `.qoder-plugin/commands/${cmd.name}/PROMPT.md`,
        `.qoder-plugin/prompts/${cmd.name}.md`,
      ];
      const hit = candidates.find((c) => files.includes(c));
      if (hit) {
        body = readBounded(path.join(dir, hit), hit, errors);
        row.bodySource = 'convention';
      } else {
        row.error = `未提供指令正文（可添加 commands/${cmd.name}.md，或在 plugin.json 里写 content）`;
        warnings.push(`指令「${cmd.name}」没有正文，安装后能出现在 / 候选但无法取用`);
      }
    }
    row.bodyChars = body ? [...body].length : 0;
    if (row.bodyChars > MAX_BODY_CHARS) {
      warnings.push(
        `指令「${cmd.name}」正文 ${row.bodyChars} 字，超过 ${MAX_BODY_CHARS} 字的部分会在执行时截断`,
      );
    }
    commands.push(row);
  }
  if (commands.length === 0) {
    warnings.push('插件包没有声明任何指令，只能作为技能包使用');
  }

  return { ok: errors.length === 0, errors, warnings, manifest, commands, skillFiles };
}

function printReport(result, json) {
  if (json) {
    process.stdout.write(`${JSON.stringify(result, null, 2)}\n`);
    return;
  }
  const m = result.manifest ?? {};
  process.stdout.write(`套件：${m.name ?? '(未命名)'} ${m.version ?? ''}\n`);
  process.stdout.write(
    `指令 ${result.commands.length} 条 · SKILL.md ${result.skillFiles.length} 个\n`,
  );
  for (const c of result.commands) {
    const source = { content: '清单 content', file: '清单 file', convention: '约定文件', missing: '未提供正文' }[
      c.bodySource
    ];
    process.stdout.write(`  /${c.name}  ${source} · ${c.bodyChars} 字${c.error ? ` · ${c.error}` : ''}\n`);
  }
  for (const e of result.errors) process.stdout.write(`  [阻断] ${e}\n`);
  for (const w of result.warnings) process.stdout.write(`  [提醒] ${w}\n`);
  process.stdout.write(result.ok ? '校验通过（warning 不阻断安装）\n' : '校验未通过\n');
}

// ── STORE 直存 ZIP ───────────────────────────────────────────

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let i = 0; i < 256; i += 1) {
    let c = i;
    for (let k = 0; k < 8; k += 1) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[i] = c >>> 0;
  }
  return table;
})();

function crc32(buf) {
  let c = 0xffffffff;
  for (let i = 0; i < buf.length; i += 1) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
  return (c ^ 0xffffffff) >>> 0;
}

function buildZip(entries) {
  const locals = [];
  const centrals = [];
  let offset = 0;
  for (const { name, data } of entries) {
    const nameBytes = Buffer.from(name, 'utf8');
    const crc = crc32(data);
    const local = Buffer.alloc(30);
    local.writeUInt32LE(0x04034b50, 0);
    local.writeUInt16LE(20, 4);
    local.writeUInt16LE(0x0800, 6); // UTF-8 名称
    local.writeUInt16LE(0, 8); // STORE
    local.writeUInt16LE(0, 10);
    local.writeUInt16LE(0x0021, 12); // 1980-01-01
    local.writeUInt32LE(crc, 14);
    local.writeUInt32LE(data.length, 18);
    local.writeUInt32LE(data.length, 22);
    local.writeUInt16LE(nameBytes.length, 26);
    local.writeUInt16LE(0, 28);
    locals.push(local, nameBytes, data);

    const central = Buffer.alloc(46);
    central.writeUInt32LE(0x02014b50, 0);
    central.writeUInt16LE(20, 4);
    central.writeUInt16LE(20, 6);
    central.writeUInt16LE(0x0800, 8);
    central.writeUInt16LE(0, 10);
    central.writeUInt16LE(0, 12);
    central.writeUInt16LE(0x0021, 14);
    central.writeUInt32LE(crc, 16);
    central.writeUInt32LE(data.length, 20);
    central.writeUInt32LE(data.length, 24);
    central.writeUInt16LE(nameBytes.length, 28);
    central.writeUInt16LE(0, 30);
    central.writeUInt16LE(0, 32);
    central.writeUInt16LE(0, 34);
    central.writeUInt16LE(0, 36);
    central.writeUInt32LE(0, 38);
    central.writeUInt32LE(offset, 42);
    centrals.push(central, nameBytes);

    offset += local.length + nameBytes.length + data.length;
  }
  const centralBuf = Buffer.concat(centrals);
  const end = Buffer.alloc(22);
  end.writeUInt32LE(0x06054b50, 0);
  end.writeUInt16LE(0, 4);
  end.writeUInt16LE(0, 6);
  end.writeUInt16LE(entries.length, 8);
  end.writeUInt16LE(entries.length, 10);
  end.writeUInt32LE(centralBuf.length, 12);
  end.writeUInt32LE(offset, 16);
  end.writeUInt16LE(0, 20);
  return Buffer.concat([...locals, centralBuf, end]);
}

// ── 命令实现 ─────────────────────────────────────────────────

function cmdInit(target, flags) {
  if (!target) usage(1);
  const dir = path.resolve(process.cwd(), target);
  if (existsSync(path.join(dir, 'plugin.json'))) {
    fail(`目录已有 plugin.json：${dir}（不覆盖既有文件）`);
  }
  const display = (flags['--name'] ?? path.basename(dir) ?? '').trim() || '我的套件';
  const manifest = readJson(path.join(TEMPLATE_DIR, 'plugin.json'));
  if (manifest.__error) fail(`内置模板损坏：${manifest.__error}`);
  manifest.name = [...display].slice(0, 80).join('');
  const files = [
    ['plugin.json', `${JSON.stringify(manifest, null, 2)}\n`],
    ['commands/hello.md', readFileSync(path.join(TEMPLATE_DIR, 'commands', 'hello.md'), 'utf8')],
    ['skills/example/SKILL.md', readFileSync(path.join(TEMPLATE_DIR, 'skills', 'example', 'SKILL.md'), 'utf8')],
    [
      'README.md',
      readFileSync(path.join(TEMPLATE_DIR, 'README.md'), 'utf8').replaceAll('{{name}}', display),
    ],
  ];
  for (const [rel, content] of files) {
    const full = path.join(dir, rel);
    mkdirSync(path.dirname(full), { recursive: true });
    writeFileSync(full, content, 'utf8');
    process.stdout.write(`  + ${rel}\n`);
  }
  process.stdout.write(`已生成「${display}」于 ${dir}\n`);
  process.stdout.write('下一步：修改文件后运行 validate，再运行 pack 生成可安装的 .zip\n');
}

function cmdValidate(target, flags) {
  if (!target) usage(1);
  const result = validateDir(path.resolve(process.cwd(), target));
  printReport(result, Boolean(flags['--json']));
  process.exitCode = result.ok ? 0 : 1;
}

function cmdPack(target, flags) {
  if (!target) usage(1);
  const dir = path.resolve(process.cwd(), target);
  if (!flags['--out']) fail('缺少 -o <输出.zip>');
  const out = path.resolve(process.cwd(), flags['--out']);
  const result = validateDir(dir);
  if (!result.ok) {
    printReport(result, false);
    fail('校验未通过，未生成安装包（先修正上面的阻断项）');
  }
  const entries = walk(dir).map((rel) => ({
    name: rel,
    data: readFileSync(path.join(dir, rel)),
  }));
  const zip = buildZip(entries);
  mkdirSync(path.dirname(out), { recursive: true });
  writeFileSync(out, zip);
  printReport(result, false);
  process.stdout.write(`已打包 ${entries.length} 个文件（${(zip.length / 1024).toFixed(1)} KB）→ ${out}\n`);
}

const { positional, flags } = parseArgs(process.argv.slice(2));
const command = positional[0];
if (flags['--help'] || flags['-h']) usage(0);
if (command === 'init') cmdInit(positional[1], flags);
else if (command === 'validate') cmdValidate(positional[1], flags);
else if (command === 'pack') cmdPack(positional[1], flags);
else usage(1);
