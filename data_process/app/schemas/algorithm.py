# -*- coding: utf-8 -*-
"""算法调用请求 schema：params 为自由字典，具体校验由算法参数规格完成。"""

from __future__ import annotations

from marshmallow import Schema, fields


class AlgorithmRunSchema(Schema):
    params = fields.Dict(load_default={})
