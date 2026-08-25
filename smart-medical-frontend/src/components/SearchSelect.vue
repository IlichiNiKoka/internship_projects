<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

interface OptionItem {
  label: string
  value: string
}

const props = withDefaults(
  defineProps<{
    options: OptionItem[]
    modelValue: string[]
    placeholder?: string
    emptyText?: string
    /** 列表第一位的"全部"选项文案 */
    allLabel?: string
  }>(),
  {
    placeholder: '请选择',
    emptyText: '无匹配选项',
    allLabel: '全部',
  },
)

const emit = defineEmits<{ (e: 'update:modelValue', v: string[]): void }>()

const open = ref(false)
const keyword = ref('')
const rootRef = ref<HTMLDivElement | null>(null)

// 选项按名称排序（不区分大小写），保证列表始终有序
const sortedOptions = computed(() =>
  [...props.options].sort((a, b) => a.label.localeCompare(b.label, 'en', { sensitivity: 'base' })),
)

const filteredOptions = computed(() => {
  const kw = keyword.value.trim().toLowerCase()
  if (!kw) {
    return sortedOptions.value
  }
  return sortedOptions.value.filter((o) => o.label.toLowerCase().includes(kw))
})

const selectedSet = computed(() => new Set(props.modelValue))

function labelOf(value: string): string {
  return props.options.find((o) => o.value === value)?.label ?? value
}

function toggle(value: string) {
  const next = new Set(props.modelValue)
  if (next.has(value)) {
    next.delete(value)
  } else {
    next.add(value)
  }
  emit('update:modelValue', [...next])
}

function clearAll() {
  emit('update:modelValue', [])
}

function onDocClick(e: MouseEvent) {
  if (rootRef.value && !rootRef.value.contains(e.target as Node)) {
    open.value = false
    keyword.value = ''
  }
}

function togglePanel() {
  open.value = !open.value
  if (open.value) {
    keyword.value = ''
  }
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))
</script>

<template>
  <div ref="rootRef" class="search-select">
    <button type="button" class="ss-trigger" :class="{ 'is-open': open }" @click="togglePanel">
      <span class="ss-trigger-text">
        <template v-if="modelValue.length === 0">
          <span class="ss-placeholder">{{ placeholder }}</span>
        </template>
        <template v-else>
          <span v-for="v in modelValue.slice(0, 3)" :key="v" class="ss-chip" :title="labelOf(v)">
            {{ labelOf(v) }}
          </span>
          <span v-if="modelValue.length > 3" class="ss-more">+{{ modelValue.length - 3 }}</span>
        </template>
      </span>
      <span class="ss-caret">▾</span>
    </button>

    <div v-if="open" class="ss-panel">
      <div class="ss-search">
        <input v-model="keyword" type="text" placeholder="搜索…" />
        <button v-if="modelValue.length" type="button" class="ss-clear" @click="clearAll">清空</button>
      </div>
      <div class="ss-list">
        <label class="ss-item ss-item--all" :title="allLabel">
          <input type="checkbox" :checked="modelValue.length === 0" @change="clearAll" />
          <span class="ss-item-label">{{ allLabel }}</span>
          <span v-if="modelValue.length > 0" class="ss-all-reset">点此恢复全部</span>
        </label>
        <label v-for="opt in filteredOptions" :key="opt.value" class="ss-item">
          <input type="checkbox" :checked="selectedSet.has(opt.value)" @change="toggle(opt.value)" />
          <span class="ss-item-label" :title="opt.label">{{ opt.label }}</span>
        </label>
        <p v-if="filteredOptions.length === 0" class="ss-empty">{{ emptyText }}</p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.search-select {
  position: relative;
  width: 100%;
  min-width: 0;
}

