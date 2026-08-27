<script setup lang="ts">
import * as echarts from 'echarts'
import type { EChartsOption } from 'echarts'
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import nyRegionGeoJsonRaw from '../assets/map/ny-region.json?raw'
import nyZip3CentersRaw from '../assets/map/ny-zip3-centers.json?raw'

import ChartCard from '../components/ChartCard.vue'
import SearchSelect from '../components/SearchSelect.vue'
import { useDashboardStore } from '../store/dashboard'
import type { FilterState } from '../types/dashboard'
import { diseaseNameCn } from '../utils/diseaseNames'
import { groupDiseasesByCategory, mdcNameCn } from '../utils/diseaseCategories'

// 注册纽约州及邻州背景地图（可视化进阶 · 全州医院分布）
// ny-region.json = 纽约州 62 县（高精度）+ 周边邻州淡色轮廓，properties.region 区分主体/背景
const nyRegionGeoJson = JSON.parse(nyRegionGeoJsonRaw) as {
  type: string
  features: Array<{ properties: { name: string; lat: string; lon: string; region: string } }>
}
echarts.registerMap('ny', nyRegionGeoJson as unknown as Parameters<typeof echarts.registerMap>[1])

// 邻州背景区域名（用于淡色显示、隐藏标签）
const BORDER_REGIONS = nyRegionGeoJson.features
  .filter((f) => f.properties.region === 'border')
  .map((f) => f.properties.name)

// 县名 -> 中心坐标（取自纽约州县级边界数据）
const NY_COUNTY_CENTERS: Record<string, [number, number]> = {}
for (const feature of nyRegionGeoJson.features) {
  if (feature.properties.region !== 'ny') {
    continue
  }
  const p = feature.properties
  const lat = parseFloat(String(p.lat).replace('+', ''))
  const lon = parseFloat(String(p.lon).replace('+', ''))
  if (!Number.isNaN(lat) && !Number.isNaN(lon)) {
    NY_COUNTY_CENTERS[p.name] = [lon, lat]
  }
}

// ZIP3 邮编前缀 -> 质心坐标（US Census 2010 ZCTA 聚合，比县中心更精确的医院定位）
const NY_ZIP3_CENTERS = JSON.parse(nyZip3CentersRaw) as Record<string, [number, number]>

function zip3Center(zip3: string): [number, number] | null {
  if (!zip3) {
    return null
  }
  const c = NY_ZIP3_CENTERS[zip3]
  return c ? [c[0], c[1]] : null
}

// 纽约州地理边界框（钳制点位，杜绝异常点落到地图外）
const NY_BBOX = { lonMin: -79.9, lonMax: -71.7, latMin: 40.4, latMax: 45.1 }
function clampToState(lon: number, lat: number): [number, number] {
  return [
    Math.min(NY_BBOX.lonMax, Math.max(NY_BBOX.lonMin, lon)),
    Math.min(NY_BBOX.latMax, Math.max(NY_BBOX.latMin, lat)),
  ]
}

// SPARCS 数据中的县名 -> 官方县名（GeoJSON NAME）
const COUNTY_NAME_FIX: Record<string, string> = {
  Manhattan: 'New York',
  'Staten Island': 'Richmond',
}

function countyCenter(county: string): [number, number] | null {
  const official = COUNTY_NAME_FIX[county] ?? county
  return NY_COUNTY_CENTERS[official] ?? null
}

const store = useDashboardStore()
const nowText = ref('')
let timer: number | null = null

const payload = computed(() => store.payload)
const dashboard = computed(() => payload.value?.dashboard)
const overview = computed(() => payload.value?.overview)
const screenLoading = ref(false)
const screenError = ref('')
const appliedFilterSummary = ref('默认展示全量数据')
const filters = reactive<FilterState>({
  disease: [],
  age: '全部年龄',
  hospital: [],
  year: '2021',
})

// ---- 实时数据源（筛选后由后端聚合；未筛选/后端不可用时回退静态汇总）----
const screen = computed(() => store.screenData)
const kpiDischarge = computed(() => screen.value?.dischargeCount ?? overview.value?.dischargeCount ?? 0)
const kpiEmergency = computed(() => screen.value?.emergencyRate ?? overview.value?.emergencyRate ?? 0)
const kpiCharges = computed(() => screen.value?.avgTotalCharges ?? overview.value?.avgTotalCharges ?? 0)
const kpiLos = computed(() => screen.value?.avgLengthOfStay ?? overview.value?.avgLengthOfStay ?? 0)
const admissionSource = computed(() => screen.value?.admissionDistribution ?? dashboard.value?.admissionDistribution ?? [])
const paymentSource = computed(() => screen.value?.paymentDistribution ?? dashboard.value?.paymentDistribution ?? [])
const countySource = computed(() => screen.value?.topCounties ?? dashboard.value?.topCounties ?? [])
const severitySource = computed(() => screen.value?.severityDistribution ?? dashboard.value?.severityDistribution ?? [])

