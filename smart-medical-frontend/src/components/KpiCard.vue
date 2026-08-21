<script setup lang="ts">
defineProps<{
  title: string
  value: string
  hint: string
  tone?: 'default' | 'accent' | 'success'
}>()
</script>

<template>
  <article class="kpi-card" :data-tone="tone ?? 'default'">
    <p class="kpi-title">{{ title }}</p>
    <strong class="kpi-value">{{ value }}</strong>
    <p class="kpi-hint">{{ hint }}</p>
  </article>
</template>
