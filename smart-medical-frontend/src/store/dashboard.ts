import { defineStore } from 'pinia'
import { ErrorCode, defaultMessage, isAuthError, isRetryableError, isServerError } from '../api/errorCodes'

import type {
  AgeGenderStat,
  AggregationRequest,
  AggregationResponse,
  ApiMetaItem,
  DashboardPayload,
  FilterState,
  ScreenData,
} from '../types/dashboard'

interface DashboardState {
  payload: DashboardPayload | null
  loading: boolean
  error: string | null
  errorCode: ErrorCode | null
  apiBaseUrl: string
  apiAvailable: boolean | null
  apiMessage: string
  dimensions: ApiMetaItem[]
  metrics: ApiMetaItem[]
  algorithms: ApiMetaItem[]
  latestAggregation: AggregationResponse | null
  latestAggregationError: string | null
  latestAggregationErrorCode: ErrorCode | null
  screenData: ScreenData | null
  screenLoading: boolean
  screenError: string | null
  screenErrorCode: ErrorCode | null
  /** 年龄 × 性别交叉分布（总览页性别切换柱状图；离线时为 null） */
  ageGenderDistribution: AgeGenderStat[] | null
  /** 筛选下拉全量选项（疾病/医院，按名称排序；在线聚合 / 离线静态） */
  filterDiseases: string[]
  filterHospitals: string[]
  /** LLM 实时监测模式：online=DeepSeek 在线 API / local=本地模型；null=未知或后端不可达 */
  llmMode: 'online' | 'local' | null
  llmProvider: string
  llmSwitching: boolean
  llmSwitchError: string | null
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)

  // 尝试解析响应体获取业务错误码
  let errorCode: ErrorCode | null = null
  let errorMessage: string | null = null
  let responseData: unknown = null

  if (response.headers.get('content-type')?.includes('application/json')) {
    try {
      responseData = await response.json()
      // 后端标准响应格式：{ code, message, data, query_time, trace_id }
      if (responseData && typeof responseData === 'object' && 'code' in responseData) {
        const body = responseData as { code: number; message?: string; data?: unknown }
        errorCode = body.code as ErrorCode
        errorMessage = body.message ?? null
      }
    } catch {
      // 忽略解析错误
    }
  }

  if (!response.ok) {
    const statusText = response.statusText
    const message = errorMessage ?? `请求失败：${response.status} ${statusText}`
    const err = new Error(message) as Error & { code?: ErrorCode; retryAfter?: number }
    err.code = errorCode ?? (response.status as ErrorCode)

    // 从 Retry-After 头获取重试等待时间
    const retryAfterHeader = response.headers.get('Retry-After')
    if (retryAfterHeader) {
      err.retryAfter = parseInt(retryAfterHeader, 10)
    }

    throw err
  }

  // 即使 HTTP 200，业务码非 200 也视为错误
  if (errorCode !== null && errorCode !== ErrorCode.OK) {
    const message = errorMessage ?? defaultMessage(errorCode)
    const err = new Error(message) as Error & { code?: ErrorCode; retryAfter?: number }
    err.code = errorCode
    throw err
  }

  return responseData as T
}

/** 大屏筛选结果缓存：相同筛选条件的重复查询直接复用（不重复打后端），LRU 上限 8 条 */
const screenQueryCache = new Map<string, ScreenData>()
const SCREEN_QUERY_CACHE_MAX = 8

function screenCacheKey(filters: FilterState): string {
  return JSON.stringify({
    d: [...filters.disease].sort(),
    a: filters.age,
    h: [...filters.hospital].sort(),
    y: filters.year,
  })
}