// ---- 可视化进阶数据源 ----
const hospital3dSource = computed(
  () =>
    screen.value?.hospital3d ??
    dashboard.value?.topFacilities.map((f) => ({
      name: f.name,
      county: f.county ?? 'Unknown',
      zip3: f.zip3 ?? '',
      dischargeCount: f.dischargeCount,
      avgCharges: f.avgCharges ?? null,
    })) ??
    [],
)
// 全州县级填色数据（数据县名 -> 官方县名）
const countyChoroSource = computed(() =>
  countySource.value.map((c) => ({
    name: COUNTY_NAME_FIX[c.name] ?? c.name,
    value: c.dischargeCount,
  })),
)
// 主要诊断类别 MDC 排行（在线=筛选后聚合；离线=静态数据回退）
const mdcSource = computed(() => screen.value?.mdcDistribution ?? dashboard.value?.mdcDistribution ?? [])

// ---- 医院位置与简介（纽约都会区知名医院，坐标为近似值；用于地图点位展示）----
const HOSPITAL_POINTS: Record<string, { lon: number; lat: number; intro: string }> = {
  'Mount Sinai Hospital': {
    lon: -73.954, lat: 40.79,
    intro: '西奈山医疗系统旗舰医院，位于曼哈顿上东区，大型学术医学中心，以心血管、神经与肿瘤治疗著称。',
  },
  'North Shore University Hospital': {
    lon: -73.701, lat: 40.787,
    intro: 'Northwell Health 医疗系统旗舰医院，位于长岛曼哈塞特，创伤中心与心脏手术实力雄厚。',
  },
  'NYU Langone Hospitals': {
    lon: -73.974, lat: 40.743,
    intro: '纽约大学兰贡医疗中心，位于曼哈顿第一大道，集临床、教学与科研一体的综合医学中心。',
  },
  'New York-Presbyterian Hospital - New York Weill Cornell Center': {
    lon: -73.954, lat: 40.765,
    intro: '纽约长老会医院威尔康奈尔主院区，曼哈顿上东区学术医疗中心，多学科综合实力强。',
  },
  'New York-Presbyterian Hospital - Columbia Presbyterian Center': {
    lon: -73.94, lat: 40.848,
    intro: '纽约长老会医院哥伦比亚主院区，位于曼哈顿华盛顿高地，以儿科与神经科学著称。',
  },
  'Bellevue Hospital Center': {
    lon: -73.976, lat: 40.739,
    intro: '美国最古老公立医院之一，纽约市健康与医院集团旗舰，位于曼哈顿东村。',
  },
  'Stony Brook University Hospital': {
    lon: -73.131, lat: 40.91,
    intro: '石溪大学医院，长岛东部学术医疗中心，纽约州立大学系统教学医院。',
  },
  'Staten Island University Hospital': {
    lon: -74.12, lat: 40.614,
    intro: '史泰登岛大学医院，覆盖史泰登岛的主要急症医疗中心。',
  },
  'Maimonides Medical Center': {
    lon: -73.983, lat: 40.633,
    intro: '迈蒙尼德医学中心，布鲁克林最大的学术医疗中心之一。',
  },
  'Montefiore Medical Center': {
    lon: -73.89, lat: 40.847,
    intro: '蒙蒂菲奥里医学中心，布朗克斯区旗舰学术医疗中心。',
  },
  'Bronx-Lebanon Hospital Center': {
    lon: -73.902, lat: 40.855,
    intro: '布朗克斯-黎巴嫩医院中心，布朗克斯中部大型社区综合医院。',
  },
  'Brookdale University Hospital Medical Center': {
    lon: -73.904, lat: 40.654,
    intro: '布鲁克代尔大学医院医学中心，布鲁克林东部大型社区医院。',
  },
  'Kings County Hospital Center': {
    lon: -73.943, lat: 40.655,
    intro: '国王县医院中心，布鲁克林主要公立教学医院。',
  },
  'Jacobi Medical Center': {
    lon: -73.845, lat: 40.853,
    intro: '雅可比医学中心，布朗克斯北部公立医院，以烧伤与创伤救治见长。',
  },
  'Queens Hospital Center': {
    lon: -73.79, lat: 40.71,
    intro: '皇后区医院中心，位于皇后区牙买加地区的主要公立医院。',
  },
  'Elmhurst Hospital Center': {
    lon: -73.879, lat: 40.739,
    intro: '埃尔姆赫斯特医院中心，皇后区主要公立医院，以多语言医疗服务著称。',
  },
  'Lincoln Medical and Mental Health Center': {
    lon: -73.908, lat: 40.81,
    intro: '林肯医疗心理健康中心，布朗克斯南部主要公立医院。',
  },
  'Harlem Hospital Center': {
    lon: -73.94, lat: 40.81,
    intro: '哈莱姆医院中心，曼哈顿北部主要公立教学医院。',
  },
  'Lenox Hill Hospital': {
    lon: -73.957, lat: 40.771,
    intro: '勒诺克斯山医院，曼哈顿上东区私立教学医院（Northwell Health 旗下）。',
  },
  'NewYork-Presbyterian Brooklyn Methodist Hospital': {
    lon: -73.984, lat: 40.665,
    intro: '纽约长老会布鲁克林卫理公会医院，位于布鲁克林公园坡。',
  },
}

