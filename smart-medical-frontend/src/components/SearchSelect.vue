<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

interface OptionItem {
  label: string
  value: string
  /** 悬浮提示全文（如中文名 + 英文原文），缺省用 label */
  title?: string
}

/** 二级菜单分组（大屏疾病选单：疾病大类 -> 具体疾病） */
interface OptionGroup {
  category: string
  items: OptionItem[]
}

const props = withDefaults(
  defineProps<{
    /** 扁平选项列表（无分组模式，如医院选单） */
    options?: OptionItem[]
    /** 二级分组选项（有分组时优先于 options，如疾病选单） */
    groups?: OptionGroup[]
    modelValue: string[]
    placeholder?: string
    emptyText?: string
    /** 列表第一位的"全部"选项文案 */
    allLabel?: string
  }>(),
  {
    options: () => [],
    groups: () => [],
    placeholder: '请选择',
    emptyText: '无匹配选项',
    allLabel: '全部',
  },
)

const emit = defineEmits<{ (e: 'update:modelValue', v: string[]): void }>()

const open = ref(false)
const keyword = ref('')
const rootRef = ref<HTMLDivElement | null>(null)

// 扁平化全量选项（分组模式下由 groups 展开，供检索与已选回显）
const flatOptions = computed<OptionItem[]>(() => {
  if (props.groups.length > 0) {
    return props.groups.flatMap((g) => g.items)
  }
  return props.options
})

// 分组模式下各组的扁平选项，用于组内过滤
const flatGroups = computed<OptionGroup[]>(() => {
  if (props.groups.length > 0) {
    return props.groups
  }
  return []
})

const keywordLower = computed(() => keyword.value.trim().toLowerCase())

// 无分组：扁平选项按名称排序（不区分大小写），保证列表始终有序
const sortedOptions = computed(() =>
  [...props.options].sort((a, b) => a.label.localeCompare(b.label, 'en', { sensitivity: 'base' })),
)

// 无分组：关键字过滤（同时匹配中文显示名与英文原值）
const filteredOptions = computed(() => {
  const kw = keywordLower.value
  if (!kw) {
    return sortedOptions.value
  }
  return sortedOptions.value.filter(
    (o) => o.label.toLowerCase().includes(kw) || o.value.toLowerCase().includes(kw),
  )
})

// 分组：过滤后保留非空组（组标题不参与过滤，仅匹配组内疾病名）
const filteredGroups = computed<OptionGroup[]>(() => {
  const kw = keywordLower.value
  if (!kw) {
    return flatGroups.value
  }
  return flatGroups.value
    .map((g) => ({
      category: g.category,
      items: g.items.filter(
        (o) => o.label.toLowerCase().includes(kw) || o.value.toLowerCase().includes(kw),
      ),
    }))
    .filter((g) => g.items.length > 0)
})

const selectedSet = computed(() => new Set(props.modelValue))

function labelOf(value: string): string {
  return flatOptions.value.find((o) => o.value === value)?.label ?? value
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
        <!-- 二级菜单模式：大类分组标题 + 组内疾病 -->
        <template v-if="groups.length > 0">
          <div v-for="group in filteredGroups" :key="group.category" class="ss-group">
            <div class="ss-group-head">
              <span class="ss-group-title">{{ group.category }}</span>
              <span class="ss-group-count">{{ group.items.length }}</span>
            </div>
            <label v-for="opt in group.items" :key="opt.value" class="ss-item ss-item--grouped">
              <input type="checkbox" :checked="selectedSet.has(opt.value)" @change="toggle(opt.value)" />
              <span class="ss-item-label" :title="opt.title ?? opt.label">{{ opt.label }}</span>
            </label>
          </div>
        </template>
        <!-- 扁平模式：直接平铺选项 -->
        <template v-else>
          <label v-for="opt in filteredOptions" :key="opt.value" class="ss-item">
            <input type="checkbox" :checked="selectedSet.has(opt.value)" @change="toggle(opt.value)" />
            <span class="ss-item-label" :title="opt.title ?? opt.label">{{ opt.label }}</span>
          </label>
        </template>
        <p v-if="filteredOptions.length === 0 && filteredGroups.length === 0" class="ss-empty">
          {{ emptyText }}
        </p>
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
  max-height: 320px;
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
  z-index: 3;
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

/* 二级菜单：大类分组标题（吸顶 + 淡色底，滚动时保持可见） */
.ss-group-head {
  position: sticky;
  top: 0;
  z-index: 1;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 5px 8px;
  margin: 4px 0 2px;
  border-radius: 6px;
  background: rgba(9, 26, 51, 0.98);
  border-bottom: 1px solid rgba(114, 188, 255, 0.18);
  cursor: default;
}
.ss-group-title {
  font-size: 12.5px;
  font-weight: 700;
  color: #7dd3fc;
  letter-spacing: 0.5px;
}
.ss-group-count {
  flex: none;
  font-size: 11px;
  color: rgba(185, 216, 255, 0.6);
  background: rgba(114, 188, 255, 0.12);
  border-radius: 999px;
  padding: 1px 8px;
}
.ss-item--grouped {
  padding-left: 18px;
  font-size: 12.5px;
}
</style>
