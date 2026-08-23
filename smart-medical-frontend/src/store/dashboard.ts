import { defineStore } from 'pinia'

import type {
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
  apiBaseUrl: string
  apiAvailable: boolean | null
  apiMessage: string
  dimensions: ApiMetaItem[]
  metrics: ApiMetaItem[]
  algorithms: ApiMetaItem[]
  latestAggregation: AggregationResponse | null
  latestAggregationError: string | null
  screenData: ScreenData | null
  screenLoading: boolean
  screenError: string | null
}

async function fetchJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init)
  if (!response.ok) {
    throw new Error(`请求失败：${response.status} ${response.statusText}`)
  }
  return response.json() as Promise<T>
}

export const useDashboardStore = defineStore('dashboard', {
  state: (): DashboardState => ({
    payload: null,
    loading: false,
    error: null,
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
    screenData: null,
    screenLoading: false,
    screenError: null,
  }),
  getters: {
    isReady: (state) => Boolean(state.payload),
    noteList: (state) => state.payload?.meta.notes ?? [],
    endpointList: (state) => state.payload?.api.endpoints ?? [],
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
      try {
        const payload = await fetchJson<DashboardPayload>('/data/dashboard-data.json')
        this.payload = payload
        // apiBaseUrl 保持相对路径（经 Vite 代理 / Nginx 转发），
        // 不再覆盖为静态 JSON 里的绝对地址，避免跨域被浏览器拦截
      } catch (error) {
        this.error = error instanceof Error ? error.message : '静态数据加载失败'
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
        this.apiAvailable = false
        this.apiMessage =
          error instanceof Error ? `实时分析服务不可用：${error.message}` : '实时分析服务不可用'
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
        this.apiMessage =
          error instanceof Error ? `元数据读取失败：${error.message}` : '元数据读取失败'
      }
    },
    async runAggregation(request: AggregationRequest) {
      this.latestAggregationError = null
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
        this.latestAggregationError =
          error instanceof Error ? error.message : '在线聚合查询失败'
      }
    },

    /** 大屏实时筛选：把 UI 筛选条件翻译成后端聚合请求，并行取数并组装 ScreenData */
    async runScreenQuery(filters: FilterState) {
      this.screenLoading = true
      this.screenError = null

      // UI 筛选条件 -> 后端过滤条件（维度 key 见后端 config/registry.py）
      const conditions: Array<{ field: string; op: string; value: string | number }> = []
      if (filters.disease && filters.disease !== '全部疾病') {
        conditions.push({ field: 'ccsr_diagnosis_description', op: 'eq', value: filters.disease })
      }
      if (filters.age && filters.age !== '全部年龄') {
        conditions.push({ field: 'age_group', op: 'eq', value: filters.age })
      }
      if (filters.hospital && filters.hospital !== '全部医院') {
        conditions.push({ field: 'facility_name', op: 'eq', value: filters.hospital })
      }
      if (filters.year) {
        conditions.push({ field: 'discharge_year', op: 'eq', value: Number(filters.year) })
      }

      const run = (dimensions: string[], metrics: string[], extra: Record<string, unknown> = {}) =>
        fetchJson<{ data: AggregationResponse }>(`${this.apiBaseUrl}/aggregations/run`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            dimensions,
            metrics,
            filters: conditions,
            limit: 100,
            ...extra,
          }),
        })

      try {
        const [kpi, admission, payment, county, severity, diagnoses, emergency] = await Promise.all([
          run(['discharge_year'], ['discharge_count', 'avg_length_of_stay', 'avg_total_charges', 'avg_total_costs']),
          run(['type_of_admission'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 10 }),
          run(['payment_typology_1'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 6 }),
          run(['hospital_county'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 8 }),
          run(['apr_severity_of_illness_description'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 10 }),
          run(['ccsr_diagnosis_description'], ['discharge_count'], { sort: [{ field: 'discharge_count', order: 'desc' }], limit: 8 }),
          run(['emergency_department_indicator'], ['discharge_count'], { limit: 2 }),
        ])

        const kpiRow = kpi.data.rows[0] ?? {}
        const total = Number(kpiRow.discharge_count ?? 0)
        const emergencyRow = emergency.data.rows.find((r) => r.emergency_department_indicator === 'Y')
        const emergencyCount = Number(emergencyRow?.discharge_count ?? 0)

        this.screenData = {
          dischargeCount: total,
          avgLengthOfStay: (kpiRow.avg_length_of_stay as number | null) ?? null,
          avgTotalCharges: (kpiRow.avg_total_charges as number | null) ?? null,
          avgTotalCosts: (kpiRow.avg_total_costs as number | null) ?? null,
          emergencyRate: total > 0 ? Math.round((emergencyCount / total) * 10000) / 100 : null,
          admissionDistribution: admission.data.rows.map((r) => ({
            name: String(r.type_of_admission ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          paymentDistribution: payment.data.rows.map((r) => ({
            name: String(r.payment_typology_1 ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          topCounties: county.data.rows.map((r) => ({
            name: String(r.hospital_county ?? 'Unknown'),
            dischargeCount: Number(r.discharge_count ?? 0),
          })),
          severityDistribution: severity.data.rows.map((r) => ({
            name: String(r.apr_severity_of_illness_description ?? 'Unknown'),
            value: Number(r.discharge_count ?? 0),
          })),
          topDiagnoses: diagnoses.data.rows.map((r) => ({
            name: String(r.ccsr_diagnosis_description ?? 'Unknown'),
            dischargeCount: Number(r.discharge_count ?? 0),
          })),
          computedAt: new Date().toLocaleTimeString(),
        }
      } catch (error) {
        this.screenError = error instanceof Error ? error.message : '在线聚合查询失败'
        this.screenData = null
      } finally {
        this.screenLoading = false
      }
    },
  },
})