// 无法定位的医院：基于名称哈希在纽约州陆地范围内生成稳定示意坐标
function fallbackPoint(name: string) {
  let hash = 0
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0
  }
  const lon = -79.3 + ((hash % 100) / 100) * 7.0 // -79.3 ~ -72.3
  const lat = 41.0 + (((hash >> 8) % 100) / 100) * 3.5 // 41.0 ~ 44.5
  return { lon, lat }
}

// 基于名称哈希生成同区域内的稳定偏移，避免点位重叠（jitter 幅度可调）
function hashOffset(name: string, range: number): [number, number] {
  let hash = 0
  for (let i = 0; i < name.length; i += 1) {
    hash = (hash * 31 + name.charCodeAt(i)) >>> 0
  }
  const dLon = (((hash % 25) - 12) / 25) * range
  const dLat = ((((hash >> 8) % 25) - 12) / 25) * range
  return [dLon, dLat]
}

const hospitalPoints = computed(() => {
  // 专门筛选医院时，地图只显示被选中的医院（在线：后端已按 in 过滤；离线：前端兜底过滤）
  let list = hospital3dSource.value
  if (filters.hospital.length > 0) {
    const selected = new Set(filters.hospital)
    list = list.filter((h) => selected.has(h.name))
  }
  // 同一医院可能在后端按 facility+county+zip3 聚合出多行（源数据中同一医院存在多条 county/zip3 记录），
  // 按医院名称去重、保留记录量最大的一行，保证"一个医院 = 一个标注点"
  const byName = new Map<string, (typeof hospital3dSource.value)[number]>()
  for (const h of list) {
    const cur = byName.get(h.name)
    if (!cur || h.dischargeCount > cur.dischargeCount) {
      byName.set(h.name, h)
    }
  }
  return [...byName.values()].map((h) => {
    const known = HOSPITAL_POINTS[h.name]
    if (known) {
      const [lon, lat] = clampToState(known.lon, known.lat)
      return {
        name: h.name,
        value: [lon, lat, h.dischargeCount],
        charges: h.avgCharges,
        intro: known.intro,
      }
    }
    // 第二优先：ZIP3 邮编区域质心（比县中心精确得多，约 5-10km 范围）
    const zip3c = zip3Center(h.zip3)
    if (zip3c) {
      const [dLon, dLat] = hashOffset(h.name, 0.012)
      const [lon, lat] = clampToState(zip3c[0] + dLon, zip3c[1] + dLat)
      return {
        name: h.name,
        value: [lon, lat, h.dischargeCount],
        charges: h.avgCharges,
        intro: `位于纽约州邮编 ${h.zip3} 区域的医院（坐标为邮编区域近似定位，详情以官方信息为准）。`,
      }
    }
    // 第三优先：县中心 + 名称哈希偏移
    const center = countyCenter(h.county)
    if (center) {
      const [dLon, dLat] = hashOffset(h.name, 0.03)
      const [lon, lat] = clampToState(center[0] + dLon, center[1] + dLat)
      return {
        name: h.name,
        value: [lon, lat, h.dischargeCount],
        charges: h.avgCharges,
        intro: `位于纽约州 ${h.county} 县的医院（坐标为县区近似定位，详情以官方信息为准）。`,
      }
    }
    // 最后兜底：州内陆地范围哈希示意坐标
    const fallback = fallbackPoint(h.name)
    const [flon, flat] = clampToState(fallback.lon, fallback.lat)
    return {
      name: h.name,
      value: [flon, flat, h.dischargeCount],
      charges: h.avgCharges,
      intro: '该医院位于纽约州（坐标为估算示意，详情以官方信息为准）。',
    }
  })
})

function formatNow() {
  const now = new Date()
  const pad = (n: number) => String(n).padStart(2, '0')
  nowText.value = `${now.getFullYear()}年${now.getMonth() + 1}月${now.getDate()}日${pad(now.getHours())}时${pad(
    now.getMinutes(),
  )}分${pad(now.getSeconds())}秒`
}

onMounted(() => {
  formatNow()
  timer = window.setInterval(formatNow, 1000)
  // 加载疾病/医院全量筛选选项（在线 = 后端聚合全量，离线 = 静态 JSON）
  void store.loadFilterOptions()
  initAdvancedCharts()
  window.addEventListener('resize', handleAdvancedResize)
})

onUnmounted(() => {
  if (timer) {
    window.clearInterval(timer)
    timer = null
  }
  window.removeEventListener('resize', handleAdvancedResize)
  mapChart?.off('click', onMapPointClick)
  mapChart?.dispose()
  mdcChart?.dispose()
  expandChart?.dispose()
})

// 疾病筛选：二级菜单（大类 -> 具体疾病）。
// 使用 store 中加载的全量选项（在线 = 后端全量聚合，离线 = 静态 JSON）。
// 疾病显示中文、value 保留英文原文（后端 in 过滤按原值匹配），悬浮可见英文原文。
const diseaseFilterGroups = computed(() => groupDiseasesByCategory(store.filterDiseases))
const ageOptions = computed(() => ['全部年龄', ...(dashboard.value?.ageDistribution.map((d) => d.name) ?? [])])

