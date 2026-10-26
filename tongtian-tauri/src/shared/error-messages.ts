// src/shared/error-messages.ts — 错误码文案映射表（流 G4）
//
// 三层架构演进 P0：9 个云端必需错误码 + 既有 Bridge 错误码的 UI 文案。
// 未知错误码走默认分支（防白屏，中性文案不承诺时间）。

import { ErrorCode } from './contracts';

const ERROR_MESSAGES: Record<string, string> = {
  // ── 三层架构演进云端必需码 ──
  [ErrorCode.BUDGET_EXHAUSTED]: '预算已用尽，任务已暂停，恢复后将自动继续',
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

  // ── 技能（Skills 目录事实来源） ──
  [ErrorCode.SKILL_NOT_FOUND]: '未找到该技能文件，请确认路径后重试',
  [ErrorCode.SKILL_INSTALL_FAILED]: '技能安装失败，请检查运行目录写入权限',
  [ErrorCode.SKILL_EXECUTION_FAILED]: '技能执行失败，请查看执行过程明细后重试',
  [ErrorCode.SKILL_INCOMPATIBLE]: '该技能包不符合安装要求（仅支持含 SKILL.md 的技能目录）',
  [ErrorCode.SKILL_UNSUPPORTED]:
    '当前运行通道不支持技能增删，请在桌面版通天办公中管理技能',
  [ErrorCode.SKILL_SCAN_BLOCKED]:
    '技能被安全扫描拦截：SKILL.md 包含高风险指令，未安装或启用',

  // ── 专家套件（套件目录 manifest.json 事实来源） ──
  [ErrorCode.SUITE_NOT_FOUND]: '未找到该已安装套件，请刷新列表后重试',
  [ErrorCode.SUITE_INCOMPATIBLE]:
    '套件包不符合安装要求：ZIP 需在根目录或一级目录下含 plugin.json，且其中必须有 name 字段',
  [ErrorCode.SUITE_INSTALL_FAILED]: '套件安装失败，请检查运行目录写入权限后重试',

  // ── 插件 SDK（安装前预检） ──
  [ErrorCode.PLUGIN_INVALID_PACKAGE]:
    '插件包无法预检：请确认上传的是含 plugin.json 的 .zip，且未超过大小上限',
  [ErrorCode.PLUGIN_UNSUPPORTED]:
    '当前运行通道无法预检插件包，请在桌面版通天办公中使用',

  // ── 本地知识库（RAG） ──
  [ErrorCode.KNOWLEDGE_INVALID_NAME]: '知识库名称不合法：不能为空，最多 60 字，且不能包含路径分隔符',
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
  [ErrorCode.KNOWLEDGE_UNSUPPORTED]: '当前运行通道没有知识库落盘能力，请在桌面版通天办公中使用',

  // ── 在线技能 / 套件市场 ──
  [ErrorCode.MARKET_UNSUPPORTED]:
    '当前运行通道没有市场下载与落盘能力，请在桌面版通天办公中使用在线市场',
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

  // ── 安全与治理（PRD §4.7） ──
  [ErrorCode.POLICY_INVALID_MODE]: '出域控制模式非法，仅支持白名单 / 黑名单',
  [ErrorCode.POLICY_INVALID_LEVEL]: '沙箱执行级别非法，仅支持 L0 / L1 / L2',
  [ErrorCode.POLICY_INVALID_RANGE]: '沙箱资源上限越界，请检查 CPU / 内存 / 写盘数值',
  [ErrorCode.POLICY_INVALID_WORKSPACE]: '工作区标识非法，无法保存沙箱策略',
  [ErrorCode.POLICY_INVALID_COMMAND]:
    '命令白名单里有非法项：只能填裸命令名（如 python、git），不能带路径、空格或 shell 元字符',
  [ErrorCode.POLICY_INVALID_HOST]: '联网白名单里有非法主机名，请检查后重试',
  [ErrorCode.POLICY_CORRUPT]: '策略数据已损坏，请重新保存后再试',
  [ErrorCode.POLICY_READ_FAILED]: '安全策略读取失败',
  [ErrorCode.POLICY_SAVE_FAILED]: '安全策略保存失败',
  [ErrorCode.POLICY_EXTERNAL_API_DISABLED]: '外部 API 已被硬开关禁用，仅允许本地模型处理',
  [ErrorCode.POLICY_DOMAIN_NOT_ALLOWED]: '该域名不在出域白名单内，请求已被拦截',
  [ErrorCode.POLICY_DOMAIN_BLOCKED]: '该域名命中出域黑名单，请求已被拦截',
  [ErrorCode.POLICY_INVALID_URL]: '无法解析目标地址，请检查 URL 配置',
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

  // ── 定时任务 ──
  [ErrorCode.SCHEDULE_NOT_FOUND]: '定时任务不存在或已被删除',
  [ErrorCode.SCHEDULE_CRON_INVALID]: '定时规则表达式非法，请检查 cron 配置',
  [ErrorCode.SCHEDULE_EXECUTION_FAILED]: '定时任务执行失败，请查看执行记录了解详情',
  [ErrorCode.SCHEDULE_INVALID_FREQ]: '定时频率非法，仅支持 每小时 / 每天 / 每周 / 每月',
  [ErrorCode.SCHEDULE_INVALID_TIME]: '执行时刻非法，请使用 24 小时制 HH:MM（00:00–23:59）',
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
  [ErrorCode.SCHEDULE_PARSE_NO_TIME]: '没有识别到执行时刻，请补充具体时间，例如「早上 9 点」',
  [ErrorCode.SCHEDULE_PARSE_TIME]: '识别到的时间不合法，请使用 00:00–23:59 之间的时刻',
  [ErrorCode.SCHEDULE_UNSUPPORTED_FREQ]: '暂不支持按分钟或每 N 小时的间隔，请改用每小时 / 每天 / 每周 / 每月',
  [ErrorCode.SCHEDULE_UNSUPPORTED_DAY]: '暂只支持每月 1 号，其他日期请手动改为每周或每天',
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
    '当前运行通道无法打开系统隐私设置，请在桌面版通天办公中管理权限',

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

  // ── 远程桌面（本机画面；只看不控） ──
  [ErrorCode.REMOTE_UNSUPPORTED]:
    '当前平台或运行通道没有桌面截屏执行器，远程桌面仅在 Windows 桌面版可用',
  [ErrorCode.REMOTE_DISABLED]:
    '电脑操控档位为禁用，远程桌面无法开启；请先在「电脑操控」页调整档位',
  [ErrorCode.REMOTE_NO_SESSION]: '远程桌面会话未开启或已结束，本次未截取画面',
  [ErrorCode.REMOTE_SESSION_EXPIRED]:
    '远程桌面会话已到期（单次最长 10 分钟），画面已停止；请重新开启会话',

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
    '当前运行通道没有本机 shell 进程，终端 AI+ 仅在桌面版通天办公中可用',
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

  // ── 意识（awareness:* 通道，Rust awareness 模块抛出） ──
  [ErrorCode.AWARENESS_DIR_FAILED]: '意识目录无法访问，请检查存储位置权限后重试',
  [ErrorCode.AWARENESS_LOAD_FAILED]: '意识内容读取失败，可稍后重试或打开文件夹直接查看文件',
  [ErrorCode.AWARENESS_SAVE_FAILED]: '意识内容保存失败，磁盘写入受阻',
  [ErrorCode.AWARENESS_FILE_INVALID]: '意识文件不在允许编辑范围内，仅支持协作风格 / 工作手册 / 用户画像 / 长期记忆',
  [ErrorCode.AWARENESS_STYLE_INVALID]: '协作风格预设非法，仅支持 高效极简 / 深思熟虑 / 温和耐心 / 果断执行',
  [ErrorCode.AWARENESS_MEMORY_EMPTY]: '记忆条目内容不能为空',
  [ErrorCode.AWARENESS_MEMORY_NOT_FOUND]: '该记忆条目已不存在，可能已被删除',

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
  [ErrorCode.MCP_UNSUPPORTED]: '当前运行通道无法与 MCP 连接器握手，请在桌面版通天办公中完成连接',
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
  [ErrorCode.MCP_OAUTH_UNSUPPORTED]: '当前运行通道无法发起 OAuth 授权，请在桌面版通天办公中完成授权',

  // ── 项目协作（跨用户 / 跨机器；桌面端 Rust → 账号服务） ──
  [ErrorCode.COLLAB_UNSUPPORTED]:
    '当前运行通道无法访问协作服务，请在桌面版通天办公中使用项目协作',
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
  [ErrorCode.OPEN_EXTERNAL_UNSUPPORTED]: '当前运行通道没有系统默认应用，请在桌面版通天办公中打开',
  [ErrorCode.FILE_ACCESS_DENIED]: '该路径不在工作区文件白名单内，无法读写',
  [ErrorCode.FILE_WRITE_FAILED]: '文件写入失败，请检查目标目录写入权限',
  [ErrorCode.FILE_READ_FAILED]: '文件读取失败，可能是二进制文件或编码异常',
  [ErrorCode.FILE_CONVERSION_FAILED]: '文档正文提取失败，可尝试用本地应用打开后另存为 docx/xlsx/pptx/pdf',
  [ErrorCode.FILE_TOO_LARGE]: '文件超出大小上限，请拆分后重试',
  [ErrorCode.FILE_BACKUP_FAILED]: '附件暂存失败，请检查运行目录写入权限',
  [ErrorCode.FILE_UNSUPPORTED]: '当前运行通道无法读取本机文档正文，请在桌面版通天办公中预览',

  // ── IM 频道（长连接只跑在引擎进程；引擎不在时如实告知，不显示假清单） ──
  [ErrorCode.IM_ENGINE_UNAVAILABLE]: 'IM 频道的长连接由本地引擎进程承载，引擎未运行时无法查看或绑定频道，请先启动通天办公',
  [ErrorCode.IM_ENGINE_UNREACHABLE]: '本地引擎无响应，IM 频道状态暂时问不到，请重启引擎后刷新',
  [ErrorCode.IM_ENGINE_ERROR]: 'IM 频道接口返回错误，请查看下方原因',
  [ErrorCode.IM_BAD_RESPONSE]: 'IM 频道接口响应无法解析，请重启引擎后重试',
  [ErrorCode.IM_CHANNEL_UNKNOWN]: '没有这个 IM 频道，请刷新频道清单',
  [ErrorCode.IM_CHANNEL_INVALID]: '频道标识非法，已拒绝该请求',
  [ErrorCode.IM_UNSUPPORTED]: '该 IM 的长连接还没接入引擎，暂不能绑定；已接入的频道见清单里的「可绑定」',
  [ErrorCode.IM_DEPENDENCY_MISSING]:
    '这台机器的引擎运行时缺该频道依赖的官方 SDK，通道不可用（清单里会显示「尚未接入」）；请重新安装通天办公的随包运行时，或按下方原因处理',
  [ErrorCode.IM_NOT_CONFIGURED]: '还没有登记该频道的凭据，请先完成绑定',
  [ErrorCode.IM_CREDENTIAL_MISSING]: '必填凭据还没填全，缺哪一项已在字段上标出',
  [ErrorCode.IM_CREDENTIAL_INVALID]: '凭据格式不对（前缀或长度不符合该 IM 的要求），请回开发者后台复制完整值',
  [ErrorCode.IM_AUTH_FAILED]: '凭据被 IM 服务端拒绝，请在后台确认应用未停用且权限齐全',
  [ErrorCode.IM_API_ERROR]: 'IM 服务端返回业务错误，请查看下方原因',
  [ErrorCode.IM_CONNECTION_CLOSED]: '长连接被 IM 服务端关闭，正在按退避重试',
  [ErrorCode.IM_CONNECTION_FAILED]: '长连接建立失败，请检查本机到 IM 服务端的网络',
  [ErrorCode.IM_CONNECTION_KICKED]:
    '这个机器人的长连接被另一条新连接顶掉了（企业微信同一个 BotID 只允许一条有效长连接）。引擎已停止自动重连，否则两边会互相踢；请确认只有一个通天办公实例或脚本在用这个 BotID',
  [ErrorCode.IM_CHANNEL_ERROR]: 'IM 频道运行异常，请查看最近错误后重试',
  [ErrorCode.IM_REPLY_FAILED]: '任务已完成，但消息回贴失败，请检查频道凭据的发消息权限',
  [ErrorCode.IM_PREFERENCE_INVALID]: 'IM 频道偏好保存失败：开关值不合法，请重试',
};