/* 触发按钮（固定高度，选中多项时高度/位置不变；文字单行溢出省略） */
.ss-trigger {
  width: 100%;
  height: 40px;
  min-height: 40px;
  box-sizing: border-box;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 0 10px;
  border-radius: 8px;
  border: 1px solid rgba(114, 188, 255, 0.22);
  background: rgba(3, 15, 31, 0.9);
  color: #e2f0ff;
  cursor: pointer;
  text-align: left;
  font: inherit;
  transition: border-color 0.15s;
}
.ss-trigger:hover,
.ss-trigger.is-open {
  border-color: rgba(114, 188, 255, 0.55);
}
.ss-trigger-text {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 4px;
  overflow: hidden;
  white-space: nowrap;
}
.ss-placeholder {
  color: rgba(226, 240, 255, 0.55);
  font-size: 13px;
}
.ss-chip {
  max-width: 130px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  padding: 2px 8px;
  border-radius: 999px;
  background: rgba(114, 188, 255, 0.2);
  color: #dbeafe;
}
.ss-more {
  font-size: 12px;
  color: #b9d8ff;
}
.ss-caret {
  flex: none;
  color: #b9d8ff;
  font-size: 12px;
}

/* 下拉面板 */
.ss-panel {
  position: absolute;
  top: calc(100% + 4px);
  left: 0;
  right: 0;
  z-index: 120;
  border-radius: 10px;
  border: 1px solid rgba(114, 188, 255, 0.35);
  background: #081426;
  box-shadow: 0 18px 44px rgba(0, 0, 0, 0.5);
  overflow: hidden;
}

/* 搜索行 */
.ss-search {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 8px;
  border-bottom: 1px solid rgba(114, 188, 255, 0.15);
}
.ss-search input {
  flex: 1;
  min-width: 0;
  font: inherit;
  font-size: 13px;
  padding: 6px 10px;
  border-radius: 6px;
  border: 1px solid rgba(114, 188, 255, 0.25);
  background: rgba(3, 15, 31, 0.9);
  color: #e2f0ff;
  outline: none;
}
.ss-search input:focus {
  border-color: rgba(114, 188, 255, 0.6);
}
.ss-search input::placeholder {
  color: rgba(226, 240, 255, 0.45);
}
.ss-clear {
  flex: none;
  font-size: 12px;
  padding: 4px 10px;
  border-radius: 6px;
  border: 1px solid rgba(114, 188, 255, 0.25);
  background: transparent;
  color: #b9d8ff;
  cursor: pointer;
}
.ss-clear:hover {
  background: rgba(114, 188, 255, 0.15);
  color: #fff;
}

/* 选项列表：右侧滚动条 */
.ss-list {
  max-height: 240px;
  overflow-y: auto;
  padding: 4px;
}
.ss-list::-webkit-scrollbar {
  width: 8px;
}
.ss-list::-webkit-scrollbar-track {
  background: transparent;
}
.ss-list::-webkit-scrollbar-thumb {
  background: rgba(114, 188, 255, 0.28);
  border-radius: 999px;
}
.ss-list::-webkit-scrollbar-thumb:hover {
  background: rgba(114, 188, 255, 0.5);
}

.ss-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 8px;
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  color: #d7e6ff;
}
.ss-item:hover {
  background: rgba(114, 188, 255, 0.12);
}
/* 列表第一位的"全部"选项 */
.ss-item--all {
  position: sticky;
  top: 0;
  z-index: 1;
  background: #0a1830;
  border-bottom: 1px solid rgba(114, 188, 255, 0.15);
  font-weight: 600;
  color: #eaf4ff;
  margin-bottom: 2px;
}
.ss-item--all:hover {
  background: #0e1f3d;
}
.ss-all-reset {
  margin-left: auto;
  font-size: 11px;
  font-weight: 400;
  color: rgba(56, 189, 248, 0.85);
}
.ss-item input[type='checkbox'] {
  flex: none;
  accent-color: #38bdf8;
  width: 15px;
  height: 15px;
  cursor: pointer;
}
.ss-item-label {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.ss-empty {
  margin: 0;
  padding: 14px 10px;
  text-align: center;
  font-size: 12.5px;
  color: rgba(226, 240, 255, 0.5);
}
</style>