// 医院全名可能很长（最长约 62 字符），下拉显示截断名称、保留全名作为筛选值，避免下拉框被撑得过宽
function shortenName(name: string, max = 22) {
  return name.length > max ? `${name.slice(0, max)}…` : name
}
const hospitalFilterOptions = computed(() =>
  store.filterHospitals.map((h) => ({ label: shortenName(h), value: h })),
)

// 图表右上角小功能：下载 + 放大（同一行并排）；放大为 ECharts 自定义按钮，触发全屏查看
function makeChartToolbox(
  title: string,
  getOption: () => EChartsOption,
): NonNullable<EChartsOption['toolbox']> {
  return {
    top: 12,
    right: 10,
    feature: {
      saveAsImage: { title: '下载图表' },
      myFullScreen: {
        show: true,
        title: '放大查看',
        icon: 'path://M7 14H5v5h5v-2H7v-3zm-2-4h2V7h3V5H5v5zm12 7h-3v2h5v-5h-2v3zM14 5v2h3v3h2V5h-5z',
        onclick: () => openExpand(title, getOption),
      },
    },
  }
}

// 多选筛选摘要：空数组显示"全部"，否则显示前 2 项 + 等 N 项
function fmtMulti(values: string[], emptyText = '全部'): string {
  if (!values.length) {
    return emptyText
  }
  if (values.length <= 2) {
    return values.join('、')
  }
  return `${values.slice(0, 2).join('、')} 等 ${values.length} 项`
}

async function applyFilters() {
  screenLoading.value = true
  screenError.value = ''

  // 筛选摘要中的疾病名显示中文，医院名保持原样
  const diseaseSummary = fmtMulti(filters.disease.map(diseaseNameCn))

  try {
    if (store.apiAvailable) {
      await store.runScreenQuery({
        disease: filters.disease,
        age: filters.age,
        hospital: filters.hospital,
        year: filters.year,
      })

      if (store.screenError) {
        screenError.value = store.screenError
      } else if (store.screenData) {
        const data = store.screenData
        appliedFilterSummary.value =
          `疾病：${diseaseSummary} / 年龄：${filters.age} / 医院：${fmtMulti(filters.hospital)} / 年份：${filters.year}` +
          ` · 后端实时聚合 ${data.dischargeCount.toLocaleString()} 条记录（${data.computedAt} 计算）`
        // 同步刷新可视化进阶图表（地图 / 3D）
        renderAdvancedCharts()
      }
    } else {
      appliedFilterSummary.value =
        `疾病：${diseaseSummary} / 年龄：${filters.age} / 医院：${fmtMulti(filters.hospital)} / 年份：${filters.year}` +
        '（未连接后端，当前展示静态汇总）'
    }
  } finally {
    window.setTimeout(() => {
      screenLoading.value = false
    }, 450)
  }
}

