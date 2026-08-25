/**
 * 统一错误码常量（与后端 app/core/error_codes.py ErrorCode 完全对齐）
 *
 * 约定：
 * - 2xx 成功
 * - 4xx 客户端错误
 * - 5xx 服务端错误
 * - HTTP 状态码与响应体 code 保持一致
 */

// 使用 const object 而非 enum，兼容 erasableSyntaxOnly
export const ErrorCode = {
  // ---- 2xx 成功 ----
  OK: 200,

  // ---- 4xx 客户端错误 ----
  BAD_REQUEST: 400,              // 请求体缺失/JSON 格式错误
  PARAM_VALIDATION_ERROR: 400,   // 参数结构校验失败 (marshmallow)
  INVALID_DIMENSION: 400,        // 维度不在注册表
  INVALID_METRIC: 400,           // 指标不在注册表
  INVALID_FILTER: 400,           // 过滤条件非法
  UNAUTHORIZED: 401,             // 未认证或凭证无效
  FORBIDDEN: 403,                // 无权限访问该资源
  NOT_FOUND: 404,                // 路由不存在
  ALGORITHM_NOT_FOUND: 404,      // 算法未注册
  METHOD_NOT_ALLOWED: 405,       // 请求方法不被允许
  REQUEST_TIMEOUT: 408,          // 请求处理超时
  CONFLICT: 409,                 // 资源版本冲突 (CAS/乐观锁、并发快照等)
  REQUEST_ENTITY_TOO_LARGE: 413, // 请求体过大 (上限 2MB)
  PAYLOAD_TOO_LARGE: 413,        // 旧名兼容别名
  UNSUPPORTED_MEDIA_TYPE: 415,   // Content-Type 必须为 application/json
  UNPROCESSABLE_ENTITY: 422,     // 语义校验失败
  TOO_MANY_REQUESTS: 429,        // 请求过于频繁，请稍后重试

  // ---- 5xx 服务端错误 ----
  INTERNAL_ERROR: 500,           // 未预期异常
  COMPUTATION_ERROR: 500,        // Spark/算法计算失败
  SERVICE_UNAVAILABLE: 503,      // 数据未加载/依赖不可用
  GATEWAY_TIMEOUT: 504,          // 计算超时
} as const;

/** 错误码类型 */
export type ErrorCode = (typeof ErrorCode)[keyof typeof ErrorCode];

/** 后端默认错误消息映射（与后端 DEFAULT_MESSAGES 对齐） */
export const DEFAULT_MESSAGES: Record<number, string> = {
  200: 'OK',
  400: '请求格式错误',           // BAD_REQUEST / PARAM_VALIDATION_ERROR / INVALID_DIMENSION / INVALID_METRIC / INVALID_FILTER
  401: '未认证或凭证无效',
  403: '无权限访问该资源',
  404: '资源不存在',             // NOT_FOUND / ALGORITHM_NOT_FOUND
  405: '请求方法不被允许',
  408: '请求处理超时',
  409: '资源状态冲突，请刷新后重试',
  413: '请求体过大（上限 2MB）',
  415: 'Content-Type 必须为 application/json',
  422: '语义校验失败',
  429: '请求过于频繁，请稍后重试',
  500: '服务内部错误',           // INTERNAL_ERROR / COMPUTATION_ERROR
  503: '分析服务暂不可用（数据未就绪）',
  504: '聚合计算超时，请缩小查询范围后重试',
};

/** 获取错误码对应的默认消息 */
export function defaultMessage(code: ErrorCode | number): string {
  return DEFAULT_MESSAGES[code] ?? '未知错误';
}

/** 判断是否为成功码 */
export function isSuccess(code: ErrorCode | number): boolean {
  return code >= 200 && code < 300;
}

/** 判断是否为客户端错误 */
export function isClientError(code: ErrorCode | number): boolean {
  return code >= 400 && code < 500;
}

/** 判断是否为服务端错误 */
export function isServerError(code: ErrorCode | number): boolean {
  return code >= 500 && code < 600;
}

/** 判断是否为可重试错误 (429, 503, 504) */
export function isRetryableError(code: ErrorCode | number): boolean {
  return code === ErrorCode.TOO_MANY_REQUESTS
    || code === ErrorCode.SERVICE_UNAVAILABLE
    || code === ErrorCode.GATEWAY_TIMEOUT;
}

/** 判断是否为认证相关错误 (401, 403) */
export function isAuthError(code: ErrorCode | number): boolean {
  return code === ErrorCode.UNAUTHORIZED || code === ErrorCode.FORBIDDEN;
}