export const useDashboardStore = defineStore('dashboard', {
  state: (): DashboardState => ({
    payload: null,
    loading: false,
    error: null,
    errorCode: null,
    // 走相对路径，由 Vite 开发代理转发到后端（见 vite.config.ts）；
    // 生产环境可通过环境变量 VITE_API_BASE_URL 指定同源网关地址
    apiBaseUrl: import.meta.env.VITE_API_BASE_URL || '/api/v1',
    apiAvailable: null,
    apiMessage: '尚未检测在线分析服务',
    dimensions: [],
    metrics: [],
    algorithms: [],
    latestAggregation: null,
    latestAggregationError: null,
    latestAggregationErrorCode: null,
    screenData: null,
    screenLoading: false,
    screenError: null,
    screenErrorCode: null,
    ageGenderDistribution: null,
    filterDiseases: [],
    filterHospitals: [],
    llmMode: null,
    llmProvider: '',
    llmSwitching: false,
    llmSwitchError: null,
  }),
  getters: {
    isReady: (state) => Boolean(state.payload),
    noteList: (state) => state.payload?.meta.notes ?? [],
    endpointList: (state) => state.payload?.api.endpoints ?? [],
    /** 是否有认证错误 (401/403) */
    hasAuthError: (state) =>
      isAuthError(state.errorCode ?? 0) ||
      isAuthError(state.latestAggregationErrorCode ?? 0) ||
      isAuthError(state.screenErrorCode ?? 0),
    /** 是否有可重试错误 (429/503/504) */
    hasRetryableError: (state) =>
      isRetryableError(state.errorCode ?? 0) ||
      isRetryableError(state.latestAggregationErrorCode ?? 0) ||
      isRetryableError(state.screenErrorCode ?? 0),
    /** 是否有服务端错误 (5xx) */
    hasServerError: (state) =>
      isServerError(state.errorCode ?? 0) ||
      isServerError(state.latestAggregationErrorCode ?? 0) ||
      isServerError(state.screenErrorCode ?? 0),
  },
  actions: {
    async init() {
      if (this.payload || this.loading) {
        return
      }
      await this.loadStaticPayload()
    },
    async loadStaticPayload() {
      this.loading = true
      this.error = null
      this.errorCode = null
      try {
        const payload = await fetchJson<DashboardPayload>('/data/dashboard-data.json')
        this.payload = payload
        // apiBaseUrl 保持相对路径（经 Vite 代理 / Nginx 转发），
        // 不再覆盖为静态 JSON 里的绝对地址，避免跨域被浏览器拦截
      } catch (error) {
        const err = error as Error & { code?: ErrorCode }
        this.error = err.message
        this.errorCode = err.code ?? null
      } finally {
        this.loading = false
      }
    },
    async pingApi() {
      try {
        const result = await fetchJson<{ data: { status: string; data: { row_count: number } } }>(
          `${this.apiBaseUrl}/health`,
        )
        this.apiAvailable = result.data.status === 'ok'
        this.apiMessage = this.apiAvailable
          ? `实时分析服务可用，数据行数 ${(result.data.data.row_count ?? 0).toLocaleString()}`
          : '实时分析服务返回异常状态'
      } catch (error) {
        const err = error as Error & { code?: ErrorCode }
        this.apiAvailable = false
        this.apiMessage =
          err.code !== undefined
            ? `${defaultMessage(err.code)} (${err.code})`
            : `实时分析服务不可用：${err.message}`
      }
    },
    async loadApiMeta() {
      if (!this.apiAvailable) {
        return
      }

      try {
        const [dimensionResult, metricResult, algorithmResult] = await Promise.all([
          fetchJson<{ data: ApiMetaItem[] }>(`${this.apiBaseUrl}/meta/dimensions`),
          fetchJson<{ data: ApiMetaItem[] }>(`${this.apiBaseUrl}/meta/metrics`),
          fetchJson<{ data: ApiMetaItem[] }>(`${this.apiBaseUrl}/meta/algorithms`),
        ])

        this.dimensions = dimensionResult.data
        this.metrics = metricResult.data
        this.algorithms = algorithmResult.data
        this.apiMessage = `已同步 ${this.dimensions.length} 个维度、${this.metrics.length} 个指标和 ${this.algorithms.length} 个算法`
      } catch (error) {
        const err = error as Error & { code?: ErrorCode }
        this.apiMessage =
          err.code !== undefined
            ? `元数据读取失败：${defaultMessage(err.code)} (${err.code})`
            : `元数据读取失败：${err.message}`
      }
    },
    async runAggregation(request: AggregationRequest) {
      this.latestAggregationError = null
      this.latestAggregationErrorCode = null
      this.latestAggregation = null

      try {
        const response = await fetchJson<{
          data: AggregationResponse
        }>(`${this.apiBaseUrl}/aggregations/run`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
          },
          body: JSON.stringify(request),
        })
        this.latestAggregation = response.data
      } catch (error) {
        const err = error as Error & { code?: ErrorCode; retryAfter?: number }
        this.latestAggregationError =
          err.code !== undefined
            ? `${defaultMessage(err.code)} (${err.code})`
            : `在线聚合查询失败：${err.message}`
        this.latestAggregationErrorCode = err.code ?? null

        // 如果是可重试错误，在错误消息中提示重试
        const code = err.code
        if (code !== undefined && code !== null && isRetryableError(code) && err.retryAfter) {
          this.latestAggregationError += `，建议 ${err.retryAfter}s 后重试`
        }
      }
    },

    /** 大屏实时筛选：把 UI 筛选条件翻译成后端批量聚合请求（一次请求 10 个子查询），
     *  并组装 ScreenData。相同筛选条件的重复请求命中前端缓存直接返回。 */
    async runScreenQuery(filters: FilterState) {
      this.screenLoading = true
      this.screenError = null
      this.screenErrorCode = null

      // 相同筛选条件的重复查询直接复用已取回的结果
      const cacheKey = screenCacheKey(filters)
      const cached = screenQueryCache.get(cacheKey)
      if (cached) {
        this.screenData = cached
        this.screenLoading = false
        return
      }

      // 界面筛选条件 -> 后端过滤条件（维度字段见后端注册表）
      const conditions: Array<{
        field: string
        op: string
        value?: string | number
        values?: Array<string | number>
      }> = []
      if (filters.disease.length > 0) {
        conditions.push({ field: 'ccsr_diagnosis_description', op: 'in', values: filters.disease })
      }
      if (filters.age && filters.age !== '全部年龄') {
        conditions.push({ field: 'age_group', op: 'eq', value: filters.age })
      }
      if (filters.hospital.length > 0) {
        conditions.push({ field: 'facility_name', op: 'in', values: filters.hospital })
      }
      if (filters.year) {
        conditions.push({ field: 'discharge_year', op: 'eq', value: Number(filters.year) })
      }

      // 全部子查询一次性提交：后端共享同一份过滤后的 DataFrame（Spark 缓存复用），
      // 从“10 次并发 Spark 作业 + 10 次过滤”降为“1 次过滤 + 10 个分组聚合”
      const q = (id: string, dimensions: string[], metrics: string[], extra: Record<string, unknown> = {}) => ({
        id,
        dimensions,
        metrics,
        ...extra,
      })
      const queries = [
        q('kpi', ['discharge_year'], ['discharge_count', 'avg_length_of_stay', 'avg_total_charges', 'avg_total_costs']),
        q('age', ['age_group'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 10 }),
        q('admission', ['type_of_admission'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 10 }),
        q('payment', ['payment_typology_1'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 6 }),
        q('county', ['hospital_county'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 70 }),
        q('severity', ['apr_severity_of_illness_description'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 10 }),
        q('diagnoses', ['ccsr_diagnosis_description'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 8 }),
        q('emergency', ['emergency_department_indicator'], ['discharge_count'], { limit: 2 }),
        // 可视化进阶：全部医院（含所在县与邮编前缀，用于地图点位精确定位）
        // limit 与静态 topFacilities 一致（Top50），避免筛选前后点位数量突增/拥挤
        q('hospital3d', ['facility_name', 'hospital_county', 'zip_code_3_digits'], ['discharge_count', 'avg_total_charges'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 50 }),
        // 主要诊断类别 MDC 排行（底部图表，随筛选联动）
        q('mdc', ['apr_mdc_description'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 10 }),
      ]

      try {
        const response = await fetchJson<{
          data: { results: Record<string, AggregationResponse> }
        }>(`${this.apiBaseUrl}/aggregations/batch`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ filters: conditions, queries }),
        })

        const res = response.data.results
        const kpiRow = res.kpi?.rows[0] ?? {}
        const total = Number(kpiRow.discharge_count ?? 0)
        const emergencyRow = res.emergency?.rows.find((r) => r.emergency_department_indicator === 'Y')
        const emergencyCount = Number(emergencyRow?.discharge_count ?? 0)

        const data: ScreenData = {
          dischargeCount: total,
          avgLengthOfStay: (kpiRow.avg_length_of_stay as number | null) ?? null,
          avgTotalCharges: (kpiRow.avg_total_charges as number | null) ?? null,
          avgTotalCosts: (kpiRow.avg_total_costs as number | null) ?? null,
          emergencyRate: total > 0 ? Math.round((emergencyCount / total) * 10000) / 100 : null,
          admissionDistribution: (res.admission?.rows ?? []).map((r) => ({
            name: String(r.type_of_admission ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          paymentDistribution: (res.payment?.rows ?? []).map((r) => ({
            name: String(r.payment_typology_1 ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          ageDistribution: (res.age?.rows ?? []).map((r) => ({
            name: String(r.age_group ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          topCounties: (res.county?.rows ?? []).map((r) => ({
            name: String(r.hospital_county ?? 'Unknown'),
            dischargeCount: Number(r.discharge_count ?? 0),
          })),
          severityDistribution: (res.severity?.rows ?? []).map((r) => ({
            name: String(r.apr_severity_of_illness_description ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          topDiagnoses: (res.diagnoses?.rows ?? []).map((r) => ({
            name: String(r.ccsr_diagnosis_description ?? 'Unknown'),
            dischargeCount: Number(r.discharge_count ?? 0),
          })),
          mdcDistribution: (res.mdc?.rows ?? []).map((r) => ({
            name: String(r.apr_mdc_description ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          hospital3d: (res.hospital3d?.rows ?? []).map((r) => ({
            name: String(r.facility_name ?? 'Unknown'),
            county: String(r.hospital_county ?? 'Unknown'),
            zip3: r.zip_code_3_digits == null ? '' : String(r.zip_code_3_digits),
            dischargeCount: Number(r.discharge_count ?? 0),
            avgCharges: r.avg_total_charges == null ? null : Number(r.avg_total_charges),
          })),
          computedAt: new Date().toLocaleTimeString(),
        }

        this.screenData = data
        // 写入前端缓存（LRU：超出上限淘汰最早插入的一条）
        screenQueryCache.set(cacheKey, data)
        if (screenQueryCache.size > SCREEN_QUERY_CACHE_MAX) {
          const oldest = screenQueryCache.keys().next().value
          if (oldest !== undefined) {
            screenQueryCache.delete(oldest)
          }
        }
      } catch (error) {
        const err = error as Error & { code?: ErrorCode; retryAfter?: number }
        this.screenError =
          err.code !== undefined
            ? `${defaultMessage(err.code)} (${err.code})`
            : `在线聚合查询失败：${err.message}`
        this.screenErrorCode = err.code ?? null

        // 如果是可重试错误，在错误消息中提示重试
        const code = err.code
        if (code !== undefined && code !== null && isRetryableError(code) && err.retryAfter) {
          this.screenError += `，建议 ${err.retryAfter}s 后重试`
        }

        // 认证错误特殊处理提示
        if (code !== undefined && code !== null && isAuthError(code)) {
          this.screenError += '，请重新登录或检查权限'
        }

        this.screenData = null
      } finally {
        this.screenLoading = false
      }
    },

    /** 总览页：加载年龄 × 性别交叉分布（在线聚合；离线时保持 null 由页面降级为全量） */
    async loadAgeGenderDistribution() {
      if (!this.apiAvailable) {
        this.ageGenderDistribution = null
        return
      }
      try {
        const response = await fetchJson<{ data: AggregationResponse }>(
          `${this.apiBaseUrl}/aggregations/run`,
          {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              dimensions: ['age_group', 'gender'],
              metrics: ['discharge_count'],
              limit: 50,
            }),
          },
        )
        this.ageGenderDistribution = response.data.rows.map((row) => ({
          age: String(row.age_group ?? 'Unknown'),
          gender: String(row.gender ?? 'Unknown'),
          value: Number(row.discharge_count ?? 0),
        }))
      } catch (error) {
        // 聚合失败不影响页面：总览页降级为仅展示全量年龄分布
        this.ageGenderDistribution = null
      }
    },

    /** 读取当前 LLM 实时监测模式（online=在线API / local=本地模型） */
    async fetchLlmMode() {
      if (!this.apiAvailable) {
        this.llmMode = null
        this.llmProvider = ''
        return
      }
      try {
        const result = await fetchJson<{
          data: { mode: 'online' | 'local'; llm: { provider?: string; model?: string } }
        }>(`${this.apiBaseUrl}/ai/provider`)
        this.llmMode = result.data.mode
        this.llmProvider = result.data.llm?.provider ?? ''
      } catch {
        this.llmMode = null
        this.llmProvider = ''
      }
    },

    /** 热切换 LLM 模式（无需重启后端），切换失败保留原模式并记录错误 */
    async switchLlmMode() {
      if (!this.apiAvailable || this.llmSwitching) {
        return
      }
      const target = this.llmMode === 'local' ? 'online' : 'local'
      this.llmSwitching = true
      this.llmSwitchError = null
      try {
        const result = await fetchJson<{
          data: { mode: 'online' | 'local'; llm: { provider?: string; model?: string } }
        }>(`${this.apiBaseUrl}/ai/provider`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ mode: target }),
        })
        this.llmMode = result.data.mode
        this.llmProvider = result.data.llm?.provider ?? ''
      } catch (error) {
        const err = error as Error & { code?: ErrorCode }
        this.llmSwitchError =
          err.code !== undefined
            ? `${defaultMessage(err.code)} (${err.code})`
            : `LLM 模式切换失败：${err.message}`
      } finally {
        this.llmSwitching = false
      }
    },

    /** 大屏筛选下拉全量选项：在线聚合全部疾病/医院（按名称排序）；离线回退静态列表 */
    async loadFilterOptions() {
      const sortByName = (a: string, b: string) => a.localeCompare(b, 'en', { sensitivity: 'base' })
      if (!this.apiAvailable) {
        this.filterDiseases = [...(this.payload?.filterOptions?.diseases ?? [])].sort(sortByName)
        this.filterHospitals = [...(this.payload?.filterOptions?.hospitals ?? [])].sort(sortByName)
        return
      }
      try {
        // 一次批量请求同时取疾病/医院全量选项（后端共享同一份全表缓存，减少 Spark 作业数）
        const response = await fetchJson<{
          data: { results: Record<string, AggregationResponse> }
        }>(`${this.apiBaseUrl}/aggregations/batch`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            filters: [],
            queries: [
              {
                id: 'diseases',
                dimensions: ['ccsr_diagnosis_description'],
                metrics: ['discharge_count'],
                limit: 1000,
              },
              {
                id: 'hospitals',
                dimensions: ['facility_name'],
                metrics: ['discharge_count'],
                limit: 1000,
              },
            ],
          }),
        })
        const d = response.data.results.diseases
        const h = response.data.results.hospitals
        this.filterDiseases = (d?.rows ?? [])
          .map((r) => String(r.ccsr_diagnosis_description ?? ''))
          .filter(Boolean)
          .sort(sortByName)
        this.filterHospitals = (h?.rows ?? [])
          .map((r) => String(r.facility_name ?? ''))
          .filter(Boolean)
          .sort(sortByName)
      } catch {
        this.filterDiseases = [...(this.payload?.filterOptions?.diseases ?? [])].sort(sortByName)
        this.filterHospitals = [...(this.payload?.filterOptions?.hospitals ?? [])].sort(sortByName)
      }
    },
  },
})
