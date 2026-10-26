/**
 * IM 频道真实性契约（任务 #46 / #47）
 *
 * 整改前这个页面整页没有后端：二维码是伪随机图案，「扫描下方二维码，自动创建并配置
 * 机器人」是不存在的注册流程，Slack 保存按钮只 setToast 不发任何请求，
 * 「准入策略（配对/开放）」在引擎里没有任何消费方。
 *
 * 现在唯一真值来源是引擎注册表：界面能配什么、连没连上、重连几次、最近错误，
 * 全部来自 `ChannelRuntime.view()`。Slack 与钉钉是 A 族方言（换一次性连接地址、逐帧回执、
 * HTTP 回贴），企业微信是 B 族方言（固定地址、带内订阅、回贴在同一条 socket 上等回执），
 * 飞书是 A 族的二进制变体（同样换地址与逐帧回执，但帧是 protobuf 且一条消息按 sum/seq
 * 拆成多帧），四条通道共用同一个连接循环与台账。本文件锁住六件事：
 *   1. 引擎通道清单与错误码、TS 契约三处对齐
 *   2. 三通道（Tauri / 引擎直连 / Mock）都有真实接线，Mock 通道如实拒绝
 *   3. 页面不再出现假承诺，且每个控件都绑定真实动作
 *   4. 凭据只进不出：视图与接口不得回显明文
 *   5. 「展示工具调用执行过程」这个偏得有引擎里的持有方和消费方（不再是死开关）
 *   6. 每个已接入通道的协议事实都能在官方 SDK / 官方文档里对上号
 */

import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { ErrorCode, IPC_CHANNELS } from '../../../src/shared/contracts';
import { errorMessageOf } from '../../../src/shared/error-messages';

function read(rel: string): string {
  return readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8').replace(/\r\n/g, '\n');
}

const imFramework = () => read('../../../engine/im_channels.py');
const slackChannel = () => read('../../../engine/slack_channel.py');
const dingtalkChannel = () => read('../../../engine/dingtalk_channel.py');
const wecomChannel = () => read('../../../engine/wecom_channel.py');
const feishuChannel = () => read('../../../engine/feishu_channel.py');
const server = () => read('../../../engine/server.py');
const rustIm = () => read('../../../src-tauri/src/bridge/im.rs');
const rustLib = () => read('../../../src-tauri/src/lib.rs');
const tauriApi = () => read('../../../src/renderer/lib/tauri-api.ts');
const engineApi = () => read('../../../src/renderer/lib/engine-api.ts');
const mockApi = () => read('../../../src/renderer/lib/mock-api.ts');
const alignmentApi = () => read('../../../src/renderer/lib/alignment-api.ts');
const store = () => read('../../../src/renderer/stores/imChannelStore.ts');
const page = () => read('../../../src/renderer/pages/IMChannel/index.tsx');
const sidebar = () => read('../../../src/renderer/components/Layout/Sidebar.tsx');
const settingsPage = () => read('../../../src/renderer/pages/Settings/index.tsx');

/** TS 契约里声明的全部 IM 错误码 */
const IM_CODES = Object.values(ErrorCode).filter((c) => c.startsWith('IM_'));

/** 引擎里 raise 出来的全部 IM 错误码 */
function engineRaisedCodes(): string[] {
  const src = `${imFramework()}\n${slackChannel()}\n${dingtalkChannel()}\n${wecomChannel()}\n${feishuChannel()}\n${server()}`;
  return [...src.matchAll(/"((?:TT_)?IM_[A-Z_]+)"/g)]
    .map((m) => m[1].replace(/^TT_/, ''))
    .filter((c) => c in ErrorCode || (Object.values(ErrorCode) as string[]).includes(c));
}

describe('错误码三处对齐（漂移即红）', () => {
  it('IM 码至少有引擎通道/连接/凭据三组', () => {
    expect(IM_CODES.length).toBeGreaterThanOrEqual(16);
    for (const needed of [
      'IM_UNSUPPORTED',
      'IM_DEPENDENCY_MISSING',
      'IM_AUTH_FAILED',
      'IM_REPLY_FAILED',
      'IM_ENGINE_UNAVAILABLE',
      'IM_CONNECTION_KICKED',
    ]) {
      expect(IM_CODES, `缺错误码 ${needed}`).toContain(needed);
    }
  });

  it('每一个 IM 码都有用户文案，未知码不裸奔', () => {
    for (const code of IM_CODES) {
      const text = errorMessageOf(code);
      expect(text, `${code} 未配文案`).not.toBe(`发生错误（${code}）`);
      expect(text.length).toBeGreaterThan(4);
    }
  });

  it('引擎抛出的 IM 码都在 TS 契约里（引擎不会返回界面看不懂的码）', () => {
    const declared = new Set<string>(Object.values(ErrorCode));
    for (const code of engineRaisedCodes()) {
      expect(declared, `引擎用到未声明的码 ${code}`).toContain(code);
    }
  });

  it('引擎 raise 的码覆盖界面会遇到的关键分支', () => {
    const src = `${imFramework()}\n${slackChannel()}`;
    for (const code of ['IM_UNSUPPORTED', 'IM_NOT_CONFIGURED', 'IM_CREDENTIAL_MISSING', 'IM_AUTH_FAILED']) {
      expect(src, `引擎没有 ${code} 分支`).toContain(`"${code}"`);
    }
  });
});

