"""
vector_store.py
向量库构建与检索模块

使用 ChromaDB 本地部署，嵌入模型为 BAAI/bge-large-zh-v1.5（硅基流动 API）。
维护两个 Collection：
  - toc_collection      : 目录节点嵌入（用于第一阶段章节检索）
  - blocks_collection   : 语义块嵌入（用于第二阶段精细检索）
"""

import json
import re
import time
from pathlib import Path
from typing import Any

import chromadb
import requests
from chromadb import Settings

from config import (
    SILICONFLOW_API_KEY,
    EMBED_MODEL, EMBED_ENDPOINT, EMBED_BATCH_SIZE, EMBED_MAX_CHARS,
    TOC_COLLECTION_NAME, BLOCKS_COLLECTION_NAME,
)

# ─────────────────────────────────────────────
# 硅基流动嵌入接口
# ─────────────────────────────────────────────


def _embed_texts_batch(texts: list[str]) -> list[list[float]]:
    """调用硅基流动 API 获取文本嵌入（单批）。"""
    if not SILICONFLOW_API_KEY:
        raise EnvironmentError("请设置环境变量 SILICONFLOW_API_KEY")

    resp = requests.post(
        EMBED_ENDPOINT,
        headers={
            "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": EMBED_MODEL,
            "input": texts,
            "encoding_format": "float",
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    # 按 index 排序后返回向量列表
    items = sorted(data["data"], key=lambda x: x["index"])
    return [item["embedding"] for item in items]


def _truncate(text: str) -> str:
    """截断到 EMBED_MAX_CHARS，防止超出模型 1K Token 上下文限制。"""
    return text[:EMBED_MAX_CHARS] if len(text) > EMBED_MAX_CHARS else text


def embed_texts(texts: list[str], retry: int = 3) -> list[list[float]]:
    """分批调用嵌入 API，自动截断 + 重试。"""
    # 先截断每条文本
    texts = [_truncate(t) for t in texts]

    all_embeddings: list[list[float]] = []
    for i in range(0, len(texts), EMBED_BATCH_SIZE):
        batch = texts[i: i + EMBED_BATCH_SIZE]
        for attempt in range(retry):
            try:
                embeddings = _embed_texts_batch(batch)
                all_embeddings.extend(embeddings)
                break
            except Exception as e:
                if attempt == retry - 1:
                    raise
                wait = 2 ** attempt
                print(f"  嵌入第 {i//EMBED_BATCH_SIZE+1} 批失败（{e}），{wait}s 后重试...")
                time.sleep(wait)
    return all_embeddings


# ─────────────────────────────────────────────
# ChromaDB 自定义嵌入函数（供 Chroma 内部调用）
# ─────────────────────────────────────────────

class BGEEmbeddingFunction:
    """ChromaDB EmbeddingFunction 接口实现（兼容新旧版本）。"""

    def __call__(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return embed_texts(input)

    def embed_documents(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return embed_texts(input)

    def embed_query(self, input: list[str]) -> list[list[float]]:  # noqa: A002
        return embed_texts(input)

    def name(self) -> str:
        return "bge-large-zh-v1.5"


# ─────────────────────────────────────────────
# 向量库管理器
# ─────────────────────────────────────────────

# Collection 名称从 config 导入（已在文件顶部 import）


class VectorStore:
    def __init__(self, persist_dir: str = "./chroma_db"):
        self._persist_dir = persist_dir
        self._client = chromadb.PersistentClient(
            path=persist_dir,
            settings=Settings(anonymized_telemetry=False),
        )
        self._embed_fn = BGEEmbeddingFunction()
        self._toc_col    = self._get_or_create(TOC_COLLECTION_NAME)
        self._blocks_col = self._get_or_create(BLOCKS_COLLECTION_NAME)

    def _get_or_create(self, name: str):
        return self._client.get_or_create_collection(
            name=name,
            metadata={"hnsw:space": "cosine"},
            embedding_function=self._embed_fn,
        )

    # ────────────────────────────────────────
    # 索引构建
    # ────────────────────────────────────────

    def index_toc(self, toc: list[dict]) -> None:
        """将目录节点写入 toc_collection。"""
        if not toc:
            return

        existing_ids = set(self._toc_col.get(ids=[t["toc_id"] for t in toc])["ids"])
        new_entries = [t for t in toc if t["toc_id"] not in existing_ids]
        if not new_entries:
            print(f"  目录节点已全部存在，跳过索引。")
            return

        ids       = [t["toc_id"]      for t in new_entries]
        documents = [t["search_text"] for t in new_entries]
        metadatas = [{
            "block_id":    t["block_id"],
            "title":       t["title"],
            "level":       t["level"],
            "page_idx":    t["page_idx"],
            "bbox":        json.dumps(t["bbox"]),
            "pdf_file_id": t["pdf_file_id"],
        } for t in new_entries]

        print(f"  正在为 {len(new_entries)} 个目录节点生成嵌入...")
        self._toc_col.add(ids=ids, documents=documents, metadatas=metadatas)
        print(f"  目录节点索引完成。")

    def index_blocks(self, blocks: list[dict]) -> None:
        """将语义块写入 blocks_collection。"""
        if not blocks:
            return

        existing_ids = set(
            self._blocks_col.get(ids=[b["block_id"] for b in blocks])["ids"]
        )
        new_blocks = [b for b in blocks if b["block_id"] not in existing_ids]
        if not new_blocks:
            print(f"  语义块已全部存在，跳过索引。")
            return

        print(f"  正在为 {len(new_blocks)} 个语义块生成嵌入...")
        ids       = [b["block_id"]    for b in new_blocks]
        documents = [b["search_text"] for b in new_blocks]
        metadatas = [{
            "chapter_path": b["chapter_path"],
            "page_range":   json.dumps(b["page_range"]),
            "bboxes":       json.dumps(b["bboxes"]),
            "block_type":   b["block_type"],
            "pdf_file_id":  b["pdf_file_id"],
            "raw_content":  b["raw_content"][:1000],  # Chroma 元数据大小限制
        } for b in new_blocks]

        # 分批写入，避免单次请求过大
        batch = 100
        for i in range(0, len(new_blocks), batch):
            self._blocks_col.add(
                ids       = ids[i:i+batch],
                documents = documents[i:i+batch],
                metadatas = metadatas[i:i+batch],
            )
            print(f"    已写入 {min(i+batch, len(new_blocks))}/{len(new_blocks)} 块...")

        print(f"  语义块索引完成。")

    def build_index_from_files(
        self,
        toc_path: str,
        blocks_path: str,
    ) -> None:
        """从预处理输出文件重建向量索引。"""
        print(f"加载目录文件: {toc_path}")
        toc = json.loads(Path(toc_path).read_text(encoding="utf-8"))

        print(f"加载语义块文件: {blocks_path}")
        blocks = json.loads(Path(blocks_path).read_text(encoding="utf-8"))

        self.index_toc(toc)
        self.index_blocks(blocks)

    # ────────────────────────────────────────
    # 检索接口
    # ────────────────────────────────────────

    def query_toc(
        self,
        query: str,
        top_k: int = 3,
        pdf_file_id: str | None = None,
    ) -> list[dict]:
        """
        第一阶段：目录级意图检索。
        返回最相关的 top_k 个目录节点，包含 chapter_path。
        """
        where = {"pdf_file_id": pdf_file_id} if pdf_file_id else None
        results = self._toc_col.query(
            query_texts=[query],
            n_results=min(top_k, max(1, self._toc_col.count())),
            where=where,
        )
        return self._parse_results(results)

    def query_blocks(
        self,
        query: str,
        chapter_paths: list[str] | None = None,
        top_k: int = 10,
        block_type_filter: str | None = None,
        pdf_file_id: str | None = None,
    ) -> list[dict]:
        """
        第二阶段：语义块精细检索，支持章节范围过滤和类型过滤。
        """
        where_conditions: list[dict] = []

        if pdf_file_id:
            where_conditions.append({"pdf_file_id": {"$eq": pdf_file_id}})

        if block_type_filter:
            where_conditions.append({"block_type": {"$eq": block_type_filter}})

        if chapter_paths:
            where_conditions.append({
                "chapter_path": {"$in": chapter_paths}
            })

        if len(where_conditions) == 0:
            where = None
        elif len(where_conditions) == 1:
            where = where_conditions[0]
        else:
            where = {"$and": where_conditions}

        total = self._blocks_col.count()
        if total == 0:
            return []

        results = self._blocks_col.query(
            query_texts=[query],
            n_results=min(top_k * 2, total),
            where=where,
        )
        candidates = self._parse_results(results)

        # 关键词重排序（BM25-like 简单实现）
        ranked = _rerank_by_keyword(query, candidates)
        return ranked[:top_k]

    def get_block_by_id(self, block_id: str) -> dict | None:
        """按 block_id 获取完整语义块信息。"""
        result = self._blocks_col.get(ids=[block_id])
        if not result["ids"]:
            return None
        meta = result["metadatas"][0]
        return {
            "block_id":     result["ids"][0],
            "raw_content":  meta.get("raw_content", ""),
            "chapter_path": meta.get("chapter_path", ""),
            "page_range":   json.loads(meta.get("page_range", "[]")),
            "bboxes":       json.loads(meta.get("bboxes", "[]")),
            "block_type":   meta.get("block_type", "text"),
            "pdf_file_id":  meta.get("pdf_file_id", ""),
            "document":     result["documents"][0] if result["documents"] else "",
        }

    # ────────────────────────────────────────
    # 工具方法
    # ────────────────────────────────────────

    def _parse_results(self, results: dict) -> list[dict]:
        parsed = []
        ids        = results.get("ids", [[]])[0]
        metas      = results.get("metadatas", [[]])[0]
        documents  = results.get("documents", [[]])[0]
        distances  = results.get("distances", [[]])[0]

        for i, rid in enumerate(ids):
            meta = metas[i] if i < len(metas) else {}
            doc  = documents[i] if i < len(documents) else ""
            dist = distances[i] if i < len(distances) else 1.0

            parsed.append({
                "id":           rid,
                "score":        round(1.0 - dist, 4),   # cosine: 距离→相似度
                "chapter_path": meta.get("chapter_path", meta.get("title", "")),
                "page_range":   _safe_json_load(meta.get("page_range", "[]")),
                "bboxes":       _safe_json_load(meta.get("bboxes", "[]")),
                "block_type":   meta.get("block_type", "text"),
                "pdf_file_id":  meta.get("pdf_file_id", ""),
                "raw_content":  meta.get("raw_content", doc),
                "document":     doc,
                # toc 专有
                "title":        meta.get("title", ""),
                "page_idx":     meta.get("page_idx", 0),
            })
        return parsed

    def stats(self) -> dict:
        return {
            "toc_count":    self._toc_col.count(),
            "blocks_count": self._blocks_col.count(),
            "persist_dir":  self._persist_dir,
        }


# ─────────────────────────────────────────────
# 关键词重排序（轻量 BM25-like）
# ─────────────────────────────────────────────

def _rerank_by_keyword(query: str, candidates: list[dict]) -> list[dict]:
    """
    用查询关键词对候选块打分叠加，与向量分数加权融合。
    简单策略：统计查询 token 在文档中出现次数，归一化后 × 0.3 叠加到向量分数。
    """
    tokens = re.findall(r"[\u4e00-\u9fff]{1,4}|[A-Za-z0-9]+", query)
    if not tokens:
        return candidates

    for item in candidates:
        text  = (item.get("raw_content") or item.get("document") or "").lower()
        hits  = sum(text.count(tok.lower()) for tok in tokens)
        kw_score = min(hits / (len(tokens) * 3 + 1), 1.0)
        item["score"] = round(item["score"] * 0.7 + kw_score * 0.3, 4)

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


def _safe_json_load(s: Any) -> Any:
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return []


# ─────────────────────────────────────────────
# CLI 入口：重建索引
# ─────────────────────────────────────────────

def _auto_find_processed(data_dir: str) -> list[tuple[str, str]]:
    """
    在 data_dir/processed/ 下自动匹配所有 *_toc.json + *_semantic_blocks.json 对。
    返回 [(toc_path, blocks_path), ...]
    """
    processed = Path(data_dir) / "processed"
    if not processed.exists():
        raise FileNotFoundError(f"processed 目录不存在：{processed}，请先运行 data_processor.py")

    pairs: list[tuple[str, str]] = []
    for toc_file in sorted(processed.glob("*_toc.json")):
        pdf_id = toc_file.stem.replace("_toc", "")
        blocks_file = processed / f"{pdf_id}_semantic_blocks.json"
        if blocks_file.exists():
            pairs.append((str(toc_file), str(blocks_file)))
        else:
            print(f"  警告：找到 {toc_file.name} 但未找到对应的语义块文件，跳过。")

    if not pairs:
        raise FileNotFoundError(f"在 {processed} 中未找到任何 *_toc.json 文件。")
    return pairs


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="构建/更新向量索引")
    parser.add_argument("--toc",      default=None, help="目录 JSON 文件路径（不填则自动扫描 data_dir/processed/）")
    parser.add_argument("--blocks",   default=None, help="语义块 JSON 文件路径（不填则自动扫描）")
    parser.add_argument("--data_dir", default="./data",     help="数据目录，用于自动扫描 processed/，默认 ./data")
    parser.add_argument("--db_dir",   default="./chroma_db", help="ChromaDB 持久化目录")
    args = parser.parse_args()

    vs = VectorStore(persist_dir=args.db_dir)

    if args.toc and args.blocks:
        # 手动指定模式
        vs.build_index_from_files(toc_path=args.toc, blocks_path=args.blocks)
    else:
        # 自动扫描模式
        pairs = _auto_find_processed(args.data_dir)
        print(f"自动发现 {len(pairs)} 个文档待索引...")
        for toc_path, blocks_path in pairs:
            print(f"  → {Path(toc_path).stem}")
            vs.build_index_from_files(toc_path=toc_path, blocks_path=blocks_path)

    print("索引统计:", vs.stats())
