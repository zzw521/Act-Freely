import {
  Callout,
  canvasImage,
  Card,
  CardBody,
  CardHeader,
  CollapsibleSection,
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

const imgRegisterPage = canvasImage('./step1-register-page.png');
const imgRegisterRedirect = canvasImage('./step2-register-redirect.png');
const imgLoginPage = canvasImage('./step3-login-page.png');
const imgLoginError = canvasImage('./step4-login-error.png');

export default function AuthLoopCompletionReport() {
  return (
    <Stack gap={20}>
      <H1>退出登录与网页验证登录闭环 — 完成报告</H1>
      <Text tone="secondary">
        通天办公(Tauri 版)账号体系落地:Loopback 回调方案打通桌面端网页验证登录,本地 FastAPI Auth
        服务提供网页端注册/登录/退出,两处退出入口真实接线,全程带 TT_AUTH_MOCK 双 Mock 开关。
      </Text>

      <Grid columns={4} gap={16}>
        <Stat value="2/2" label="目标需求完成" tone="success" />
        <Stat value="93" label="Rust 测试通过" tone="success" />
        <Stat value="20" label="Python 断言通过" tone="success" />
        <Stat value="122" label="前端 vitest 通过" tone="success" />
      </Grid>

      <Divider />

      <H2>成果摘要</H2>
      <Table
        headers={['需求', '交付内容', '状态']}
        rows={[
          [
            '1. 本地退出登录与点击跳转网页登录验证',
            'Rust auth_status/auth_login/auth_logout 三件套 command;loopback 回调 + state 校验 + code 换 token + keyring 存储;Sidebar 与 Settings 两处退出入口接线(确认弹窗 + Snackbar);未登录点击用户卡片直接触发网页登录',
            '已完成',
          ],
          [
            '2. 网页端登录、注册、退出功能',
            'auth-server(FastAPI):品牌化登录/注册网页 + register/login/exchange/refresh/logout API;手写 HS256 JWT(access 15min + refresh 30d 轮换);一次性授权码绑定 state;回调地址仅允许 loopback',
            '已完成',
          ],
        ]}
      />

      <Callout tone="info" title="方案选型:Loopback(方案 B)">
        tokio(net)/reqwest/keyring/tauri-plugin-opener 全部为现成依赖,零新增 crate,无需系统协议注册,跨平台无差异,
        较 Deep Link 更简单。云端 Phase A 上线后仅需更换 TT_AUTH_SERVER_URL,客户端契约不变。
      </Callout>

      <H2>关键步骤</H2>
      <Timeline
        events={[
          {
            title: '网页端 Auth 服务',
            description:
              'auth-server/server.py:users/sessions 表、pbkdf2 密码哈希、JWT 签发与轮换、登录/注册页内嵌模板',
            tone: 'success',
          },
          {
            title: 'Rust auth 模块',
            description:
              'auth/mod.rs + service.rs:loopback TcpListener 收码、state 校验、code 换 token、Vault(keyring)与 settings_kv 持久化;settings 新增 delete',
            tone: 'success',
          },
          {
            title: '前端契约与状态',
            description:
              'contracts Auth 类型与 auth:* channel、tauri-api/mock-api/api.ts 三层适配、useAuthStore(localStorage 兜底)',
            tone: 'success',
          },
          {
            title: 'UI 接线',
            description:
              'Sidebar 用户卡片/菜单动态化 + 退出确认弹窗;Settings 个人资料 UID/套餐动态化 + 登录/退出按钮',
            tone: 'success',
          },
          {
            title: '配置与脚本',
            description:
              '.env.example 增补 TT_AUTH_MOCK/TT_AUTH_SERVER_URL;init.sh/init.ps1 联调模式自动拉起 auth-server',
            tone: 'success',
          },
        ]}
      />

      <H2>变更文件</H2>
      <Table
        headers={['层次', '文件', '变更']}
        rows={[
          ['网页端', 'auth-server/server.py', '新增:FastAPI Auth 服务(页面 + 5 个 API)'],
          ['网页端', 'auth-server/test_auth.py', '新增:全链路冒烟测试(20 项断言)'],
          ['Rust', 'src-tauri/src/auth/mod.rs · service.rs', '新增:auth 三件套 command 与核心逻辑'],
          ['Rust', 'src-tauri/src/lib.rs', '修改:注册 mod auth 与 3 个 command(共 29 个)'],
          ['Rust', 'src-tauri/src/store/settings.rs', '修改:新增 delete(退出清理 profile)'],
          ['前端', 'src/shared/contracts.ts', '修改:AuthProfile/AuthStatus/AuthLoginResp + auth channel'],
          ['前端', 'src/renderer/lib/tauri-api.ts · mock-api.ts · api.ts', '修改:auth 三通道适配 + authAPI'],
          ['前端', 'src/renderer/stores/useAuthStore.ts · index.ts', '新增:登录态 store 与导出'],
          ['前端', 'Sidebar.tsx · Settings/index.tsx', '修改:退出/登录入口真实接线'],
          ['配置', '.env.example · init.sh · init.ps1 · .gitignore', '修改:Mock 开关与 Auth 服务拉起'],
          ['测试', 'tests/unit/auth/auth.test.ts', '新增:12 项前端单测'],
        ]}
      />

      <H2>验证证据</H2>
      <Table
        headers={['层级', '内容', '结果']}
        rows={[
          ['Rust', 'cargo test --lib(auth 模块 + settings delete + 全量回归)', '93 passed / 0 failed'],
          [
            'Python',
            'auth-server/test_auth.py(注册→回跳→换 token→轮换→吊销 + 错误分支)',
            '20 项断言全过, ExitCode=0',
          ],
          ['前端', 'npx vitest run(新增 auth 12 项 + 全量回归)', '122 passed / 7 files'],
          ['类型检查', 'npx tsc --noEmit', '零错误'],
          [
            '浏览器 E2E',
            '注册提交→302 携带 code/state;登录→新 code;错误密码页面拦截不跳转',
            '4 步全部通过',
          ],
          [
            '闭环取证',
            '浏览器真实 code 换 token(uid=tt-245b48e029)→logout revoked=true→退出后 refresh 401 拒绝',
            '符合预期',
          ],
        ]}
      />

      <CollapsibleSection title="浏览器交互验证截图(网页端登录/注册页)" defaultOpen>
        <Grid columns={2} gap={16}>
          <Card>
            <CardHeader title="注册页加载" />
            <CardBody>
              <img src={imgRegisterPage} alt="注册页" style={{ width: '100%', borderRadius: 8 }} />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="注册成功 302 回跳(携带 code/state)" />
            <CardBody>
              <img src={imgRegisterRedirect} alt="注册回跳" style={{ width: '100%', borderRadius: 8 }} />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="登录页" />
            <CardBody>
              <img src={imgLoginPage} alt="登录页" style={{ width: '100%', borderRadius: 8 }} />
            </CardBody>
          </Card>
          <Card>
            <CardHeader title="错误密码拦截(不泄露 code)" />
            <CardBody>
              <img src={imgLoginError} alt="登录错误提示" style={{ width: '100%', borderRadius: 8 }} />
            </CardBody>
          </Card>
        </Grid>
      </CollapsibleSection>

      <Divider />

      <H2>最终结论</H2>
      <Stack gap={8}>
        <Text>
          <Tag tone="success">目标达成</Tag> 两项需求全部落地并经端到端验证:原纯 UI 占位的两处退出入口已真实生效,
          网页验证登录从零建成(本地 Auth 服务 + Loopback 回调闭环)。
        </Text>
        <Text tone="secondary" size="small">
          退出清理边界:keyring token + SQLite 账号 profile + 前端 store,本地任务/会话数据按 local-first 保留。
          开发联调切换:$env:TT_AUTH_MOCK='false' 后运行 init.ps1 即走真实网页登录。
        </Text>
      </Stack>
    </Stack>
  );
}
