<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import { computed, ref, watch } from 'vue'

import ChartCard from '../components/ChartCard.vue'
import KpiCard from '../components/KpiCard.vue'
import { useDashboardStore } from '../store/dashboard'
import { diseaseNameCn } from '../utils/diseaseNames'

const store = useDashboardStore()
const payload = computed(() => store.payload)
const overview = computed(() => payload.value?.overview)
const dashboard = computed(() => payload.value?.dashboard)

// ---------------------------------------------------------------
// 年龄结构 × 性别：合并柱状图（可选性别图例）
//  - 默认「全部」：展示全年龄分布（静态汇总）
//  - 选择男/女：在线模式下读取后端 age×gender 交叉聚合；离线时禁用性别选项
// ---------------------------------------------------------------
type GenderFilter = '全部' | 'Male' | 'Female'
const genderOptions: Array<{ label: string; value: GenderFilter }> = [
  { label: '全部', value: '全部' },
  { label: '男', value: 'Male' },
  { label: '女', value: 'Female' },
]
const genderFilter = ref<GenderFilter>('全部')

const ageGenderReady = computed(
  () => store.apiAvailable === true && (store.ageGenderDistribution?.length ?? 0) > 0,
)

// 后端在线后自动加载交叉数据；离线时清空（由 UI 禁用性别选项）
watch(
  () => store.apiAvailable,
  (available) => {
    if (available === true) {
      store.loadAgeGenderDistribution()
    } else {
      store.ageGenderDistribution = null
    }
  },
  { immediate: true },
)

const ageGenderSubtitle = computed(() => {
  if (genderFilter.value === '全部') {
    return ageGenderReady.value ? '各年龄段住院人次 · 全部患者' : '各年龄段住院人次（后端离线，仅全量数据）'
  }
  return `${genderFilter.value === 'Male' ? '男性' : '女性'}各年龄段住院人次 · 在线聚合`
})

const ageGenderOption = computed<EChartsOption>(() => {
  const ages = dashboard.value?.ageDistribution ?? []
  const values = ages.map((item) => {
    if (genderFilter.value === '全部' || !ageGenderReady.value) {
      return item.value
    }
    const hit = (store.ageGenderDistribution ?? []).find(
      (row) => row.gender === genderFilter.value && row.age === item.name,
    )
    return hit?.value ?? 0
  })
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 40, right: 16, top: 28, bottom: 32 },
    xAxis: {
      type: 'category',
      data: ages.map((item) => item.name),
      axisLabel: { color: '#6b7280' },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#6b7280' },
      splitLine: { lineStyle: { color: '#eef1f8' } },
    },
    series: [
      {
        type: 'bar',
        barWidth: '46%',
        data: values,
        itemStyle: { color: '#4f73d9', borderRadius: [6, 6, 0, 0] },
      },
    ],
  }
})

// ---------------------------------------------------------------
// 名称过长时截断，避免图表轴标签溢出
// ---------------------------------------------------------------
function shortenLabel(name: string, max = 16) {
  return name.length > max ? `${name.slice(0, max)}…` : name
}

const diagnosisOption = computed<EChartsOption>(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 150, right: 24, top: 16, bottom: 28 },
  xAxis: {
    type: 'value',
    axisLabel: { color: '#6b7280' },
    splitLine: { lineStyle: { color: '#eef1f8' } },
  },
  yAxis: {
    type: 'category',
    inverse: true,
    // 重点病种名称本地化为中文显示
    data: (dashboard.value?.topDiagnoses.slice(0, 6) ?? []).map((item) => diseaseNameCn(item.name)),
    axisLabel: { color: '#6b7280' },
  },
  series: [
    {
      type: 'bar',
      barWidth: 14,
      data: (dashboard.value?.topDiagnoses.slice(0, 6) ?? []).map((item) => item.dischargeCount),
      itemStyle: { color: '#16a34a', borderRadius: [0, 6, 6, 0] },
    },
  ],
}))

// ---------------------------------------------------------------
// 重点县区住院人次（由可视化大屏移入）：折线 + 面积
// ---------------------------------------------------------------
const topCountyOption = computed<EChartsOption>(() => {
  const items = dashboard.value?.topCounties ?? []
  return {
    tooltip: { trigger: 'axis' },
    grid: { left: 48, right: 16, top: 28, bottom: 64 },
    dataZoom: [{ type: 'inside' }, { type: 'slider', height: 12, bottom: 8 }],
    xAxis: {
      type: 'category',
      data: items.map((item) => item.name),
      axisLabel: { color: '#6b7280', rotate: 18 },
    },
    yAxis: {
      type: 'value',
      axisLabel: { color: '#6b7280' },
      splitLine: { lineStyle: { color: '#eef1f8' } },
    },
    series: [
      {
        type: 'line',
        smooth: true,
        itemStyle: { color: '#0ea5e9' },
        areaStyle: { color: 'rgba(14, 165, 233, 0.14)' },
        data: items.map((item) => item.dischargeCount),
      },
    ],
  }
})

