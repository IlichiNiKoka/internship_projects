<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import { computed, onMounted, onUnmounted, reactive, ref } from 'vue'

import ChartCard from '../components/ChartCard.vue'
import { useDashboardStore } from '../store/dashboard'
import type { FilterState } from '../types/dashboard'

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
  disease: '全部疾病',
  age: '全部年龄',
  hospital: '全部医院',
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
const diagnosisSource = computed(() => screen.value?.topDiagnoses ?? dashboard.value?.topDiagnoses ?? [])

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
})

onUnmounted(() => {
  if (timer) {
    window.clearInterval(timer)
    timer = null
  }
})

const diseaseOptions = computed(() => ['全部疾病', ...(dashboard.value?.topDiagnoses.slice(0, 8).map((d) => d.name) ?? [])])
const ageOptions = computed(() => ['全部年龄', ...(dashboard.value?.ageDistribution.map((d) => d.name) ?? [])])
const hospitalOptions = computed(() => ['全部医院', ...(dashboard.value?.topFacilities.slice(0, 8).map((d) => d.name) ?? [])])

const chartToolbox = {
  feature: {
    saveAsImage: { title: '下载图表' },
    dataZoom: { title: { zoom: '区域缩放', back: '重置缩放' } },
    restore: { title: '重置' },
  },
}

async function applyFilters() {
  screenLoading.value = true
  screenError.value = ''

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
          `疾病：${filters.disease} / 年龄：${filters.age} / 医院：${filters.hospital} / 年份：${filters.year}` +
          ` · 后端实时聚合 ${data.dischargeCount.toLocaleString()} 条记录（${data.computedAt} 计算）`
      }
    } else {
      appliedFilterSummary.value =
        `疾病：${filters.disease} / 年龄：${filters.age} / 医院：${filters.hospital} / 年份：${filters.year}` +
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

const kpiOption = computed<EChartsOption>(() => ({
  title: {
    text: '历史住院数据总览',
    left: 16,
    top: 12,
    textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
  },
  toolbox: chartToolbox,
  grid: { left: 16, right: 16, top: 52, bottom: 18 },
  xAxis: { show: false, min: 0, max: 1 },
  yAxis: { show: false, min: 0, max: 1 },
  series: [
    {
      type: 'scatter',
      symbolSize: 1,
      data: [],
      markPoint: {
        symbol: 'circle',
        symbolSize: 130,
        label: {
          color: '#e2f0ff',
          formatter: () => `${(kpiLos.value ?? 0).toFixed(1)}\n平均住院(天)`,
          fontSize: 13,
          lineHeight: 18,
        },
        itemStyle: {
          color: 'rgba(37, 99, 235, 0.18)',
          borderColor: 'rgba(96, 165, 250, 0.55)',
          borderWidth: 2,
        },
        data: [{ xAxis: 0.3, yAxis: 0.5 }],
      },
      markPoint2: undefined,
    } as any,
  ],
}))

const admissionOption = computed<EChartsOption>(() => ({
  title: {
    text: '入院类型分布',
    left: 16,
    top: 12,
    textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
  },
  toolbox: chartToolbox,
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
}))

const paymentOption = computed<EChartsOption>(() => ({
  title: {
    text: '支付方式构成',
    left: 16,
    top: 12,
    textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
  },
  toolbox: chartToolbox,
  tooltip: { trigger: 'item' },
  legend: {
    bottom: 8,
    textStyle: { color: '#b9d8ff' },
    itemWidth: 10,
    itemHeight: 10,
  },
  series: [
    {
      type: 'pie',
      radius: ['48%', '70%'],
      center: ['50%', '50%'],
      label: { color: '#e2f0ff' },
      data:
        paymentSource.value.slice(0, 6).map((d) => ({
          name: d.name,
          value: d.value,
        })) ?? [],
    },
  ],
}))

const countyOption = computed<EChartsOption>(() => ({
  title: {
    text: '重点县区住院人次',
    left: 16,
    top: 12,
    textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
  },
  toolbox: chartToolbox,
  tooltip: { trigger: 'axis' },
  grid: { left: 48, right: 18, top: 52, bottom: 60 },
  dataZoom: [{ type: 'inside' }, { type: 'slider', height: 10, bottom: 8 }],
  xAxis: {
    type: 'category',
    axisLabel: { color: '#b9d8ff', rotate: 18 },
    data: countySource.value.map((d) => d.name) ?? [],
  },
  yAxis: {
    type: 'value',
    axisLabel: { color: '#b9d8ff' },
    splitLine: { lineStyle: { color: 'rgba(114, 188, 255, 0.12)' } },
  },
  series: [
    {
      type: 'line',
      smooth: true,
      itemStyle: { color: '#98f5ff' },
      areaStyle: { color: 'rgba(114, 188, 255, 0.18)' },
      data: countySource.value.map((d) => d.dischargeCount) ?? [],
    },
  ],
}))

const severityOption = computed<EChartsOption>(() => ({
  title: {
    text: '病情严重程度统计',
    left: 16,
    top: 12,
    textStyle: { color: '#e2f0ff', fontSize: 14, fontWeight: 600 },
  },
  toolbox: chartToolbox,
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
}))
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
        <select v-model="filters.disease">
          <option v-for="item in diseaseOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="screen-field">
        <span>年龄</span>
        <select v-model="filters.age">
          <option v-for="item in ageOptions" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <label class="screen-field">
        <span>医院</span>
        <select v-model="filters.hospital">
          <option v-for="item in hospitalOptions" :key="item" :value="item">{{ item }}</option>
        </select>
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

    <section class="screen-grid">
      <div class="screen-panel">
        <div class="screen-kpi">
          <div class="screen-kpi-item">
            <span>总住院人次</span>
            <strong>{{ kpiDischarge.toLocaleString() }}</strong>
          </div>
          <div class="screen-kpi-item">
            <span>急诊占比</span>
            <strong>{{ kpiEmergency }}%</strong>
          </div>
          <div class="screen-kpi-item">
            <span>平均费用(美元)</span>
            <strong>{{ kpiCharges.toLocaleString() }}</strong>
          </div>
        </div>
        <ChartCard title="概要圆环" :option="kpiOption" height="220px" theme="dark" />
      </div>

      <div class="screen-panel">
        <ChartCard title="重点县区" :option="countyOption" height="320px" theme="dark" />
      </div>

      <div class="screen-panel">
        <ChartCard title="入院类型" :option="admissionOption" height="320px" theme="dark" />
      </div>

      <div class="screen-panel">
        <ChartCard title="支付方式" :option="paymentOption" height="320px" theme="dark" />
      </div>

      <div class="screen-panel">
        <div class="screen-word">
          <h3>高频关键词云（示意）</h3>
          <p>
            该区域用于后续接入疾病/科室/地区的关键词云或文本聚类结果。当前以“Top 疾病/县区/支付方式”作为替代展示。
          </p>
          <div class="screen-word-tags">
            <span v-for="item in diagnosisSource.slice(0, 8)" :key="item.name">{{ item.name }}</span>
          </div>
        </div>
      </div>

      <div class="screen-panel">
        <ChartCard title="严重程度" :option="severityOption" height="320px" theme="dark" />
      </div>
    </section>

    <section class="screen-footnote">
      <span>已支持：筛选、缩放、下载导出、加载提示、异常提示。</span>
      <span>若分析 API 可用，筛选动作会优先尝试联动在线聚合接口。</span>
    </section>
  </div>
</template>