describe('引擎通道清单：支持与否只有一个来源', () => {
  it('已接入的是 Slack、钉钉、企业微信与飞书，其余仍走 Pending 如实拒绝', () => {
    expect(slackChannel()).toContain('supported = True');
    expect(dingtalkChannel()).toContain('supported = SDK_IMPORT_ERROR == ""');
    expect(wecomChannel()).toContain('supported = SDK_IMPORT_ERROR == ""');
    // 飞书看的是「装没装官方 SDK」，不是导入成没成功：启动阶段根本不 import
    expect(feishuChannel()).toContain('supported = SDK_PRESENT');
    expect(imFramework()).toContain('class PendingChannel(ImChannel)');
    expect(imFramework()).toContain('class WeChatChannel(PendingChannel)');
    // 钉钉、企业微信与飞书不再是占位类：占位清单里不能还留着它们（否则注册表会出现两份）
    expect(imFramework()).not.toContain('class DingTalkChannel(PendingChannel)');
    expect(imFramework()).not.toContain('class WeComChannel(PendingChannel)');
    expect(imFramework()).not.toContain('class FeishuChannel(PendingChannel)');
    expect(feishuChannel()).toContain('class FeishuChannel(ImChannel)');
    // 未实现的通道不假装可绑定：verify/open_connection 一律抛 IM_UNSUPPORTED
    expect(imFramework()).toContain('class ImChannel(ABC)');
    // 注册靠引擎 import，界面/Rust 不另列平台清单
    expect(server()).toContain('import slack_channel');
    expect(server()).toContain('import dingtalk_channel');
    expect(server()).toContain('import wecom_channel');
    expect(server()).toContain('import feishu_channel');
  });

  it('通道清单顺序由引擎决定：已接入排在未接入之前', () => {
    expect(imFramework()).toContain('key=lambda r: not r.channel.supported');
  });

  it('同构四步在框架里都有实现点（换地址→连 wss→逐帧回执→回贴）', () => {
    const src = imFramework();
    for (const hook of ['def open_connection', 'def parse_event', 'def send', 'def ack', 'def verify']) {
      expect(src, `框架缺 ${hook}`).toContain(hook);
    }
    expect(src).toContain('ws_connect(');
    expect(src).toContain('BACKOFF_SECONDS');
    // 回执由通道自己发（Slack 的 challenge 需要带响应体，钉钉的 SYSTEM 帧是另一种形状）
    expect(src).toContain('await channel.ack(ws, frame, ev)');
    expect(slackChannel()).toContain('"envelope_id"');
  });

  it('三族方言共用同一条循环：B 族靠 on_open/on_close/protocol_heartbeat，二进制族靠 binary_frames/decode_frame 接入', () => {
    const src = imFramework();
    expect(src).toContain('A 族（钉钉 / Slack）');
    expect(src).toContain('B 族（企业微信 aibot）');
    expect(src).toContain('A 族的二进制变体（飞书）');
    // 带内握手（订阅帧）与握手后要撤掉的保活任务
    expect(src).toContain('async def on_open');
    expect(src).toContain('async def on_close');
    expect(src).toContain('await channel.on_open(ws)');
    expect(src).toContain('await channel.on_close()');
    // 自带应用层心跳的通道关掉传输层 ping，否则和官方 SDK 的行为不一致
    expect(src).toContain('protocol_heartbeat: float | None = 10');
    expect(src).toContain('heartbeat=channel.protocol_heartbeat');
    expect(wecomChannel()).toContain('protocol_heartbeat = None');
    expect(feishuChannel()).toContain('protocol_heartbeat = None');
    // 框架不假设帧的形状：二进制通道自己回答「这一帧是什么」，返回 None 表示分片没收齐
    expect(src).toContain('binary_frames: bool = False');
    expect(src).toContain('def decode_frame(self, raw: bytes)');
    expect(src).toContain('frame = channel.decode_frame(msg.data)');
    expect(feishuChannel()).toContain('binary_frames = True');
    expect(feishuChannel()).toContain('def decode_frame(self, raw: bytes) -> dict | None');
    // 回贴要等的回执正是收帧循环读到的下一帧 → 任务必须并到后台，同会话内串行
    expect(src).toContain('def _spawn(');
    expect(src).toContain('def _conversation_lock(');
    expect(src).toContain('self._spawn(ev)');
    // 重试有副作用的错误（被新连接顶掉）不许退避重连
    expect(src).toContain('or not exc.retryable');
    expect(src).toContain('retryable: bool = True');
  });

  it('五个 HTTP 端点齐备，且挂在 FastAPI 生命周期上（启动即自动重连）', () => {
    const src = server();
    for (const route of [
      '@app.get("/api/im/channels")',
      '@app.post("/api/im/{kind}/config")',
      '@app.post("/api/im/{kind}/start")',
      '@app.post("/api/im/{kind}/stop")',
      '@app.post("/api/im/{kind}/forget")',
    ]) {
      expect(src, `引擎缺端点 ${route}`).toContain(route);
    }
    expect(src).toContain('lifespan=_im_lifespan');
    expect(src).toContain('async def _im_bootstrap');
    // IM 文本 → 真实任务，不是打个日志就算收到
    expect(src).toContain('_im_run_task');
    expect(src).toContain('_run_execution(session, task_id, text');
  });

  it('凭据文件 0600、台账损坏不覆盖、解绑不删历史', () => {
    const src = imFramework();
    expect(src).toContain('os.chmod(p, 0o600)');
    expect(src).toContain('.corrupt.json');
    expect(src).toContain('def drop_config');
    expect(src).not.toContain('unlink');
  });
});