// ---------------------------------------------------------------
// Top 医院 3D 立体视图（由可视化大屏移入）：渐变双柱模拟立体
// ---------------------------------------------------------------
const topHospitalOption = computed<EChartsOption>(() => {
  const items = (dashboard.value?.topFacilities ?? []).slice(0, 8)
  return {
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params: unknown) => {
        const list = params as Array<{ name: string; value: number }>
        const idx = list.length ? Math.min(list.length - 1, 0) : 0
        const p = list[idx]
        const hospital = items.find((i) => shortenLabel(i.name, 9) === p?.name)
        const charges = hospital?.avgCharges == null ? '—' : Number(hospital.avgCharges).toLocaleString()
        return `${hospital?.name ?? p?.name}<br/>住院人次：${(p?.value ?? 0).toLocaleString()}<br/>平均费用：$${charges}`
      },
    },
    grid: { left: 62, right: 16, top: 36, bottom: 66 },
    xAxis: {
      type: 'category',
      data: items.map((i) => shortenLabel(i.name, 9)),
      axisLabel: { color: '#6b7280', rotate: 18, fontSize: 10 },
    },
    yAxis: {
      type: 'value',
      name: '住院人次',
      nameTextStyle: { color: '#6b7280' },
      axisLabel: { color: '#6b7280' },
      splitLine: { lineStyle: { color: '#eef1f8' } },
    },
    series: [
      {
        name: '住院人次',
        type: 'bar',
        barWidth: 22,
        z: 3,
        itemStyle: {
          color: {
            type: 'linear',
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: '#38bdf8' },
              { offset: 1, color: '#0369a1' },
            ],
          },
          borderRadius: [4, 4, 0, 0],
        },
        data: items.map((i) => i.dischargeCount),
      },
      {
        name: '底座',
        type: 'bar',
        barWidth: 22,
        barGap: '-100%',
        z: 2,
        itemStyle: { color: 'rgba(2, 132, 199, 0.28)', borderRadius: [4, 4, 0, 0] },
        data: items.map((i) => Math.round(i.dischargeCount * 0.92)),
      },
    ],
  }
})

const severityOption = computed<EChartsOption>(() => {
  const matrix = dashboard.value?.severityMortalityMatrix
  if (!matrix) {
    return {}
  }
  const max = Math.max(...matrix.values.map((item) => item.count), 1)
  return {
    tooltip: { trigger: 'item' },
    grid: { left: 72, right: 16, top: 24, bottom: 64 },
    xAxis: { type: 'category', data: matrix.xAxis, name: '死亡风险', axisLabel: { color: '#6b7280' } },
    yAxis: { type: 'category', data: matrix.yAxis, name: '严重程度', axisLabel: { color: '#6b7280' } },
    visualMap: {
      min: 0,
      max,
      orient: 'horizontal',
      left: 'center',
      bottom: 4,
      inRange: { color: ['#eef2ff', '#93b4f7', '#4f73d9', '#1e3a8a'] },
    },
    series: [
      {
        type: 'heatmap',
        data: matrix.values.map((item) => [
          matrix.xAxis.indexOf(item.mortality),
          matrix.yAxis.indexOf(item.severity),
          item.count,
        ]),
        label: { show: true, color: '#334155', fontSize: 11 },
      },
    ],
  }
})
</script>

<template>
  <div v-if="overview && dashboard" class="view-stack">
    <section class="kpi-grid">
      <KpiCard
        title="总住院人次"
        :value="overview.dischargeCount.toLocaleString()"
        hint="清洗后全部记录"
        tone="accent"
      />
      <KpiCard
        title="平均住院时长"
        :value="`${overview.avgLengthOfStay} 天`"
        hint="人均住院天数"
      />
      <KpiCard
        title="平均总费用"
        :value="`$${overview.avgTotalCharges.toLocaleString()}`"
        hint="人均医疗账单"
        tone="success"
      />
      <KpiCard
        title="急诊占比"
        :value="`${overview.emergencyRate}%`"
        hint="急诊入院患者比例"
      />
    </section>

    <section class="chart-grid chart-grid--2">
      <ChartCard title="年龄结构 × 性别" :subtitle="ageGenderSubtitle" :option="ageGenderOption">
        <template #extra>
          <div class="gender-switch" role="group" aria-label="按性别筛选">
            <button
              v-for="opt in genderOptions"
              :key="opt.value"
              type="button"
              class="gender-btn"
              :class="{ 'is-active': genderFilter === opt.value }"
              :disabled="opt.value !== '全部' && !ageGenderReady"
              :title="opt.value !== '全部' && !ageGenderReady ? '后端离线，仅提供全量数据' : ''"
              @click="genderFilter = opt.value"
            >
              {{ opt.label }}
            </button>
          </div>
        </template>
      </ChartCard>
      <ChartCard title="重点病种 Top 6" subtitle="按出院人次排序" :option="diagnosisOption" />
    </section>

    <section class="chart-grid chart-grid--2">
      <ChartCard title="重点县区住院人次" subtitle="Top 10 县区（由可视化大屏移入）" :option="topCountyOption" />
      <ChartCard title="Top 医院 3D 立体视图" subtitle="住院人次 Top 8（由可视化大屏移入）" :option="topHospitalOption" />
    </section>

    <section class="chart-grid chart-grid--single">
      <ChartCard title="严重程度 × 死亡风险" subtitle="联合分布热力图（全量画像）" :option="severityOption" />
    </section>
  </div>
</template>

<style scoped>
.gender-switch {
  display: inline-flex;
  gap: 6px;
}
.gender-btn {
  font-size: 12.5px;
  line-height: 1;
  padding: 6px 12px;
  border-radius: 999px;
  border: 1px solid #dbe3f5;
  background: #f4f7ff;
  color: #4b5563;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.gender-btn:hover:not(:disabled) {
  border-color: #4f73d9;
  color: #4f73d9;
}
.gender-btn.is-active {
  background: #4f73d9;
  border-color: #4f73d9;
  color: #fff;
}
.gender-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}

@media (max-width: 720px) {
  :deep(.panel-header) {
    flex-direction: column;
  }
  .gender-switch {
    margin-top: 2px;
  }
}
</style>