/** `BRIDGE_CHAT_HTTP_{status}` 为动态码，按前缀归一 */
const BRIDGE_CHAT_HTTP_PREFIX = 'BRIDGE_CHAT_HTTP_';

function matchMessage(code: string): string | undefined {
  if (code.startsWith(BRIDGE_CHAT_HTTP_PREFIX)) {
    const status = code.slice(BRIDGE_CHAT_HTTP_PREFIX.length);
    return `Agent 引擎返回错误（HTTP ${status}）`;
  }
  return ERROR_MESSAGES[code];
}

/** 错误码 → 用户文案（未知码降级展示原始码，不白屏） */
export function errorMessageOf(code: string | undefined | null): string {
  if (!code) return '发生未知错误';
  return matchMessage(code) ?? `发生错误（${code}）`;
}

/**
 * Tauri command 拒绝值 → 用户文案。
 * Rust 侧 `Err(String)` 在 JS 里是裸字符串（非 Error 实例），格式为 `错误码: 诊断详情`，
 * 详情可能含盘符冒号（如 resource_dir="C:\..."），故只在第一个冒号处切分。
 */
export function errorTextOf(err: unknown, fallback = '发生未知错误'): string {
  const raw = typeof err === 'string' ? err : err instanceof Error ? err.message : '';
  if (!raw.trim()) return fallback;
  const sep = raw.indexOf(':');
  const code = (sep < 0 ? raw : raw.slice(0, sep)).trim();
  const detail = sep < 0 ? '' : raw.slice(sep + 1).trim();
  const mapped = matchMessage(code);
  if (!mapped) return raw; // 未映射的码：原样展示，保留诊断信息
  return detail ? `${mapped}（${detail}）` : mapped;
}
