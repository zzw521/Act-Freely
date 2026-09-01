import {
  Stack,
  Row,
  Grid,
  Divider,
  H1,
  H2,
  H3,
  Text,
  Table,
  Stat,
  Tag,
  Callout,
  Timeline,
  Card,
  CardBody,
  canvasImage,
} from 'qoder/canvas';

const workbenchShot = canvasImage('./verify-workbench.png');
const connectorsShot = canvasImage('./verify-connectors.png');
const settingsShot = canvasImage('./verify-settings.png');

export default function QwenworkAlignmentReport() {
  return (
    <Stack gap={24}>
      <Stack gap={8}>
        <H1>千问办公对标整改 · 完成报告</H1>
        <Text tone="secondary">
          调研 QwenWork 桌面端功能全景，对标现有「通天办公」UI，产出整改设计 SPEC 并完成全部 UI 实现与浏览器验证。
        </Text>
        <Row gap={8}>
          <Tag tone="success">目标已完成</Tag>
          <Tag>2026-08-04</Tag>
          <Tag>tongtian-office</Tag>
        </Row>
      </Stack>

      <Grid columns={4} gap={16}>
        <Stat value="1" label="SPEC 设计文档（696 行）" tone="success" />
        <Stat value="6" label="新增页面" tone="success" />
        <Stat value="10" label="变更文件" />
        <Stat value="8" label="浏览器验证通过页面" tone="success" />
      </Grid>

      <Divider />

      <H2>成果摘要</H2>
      <Stack gap={12}>
        <Callout tone="success" title="设计交付">
          完成 docs/changes/qwenwork-ui-alignment/spec.md：对标调研（QwenWork 14 大能力域、设置三分组架构、Hooks 12
          事件模型、连接器授权模型）、18 项差距矩阵（P0-P2 优先级）、信息架构与路由重组、逐页 UI 设计、HookRunner 等新模块设计、
          增量数据模型与 26 个 IPC 契约、实施分期。
        </Callout>
        <Callout tone="info" title="实现交付">
          按 SPEC 完整实现渲染层整改：连接器中心 / Hooks 管理 / 意识 / 电脑操控 / IM 频道 / 归档共 6 个新页面；设置页重组为
          通用 / 扩展与集成 / 高级设置三分组（12 个分区全部落地）；侧边栏路由修正；工作台新增模式切换胶囊组（通用/设计/幻灯片/写作）
          与模型档位选择器（自动/快速/旗舰）。
        </Callout>
      </Stack>

      <H2>关键步骤</H2>
      <Timeline
        events={[
          {
            title: '对标调研',
            description: '抓取 qwenwork.cn 官方文档与阿里云帮助镜像，核实系统设置三分组结构与 Hooks 事件全文',
            tone: 'success',
          },
          {
            title: '差距分析',
            description: '盘点现有 9 个页面与 15 个组件，输出 18 项差距矩阵（G01-G18）并分级',
            tone: 'success',
          },
          {
            title: 'SPEC 编写',
            description: '产出 7 章 696 行整改设计方案，含页面级 UI 设计、模块设计、SQLite/IPC 增量契约',
            tone: 'success',
          },
          {
            title: 'UI 实现',
            description: '新增 6 页面 + Settings 三分组重写 + Sidebar/App 路由 + Workbench 改造，共 10 个文件变更',
            tone: 'success',
          },
          {
            title: '验证闭环',
            description: 'renderer typecheck 零错误；Vite dev server 启动后逐页浏览器验证，8 个页面全部渲染正常并留截图证据',
            tone: 'success',
          },
        ]}
      />

      <H2>变更文件</H2>
      <Table
        headers={['文件', '类型', '说明']}
        rows={[
          ['docs/changes/qwenwork-ui-alignment/spec.md', '新增', '整改设计 SPEC（696 行，7 章）'],
          ['pages/ConnectorCenter/index.tsx', '新增', '连接器中心：内置/集成市场 + 授权流 + 状态徽标'],
          ['pages/HooksManager/index.tsx', '新增', 'Hooks 管理：12 事件列表 + 编辑器 + 试运行 + 内置模板'],
          ['pages/AwarenessPage/index.tsx', '新增', '意识页：协作风格 / 工作手册 / 自动记忆三区块'],
          ['pages/ComputerUsePanel/index.tsx', '新增', '电脑操控：三档策略 + 权限引导 + 四段式透明度 + 风险警示'],
          ['pages/IMChannel/index.tsx', '新增', 'IM 频道：渠道列表 + 回复策略 + 企业 IM 能力分组开关'],
          ['pages/ArchiveView/index.tsx', '新增', '归档页：恢复/删除/全部删除（DELETE 确认词）'],
          ['pages/Settings/index.tsx', '重写', '三分组导航 + 12 分区（偏好/个人资料/系统/语音/快照/快捷键/意识/更新/工作台/模型/沙箱/实验）'],
          ['components/Layout/Sidebar.tsx', '修改', '连接器→/connectors、IM→/im、新增电脑操控入口'],
          ['App.tsx + pages/Workbench/index.tsx', '修改', '注册 6 条新路由；工作台增加 ModeSwitcher（模式+档位）'],
        ]}
      />

      <H2>验证证据</H2>
      <Table
        headers={['页面', '路由', '验证方式', '结果']}
        rows={[
          ['工作台', '/', '截图 + 文本断言', '模式胶囊组与档位选择器渲染正常'],
          ['连接器中心', '/connectors', '截图 + 文本断言', '内置/市场 Tab、状态徽标、授权文案正常'],
          ['设置（系统设置分区）', '/settings?s=system', '截图 + 文本断言', '三分组导航 + 7 项系统权限清单正常'],
          ['Hooks 管理', '/hooks', '文本断言', '12 事件 + hook 计数 + 阻塞语义说明正常'],
          ['意识', '/awareness', '文本断言', '风格/工作手册/记忆条目三区块正常'],
          ['电脑操控', '/computer-use', '文本断言', '三档策略 + 四段式透明度 + 风险提示正常'],
          ['IM 频道', '/im', '文本断言', '渠道绑定 + 回复策略 + 能力分组开关正常'],
          ['已归档', '/archive', '文本断言', '归档列表 + 恢复/删除操作正常'],
        ]}
      />

      <H3>工作台（模式切换 + 模型档位）</H3>
      <Card>
        <CardBody>
          <img src={workbenchShot} alt="工作台页面：模式切换胶囊组与模型档位选择器" style={{ width: '100%', borderRadius: 8 }} />
        </CardBody>
      </Card>

      <H3>连接器中心（授权流）</H3>
      <Card>
        <CardBody>
          <img src={connectorsShot} alt="连接器中心：内置连接器与市场 Tab" style={{ width: '100%', borderRadius: 8 }} />
        </CardBody>
      </Card>

      <H3>设置页（三分组重组）</H3>
      <Card>
        <CardBody>
          <img src={settingsShot} alt="设置页：通用/扩展与集成/高级设置三分组导航" style={{ width: '100%', borderRadius: 8 }} />
        </CardBody>
      </Card>

      <Divider />

      <H2>最终结论</H2>
      <Callout tone="success" title="整改目标达成">
        千问办公桌面端对标整改的设计与实现已全部完成：SPEC 文档、6 个新页面、设置三分组重组、导航路由修正、工作台模式与档位改造均已落地，
        renderer 类型检查零错误，8 个页面经浏览器实际渲染验证通过。下一步可运行 /apex-sdd:plan 将 SPEC 拆解为实现任务，
        或按 SPEC 第 6 章补齐主进程侧的 HookRunner、AwarenessService 等服务与 IPC 实现。
      </Callout>

      <Text tone="secondary" size="small">
        验证环境：Vite 5.4 dev server @ localhost:5199 · 截图与本报告同目录（verify-*.png）
      </Text>
    </Stack>
  );
}
