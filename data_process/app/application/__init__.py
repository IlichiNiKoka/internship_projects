# -*- coding: utf-8 -*-
"""人员1 AI 应用编排层。

本包只负责把人员3的分析能力与人员4的意图/摘要能力组织成可供前端使用的
完整应用，不在这里实现统计口径或模型推理。
"""

from app.application.service import MedicalAssistantService

__all__ = ["MedicalAssistantService"]
