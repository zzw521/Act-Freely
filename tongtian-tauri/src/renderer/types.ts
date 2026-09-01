/**
 * 通天办公 - 渲染层共享类型导出
 * 
 * 从 shared/types.ts 重新导出到 renderer 根目录，方便导入
 */

export {
  // 状态枚举
  TaskStatus,
  PlanStatus,
  StepStatus,
  StepType,
  Role,
  ModelSource,
  FileOpType,
  FileOpStatus,
  DataSensitivity,
  // 核心接口
  type Task,
  type Message,
  type FileOp,
  type Plan,
  type Step,
  // API/事件类型
  type ChatEvent,
  // 模型类型
  type ModelConfig,
  type ModelTestReq,
  type ModelTestResp,
  // 安全类型
  type SandboxPolicy,
  type AuditLogEntry,
} from '../shared/types';
