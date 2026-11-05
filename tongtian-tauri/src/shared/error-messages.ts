// src/shared/error-messages.ts — 错误码文案映射表（流 G4）
//
// 三层架构演进 P0：9 个云端必需错误码 + 既有 Bridge 错误码的 UI 文案。
// 未知错误码走默认分支（防白屏，中性文案不承诺时间）。

import { ErrorCode } from './contracts';

const ERROR_MESSAGES: Record<string, string> = {
  // ── 三层架构演进云端必需码 ──
  [ErrorCode.BUDGET_EXHAUSTED]: '预算已用满，任务已暂停：调高预算后在任务上点「继续」才会恢复，不会自动接着跑',
  [ErrorCode.BUDGET_TASK_EXCEEDED]:
    '本任务的单任务预算已用满，已停在下一轮模型调用之前：在设置 →「模型与用量」调高单任务预算（填 0 为不限）后点「继续」唤回，也可以直接取消本任务',
  [ErrorCode.QUEUE_TIMEOUT]: '排队超时，任务已失败，可稍后重试',
  [ErrorCode.LEASE_LOST]: '执行环境已重置，任务将重新排队',
  [ErrorCode.FILE_REJECTED]: '文件被拒绝（格式不支持或超出大小限制）',
  [ErrorCode.SYNC_CONFLICT]: '数据同步冲突，请在冲突列表中裁决',
  [ErrorCode.PAYMENT_GRACE]: '订阅处于支付宽限期，新任务暂不可用',
  [ErrorCode.DATA_TIER_BLOCKED]: '数据分级限制：敏感数据禁止外发，请使用本地模型',
  [ErrorCode.INJECTION_BLOCKED]: '检测到异常内容注入，已拦截该请求',
  [ErrorCode.QUOTA_EXCEEDED]: '配额已用尽，新任务被阻断',

  // ── 既有 Bridge/任务错误码 ──
  [ErrorCode.TASK_NOT_FOUND]: '任务不存在或已结束',
  [ErrorCode.TASK_ALREADY_RUNNING]: '该任务正在其他端执行，当前仅提供查看',
  [ErrorCode.MODEL_RATE_LIMITED]: '模型繁忙，正在重试',
  [ErrorCode.MODEL_CONNECTION_FAILED]: '模型连接失败，请检查配置',
  [ErrorCode.LOCAL_MODEL_UNSUPPORTED]:
    '当前运行通道无法探测本机 Ollama，请在桌面版使用，或手动填写接口地址与模型名',
  [ErrorCode.LOCAL_MODEL_NOT_RUNNING]:
    '未检测到本机 Ollama 服务，请确认已安装并启动 Ollama 后重试',
  [ErrorCode.LOCAL_MODEL_INVALID]:
    '本地模型检测地址不合法：只支持本机回环地址，且路径须为根或 /v1',
  [ErrorCode.BRIDGE_ERROR]: '与 Agent 引擎通信失败',
  [ErrorCode.BRIDGE_PYTHON_NOT_FOUND]: '未检测到 Python 环境，请先安装 Python 3.10+',
  [ErrorCode.BRIDGE_START_TIMEOUT]: 'Agent 引擎启动超时',
  [ErrorCode.BRIDGE_ENGINE_EXITED]: 'Agent 引擎启动后立即退出，请查看引擎日志或重新安装应用',
  [ErrorCode.BRIDGE_SCRIPT_NOT_FOUND]: 'Agent 引擎脚本缺失，安装包资源不完整',
  [ErrorCode.BRIDGE_NO_FREE_PORT]: '本地端口 18000-19000 已占满，请重启应用后重试',
  [ErrorCode.BRIDGE_WORKDIR_FAILED]: '任务工作目录创建失败，请检查工作目录权限',
  [ErrorCode.BRIDGE_AGENT_NOT_FOUND]: '未安装 qwenpaw 引擎，请在设置页改用默认引擎或安装该引擎',
  [ErrorCode.BRIDGE_EXE_PATH]: '无法定位应用运行目录，请重新安装应用',
  [ErrorCode.BRIDGE_SPAWN_FAILED]: 'Agent 引擎进程启动失败，请检查 Python 环境后重试',
  [ErrorCode.BRIDGE_COMM_FAILED]: '与本地 Agent 引擎通信失败，引擎可能已退出，请重试任务',
  [ErrorCode.BRIDGE_CONTROL_FAILED]: '引擎拒绝了该控制指令，请稍后重试',
  [ErrorCode.BRIDGE_CONTROL_PARSE]: '引擎返回了无法解析的控制响应',
  [ErrorCode.BRIDGE_CANCEL_PARSE]: '引擎返回了无法解析的取消响应',
  [ErrorCode.BRIDGE_CHAT_PARSE]: '引擎返回了无法解析的对话响应，请重试任务',
  [ErrorCode.BRIDGE_STARTUP_FAILED]: 'Agent 引擎启动失败',
  [ErrorCode.BRIDGE_COMMUNICATION_FAILED]: '与 Agent 引擎通信失败',
  [ErrorCode.ENGINE_NOT_RUNNING]:
    '该工作空间的引擎尚未运行，请先在该工作空间发起一次任务或启动引擎后重试',
  [ErrorCode.IPC_CHANNEL_UNHANDLED]:
    '当前运行通道没有实现该功能，操作未生效；请升级桌面版或切换到支持的通道',
  EMPTY_GOAL: '任务描述为空',
  TASK_CANCELLED: '任务已取消',

  // ── 工作空间（workspace_list / workspace_create 抛出的码不得裸奔） ──
  [ErrorCode.WORKSPACE_INVALID_ID]:
    '工作空间标识非法：仅支持小写字母、数字、下划线与连字符，且不能包含路径分隔符',
  [ErrorCode.WORKSPACE_NOT_FOUND]: '工作空间不存在，请先创建或切换到其他工作空间',
  [ErrorCode.WORKSPACE_CREATION_FAILED]: '工作空间目录创建失败，请检查数据目录写入权限',
  [ErrorCode.WORKSPACE_SUSPENDED]: '该工作空间已挂起，恢复后才能执行任务',
  [ErrorCode.WORKSPACE_DELEGATE_FAILED]: '任务委派到其他工作空间失败，请稍后重试',
  [ErrorCode.WORK_DIR_INVALID]: '所选工作目录不存在或不是目录，任务未提交，请重新选择工作目录',

  // ── 系统集成（开机自启动） ──
  [ErrorCode.AUTOSTART_FAILED]: '开机自启动设置未能写入系统启动项，请检查权限后重试',
  [ErrorCode.AUTOSTART_UNSUPPORTED]: '当前系统不支持在应用内设置开机自启动，请在系统设置中手动配置',
  [ErrorCode.SETTING_KEY_RESERVED]: '该设置项由系统专用命令维护，不能通过设置页直接修改',

  // ── 技能（Skills 目录事实来源） ──
  [ErrorCode.SKILL_NOT_FOUND]: '未找到该技能文件，请确认路径后重试',
  [ErrorCode.SKILL_INSTALL_FAILED]: '技能安装失败，请检查运行目录写入权限',
  [ErrorCode.SKILL_EXECUTION_FAILED]: '技能执行失败，请查看执行过程明细后重试',
  [ErrorCode.SKILL_INCOMPATIBLE]: '该技能包不符合安装要求（仅支持含 SKILL.md 的技能目录）',
  [ErrorCode.SKILL_UNSUPPORTED]:
    '当前运行通道不支持技能增删，请在桌面版任我行中管理技能',
  [ErrorCode.SKILL_SCAN_BLOCKED]:
    '技能被安全扫描拦截：SKILL.md 包含高风险指令，未安装或启用',

  // ── 专家套件（套件目录 manifest.json 事实来源） ──
  [ErrorCode.SUITE_NOT_FOUND]: '未找到该已安装套件，请刷新列表后重试',
  [ErrorCode.SUITE_INCOMPATIBLE]:
    '套件包不符合安装要求：ZIP 需在根目录或一级目录下含 plugin.json，且其中必须有 name 字段',
  [ErrorCode.SUITE_INSTALL_FAILED]: '套件安装失败，请检查运行目录写入权限后重试',
  [ErrorCode.SUITE_ROLLBACK_FAILED]:
    '套件升级失败，且旧版本没能自动放回原位：请按明细里的两个路径手工恢复（把回收目录里的旧套件移回套件目录）',

  // ── 插件 SDK（安装前预检） ──
  [ErrorCode.PLUGIN_INVALID_PACKAGE]:
    '插件包无法预检：请确认上传的是含 plugin.json 的 .zip，且未超过大小上限',
  [ErrorCode.PLUGIN_UNSUPPORTED]:
    '当前运行通道无法预检插件包，请在桌面版任我行中使用',

  // ── 本地知识库（RAG） ──
  [ErrorCode.KNOWLEDGE_INVALID_NAME]: '知识库名称不合法：不能为空，最多 60 字，且不能包含路径分隔符',
  [ErrorCode.KNOWLEDGE_INVALID_SCOPE]:
    '知识库归属不合法：只能选择“所有项目共享”，或指定一个合法的工作区',
  [ErrorCode.KNOWLEDGE_INVALID_DESC]: '知识库描述过长（最多 500 字）',
  [ErrorCode.KNOWLEDGE_INVALID_ID]: '知识库标识不合法',
  [ErrorCode.KNOWLEDGE_INVALID_DOC]: '文档文件名不合法（文件名过长或包含非法字符）',
  [ErrorCode.KNOWLEDGE_EXISTS]: '同名知识库已存在，请换一个名称',
  [ErrorCode.KNOWLEDGE_NOT_FOUND]: '知识库不存在或已删除，请刷新列表',
  [ErrorCode.KNOWLEDGE_SAVE_FAILED]: '知识库保存失败，请检查数据目录写入权限',
  [ErrorCode.KNOWLEDGE_LIST_FAILED]: '知识库列表读取失败，请检查数据目录权限',
  [ErrorCode.KNOWLEDGE_META_UNREADABLE]: '知识库元信息读不到，目录可能已损坏',
  [ErrorCode.KNOWLEDGE_META_INVALID]: '知识库元信息损坏（meta.json 不是合法 JSON）',
  [ErrorCode.KNOWLEDGE_NO_DOCS]: '未选择任何要添加的文档',
  [ErrorCode.KNOWLEDGE_DOC_NOT_FOUND]: '文档不存在，可能已被移除',
  [ErrorCode.KNOWLEDGE_OPEN_FAILED]: '打开知识库目录失败',
  [ErrorCode.KNOWLEDGE_DIR_INVALID]: '知识库目录不在本机知识库根目录下，已拒绝检索',
  [ErrorCode.KNOWLEDGE_QUERY_EMPTY]: '检索词为空',
  [ErrorCode.KNOWLEDGE_UNSUPPORTED]: '当前运行通道没有知识库落盘能力，请在桌面版任我行中使用',

  // ── 在线技能 / 套件市场 ──
  [ErrorCode.MARKET_UNSUPPORTED]:
    '当前运行通道没有市场下载与落盘能力，请在桌面版任我行中使用在线市场',
  [ErrorCode.MARKET_INVALID_URL]:
    '市场索引地址不合法：远程地址必须使用 HTTPS；本机调试可使用 http://127.0.0.1 或 http://localhost',
  [ErrorCode.MARKET_NOT_CONFIGURED]: '尚未配置在线市场索引地址，请先在市场窗口中填写',
  [ErrorCode.MARKET_FETCH_FAILED]: '无法连接市场索引，请检查地址、网络或代理设置后重试',
  [ErrorCode.MARKET_HTTP_ERROR]: '市场服务器返回了异常状态，请检查索引地址是否有效',
  [ErrorCode.MARKET_INVALID_INDEX]:
    '市场索引格式不合法，请检查 skills / suites 条目是否包含 id、下载地址与 SHA-256',
  [ErrorCode.MARKET_ITEM_NOT_FOUND]: '市场索引里找不到该条目，可能已下架；请刷新后重试',
  [ErrorCode.MARKET_HASH_MISMATCH]:
    '下载内容与索引声明的 SHA-256 不一致，已拒绝安装；请刷新索引或联系市场维护者',
  [ErrorCode.MARKET_TOO_LARGE]: '市场返回内容超过大小上限，已拒绝下载',
  [ErrorCode.MARKET_INVALID_PUBLISHER]:
    '发布者公钥不合法：需要 32 字节 Ed25519 公钥的 base64 表示，标识只能包含字母、数字、点、下划线与连字符',
  [ErrorCode.MARKET_UNTRUSTED_PUBLISHER]:
    '该发布者尚未被本机信任，已拒绝安装；请先在市场窗口的「发布者信任」中核对公钥指纹并信任',
  [ErrorCode.MARKET_PUBLISHER_KEY_CHANGED]:
    '发布者公钥与已信任的不一致，已拒绝安装；如确认是发布者轮换，请先移除旧信任再添加新公钥',
  [ErrorCode.MARKET_SIGNATURE_REQUIRED]:
    '该市场要求发布者签名，但条目没有签名，已拒绝安装',
  [ErrorCode.MARKET_SIGNATURE_INVALID]:
    '发布者签名与条目内容不匹配，已拒绝安装；文件可能被篡改或签名已过期',
  [ErrorCode.MARKET_TRUST_SAVE_FAILED]: '发布者信任保存失败，请重试',
  [ErrorCode.MARKET_ITEM_REVOKED]:
    '该条目已被在线市场撤销，本机已禁止它进入执行面；请在市场里检查更新，安装未撤销的版本或卸载',

  // ── 安全与治理（PRD §4.7） ──
  [ErrorCode.POLICY_INVALID_MODE]: '出域控制模式非法，仅支持白名单 / 黑名单',
  [ErrorCode.POLICY_INVALID_LEVEL]: '沙箱执行级别非法，仅支持 L0 / L1 / L2',
  [ErrorCode.POLICY_INVALID_RANGE]: '沙箱资源上限越界，请检查 CPU / 内存 / 写盘数值',
  [ErrorCode.POLICY_INVALID_WORKSPACE]: '工作区标识非法，无法保存沙箱策略',
  [ErrorCode.POLICY_INVALID_COMMAND]:
    '命令白名单里有非法项：只能填裸命令名（如 python、git），不能带路径、空格或 shell 元字符',
  [ErrorCode.POLICY_INVALID_SHELL]: '通用 shell 依赖命令执行授权，请先开启命令执行再保存',
  [ErrorCode.POLICY_INVALID_HOST]: '联网白名单里有非法主机名，请检查后重试',
  [ErrorCode.POLICY_CORRUPT]: '策略数据已损坏，请重新保存后再试',
  [ErrorCode.POLICY_READ_FAILED]: '安全策略读取失败',
  [ErrorCode.POLICY_SAVE_FAILED]: '安全策略保存失败',
  [ErrorCode.POLICY_EXTERNAL_API_DISABLED]: '外部 API 已被硬开关禁用，仅允许本地模型处理',
  [ErrorCode.POLICY_DOMAIN_NOT_ALLOWED]: '该域名不在出域白名单内，请求已被拦截',
  [ErrorCode.POLICY_DOMAIN_BLOCKED]: '该域名命中出域黑名单，请求已被拦截',
  [ErrorCode.POLICY_INVALID_URL]: '无法解析目标地址，请检查 URL 配置',
  [ErrorCode.POLICY_UNSUPPORTED]: '当前系统没有可用的系统级沙箱，无法启用该执行级别',
  [ErrorCode.COMPLIANCE_DATA_LEAK]:
    '内容含机密或个人信息，按合规策略已阻止发往外部模型；请改用本地模型，或在安全合规页调整机密本地化设置',
  [ErrorCode.AUDIT_QUERY_FAILED]: '审计日志查询失败',
  [ErrorCode.AUDIT_WRITE_FAILED]: '审计日志写入失败',
  [ErrorCode.AUDIT_EXPORT_FAILED]: '审计日志导出失败',
  [ErrorCode.AUDIT_EXPORT_FORMAT_UNSUPPORTED]: '审计导出格式不支持，仅支持 CSV / JSON',

  // ── 安全工作环境（工作环境文件用量与清理） ──
  [ErrorCode.SANDBOX_ENV_USAGE_FAILED]: '工作环境用量统计失败，可稍后重试',
  [ErrorCode.SANDBOX_ENV_CLEAR_FAILED]: '工作环境文件清理失败，文件可能正被占用，请关闭相关任务后重试',
  [ErrorCode.SANDBOX_ENV_BASE_MISSING]: '数据目录无法访问，请检查工作目录权限后重试',
  [ErrorCode.SANDBOX_ENV_PATH_INVALID]: '工作环境路径无法解析，请重启应用后重试',
  [ErrorCode.SANDBOX_ENV_PATH_OUT_OF_SCOPE]: '清理路径超出工作环境目录范围，已被安全策略拦截',
  [ErrorCode.SANDBOX_ENV_CLEAR_SKIPPED_SYMLINK]: '该条目是符号链接，为避免误删外部数据已跳过',
  [ErrorCode.SANDBOX_ENV_UNSUPPORTED]: '当前环境无法访问工作环境目录，请在桌面版中统计与清理',

  // ── 模型用量统计 ──
  [ErrorCode.USAGE_INVALID_PERIOD]: '用量统计周期非法，仅支持 今日 / 本月 / 全部',
  [ErrorCode.USAGE_RECORD_FAILED]: '用量台账写入失败（已跳过，不影响任务执行）',
  [ErrorCode.USAGE_QUERY_FAILED]: '模型用量统计查询失败',
  [ErrorCode.USAGE_STATS_FAILED]: '用量总览生成失败，可先看下方各模型明细',
  [ErrorCode.USAGE_REPORT_FAILED]: '用量报表生成失败（按日趋势或明细读取受阻），可稍后重试',
  [ErrorCode.USAGE_UPLOAD_FAILED]: '用量上报失败，可稍后在用量页重试',
  [ErrorCode.USAGE_UPLOAD_LOGIN_REQUIRED]: '请先登录账号，再把本地用量上报到合并视图',
  [ErrorCode.USAGE_UPLOAD_AUTH_EXPIRED]: '登录已过期，请重新登录后再上报用量',

  // ── 定时任务 ──
  [ErrorCode.SCHEDULE_NOT_FOUND]: '定时任务不存在或已被删除',
  [ErrorCode.SCHEDULE_CRON_INVALID]: '定时规则表达式非法，请检查 cron 配置',
  [ErrorCode.SCHEDULE_EXECUTION_FAILED]: '定时任务执行失败，请查看执行记录了解详情',
  [ErrorCode.SCHEDULE_INVALID_FREQ]:
    '定时频率非法，支持 每小时 / 每天 / 每周 / 每月 / 间隔（每 N 分钟或小时或天，1 分钟～一周）/ 一次性',
  [ErrorCode.SCHEDULE_INVALID_TIME]:
    '执行时刻或执行日期非法：时刻请用 24 小时制 HH:MM（00:00–23:59），一次性任务需选今后的日期与时刻',
  [ErrorCode.SCHEDULE_INVALID_WEEKDAY]: '每周任务需指定星期（1=周一 … 7=周日）',
  [ErrorCode.SCHEDULE_INVALID_NAME]: '任务名称不能为空',
  [ErrorCode.SCHEDULE_INVALID_ID]: '缺少定时任务标识，请从任务列表重新进入编辑',
  [ErrorCode.SCHEDULE_QUERY_FAILED]: '定时任务查询失败',
  [ErrorCode.SCHEDULE_SAVE_FAILED]: '定时任务保存失败',
  [ErrorCode.SCHEDULE_ALREADY_RUNNING]: '该定时任务正在执行中，请等待本次执行结束',
  [ErrorCode.SCHEDULE_KEEP_AWAKE_FAILED]: '保持系统唤醒未能生效（唤醒约束进程启动失败）',
  [ErrorCode.SCHEDULE_KEEP_AWAKE_UNSUPPORTED]: '当前平台暂不支持保持系统唤醒',
  [ErrorCode.SCHEDULE_PARSE_EMPTY]: '请先输入任务描述，例如「每天早上 9 点检查服务」',
  [ErrorCode.SCHEDULE_PARSE_FAILED]: '无法理解这段描述，请换个说法或手动填写频率与时刻',
  [ErrorCode.SCHEDULE_PARSE_NO_TIME]:
    '没有识别到执行时刻，请补充具体时间，例如「早上 9 点」；一次性任务可写「明天上午 9 点」或「3 月 8 日晚上 8 点」',
  [ErrorCode.SCHEDULE_PARSE_TIME]: '识别到的时间不合法，请使用 00:00–23:59 之间的时刻',
  [ErrorCode.SCHEDULE_UNSUPPORTED_FREQ]:
    '暂不支持这种间隔说法：「每 N 分钟 / 每 N 小时 / 每 N 天」可用（最长一周）；「每 N 周」请改用每周或每天',
  [ErrorCode.SCHEDULE_UNSUPPORTED_DAY]:
    '每月只支持 1 号；其他日期请改用每周 / 每天，或建一次性任务并写明具体日期',
  [ErrorCode.SCHEDULE_INVALID_NOTIFY]: '执行结果推送配置不完整：频道与目标需成对填写',

  // ── 网络代理（Rust proxy.rs，保存前整组校验） ──
  [ErrorCode.PROXY_URL_REQUIRED]: '选择「自定义」代理时需要填写代理地址，例如 http://127.0.0.1:7890',
  [ErrorCode.PROXY_INVALID_URL]: '代理地址不合法，需带协议与主机端口，例如 http://127.0.0.1:7890',
  [ErrorCode.PROXY_CLIENT_BUILD_FAILED]: '按当前代理配置创建网络连接失败，请检查代理地址后重试',

  // ── 系统权限（Rust security/permissions.rs，去授权打开系统隐私设置） ──
  [ErrorCode.PERMISSION_KEY_UNKNOWN]: '未知的系统权限项，请刷新列表后重试',
  [ErrorCode.PERMISSION_PANE_UNAVAILABLE]:
    '当前系统没有这项权限的开关入口，请在系统设置的隐私页面中管理',
  [ErrorCode.PERMISSION_OPEN_FAILED]: '无法打开系统设置面板，请手动进入系统设置的隐私页面',
  [ErrorCode.PERMISSION_UNSUPPORTED]:
    '当前运行通道无法打开系统隐私设置，请在桌面版任我行中管理权限',

  // ── 对齐整改通道（Hooks / 连接器 / 归档 / 电脑操控） ──
  [ErrorCode.HOOK_INVALID_EVENT]: 'Hook 事件名不在支持清单内，请从下拉列表选择',
  [ErrorCode.HOOK_INVALID_COMMAND]: 'Hook 命令不能为空',
  [ErrorCode.HOOK_INVALID_TIMEOUT]: 'Hook 超时需在 1~600 秒之间',
  [ErrorCode.HOOK_INVALID_GROUP]: 'Hook 事件需配置为分组建制数组',
  [ErrorCode.HOOK_INVALID_MATCHER]: 'Hook matcher 需为字符串（留空或 * 表示匹配全部）',
  [ErrorCode.HOOK_CONFIG_INVALID]: 'Hook 配置结构非法，请重新填写',
  [ErrorCode.HOOK_SAVE_FAILED]: 'Hook 配置保存失败',
  [ErrorCode.HOOK_QUERY_FAILED]: 'Hook 配置读取失败',
  [ErrorCode.HOOK_TEST_FAILED]: 'Hook 试运行异常终止，请查看命令输出',
  [ErrorCode.HOOK_TEST_UNSUPPORTED]: '当前通道无法执行系统命令，请在桌面应用内试运行',
  [ErrorCode.HOOK_GRANT_FAILED]: 'Hook 放行保存失败，请重试',
  [ErrorCode.HOOK_REVOKE_FAILED]: 'Hook 撤销放行失败，请重试',
  [ErrorCode.HOOK_GRANT_QUERY_FAILED]: 'Hook 放行台账读取失败',

  [ErrorCode.ARCHIVE_INVALID_TASK_ID]: '任务标识为空，无法操作归档',
  [ErrorCode.ARCHIVE_TASK_NOT_FOUND]: '任务不存在或已结束，无法归档',
  [ErrorCode.ARCHIVE_CONFIRM_REQUIRED]: '清空归档清单需输入确认词 DELETE',
  [ErrorCode.ARCHIVE_SAVE_FAILED]: '归档标记写入失败',
  [ErrorCode.ARCHIVE_DELETE_FAILED]: '归档任务删除失败',
  [ErrorCode.ARCHIVE_QUERY_FAILED]: '归档清单读取失败',

  [ErrorCode.COMPUTER_USE_INVALID_POLICY]: '电脑操控档位非法，仅支持 每次询问 / 自动允许 / 禁用',
  [ErrorCode.COMPUTER_USE_UNSUPPORTED]:
    '当前平台或运行通道没有电脑操控执行器，截屏与键鼠注入仅在 Windows 桌面版可用',
  [ErrorCode.COMPUTER_USE_DISABLED]: '电脑操控已禁用，本次截屏或操作未执行',
  [ErrorCode.COMPUTER_USE_DENIED_BY_USER]: '你取消了本次电脑操控确认，操作未执行',
  [ErrorCode.COMPUTER_USE_CAPTURE_FAILED]:
    '屏幕截取失败，锁屏或 UAC 安全桌面下无法截屏；请解锁后重试',
  [ErrorCode.COMPUTER_USE_ACTION_FAILED]:
    '键鼠操作未执行，目标窗口可能以管理员权限运行，或当前处于安全桌面',
  [ErrorCode.COMPUTER_USE_INVALID_ACTION]: '电脑操控动作参数不合法，本次操作未执行',
  [ErrorCode.COMPUTER_USE_GATE_UNAUTHORIZED]:
    '本机电脑操控网关拒绝了请求，请重启应用后重试',
  [ErrorCode.COMPUTER_USE_OUT_OF_BOUNDS]: '目标坐标不在屏幕范围内，本次操作未执行',
  [ErrorCode.COMPUTER_USE_BLOCKED_COMBO]: '出于安全考虑，Win 键组合与任务管理器快捷键不允许发送',
  [ErrorCode.COMPUTER_USE_BAD_JSON]: '电脑操控请求格式非法，本次操作未执行',

  // ── 本机画面（本机画面；只看不控） ──
  [ErrorCode.REMOTE_UNSUPPORTED]:
    '当前平台或运行通道没有桌面截屏执行器，本机画面仅在 Windows 桌面版可用',
  [ErrorCode.REMOTE_DISABLED]:
    '电脑操控档位为禁用，本机画面无法开启；请先在「电脑操控」页调整档位',
  [ErrorCode.REMOTE_NO_SESSION]: '本机画面会话未开启或已结束，本次未截取画面',
  [ErrorCode.REMOTE_SESSION_EXPIRED]:
    '本机画面会话已到期（单次最长 10 分钟），画面已停止；请重新开启会话',

  [ErrorCode.BROWSER_USE_INVALID_POLICY]: '浏览器档位非法，仅支持 每次询问 / 自动允许 / 禁用',
  [ErrorCode.BROWSER_USE_NOT_FOUND]:
    '本机没有找到 Microsoft Edge 或 Google Chrome，安装浏览器后才能使用浏览器 AI+',
  [ErrorCode.BROWSER_USE_DISABLED]: '浏览器 AI+ 已禁用，本次会话或操作未执行',
  [ErrorCode.BROWSER_USE_BLOCKED_SCHEME]: '受控浏览器只允许打开 http/https 页面',
  [ErrorCode.BROWSER_USE_INVALID_ACTION]: '浏览器动作参数不合法，本次操作未执行',
  [ErrorCode.BROWSER_USE_DENIED_BY_USER]: '你取消了本次浏览器操作确认，操作未执行',
  [ErrorCode.BROWSER_USE_CONFIRM_UNAVAILABLE]:
    '当前系统没有可用的确认弹窗，无法在「每次询问」档位下执行；请切换档位后重试',
  [ErrorCode.BROWSER_USE_LAUNCH_FAILED]: '受控浏览器启动失败，请检查浏览器安装与权限后重试',
  [ErrorCode.BROWSER_USE_LAUNCH_TIMEOUT]: '受控浏览器启动超时，请关闭残留窗口后重试',
  [ErrorCode.BROWSER_USE_SESSION_FAILED]: '受控浏览器会话读写失败，请重启应用后重试',
  [ErrorCode.BROWSER_USE_STOP_FAILED]: '结束受控浏览器失败，可在任务管理器中确认进程状态',
  [ErrorCode.BROWSER_USE_ACTION_FAILED]: '网页操作未执行，受控浏览器可能已关闭或页面连接中断',
  [ErrorCode.BROWSER_USE_GATE_UNAUTHORIZED]:
    '本机浏览器网关拒绝了请求，请重启应用后重试',
  [ErrorCode.BROWSER_USE_BAD_JSON]: '浏览器请求格式非法，本次操作未执行',

  // ── 终端 AI+ 与 ACP 委派 ──
  [ErrorCode.TERMINAL_POLICY_INVALID]: '终端开关取值非法，仅支持开启或关闭',
  [ErrorCode.TERMINAL_DISABLED]:
    '终端 AI+ 未启用，请先在「设置 → ACP 与终端」中显式开启',
  [ErrorCode.TERMINAL_UNSUPPORTED]:
    '当前运行通道没有本机 shell 进程，终端 AI+ 仅在桌面版任我行中可用',
  [ErrorCode.TERMINAL_CWD_INVALID]: '工作目录不存在或不是目录，请重新选择',
  [ErrorCode.TERMINAL_SESSION_UNKNOWN]:
    '本应用没有这个终端会话，可能已关闭或界面已刷新；请重新打开终端',
  [ErrorCode.TERMINAL_NOT_FOUND]: '终端会话不存在或已关闭，请重新打开',
  [ErrorCode.TERMINAL_LIMIT]: '终端会话已达上限（5 个），请先关闭不用的会话',
  [ErrorCode.TERMINAL_EXITED]: 'shell 已退出，请重新打开终端',
  [ErrorCode.TERMINAL_INPUT_TOO_LARGE]: '单次输入不能超过 64KB',
  [ErrorCode.TERMINAL_SOURCE_INVALID]: '终端输入来源非法，仅支持手动输入或 AI 建议',
  [ErrorCode.TERMINAL_QUESTION_REQUIRED]: '请先描述要解决的问题，再让 AI 给出命令建议',
  [ErrorCode.TERMINAL_SPAWN_FAILED]: '无法启动本机 shell，请检查系统环境后重试',
  [ErrorCode.TERMINAL_WRITE_FAILED]: '写入 shell 失败，会话可能已退出',
  [ErrorCode.TERMINAL_AI_FAILED]:
    '命令建议生成失败，请检查模型配置或稍后重试（命令未执行）',
  [ErrorCode.TERMINAL_STATE_FAILED]: '终端会话表不可用，请重启应用后重试',
  [ErrorCode.TERMINAL_FAILED]: '终端请求执行失败，请稍后重试；若持续失败请重启应用',
  [ErrorCode.ACP_PERMISSION_INVALID]: 'ACP 审批策略非法，仅支持 拒绝 / 放行',
  [ErrorCode.ACP_TIMEOUT_INVALID]: 'ACP 超时需在 60–1800 秒之间',
  [ErrorCode.ACP_COMMAND_REQUIRED]: '启用 ACP 委派前必须填写外部 Agent 启动命令',
  [ErrorCode.ACP_COMMAND_MISSING]:
    'ACP 委派已启用但没有启动命令，已按未启用处理；请到「设置 → ACP 与终端」补全',
  [ErrorCode.ACP_COMMAND_NOT_FOUND]: '找不到外部 Agent 的启动命令，请检查命令与 PATH',
  [ErrorCode.ACP_SPAWN_FAILED]: '外部编码 Agent 启动失败，请检查命令参数',
  [ErrorCode.ACP_PROCESS_EXITED]:
    '外部编码 Agent 在握手前退出，请检查命令是否为 ACP 协议服务',
  [ErrorCode.ACP_TIMEOUT]: '外部编码 Agent 执行超时，进程已结束',
  [ErrorCode.ACP_RPC_ERROR]: '外部编码 Agent 返回协议错误，本次委派未完成',
  [ErrorCode.ACP_AUTH_REQUIRED]:
    '外部编码 Agent 要求交互式认证，当前未实现；请先在它的终端里完成登录',
  [ErrorCode.ACP_TERMINAL_UNSUPPORTED]:
    '外部编码 Agent 请求终端能力，当前未开放；请让它改用文件读写或直接作答',
  [ErrorCode.ACP_PATH_DENIED]:
    '外部编码 Agent 试图读写工作目录之外的文件，已拒绝',

  // ── PII 出站脱敏与逐工具审批 ──
  [ErrorCode.PII_MODE_INVALID]: '出站脱敏档位非法，仅支持 关闭 / 脱敏后发送 / 命中即中止',
  [ErrorCode.PRIVACY_CONFIG_UNAVAILABLE]:
    '出站脱敏档位读取失败，任务已中止（避免在未知档位下把内容发给模型）；请到系统设置重新选择档位',
  [ErrorCode.PRIVACY_OUTBOUND_BLOCKED]:
    '发给模型的文本命中身份证号、手机号、银行卡号、邮箱或明文密钥，按「命中即中止」已停止任务；可在系统设置的数据共享里改为脱敏后发送',
  [ErrorCode.TOOL_APPROVAL_INVALID_POLICY]: '工具审批档位非法，仅支持 关闭 / 敏感工具 / 所有工具',
  [ErrorCode.TOOL_APPROVAL_UNAVAILABLE]:
    '工具审批已开启，但本机确认网关不可用，任务已中止（不会跳过审批执行）',
  [ErrorCode.TOOL_APPROVAL_GUARD_FAILED]:
    '工具审批门禁没有装上，任务已中止；请重启应用后重试',
  [ErrorCode.TOOL_APPROVAL_DENIED_BY_USER]: '你拒绝了本次工具调用，操作未执行',
  [ErrorCode.TOOL_APPROVAL_CONFIRM_UNAVAILABLE]:
    '当前系统无法拉起工具确认框，操作未执行；可在设置中关闭工具审批或安装 zenity/kdialog',
  [ErrorCode.FILE_PERMISSION_INVALID]:
    '文件访问档位非法，仅支持 每次询问 / 自动审批 / 完全访问，任务未提交',
  [ErrorCode.FILE_PERMISSION_DENIED_BY_USER]:
    '你拒绝了这次文件操作（档位「每次询问」），文件未被创建 / 更新 / 删除',
  [ErrorCode.FILE_PERMISSION_ASK_NOT_REQUIRED]:
    '当前文件访问档位不是「每次询问」，这次操作不需要确认；档位可在对话输入框里 + 号旁的盾牌处调整',
  [ErrorCode.FILE_PERMISSION_CONFIRM_UNAVAILABLE]:
    '当前系统拉不起文件操作确认框，这次写入或删除未执行；可在对话输入框里的盾牌处改用「自动审批」档',
  [ErrorCode.TRASH_DIR_UNAVAILABLE]:
    '删除前的备份目录写不进去，文件保持原样；请检查应用数据目录的写入权限',
  [ErrorCode.TRASH_NAME_EXHAUSTED]:
    '备份目录里同一秒的重名文件太多，这次删除已取消；可手动清理应用数据目录下的 file-trash 后重试',
  [ErrorCode.TRASH_NAME_UNAVAILABLE]:
    '备份时取不到文件名，文件保持原样；请确认该路径确实是一个可读的普通文件',
  [ErrorCode.TRASH_BACKUP_FAILED]:
    '没能把文件复制进备份目录，文件未被删除；请确认文件仍存在、未被其他程序独占',

  // ── 意识（awareness:* 通道，Rust awareness 模块抛出） ──
  [ErrorCode.AWARENESS_DIR_FAILED]: '意识目录无法访问，请检查存储位置权限后重试',
  [ErrorCode.AWARENESS_LOAD_FAILED]: '意识内容读取失败，可稍后重试或打开文件夹直接查看文件',
  [ErrorCode.AWARENESS_SAVE_FAILED]: '意识内容保存失败，磁盘写入受阻',
  [ErrorCode.AWARENESS_FILE_INVALID]: '意识文件不在允许编辑范围内，仅支持协作风格 / 工作手册 / 用户画像 / 长期记忆',
  [ErrorCode.AWARENESS_STYLE_INVALID]: '协作风格预设非法，仅支持 高效极简 / 深思熟虑 / 温和耐心 / 果断执行',
  [ErrorCode.AWARENESS_MEMORY_EMPTY]: '记忆条目内容不能为空',
  [ErrorCode.AWARENESS_MEMORY_NOT_FOUND]: '该记忆条目已不存在，可能已被删除',

  // ── 长期记忆库（Rust store::memories 抛出的码不得裸奔） ──
  [ErrorCode.MEMORY_INVALID_SCOPE]:
    '记忆归属非法：仅支持全局或当前项目，项目记忆需要合法的工作区标识',
  [ErrorCode.MEMORY_INVALID_KIND]: '记忆类型非法，仅支持 任务 / 事实 / 偏好 / 决策',
  [ErrorCode.MEMORY_CONTENT_EMPTY]: '记忆条目内容不能为空',
  [ErrorCode.MEMORY_CONTENT_TOO_LONG]: '记忆条目过长，请精简到 1000 字以内',
  [ErrorCode.MEMORY_NOT_FOUND]: '该记忆条目已不存在，可能已被删除',
  [ErrorCode.MEMORY_SAVE_FAILED]: '记忆写入失败，请稍后重试',
  [ErrorCode.MEMORY_LIST_FAILED]: '记忆列表读取失败，请稍后重试',
  [ErrorCode.MEMORY_LOAD_FAILED]: '记忆条目读取失败，请稍后重试',
  [ErrorCode.MEMORY_STATUS_INVALID]: '记忆复核动作不可用：当前状态不支持该操作，请刷新后重试',
  [ErrorCode.MEMORY_SUPERSEDED]: '这条记忆已被更新的同类事实取代，不能直接恢复；如需保留请新增一条并确认',
  [ErrorCode.MEMORY_INVALID_FACT]: '事实字段非法：主体/属性最多 80 字，取值最多 300 字',
  [ErrorCode.MEMORY_SECRET_BLOCKED]: '内容疑似包含凭据，已拒绝写入记忆库',
  [ErrorCode.MEMORY_IMPORT_INVALID]: '导入文件不是合法的记忆导出文件（格式或版本不符）',
  [ErrorCode.MEMORY_EXPORT_FAILED]: '记忆导出失败：无法写入导出目录，请检查运行目录权限或磁盘空间',
  [ErrorCode.MEMORY_DESKTOP_ONLY]: '复核、实体页与导入导出只在桌面版可用（浏览器直连没有桌面记忆库）',

  // ── MCP 连接器（Rust mcp 模块抛出的码不得裸奔） ──
  [ErrorCode.MCP_CONNECTION_FAILED]: '未能连上该 MCP 连接器，请检查启动命令或服务地址后重试',
  [ErrorCode.MCP_TOOL_NOT_FOUND]: '连接器未提供该工具，可能是工具清单已更新，请重新连接',
  [ErrorCode.MCP_TOOL_BLOCKED]: '该 MCP 工具已被安全策略拦截',
  [ErrorCode.MCP_AUTH_FAILED]: 'MCP 连接器鉴权失败，请检查环境变量或请求头配置',
  [ErrorCode.MCP_TIMEOUT]: 'MCP 连接器握手超时，请调大超时时间或确认服务已就绪',
  [ErrorCode.MCP_INVALID_ID]: '缺少 MCP 连接器标识，请从连接器列表重新进入',
  [ErrorCode.MCP_INVALID_NAME]: '连接器名称不能为空，且不超过 60 个字符',
  [ErrorCode.MCP_INVALID_TRANSPORT]: '传输方式非法，仅支持 STDIO / Streamable HTTP / SSE',
  [ErrorCode.MCP_INVALID_COMMAND]: '启动命令非法，需为非空单行命令',
  [ErrorCode.MCP_INVALID_URL]: '服务地址非法，请填写 http(s):// 开头的完整地址',
  [ErrorCode.MCP_INVALID_HEADER]: '环境变量或请求头配置非法，键名需是无空白的标识符',
  [ErrorCode.MCP_SERVER_EXISTS]: '已存在同名连接器，请更换名称或先删除原有配置',
  [ErrorCode.MCP_SERVER_NOT_FOUND]: '该 MCP 连接器不存在或已被删除，请刷新列表',
  [ErrorCode.MCP_SAVE_FAILED]: 'MCP 连接器配置保存失败，请检查数据目录写入权限',
  [ErrorCode.MCP_QUERY_FAILED]: 'MCP 连接器清单读取失败',
  [ErrorCode.MCP_PROTOCOL_ERROR]: 'MCP 连接器响应不符合协议，未能返回可解析的 JSON-RPC 消息',
  [ErrorCode.MCP_TRANSPORT_UNSUPPORTED]: '该传输方式需要常驻事件流，请改用 Streamable HTTP 端点',
  [ErrorCode.MCP_INVALID_CONFIG]: '连接器配置 JSON 无法解析，请检查格式后重新粘贴',
  [ErrorCode.MCP_SANDBOX_FAILED]: '在系统沙箱内启动连接器失败，请检查沙箱设置或临时关闭沙箱后重试',
  [ErrorCode.MCP_UNSUPPORTED]: '当前运行通道无法与 MCP 连接器握手，请在桌面版任我行中完成连接',
  [ErrorCode.MCP_OAUTH_INVALID_CONFIG]: 'OAuth 配置非法：授权地址与 token 地址需为 HTTPS，client_id 不能为空',
  [ErrorCode.MCP_OAUTH_REQUIRED]: '该连接器需要先完成 OAuth 授权才能连接',
  [ErrorCode.MCP_OAUTH_REAUTH_REQUIRED]: 'OAuth 授权已过期或被提供方拒绝，请重新授权',
  [ErrorCode.MCP_OAUTH_REJECTED]: '连接器拒绝了访问令牌（HTTP 401），已尝试续期仍失败，请重新授权',
  [ErrorCode.MCP_OAUTH_IN_PROGRESS]: '已有授权流程进行中，请先在浏览器完成或等待超时后再试',
  [ErrorCode.MCP_OAUTH_TIMEOUT]: '等待浏览器授权超时，请重试并确认已在浏览器完成登录与同意',
  [ErrorCode.MCP_OAUTH_DENIED]: '授权被拒绝，请确认已同意所需权限后重试',
  [ErrorCode.MCP_OAUTH_STATE_MISMATCH]: '授权回调校验失败（state 不匹配），已中止，请重新发起授权',
  [ErrorCode.MCP_OAUTH_CALLBACK_INVALID]: '授权回调缺少授权码，请重新发起授权',
  [ErrorCode.MCP_OAUTH_TOKEN_REJECTED]: '提供方拒绝了令牌请求，请检查授权配置与授权码后重试',
  [ErrorCode.MCP_OAUTH_INVALID_RESPONSE]: '提供方返回的令牌响应不符合 OAuth 规范，无法完成授权',
  [ErrorCode.MCP_OAUTH_NETWORK]: 'OAuth 授权网络请求失败，请检查网络或代理设置后重试',
  [ErrorCode.MCP_OAUTH_OPEN_FAILED]: '无法打开系统浏览器完成授权，请检查默认浏览器设置',
  [ErrorCode.MCP_OAUTH_UNSUPPORTED]: '当前运行通道无法发起 OAuth 授权，请在桌面版任我行中完成授权',
  [ErrorCode.MCP_HUB_NOT_STARTED]: '连接器的常驻会话通道尚未开启，本轮已改用临时连接，请重启应用后再试',
  [ErrorCode.MCP_HUB_UNKNOWN_OP]: '连接器的常驻会话通道收到无法识别的指令，已忽略该请求，请重启应用后再试',
  // 连接器目录与配置生成（引擎 connect 车道 `/api/mcp/*`，缺陷 D-39）：这十个码只在
  // 引擎 HTTP 层出现，Rust 不抛它们，所以没往 `ErrorCode` 里加成员——枚举在
  // `contracts.ts`（唯一写入者的注册面），加成员等于替桌面壳承诺一条它还没实现的分支。
  // 与 `SUPERAGENT_DESKTOP_ONLY`、`AGENT_TICKET_CLOSED` 同法：字符串字面量键，文案照旧齐全。
  MCP_CONNECTOR_UNKNOWN: '连接器目录里没有这一条，请先从目录里取一次再配置',
  MCP_CONNECTOR_ID_MISSING: '没带连接器标识，请先从连接器目录里选一条',
  MCP_CONNECTOR_MISSING_FIELD: '连接器还差必填项，请按表单里的中文提示补全',
  MCP_CONNECTOR_INVALID: '连接器配置不合法，请检查目录里的必填项与取值',
  MCP_CONNECTOR_SPEC_FAILED: '连接器配置生成失败，请检查填写内容后重试',
  MCP_CATALOG_EMPTY_CATEGORY: '这个分类下暂时没有连接器，请换一个分类看看',
  MCP_CATALOG_FAILED: '连接器目录读取失败，请重启应用后再试',
  MCP_RECONCILE_FAILED: '连接器与目录的对账读取失败，请稍后重试',
  MCP_RECONCILE_NO_ROWS: '还没有已配置的连接器，可先从连接器目录里添加一条',
  MCP_HEALTH_FAILED: '连接器健康状态读取失败，请稍后重试',
  MCP_DESKTOP_ONLY: '这一步要把凭据写进本机钥匙串，只在桌面版任我行中可用',

  // ── 项目协作（跨用户 / 跨机器；桌面端 Rust → 账号服务） ──
  [ErrorCode.COLLAB_UNSUPPORTED]:
    '当前运行通道无法访问协作服务，请在桌面版任我行中使用项目协作',
  [ErrorCode.COLLAB_LOGIN_REQUIRED]:
    '项目协作需要真实账号登录；当前为 Mock 登录，请关闭 TT_AUTH_MOCK 后重新登录',
  [ErrorCode.COLLAB_AUTH_EXPIRED]: '登录态已失效，请退出后重新登录再使用项目协作',
  [ErrorCode.COLLAB_AUTH_REFRESH_FAILED]: '刷新登录态失败，请退出后重新登录',
  [ErrorCode.COLLAB_AUTH_NETWORK]: '无法连接账号服务，请检查服务地址与网络后重试',
  [ErrorCode.COLLAB_SERVER_INVALID]:
    '协作服务地址不合法：远程地址必须使用 HTTPS；本机调试可使用 http://127.0.0.1 或 http://localhost',
  [ErrorCode.COLLAB_NETWORK]: '无法连接协作服务，请检查服务地址、网络或代理设置后重试',
  [ErrorCode.COLLAB_BAD_RESPONSE]: '协作服务返回了无法解析的响应，请检查服务端版本',
  [ErrorCode.COLLAB_INVALID_INPUT]: '协作请求参数不合法，请刷新后重试',
  // 工单收口后服务端拒绝一切写入（D-47：停止是唯一撤权口径），本地不改写它的码
  AGENT_TICKET_CLOSED: '这张工单已经收口，停止之后不再接受任何写入；如需继续请重新发起任务',
  // 其余 agent.* 码（D-47.5）：判定都在服务端，这里只把「为什么被拒、下一步做什么」说给人听
  AGENT_TICKET_INVALID_INPUT: '工单请求内容不合法，请按提示检查后重试',
  AGENT_TICKET_FORBIDDEN: '你没有这一步的权限：改范围与停止只属于工单创建者或项目管理员，报告与已读只属于领取这单的执行体',
  AGENT_TICKET_NOT_FOUND: '这张工单不存在或已被删除，请刷新后重试',
  AGENT_MANDATE_REVOKED: '这张工单当前没有生效授权，执行体不能开工；要开工请由人重新交代范围',
  AGENT_MANDATE_SUPERSEDED: '你依据的那份授权已被新版取代，请刷新后按当前生效的授权继续',
  AGENT_MANDATE_CHANGED: '授权正文已经改过（摘要对不上），续跑前必须按新正文重新校验',
  AGENT_REPORT_KIND_INVALID: '报告类型只能是 progress / waiting / ready / blocked / failed / dropped 六态之一',
  AGENT_HANDBACK_DONE: '这单已经交回过一次：现在等人改授权或收口，重复交回只会让人重复处理同一件事',
  AGENT_ATTENTION_QUESTION_REQUIRED: '「等你回答」必须写明要问的那句话，否则工单只能按受阻处理',
  AGENT_ATTENTION_NOT_OPEN: '这个问题已经 answered 或撤回，请刷新后看最新的待办',
  AGENT_ATTENTION_NOT_FOUND: '这条待人工问题不存在，请刷新后重试',
  AGENT_ASSIGNMENT_NOT_FOUND: '这次执行体绑定不存在，或现在没有在跑的执行体，请刷新工单详情',
  AGENT_ASSIGNMENT_STALE: '这单已由其它执行体接手，旧执行体的写入不再改变工单状态',
  AGENT_ASSIGNMENT_RUNNING: '这份授权已经在跑，不能再开一个执行体；要先结束现有绑定',
  PROJECT_NOT_FOUND: '项目不存在或你不是成员',
  PROJECT_FORBIDDEN: '当前项目角色没有该操作权限',
  PROJECT_CONFLICT: '任务已被其他人修改，请刷新后重试',
  PROJECT_TASK_NOT_FOUND: '任务不存在，可能已被删除，请刷新后重试',
  PROJECT_ALREADY_MEMBER: '该邮箱已是项目成员',
  PROJECT_INVITE_EMAIL_MISMATCH: '邀请码与当前登录邮箱不一致，无法接受该邀请',
  PROJECT_INVITE_INVALID: '邀请码无效、已使用或已过期，请让项目管理员重新邀请',
  PROJECT_OWNER_IMMUTABLE: '项目所有者不能退出项目，请先转让所有权',
  PROJECT_MEMBER_NOT_FOUND: '该成员不在项目中，请刷新后重试',
  PROJECT_EMPTY_PATCH: '没有需要修改的字段',
  PROJECT_INVALID_INPUT: '协作请求内容不合法，请检查后重试',
  PROJECT_ASSIGNEE_INVALID: '负责人必须是项目成员且不是只读成员',
  RATE_LIMITED: '操作过于频繁，请稍后再试',

  // ── 文件与文档预览（file_list / file_read / file_preview 抛出的码不得裸奔） ──
  [ErrorCode.FILE_NOT_FOUND]: '文件不存在或已被移动，请刷新目录后重试',
  [ErrorCode.OPEN_FAILED]: '无法用系统默认应用打开该文件，请检查系统文件关联设置后重试',
  [ErrorCode.OPEN_EXTERNAL_UNSUPPORTED]: '当前运行通道没有系统默认应用，请在桌面版任我行中打开',
  [ErrorCode.FILE_ACCESS_DENIED]: '该路径不在工作区文件白名单内，无法读写',
  [ErrorCode.FILE_WRITE_FAILED]: '文件写入失败，请检查目标目录写入权限',
  [ErrorCode.FILE_READ_FAILED]: '文件读取失败，可能是二进制文件或编码异常',
  [ErrorCode.FILE_CONVERSION_FAILED]: '文档正文提取失败，可尝试用本地应用打开后另存为 docx/xlsx/pptx/pdf',
  [ErrorCode.FILE_TOO_LARGE]: '文件超出大小上限，请拆分后重试',
  [ErrorCode.FILE_BACKUP_FAILED]: '附件暂存失败，请检查运行目录写入权限',
  [ErrorCode.FILE_STAGE_FAILED]: '附件暂存失败：文件没能复制到运行目录，请检查磁盘空间与目录权限后重试',
  [ErrorCode.FILE_UNSUPPORTED]: '当前运行通道无法读取本机文档正文，请在桌面版任我行中预览',

  // ── IM 频道（长连接只跑在引擎进程；引擎不在时如实告知，不显示假清单） ──
  [ErrorCode.IM_ENGINE_UNAVAILABLE]:
    'IM 频道的长连接由本地引擎进程承载；本地引擎未启动或启动失败，暂无法查看或绑定频道，请稍后重试',
  [ErrorCode.IM_ENGINE_UNREACHABLE]: '本地引擎无响应，IM 频道状态暂时问不到，请重启引擎后刷新',
  [ErrorCode.IM_ENGINE_ERROR]: 'IM 频道接口返回错误，请查看下方原因',
  [ErrorCode.IM_BAD_RESPONSE]: 'IM 频道接口响应无法解析，请重启引擎后重试',
  [ErrorCode.IM_CHANNEL_UNKNOWN]: '没有这个 IM 频道，请刷新频道清单',
  [ErrorCode.IM_CHANNEL_INVALID]: '频道标识非法，已拒绝该请求',
  [ErrorCode.IM_UNSUPPORTED]: '该 IM 的长连接还没接入引擎，暂不能绑定；已接入的频道见清单里的「可绑定」',
  [ErrorCode.IM_DEPENDENCY_MISSING]:
    '这台机器的引擎运行时缺该频道依赖的官方 SDK，通道不可用（清单里会显示「尚未接入」）；请重新安装任我行的随包运行时，或按下方原因处理',
  [ErrorCode.IM_NOT_CONFIGURED]: '还没有登记该频道的凭据，请先完成绑定',
  [ErrorCode.IM_CREDENTIAL_MISSING]: '必填凭据还没填全，缺哪一项已在字段上标出',
  [ErrorCode.IM_CREDENTIAL_INVALID]: '凭据格式不对（前缀或长度不符合该 IM 的要求），请回开发者后台复制完整值',
  [ErrorCode.IM_LOGIN_TICKET_MISSING]: '二维码票据丢了，请关闭后重新生成二维码再扫码',
  [ErrorCode.IM_AUTH_FAILED]: '凭据被 IM 服务端拒绝，请在后台确认应用未停用且权限齐全',
  [ErrorCode.IM_API_ERROR]: 'IM 服务端返回业务错误，请查看下方原因',
  [ErrorCode.IM_CONNECTION_CLOSED]: '长连接被 IM 服务端关闭，正在按退避重试',
  [ErrorCode.IM_CONNECTION_FAILED]: '长连接建立失败，请检查本机到 IM 服务端的网络',
  [ErrorCode.IM_CONNECTION_KICKED]:
    '这个机器人的长连接被另一条新连接顶掉了（企业微信同一个 BotID 只允许一条有效长连接）。引擎已停止自动重连，否则两边会互相踢；请确认只有一个任我行实例或脚本在用这个 BotID',
  [ErrorCode.IM_CHANNEL_ERROR]: 'IM 频道运行异常，请查看最近错误后重试',
  [ErrorCode.IM_REPLY_FAILED]: '任务已完成，但消息回贴失败，请检查频道凭据的发消息权限',
  [ErrorCode.IM_PREFERENCE_INVALID]: 'IM 频道偏好保存失败：开关值不合法，请重试',

  // ── D-41 补齐：IM 媒体 / 群策略 / 限流（引擎 im_*.py 与 connect 车道 /api/im/*）──
  // 与 D-39 的 connect 车道同法：字符串字面量键，不开 contracts.ts 的枚举；
  // 每条只写「怎么办」，引擎带来的中文详情由 errorTextOf 追加在后面。
  // 这一族不许降级成「发生错误（IM_POLICY_INVALID）」——设置页里读到降级就等于
  // 用户改完策略不知道有没有生效。

  // 群策略读写（引擎 im_policy.py / connect 车道 /api/im/policies）
  IM_POLICY_INVALID: '策略项没保存：取值不在可选项里或作用域标识不合法，请按提示改后再提交',
  IM_POLICY_UNREADABLE: '群策略表读不出来，本次显示的是出厂默认；请检查引擎数据目录后重试',
  IM_POLICY_WRITE_FAILED: '群策略写入失败，刚才的改动没有生效，请检查数据目录写入权限后重试',
  // 流量面（connect 车道 /api/im/traffic）
  IM_TRAFFIC_UNREADABLE: '限流与收发的读数取不到，频道本身仍在按原状态运行，请稍后刷新',
  // 附件承载（引擎 im_media.py）
  IM_MEDIA_BAD_URL: '这条消息的附件没有可用的下载地址，已按「未收到附件」处理，文字部分照常处理',
  IM_MEDIA_UNSUPPORTED: '这种类型的附件暂不接收，已在消息里以文字说明呈现',
  IM_MEDIA_PATH_ESCAPE: '附件的存放位置越出了附件根，已拒绝保存；这是异常标识，不代表附件丢失',
  // 发送闸门（引擎 im_ratelimit.py）
  IM_RATE_LIMIT_INVALID: '限流参数不合法，本条回复没有发出；请回到「IM 设置」把每分钟条数与突发数改回范围内',
  IM_RATE_LIMITED: 'IM 服务端判定发送过快，已按它给的秒数暂停发送，恢复时间与原因见频道状态',
  IM_SEND_FAILED: '回贴没有发出去，任务结果仍已记录在会话台账里，可在频道状态查看具体原因',
  // 框架层的登录形状（引擎 im_channels.py）
  IM_LOGIN_REQUIRED: '这个频道要先完成登录才能收发消息，请在频道卡片里扫码或授权',
  IM_LOGIN_UNSUPPORTED: '这个频道不走扫码登录，请直接填写凭据',
  IM_LOGIN_EXPIRED: '登录已过期，请重新扫码或授权后再收发',
  IM_LOGIN_FAILED: '登录没有被 IM 服务端接受，请检查下方原因后重试',

  // ── 品牌外观（system/branding.rs；名称与图标单次自定义） ──
  BRAND_NAME_EMPTY: '应用名称不能为空',
  BRAND_NAME_TOO_LONG: '应用名称最多 24 个字符',
  BRAND_NAME_INVALID: '应用名称不能包含换行或控制字符',
  BRAND_ALREADY_CUSTOMIZED: '品牌修改机会已使用，应用内不再提供第二次修改',
  BRAND_NO_CHANGE: '请至少修改名称或选择一张图标',
  BRAND_UNSUPPORTED: '当前通道没有桌面窗口与托盘，无法修改应用名称或图标',
  BRAND_ICON_NOT_PNG: '只支持 PNG 图片',
  BRAND_ICON_TOO_LARGE: '图片不能超过 4 MB',
  BRAND_ICON_TOO_LARGE_DIM: '图片不能超过 1024×1024 像素',
  BRAND_ICON_TOO_SMALL: '图片至少 16×16 像素',
  BRAND_ICON_INVALID: '图片无法解析，请换一张 PNG',
  BRAND_ICON_SAVE_FAILED: '图标保存失败，请检查运行目录写入权限',

  // ── D-20 补齐：Rust 侧已按 `CODE: 详情` 抛出、但此前没有登记的码 ──
  // 这些码此前会以 `AUTH_EXCHANGE_REJECTED: …` 原样透出到界面（错误码不该给用户读）。
  // 下面每条只写「怎么办」，Rust 带来的诊断详情由 errorTextOf 追加在括号里。

  // 登录与账号服务（Rust auth 模块）
  AUTH_LOGIN_IN_PROGRESS: '已有一个登录窗口在进行中，请先完成或取消它',
  AUTH_STATE_MISMATCH: '登录回调校验失败，请重新发起登录',
  AUTH_SERVER_URL_INVALID: '账号服务地址填错了，请检查协议与端口',
  AUTH_SERVER_INSECURE: '远程账号服务必须使用 HTTPS，请改回 https 地址',
  AUTH_SERVER_BAD_RESPONSE: '账号服务健康检查没有返回可解析的结果，请确认服务已启动',
  AUTH_SERVER_MISMATCH: '这个地址不是通天办公的综合服务端，请核对服务地址',
  AUTH_EXCHANGE_REJECTED: '登录凭据被账号服务拒绝，请重新登录',

  // IM 推送入参（Rust bridge/im.rs）
  IM_TARGET_MISSING: '没有指定要推送给谁，请先选择会话',
  IM_TEXT_MISSING: '推送内容为空，请填写要发送的文字',

  // 专家（Rust expert 模块）
  EXPERT_INVALID: '专家标识或名称不合规，请按提示修改后保存',
  EXPERTS_CORRUPT: '专家注册表已损坏，请从备份恢复或重建专家列表',
  EXPERT_NOT_FOUND: '该专家已不存在，请刷新列表后重试',
  EXPERT_MODEL_NOT_FOUND: '专家绑定的模型还没有添加，请先在模型中心添加该模型',
  EXPERT_MODEL_UNAVAILABLE: '专家绑定的模型当前不可用，请换一个模型或检查其配置',
  EXPERT_NAME_DUPLICATED: '已有同名专家，请换一个名称（同名会无法确定派发给谁）',
  EXPERT_LIMIT: '专家数量已达上限，请先删除不用的专家',

  // 模型配置与计划解析（Rust harness 模块）
  MODEL_INCOMPLETE: '模型配置不完整，请补齐服务端模型名与接入地址',
  MODEL_NOT_CONFIGURED: '还没有可用的模型，请在「设置 → 模型与用量」中添加模型',
  MODEL_KEY_MISSING: '该模型没有配置 API Key，请补填后重试',
  PLAN_INVALID: '模型返回的任务计划无法解析，请重试或更换模型',
  CHECKPOINT_WORKDIR_MISSING: '检查点记录的工作目录已不存在，无法从该检查点继续',
  HARNESS_WORKDIR_INVALID: '任务的工作目录不可访问，请确认目录存在且有读取权限',

  // 会话与工作台（Rust lib.rs / store/messages.rs）
  CHAT_INVALID_CONVERSATION: '会话标识为空，请刷新后重新打开会话',
  MODE_INVALID: '未知的工作台模式，请重新选择工作模式',
  CHAT_EDIT_INVALID_CONV: '会话标识为空，无法修改这条提问',
  CHAT_EDIT_EMPTY_CONTENT: '提问内容不能为空，请输入文字后再提交',
  CHAT_EDIT_INVALID_TURN: '轮次不合法，请刷新后重试',
  CHAT_EDIT_TURN_MISSING: '这一轮在本地库里没有提问记录，无法编辑',
  CHAT_FEEDBACK_INVALID_VALUE: '评价取值不合法，请重新点击点赞或点踩',
  CHAT_FEEDBACK_TURN_MISSING: '这一轮还没有回复内容，无法记录评价',

  // 模型价格与故障转移（Rust model/service.rs）
  MODEL_PRICE_INVALID: '单价必须是非负数字，请检查输入',
  MODEL_PRICE_CURRENCY_INVALID: '币种必须是 3 位字母代码（如 CNY、USD）',
  MODEL_PRICE_INCOMPLETE: '输入与输出单价要同时填写，或同时留空',
  MODEL_FALLBACK_LIMIT: '故障转移链最多 3 个模型，请删掉多余的',
  MODEL_FALLBACK_INVALID: '故障转移链里有空模型，请重新选择',
  MODEL_FALLBACK_SELF: '故障转移链不能指向模型自己',
  MODEL_FALLBACK_DUPLICATE: '故障转移链里有重复模型，请去重',
  MODEL_FALLBACK_NOT_FOUND: '转移目标模型已不存在，请重新选择',

  // PPTX 模板（Rust pptx 模块）
  PPTX_INVALID_NAME: '模板名称不合法，请去掉控制字符后重试',
  PPTX_TEMPLATE_UNSUPPORTED: '只支持 .pptx 模板，.ppt 等旧格式请先另存为 .pptx',
  PPTX_TEMPLATE_INVALID: '该 .pptx 结构不合法（可能已损坏或含越界路径），已拒绝导入',
  PPTX_TEMPLATE_UNREADABLE: '模板文件读不到，请确认文件仍存在且有读取权限',
  PPTX_TEMPLATE_TOO_LARGE: '模板体积超出上限，请压缩或改用更小的模板',
  PPTX_LIMIT_REACHED: '模板数量已达上限，请先删除不用的模板',
  PPTX_TEMPLATE_EXISTS: '同名模板已存在，请改名或先删除原模板',

  // 系统级沙箱（Rust sandbox 模块；Hooks 与终端委派共用）
  SANDBOX_UNSUPPORTED: '当前平台没有系统级沙箱能力，该功能仅在 Windows 10/11 可用',
  SANDBOX_PROFILE_FAILED: '创建系统隔离账户失败，请以管理员权限重试或重启后再试',
  SANDBOX_CAPABILITY_FAILED: '系统隔离权限标识解析失败，请重启应用后再试',
  SANDBOX_ACL_FAILED: '目录权限设置失败，请检查 icacls 是否被安全软件拦截',
  SANDBOX_PROGRAM_NOT_FOUND: '找不到要执行的程序，请确认已安装并在 PATH 中',
  SANDBOX_LAUNCH_FAILED: '隔离执行启动失败，请检查程序与参数后重试',
  SANDBOX_PIPE_FAILED: '无法建立输出管道，请关闭占用该程序的其他窗口后重试',

  // 任务栏角标（Rust system/badge.rs）
  BADGE_UNSUPPORTED: '当前平台不支持任务栏未读角标，可用系统通知代替',
  BADGE_SET_FAILED: '任务栏角标写入失败，窗口可能已关闭',
  BADGE_WINDOW_MISSING: '主窗口不存在，无法更新任务栏角标',

  // ── D-20 补齐第二批：经 map_err / ok_or 抛出的码（此前会以裸码透出） ──

  // 登录回调与账号服务
  AUTH_LOGIN_TIMEOUT: '等待登录超时，请在浏览器里完成登录后重试',
  AUTH_SERVER_UNREACHABLE: '连不上账号服务，请先启动综合服务端',
  AUTH_EXCHANGE_FAILED: '换取登录令牌失败，请确认账号服务正在运行',
  AUTH_EXCHANGE_INVALID: '账号服务没有返回可用的登录态，请重新登录',
  AUTH_EXCHANGE_PARSE: '账号服务的登录响应无法解析，请检查服务端版本',
  AUTH_OPEN_BROWSER_FAILED: '登录页面打不开，请手动复制登录地址到浏览器',
  CALLBACK_BIND_FAILED: '本地登录回调端口被占用，请关闭占用该端口的程序后重试',
  CALLBACK_ACCEPT_FAILED: '登录回调接收失败，请重新发起登录',

  // 会话与评价落库（Rust store/messages.rs；SQLite 失败要说人话）
  CHAT_MESSAGE_SAVE_FAILED: '这条消息没有写进本地记录，请检查磁盘空间与目录权限',
  CHAT_FEEDBACK_CHECK_FAILED: '读取该轮评价时出错，请刷新后重试',
  CHAT_FEEDBACK_SAVE_FAILED: '评价没有保存成功，请重新点击点赞或点踩',
  CHAT_EDIT_CHECK_FAILED: '编辑前检查该轮记录失败，请刷新后重试',
  CHAT_EDIT_BEGIN_FAILED: '无法开始编辑这条提问，请稍后重试',
  CHAT_EDIT_TRUNCATE_FAILED: '删除后续轮次失败，提问保持原样',
  CHAT_EDIT_FEEDBACK_FAILED: '清理后续轮次的评价失败，请刷新后重试',
  CHAT_EDIT_SAVE_FAILED: '修改后的提问没有写入本地记录，请重试',
  CHAT_EDIT_COMMIT_FAILED: '修改未能提交（本地记录已回滚），请重试',

  // 检查点、执行面与状态校验（Rust harness / lib.rs）
  CHECKPOINT_UNAVAILABLE: '本地还没有可恢复的检查点，本轮无法从中间步骤继续',
  CHECKPOINT_MODEL_UNAVAILABLE: '恢复检查点所需的模型配置读不出来，请重新配置模型后再试',
  CHECKPOINT_RESUME_FAILED: '无法恢复本地执行上下文，请重新发起该任务',
  RUNTIME_BUNDLE_PANIC: '任务输入装配内部异常，请重试；持续失败请查看日志',
  HARNESS_ERROR: '任务执行失败，请查看下方原因后重试',
  [ErrorCode.VALIDATION_ERROR]: '当前任务状态不允许这个操作，请刷新后确认状态',
  WORK_DIR_UNAVAILABLE: '无法创建工作目录，请检查磁盘与写入权限',
  MODEL_EMPTY_RESPONSE: '模型没有返回任何内容，请重试或换一个模型',
  MODEL_STREAM_FAILED: '模型回复中断，请重试；连续中断请检查网络',
  MODEL_CLIENT_FAILED: '无法发起模型请求，请检查模型接入地址配置',

  // 模型配置读写（Rust model/service.rs）
  [ErrorCode.MODEL_NOT_FOUND]: '该模型不在列表里，请刷新模型中心后重试',
  [ErrorCode.MODEL_AUTH_FAILED]: '模型密钥被拒绝，请在模型中心确认 API Key 有效',
  MODEL_CONFIG_INVALID: '模型配置里的故障转移清单不是合法列表，请重新保存',
  MODEL_WRITE_FAILED: '模型设置没有保存成功，请检查本地存储后重试',

  // 专家注册表写入
  EXPERTS_SAVE_FAILED: '专家列表保存失败，请检查本地存储是否可写',

  // PPTX 模板台账与文件
  PPTX_LEDGER_UNREADABLE: '模板台账读不出来，请检查模板目录是否可读',
  PPTX_LEDGER_INVALID: '模板台账不是合法 JSON，请删除该文件后重新导入模板',
  PPTX_SAVE_FAILED: '模板目录不可写，请检查运行目录权限',
  PPTX_TEMPLATE_NOT_FOUND: '该模板不存在（内置模板不可删除）',

  // 系统沙箱补充（Hooks 与终端委派共用；D-03 同一条链路）
  SANDBOX_FILE_BLOCKED: '该路径命中沙箱文件黑名单，已阻止访问',
  SANDBOX_LAUNCHER_MISSING: '找不到任我行自身程序路径，系统沙箱不可用',
  SANDBOX_TEMP_FAILED: '无法创建沙箱临时目录，请检查 LOCALAPPDATA 与磁盘空间',
  SANDBOX_WORKSPACE_MISSING: '工作区目录不存在，请先在设置中选择有效的工作目录',

  // Hook 审批网关（Rust bridge/hook_gate.rs；拒绝原因回给 Hook 进程）
  HOOK_GATE_UNAUTHORIZED: 'Hook 请求令牌不正确，网关已拒绝（请确认由本机的任我行启动 Hook）',
  HOOK_GATE_BAD_JSON: 'Hook 请求体不是合法 JSON，网关已拒绝',
  HOOK_GATE_MISSING_EVENT: 'Hook 请求缺少事件类型，网关已拒绝',
  HOOK_GATE_PANIC: 'Hook 网关处理时发生内部异常，本次 Hook 没有得出结果',
  HOOK_SANDBOX_FAILED: 'Hook 在系统隔离下执行失败，请查看原因或临时关闭系统隔离',
  HOOK_SANDBOX_SHELL_REQUIRED: 'Hook 命令含管道或重定向，系统隔离下无法等价执行；请改写为「程序 + 参数」的单条命令，或在设置页关闭系统隔离',
  HOOK_SANDBOX_UNBALANCED_QUOTE: 'Hook 命令里的双引号没有成对闭合，系统隔离下无法安全拆分参数，请修正引号',

  // 工具逐次审批网关（Rust bridge/tool_gate.rs）
  TOOL_APPROVAL_UNAUTHORIZED: '工具审批请求令牌不正确，已拒绝本次授权查询',
  TOOL_APPROVAL_BAD_JSON: '工具审批请求体不是合法 JSON，已拒绝',
  TOOL_APPROVAL_DISABLED: '工具审批开关处于关闭状态，网关拒绝了这次审批请求',
  TOOL_APPROVAL_INVALID_TOOL: '工具名缺失或过长，无法登记审批结果',

  // 本地存储初始化（Rust store；启动即失败时给得出原因）
  STORE_MKDIR_FAILED: '无法创建本地数据目录，请检查磁盘与写入权限',
  STORE_OPEN_FAILED: '本地记录文件打不开，请确认文件未被其他实例占用',
  STORE_PRAGMA_FAILED: '本地数据库参数设置失败，请重启应用；持续失败请备份后重建记录',
  STORE_MIGRATE_FAILED: '本地记录结构升级失败，应用仍按旧版本读写；请备份后重试',
  STORE_MIGRATE_RECORD_FAILED: '有历史数据没能迁移到新结构，其余部分可正常使用',

  // 任务窗口与前台唤起
  TASK_WINDOW_INVALID: '会话标识不合法，无法唤起任务窗口',
  TASK_WINDOW_FOCUS_FAILED: '任务窗口唤起失败，请从任务列表重新打开',
  TASK_WINDOW_CREATE_FAILED: '任务窗口创建失败，请重试',

  // 引擎不可达（终端 AI+ 委派走本机引擎）
  ENGINE_UNREACHABLE: '连不上本机 Agent 引擎，请先启动引擎或重启应用',

  // 任务状态成因（事件流里的 statusReasonCode，界面按需展示）
  USER_CANCELLED: '任务由你手动取消',
  REJECTED: '该计划被拒绝，任务未执行',

  // ── D-20 补齐第三批：引擎（Python）抛出的码 ──
  // 引擎经 HTTP/SSE 返回的也是 `CODE: 中文详情`，与桌面端同一套文案表，避免两套口径。

  // ACP 终端委派（engine/acp_server.py）
  ACP_ENGINE_UNAVAILABLE: '没找到本机 Agent 引擎，请先启动任我行桌面端，或设置引擎地址',
  ACP_SESSION_NOT_FOUND: '该 ACP 会话不存在，请先新建会话再发起对话',
  ACP_SESSION_LOAD_UNSUPPORTED: '这个 ACP Agent 不支持恢复历史会话，请新建会话',
  ACP_CWD_INVALID: 'ACP 指定的工作目录不存在，请先在设置里选择有效目录',
  ACP_UNSUPPORTED_CONTENT: '这个 ACP Agent 目前只接受纯文本提问，请去掉图片或附件',
  ACP_EMPTY_PROMPT: 'ACP 提问正文为空，请填写内容后再发送',

  // 平台网关与模型校验（engine/agent_platform/adapters.py、engine/server.py）
  GATEWAY_ERROR: 'IM 网关调用失败，请查看下方原因后重试',
  MODEL_INVALID: '模型配置不完整，请检查模型名称、来源与接入地址',

  // ── D-46 Super-agent 事件账本与准入策略（Rust superagent/{ledger,policy,sources}.rs）──
  // 绝大多数只在后台循环里出现，但错误口径是全量扫描的：任何一条被界面拿到时，
  // 也要说得出人话和下一步。策略读不到时的文案必须写明「已回落到影子模式」，
  // 不能让人以为自动接单还在跑。
  SUPERAGENT_SOURCE_KIND_UNSUPPORTED: '这个事件入口类型暂不支持，请在入口列表里选择已支持的类型',
  SUPERAGENT_SOURCE_INSTANCE_REQUIRED: '事件入口必须指明具体对象（哪个项目、哪个会话或哪个目录），不支持整类放行',
  SUPERAGENT_SOURCE_INSERT_FAILED: '事件入口登记失败，请稍后重试；持续失败请检查本地记录是否可写',
  SUPERAGENT_SOURCE_UPDATE_FAILED: '事件入口的设置没能保存，请重试',
  SUPERAGENT_SOURCE_READ_FAILED: '事件入口读取失败，请刷新入口列表',
  SUPERAGENT_SOURCE_LIST_FAILED: '事件入口列表读取失败，请刷新后重试',
  SUPERAGENT_SOURCE_NOT_FOUND: '这个事件入口已不存在，可能被关闭或改名，请刷新入口列表',
  SUPERAGENT_CURSOR_UPDATE_FAILED: '事件读取位置没能保存，下次启动可能重读这一段，请重试',
  SUPERAGENT_TRIGGER_ID_MISSING: '这条事件缺少上游标识，无法判断是否重复，已拒绝入账',
  SUPERAGENT_INGEST_BEGIN_FAILED: '事件入账没能开始，请重试；提示被占用时请先关闭其他实例',
  SUPERAGENT_TRIGGER_INSERT_FAILED: '事件没能写入账本，请重试',
  SUPERAGENT_TRIGGER_READ_FAILED: '事件读取失败，请刷新候选清单',
  SUPERAGENT_INGEST_COMMIT_FAILED: '事件写入未提交（已整体回滚，不会留下半条记录），请重试',
  SUPERAGENT_TRIGGER_NOT_FOUND: '这条事件已不在账本里，请刷新后重试',
  SUPERAGENT_TRIGGER_LIST_FAILED: '事件清单读取失败，请刷新后重试',
  SUPERAGENT_POLICY_INVALID: '准入策略不合法，已按默认策略（影子模式、零授权）继续，请修正后重新保存',
  SUPERAGENT_MODE_NOT_AVAILABLE: '自动接单需要先落地工单层与执行体隔离，现在只能选「影子模式」或「关闭」',
  SUPERAGENT_POLICY_READ_FAILED: '准入策略读取失败，已按默认策略（影子模式、零授权）继续',
  SUPERAGENT_POLICY_INSERT_FAILED: '新的准入策略没能保存，请重试',
  SUPERAGENT_DECISION_INSERT_FAILED: '准入判定没能留痕，候选清单会缺这一条，请重试',
  SUPERAGENT_DECISION_READ_FAILED: '准入判定读取失败，请刷新后重试',
  SUPERAGENT_CANDIDATE_READ_FAILED: '主动候选清单读取失败，请刷新后重试',
  SUPERAGENT_DECISION_COUNT_FAILED: '今日研判条数没能统计，为避免超量已跳过本轮研判',
  SUPERAGENT_STATS_FAILED: '事件账本统计读取失败，请刷新后重试',

  // 事件源读取（superagent/sources.rs）：都是后台巡检里的失败，入口列表要能原样解释
  SUPERAGENT_SOURCE_NOT_WIRED: '这类事件源还在引擎侧，尚未接到本机的账本，暂时读不到事件',
  SUPERAGENT_SCHEDULE_INVALID: '定时或条件入口缺少有效的时间配置（间隔至少 60000 毫秒，或指定一个到点时间）',
  SUPERAGENT_WORKSPACE_DIR_MISSING: '工作区入口没有配置有效目录，请重新选择要监听的工作区后再开启',
  SUPERAGENT_WORKSPACE_SCAN_FAILED: '工作区目录读取失败（可能被移动或权限不足），请重新选择目录',
  SUPERAGENT_FETCH_EVENTS_FAILED: '项目事件读取失败，账本保持在上次的位置，恢复后会补齐这一段',

  // 候选清单导出（superagent/mod.rs）：影子模式的产物就是这份清单，导出失败要说清文件有没有落盘
  SUPERAGENT_EXPORT_FORMAT_UNSUPPORTED: '这个导出格式不支持，清单只能导出为 JSON 或 CSV',
  SUPERAGENT_EXPORT_FAILED: '候选清单没能写成文件（账本本身没变，可稍后重试；请检查本地记录目录是否可写）',

  // 模型研判（superagent/assess.rs + ledger.rs）：第二层只花模型的钱问「值不值得开工」，
  // 失败都不改变第一层的判定，也不留下「已研判」的假记录，界面要说清「这条还没判过」。
  SUPERAGENT_ASSESS_MODEL_MISSING:
    '本机还没配好可用模型，模型研判这一层跑不起来；事件仍照常记账与判定，请先在模型设置里选择默认模型',
  SUPERAGENT_ASSESS_TIMEOUT:
    '模型没能在规定时间内给出研判，这条候选仍算「未研判」，可以稍后再试一次',
  SUPERAGENT_ASSESS_FAILED:
    '模型研判调用失败（账本与判定都不受影响），请检查模型是否可用后重试',
  SUPERAGENT_ASSESS_WRITE_FAILED:
    '模型给出的结论没能写进判定留痕，这条候选仍显示未研判，请刷新后重试',
  SUPERAGENT_ASSESS_COUNT_FAILED:
    '今日模型研判条数没能统计，为避免超量已跳过这一轮，事件记账不受影响',
  SUPERAGENT_POLICY_REQUEUE_FAILED:
    '策略已经保存成新版本，但还没研判的候选没能重新排队；这些候选暂时不会送去模型研判，事件记账与判定留痕都不受影响，可再保存一次策略触发重排',

  // 本地通道（renderer/lib/superagent-local.ts）：浏览器/Mock 拿不到本机账本，如实拒绝而不编造候选
  SUPERAGENT_DESKTOP_ONLY: '主动候选的事件账本与后台巡检只在桌面版本机运行，请在桌面版任我行中查看这一页',

  // ── D-12 应用更新回路（Rust app_update/{mod,manifest}.rs，走 exec 车道 exec:update_*）──
  // 这一族的原则是「宁可拒得难看，也不给一个点了必然失败的按钮」：所以每条文案都要
  // 说清**卡在哪一步**以及**下一步做什么**，不能只说「更新失败」。
  // 清单格式类的码出现在下载之前（验签先于下载），下载/暂存类出现在之后，两者不能混着写。
  UPDATE_INVALID_MANIFEST:
    '更新清单不符合约定格式（字段缺失、版本写法不对或超出条数上限），本机已按「无可用更新」处理，不会尝试猜测清单内容',
  UPDATE_INVALID_URL:
    '更新渠道地址不合法：只接受 https 的完整地址（本机自测可用 http 回环），也不允许指向内网或保留地址，请在设置里改正后再试',
  UPDATE_INVALID_CHANNEL:
    '更新渠道名不合法：只能是小写字母、数字、连字符与下划线组成的 1—32 个字符（如 stable / beta），请重新填写',
  UPDATE_INVALID_REQUEST:
    '这条更新请求缺少必要参数或走错了步骤（缺版本号、缺文件名、确认词不对或动作与请求不符），本机没有执行任何动作',
  UPDATE_STAGE_FAILED:
    '安装包没能写进本机暂存区（目录或磁盘不可写、文件改名失败），清单与已下载的部分都保留着，请检查磁盘空间与目录权限后重试',
  UPDATE_TOO_LARGE:
    '更新包超出允许的大小上限，或与清单声明的字节数不符，本机已中断这次下载并清理了临时文件',
  UPDATE_FETCH_FAILED:
    '取不到更新清单（DNS 解析失败、连接超时或网络不通）：这一步没有改动本机任何文件，可稍后重试，或在设置里改用手动导入本地包',
  UPDATE_HTTP_ERROR:
    '更新服务器返回了错误状态码，这份清单今天拿不到：请确认渠道地址是否正确，或稍后再试',
  UPDATE_HASH_MISMATCH:
    '下载完成的安装包哈希与清单声明不一致，已丢弃这份文件并保留暂存区原有内容：这可能是链路被改动或清单写错了，本机不会安装它',
  UPDATE_SIGNATURE_INVALID:
    '安装包的签名与清单内容不匹配（发布者、版本、平台、架构或哈希被改动都会导致验不过），本机已拒绝，这个包不会进入下载或安装',
  UPDATE_NO_TRUST_ANCHOR:
    '本机还没有任何受信任的发布公钥，因此无法验证任何更新包的签名：请在设置里由管理员添加官方公钥（发布者标识 + base64 公钥），或改用手动导入本地安装包',
  UPDATE_UNTRUSTED_PUBLISHER:
    '这份更新包的发布者不在本机信任列表里，已拒绝；如果确实要接受这个发布者，请先在设置里添加它的公钥',
  UPDATE_PUBLISHER_KEY_CONFLICT:
    '同一个发布者标识已经有一把不同的公钥（随包内嵌的删不掉，本机的换钥要先删旧记录）：本次没有写入新的信任，请核对公钥来源',
  UPDATE_INVALID_PUBLISHER_KEY:
    '发布公钥格式不对：需要 32 字节 Ed25519 公钥的 base64 写法，请从发布方原样复制后重试',
  UPDATE_UNSUPPORTED_ARTIFACT:
    '这份更新包的类型或平台/架构与本机不符（当前只支持 Windows 的 NSIS 安装包与 MSI），本机不会尝试执行它',
  UPDATE_INSTALL_FAILED:
    '安装器没能启动（文件被占用、权限不足或被安全软件拦下）：暂存的安装包与回滚材料都还在，状态已退回「已下载」，可以稍后重试',

  // exec 车道里已登记但 Rust 侧尚未实现的 method（ipc/mod.rs 的 NOT_IMPLEMENTED）
  IPC_LANE_NOT_IMPLEMENTED:
    '这个动作在本机的执行车道里还没有实现实现（返回的是原始方法名）：界面按失败处理，不会显示成已完成',
};

