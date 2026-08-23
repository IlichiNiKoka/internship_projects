# -*- coding: utf-8 -*-
"""人员1 AI 应用接口请求校验。"""

from __future__ import annotations

from marshmallow import Schema, ValidationError, fields, validate as ma_validate


_SESSION_ID = r"^ses_[a-f0-9]{20}$"
_REQUEST_ID = r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$"
_ANALYSIS_ID = r"^ana_[a-f0-9]{20}$"
_REPORT_ID = r"^rpt_[a-f0-9]{20}$"


def _not_blank(value: str) -> None:
    if not value.strip():
        raise ValidationError("message 不能为空白字符")


def _title_not_blank(value: str) -> None:
    if not value.strip():
        raise ValidationError("title 不能为空白字符")


class ChatRequestSchema(Schema):
    message = fields.Str(
        required=True,
        validate=ma_validate.And(ma_validate.Length(min=1, max=500), _not_blank),
        error_messages={"required": "缺少 message 用户消息"},
    )
    session_id = fields.Str(
        load_default=None,
        allow_none=True,
        validate=ma_validate.Regexp(_SESSION_ID, error="session_id 格式不合法"),
    )
    analysis_id = fields.Str(
        load_default=None,
        allow_none=True,
        validate=ma_validate.Regexp(_ANALYSIS_ID, error="analysis_id 格式不合法"),
    )
    generate_report = fields.Bool(load_default=False)
    request_id = fields.Str(
        load_default=None,
        allow_none=True,
        validate=ma_validate.Regexp(_REQUEST_ID, error="request_id 格式不合法"),
    )


class ReportRequestSchema(Schema):
    analysis_ids = fields.List(
        fields.Str(validate=ma_validate.Regexp(_ANALYSIS_ID)),
        load_default=None,
        allow_none=True,
        validate=ma_validate.Length(min=1, max=10),
    )
    title = fields.Str(
        load_default=None,
        allow_none=True,
        validate=ma_validate.And(ma_validate.Length(min=1, max=120), _title_not_blank),
    )


class ResourceIdSchema(Schema):
    session_id = fields.Str(
        required=True,
        validate=ma_validate.Regexp(_SESSION_ID, error="session_id 格式不合法"),
    )
    report_id = fields.Str(
        load_default=None,
        allow_none=True,
        validate=ma_validate.Regexp(_REPORT_ID, error="report_id 格式不合法"),
    )