describe('钉钉通道：协议事实来自官方 SDK，不自己发明字段', () => {
  const ding = () => dingtalkChannel();

  it('引官方 dingtalk_stream 的值对象来解码与回执（字段名不手抄）', () => {
    const src = ding();
    for (const sym of [
      'from dingtalk_stream import',
      'from dingtalk_stream.frames import Headers',
      'from dingtalk_stream.version import VERSION_STRING',
      'CallbackMessage.from_dict',
      'SystemMessage.from_dict',
      'ChatbotMessage.from_dict',
      'AckMessage.STATUS_OK',
      'Headers.CONTENT_TYPE_APPLICATION_JSON',
    ]) {
      expect(src, `钉钉通道缺 ${sym}`).toContain(sym);
    }
    // 订阅 topic 用 SDK 自己的常量，而不是抄一份字符串
    expect(src).toContain('ChatbotMessage.TOPIC');
  });

  it('换地址与回贴按 SDK 的写法（ticket 拼进查询串、回贴走 sessionWebhook）', () => {
    const src = ding();
    expect(src).toContain('quote_plus(ticket)');
    expect(src).toContain('/v1.0/gateway/connections/open');
    expect(src).toContain('/v1.0/oauth2/accessToken');
    expect(src).toContain('"subscriptions"');
    // 回贴地址取自 SDK ChatbotMessage 的 session_webhook 属性（字段名不手抄）
    expect(src).toContain('session_webhook');
    // 回贴载荷与 ChatbotHandler.reply_text 一致
    expect(src).toContain('"msgtype": "text"');
    expect(src).toContain('atUserIds');
  });

  it('Mock 开关沿用 SDK 自己的注入点，且引擎侧有本地对拍', () => {
    expect(ding()).toContain('DINGTALK_OPENAPI_ENDPOINT');
    const fake = read('../../../tests/integration/fake_dingtalk.py');
    expect(fake).toContain('/v1.0/gateway/connections/open');
    expect(fake).toContain('"/v1.0/im/bot/messages/get"');
    // 钉钉的 data 是 JSON 字符串，替身必须按同一形状发
    expect(fake).toContain('json.dumps(data, ensure_ascii=False)');
    const selftest = read('../../../tests/integration/engine_im_dingtalk_selftest.py');
    expect(selftest).toContain('FakeDingTalk');
    expect(selftest).toContain('phase_c()');
  });

  it('依赖声明与随包锁一致（缺 SDK 会让通道退回未接入）', () => {
    expect(read('../../../engine/requirements.txt')).toContain('dingtalk-stream==');
    expect(read('../../../engine/requirements.lock.txt')).toContain('dingtalk-stream==0.24.3');
    // SDK 的 websockets 依赖必须一起进锁：少一个就是导入期缺件
    expect(read('../../../engine/requirements.lock.txt')).toContain('websockets==');
    expect(read('../../../scripts/prepare-python.sh')).toContain('"dingtalk_stream"');
  });

  it('凭据只进请求体：账号信息里是脱敏 AppKey，且不写日志不打印', () => {
    const src = ding();
    expect(src).toContain('"appKey": _mask(client_id)');
    expect(src).toContain('def _mask(');
    expect(src).not.toContain('logging');
    expect(src).not.toContain('print(');
    // 错误详情只带钉钉的 code/message，不整包回显响应体（响应体可能含 accessToken）
    expect(src).toContain('只带钉钉的错误码与文案');
  });

  it('缺 SDK 时如实退回「尚未接入」，不给能点但必然失败的控件', () => {
    const src = ding();
    expect(src).toContain('supported = SDK_IMPORT_ERROR == ""');
    expect(src).toContain('def unsupported_reason');
    expect(src).toContain('无法按钉钉 Stream 协议收发');
  });
});