/**
 * 文案表的键清单（只给契约测试反向核对用，不参与任何运行时分支）。
 * 有它，测试就不必再正则解析这份上千行的表；两边同改时才不会被静默放过。
 */
export const ERROR_MESSAGE_CODES: readonly string[] = Object.keys(ERROR_MESSAGES);

/** `BRIDGE_CHAT_HTTP_{status}` 为动态码，按前缀归一 */
const BRIDGE_CHAT_HTTP_PREFIX = 'BRIDGE_CHAT_HTTP_';

/** `COLLAB_HTTP_{status}` 同为动态码（协作服务返回的 HTTP 状态直接进码里） */
const COLLAB_HTTP_PREFIX = 'COLLAB_HTTP_';

/**
 * `MODEL_HTTP_{status}` 是本地 harness 的模型调用码：端点返回的状态码直接拼进码里
 * （400 常见于「这台模型不接受工具调用」，401/403 是密钥，429 是限流，5xx 是端点故障）。
 */
const MODEL_HTTP_PREFIX = 'MODEL_HTTP_';

function matchMessage(code: string): string | undefined {
  if (code.startsWith(BRIDGE_CHAT_HTTP_PREFIX)) {
    const status = code.slice(BRIDGE_CHAT_HTTP_PREFIX.length);
    return `Agent 引擎返回错误（HTTP ${status}）`;
  }
  if (code.startsWith(COLLAB_HTTP_PREFIX)) {
    const status = code.slice(COLLAB_HTTP_PREFIX.length);
    return `协作服务返回错误（HTTP ${status}）`;
  }
  if (code.startsWith(MODEL_HTTP_PREFIX)) {
    const status = code.slice(MODEL_HTTP_PREFIX.length);
    return `模型服务返回错误（HTTP ${status}）：请核对模型名与端点是否支持本轮的工具调用，必要时在容灾链里换一台`;
  }
  return ERROR_MESSAGES[code];
}

