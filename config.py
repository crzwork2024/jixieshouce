"""
config.py
全局配置读取与校验

所有默认值在 .env.example 中定义，此文件只负责读取环境变量。
修改配置只需编辑 .env，无需改动代码。
"""

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

# ── API 认证 ───────────────────────────────────────────────
SILICONFLOW_API_KEY: str = os.getenv("SILICONFLOW_API_KEY", "")

# ── 嵌入模型 ───────────────────────────────────────────────
EMBED_MODEL:      str = os.getenv("EMBED_MODEL")
EMBED_ENDPOINT:   str = os.getenv("EMBED_ENDPOINT")
EMBED_BATCH_SIZE: int = int(os.getenv("EMBED_BATCH_SIZE"))
EMBED_MAX_CHARS:  int = int(os.getenv("EMBED_MAX_CHARS"))
EMBED_DIM:        int = int(os.getenv("EMBED_DIM"))

# ── LLM ───────────────────────────────────────────────────
LLM_MODEL:        str   = os.getenv("LLM_MODEL")
LLM_ENDPOINT:     str   = os.getenv("LLM_ENDPOINT")
LLM_TEMPERATURE:  float = float(os.getenv("LLM_TEMPERATURE"))
LLM_MAX_TOKENS:   int   = int(os.getenv("LLM_MAX_TOKENS"))

# ── 检索参数 ───────────────────────────────────────────────
TOP_K_TOC:    int = int(os.getenv("TOP_K_TOC"))
TOP_K_BLOCKS: int = int(os.getenv("TOP_K_BLOCKS"))

# ── 路径 ───────────────────────────────────────────────────
DATA_DIR:     Path = Path(os.getenv("DATA_DIR"))
CHROMA_DIR:   Path = Path(os.getenv("CHROMA_DIR"))
FRONTEND_DIR: Path = Path(os.getenv("FRONTEND_DIR"))

# ── ChromaDB Collection 名 ─────────────────────────────────
TOC_COLLECTION_NAME:    str = os.getenv("TOC_COLLECTION_NAME")
BLOCKS_COLLECTION_NAME: str = os.getenv("BLOCKS_COLLECTION_NAME")


def validate() -> None:
    """服务启动时调用，检查必要配置是否齐全。"""
    errors = []
    if not SILICONFLOW_API_KEY:
        errors.append("SILICONFLOW_API_KEY 未设置")
    # 检查数值型配置是否因缺失而变成 None（int(None) 会在上面就报错，这里兜底字符串类型）
    required_str = {
        "EMBED_MODEL": EMBED_MODEL,
        "LLM_MODEL":   LLM_MODEL,
    }
    for key, val in required_str.items():
        if not val:
            errors.append(f"{key} 未设置")
    if errors:
        lines = "\n".join(f"  ✗ {e}" for e in errors)
        raise EnvironmentError(
            f"配置校验失败，请检查 .env 文件：\n{lines}\n"
            f"参考模板：.env.example"
        )