describe('企业微信通道：协议事实来自官方 aibot SDK，不自己发明字段', () => {
  const wc = () => wecomChannel();

  it('引官方 SDK 的 cmd 常量与 req_id 拼法（字段名不手抄）', () => {
    const src = wc();
    for (const sym of [
      'from aibot import WsCmd, generate_req_id',
      'from aibot.types import MessageType',
      'from aibot.ws import DEFAULT_WS_URL',
      'WsCmd.SUBSCRIBE',
      'WsCmd.HEARTBEAT',
      'WsCmd.CALLBACK',
      'WsCmd.EVENT_CALLBACK',
      'WsCmd.RESPONSE',
      'WsCmd.SEND_MSG',
      'generate_req_id(WsCmd.SUBSCRIBE)',
      'MessageType.Text.value',
    ]) {
      expect(src, `企业微信通道缺 ${sym}`).toContain(sym);
    }
    // 只借协议，不跑 SDK 的连接循环：否则框架的退避/台账之上会叠出第二套重连
    expect(src).not.toContain('WSClient(');
    expect(src).toContain('wss://openws.work.weixin.qq.com');
  });

  it('B 族方言的四件事按官方线形状写（订阅/心跳/回贴/主动推送）', () => {
    const src = wc();
    expect(src).toContain('"cmd": WsCmd.SUBSCRIBE');
    expect(src).toContain('"body": {"bot_id": bot_id, "secret": secret}');
    expect(src).toContain('"cmd": WsCmd.HEARTBEAT');
    // 回贴透传回调帧的 req_id，不自己新生成
    expect(src).toContain('"headers": {"req_id": req_id}');
    expect(src).toContain('"msgtype": "stream"');
    expect(src).toContain('"finish": True');
    // req_id 不成立了就改走官方主动推送，而不是假装回贴成功
    expect(src).toContain('REPLY_WINDOW_SECONDS = 24 * 3600');
    expect(src).toContain('WsCmd.SEND_MSG');
    // 没有逐帧回执：回贴本身就是对回调的应答
    expect(src).toContain('企业微信长连接没有这个动作');
  });

  it('等回执与判死的阈值取自官方 SDK，不是猜的', () => {
    const src = wc();
    expect(src).toContain('REPLY_ACK_TIMEOUT_SECONDS = 5.0');
    expect(src).toContain('MAX_MISSED_PONG = 2');
    expect(src).toContain('MAX_REPLY_TARGETS = 64');
    expect(src).toContain('MAX_SEEN_MSGIDS = 256');
    // 缺 errcode 的响应帧按失败算，不给「没说不行就是行」的假成功
    expect(src).toContain('def _errcode_of');
    expect(src).toContain('if _errcode_of(ack) != 0');
  });

  it('会话标识按官方口径：群聊 chatid、单聊 from.userid，且按 msgid 排重', () => {
    const src = wc();
    expect(src).toContain('def _conversation_of');
    expect(src).toContain('body.get("chatid")');
    expect(src).toContain('body.get("from")');
    expect(src).toContain('userid');
    expect(src).toContain('compat_info');
    expect(src).toContain('排重');
  });

  it('一个 BotID 只允许一条连接：被顶掉就不自动重连，界面无「正在重试」话术', () => {
    const src = wc();
    expect(src).toContain('IM_CONNECTION_KICKED');
    expect(src).toContain('retryable=False');
    expect(src).toContain('disconnected_event');
    expect(src).toContain('只允许一条有效长连接');
    // 探测连接会踢掉正在干活的那条，所以校验凭据前先停自己
    expect(src).toContain('await self.runtime.stop()');

    expect(ErrorCode.IM_CONNECTION_KICKED).toBe('IM_CONNECTION_KICKED');
    const text = errorMessageOf(ErrorCode.IM_CONNECTION_KICKED);
    expect(text).toContain('顶掉');
    expect(text).toContain('已停止自动重连');
    expect(text).not.toContain('正在重试');
  });

  it('Mock 开关沿用 SDK 自己的注入点，且引擎侧有本地对拍', () => {
    const src = wc();
    expect(src).toContain('TT_WECOM_WS_URL');
    expect(src).toContain('TT_WECOM_HEARTBEAT_SECONDS');
    const fake = read('../../../tests/integration/fake_wecom.py');
    for (const sym of ['aibot_subscribe', 'aibot_msg_callback', 'aibot_event_callback', 'disconnected_event', 'aibot_respond_msg']) {
      expect(fake, `假网关缺 ${sym}`).toContain(sym);
    }
    const selftest = read('../../../tests/integration/engine_im_wecom_selftest.py');
    expect(selftest).toContain('FakeWeCom');
    expect(selftest).toContain('phase_c()');
  });

  it('依赖声明与随包锁一致（缺 SDK 会让通道退回未接入）', () => {
    expect(read('../../../engine/requirements.txt')).toContain('wecom-aibot-python-sdk==');
    const lock = read('../../../engine/requirements.lock.txt');
    expect(lock).toContain('wecom-aibot-python-sdk==1.0.2');
    // SDK 自己声明的 pyee 事件发射器必须一起进锁：少一个就是导入期缺件
    expect(lock).toContain('pyee==');
    expect(lock).toContain('websockets==');
    expect(read('../../../scripts/prepare-python.sh')).toContain('"aibot"');
  });

  it('凭据只进订阅帧：账号信息里是脱敏 BotID，不写日志不打印', () => {
    const src = wc();
    expect(src).toContain('"botId": _mask(bot_id)');
    expect(src).toContain('def _mask(');
    expect(src).not.toContain('logging');
    expect(src).not.toContain('print(');
  });

  it('缺 SDK 时如实退回未接入，且没有真凭据就不说已打通', () => {
    const src = wc();
    expect(src).toContain('supported = SDK_IMPORT_ERROR == ""');
    expect(src).toContain('def unsupported_reason');
    expect(src).toContain('无法按企业微信长连接协议收发');
    // 这条链路只在本地假网关上对拍过，界面话术不能越界
    expect(src).toContain('现在没有真实企业微信机器人凭据');
    expect(src).toContain('界面不能说');
  });
});

