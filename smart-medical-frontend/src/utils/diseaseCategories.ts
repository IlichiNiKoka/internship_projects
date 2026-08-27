/**
 * 疾病大类（APR-DRG MDC）中文名 + 大屏疾病筛选二级菜单分组逻辑。
 *
 * 分组依据：utils/diseaseMdcMap.ts（由 209 万行 SPARCS 数据离线统计生成，
 * 每个 CCSR 诊断取记录数最多的 MDC 作为其大类）。
 */

import { DISEASE_MDC_MAP } from './diseaseMdcMap'
import { diseaseNameCn, diseaseNameWithEn } from './diseaseNames'

// MDC 类别英文名 -> 中文名（源数据为英文，图表/菜单展示本地化为中文）
export const MDC_NAME_CN: Record<string, string> = {
  'DISEASES AND DISORDERS OF THE CIRCULATORY SYSTEM': '循环系统疾病',
  'PREGNANCY, CHILDBIRTH AND THE PUERPERIUM': '妊娠分娩与产褥期',
  'DISEASES AND DISORDERS OF THE RESPIRATORY SYSTEM': '呼吸系统疾病',
  'NEWBORNS AND OTHER NEONATES WITH CONDITIONS ORIGINATING IN THE PERINATAL PERIOD': '新生儿及围产期',
  'DISEASES AND DISORDERS OF THE DIGESTIVE SYSTEM': '消化系统疾病',
  'DISEASES AND DISORDERS OF THE MUSCULOSKELETAL SYSTEM AND CONNECTIVE TISSUE': '肌肉骨骼与结缔组织',
  'INFECTIOUS AND PARASITIC DISEASES (SYSTEMIC OR UNSPECIFIED SITES)': '感染与寄生虫病',
  'DISEASES AND DISORDERS OF THE NERVOUS SYSTEM': '神经系统疾病',
  'DISEASES AND DISORDERS OF THE KIDNEY AND URINARY TRACT': '肾脏与泌尿系统疾病',
  'MENTAL DISEASES AND DISORDERS': '精神疾病与障碍',
  'ENDOCRINE, NUTRITIONAL AND METABOLIC DISEASES AND DISORDERS': '内分泌营养代谢疾病',
  'DISEASES AND DISORDERS OF THE HEPATOBILIARY SYSTEM AND PANCREAS': '肝胆与胰腺疾病',
  'ALCOHOL/DRUG USE AND ALCOHOL/DRUG INDUCED ORGANIC MENTAL DISORDERS': '酒精/药物所致精神障碍',
  'DISEASES AND DISORDERS OF THE SKIN, SUBCUTANEOUS TISSUE AND BREAST': '皮肤皮下与乳腺疾病',
  'DISEASES AND DISORDERS OF THE BLOOD AND BLOOD FORMING ORGANS AND IMMUNOLOGICAL DISORDERS': '血液与免疫系统疾病',
  'INJURIES, POISONINGS AND TOXIC EFFECTS OF DRUGS': '损伤中毒与药物毒性',
  'FACTORS INFLUENCING HEALTH STATUS AND OTHER CONTACTS WITH HEALTH SERVICES': '健康影响因素与医疗接触',
  'MYELOPROLIFERATIVE DISEASES AND DISORDERS, AND POORLY DIFFERENTIATED NEOPLASM': '骨髓增殖与低分化肿瘤',
  'DISEASES AND DISORDERS OF THE EAR, NOSE, MOUTH AND THROAT': '耳鼻咽喉口腔疾病',
  'DISEASES AND DISORDERS OF THE FEMALE REPRODUCTIVE SYSTEM': '女性生殖系统疾病',
  'DISEASES AND DISORDERS OF THE MALE REPRODUCTIVE SYSTEM': '男性生殖系统疾病',
  'HUMAN IMMUNODEFICIENCY VIRUS INFECTIONS': '人类免疫缺陷病毒感染',
  'MULTIPLE SIGNIFICANT TRAUMA': '多发性严重创伤',
  'DISEASES AND DISORDERS OF THE EYE': '眼部疾病',
  'PRE MDC': '术前预分诊',
  'BURNS': '烧伤',
}

/** 兜底分组：未能归类到已知 MDC 的疾病统一放入「其他疾病」。 */
const FALLBACK_CATEGORY = '其他疾病'

export function mdcNameCn(name: string): string {
  return MDC_NAME_CN[name] ?? name
}

/** 单个疾病的大类中文名（未归类时回退「其他疾病」）。 */
export function diseaseCategoryCn(diseaseEn: string): string {
  const mdc = DISEASE_MDC_MAP[diseaseEn]
  return mdc ? mdcNameCn(mdc) : FALLBACK_CATEGORY
}

export interface DiseaseGroupItem {
  label: string
  value: string
  /** 悬浮提示：中文名 + 英文原文 */
  title: string
}

export interface DiseaseGroup {
  /** 大类中文名（二级菜单的组标题） */
  category: string
  /** 组内疾病（中文显示、英文值），按中文名排序 */
  items: DiseaseGroupItem[]
}

/**
 * 把全量疾病英文名列表按疾病大类分组，生成二级菜单数据。
 * 组按疾病数量降序排列（数量相同按中文名排序），组内疾病按中文名排序。
 */
export function groupDiseasesByCategory(diseases: string[]): DiseaseGroup[] {
  const groups = new Map<string, DiseaseGroupItem[]>()
  for (const d of diseases) {
    const category = diseaseCategoryCn(d)
    const list = groups.get(category)
    const item: DiseaseGroupItem = {
      label: diseaseNameCn(d),
      value: d,
      title: diseaseNameWithEn(d),
    }
    if (list) {
      list.push(item)
    } else {
      groups.set(category, [item])
    }
  }
  return [...groups.entries()]
    .map(([category, items]) => ({
      category,
      items: items.sort((a, b) => a.label.localeCompare(b.label, 'zh-Hans-CN')),
    }))
    .sort((a, b) => b.items.length - a.items.length || a.category.localeCompare(b.category, 'zh-Hans-CN'))
}
