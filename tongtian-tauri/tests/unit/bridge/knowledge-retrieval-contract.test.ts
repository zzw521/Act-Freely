// tests/unit/bridge/knowledge-retrieval-contract.test.ts — 知识库检索增强契约
//
// 检索质量由「候选召回 + 排序 + 读全文」三段共同决定，任何一段退回
// 「只按词频返回片段」都会让回答看起来有出处、实际答非所问。
// 这里用源码字面量把引擎、审批白名单、Rust 下发与页面说明钉在一起；
// 真实索引/分页/路径边界由 tests/integration/engine_knowledge_selftest.py 验证。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const engineKnowledge = () => read('../../../engine/knowledge_runtime.py');
const engineApproval = () => read('../../../engine/tool_approval.py');
const engineServer = () => read('../../../engine/server.py');
const rustRuntime = () => read('../../../src-tauri/src/bridge/runtime.rs');
const knowledgePage = () => read('../../../src/renderer/pages/Knowledge/index.tsx');

describe('知识库检索增强：召回 → 排序 → 读全文', () => {
  it('排序同时使用词法、查询覆盖与文档名，而不是只按词频', () => {
    const src = engineKnowledge();
    expect(src).toContain('LEXICAL_WEIGHT =');
    expect(src).toContain('COVERAGE_WEIGHT =');
    expect(src).toContain('TITLE_WEIGHT =');
    expect(src).toContain('def _rank_rows');
    expect(src).toContain('def _title_chunks');
    // 标题命中是补召回：正文命中的词法分不能被覆盖掉
    expect(src).toContain('candidates.setdefault((doc, seq)');
  });

  it('片段检索与读全文同时注册，提示词指向读全文', () => {
    const src = engineKnowledge();
    expect(src).toContain('toolkit.register_tool_function(search_knowledge)');
    expect(src).toContain('toolkit.register_tool_function(read_knowledge_document)');
    expect(src).toContain('def read_knowledge_document(');
    expect(src).toContain('nextOffset');
    expect(src).toContain('用 offset={next_offset} 继续');
    expect(src).toContain('read_knowledge_document 按出处里的文档名读取全文');
    // 没有可用知识库时两个工具都不注册
    expect(src).toContain('if not self.ready():');
  });

  it('读全文只接受 text/ 下的单层文件名，并如实处理跨库同名', () => {
    const src = engineKnowledge();
    expect(src).toContain('文档名不合法');
    expect(src).toContain('path.parent != text_dir');
    expect(src).toContain('同名文档出现在多个知识库');
    expect(src).toContain('MAX_READ_CHARS');
  });

  it('只读工具审批白名单包含读全文，敏感模式不误弹窗', () => {
    expect(engineApproval()).toContain('"read_knowledge_document"');
  });

  it('引擎从会话 runtime 建知识库运行时并交给 build_agent', () => {
    const src = engineServer();
    expect(src).toContain('kn.KnowledgeRuntime.from_runtime(session.runtime)');
    expect(src).toContain('knowledge=knowledge');
    expect(src).toContain('knowledge.install(toolkit)');
  });

  it('Rust 仍下发知识库运行时块，页面说明与真实工具一致', () => {
    expect(rustRuntime()).toContain('"knowledge": knowledge::runtime_bases(&knowledge_root)');
    expect(knowledgePage()).toContain('read_knowledge_document');
  });
});