describe('飞书通道：协议事实来自官方 lark-oapi，且整包 SDK 只在绑定时加载', () => {
  const fs = () => feishuChannel();

  it('换地址/帧形状/回执载荷都引 SDK 自己的常量与模型（字段名不手抄）', () => {
    const src = fs();
    for (const sym of [
      'from lark_oapi.ws.enum import FrameType, MessageType',
      'from lark_oapi.ws.model import Response',
      'from lark_oapi.ws.pb.pbbp2_pb2 import Frame',
      'from lark_oapi.core.json import JSON',
      'from lark_oapi.core.const import AUTHORIZATION, USER_AGENT',
      'from lark_oapi.api.im.v1.model.p2_im_message_receive_v1 import P2ImMessageReceiveV1',
      'const.GEN_ENDPOINT_URI',
      '{"AppID": app_id, "AppSecret": app_secret}',
      'const.OK',
      'const.SYSTEM_BUSY',
      'const.EXCEED_CONN_LIMIT',
      'const.HEADER_TYPE',
      'const.HEADER_MESSAGE_ID',
      'const.HEADER_SUM',
      'const.HEADER_SEQ',
      'const.HEADER_BIZ_RT',
      'sdk["const"].SERVICE_ID',
      'sdk["const"].DEVICE_ID',
      'sdk["JSON"].marshal(sdk["Response"](code=200))',
      'EVENT_MESSAGE_RECEIVE = "im.message.receive_v1"',
    ]) {
      expect(src, `飞书通道缺 ${sym}`).toContain(sym);
    }
    // 鉴权类错误码也取自 SDK 常量：飞书换码时这里跟着变，不会留下手抄的旧码
    for (const codeSym of ['c.FORBIDDEN', 'c.AUTH_FAILED', 'c.NO_CREDENTIAL']) {
      expect(src, `飞书通道缺鉴权码 ${codeSym}`).toContain(codeSym);
    }
    // 只借协议，不跑 SDK 的连接循环：否则框架的退避/台账之上会叠出第二套重连
    expect(src).not.toContain('from lark_oapi.ws import Client');
    expect(src).not.toContain('lark_oapi.ws.client');
  });

  it('整包接入、绑定时才导入：模块作用域只探测装没装，慢 import 交给绑定那一次', () => {
    const src = fs();
    expect(src).toContain('from importlib.util import find_spec');
    expect(src).toContain('SDK_PRESENT = find_spec(SDK_PACKAGE) is not None');
    // 启动阶段（模块作用域）不能出现顶层 import：那会把这几十秒算到每个用户头上
    expect(src).not.toContain('\nfrom lark_oapi');
    expect(src).not.toContain('\nimport lark_oapi');
    // 真 import 只在线程池里跑一次，同步路径只认已就绪的缓存
    expect(src).toContain('return await asyncio.to_thread(_load_sdk_sync)');
    expect(src).toContain('def sdk_loaded()');
    expect(src).toContain('飞书通道还没加载完官方 SDK 就收到了帧');
    // 五个网络/收发动作各自预热一次，缺一个就是「没加载完就用」
    expect((src.match(/await load_sdk\(\)/g) ?? []).length).toBeGreaterThanOrEqual(5);
    // 界面按这个事实说话，不给「点了就连上」的错觉
    expect(src).toContain('实测 8~33 秒');
  });

  it('二进制帧的合包与回执阈值照 SDK 的线形状写（半截消息不当事件、不乱回执）', () => {
    const src = fs();
    expect(src).toContain('FRAGMENT_TTL_SECONDS = 5');
    expect(src).toContain('def _combine(');
    expect(src).toContain('if total > 1 and message_id:');
    expect(src).toContain('return None');
    // 没收齐的帧不是事件，也不该回执；控制帧与 card 帧 SDK 同样不回
    expect(src).toContain('if frame.get("kind") != "data" or frame.get("type") != "event"');
    expect(src).toContain('控制帧与 card 帧 SDK 都不回执');
    // 保活是应用层 ping 帧，间隔可能被 pong 里的 ClientConfig 改掉
    expect(src).toContain('def _ping_frame(');
    expect(src).toContain('sdk["MessageType"].PING.value');
    expect(src).toContain('async def on_open(self, ws) -> None');
    expect(src).toContain('async def on_close(self) -> None');
    expect(src).toContain('def _configure(self, conf: dict) -> None');
    expect(src).toContain('DEFAULT_PING_SECONDS = 120.0');
  });

  it('事件正文用官方模型解一层，非文本/机器人自己发的不触发任务', () => {
    const src = fs();
    expect(src).toContain('sdk_loaded()["Event"](body)');
    expect(src).toContain('!= EVENT_MESSAGE_RECEIVE');
    expect(src).toContain('message_type');
    expect(src).toContain('sender_type');
    expect(src).toContain('机器人自己回贴的那条不能触发第二个任务');
    expect(src).toContain('富文本/图片/文件不是任务文本');
    // 飞书在回执没送出去时会重投，同一条事件只能当一次
    expect(src).toContain('def _remember_event');
    expect(src).toContain('MAX_SEEN_EVENTS = 256');
    expect(src).toContain('REPLY_TARGETS = 64');
  });

  it('回贴走 Web API：Bearer 头取自官方 transport 常量，token 提前失效只换发一次', () => {
    const src = fs();
    expect(src).toContain('/open-apis/auth/v3/tenant_access_token/internal');
    expect(src).toContain('/open-apis/im/v1/messages/{message_id}/reply');
    expect(src).toContain('receive_id_type=chat_id');
    // 这里写错过一次（把 token 塞进 User-Agent 头），锁住不让它回来
    expect(src).toContain('sdk["AUTHORIZATION"]: f"Bearer {token}"');
    expect(src).not.toContain('sdk["USER_AGENT"]: f"Bearer');
    expect(src).toContain('TOKEN_REFRESH_MARGIN_SECONDS = 60');
    expect(src).toContain('token 在飞书侧提前失效');
  });

  it('长连接数达上限按不可重试处理：退避重连只会继续占名额', () => {
    const src = fs();
    expect(src).toContain('长连接数已达上限');
    expect(src).toContain('IM_BAD_RESPONSE');
    expect(src).toContain('订阅方式');
    // 事件订阅没选「长连接」时换不到地址，报错文案直接说该改哪里
    expect(src).toContain('把订阅方式改为长连接');
  });

  it('Mock 开关沿用 SDK 自己的注入点，且引擎侧有本地对拍', () => {
    const src = fs();
    expect(src).toContain('TT_FEISHU_DOMAIN');
    expect(src).toContain('TT_FEISHU_PING_INTERVAL');
    const fake = read('../../../tests/integration/fake_feishu.py');
    for (const sym of [
      'ws_const.GEN_ENDPOINT_URI',
      'from lark_oapi.ws.pb.pbbp2_pb2 import Frame',
      '/open-apis/auth/v3/tenant_access_token/internal',
      '/open-apis/im/v1/messages/{message_id}/reply',
      'HEADER_BIZ_RT',
      'im.message.receive_v1',
    ]) {
      expect(fake, `假飞书缺 ${sym}`).toContain(sym);
    }
    const selftest = read('../../../tests/integration/engine_im_feishu_selftest.py');
    expect(selftest).toContain('FakeFeishu');
    expect(selftest).toContain('phase_c()');
    // 启动阶段不加载 SDK 与 aiohttp 会话的循环归属都要各自成段，否则测的是假象
    expect(selftest).toContain('BOOT_HAS_LARK');
    expect(selftest).toContain('phase_c_live()');
  });

  it('依赖声明与随包锁一致（缺 SDK 会让通道退回未接入）', () => {
    expect(read('../../../engine/requirements.txt')).toContain('lark-oapi==');
    const lock = read('../../../engine/requirements.lock.txt');
    expect(lock).toContain('lark-oapi==1.7.5');
    // SDK 自己声明的依赖必须一起进锁：少一个就是导入期缺件
    for (const dep of ['pycryptodome==', 'requests-toolbelt==', 'websockets==']) {
      expect(lock, `随包锁缺 ${dep}`).toContain(dep);
    }
    const prep = read('../../../scripts/prepare-python.sh');
    expect(prep).toContain('"lark_oapi"');
    expect(prep).toContain('feishu_channel.FeishuChannel');
    // 随包冒烟同时量两段：启动侧不加载、运行时加载得动
    expect(prep).toContain('绑定时才加载');
  });

  it('凭据只进请求体：账号信息里是脱敏 App ID，不写日志不打印', () => {
    const src = fs();
    expect(src).toContain('"appId": _mask(app_id)');
    expect(src).toContain('"connectionId": _mask(device_id)');
    expect(src).toContain('def _mask(');
    expect(src).not.toContain('logging');
    expect(src).not.toContain('print(');
    expect(src).toContain('app_secret 只进换取连接地址与 token 的请求体');
  });

  it('缺 SDK 与没有真凭据都如实说，不假装已打通', () => {
    const src = fs();
    expect(src).toContain('def unsupported_reason');
    expect(src).toContain('飞书通道暂不能绑定');
    expect(src).toContain('IM_DEPENDENCY_MISSING');
    expect(src).toContain('现在没有真实飞书应用凭据');
    expect(src).toContain('界面不能说');

    expect(ErrorCode.IM_DEPENDENCY_MISSING).toBe('IM_DEPENDENCY_MISSING');
    const text = errorMessageOf(ErrorCode.IM_DEPENDENCY_MISSING);
    expect(text).toContain('官方 SDK');
    expect(text).toContain('尚未接入');
  });
});