function exportDashboard() {
  const blob = new Blob([JSON.stringify(payload.value, null, 2)], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = url
  link.download = 'medical-dashboard-export.json'
  link.click()
  URL.revokeObjectURL(url)
}

// ---------------------------------------------------------------
// 可视化进阶：地图 / 3D / 动态时序（直接管理 ECharts 实例）
// ---------------------------------------------------------------
const mapRef = ref<HTMLDivElement | null>(null)
const mdcRef = ref<HTMLDivElement | null>(null)
let mapChart: echarts.ECharts | null = null
let mdcChart: echarts.ECharts | null = null

const advAxisLabel = { color: '#b9d8ff' }
const advSplitLine = { lineStyle: { color: 'rgba(114, 188, 255, 0.12)' } }

function buildMapOption(): EChartsOption {
  const values = countyChoroSource.value.map((d) => d.value)
  const max = Math.max(...values, 1)
  // 有住院数据的县名集合（用于隐藏无数据县的标签与交互）
  const dataCountyNames = new Set(countyChoroSource.value.map((d) => d.name))
  const noDataRegions = nyRegionGeoJson.features
    .filter((f) => f.properties.region === 'ny' && !dataCountyNames.has(f.properties.name))
    .map((f) => ({
      name: f.properties.name,
      label: { show: false },
      itemStyle: { areaColor: '#0a2138', borderColor: '#1e3a5f', borderWidth: 0.6 },
      emphasis: { label: { show: false }, itemStyle: { areaColor: '#0a2138' } },
    }))
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      // 地图/点位的统一提示：由各数据系列的提示框内容生成
    },
    geo: {
      map: 'ny',
      roam: true,
      zoom: 4.2,
      center: [-76.1, 42.55],
      label: { show: true, color: '#e2f0ff', fontSize: 10 },
      itemStyle: { borderColor: '#1e3a5f', borderWidth: 0.6, areaColor: '#0b2745' },
      emphasis: {
        label: { show: true, color: '#ffffff' },
        itemStyle: { areaColor: '#f59e0b' },
      },
      // 邻州背景 + 无数据县：淡色填充、不显示标签、不参与悬停高亮
      regions: [
        ...BORDER_REGIONS.map((name) => ({
          name,
          label: { show: false },
          itemStyle: { areaColor: '#0a1f38', borderColor: 'rgba(114, 188, 255, 0.16)', borderWidth: 0.5 },
          emphasis: {
            label: { show: false },
            itemStyle: { areaColor: '#0a1f38' },
          },
        })),
        ...noDataRegions,
      ],
    },
    visualMap: {
      min: 0,
      max,
      left: 8,
      bottom: 8,
      text: ['高', '低'],
      textStyle: { color: '#b9d8ff' },
      inRange: { color: ['#0c4a6e', '#0ea5e9', '#7dd3fc'] },
      calculable: true,
      seriesIndex: 0,
    },
    series: [
      {
        name: '县级住院人次',
        type: 'map',
        geoIndex: 0,
        data: countyChoroSource.value,
        tooltip: {
          formatter: (p: unknown) => {
            const item = p as { name: string; value?: number }
            const v = item.value
            // 无数据 / NaN 的县不弹提示
            if (v == null || Number.isNaN(Number(v))) {
              return null as unknown as string
            }
            return `${item.name} 县<br/>住院人次：${Number(v).toLocaleString()}`
          },
        },
      },
      {
        name: '医院分布',
        type: 'effectScatter',
        coordinateSystem: 'geo',
        data: hospitalPoints.value,
        symbolSize: (val: unknown) => {
          const v = val as number[]
          return Math.max(12, Math.min(36, Math.round(Math.sqrt(Number(v[2]) / 60))))
        },
        rippleEffect: { brushType: 'stroke', scale: 3 },
        // 医院数量多时默认不显示标签，悬停通过提示框查看详情，避免点位/文字重叠
        label: { show: false },
        itemStyle: {
          color: '#fbbf24',
          borderColor: '#ffffff',
          borderWidth: 1.5,
          shadowBlur: 14,
          shadowColor: 'rgba(251, 191, 36, 0.9)',
        },
        tooltip: {
          // 约束提示框始终在图表容器内，不超出页面框
          confine: true,
          extraCssText: 'max-width:300px;word-break:break-word;overflow-wrap:anywhere;',
          formatter: (p: unknown) => {
            // 平均费用等自定义字段位于数据项对象内，医院名与坐标在顶层
            const raw = p as {
              name?: string
              value?: number[]
              data?: { name?: string; value?: number[]; charges?: number | null }
            }
            const d = raw.data ?? {}
            const name = d.name ?? raw.name ?? ''
            const count = Number(d.value?.[2] ?? raw.value?.[2] ?? 0)
            const charges =
              d.charges == null ? '—' : `$${Number(d.charges).toLocaleString()}`
            return (
              `<div style="max-width:280px">` +
              `<b style="color:#fcd34d">${name}</b><br/>` +
              `住院人次：${count.toLocaleString()}<br/>` +
              `平均费用：${charges}` +
              `</div>`
            )
          },
        },
      },
    ],
  }
}

function buildMdcOption(): EChartsOption {
  return {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const p = params as Array<{ name?: string; value?: number | string }>
        const item = p[0]
        if (!item) {
          return ''
        }
        return `<b style="color:#7dd3fc">${mdcNameCn(String(item.name ?? ''))}</b><br/>住院人次：${Number(item.value ?? 0).toLocaleString()}`
      },
    },
    grid: { left: 112, right: 30, top: 10, bottom: 24 },
    xAxis: { type: 'value', axisLabel: advAxisLabel, splitLine: advSplitLine },
    yAxis: {
      type: 'category',
      data: mdcSource.value.map((c) => mdcNameCn(c.name)),
      axisLabel: { ...advAxisLabel, fontSize: 11, width: 100, overflow: 'truncate' },
    },
    series: [
      {
        type: 'bar',
        barWidth: 12,
        itemStyle: { color: '#22d3ee', borderRadius: [0, 3, 3, 0] },
        data: mdcSource.value.map((c) => c.value),
      },
    ],
  }
}

function renderAdvancedCharts() {
  mapChart?.setOption(buildMapOption(), true)
  mdcChart?.setOption(buildMdcOption(), true)
}

// 点击地图医院点位 → 与"医院筛选框"等效：直接筛选该医院
function onMapPointClick(params: unknown) {
  const p = params as { componentType?: string; seriesType?: string; data?: { name?: string } }
  if (p.componentType !== 'series' || p.seriesType !== 'effectScatter') {
    return
  }
  const name = p.data?.name
  if (!name) {
    return
  }
  filters.hospital = [name]
  void applyFilters()
}

function initAdvancedCharts() {
  if (mapRef.value) {
    mapChart = echarts.init(mapRef.value)
    mapChart.on('click', onMapPointClick)
  }
  if (mdcRef.value) {
    mdcChart = echarts.init(mdcRef.value)
  }
  renderAdvancedCharts()
}

