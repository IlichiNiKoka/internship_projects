# -*- coding: utf-8 -*-
"""人员1应用服务：工具调用、多轮上下文与医疗洞察报告的统一编排入口。"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

from app.ai.intent.catalog import INTENT_BY_KEY
from app.application.memory import (
    LangChainSessionMemory,
    SessionStore,
    build_session_store,
)
from app.application.models import (
    AnalysisRecord,
    ConversationMessage,
    ConversationSession,
    new_id,
)
from app.application.reports import MedicalReportService
from app.application.tools import (
    RetryPolicy,
    ToolCallResult,
    ToolExecutor,
    ToolInvocationError,
    ToolParameterError,
    ToolRegistry,
)
from app.core.exceptions import ConflictError, ResourceNotFoundError


_REFERENCE_RE = re.compile(
    r"(刚才|上次|上一轮|前面(?:的)?|这个结果|该结果|"
    r"上述(?:结果|分析)|前述(?:结果|分析)|继续(?:分析|看)?|"
    r"再按|再看|用它|其中|基于(?:该|这个|上述|前述)(?:结果|分析)?|"
    r"^那(?:么)?(?=按|再|就|看|分析|$))"
)
_REPORT_RE = re.compile(
    r"^\s*(?:(?:请|麻烦)(?:帮我)?|帮我)?\s*"
    r"(?:(?:基于|用).{0,12})?(?:生成|出|做|整理)"
    r".{0,18}(?:洞察报告|报告)(?:给我|看看)?[\s。！？?]*$|"
    r"^\s*(?:查看)?(?:刚才的|上次的)?(?:洞察报告|报告)"
    r"[\s。！？?]*$"
)
_REPORT_TERM_RE = re.compile(r"(洞察报告|报告)")
_REPORT_NEGATION_RE = re.compile(
    r"(?:不要|不用|无需|无须|不需要|别)"
    r".{0,8}(?:生成|出|做|整理|导出|查看|展示)?"
    r".{0,4}(?:洞察报告|报告)"
)
_REPORT_ACTION_RE = re.compile(
    r"(生成|出|做|整理|导出|查看|展示|发|给).{0,20}(洞察报告|报告)|"
    r"(洞察报告|报告).{0,8}(生成|导出|查看|展示|发|给)"
)
_EXPLAIN_RE = re.compile(r"(解释|说明|总结|解读|怎么看|意味着什么|详细一点)")


@dataclass
class ChatResult:
    session_id: str
    status: str
    user_message: dict[str, Any]
    assistant_message: dict[str, Any]
    intent: dict[str, Any]
    analysis: dict[str, Any] | None = None
    report: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)
    history_size: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "status": self.status,
            "user_message": self.user_message,
            "assistant_message": self.assistant_message,
            "intent": self.intent,
            "analysis": self.analysis,
            "report": self.report,
            "warnings": self.warnings,
            "context": self.context,
            "history_size": self.history_size,
        }


class MedicalAssistantService:
    """面向前端的有状态医疗分析助手。"""

    def __init__(
        self,
        *,
        intent_classifier,
        summary_generator,
        tool_executor: ToolExecutor,
        session_store: SessionStore,
        report_service: MedicalReportService,
        max_messages: int = 100,
        max_analyses: int = 20,
        max_reports: int = 10,
        max_result_rows: int = 200,
    ):
        self._classifier = intent_classifier
        self._generator = summary_generator
        self._executor = tool_executor
        self._store = session_store
        self._reports = report_service
        self._max_messages = max(4, int(max_messages))
        self._max_analyses = max(1, int(max_analyses))
        self._max_reports = max(1, int(max_reports))
        self._max_result_rows = max(1, int(max_result_rows))
        # 工具在服务装配时预注册；未安装/不兼容 LangChain 时保持
        # ToolExecutor 直接调用的无依赖回退。
        try:
            self._langchain_tools = {
                item.name: item for item in self._executor.as_langchain_tools()
            }
        except Exception:
            self._langchain_tools = {}

    # ------------------------------------------------------------------
    # 聊天主流程
    # ------------------------------------------------------------------
    def chat(
        self,
        message: str,
        *,
        session_id: str | None = None,
        analysis_id: str | None = None,
        generate_report: bool = False,
        request_id: str | None = None,
    ) -> ChatResult:
        explicit_session = session_id is not None
        session_id = session_id or new_id("ses")
        memory = LangChainSessionMemory(
            self._store, session_id, max_messages=self._max_messages
        )
        # SessionStore 自身已经提供进程内/Redis 同会话锁；只保留一层锁，
        # 避免为任意 session_id 永久缓存第二份 RLock。
        with memory.session_lock():
            session = memory.load_session()
            if session is None:
                if explicit_session:
                    raise ResourceNotFoundError(f"会话不存在或已过期: {session_id}")
                session = ConversationSession(id=session_id)

            idempotency_request = {
                "message": message,
                "analysis_id": analysis_id,
                "generate_report": bool(generate_report),
            }
            if request_id:
                replay = self._find_idempotent_result(
                    session, request_id, idempotency_request
                )
                if replay is not None:
                    return replay

            classified = self._classifier.classify(message)
            has_reference_phrase = bool(_REFERENCE_RE.search(message))
            report_negated = bool(_REPORT_NEGATION_RE.search(message))
            report_phrase = not report_negated and bool(
                _REPORT_RE.search(message) or _REPORT_ACTION_RE.search(message)
            )
            explain_phrase = bool(_EXPLAIN_RE.search(message))
            report_existing = report_phrase and (
                analysis_id is not None
                or has_reference_phrase
                or classified.intent == "unsupported"
            )
            explain_existing = explain_phrase and (
                analysis_id is not None
                or has_reference_phrase
                or classified.intent == "unsupported"
            )
            referenced = self._select_reference(
                session,
                message,
                analysis_id,
                allow_latest=report_existing or explain_existing,
            )
            metadata = {}
            if request_id:
                metadata = {
                    "request_id": request_id,
                    "idempotency_request": copy.deepcopy(idempotency_request),
                }
            user_message = ConversationMessage(
                id=new_id("msg"),
                role="user",
                content=message,
                metadata=metadata,
            )
            session.messages.append(user_message)

            needs_reference = (
                analysis_id is not None
                or has_reference_phrase
                or report_existing
                or explain_existing
            )
            if needs_reference and referenced is None:
                return self._clarification(
                    session,
                    user_message,
                    "当前会话还没有可引用的分析结果，请先提出一个医疗分析问题。",
                    reason="missing_analysis_reference",
                )

            # “生成刚才报告”直接引用已保存分析，不重新执行昂贵 Spark 工具。
            if report_existing:
                if referenced is None:
                    return self._clarification(
                        session,
                        user_message,
                        "当前会话还没有可用于生成报告的分析结果，请先提出一个医疗分析问题。",
                        reason="missing_analysis_reference",
                    )
                report = self._generate_report_locked(
                    session, [referenced.id], title=None
                )
                assistant = self._append_assistant(
                    session,
                    report["executive_summary"],
                    user_message.id,
                    analysis_id=referenced.id,
                    report_id=report["report_id"],
                    status="report_generated",
                )
                report_intent = {
                    "query": message,
                    "intent": "report_generation",
                    "intent_label": "医疗洞察报告生成",
                    "confidence": 1.0,
                    "params": {"analysis_ids": [referenced.id]},
                    "missing_required": [],
                    "downstream": "report",
                    "downstream_target": "medical_insight_report",
                    "matched_signals": {"report": ["报告命令"]},
                    "context_inherited": True,
                }
                context = {
                    "referenced_analysis_id": referenced.id,
                    "context_inherited": True,
                }
                self._record_idempotent_response(
                    user_message,
                    status="report_generated",
                    intent=report_intent,
                    warnings=[],
                    context=context,
                )
                self._save(session)
                return ChatResult(
                    session_id=session.id,
                    status="report_generated",
                    user_message=user_message.to_dict(),
                    assistant_message=assistant.to_dict(),
                    intent=report_intent,
                    analysis=referenced.to_dict(),
                    report=report,
                    context=context,
                    history_size=len(session.messages),
                )

            # 明确解读历史结果时，无论分类器是否命中原算法意图，
            # 都只重新摘要，不重跑人员3的昂贵分析。
            if explain_existing and referenced is not None:
                return self._resummarize(
                    session, user_message, referenced, classified
                )

            # “生成按医院统计报告”同时包含新分析意图：先执行本轮
            # 分析，再为新结果生成报告，不得误用上一轮结果。
            if (
                not report_negated
                and _REPORT_TERM_RE.search(message)
                and classified.intent != "unsupported"
            ):
                generate_report = True

            intent_key = classified.intent
            params = copy.deepcopy(classified.params)
            context_applied = False

            if referenced is not None and (analysis_id is not None or _REFERENCE_RE.search(message)):
                intent_key, params, context_applied = self._merge_follow_up(
                    message, classified, referenced
                )

            intent_payload = self._intent_payload(
                classified, intent_key, params, context_applied
            )

            if intent_key == "unsupported":
                answer = (
                    "当前助手支持医疗数据聚合、总体统计、疾病关联、住院费用预测、"
                    "再入院风险和平台能力查询。请补充希望分析的维度或指标。"
                )
                assistant = self._append_assistant(
                    session, answer, user_message.id, status="unsupported"
                )
                context = self._context_payload(referenced, context_applied)
                self._record_idempotent_response(
                    user_message,
                    status="unsupported",
                    intent=intent_payload,
                    warnings=[],
                    context=context,
                )
                self._save(session)
                return ChatResult(
                    session_id=session.id,
                    status="unsupported",
                    user_message=user_message.to_dict(),
                    assistant_message=assistant.to_dict(),
                    intent=intent_payload,
                    context=context,
                    history_size=len(session.messages),
                )

            if intent_payload.get("missing_required"):
                return self._clarification(
                    session,
                    user_message,
                    f"还需要补充以下分析条件：{', '.join(intent_payload['missing_required'])}。",
                    intent=intent_payload,
                    reason="missing_required_params",
                )

            try:
                tool_result = self._execute_tool(intent_key, params)
            except ToolParameterError as exc:
                return self._clarification(
                    session,
                    user_message,
                    str(exc),
                    intent=intent_payload,
                    reason="tool_parameter_error",
                    missing=exc.missing,
                )
            except ToolInvocationError as exc:
                warning = {
                    "code": "TOOL_INVOCATION_FAILED",
                    "tool": exc.tool_name,
                    "attempts": exc.attempts,
                    "trace_id": exc.trace_id,
                }
                answer = "分析服务暂时未能完成本次请求，请稍后重试。"
                if exc.trace_id:
                    answer += f" 追踪编号：{exc.trace_id}。"
                assistant = self._append_assistant(
                    session,
                    answer,
                    user_message.id,
                    status="failed",
                    metadata={"warning": warning},
                )
                context = self._context_payload(referenced, context_applied)
                self._record_idempotent_response(
                    user_message,
                    status="failed",
                    intent=intent_payload,
                    warnings=[warning],
                    context=context,
                )
                self._save(session)
                return ChatResult(
                    session_id=session.id,
                    status="failed",
                    user_message=user_message.to_dict(),
                    assistant_message=assistant.to_dict(),
                    intent=intent_payload,
                    warnings=[warning],
                    context=context,
                    history_size=len(session.messages),
                )

            intent_spec = INTENT_BY_KEY.get(intent_key)
            summary = self._generator.generate(
                user_query=message,
                intent_label=intent_spec.label_cn if intent_spec else intent_key,
                intent_key=intent_key,
                analysis_result=tool_result.summary_data,
            )
            summary_dict = summary.to_dict() if hasattr(summary, "to_dict") else dict(summary)
            warnings: list[dict[str, Any]] = []
            answer = str(summary_dict.get("text") or "分析已完成。")
            hallucination = summary_dict.get("hallucination") or {}
            if hallucination.get("passed") is not True:
                warnings.append({
                    "code": "UNTRUSTED_SUMMARY",
                    "message": "自动摘要未通过数字一致性校验，请以结构化分析结果为准",
                })
                answer = "分析已完成，但自动摘要未通过数字一致性校验，请以结构化结果为准。"
            elif summary_dict.get("fell_back_to_mock"):
                warnings.append({
                    "code": "LLM_FALLBACK",
                    "message": "本次使用确定性模板生成摘要",
                })

            assistant = self._append_assistant(
                session,
                answer,
                user_message.id,
                intent=intent_key,
                status="completed",
                metadata={"warnings": warnings},
            )
            assembled = self._compact_result(tool_result.assembled_result())
            record = AnalysisRecord(
                id=new_id("ana"),
                message_id=assistant.id,
                query=message,
                intent=intent_key,
                tool_name=tool_result.tool_name,
                tool_input=copy.deepcopy(tool_result.params),
                result=assembled,
                summary=summary_dict,
                attempts=tool_result.attempts,
                elapsed_seconds=tool_result.elapsed_seconds,
            )
            assistant.analysis_id = record.id
            session.analyses.append(record)

            report = None
            if generate_report:
                report = self._generate_report_locked(session, [record.id], title=None)
                assistant.report_id = report["report_id"]
            context = self._context_payload(referenced, context_applied)
            self._record_idempotent_response(
                user_message,
                status="completed",
                intent=intent_payload,
                warnings=warnings,
                context=context,
            )
            self._save(session)
            return ChatResult(
                session_id=session.id,
                status="completed",
                user_message=user_message.to_dict(),
                assistant_message=assistant.to_dict(),
                intent=intent_payload,
                analysis=record.to_dict(),
                report=report,
                warnings=warnings,
                context=context,
                history_size=len(session.messages),
            )

    # ------------------------------------------------------------------
    # 会话与报告 API
    # ------------------------------------------------------------------
    def get_session(self, session_id: str, *, include_results: bool = False) -> dict[str, Any]:
        memory = LangChainSessionMemory(self._store, session_id)
        with memory.session_lock():
            session = memory.load_session()
            if session is None:
                raise ResourceNotFoundError(f"会话不存在或已过期: {session_id}")
            return session.to_dict(
                include_analysis_results=include_results,
                include_reports=False,
            )

    def delete_session(self, session_id: str) -> bool:
        memory = LangChainSessionMemory(self._store, session_id)
        with memory.session_lock():
            if memory.load_session() is None:
                raise ResourceNotFoundError(f"会话不存在或已过期: {session_id}")
            # 已在会话锁内，直接删除以避免 Redis 非重入锁二次获取。
            if not memory.delete_session(acquire_lock=False):
                raise ResourceNotFoundError(f"会话不存在或已过期: {session_id}")
            return True

    def generate_report(
        self,
        session_id: str,
        *,
        analysis_ids: list[str] | None = None,
        title: str | None = None,
    ) -> dict[str, Any]:
        memory = LangChainSessionMemory(self._store, session_id)
        with memory.session_lock():
            session = memory.load_session()
            if session is None:
                raise ResourceNotFoundError(f"会话不存在或已过期: {session_id}")
            report = self._generate_report_locked(session, analysis_ids, title)
            self._save(session)
            return report

    def get_report(self, session_id: str, report_id: str) -> dict[str, Any]:
        memory = LangChainSessionMemory(self._store, session_id)
        with memory.session_lock():
            session = memory.load_session()
            if session is None:
                raise ResourceNotFoundError(f"会话不存在或已过期: {session_id}")
            report = session.find_report(report_id)
            if report is None:
                raise ResourceNotFoundError(f"报告不存在: {report_id}")
            return copy.deepcopy(report)

    def tools_meta(self) -> dict[str, Any]:
        return {
            "tools": self._executor.registry.metadata(),
            "langchain": {
                "available": bool(self._langchain_tools),
                "registered_tools": list(self._langchain_tools),
            },
        }

    def health(self) -> dict[str, Any]:
        return {
            "status": "ok",
            "session_store": self._store.health(),
            "tool_count": len(self._executor.registry.metadata()),
        }

    # ------------------------------------------------------------------
    # 内部流程
    # ------------------------------------------------------------------
    def _select_reference(
        self,
        session: ConversationSession,
        message: str,
        analysis_id: str | None,
        *,
        allow_latest: bool = False,
    ) -> AnalysisRecord | None:
        if analysis_id:
            record = session.find_analysis(analysis_id)
            if record is None:
                raise ResourceNotFoundError(f"当前会话中不存在分析结果: {analysis_id}")
            return record
        if allow_latest or _REFERENCE_RE.search(message):
            return session.latest_analysis()
        return None

    def _merge_follow_up(self, message, classified, previous: AnalysisRecord):
        current = classified
        if classified.intent == "unsupported" and previous.intent == "aggregation_query":
            probe = self._classifier.classify(f"按{message}分组")
            if probe.intent == "aggregation_query":
                current = probe
        if current.intent == "unsupported":
            return previous.intent, copy.deepcopy(previous.tool_input), True
        if current.intent != previous.intent:
            return current.intent, copy.deepcopy(current.params), False

        old = copy.deepcopy(previous.tool_input)
        new = copy.deepcopy(current.params)
        if current.intent != "aggregation_query":
            old.update(new)
            return current.intent, old, True

        merged = old
        if new.get("dimensions"):
            if re.search(r"(加上|同时|以及|和.*一起)", message):
                merged["dimensions"] = list(dict.fromkeys(
                    list(old.get("dimensions") or []) + list(new["dimensions"])
                ))
            else:
                merged["dimensions"] = list(new["dimensions"])
        if new.get("metrics"):
            merged["metrics"] = list(new["metrics"])
        if "filters" in new:
            merged["filters"] = self._merge_filters(
                old.get("filters") or [], new.get("filters") or []
            )
        for key in ("limit", "sort"):
            if key in new:
                merged[key] = copy.deepcopy(new[key])
        return current.intent, merged, True

    @staticmethod
    def _merge_filters(old: list[dict], new: list[dict]) -> list[dict]:
        replaced_fields = {item.get("field") for item in new}
        kept = [item for item in old if item.get("field") not in replaced_fields]
        return kept + copy.deepcopy(new)

    def _resummarize(self, session, user_message, record, classified) -> ChatResult:
        data = record.result.get("summary_data") if isinstance(record.result, dict) else record.result
        result = self._generator.generate(
            user_query=user_message.content,
            intent_label=record.intent,
            intent_key=record.intent,
            analysis_result=data,
        )
        summary = result.to_dict() if hasattr(result, "to_dict") else dict(result)
        trusted = (summary.get("hallucination") or {}).get("passed") is True
        answer = str(summary.get("text") or "") if trusted else (
            "自动解读未通过数字一致性校验，请以原始结构化分析结果为准。"
        )
        assistant = self._append_assistant(
            session,
            answer,
            user_message.id,
            intent=record.intent,
            analysis_id=record.id,
            status="context_summary",
        )
        intent_payload = self._intent_payload(
            classified, record.intent, record.tool_input, True
        )
        warnings = [] if trusted else [{"code": "UNTRUSTED_SUMMARY"}]
        context = {
            "referenced_analysis_id": record.id,
            "context_inherited": True,
        }
        self._record_idempotent_response(
            user_message,
            status="context_summary",
            intent=intent_payload,
            warnings=warnings,
            context=context,
        )
        self._save(session)
        return ChatResult(
            session_id=session.id,
            status="context_summary",
            user_message=user_message.to_dict(),
            assistant_message=assistant.to_dict(),
            intent=intent_payload,
            analysis=record.to_dict(),
            warnings=warnings,
            context=context,
            history_size=len(session.messages),
        )

    def _clarification(
        self,
        session,
        user_message,
        answer,
        *,
        intent: dict[str, Any] | None = None,
        reason: str,
        missing: list[str] | None = None,
    ) -> ChatResult:
        assistant = self._append_assistant(
            session, answer, user_message.id, status="needs_clarification"
        )
        intent_payload = intent or {"intent": "clarification", "params": {}}
        context = {"reason": reason, "missing": missing or []}
        self._record_idempotent_response(
            user_message,
            status="needs_clarification",
            intent=intent_payload,
            warnings=[],
            context=context,
        )
        self._save(session)
        return ChatResult(
            session_id=session.id,
            status="needs_clarification",
            user_message=user_message.to_dict(),
            assistant_message=assistant.to_dict(),
            intent=intent_payload,
            context=context,
            history_size=len(session.messages),
        )

    def _generate_report_locked(self, session, analysis_ids, title):
        if analysis_ids:
            selected = []
            for item_id in dict.fromkeys(analysis_ids):
                record = session.find_analysis(item_id)
                if record is None:
                    raise ResourceNotFoundError(f"当前会话中不存在分析结果: {item_id}")
                selected.append(record)
        else:
            selected = list(session.analyses)
        if not selected:
            raise ResourceNotFoundError("当前会话没有可用于生成报告的分析结果")
        report = self._reports.generate(
            session_id=session.id,
            analyses=selected,
            title=title,
        )
        session.reports.append(report)
        return report

    def _execute_tool(self, intent: str, params: dict[str, Any]) -> ToolCallResult:
        """优先通过预注册 StructuredTool 执行完整工具链。

        LangChain 只是编排外壳，其内部仍由 ToolExecutor 保证参数转换、
        重试和结果组装语义；未安装依赖时直接调用 executor。
        """
        registered = self._executor.registry.for_intent(intent)
        langchain_tool = (
            self._langchain_tools.get(registered.name) if registered is not None else None
        )
        if langchain_tool is None:
            return self._executor.execute(intent, params)

        assembled = langchain_tool.invoke({"params": copy.deepcopy(params)})
        if not isinstance(assembled, dict):
            raise ToolInvocationError(
                "LangChain 工具返回结构不合法",
                tool_name=registered.name,
                attempts=1,
            )
        provenance = assembled.get("provenance") or {}
        return ToolCallResult(
            intent=str(assembled.get("intent") or intent),
            tool_name=str(assembled.get("tool") or registered.name),
            params=copy.deepcopy(assembled.get("request") or params),
            raw_result=copy.deepcopy(assembled.get("data")),
            summary_data=copy.deepcopy(assembled.get("summary_data")),
            attempts=int(provenance.get("attempts") or 1),
            elapsed_seconds=float(provenance.get("elapsed_seconds") or 0.0),
            called_at=str(provenance.get("called_at") or ""),
        )

    @staticmethod
    def _intent_payload(classified, intent, params, inherited) -> dict[str, Any]:
        """重建实际执行意图的全部元数据，避免继承上下文后出现
        ``intent=aggregation`` 但 label/downstream 仍是 unsupported 的矛盾响应。
        """
        payload = classified.to_dict()
        spec = INTENT_BY_KEY.get(intent)
        missing = []
        if spec is not None:
            missing = [
                key for key in spec.requires_params
                if params.get(key) in (None, "", [], {})
            ]
            payload.update({
                "intent_label": spec.label_cn,
                "downstream": spec.downstream,
                "downstream_target": spec.target,
            })
        payload.update({
            "intent": intent,
            "params": copy.deepcopy(params),
            "missing_required": missing,
            "context_inherited": bool(inherited),
        })
        if intent != classified.intent:
            payload["classified_intent"] = classified.intent
        return payload

    @staticmethod
    def _record_idempotent_response(
        user_message: ConversationMessage,
        *,
        status: str,
        intent: dict[str, Any],
        warnings: list[dict[str, Any]],
        context: dict[str, Any],
    ) -> None:
        if not user_message.metadata.get("request_id"):
            return
        # 只存储可重建响应的小型元数据；分析与报告仍通过
        # assistant 上的 ID 从会话快照中读取，避免在 JSON 中整体复制。
        user_message.metadata["idempotency_result"] = {
            "status": status,
            "intent": copy.deepcopy(intent),
            "warnings": copy.deepcopy(warnings),
            "context": copy.deepcopy(context),
        }

    def _save(self, session: ConversationSession) -> None:
        session.messages = session.messages[-self._max_messages:]
        session.analyses = session.analyses[-self._max_analyses:]
        session.reports = session.reports[-self._max_reports:]
        session.touch()
        # 统一经 LangChain Memory 适配器保存完整会话，外层已持有
        # session_lock，save_session 不重复获取 Redis 非重入锁。
        LangChainSessionMemory(
            self._store, session.id, max_messages=self._max_messages
        ).save_session(session)

    def _compact_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """限制会话存储中的大结果集；报告/前端均能看到截断标志。"""
        compacted = copy.deepcopy(result)
        truncated_paths = []
        for path in (("data", "rows"), ("summary_data", "rows")):
            parent = compacted.get(path[0])
            if isinstance(parent, dict) and isinstance(parent.get(path[1]), list):
                rows = parent[path[1]]
                if len(rows) > self._max_result_rows:
                    parent[path[1]] = rows[:self._max_result_rows]
                    parent["storage_truncated"] = True
                    parent["original_row_count"] = len(rows)
                    truncated_paths.append(".".join(path))
        if truncated_paths:
            compacted.setdefault("provenance", {})["storage_truncated_paths"] = truncated_paths
        return compacted

    @staticmethod
    def _append_assistant(
        session,
        content,
        reply_to,
        *,
        intent=None,
        analysis_id=None,
        report_id=None,
        status="completed",
        metadata=None,
    ):
        payload = {"reply_to": reply_to, "status": status, **(metadata or {})}
        message = ConversationMessage(
            id=new_id("msg"),
            role="assistant",
            content=content,
            intent=intent,
            analysis_id=analysis_id,
            report_id=report_id,
            metadata=payload,
        )
        session.messages.append(message)
        return message

    def _find_idempotent_result(
        self,
        session,
        request_id,
        current_request: dict[str, Any],
    ) -> ChatResult | None:
        for index, message in enumerate(session.messages):
            if message.role != "user" or message.metadata.get("request_id") != request_id:
                continue
            stored_request = message.metadata.get("idempotency_request")
            if not isinstance(stored_request, dict):
                # 兼容升级前的会话快照；旧版未保存其余两个字段，
                # 只能以当时的默认值恢复。
                stored_request = {
                    "message": message.content,
                    "analysis_id": None,
                    "generate_report": False,
                }
            comparable_stored = {
                "message": stored_request.get("message"),
                "analysis_id": stored_request.get("analysis_id"),
                "generate_report": bool(stored_request.get("generate_report", False)),
            }
            comparable_current = {
                "message": current_request.get("message"),
                "analysis_id": current_request.get("analysis_id"),
                "generate_report": bool(current_request.get("generate_report", False)),
            }
            if comparable_stored != comparable_current:
                raise ConflictError(
                    message="request_id 已被不同请求使用",
                    detail={
                        "request_id": request_id,
                        "conflicting_fields": [
                            key for key in comparable_current
                            if comparable_current[key] != comparable_stored[key]
                        ],
                    },
                )
            assistant = next(
                (
                    item for item in session.messages[index + 1:]
                    if item.role == "assistant" and item.metadata.get("reply_to") == message.id
                ),
                None,
            )
            if assistant is None:
                return None
            analysis = session.find_analysis(assistant.analysis_id)
            report = session.find_report(assistant.report_id) if assistant.report_id else None
            saved_result = message.metadata.get("idempotency_result")
            saved_result = saved_result if isinstance(saved_result, dict) else {}
            saved_context = copy.deepcopy(saved_result.get("context") or {})
            saved_context.update({
                "idempotency_key": request_id,
                "original_status": saved_result.get("status")
                or assistant.metadata.get("status")
                or "completed",
            })
            fallback_intent = {
                "intent": analysis.intent if analysis else (assistant.intent or "unknown"),
                "params": copy.deepcopy(analysis.tool_input) if analysis else {},
            }
            return ChatResult(
                session_id=session.id,
                status="replayed",
                user_message=message.to_dict(),
                assistant_message=assistant.to_dict(),
                intent=copy.deepcopy(saved_result.get("intent") or fallback_intent),
                analysis=analysis.to_dict() if analysis else None,
                report=copy.deepcopy(report),
                warnings=copy.deepcopy(saved_result.get("warnings") or []),
                context=saved_context,
                history_size=len(session.messages),
            )
        return None

    @staticmethod
    def _context_payload(reference, inherited):
        return {
            "referenced_analysis_id": reference.id if reference else None,
            "context_inherited": bool(inherited),
        }


def build_application_service(settings, *, data_provider, cache, redis_client=None):
    """应用工厂使用的集中装配函数，所有依赖均可在单元测试中替换。"""
    from app.ai.intent.classifier import IntentClassifier
    from app.ai.summary.generator import SummaryGenerator
    from app.ai.summary.llm_client import build_client
    from app.application.clients import HTTPAnalysisClient, LocalAnalysisClient

    mode = str(getattr(settings, "analysis_api_mode", "local") or "local").lower()
    if mode == "http":
        analysis_client = HTTPAnalysisClient(
            getattr(settings, "analysis_api_base_url", ""),
            timeout=float(getattr(settings, "analysis_api_timeout", 30.0)),
            api_key=str(getattr(settings, "analysis_api_key", "") or ""),
        )
    elif mode == "local":
        from app.services.aggregation_service import AggregationService
        from app.services.algorithm_service import AlgorithmService

        analysis_client = LocalAnalysisClient(
            AggregationService(provider=data_provider, cache=cache, settings=settings),
            AlgorithmService(provider=data_provider),
        )
    else:
        raise ValueError(f"不支持的 analysis_api_mode: {mode}")

    registry = ToolRegistry.build_default(
        analysis_client,
        default_limit=int(getattr(settings, "agg_default_limit", 100)),
        max_limit=int(getattr(settings, "agg_max_limit", 1000)),
    )
    executor = ToolExecutor(
        registry,
        retry_policy=RetryPolicy(
            max_attempts=int(getattr(settings, "tool_max_attempts", 3)),
            base_delay_seconds=float(getattr(settings, "tool_retry_base_seconds", 0.1)),
            max_delay_seconds=float(getattr(settings, "tool_retry_max_seconds", 1.0)),
        ),
    )
    llm_client = build_client(settings)
    summary_generator = SummaryGenerator(
        llm_client, tolerance=float(getattr(settings, "hallucination_tolerance", 0.02))
    )
    store = build_session_store(settings, redis_client=redis_client)
    report_service = MedicalReportService(
        summary_generator,
        max_analyses=int(getattr(settings, "report_max_analyses", 10)),
    )
    return MedicalAssistantService(
        intent_classifier=IntentClassifier(
            min_confidence=float(getattr(settings, "intent_min_confidence", 0.45))
        ),
        summary_generator=summary_generator,
        tool_executor=executor,
        session_store=store,
        report_service=report_service,
        max_messages=int(getattr(settings, "conversation_max_messages", 100)),
        max_analyses=int(getattr(settings, "conversation_max_analyses", 20)),
        max_reports=int(getattr(settings, "conversation_max_reports", 10)),
        max_result_rows=int(getattr(settings, "conversation_max_result_rows", 200)),
    )