describe('三条渲染层通道（Tauri / 引擎直连 / Mock）接线', () => {
  it('IPC 通道常量齐备', () => {
    expect(IPC_CHANNELS.IM_CHANNELS).toBe('im:channels');
    expect(IPC_CHANNELS.IM_CONFIGURE).toBe('im:configure');
    expect(IPC_CHANNELS.IM_START).toBe('im:start');
    expect(IPC_CHANNELS.IM_STOP).toBe('im:stop');
    expect(IPC_CHANNELS.IM_FORGET).toBe('im:forget');
  });

  it('桌面通道映射到 Rust 命令', () => {
    const src = tauriApi();
    expect(src).toContain("if (channel === 'im:channels') return invoke('im_channels');");
    expect(src).toContain("if (channel === 'im:configure')");
    expect(src).toContain("return invoke('im_configure', {");
    for (const cmd of ['im_start', 'im_stop', 'im_forget']) {
      expect(src, `缺 ${cmd}`).toContain(`invoke('${cmd}'`);
    }
  });

  it('浏览器直连问的是引擎同一个接口', () => {
    const src = engineApi();
    expect(src).toContain("if (channel.startsWith('im:')) return handleEngineImChannel(channel, args);");
    for (const path of ['/api/im/channels', '/api/im/${kind}/config', '/api/im/${kind}/start', '/api/im/${kind}/stop', '/api/im/${kind}/forget']) {
      expect(src, `缺引擎路径 ${path}`).toContain(path);
    }
    // 引擎无响应时报真实码，不返回空数组冒充「没有频道」
    expect(src).toContain('IM_ENGINE_UNREACHABLE');
  });

  it('Mock 通道拒绝而不是造假数据', () => {
    const src = mockApi();
    expect(src).toContain("if (channel.startsWith('im:'))");
    expect(src).toContain('IM_ENGINE_UNAVAILABLE');
    // 抛错而不是 return []：兜底空数组会被界面当成「引擎没有注册频道」
    expect(src).toContain("throw new Error(\n        'IM_ENGINE_UNAVAILABLE");
  });

  it('Rust 侧是引擎代理，且 kind 过路径校验', () => {
    const src = rustIm();
    expect(src).toContain('IM_ENGINE_UNAVAILABLE');
    expect(src).toContain('fn validate_kind');
    // 四个写操作都要先过路径校验，否则 kind 能拼出 `/api/im/../x`
    expect(src.match(/validate_kind\(&kind\)/g)?.length).toBeGreaterThanOrEqual(4);
    expect(src).toContain('/api/im/{kind}/config');
    expect(rustLib()).toContain('bridge::im::im_channels,');
    for (const cmd of ['im_configure', 'im_start', 'im_stop', 'im_forget']) {
      expect(rustLib(), `Rust 未注册 ${cmd}`).toContain(`bridge::im::${cmd},`);
    }
    expect(read('../../../src-tauri/src/bridge/mod.rs')).toContain('pub mod im;');
  });

  it('渲染层封装只转发，不自己保存第二份频道状态', () => {
    const src = alignmentApi();
    for (const fn of ['getImChannels', 'configureImChannel', 'startImChannel', 'stopImChannel', 'forgetImChannel']) {
      expect(src, `缺 ${fn}`).toContain(fn);
    }
    expect(store()).toContain('getImChannels()');
    // store 里没有本地伪造的会话样例
    expect(store()).not.toContain('mockSessions');
  });
});

