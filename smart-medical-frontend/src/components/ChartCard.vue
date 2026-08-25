<script setup lang="ts">
import type { EChartsOption } from 'echarts'
import * as echarts from 'echarts'
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'

const props = withDefaults(
  defineProps<{
    title: string
    subtitle?: string
    option: EChartsOption
    height?: string
    theme?: 'light' | 'dark'
  }>(),
  {
    subtitle: '',
    height: '320px',
    theme: 'light',
  },
)

const rootRef = ref<HTMLDivElement | null>(null)
let chartInstance: echarts.ECharts | null = null
let resizeObserver: ResizeObserver | null = null

function renderChart() {
  if (!rootRef.value) {
    return
  }

  if (!chartInstance) {
    chartInstance = echarts.init(rootRef.value, props.theme === 'dark' ? 'dark' : undefined)
  }

  chartInstance.setOption(props.option, true)
}

function handleResize() {
  chartInstance?.resize()
}

watch(
  () => props.option,
  () => {
    renderChart()
  },
  { deep: true },
)

watch(
  () => props.theme,
  () => {
    if (!rootRef.value) {
      return
    }
    chartInstance?.dispose()
    chartInstance = echarts.init(rootRef.value, props.theme === 'dark' ? 'dark' : undefined)
    renderChart()
  },
)

onMounted(() => {
  renderChart()
  window.addEventListener('resize', handleResize)
  // 容器尺寸变化（如弹性布局拉伸）时自动 resize 图表
  if (rootRef.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => {
      chartInstance?.resize()
    })
    resizeObserver.observe(rootRef.value)
  }
})

onBeforeUnmount(() => {
  window.removeEventListener('resize', handleResize)
  resizeObserver?.disconnect()
  resizeObserver = null
  chartInstance?.dispose()
  chartInstance = null
})
</script>

<template>
  <section class="panel chart-card">
    <header class="panel-header">
      <div>
        <h3>{{ title }}</h3>
        <p v-if="subtitle">{{ subtitle }}</p>
      </div>
      <div class="panel-header-extra">
        <slot name="extra" />
      </div>
    </header>
    <div ref="rootRef" class="chart-root" :style="{ height }"></div>
  </section>
</template>