function handleAdvancedResize() {
  mapChart?.resize()
  mdcChart?.resize()
  expandChart?.resize()
}

// ---------------------------------------------------------------
// 图表放大查看：点击各图表面板右上角按钮，在全屏遮罩中以大尺寸重渲染
// ---------------------------------------------------------------
const expanded = ref<{ title: string; getOption: () => EChartsOption } | null>(null)
const expandRef = ref<HTMLDivElement | null>(null)
let expandChart: echarts.ECharts | null = null

function openExpand(title: string, getOption: () => EChartsOption) {
  expanded.value = { title, getOption }
}

function closeExpand() {
  expanded.value = null
}

watch(expanded, async (val) => {
  if (!val) {
    expandChart?.dispose()
    expandChart = null
    return
  }
  await nextTick()
  if (expandRef.value) {
    expandChart?.dispose()
    expandChart = echarts.init(expandRef.value)
    expandChart.setOption(val.getOption(), true)
  }
})

// 住院年龄段分布（在线=筛选后聚合；离线=静态汇总）
const ageSource = computed(() => screen.value?.ageDistribution ?? dashboard.value?.ageDistribution ?? [])

function buildAgeOption(): EChartsOption {
  return {
    title: {
      text: '住院年龄段分布',
      left: 16,
      top: 12,
      textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
    },
    toolbox: makeChartToolbox('住院年龄段分布', buildAgeOption),
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 76, right: 26, top: 52, bottom: 28 },
    xAxis: {
      type: 'value',
      axisLabel: { color: '#b9d8ff' },
      splitLine: { lineStyle: { color: 'rgba(114, 188, 255, 0.12)' } },
    },
    yAxis: {
      type: 'category',
      axisLabel: { color: '#b9d8ff', fontSize: 11 },
      data: ageSource.value.map((d) => d.name) ?? [],
    },
    series: [
      {
        type: 'bar',
        barWidth: 12,
        itemStyle: { color: '#38bdf8', borderRadius: [0, 3, 3, 0] },
        data: ageSource.value.map((d) => d.value) ?? [],
      },
    ],
  }
}
const ageOption = computed(buildAgeOption)

function buildAdmissionOption(): EChartsOption {
  return {
    title: {
      text: '入院类型分布',
      left: 16,
      top: 12,
      textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
    },
    toolbox: makeChartToolbox('入院类型分布', buildAdmissionOption),
    tooltip: { trigger: 'axis' },
  grid: { left: 48, right: 18, top: 52, bottom: 40 },
  xAxis: {
    type: 'value',
    axisLabel: { color: '#b9d8ff' },
    splitLine: { lineStyle: { color: 'rgba(114, 188, 255, 0.12)' } },
  },
  yAxis: {
    type: 'category',
    axisLabel: { color: '#b9d8ff' },
    data: admissionSource.value.map((d) => d.name) ?? [],
  },
  series: [
    {
      type: 'bar',
      barWidth: 14,
      itemStyle: { color: '#72bcff' },
      data: admissionSource.value.map((d) => d.value) ?? [],
    },
  ],
  }
}
const admissionOption = computed(buildAdmissionOption)

function buildPaymentOption(): EChartsOption {
  return {
    title: {
      text: '支付方式构成',
      left: 16,
      top: 12,
      textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
    },
    toolbox: makeChartToolbox('支付方式构成', buildPaymentOption),
    tooltip: { trigger: 'item' },
  // 支付方式类别较多（全选 6+ 项），图例放右侧垂直排列 + 可滚动 + 长名截断，避免底部拥挤
  legend: {
    orient: 'vertical',
    right: 6,
    top: 'middle',
    type: 'scroll',
    width: 126,
    textStyle: { color: '#b9d8ff', fontSize: 11 },
    itemWidth: 9,
    itemHeight: 9,
    itemGap: 8,
    icon: 'circle',
    formatter: (name: string) => (name.length > 12 ? `${name.slice(0, 12)}…` : name),
  },
  series: [
    {
      type: 'pie',
      radius: ['42%', '60%'],
      center: ['33%', '50%'],
      // 标签显示在环带内部（短名+占比），避免外伸标签与右侧图例重合遮挡
      label: {
        color: '#e2f0ff',
        fontSize: 11,
        position: 'inside',
        formatter: (p: unknown) => {
          const item = p as { name?: string; percent?: number }
          const full = item.name ?? ''
          const name = full.length > 8 ? `${full.slice(0, 8)}…` : full
          return `${name}\n${Math.round(item.percent ?? 0)}%`
        },
      },
      labelLine: { show: false },
      data:
        paymentSource.value.slice(0, 6).map((d) => ({
          name: d.name,
          value: d.value,
        })) ?? [],
    },
  ],
  }
}
const paymentOption = computed(buildPaymentOption)