describe('页面不再保留假承诺', () => {
  it('假二维码与自动注册文案已删除', () => {
    const src = page();
    for (const fake of ['FakeQr', 'QR_DIALOGS', '自动创建并配置机器人', '刷新二维码', '准入策略', '配对模式']) {
      expect(src, `页面仍含 ${fake}`).not.toContain(fake);
    }
  });

  it('Mock 开关式假提示已删除（不再有「开发阶段 Mock」话术）', () => {
    const src = page();
    for (const fake of ['mockEnabled', '已提交', '即将上线', '连接测试通过']) {
      expect(src, `页面仍含 ${fake}`).not.toContain(fake);
    }
  });

  /**
   * 四条已接入通道只在本地假服务端跑过双向对拍，仓库里没有真实企业凭据。
   * 界面不能把「协议实现」说成「真实打通」，飞书首次绑定的 SDK 冷启动也要提前说明。
   */
  it('如实标注真实凭据未验证、飞书冷启动与个人微信未接入', () => {
    const src = page();
    expect(src).toContain('只在本地假服务端做过双向对拍');
    expect(src).toContain('尚未用真实企业凭据做端到端验证');
    expect(src).toContain('8~33 秒');
    expect(src).toContain('个人微信');
  });

  it('清单来自引擎，凭据字段按引擎下发的元数据渲染', () => {
    const src = page();
    expect(src).toContain('useImChannelStore');
    expect(src).toContain('const STATE_META: Record<ImChannelState,');
    expect(src).toContain('openChannel.fields.map');
    expect(src).toContain('type="password"');
    expect(src).toContain('openChannel.setupUrl');
    expect(src).toContain('c.unsupportedReason');
    // 引擎下发的账号事实都要有中文名，别把 wsUrl 这类协议术语原样丢给用户
    expect(src).toContain("botId: 'BotID（脱敏）'");
    expect(src).toContain("wsUrl: '长连接地址'");
    expect(src).toContain("oneConnectionPerBot: '单连接限制'");
    expect(src).toContain("subscribeEvent: '订阅事件'");
    expect(src).toContain("connectionId: '连接标识（脱敏）'");
    expect(src).toContain("pingIntervalSeconds: '保活间隔秒'");
    // Slack 的 app_id 本来就是公开标识，标签不能替飞书说「脱敏」
    expect(src).toContain("appId: 'App ID',");
    expect(src).not.toContain("appId: 'App',");
    // 界面不再自己列平台清单
    expect(src).not.toContain('const CHANNELS: ChannelDef[]');
  });

  it('每个控件都绑真实动作，状态/重连/错误/账号事实都展示', () => {
    const src = page();
    expect(src).toContain('configureImChannel(');
    expect(src).toContain('startImChannel(kind)');
    expect(src).toContain('stopImChannel(kind)');
    expect(src).toContain('forgetImChannel(kind)');
    expect(src).toContain('loadSessions()');
    expect(src).toContain('c.reconnects');
    expect(src).toContain('c.lastError');
    expect(src).toContain('c.connectedAtMs');
    expect(src).toContain('Object.entries(c.account)');
    expect(src).toContain('c.sessions');
    // 引擎不可用时如实报错并可重试，不是静默空列表
    expect(src).toContain('errorTextOf(error');
    expect(src).toContain('engineDown');
  });

  /**
   * 实机验证踩到的坑：原先只在过渡态轮询，频道进入 connected 之后从 IM 发来的消息
   * 不会出现在「IM 发起的会话」里，界面看着就像「连上了但收不到东西」。
   * 刷新点同时从页面挪到常驻的侧边栏——两处各自 setInterval 会让同一个接口每 4s 被问两遍。
   */
  it('已连接后继续问引擎，且刷新点只有一个（常驻侧边栏）', () => {
    const nav = sidebar();
    expect(nav).toContain("['connected', 'connecting', 'reconnecting'].includes(c.state)");
    expect(nav).toContain('window.setInterval(() => void loadSessions(), 4000)');
    expect(nav).toContain('window.clearInterval(timer)');

    const src = page();
    expect(src).not.toContain('setInterval');
    // 进页面先问一次，不等下一个轮询周期
    expect(src).toContain('void loadSessions();');
  });

  it('凭据只写不读：界面不回填明文，也不展示接口返回的 secret 值', () => {
    const src = page();
    expect(src).toContain('f.present');
    expect(src).toContain('留空即沿用已登记的值');
    // 引擎视图侧本来就不含凭据值
    expect(imFramework()).toContain('def view(self, present: bool) -> dict');
    expect(rustIm()).toContain('IM_ENGINE_UNAVAILABLE');
  });
});

