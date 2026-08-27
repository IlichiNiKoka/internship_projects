# -*- coding: utf-8 -*-
"""生成前端 diseaseMdcMap.ts：CCSR 诊断 -> MDC 大类 映射（数据驱动）。"""
import json
import os

mapping = json.load(open(os.path.join(os.environ["TEMP"], "disease_mdc.json"), encoding="utf-8"))
lines = [
    "/**",
    " * CCSR 诊断描述 -> APR-DRG MDC 疾病大类 映射（离线由 209 万行 SPARCS 数据统计生成：",
    " * 每个 CCSR 诊断取记录数最多的 MDC 作为其大类）。",
    " * 用于大屏疾病筛选的二级菜单分组。",
    " */",
    "",
    "export const DISEASE_MDC_MAP: Record<string, string> = {",
]
for ccsr in sorted(mapping):
    mdc = mapping[ccsr]
    ccsr_esc = ccsr.replace("\\", "\\\\").replace("'", "\\'")
    mdc_esc = mdc.replace("\\", "\\\\").replace("'", "\\'")
    lines.append("  '%s': '%s'," % (ccsr_esc, mdc_esc))
lines.append("}")
out = "smart-medical-frontend/src/utils/diseaseMdcMap.ts"
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(lines) + "\n")
print("written", out, len(mapping), "entries")
