import {
  Callout,
  Divider,
  Grid,
  H1,
  H2,
  Stack,
  Stat,
  Table,
  Tag,
  Text,
  Timeline,
} from 'qoder/canvas';

export default function SixExpertRequirementsAnalysisReport() {
  return (
    <Stack gap={20}>
      <Stack gap={8}>
        <H1>六专家需求分析 · 完成报告</H1>
        <Text tone="secondary">
          通天办公 · Agent 办公系统三层架构（Agent 平台 / 用户端 / 管理控制台）需求分析 ——
          /apex-sdd:explore 流程：6 位专家独立分析 + 5 轮汇总式裁决 + TL 汇总落盘
        </Text>
      </Stack>

      <Grid columns={4} gap={12}>
        <Stat value="6" label="专家分报告" description="并行 subagent 独立分析" />
        <Stat value="5 轮 / 19 项" label="冲突裁决" description="R1-R5 全部经用户确认" tone="success" />
        <Stat value="17 / 15 / 6" label="风险汇总（高/中/低）" description="高风险均有 P0 缓解建议" tone="warning" />
        <Stat value="16 项" label="待澄清遗留" description="均附专家推荐答案" />
      </Grid>

      <Callout tone="success" title="任务已完成并通过逐项审计">
        最终需求分析报告已落盘 docs/changes/agent-platform-evolution/requirements-analysis-report.md（35.5KB），
        六份专家分报告作为证据链保留于 experts/ 目录。总体结论：三层架构是现有云端体系的结构性演进而非推倒重来——
        组件重组（Runner+Gateway 下沉为平台）、契约加层（薄壳 BFF）、能力补全（知识/调度/预算硬控/平台运营/注入防御）。
      </Callout>

      <Divider />

      <H2>关键步骤</H2>
      <Timeline
        events={[
          {
            id: 's1',
            timestamp: '阶段 0',
            title: '需求理解与文档调研',
            description: '通读主 SPEC v2.1、云端与账号体系 spec、占位能力清单；确认 apex-sdd 技能链；裁决系统定位（云端体系演进）与迭代模式（汇总式裁决）',
          },
          {
            id: 's2',
            timestamp: '阶段 1',
            title: '六位专家并行独立分析',
            description: '产品/架构/业务/运营/安全/测试 6 个 GeneralPurpose subagent 并行执行，统一任务书 + 统一输出格式（结论摘要/风险清单/建议优先级/待澄清），落盘 experts/01~06',
          },
          {
            id: 's3',
            timestamp: '阶段 2 · R1-R2',
            title: '三层边界与容量稳定性裁决',
            description: '薄壳 BFF 转发、任务权威状态归平台、桌面端为用户端形态之一；并行上限 500/50/3、仅 executing 计数、离线按时长分策略、禁止抢占接管',
          },
          {
            id: 's4',
            timestamp: '阶段 2 · R3-R4',
            title: 'MVP 边界与安全合规裁决',
            description: '知识收敛为工作区文档问答 P1、技能市场 Phase C 只读分发版、控制台审计+成本全含、IM 仅 Webhook 预留；Confidential 硬拒绝、审计哈希为主+高危全文 90 天、注入分级处置',
          },
          {
            id: 's5',
            timestamp: '阶段 2 · R5',
            title: '量化定稿',
            description: 'SSE 双口径分责、漂移检测月预算 2% 封顶、免费配额 20 万 token/日、超限动作降级→排队→阻断（组织可覆盖）',
          },
          {
            id: 's6',
            timestamp: '阶段 3',
            title: 'TL 汇总与落盘',
            description: '五段结构最终报告（六视角/裁决表/风险汇总/MVP 建议/待澄清清单）+ 一致性检查（10 项下游文档修订清单）+ 附录 A 冻结基线速查表',
          },
          {
            id: 's7',
            timestamp: '审计',
            title: '完成度逐项审计',
            description: '对照计划文件逐条验证：7 个交付文件存在、报告五段结构完整、质量门四项通过，goal 标记 complete',
          },
        ]}
      />

      <H2>变更文件</H2>
      <Table
        headers={['文件', '类型', '说明']}
        rows={[
          [
            'docs/changes/agent-platform-evolution/requirements-analysis-report.md',
            '最终交付物',
            'TL 汇总需求分析报告（35.5KB）：六大视角结果、19 项冲突裁决表、风险汇总（38 项）、MVP 建议（范围/Not Doing/里程碑）、16 项待澄清清单、一致性检查',
          ],
          [
            'docs/changes/agent-platform-evolution/experts/01-product.md',
            '专家分报告',
            '产品视角：北极星「周交付任务数」、分阶段量化目标、功能优先级、MVP 边界与 Phase A/B/C 差异对比',
          ],
          [
            'docs/changes/agent-platform-evolution/experts/02-architecture.md',
            '专家分报告',
            '架构视角：三层边界与依赖机制、六组件复用/扩展/新增映射、容量估算（BFF 1500 QPS/事件总线 10K msg/s/Runner 250 vCPU）、长任务稳定性',
          ],
          [
            'docs/changes/agent-platform-evolution/experts/03-business.md',
            '专家分报告',
            '业务视角：14 态云端状态机（35 条转移）、14 个异常场景处置矩阵、4 类三方闭环、跨端裁决与续接规则',
          ],
          [
            'docs/changes/agent-platform-evolution/experts/04-operations.md',
            '专家分报告',
            '运营视角：五层 18 项指标体系、7 大运营抓手、四级配额 + 四档预警 + 三级超限动作、月成本 ¥28-40 万估算',
          ],
          [
            'docs/changes/agent-platform-evolution/experts/05-security.md',
            '专家分报告',
            '安全视角：四类零覆盖攻击面、L1-L5 注入防御链（逐层验收标准）、RBAC 矩阵、审计事件覆盖清单',
          ],
          [
            'docs/changes/agent-platform-evolution/experts/06-testing.md',
            '专家分报告',
            '测试视角：20 项三层验收指标（M1-M20）、LLM 黄金集与语义回放、8 场景压测矩阵 + T1-T10 专项、契约双层 CI 门禁',
          ],
        ]}
      />

      <H2>验证证据</H2>
      <Table
        headers={['计划要求', '证据', '结论']}
        rows={[
          ['6 位专家独立分析并落盘', 'experts/ 目录 6 个文件（18-30KB），抽查含统一四段结构', '通过'],
          ['5 轮汇总式裁决循环', 'R1-R5 共 19 项裁决（ADJ-R1-1 ~ ADJ-R5-4），每轮经 AskUserQuestion 获用户裁决', '通过'],
          ['最终报告落盘指定位置', 'requirements-analysis-report.md 存在（35,508 字节）', '通过'],
          ['报告五段结构', '§1 六视角 / §2 裁决表 / §3 风险汇总 / §4 MVP 建议 / §5 待澄清清单 章节均存在', '通过'],
          ['一致性检查', '§6 输出 10 项下游文档修订清单（云端 SPEC §2/§5/§7/§8/§10、主 SPEC §3.1/§5.3/§6.1 等）', '通过'],
          ['质量门：指标量化', '附录 A 容量与配额基线速查表全部为裁决后冻结数值', '通过'],
          ['质量门：依赖显式声明', '前端→各自后端→平台单向依赖 + 私有子网/服务凭据/CSP/CI 扫描四重机制', '通过'],
          ['质量门：高风险有缓解建议', '17 项高风险逐条对应缓解建议与优先级（P0/P1）', '通过'],
        ]}
        rowTone={[
          'success',
          'success',
          'success',
          'success',
          'success',
          'success',
          'success',
          'success',
        ]}
      />

      <H2>核心裁决结论速览</H2>
      <Stack gap={8}>
        <Stack gap={6}>
          <Text weight="semibold">三层边界与契约（R1）</Text>
          <Text tone="secondary">
            薄壳 BFF 转发（/api/v1 外壳保留）· 任务权威状态归平台 task-service · 桌面端为用户端的一种形态（local-first 不变）
          </Text>
        </Stack>
        <Stack gap={6}>
          <Text weight="semibold">容量与稳定性（R2）</Text>
          <Text tone="secondary">
            并行上限 全局500/组织50/用户3 配置化 · 仅 executing 计数 · 离线按时长分策略（5min 阈值）· 禁止抢占、空闲 5min 释放
          </Text>
        </Stack>
        <Stack gap={6}>
          <Text weight="semibold">MVP 边界（R3）</Text>
          <Text tone="secondary">
            知识=工作区文档问答 P1 · 技能市场=Phase C 只读分发版 · 控制台审计+成本全含 · IM 仅 Webhook 预留
          </Text>
        </Stack>
        <Stack gap={6}>
          <Text weight="semibold">安全合规（R4）</Text>
          <Text tone="secondary">
            Confidential 硬拒绝 · 审计哈希为主+高危全文 90 天 · agent 消息限同任务链 · 注入分级处置（误报 &lt;1%、P95 &lt;200ms）
          </Text>
        </Stack>
        <Stack gap={6}>
          <Text weight="semibold">量化定稿（R5）</Text>
          <Text tone="secondary">
            SSE 双口径（服务内 500ms / 端到端 1.5s）· 漂移检测月预算 2% 封顶 · 免费配额 20 万 token/日 · 超限降级→排队→阻断
          </Text>
        </Stack>
      </Stack>

      <Divider />

      <H2>最终结果</H2>
      <Stack gap={8}>
        <Text>
          需求分析报告以「现有云端体系演进」为定位完成：Phase A 原样执行，Phase B 6→8~9 周，Phase C 6→7~8 周；
          MVP 验收硬指标——峰值 2000 会话 SSE 断开率 = 0、超限 100% 走降级/排队、预算硬停 ≤5s（误差 ≤±2%）、平台可用性 99.9%。
        </Text>
        <Stack gap={6}>
          <Text weight="semibold">后续轮次（/apex-sdd:explore 流水线）</Text>
          <Grid columns={3} gap={8}>
            <Tag tone="info">PRD（按应用拆分）</Tag>
            <Tag tone="info">前端脚手架（用户端/控制台）</Tag>
            <Tag tone="info">ARCH + ADR</Tag>
          </Grid>
        </Stack>
      </Stack>

      <Text tone="secondary" size="small">
        生成于 2026-08-05 · 交付目录 docs/changes/agent-platform-evolution/ · 流程 /apex-sdd:explore（idea-refine + grill-me 汇总式）
      </Text>
    </Stack>
  );
}
