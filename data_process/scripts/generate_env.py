# -*- coding: utf-8 -*-
"""按机器自动生成 data_process/.env（deploy-infra.bat 部署完容器后调用）。

仅使用标准库，无需先安装任何 pip 依赖：
    python scripts/generate_env.py            # .env 已存在则跳过
    python scripts/generate_env.py --force    # 覆盖已有 .env

生成逻辑：
  1. 检测本机 Ollama（http://localhost:11434/api/tags）是否有可用模型；
     * 有 -> LLM 配置指向本地 Ollama（优先 qwen 系列）；
     * 无 -> 回退到项目使用的在线 API（OpenCode Zen，OpenAI 兼容协议）。
  2. Parquet 快照路径按当前仓库位置写成绝对路径；
  3. Spark JAVA_HOME / HADOOP_HOME 留空注释，交由 app/utils/spark.py 自动探测。
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent   # 仓库根
BACKEND_ROOT = PROJECT_ROOT / "data_process"
ENV_PATH = BACKEND_ROOT / ".env"

OLLAMA_BASE = "http://localhost:11434"
OLLAMA_TIMEOUT = 2.0   # 秒；本地服务未启动时快速失败

# 在线备用 API（OpenCode Zen，OpenAI 兼容协议；免费端点，单次延迟较高）
ONLINE_API = {
    "ANALYTICS_LLM_PROVIDER": "openai",
    "ANALYTICS_LLM_API_KEY":
        "sk-vrQTYD6e9toVH4iJerQTapYLym19kphZNQi3K3VvubQbpMuyAKPhGE72N9Lx9ZxV",
    "ANALYTICS_LLM_BASE_URL": "https://opencode.ai/zen/v1",
    "ANALYTICS_LLM_MODEL": "nemotron-3-ultra-free",
    "ANALYTICS_LLM_TIMEOUT": "30",
    "ANALYTICS_LLM_MAX_RETRIES": "1",
    "ANALYTICS_LLM_RETRY_BUDGET_SECONDS": "40",
    "ANALYTICS_LLM_EXTRA_BODY": '{"reasoning_effort":"none"}',
}


def detect_ollama_model() -> str | None:
    """返回本机 Ollama 上可用的模型名；不可用/无模型返回 None。"""
    try:
        req = urllib.request.Request(
            f"{OLLAMA_BASE}/api/tags", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=OLLAMA_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None
    models = [m.get("name", "") for m in data.get("models") or []]
    if not models:
        return None
    # 项目摘要链路针对 qwen 系列调优（think=false），优先选 qwen
    for name in models:
        if "qwen" in name.lower():
            return name
    return models[0]


def build_env_text(llm_cfg: dict, parquet_path: Path) -> str:
    """组装完整 .env 内容（LLM 区块按检测结果替换）。"""
    text = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    parquet_abs = str(parquet_path).replace("\\", "/")

    llm_block = "\n".join(f"{k}={v}" for k, v in llm_cfg.items())
    marker = "# ---- AI 智能层"
    head, _, _ = text.partition(marker)
    tail = (
        f"{marker}（generate_env.py 按本机环境自动生成）----\n"
        f"{llm_block}\n"
        f"ANALYTICS_LLM_TEMPERATURE=0.2\n"
        f"ANALYTICS_LLM_MAX_TOKENS=4000\n"
        f"ANALYTICS_INTENT_MIN_CONFIDENCE=0.45\n"
        f"\n# ---- MySQL -> Parquet 快照绝对路径（随仓库位置生成）----\n"
        f"ANALYTICS_PARQUET_SNAPSHOT_ENABLED=true\n"
        f"ANALYTICS_DATA_PARQUET_PATH={parquet_abs}\n"
    )
    return head + tail


def main() -> int:
    parser = argparse.ArgumentParser(description="按机器生成 data_process/.env")
    parser.add_argument("--force", action="store_true", help="覆盖已存在的 .env")
    args = parser.parse_args()

    if ENV_PATH.exists() and not args.force:
        print(f"[SKIP] {ENV_PATH} 已存在，不覆盖（如需重新生成请加 --force）")
        return 0

    parquet_path = BACKEND_ROOT / "processed" / "sparcs_snapshot.parquet"

    model = detect_ollama_model()
    if model:
        llm_cfg = {
            "ANALYTICS_LLM_PROVIDER": "ollama",
            "ANALYTICS_LLM_API_KEY": "ollama",
            "ANALYTICS_LLM_BASE_URL": OLLAMA_BASE,
            "ANALYTICS_LLM_MODEL": model,
            "ANALYTICS_LLM_TIMEOUT": "60",
            "ANALYTICS_LLM_MAX_RETRIES": "1",
            "ANALYTICS_LLM_RETRY_BUDGET_SECONDS": "90",
            "ANALYTICS_LLM_OLLAMA_THINK": "false",
        }
        print(f"[OK]   检测到本机 Ollama 模型：{model} -> 使用本地 LLM")
    else:
        llm_cfg = ONLINE_API
        print("[WARN] 未检测到本机 Ollama / 可用模型 -> 使用在线 API（OpenCode Zen）")
        print("       如后续安装了 Ollama，可重跑本脚本或参照 .env.example 切换")

    ENV_PATH.write_text(build_env_text(llm_cfg, parquet_path), encoding="utf-8")
    print(f"[OK]   已生成 {ENV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