function buildSeverityOption(): EChartsOption {
  return {
    title: {
      text: '病情严重程度统计',
      left: 16,
      top: 12,
      textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
    },
    toolbox: makeChartToolbox('病情严重程度统计', buildSeverityOption),
    tooltip: { trigger: 'axis' },
  grid: { left: 48, right: 18, top: 52, bottom: 40 },
  xAxis: {
    type: 'value',
    axisLabel: { color: '#b9d8ff' },
    splitLine: { lineStyle: { color: 'rgba(114, 188, 255, 0.12)' } },
  },
  yAxis: {
    type: 'category',
    axisLabel: { color: '#b9d8ff' },
    data: severitySource.value.map((d) => d.name) ?? [],
  },
  series: [
    {
      type: 'bar',
      barWidth: 14,
      itemStyle: { color: '#7c3aed' },
      data: severitySource.value.map((d) => d.value) ?? [],
    },
  ],
  }
}
const severityOption = computed(buildSeverityOption)
</script>

<template>
  <div v-if="payload && dashboard && overview" class="screen-shell">
    <header class="screen-header">
      <h1>医疗数据可视化大屏</h1>
      <p class="screen-time">当前时间：{{ nowText }}</p>
    </header>

    <section class="screen-filterbar">
      <label class="screen-field">
        <span>疾病</span>
        <SearchSelect
          v-model="filters.disease"
          :groups="diseaseFilterGroups"
          placeholder="全部疾病"
          all-label="全部疾病"
          empty-text="无匹配疾病"
        />
      </label>
      <label class="screen-field">
        <span>年龄</span>
        <select v-model="filters.age">
          <option v-for="item in ageOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="screen-field">
        <span>医院</span>
        <SearchSelect
          v-model="filters.hospital"
          :options="hospitalFilterOptions"
          placeholder="全部医院"
          all-label="全部医院"
          empty-text="无匹配医院"
        />
      </label>
      <label class="screen-field">
        <span>年份</span>
        <select v-model="filters.year">
          <option value="2021">2021</option>
        </select>
      </label>
      <button class="screen-action" type="button" @click="applyFilters">应用筛选</button>
      <button class="screen-action screen-action--ghost" type="button" @click="exportDashboard">下载导出</button>
    </section>

    <section class="screen-messagebar">
      <span v-if="screenLoading">数据加载中...</span>
      <span v-else-if="screenError">{{ screenError }}</span>
      <span v-else>{{ appliedFilterSummary }}</span>
    </section>

    <section class="screen-kpi-row">
      <div class="screen-kpi-box">
        <span>总住院人次</span>
        <strong>{{ kpiDischarge.toLocaleString() }}</strong>
      </div>
      <div class="screen-kpi-box">
        <span>急诊占比</span>
        <strong>{{ kpiEmergency }}%</strong>
      </div>
      <div class="screen-kpi-box">
        <span>平均费用(美元)</span>
        <strong>{{ kpiCharges.toLocaleString() }}</strong>
      </div>
      <div class="screen-kpi-box">
        <span>平均住院(天)</span>
        <strong>{{ (kpiLos ?? 0).toFixed(1) }}</strong>
      </div>
    </section>

    <section class="screen-main-layout">
      <!-- 左列图表 -->
      <div class="screen-col">
        <div class="screen-panel">
          <ChartCard title="入院类型" :option="admissionOption" height="176px" theme="dark" />
        </div>
        <div class="screen-panel">
          <ChartCard title="支付方式" :option="paymentOption" height="176px" theme="dark" />
        </div>
      </div>

      <!-- 中列：大地图（主视觉） -->
      <div class="screen-col screen-col--map">
        <div class="screen-map-zone">
          <button class="screen-zoom-btn" type="button" title="放大查看" @click="openExpand('纽约州医院分布地图', buildMapOption)">⤢</button>
          <div class="screen-adv-head">
            <h3>纽约州医院分布地图 · 数据来源医院位置</h3>
            <p>橙色脉冲光点为数据中的医院，悬停可查看该医院的住院人次与平均费用；点击光点可筛选该医院；滚轮缩放 / 拖拽移动，县区颜色越深住院人次越多，灰色为周边邻州</p>
          </div>
          <div ref="mapRef" class="screen-adv-chart screen-adv-chart--map"></div>
        </div>
      </div>

      <!-- 右列图表 -->
      <div class="screen-col">
        <div class="screen-panel">
          <ChartCard title="年龄段分布" :option="ageOption" height="176px" theme="dark" />
        </div>
        <div class="screen-panel">
          <ChartCard title="严重程度" :option="severityOption" height="176px" theme="dark" />
        </div>
      </div>
    </section>

    <section class="screen-bottom">
      <div class="screen-panel">
        <button class="screen-zoom-btn" type="button" title="放大查看" @click="openExpand('主要诊断类别 Top10', buildMdcOption)">⤢</button>
        <div class="screen-adv-head">
          <h3>主要诊断类别 Top{{ mdcSource.length }}</h3>
          <p>按 MDC 医学大分类统计住院人次排行（如循环系统、呼吸系统、妊娠分娩等），随筛选联动更新</p>
        </div>
        <div ref="mdcRef" class="screen-adv-chart screen-adv-chart--rank"></div>
      </div>
    </section>

    <!-- 图表放大查看遮罩 -->
    <div v-if="expanded" class="screen-expand-mask" @click.self="closeExpand">
      <div class="screen-expand-card">
        <header class="screen-expand-head">
          <h3>{{ expanded.title }}</h3>
          <button class="screen-action" type="button" @click="closeExpand">关闭</button>
        </header>
        <div ref="expandRef" class="screen-expand-chart"></div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.screen-panel--wide {
  grid-column: 1 / -1;
}

