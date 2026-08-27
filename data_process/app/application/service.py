# -*- coding: utf-8 -*-
"""人员1应用服务：工具调用、多轮上下文与医疗洞察报告的统一编排入口。"""

from __future__ import annotations

import copy
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

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
        llm_client=None,
        ai_service=None,
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
        self._llm_client = llm_client  # 二期：unsupported 场景 grounded 回复
        # Agent 编排服务（AIService）；未注入时 chat() 回退到本地规则流水线
        self._ai_service = ai_service
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

            # “生成按医院统计报告”：Agent 执行分析后再为新分析生成报告。
            if not report_negated and _REPORT_TERM_RE.search(message):
                generate_report = True

            # 未注入 Agent 编排服务（单元测试/离线模式）时，
            # 回退到本地规则意图识别 + 直接工具调用的轻量路径。
            if self._ai_service is None:
                return self._chat_local_pipeline(
                    session,
                    user_message,
                    message=message,
                    classified=classified,
                    referenced=referenced,
                    analysis_id=analysis_id,
                    generate_report=generate_report,
                )

            # ------------------------------------------------------------------
            # 核心变化：取消本地意图识别，直接交给 AIService(Agent) 端到端执行。
            #   LLM 负责规划工具、生成 Spark aggregation params（不生成裸 SQL）
            #   本地负责：校验参数、调用 Spark/算法、生成摘要与记录。
            # ------------------------------------------------------------------
            # 1) 构造历史上下文（便于 Agent 做跟进式分析）
            history_text = self._memory_variables_for_agent(session, referenced)
            context_applied_any = bool(referenced or analysis_id or bool(_REFERENCE_RE.search(message)))
            if context_applied_any and referenced is not None:
                rsummary = ""
                if isinstance(referenced.summary, dict):
                    rsummary = referenced.summary.get("text") or ""
                elif isinstance(referenced.result, dict):
                    rsummary = (referenced.result.get("summary") or {}).get("text") or ""
                if rsummary:
                    history_text = (
                        f"{history_text}\n[引用分析结果摘要]\n{rsummary}"
                    )

            # 2) Agent 端到端执行（工具规划 + 执行 + 摘要生成，含 self-correct 与降级）
            ai_result = self._ai_service.execute(message, history=history_text)
            intent = ai_result.intent
            intent_key = (
                intent.intent if intent.intent in INTENT_BY_KEY else "freeform_query"
            )
            intent_label = intent.spec.label_cn if intent.spec else intent_key
            intent_payload = self._intent_payload(
                intent, intent_key, copy.deepcopy(intent.params), context_applied_any,
            )

            # 3) 从 AIService 结果抽取出结构化分析，写入 AnalysisRecord
            summary_dict = (
                ai_result.summary.to_dict()
                if hasattr(ai_result.summary, "to_dict")
                else dict(ai_result.summary)
            )
            warnings: list[dict[str, Any]] = []
            if summary_dict.get("fell_back_to_mock"):
                warnings.append({
                    "code": "LLM_FALLBACK",
                    "message": "本次使用确定性模板生成摘要",
                })
            answer = str(summary_dict.get("text") or "分析已完成。")

            analysis_blob = ai_result.analysis if isinstance(ai_result.analysis, dict) else {}
            agent_calls = analysis_blob.get("calls") or []
            primary = analysis_blob.get("primary")
            if primary is None and agent_calls:
                primary = agent_calls[0].get("result")
            tool_name = (agent_calls[0].get("tool") if agent_calls else "agent") or "agent"
            tool_input = (agent_calls[0].get("params") if agent_calls else intent.params) or {}
            elapsed = 0.0
            if isinstance(primary, dict) and isinstance(primary.get("provenance"), dict):
                elapsed = float(primary["provenance"].get("elapsed_seconds") or 0)

            assistant = self._append_assistant(
                session,
                answer,
                user_message.id,
                intent=intent_key,
                status="completed",
                metadata={"warnings": warnings, "agent_calls": agent_calls},
            )
            assembled = self._compact_result(primary if primary is not None else analysis_blob)
            # Agent 路径补齐 summary_data：前端结构化渲染（表格/图表/KPI）、
            # 洞察报告的摘要与数字一致性校验都依赖该字段；本地回退路径由
            # ToolCallResult.assembled_result() 提供，此处对齐两条路径契约。
            # 缺失会导致前端图表为空、报告层误判「空数据」而 fail-closed。
            if isinstance(assembled, dict) and "summary_data" not in assembled:
                try:
                    from app.application.tools import normalize_for_summary
                    assembled["summary_data"] = normalize_for_summary(
                        intent_key, primary if primary is not None else analysis_blob,
                    )
                except Exception:
                    logger.warning(
                        "Agent 分析结果 summary_data 适配失败 intent=%s", intent_key,
                        exc_info=True,
                    )
            record = AnalysisRecord(
                id=new_id("ana"),
                message_id=assistant.id,
                query=message,
                intent=intent_key,
                tool_name=tool_name,
                tool_input=copy.deepcopy(tool_input),
                result=assembled,
                summary=summary_dict,
                attempts=1,
                elapsed_seconds=elapsed,
            )
            assistant.analysis_id = record.id
            session.analyses.append(record)

            report = None
            if generate_report:
                report = self._generate_report_locked(session, [record.id], title=None)
                assistant.report_id = report["report_id"]
            context = self._context_payload(referenced, context_applied_any)
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
    # 本地回退流水线（未注入 AIService 时使用，如单元测试/离线模式）
    # ------------------------------------------------------------------
    def _chat_local_pipeline(
        self,
        session: ConversationSession,
        user_message: ConversationMessage,
        *,
        message: str,
        classified,
        referenced: AnalysisRecord | None,
        analysis_id: str | None = None,
        generate_report: bool = False,
    ) -> ChatResult:
        """本地规则意图识别 → 直接工具调用 → 摘要生成的传统路径。"""
        intent_key = classified.intent
        params = copy.deepcopy(classified.params)
        context_applied = False

        if referenced is not None and (
            analysis_id is not None or _REFERENCE_RE.search(message)
        ):
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
        local_warnings: list[dict[str, Any]] = []
        answer = str(summary_dict.get("text") or "分析已完成。")
        hallucination = summary_dict.get("hallucination") or {}
        if hallucination.get("passed") is not True:
            # 兼容旧摘要对象：无 prompt_only 标记时视为未过校验
            local_warnings.append({
                "code": "UNTRUSTED_SUMMARY",
                "message": "自动摘要未通过数字一致性校验，请以结构化分析结果为准",
            })
            answer = "分析已完成，但自动摘要未通过数字一致性校验，请以结构化结果为准。"
        elif summary_dict.get("fell_back_to_mock"):
            local_warnings.append({
                "code": "LLM_FALLBACK",
                "message": "本次使用确定性模板生成摘要",
            })

        assistant = self._append_assistant(
            session,
            answer,
            user_message.id,
            intent=intent_key,
            status="completed",
            metadata={"warnings": local_warnings},
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
            warnings=local_warnings,
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
            warnings=local_warnings,
            context=context,
            history_size=len(session.messages),
        )

    # ------------------------------------------------------------------
    # LLM 客户端热切换（provider.py 调用）：同步本服务的 _llm_client / _generator
    # 以及内部 Agent 编排服务，保证在线/本地切换对聊天路径立即生效。
    # ------------------------------------------------------------------
    def rebind_client(self, client) -> None:
        self._llm_client = client
        if self._generator is not None:
            self._generator._client = client
        if self._ai_service is not None:
            self._ai_service.rebind_client(client)

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
        # 重构后分类器把无法规则匹配的输入（如“继续”“那去年呢”）归为
        # freeform_query，与旧版 unsupported 同义：有引用时继承上次分析。
        bare_follow_up = classified.intent in ("unsupported", "freeform_query")
        current = classified
        if bare_follow_up and previous.intent == "aggregation_query":
            probe = self._classifier.classify(f"按{message}分组")
            if probe.intent == "aggregation_query":
                current = probe
        if bare_follow_up:
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
        # 幻觉后置校验已移除：直接采用生成文本，防幻觉靠提示词强约束
        answer = str(summary.get("text") or "")
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
        warnings: list[dict[str, Any]] = []
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

    # ------------------------------------------------------------------
    def _grounded_fallback_answer(self, user_query: str) -> str:
        """对 unsupported/边界问题做 LLM grounded 回复：基于系统能力回答，
        不编造数据，超出能力范围明确说明，再引导到数据分析类问题。

        失败或 LLM 不可用时回退到固定话术。
        """
        from app.ai.summary.llm_client import MockClient

        default = (
            "我可以基于本平台的 SPARCS 出院记录数据为你分析：按医院/疾病/年龄/"
            "年份/支付方式等维度的住院人次、费用、住院时长等指标；总体统计、"
            "疾病关联、费用预测和再入院风险评估等。如果你的问题与医疗数据相关，"
            "请补充要分析的维度或指标。"
        )
        client = self._llm_client
        if client is None or isinstance(client, (MockClient,)):
            return default
        system_prompt = (
            "你是一个基于医院出院记录(SPARCS 2021)数据的医疗分析助手。\n"
            "【系统能力】\n"
            "- 聚合查询：支持维度=出院年份/医院名称/年龄段/性别/疾病诊断(CCSR)/"
            "手术操作(CCSR)/支付方式/入院类型/疾病严重程度/死亡风险/内外科标识；"
            "支持指标=出院人次/总费用/平均费用/住院总天数/平均住院天数/急诊率；\n"
            "- 平台总览统计：核心指标+分布；\n"
            "- 疾病关联分析(Association)：疾病与操作/支付方式/入院类型等关联；\n"
            "- 住院费用预测：根据患者特征预测费用；\n"
            "- 再入院风险评估：人群画像与单条评估。\n"
            "【数据范围】仅覆盖纽约州 SPARCS 出院记录(2021为主,209万行)。\n"
            "\n"
            "【回答原则：必须 grounded，杜绝编造】\n"
            "1. 明确区分：我能基于数据回答的 vs 数据未覆盖的 vs 完全超出能力范围的；\n"
            "2. 用户问题若与医疗/医院/费用/疾病/患者等相关但表述模糊：先说明你可能需要"
            "   他提供哪些信息(要分析什么维度、什么指标)，再举1-2个可回答的示例问题；\n"
            "3. 数据里没有的(如天气、股价、电影、编程、翻译、实时行情、医学科普"
            "   具体诊疗建议、个人定制医嘱、SPARCS 外的数据、2022年后的数据等)，"
            "   明确说："
            "   「当前数据库仅包含纽约州2021年出院记录，不覆盖此类信息」，"
            "   然后可以引导：你可以尝试问一些与本平台数据相关的分析问题；\n"
            "4. 闲聊/打招呼(你好/谢谢/再见等)：友好简洁回应；\n"
            "5. 不编造任何数据、数字、结论；所有结论前面都要加"
            "   「如果基于当前出院记录数据的话…」或类似措辞；\n"
            "6. 语言简洁，不超过200字。\n"
        )
        user_prompt = f"用户问题：{user_query}\n\n请按原则回答："
        try:
            raw = client.chat(system_prompt, user_prompt)
            if raw and not raw.startswith("__MOCK__"):
                text = raw.strip()
                # 去除 LLM 可能包的 ```markdown
                if text.startswith("```"):
                    import re as _re
                    text = _re.sub(r"^```[\w]*\n", "", text)
                    text = _re.sub(r"\n```$", "", text).strip()
                if text:
                    return text[:800]
        except Exception as exc:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "grounded fallback LLM 失败: %s", exc, exc_info=False,
            )
        return default

    # ------------------------------------------------------------------
    def _memory_variables_for_agent(self, session, referenced) -> str:
        """把多轮对话记忆压缩为 Agent（单轮 LLM 规划）能消费的一段纯文本。

        LLM 规划是单轮调用，我们在这里把（可能压缩过的）历史摘要 + 最近若干轮
        拼接成「上下文对话历史摘要」。
        """
        parts: list[str] = []
        metadata = session.metadata or {}
        compressed = metadata.get("compressed_summary") if isinstance(metadata, dict) else ""
        if compressed:
            parts.append(f"【历史摘要】{compressed}")
        # 最近 5 轮 user/assistant，取纯文本
        recent = list(session.messages[-10:]) if session.messages else []
        lines: list[str] = []
        for m in recent:
            role = "用户" if m.role == "user" else ("助手" if m.role == "assistant" else m.role)
            content = (m.content or "").strip()
            if not content:
                continue
            if len(content) > 400:
                content = content[:397] + "..."
            lines.append(f"- {role}: {content}")
        if lines:
            parts.append("【最近对话】\n" + "\n".join(lines))
        if referenced is not None:
            parts.append(f"【被引用的分析ID】{referenced.id}（intent={referenced.intent}）")
        return "\n\n".join(parts)[:3000]

    # ------------------------------------------------------------------
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
    from app.ai.service import AIService
    from app.ai.summary.generator import SummaryGenerator
    from app.ai.summary.llm_client import build_client
    from app.application.clients import HTTPAnalysisClient, LocalAnalysisClient

    mode = str(getattr(settings, "analysis_api_mode", "local") or "local").lower()
    aggregation_service = None
    algorithm_service = None
    if mode == "http":
        analysis_client = HTTPAnalysisClient(
            getattr(settings, "analysis_api_base_url", ""),
            timeout=float(getattr(settings, "analysis_api_timeout", 30.0)),
            api_key=str(getattr(settings, "analysis_api_key", "") or ""),
        )
    elif mode == "local":
        from app.services.aggregation_service import AggregationService
        from app.services.algorithm_service import AlgorithmService

        aggregation_service = AggregationService(
            provider=data_provider, cache=cache, settings=settings
        )
        algorithm_service = AlgorithmService(provider=data_provider)
        analysis_client = LocalAnalysisClient(aggregation_service, algorithm_service)
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
    summary_generator = SummaryGenerator(llm_client)
    store = build_session_store(settings, redis_client=redis_client)
    report_service = MedicalReportService(
        summary_generator,
        max_analyses=int(getattr(settings, "report_max_analyses", 10)),
    )
    intent_classifier = IntentClassifier(
        min_confidence=float(getattr(settings, "intent_min_confidence", 0.45))
    )
    # 本地模式需把 Spark/算法服务交给 Agent 做工具规划；HTTP 模式由
    # analysis_client 承担，Agent 侧服务留空（execute 时按 client 走）。
    ai_service = AIService(
        settings=settings,
        aggregation_service=aggregation_service,
        algorithm_service=algorithm_service,
        intent_classifier=intent_classifier,
        summary_generator=summary_generator,
        llm_client=llm_client,
    )
    return MedicalAssistantService(
        intent_classifier=intent_classifier,
        summary_generator=summary_generator,
        tool_executor=executor,
        session_store=store,
        report_service=report_service,
        llm_client=llm_client,
        ai_service=ai_service,
        max_messages=int(getattr(settings, "conversation_max_messages", 100)),
        max_analyses=int(getattr(settings, "conversation_max_analyses", 20)),
        max_reports=int(getattr(settings, "conversation_max_reports", 10)),
        max_result_rows=int(getattr(settings, "conversation_max_result_rows", 200)),
    )