/** 错误码 → 用户文案（未知码降级展示原始码，不白屏） */
export function errorMessageOf(code: string | undefined | null): string {
  if (!code) return '发生未知错误';
  return matchMessage(code) ?? `发生错误（${code}）`;
}

/**
 * Tauri command 拒绝值 → 原始 `错误码: 诊断详情` 字符串（**不做文案映射**）。
 *
 * 只给需要按码分支的地方用（如 `startsWith('ARCHIVE_CONFIRM_REQUIRED')`、正则判断可否重试）。
 * 展示一律走 `errorTextOf`：这里返回的是给程序看的裸码，直接上界面就把错误码丢给了用户。
 * 非字符串且非 Error 的载荷返回空串——调用方据此走「未知错误」分支，不要把 `String(obj)`
 * 的 `[object Object]` 当成错误信息传下去。
 */
export function rawErrorText(err: unknown): string {
  if (typeof err === 'string') return err;
  if (err instanceof Error) return err.message;
  return '';
}

/**
 * Tauri command 拒绝值 → 用户文案。
 * Rust 侧 `Err(String)` 在 JS 里是裸字符串（非 Error 实例），格式为 `错误码: 诊断详情`，
 * 详情可能含盘符冒号（如 resource_dir="C:\..."），故只在第一个冒号处切分。
 */
export function errorTextOf(err: unknown, fallback = '发生未知错误'): string {
  const raw = rawErrorText(err);
  if (!raw.trim()) return fallback;
  const sep = raw.indexOf(':');
  const code = (sep < 0 ? raw : raw.slice(0, sep)).trim();
  const detail = sep < 0 ? '' : raw.slice(sep + 1).trim();
  const mapped = matchMessage(code);
  if (!mapped) return raw; // 未映射的码：原样展示，保留诊断信息
  return detail ? `${mapped}（${detail}）` : mapped;
}