.screen-kpi-row {
  display: grid;
  grid-template-columns: repeat(4, minmax(0, 1fr));
  gap: 10px;
  margin-bottom: 12px;
}
.screen-kpi-box {
  border-radius: 10px;
  border: 1px solid rgba(0, 168, 255, 0.28);
  background: rgba(3, 15, 31, 0.72);
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.screen-kpi-box span {
  font-size: 13px;
  color: rgba(226, 240, 255, 0.75);
}
.screen-kpi-box strong {
  font-size: 24px;
  color: #7dd3fc;
  font-weight: 700;
  letter-spacing: 0.5px;
}

.screen-main-layout {
  display: grid;
  grid-template-columns: 1fr 1.8fr 1fr;
  gap: 12px;
  align-items: stretch;
  margin-bottom: 14px;
}
.screen-col {
  display: flex;
  flex-direction: column;
  gap: 12px;
  min-width: 0;
}
/* 左右列面板弹性拉伸：与中间地图等高铺满，上下两排图不留空档 */
.screen-col .screen-panel {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.screen-col .screen-panel .chart-card {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}
.screen-col .screen-panel :deep(.chart-root) {
  flex: 1;
  min-height: 0;
  width: 100%;
}
.screen-col--map .screen-map-zone {
  flex: 1;
  display: flex;
  flex-direction: column;
}
.screen-bottom {
  display: grid;
  grid-template-columns: 1fr;
  gap: 12px;
}

.screen-map-zone {
  position: relative;
  border-radius: 10px;
  border: 1px solid rgba(0, 168, 255, 0.28);
  background: rgba(3, 15, 31, 0.72);
  box-shadow: 0 18px 40px rgba(0, 0, 0, 0.35);
  padding: 12px 12px 10px;
  min-height: 660px;
}

.screen-adv-head {
  position: relative;
  z-index: 1;
  color: #e2f0ff;
  padding: 4px 8px 8px;
}
.screen-adv-head h3 {
  margin: 0 0 2px;
  font-size: 14px;
}
.screen-adv-head p {
  margin: 0;
  font-size: 12px;
  color: rgba(226, 240, 255, 0.72);
}

.screen-adv-chart {
  position: relative;
  z-index: 1;
  width: 100%;
}
.screen-adv-chart--map {
  flex: 1;
  min-height: 560px;
  width: 100%;
}
.screen-adv-chart--rank {
  height: 220px;
}

/* 图表放大图标按钮（各图表面板右上角，与 toolbox 下载图标同区域） */
.screen-zoom-btn {
  position: absolute;
  top: 6px;
  right: 10px;
  z-index: 3;
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  border: 1px solid rgba(114, 188, 255, 0.35);
  border-radius: 50%;
  background: rgba(3, 15, 31, 0.85);
  color: #b9d8ff;
  font-size: 14px;
  line-height: 1;
  padding: 0;
  cursor: pointer;
  transition: all 0.15s;
}
.screen-zoom-btn:hover {
  background: rgba(114, 188, 255, 0.25);
  color: #ffffff;
  border-color: rgba(114, 188, 255, 0.6);
}

/* 图表放大查看遮罩 */
.screen-expand-mask {
  position: fixed;
  inset: 0;
  z-index: 200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 28px;
  background: rgba(2, 6, 23, 0.82);
  backdrop-filter: blur(4px);
}
.screen-expand-card {
  width: min(1240px, 94vw);
  height: min(780px, 90vh);
  display: flex;
  flex-direction: column;
  padding: 16px 18px 18px;
  border: 1px solid rgba(0, 168, 255, 0.35);
  border-radius: 14px;
  background: #04101f;
  box-shadow: 0 30px 90px rgba(0, 0, 0, 0.6);
}
.screen-expand-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  color: #e2f0ff;
}
.screen-expand-head h3 {
  margin: 0;
  font-size: 18px;
}
.screen-expand-head .screen-action {
  align-self: center;
  padding: 8px 18px;
}
.screen-expand-chart {
  flex: 1;
  min-height: 0;
  width: 100%;
}

@media (max-width: 1100px) {
  .screen-main-layout {
    grid-template-columns: 1fr;
  }
  .screen-bottom {
    grid-template-columns: 1fr;
  }
  .screen-col--map .screen-map-zone {
    min-height: 400px;
  }
  /* 单列布局下两侧面板恢复内容自适应高度，避免图表塌陷 */
  .screen-col .screen-panel {
    flex: none;
  }
  .screen-col .screen-panel :deep(.chart-root) {
    flex: none;
    height: 220px;
  }
}

@media (max-width: 860px) {
  .screen-kpi-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
  .screen-adv-chart--map {
    min-height: 340px;
  }
}
</style>
