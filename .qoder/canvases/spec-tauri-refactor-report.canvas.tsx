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
} from 'qoder/canvas';

export default function SpecTauriRefactorReport() {
  return (
    <Stack gap={20}>
      <Stack gap={8}>
        <H1>Spec 重构完成报告：Tauri + Rust 基础架构</H1>
        <Text tone="secondary">
          将通天办公全部顶层 spec 与架构设计文件由「Electron + Node/TS」整体重写为「Tauri 2.x + Rust 全栈核心，Python 仅按需辅助」，文档版本统一升级至 v2.0。
        </Text>
      </Stack>

      <Grid columns={4} gap={16}>
        <Stat value="3" label="重写文档数" />
        <Stat value="2587" label="交付总行数" />
        <Stat value="9" label="Rust crate 定义" />
        <Stat value="100%" label="计划交付项完成率" tone="success" />
      </Grid>

      <Divider />

      <H2>成果摘要</H2>
      <Table
        headers={['交付物', '版本', '核心内容']}
        rows={[
          [
            'docs/spec/spec-通天办公.md（主 SPEC）',
            'v1.1 → v2.0（1913 行）',
            '架构总览、八维度选型评分、8 大模块 Rust 契约、SQLite DDL、Tauri command 契约表、capability 安全配置、tauri-bundler 打包、测试策略与非功能指标',
          ],
          [
            'docs/spec/spec-通天办公-qwenpaw-改造方案.md',
            'v2.0（268 行）',
            '改写为《Electron → Tauri 架构演进方案》：收益量化、现有代码资产迁移映射、QwenPaw 能力处置矩阵、四阶段迁移路径、风险评估',
          ],
          [
            'tongtian-office-mvp-design.md',
            'v2.0（406 行）',
            '桌面壳结论反转为 Tauri、Rust core 即后端、9 crate 工程结构、任务清单 T0-T11、Mock 开关与 init.sh 规范',
          ],
        ]}
      />

      <H2>关键步骤</H2>
      <Table
        headers={['步骤', '动作', '结果']}
        rows={[
          ['1. 调研', '通读三份现有文档全文（3292 行）与 apex-sdd 技能模板', '确认沿用现有 spec 章节结构作为模板'],
          ['2. 决策确认', '与用户确认 Rust/Python 分工与重构范围', '定为 Rust 全栈 + Python 仅辅助；范围限定顶层三份文档'],
          ['3. 主 SPEC 重写', '分段写入架构/选型/模块详设/数据模型/API 契约/安全/打包/测试/NFR', '1913 行，v2.0'],
          ['4. 演进方案重写', '资产盘点映射 + QwenPaw 能力处置 + 四阶段迁移路径', '268 行，v2.0'],
          ['5. MVP 设计重写', '选型反转 + Cargo workspace 结构 + 任务清单重映射', '406 行，v2.0'],
          ['6. 交叉验证', 'grep 核验章节结构、crate 命名、旧架构残留', '全部通过'],
        ]}
      />

      <H2>变更文件</H2>
      <Table
        headers={['文件', '变更类型', '规模变化']}
        rows={[
          ['docs/spec/spec-通天办公.md', '整篇重写', '1663 行 → 1913 行'],
          ['docs/spec/spec-通天办公-qwenpaw-改造方案.md', '整篇重写（主题改为架构演进）', '537 行 → 268 行'],
          ['tongtian-office-mvp-design.md', '整篇重写', '392 行 → 406 行'],
        ]}
        rowTone={['success', 'success', 'success']}
      />
      <Text tone="secondary" size="small">
        范围约束遵守：docs/changes/ 历史变更 spec 未改动；通天办公-PRD.html 业务需求未改；tongtian-office/ 现有代码未改。
      </Text>

      <Divider />

      <H2>验证证据</H2>
      <Table
        headers={['验证项', '方法', '结论']}
        rows={[
          ['版本与基调', '读取三份文档头部', '均为 v2.0，明确声明 Tauri 2.x + Rust 全栈核心'],
          ['章节完整性', 'grep 标题结构', '主 SPEC §1-§9 + 附录 A/B 齐全；演进方案 §1-§7 齐全；MVP 设计 §1-§8 齐全'],
          ['crate 命名一致性', 'grep crates/* 命名', '三份文档 9 个 crate（agent/model-hub/file-ops/storage/workspace/skills/mcp/sandbox/python-bridge）完全一致'],
          ['接口命名一致性', '对照 command 契约', '统一 domain_action 蛇形命名（chat_submit 等 32 个），event 通道 agent://event 一致'],
          ['旧架构残留', 'grep Electron/keytar/better-sqlite3 等', '仅存在于选型对比与迁移历史语境，无错误残留'],
          ['开发规范落地', 'grep Mock 开关 / init.sh', 'MOCK_ENABLED 开关表、分支覆盖率 ≥80%、幂等 init.sh 规范均已写入'],
        ]}
      />

      <Callout tone="success" title="最终结果">
        <Stack gap={6}>
          <Text>
            计划全部交付物已落地并通过逐项审计：三份文档构成自洽的 Tauri + Rust 目标态规格体系（主 SPEC 定义架构与契约、演进方案定义迁移路径、MVP 设计定义落地结构）。
          </Text>
          <Text>
            预期收益：安装包体积约 25MB（-85%）、冷启动 &lt; 1s（-70%）、空闲内存约 120-150MB（-70%）、单二进制分发无运行时依赖。
          </Text>
          <Text>
            下一步建议：运行 /apex-sdd:plan 将 SPEC v2.0 拆解为有序实现任务（可按后端 / 前端双线并行分解）。
          </Text>
        </Stack>
      </Callout>

      <Stack gap={4}>
        <Tag tone="info">Tauri 2.x</Tag>
        <Text tone="secondary" size="small">
          生成时间：2026-08-05 · 纯文档重构任务，无运行时产物与截图。
        </Text>
      </Stack>
    </Stack>
  );
}
