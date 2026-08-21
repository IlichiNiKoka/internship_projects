<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import { computed } from 'vue'

import ChartCard from '../components/ChartCard.vue'
import KpiCard from '../components/KpiCard.vue'
import { useDashboardStore } from '../store/dashboard'

const store = useDashboardStore()
const payload = computed(() => store.payload)
const overview = computed(() => payload.value?.overview)
const dashboard = computed(() => payload.value?.dashboard)

const ageOption = computed<EChartsOption>(() => ({
  tooltip: { trigger: 'axis' },
  grid: { left: 40, right: 16, top: 28, bottom: 32 },
  xAxis: {
    type: 'category',
    data: dashboard.value?.ageDistribution.map((item) => item.name) ?? [],
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
      data: dashboard.value?.ageDistribution.map((item) => item.value) ?? [],
      itemStyle: { color: '#4f73d9', borderRadius: [6, 6, 0, 0] },
    },
  ],
}))

const paymentOption = computed<EChartsOption>(() => ({
  tooltip: { trigger: 'item' },
  legend: { bottom: 0, icon: 'circle', itemWidth: 8, itemHeight: 8, textStyle: { color: '#6b7280' } },
  series: [
    {
      type: 'pie',
      radius: ['42%', '66%'],
      center: ['50%', '44%'],
      label: { show: false },
      data: (dashboard.value?.paymentDistribution.slice(0, 6) ?? []).map((item) => ({
        name: item.name,
        value: item.value,
      })),
    },
  ],
}))

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
    data: (dashboard.value?.topDiagnoses.slice(0, 6) ?? []).map((item) => item.name),
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
      <ChartCard title="年龄结构" subtitle="各年龄段住院人次" :option="ageOption" />
      <ChartCard title="支付方式构成" subtitle="主要支付渠道占比" :option="paymentOption" />
    </section>

    <section class="chart-grid chart-grid--2">
      <ChartCard title="重点病种 Top 6" subtitle="按出院人次排序" :option="diagnosisOption" />
      <ChartCard title="严重程度 × 死亡风险" subtitle="联合分布热力图" :option="severityOption" />
    </section>

    <section class="overview-footnote">
      <span>数据源：平台清洗汇总 JSON（{{ payload?.meta.cleanRows.toLocaleString() }} 条记录）</span>
      <span>生成时间：{{ payload?.meta.generatedAt }}</span>
    </section>
  </div>
</template>
