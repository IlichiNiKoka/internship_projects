export interface NameValue {
  name: string
  value: number
}

/** 年龄 × 性别 交叉统计（来自后端实时聚合，用于总览页性别切换柱状图） */
export interface AgeGenderStat {
  age: string
  gender: string
  value: number
}

export interface DiagnosisStat {
  name: string
  dischargeCount: number
  avgCharges: number
  avgCosts: number
}

export interface RegionStat {
  name: string
  dischargeCount: number
  avgCharges: number
  avgCosts: number
  /** 所在县 / 邮编前缀（静态汇总可选提供，用于离线模式地图点位定位） */
  county?: string
  zip3?: string
}

export interface AdmissionCostStat extends RegionStat {
  avgLengthOfStay: number
}

export interface SeverityMortalityValue {
  severity: string
  mortality: string
  count: number
}

export interface ApiEndpointSpec {
  name: string
  method: string
  path: string
}

export interface DashboardPayload {
  meta: {
    generatedAt: string
    sourceCsv: string
    cleanRows: number
    frontendRequirementsDocStatus: string
    notes: string[]
  }
  overview: {
    dischargeCount: number
    avgLengthOfStay: number
    avgTotalCharges: number
    avgTotalCosts: number
    emergencyRate: number
    topCounty: string
    topDiagnosis: string
  }
  /** 筛选下拉全量选项（按名称排序；离线模式也提供全部疾病/医院） */
  filterOptions?: {
    diseases: string[]
    hospitals: string[]
  }
  dashboard: {
    ageDistribution: NameValue[]
    genderDistribution: NameValue[]
    admissionDistribution: NameValue[]
    paymentDistribution: NameValue[]
    dispositionDistribution: NameValue[]
    serviceAreaDistribution: NameValue[]
    medicalSurgicalDistribution: NameValue[]
    severityDistribution: NameValue[]
    mortalityDistribution: NameValue[]
    /** 主要诊断类别 MDC 排行（静态回退数据；在线由后端实时聚合） */
    mdcDistribution?: NameValue[]
    topDiagnoses: DiagnosisStat[]
    topCounties: RegionStat[]
    topFacilities: RegionStat[]
    admissionCostComparison: AdmissionCostStat[]
    severityMortalityMatrix: {
      xAxis: string[]
      yAxis: string[]
      values: SeverityMortalityValue[]
    }
  }
  algorithms: {
    associationRulesPreview: Array<{
      diagnosis: string
      procedure: string
      count: number
      support: number
    }>
    costInsightPreview: {
      byAdmissionType: AdmissionCostStat[]
      bySeverity: Array<{
        name: string
        dischargeCount: number
        avgCharges: number
      }>
      byMortalityRisk: Array<{
        name: string
        dischargeCount: number
        avgCharges: number
      }>
    }
    readmissionRiskProxy: {
      distribution: NameValue[]
      factors: string[]
    }
  }
  quality: {
    summary: {
      rawRows: number
      cleanRows: number
      duplicateRowsRemoved: number
      dedupRate: number
      overallCellCompleteness: number
      keyFieldCompleteness: number
      processingSeconds: number
      normalizedFieldCount: number
    }
    topMissingFields: Array<{
      field: string
      missing: number
      missingRate: number
    }>
    keyFieldDistributions: Record<string, Record<string, number>>
    processingRules: Array<{
      step: string
      what: string
      why: string
    }>
  }
  api: {
    baseUrl: string
    endpoints: ApiEndpointSpec[]
  }
}

export interface ApiMetaItem {
  key: string
  label: string
  column?: string | null
  unit?: string
  aggregation?: string
  description?: string
  value_type?: string
}

export interface AggregationRequest {
  dimensions: string[]
  metrics: string[]
  filters?: Array<{
    field: string
    op: string
    value?: string | number
    /** 多值过滤（op=in 时使用） */
    values?: Array<string | number>
  }>
  sort?: Array<{ field: string; order: string }>
  limit?: number
}

export interface AggregationResponseRow {
  [key: string]: string | number | boolean | null
}

export interface AggregationResponse {
  dimensions: Array<{
    key: string
    column: string
    label: string
  }>
  metrics: Array<{
    key: string
    label: string
    unit: string
  }>
  filters: unknown[]
  rows: AggregationResponseRow[]
  row_count: number
  truncated: boolean
  cached: boolean
  compute_seconds: number
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  title?: string
  content: string
  bullets?: string[]
  /** 后端返回的结构化数据表 */
  table?: { columns: string[]; rows: Array<Record<string, unknown>> } | null
  /** 后端返回的指标卡 */
  kpis?: Array<{ label: string; value: string }> | null
  createdAt: number
  status?: 'ok' | 'error'
  source?: 'llm-api' | 'local-fallback' | 'backend-ai'
  /** 大模型返回的可视化配置（ECharts option 结构），前端直接渲染 */
  chart?: Record<string, unknown> | null
}

export interface ChatResult {
  reply: string
  title?: string
  bullets?: string[]
  chart?: Record<string, unknown> | null
  source: 'llm-api' | 'local-fallback'
}

/** 大屏筛选后由后端实时聚合得到的数据（驱动大屏全部图表） */
export interface ScreenData {
  dischargeCount: number
  avgLengthOfStay: number | null
  avgTotalCharges: number | null
  avgTotalCosts: number | null
  emergencyRate: number | null
  admissionDistribution: NameValue[]
  paymentDistribution: NameValue[]
  /** 住院年龄段分布（筛选后后端实时聚合） */
  ageDistribution: NameValue[]
  topCounties: Array<{ name: string; dischargeCount: number }>
  severityDistribution: NameValue[]
  topDiagnoses: Array<{ name: string; dischargeCount: number }>
  /** 主要诊断类别 MDC 排行（筛选后后端实时聚合） */
  mdcDistribution: NameValue[]
  /** 可视化进阶：全部医院（含所在县/邮编前缀，用于地图点位定位） */
  hospital3d: Array<{
    name: string
    county: string
    /** 3 位邮编前缀（ZIP3 区域质心定位用；后端不可用时为空串） */
    zip3: string
    dischargeCount: number
    avgCharges: number | null
  }>
  computedAt: string
}

export interface FilterState {
  /** 疾病多选（空数组 = 全部） */
  disease: string[]
  age: string
  /** 医院多选（空数组 = 全部） */
  hospital: string[]
  year: string
}
