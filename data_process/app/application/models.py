# -*- coding: utf-8 -*-
"""会话、分析快照与报告的持久化数据模型。"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now() -> str:
    """返回带时区的 ISO-8601 时间，避免不同部署节点使用本地时区。"""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def new_id(prefix: str) -> str:
    """生成不包含敏感业务信息、适合放入 URL 的短标识。"""
    return f"{prefix}_{uuid.uuid4().hex[:20]}"


@dataclass
class ConversationMessage:
    id: str
    role: str
    content: str
    created_at: str = field(default_factory=utc_now)
    intent: str | None = None
    analysis_id: str | None = None
    report_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationMessage":
        return cls(
            id=str(data["id"]),
            role=str(data["role"]),
            content=str(data.get("content") or ""),
            created_at=str(data.get("created_at") or utc_now()),
            intent=data.get("intent"),
            analysis_id=data.get("analysis_id"),
            report_id=data.get("report_id"),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class AnalysisRecord:
    id: str
    message_id: str
    query: str
    intent: str
    tool_name: str
    tool_input: dict[str, Any]
    result: Any
    summary: dict[str, Any]
    attempts: int = 1
    elapsed_seconds: float = 0.0
    created_at: str = field(default_factory=utc_now)

    def to_dict(self, include_result: bool = True) -> dict[str, Any]:
        data = asdict(self)
        if not include_result:
            data.pop("result", None)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AnalysisRecord":
        return cls(
            id=str(data["id"]),
            message_id=str(data.get("message_id") or ""),
            query=str(data.get("query") or ""),
            intent=str(data.get("intent") or "unsupported"),
            tool_name=str(data.get("tool_name") or ""),
            tool_input=dict(data.get("tool_input") or {}),
            result=data.get("result"),
            summary=dict(data.get("summary") or {}),
            attempts=int(data.get("attempts") or 1),
            elapsed_seconds=float(data.get("elapsed_seconds") or 0.0),
            created_at=str(data.get("created_at") or utc_now()),
        )


@dataclass
class ConversationSession:
    id: str
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    version: int = 0
    messages: list[ConversationMessage] = field(default_factory=list)
    analyses: list[AnalysisRecord] = field(default_factory=list)
    reports: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.updated_at = utc_now()
        self.version += 1

    def latest_analysis(self) -> AnalysisRecord | None:
        return self.analyses[-1] if self.analyses else None

    def find_analysis(self, analysis_id: str | None) -> AnalysisRecord | None:
        if not analysis_id:
            return None
        return next((item for item in self.analyses if item.id == analysis_id), None)

    def find_report(self, report_id: str) -> dict[str, Any] | None:
        return next((item for item in self.reports if item.get("report_id") == report_id), None)

    def to_dict(
        self,
        *,
        include_analysis_results: bool = True,
        include_reports: bool = True,
    ) -> dict[str, Any]:
        return {
            "session_id": self.id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "metadata": dict(self.metadata),
            "messages": [item.to_dict() for item in self.messages],
            "analyses": [
                item.to_dict(include_result=include_analysis_results)
                for item in self.analyses
            ],
            "reports": list(self.reports) if include_reports else [
                {
                    "report_id": item.get("report_id"),
                    "title": item.get("title"),
                    "generated_at": item.get("generated_at"),
                }
                for item in self.reports
            ],
        }

    def to_storage_dict(self) -> dict[str, Any]:
        return self.to_dict(include_analysis_results=True, include_reports=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationSession":
        return cls(
            id=str(data.get("session_id") or data.get("id") or new_id("ses")),
            created_at=str(data.get("created_at") or utc_now()),
            updated_at=str(data.get("updated_at") or utc_now()),
            version=int(data.get("version") or 0),
            metadata=dict(data.get("metadata") or {}),
            messages=[
                ConversationMessage.from_dict(item)
                for item in data.get("messages") or []
            ],
            analyses=[
                AnalysisRecord.from_dict(item)
                for item in data.get("analyses") or []
            ],
            reports=[dict(item) for item in data.get("reports") or []],
        )
