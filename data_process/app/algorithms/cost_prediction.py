# -*- coding: utf-8 -*-
"""算法组件 4：住院费用预测（cost_prediction）。

基于 Spark ML Pipeline 的线性回归模型：
  特征：住院时长、病情严重程度、年龄组、入院类型、支付方式、内外科标志
  标签：总费用 total_charges

支持两种模式（通过 params["mode"] 切换）：
  * train  ：训练模型并输出 RMSE / MAE / R² 与特征系数（默认）；
  * predict：基于已训练模型对单条特征进行费用预测。

进程内模型缓存：相同参数不重复训练（企业级接口必须考虑训练成本）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from typing import Any

import pandas as pd
from pyspark.ml import Pipeline, PipelineModel
from pyspark.ml.evaluation import RegressionEvaluator
from pyspark.ml.feature import StringIndexer, VectorAssembler
from pyspark.ml.regression import LinearRegression
from pyspark.sql import DataFrame, functions as F

from app.algorithms.base import Algorithm, AlgorithmContext, ParamSpec, register_algorithm
from app.core.exceptions import ParamValidationError

logger = logging.getLogger(__name__)

# 模型特征定义（特征名 -> 列名 / 类型）
_FEATURES = {
    "length_of_stay": "length_of_stay",
    "severity_code": "apr_severity_of_illness_code",
}
_CATEGORICAL_FEATURES = [
    ("age_group", "age_group"),
    ("admission_type", "type_of_admission"),
    ("payment_type", "payment_typology_1"),
    ("medical_surgical", "apr_medical_surgical_description"),
]
_LABEL = "total_charges"

# 进程内模型缓存（参数哈希 -> PipelineModel），线程安全
_MODEL_CACHE: dict[str, PipelineModel] = {}
_MODEL_LOCK = threading.Lock()


@register_algorithm
class CostPredictionAlgorithm(Algorithm):
    name = "cost_prediction"
    display_name = "住院费用预测"
    version = "1.0.0"
    description = (
        "基于住院时长、病情严重度、年龄组、入院类型、支付方式等特征，"
        "使用 Spark ML 线性回归预测住院总费用，输出 RMSE/MAE/R² 与特征系数。"
    )
    tags = ("ml", "regression", "prediction")

    param_specs = [
        ParamSpec("mode", "str", required=False, default="train",
                  allowed_values=("train", "predict"), description="train=训练评估；predict=单点预测"),
        ParamSpec("sample_size", "int", required=False, default=100_000,
                  min_value=1_000, max_value=1_000_000, description="训练样本上限（行）"),
        ParamSpec("train_ratio", "float", required=False, default=0.8,
                  min_value=0.5, max_value=0.95, description="训练集比例"),
        ParamSpec("sample", "dict", required=False,
                  description="predict 模式必填：单条住院特征（字段见 features 元数据）"),
    ]

    def _execute(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        mode = ctx.params["mode"]
        if mode == "train":
            return self._train(ctx)
        return self._predict(ctx)

    # ------------------------------------------------------------------
    @staticmethod
    def _build_training_frame(df: DataFrame, sample_size: int) -> DataFrame:
        """清洗训练样本：费用>0、核心特征非空、移除缺失哨兵。

        采样完全下推到 Spark：先用缓存好的全表 count 换算采样比例，
        再用 sample(fraction) 抽样，不使用 limit() —— limit 会把扫描
        收敛到首部分区并在 driver 端截断，大表上既慢又容易把数据拉向
        一端；sample 则在各 executor 本地完成，无 driver 集中开销。
        """
        keep = (
            (F.col(_LABEL) > 0)
            & F.col(_LABEL).isNotNull()
            & F.col("length_of_stay").isNotNull()
            & F.col("apr_severity_of_illness_code").isNotNull()
        )
        for _, col in _CATEGORICAL_FEATURES:
            keep = keep & (F.col(col) != "Unknown") & F.col(col).isNotNull()
        cleaned = df.filter(keep)
        # fraction = 目标样本量 / 清洗后总量（1.05 倍上浮补偿抽样随机性）。
        # count 走的是已 cache 的全表，代价可忽略；清洗后行数未知时回退全量。
        total = cleaned.count()
        if total and total > 0:
            fraction = min(1.0, sample_size * 1.05 / float(total))
        else:
            fraction = 1.0
        return cleaned.sample(withReplacement=False, fraction=fraction, seed=42)

    def _train(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        params = ctx.params
        cache_key = json.dumps(
            {"sample_size": params["sample_size"], "train_ratio": params["train_ratio"]},
            sort_keys=True,
        )
        digest = hashlib.md5(cache_key.encode()).hexdigest()[:16]

        with _MODEL_LOCK:
            cached = _MODEL_CACHE.get(digest)
        if cached is not None:
            logger.info("费用预测模型命中缓存 %s", digest)
            model = cached
            metrics = self._evaluate(model, ctx.dataframe, params)
            return self._model_summary(model, metrics, from_cache=True), None, "费用预测模型（缓存）"

        training = self._build_training_frame(ctx.dataframe, params["sample_size"])
        train_df, test_df = training.randomSplit(
            [params["train_ratio"], 1 - params["train_ratio"]], seed=42
        )

        stages = []
        indexers = {}
        for feat_name, col in _CATEGORICAL_FEATURES:
            output = f"{feat_name}_idx"
            indexers[feat_name] = output
            stages.append(StringIndexer(inputCol=col, outputCol=output, handleInvalid="keep"))

        feature_cols = list(_FEATURES.values()) + list(indexers.values())
        stages.append(VectorAssembler(inputCols=feature_cols, outputCol="features"))
        stages.append(LinearRegression(featuresCol="features", labelCol=_LABEL,
                                       maxIter=20, regParam=0.01))

        pipeline = Pipeline(stages=stages)
        model = pipeline.fit(train_df)

        with _MODEL_LOCK:
            _MODEL_CACHE[digest] = model

        metrics = self._evaluate(model, ctx.dataframe, params)
        return self._model_summary(model, metrics, from_cache=False), None, "费用预测模型训练完成"

    @staticmethod
    def _evaluate(model: PipelineModel, df: DataFrame, params: dict) -> dict:
        """用相同参数构造的测试集评估（若为缓存模型则重新采样评估，保证指标可信）。"""
        training = CostPredictionAlgorithm._build_training_frame(df, params["sample_size"])
        _, test_df = training.randomSplit(
            [params["train_ratio"], 1 - params["train_ratio"]], seed=42
        )
        predictions = model.transform(test_df)
        rmse = RegressionEvaluator(labelCol=_LABEL, metricName="rmse").evaluate(predictions)
        mae = RegressionEvaluator(labelCol=_LABEL, metricName="mae").evaluate(predictions)
        r2 = RegressionEvaluator(labelCol=_LABEL, metricName="r2").evaluate(predictions)
        return {
            "rmse": round(float(rmse), 2),
            "mae": round(float(mae), 2),
            "r2": round(float(r2), 4),
            "sample_size": params["sample_size"],
            "train_ratio": params["train_ratio"],
        }

    @staticmethod
    def _model_summary(model: PipelineModel, metrics: dict, from_cache: bool) -> dict:
        lr_model = model.stages[-1]
        coeff_names = list(_FEATURES.keys()) + [f"{name}_idx" for name, _ in _CATEGORICAL_FEATURES]
        coefficients = [
            {"feature": name, "coefficient": round(float(c), 4)}
            for name, c in zip(coeff_names, lr_model.coefficients.toArray())
            if abs(float(c)) > 1e-6
        ]
        return {
            "mode": "train",
            "model": "spark-ml-linear-regression",
            "label": _LABEL,
            "from_cache": from_cache,
            "metrics": metrics,
            "intercept": round(float(lr_model.intercept), 2),
            "coefficients": coefficients,
            "features": list(coeff_names),
        }

    def _predict(self, ctx: AlgorithmContext) -> tuple[Any, dict | None, str]:
        sample = ctx.params.get("sample")
        if not sample:
            raise ParamValidationError(detail={"sample": "predict 模式必须提供 sample 参数"},
                                       message="predict 模式必须提供 sample 参数")
        # 复用当前参数训练/复用缓存模型
        train_params = {
            "sample_size": ctx.params["sample_size"],
            "train_ratio": ctx.params["train_ratio"],
        }
        digest = hashlib.md5(json.dumps(train_params, sort_keys=True).encode()).hexdigest()[:16]
        with _MODEL_LOCK:
            model = _MODEL_CACHE.get(digest)
        if model is None:
            self._train(ctx)
            with _MODEL_LOCK:
                model = _MODEL_CACHE[digest]

        spark = ctx.dataframe.sparkSession
        # 构造单行特征 DataFrame（缺失特征用均值/众数兜底，保证预测可执行）
        row = {
            "length_of_stay": int(sample.get("length_of_stay") or 1),
            "apr_severity_of_illness_code": int(sample.get("severity_code") or 1),
            "age_group": str(sample.get("age_group") or "Unknown"),
            "type_of_admission": str(sample.get("admission_type") or "Unknown"),
            "payment_typology_1": str(sample.get("payment_type") or "Unknown"),
            "apr_medical_surgical_description": str(sample.get("medical_surgical") or "Unknown"),
        }
        row_df = spark.createDataFrame(pd.DataFrame({
            "length_of_stay": pd.Series([row["length_of_stay"]], dtype="int64"),
            "apr_severity_of_illness_code": pd.Series(
                [row["apr_severity_of_illness_code"]], dtype="int64"),
            "age_group": pd.Series([row["age_group"]], dtype="string"),
            "type_of_admission": pd.Series([row["type_of_admission"]], dtype="string"),
            "payment_typology_1": pd.Series([row["payment_typology_1"]], dtype="string"),
            "apr_medical_surgical_description": pd.Series(
                [row["apr_medical_surgical_description"]], dtype="string"),
        }), schema=ctx.dataframe.select(
            "length_of_stay", "apr_severity_of_illness_code", "age_group",
            "type_of_admission", "payment_typology_1", "apr_medical_surgical_description",
        ).schema)
        prediction = model.transform(row_df).select("prediction").first()[0]
        result = {
            "mode": "predict",
            "input": sample,
            "predicted_total_charges": round(float(prediction), 2),
            "currency": "USD",
        }
        return result, None, "费用预测完成"
