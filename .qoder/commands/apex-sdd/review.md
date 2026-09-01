---
description: Conduct a five-axis code review — correctness, readability, architectures-design.md, security, performance
---

Invoke the agent-skills:code-review-and-quality skill.

Review the current changes (staged or recent commits) across all five axes:

1. **Correctness** — Does it match the spec? Edge cases handled? Tests adequate?
2. **Readability** — Clear names? Straightforward logic? Well-organized?
3. **Architecture** — Follows existing patterns? Clean boundaries? Right abstraction level?
4. **Security** — Input validated? Secrets safe? Auth checked? (Use security-and-hardening skill)
5. **Performance** — No N+1 queries? No unbounded ops? (Use performance-optimization skill)

Categorize findings as Critical, Important, or Suggestion.
Output a structured review with specific file:line references and fix recommendations.

**Next step:** Run `/apex-sdd:ship` to run the pre-launch checklist and make the go/no-go decision.

> Suggested prompt: `/apex-sdd:ship 业务：{业务}，基于端到端集成测试报告执行上线前检查清单，输出 GO / NO-GO 决策与回滚方案`
