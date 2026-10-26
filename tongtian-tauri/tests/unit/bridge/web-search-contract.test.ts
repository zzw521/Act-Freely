// tests/unit/bridge/web-search-contract.test.ts — 联网搜索契约
//
// 搜索把「公开网页」带进模型上下文，风险与能力同时放大：配置了不等于授权，
// 授权了也不能绕过白名单/SSRF/审计。这里用源码字面量钉住注册门槛、请求形态、
// 结果边界与引擎接线；真实 HTTP 行为由 tests/integration/engine_web_search_selftest.py 验证。

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const engineWebSearch = () => read('../../../engine/web_search.py');
const engineApproval = () => read('../../../engine/tool_approval.py');
const engineServer = () => read('../../../engine/server.py');
const envExample = () => read('../../../.env.example');

describe('联网搜索：配置 → 授权 → 真实检索', () => {
  it('三种搜索源与 Mock 开关都来自显式配置', () => {
    const src = engineWebSearch();
    expect(src).toContain('PROVIDERS = ("searxng", "tavily", "serper")');
    expect(src).toContain('TT_SEARCH_PROVIDER');
    expect(src).toContain('TT_SEARCH_URL');
    expect(src).toContain('TT_SEARCH_API_KEY');
    expect(src).toContain('TT_SEARCH_MOCK');
    // searxng 无默认端点：必须给自建实例地址，不能猜一个公网服务
    expect(src).toContain('需要配置 TT_SEARCH_URL');
    expect(src).toContain('DEFAULT_ENDPOINTS');
  });

  it('配置了也不等于可用：联网授权、白名单与策略错误都会拦住', () => {
    const src = engineWebSearch();
    expect(src).toContain('def denial(self)');
    expect(src).toContain('没有沙箱运行时策略');
    expect(src).toContain('用户未授权联网获取');
    expect(src).toContain('不在联网白名单内');
    expect(src).toContain('沙箱策略读取失败');
    // Mock 不发网络请求，但仍走同一套注册门槛
    expect(src).toContain('Mock 不发网络请求，但仍要求显式授权');
    expect(src).toContain('if not self.ready:');
  });

  it('真实请求沿用策略校验，重定向逐跳复核且响应有上限', () => {
    const src = engineWebSearch();
    expect(src).toContain('_CheckedRedirect(validate)');
    expect(src).toContain('self.grants.network_denial(next_url)');
    expect(src).toContain('MAX_SEARCH_BYTES');
    expect(src).toContain('SEARCH_TIMEOUT');
    expect(src).toContain('MAX_QUERY_CHARS');
    expect(src).toContain('MAX_HITS');
  });

  it('三种搜索源的请求形态与解析各归各位', () => {
    const src = engineWebSearch();
    expect(src).toContain('urllib.parse.urlencode({"q": query, "format": "json"})');
    expect(src).toContain('"api_key": self.api_key');
    expect(src).toContain('"max_results": top_k');
    expect(src).toContain('"num": top_k');
    expect(src).toContain('headers["X-API-KEY"] = self.api_key');
    expect(src).toContain('PARSERS = {');
    expect(src).toContain('def parse_tavily');
    expect(src).toContain('def parse_serper');
    expect(src).toContain('def parse_searxng');
  });

  it('结果只保留 http(s) 且审计不记查询正文', () => {
    const src = engineWebSearch();
    expect(src).toContain('parts.scheme.lower() not in ("http", "https")');
    expect(src).toContain('parts.username or parts.password');
    expect(src).toContain('"action": "network"');
    expect(src).toContain('查询 {len(str(query or \'\'))} 字');
    expect(src).toContain('provider={self.provider} results={len(rows)}');
    // 摘要不是原文，Mock 结果不得当事实引用
    expect(src).toContain('摘要不是原文');
    expect(src).toContain('不得作为事实引用');
  });

  it('联网搜索是敏感工具，审批模式不能当成只读放行', () => {
    const block = engineApproval().match(/READ_ONLY_TOOLS\s*=\s*frozenset\(\{([\s\S]*?)\}\)/);
    expect(block).not.toBeNull();
    expect(block![1]).not.toContain('"web_search"');
  });

  it('引擎只把搜索工具交给父 Agent，并合并可见说明', () => {
    const src = engineServer();
    expect(src).toContain('web = ws.WebSearchRuntime.from_env(grants)');
    expect(src).toContain('web=web,');
    expect(src).toContain('*web.notes()');
    expect(src).toContain('web.install(toolkit, emit=emit, task_id=task_id)');
    expect(src).toContain('sys_prompt += web.system_hint()');
    // 子 Agent 不继承联网搜索（与执行/浏览器/电脑操控的边界一致）
    const child = src.slice(src.indexOf('def _make_child_agent'), src.indexOf('subagent_runner = sar.SubagentRunner'));
    expect(child).not.toContain('web=');
  });

  it('示例环境变量把「配置」与「授权」分开说明', () => {
    const env = envExample();
    expect(env).toContain('TT_SEARCH_PROVIDER=');
    expect(env).toContain('TT_SEARCH_URL=');
    expect(env).toContain('TT_SEARCH_API_KEY=');
    expect(env).toContain('TT_SEARCH_MOCK=');
    expect(env).toContain('联网白名单');
  });
});