/**
 * 「在 IM 频道中展示工具调用执行过程」原先是个纯装饰开关：
 * 只把布尔写进 settings_kv，全工程没有任何读取点，IM 回贴永远只有最终回复。
 * 现在这个偏好由引擎持有并消费（回贴正文是引擎拼的），设置页读写的都是同一个真值。
 */
describe('执行过程偏好：引擎持有、引擎消费', () => {
  it('引擎有一份带出厂默认的偏好，且只认已知布尔键', () => {
    const src = imFramework();
    expect(src).toContain('PREFERENCE_DEFAULTS: dict = {"showToolProcess": False}');
    expect(src).toContain('def load_preferences');
    expect(src).toContain('def save_preferences');
    expect(src).toContain('"im_preferences.json"');
    expect(src).toContain('IM_PREFERENCE_INVALID');
    // 未知键与非布尔值都不写盘（否则设置页显示的开关和生效的开关是两件事）
    expect(src).toContain('未知偏好项');
    expect(src).toContain('只接受 true/false');
  });

  it('回贴正文按偏好决定带不带执行过程，步骤来自执行流真实事件', () => {
    const src = server();
    expect(src).toContain('im_channels.load_preferences().get("showToolProcess")');
    expect(src).toContain('def _im_reply_of(final_text: str, artifacts: list, error: str, steps: list | None = None)');
    expect(src).toContain('"tool": tool_name');
    expect(src).toContain('tool_steps.append(name)');
    expect(src).toContain('_im_reply_of(final_text, artifacts, error, tool_steps)');
    expect(src).toContain('@app.get("/api/im/preferences")');
    expect(src).toContain('@app.post("/api/im/preferences")');
  });

  it('通道常量、Rust 命令、三通道接线齐备', () => {
    expect(IPC_CHANNELS.IM_PREFERENCES_GET).toBe('im:preferencesGet');
    expect(IPC_CHANNELS.IM_PREFERENCES_SET).toBe('im:preferencesSet');
    expect(IPC_CHANNELS.IM_PREFERENCES_GET.slice(0, 3)).toBe('im:');

    const rust = rustIm();
    expect(rust).toContain('pub async fn im_preferences_get');
    expect(rust).toContain('pub async fn im_preferences_set');
    expect(rust).toContain('fn validate_preferences');
    expect(rust).toContain('"/api/im/preferences"');
    expect(rustLib()).toContain('bridge::im::im_preferences_get,');
    expect(rustLib()).toContain('bridge::im::im_preferences_set,');

    expect(tauriApi()).toContain("if (channel === 'im:preferencesGet') return invoke('im_preferences_get');");
    expect(tauriApi()).toContain("if (channel === 'im:preferencesSet')");
    expect(engineApi()).toContain("case 'im:preferencesGet':");
    expect(engineApi()).toContain("case 'im:preferencesSet':");

    const api = alignmentApi();
    expect(api).toContain('export async function getImPreferences');
    expect(api).toContain('export async function setImPreferences');
    expect(api).toContain('IPC_CHANNELS.IM_PREFERENCES_GET');
    expect(api).toContain('IPC_CHANNELS.IM_PREFERENCES_SET');
    // 不在渲染层另存一份（localStorage 兜底会让开关显示成「界面以为的状态」）
    expect(api).not.toContain('tt:imPreferences');
  });

  it('错误码进契约且有用户文案', () => {
    expect(ErrorCode.IM_PREFERENCE_INVALID).toBe('IM_PREFERENCE_INVALID');
    expect(errorMessageOf(ErrorCode.IM_PREFERENCE_INVALID)).not.toBe(
      `发生错误（${ErrorCode.IM_PREFERENCE_INVALID}）`,
    );
  });

  it('设置页开关绑的是引擎真值，旧的死键不再出现', () => {
    const src = settingsPage();
    expect(src).toContain('getImPreferences()');
    expect(src).toContain('setImPreferences({ showToolProcess: enabled })');
    expect(src).toContain('disabled={!prefs || saving}');
    expect(src).toContain("errorTextOf(e, tr('IM 频道偏好读取失败'))");
    expect(src).toContain('正在读取引擎里的 IM 频道偏好…');
    // 那个只写库没人读的布尔已彻底移除
    expect(src).not.toContain('imShowToolProcess');
    expect(read('../../../src/shared/contracts.ts')).not.toContain('imShowToolProcess');
  });
});
