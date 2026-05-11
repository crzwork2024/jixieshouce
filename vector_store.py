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
    CHILD_CHUNKS_COLLECTION_NAME, PARENT_BLOCKS_COLLECTION_NAME,
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
        # 原有 collection（向后兼容）
        self._toc_col    = self._get_or_create(TOC_COLLECTION_NAME)
        self._blocks_col = self._get_or_create(BLOCKS_COLLECTION_NAME)
        # 父子块 collection
        self._child_col  = self._get_or_create(CHILD_CHUNKS_COLLECTION_NAME)
        # 父块存储（不需要嵌入，只存 metadata；此处用独立 collection 无嵌入函数）
        self._parent_col = self._client.get_or_create_collection(
            name=PARENT_BLOCKS_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

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

    def index_child_chunks(self, child_blocks: list[dict]) -> None:
        """将子块写入 child_chunks collection（用于向量搜索）。"""
        if not child_blocks:
            return

        existing_ids = set(
            self._child_col.get(ids=[b["block_id"] for b in child_blocks])["ids"]
        )
        new_blocks = [b for b in child_blocks if b["block_id"] not in existing_ids]
        if not new_blocks:
            print("  子块已全部存在，跳过索引。")
            return

        print(f"  正在为 {len(new_blocks)} 个子块生成嵌入...")
        ids       = [b["block_id"]    for b in new_blocks]
        documents = [b["search_text"] for b in new_blocks]
        metadatas = [{
            "parent_block_id": b.get("parent_block_id", ""),
            "chapter_path":    b.get("chapter_path", ""),
            "page_range":      json.dumps(b.get("page_range", [])),
            "bboxes":          json.dumps(b.get("bboxes", [])),
            "block_type":      b.get("block_type", "text"),
            "pdf_file_id":     b.get("pdf_file_id", ""),
            "raw_content":     b.get("raw_content", "")[:500],
        } for b in new_blocks]

        batch = 100
        for i in range(0, len(new_blocks), batch):
            self._child_col.add(
                ids       = ids[i:i+batch],
                documents = documents[i:i+batch],
                metadatas = metadatas[i:i+batch],
            )
            print(f"    已写入子块 {min(i+batch, len(new_blocks))}/{len(new_blocks)}...")
        print("  子块索引完成。")

    def index_parent_blocks(self, parent_blocks: list[dict]) -> None:
        """将父块元数据写入 parent_blocks collection（不需要嵌入，仅存储完整内容）。"""
        if not parent_blocks:
            return

        existing_ids = set(
            self._parent_col.get(ids=[b["parent_block_id"] for b in parent_blocks])["ids"]
        )
        new_blocks = [b for b in parent_blocks if b["parent_block_id"] not in existing_ids]
        if not new_blocks:
            print("  父块已全部存在，跳过。")
            return

        print(f"  写入 {len(new_blocks)} 个父块元数据...")
        ids       = [b["parent_block_id"] for b in new_blocks]
        # parent_col 没有 embedding_function，documents 只是占位文本
        documents = [b.get("search_text", "")[:200] for b in new_blocks]
        metadatas = [{
            "parent_type":     b.get("parent_type", "section"),
            "chapter_path":    b.get("chapter_path", ""),
            "page_range":      json.dumps(b.get("page_range", [])),
            "bboxes":          json.dumps(b.get("bboxes", [])),
            "child_block_ids": json.dumps(b.get("child_block_ids", [])),
            "pdf_file_id":     b.get("pdf_file_id", ""),
            # 完整 raw_content（含 HTML 表格 / 图片标记）存在 metadata 中
            # ChromaDB metadata value 最大 ~1MB，但单字段建议 ≤ 64KB
            "raw_content":     b.get("raw_content", ""),
        } for b in new_blocks]

        batch = 50
        for i in range(0, len(new_blocks), batch):
            self._parent_col.add(
                ids       = ids[i:i+batch],
                documents = documents[i:i+batch],
                metadatas = metadatas[i:i+batch],
            )
            print(f"    已写入父块 {min(i+batch, len(new_blocks))}/{len(new_blocks)}...")
        print("  父块存储完成。")

    def get_parent_block(self, parent_block_id: str) -> dict | None:
        """按 parent_block_id 从 parent_blocks collection 拉取完整父块。"""
        result = self._parent_col.get(ids=[parent_block_id])
        if not result["ids"]:
            return None
        meta = result["metadatas"][0]
        return {
            "parent_block_id": result["ids"][0],
            "parent_type":     meta.get("parent_type", "section"),
            "chapter_path":    meta.get("chapter_path", ""),
            "page_range":      _safe_json_load(meta.get("page_range", "[]")),
            "bboxes":          _safe_json_load(meta.get("bboxes", "[]")),
            "child_block_ids": _safe_json_load(meta.get("child_block_ids", "[]")),
            "pdf_file_id":     meta.get("pdf_file_id", ""),
            "raw_content":     meta.get("raw_content", ""),
        }

    def build_index_from_files(
        self,
        toc_path: str,
        blocks_path: str,
        parent_path: str | None = None,
        child_path:  str | None = None,
    ) -> None:
        """从预处理输出文件重建向量索引（支持父子块模式）。"""
        print(f"加载目录文件: {toc_path}")
        toc = json.loads(Path(toc_path).read_text(encoding="utf-8"))

        print(f"加载语义块文件: {blocks_path}")
        blocks = json.loads(Path(blocks_path).read_text(encoding="utf-8"))

        self.index_toc(toc)
        self.index_blocks(blocks)

        if parent_path and child_path:
            print(f"加载父块文件: {parent_path}")
            parent_blocks = json.loads(Path(parent_path).read_text(encoding="utf-8"))
            print(f"加载子块文件: {child_path}")
            child_blocks = json.loads(Path(child_path).read_text(encoding="utf-8"))
            self.index_parent_blocks(parent_blocks)
            self.index_child_chunks(child_blocks)

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
        第二阶段：父子块精细检索。
        优先在 child_chunks 搜索（若已建立父子索引），
        命中子块后 → 按 parent_block_id 去重 → 拉取完整父块。
        若 child_chunks 为空，降级到旧版 blocks_collection 检索。
        """
        use_parent_child = self._child_col.count() > 0

        if use_parent_child:
            return self._query_blocks_parent_child(
                query, chapter_paths, top_k, block_type_filter, pdf_file_id
            )
        else:
            return self._query_blocks_legacy(
                query, chapter_paths, top_k, block_type_filter, pdf_file_id
            )

    def _query_blocks_parent_child(
        self,
        query: str,
        chapter_paths: list[str] | None,
        top_k: int,
        block_type_filter: str | None,
        pdf_file_id: str | None,
    ) -> list[dict]:
        """子→父检索：搜索 child_chunks → 拉取完整父块。"""
        where_conditions: list[dict] = []
        if pdf_file_id:
            where_conditions.append({"pdf_file_id": {"$eq": pdf_file_id}})
        if block_type_filter:
            where_conditions.append({"block_type": {"$eq": block_type_filter}})
        # 不在 Chroma 侧用 chapter_path $in：与层级路径不兼容，改为向量检索后再 Python 过滤

        where = None
        if len(where_conditions) == 1:
            where = where_conditions[0]
        elif len(where_conditions) > 1:
            where = {"$and": where_conditions}

        total = self._child_col.count()
        fetch_n = min(total, max(top_k * 15, 60))
        results = self._child_col.query(
            query_texts=[query],
            n_results=max(1, fetch_n),
            where=where,
        )
        child_hits = self._parse_results(results)
        child_hits = _apply_toc_filter_smart(child_hits, chapter_paths)

        # 按 parent_block_id 去重，保留最高分
        seen_parents: dict[str, dict] = {}
        for child in child_hits:
            pid = (child.get("parent_block_id") or "").strip() or child["id"]
            if pid not in seen_parents or child["score"] > seen_parents[pid]["score"]:
                seen_parents[pid] = child

        # 排序，取 top_k 个父块
        top_parents = sorted(seen_parents.values(), key=lambda x: x["score"], reverse=True)[:top_k]

        # 拉取完整父块（含完整 raw_content / HTML 表格 / 图片）
        enriched: list[dict] = []
        for child_hit in top_parents:
            pid = (child_hit.get("parent_block_id") or "").strip() or child_hit["id"]
            parent = self.get_parent_block(pid)
            if parent:
                enriched.append({
                    **parent,
                    "id":        pid,
                    "score":     child_hit["score"],
                    "block_type": parent.get("parent_type", "section"),
                })
            else:
                # 父块不存在（数据不一致），直接使用子块信息
                enriched.append({**child_hit, "id": child_hit["id"]})

        # 关键词重排序
        return _rerank_by_keyword(query, enriched)[:top_k]

    def _query_blocks_legacy(
        self,
        query: str,
        chapter_paths: list[str] | None,
        top_k: int,
        block_type_filter: str | None,
        pdf_file_id: str | None,
    ) -> list[dict]:
        """旧版检索（向后兼容，当 child_chunks 为空时使用）。"""
        where_conditions: list[dict] = []
        if pdf_file_id:
            where_conditions.append({"pdf_file_id": {"$eq": pdf_file_id}})
        if block_type_filter:
            where_conditions.append({"block_type": {"$eq": block_type_filter}})
        # chapter_path：不在 DB 侧 $in（层级路径与 TOC 短标题不一致）

        where = None
        if len(where_conditions) == 1:
            where = where_conditions[0]
        elif len(where_conditions) > 1:
            where = {"$and": where_conditions}

        total = self._blocks_col.count()
        if total == 0:
            return []

        fetch_n = min(total, max(top_k * 12, 48))
        results = self._blocks_col.query(
            query_texts=[query],
            n_results=max(1, fetch_n),
            where=where,
        )
        candidates = self._parse_results(results)
        candidates = _apply_toc_filter_smart(candidates, chapter_paths)
        ranked = _rerank_by_keyword(query, candidates)
        return ranked[:top_k]

    def get_block_by_id(self, block_id: str) -> dict | None:
        """
        按 ID 获取块信息。
        优先在 parent_blocks 查找（返回完整父块含完整 raw_content），
        其次在 child_chunks 找（返回子块），
        最后降级到旧 blocks_collection。
        """
        # 1. 尝试父块
        parent = self.get_parent_block(block_id)
        if parent:
            return {
                "block_id":     parent["parent_block_id"],
                "raw_content":  parent["raw_content"],
                "chapter_path": parent["chapter_path"],
                "page_range":   parent["page_range"],
                "bboxes":       parent["bboxes"],
                "block_type":   parent.get("parent_type", "section"),
                "pdf_file_id":  parent["pdf_file_id"],
                "document":     "",
            }

        # 2. 尝试子块
        result = self._child_col.get(ids=[block_id])
        if result["ids"]:
            meta = result["metadatas"][0]
            # 若子块有父块，返回父块
            pid = meta.get("parent_block_id", "")
            if pid:
                parent = self.get_parent_block(pid)
                if parent:
                    return {
                        "block_id":     parent["parent_block_id"],
                        "raw_content":  parent["raw_content"],
                        "chapter_path": parent["chapter_path"],
                        "page_range":   parent["page_range"],
                        "bboxes":       parent["bboxes"],
                        "block_type":   parent.get("parent_type", "section"),
                        "pdf_file_id":  parent["pdf_file_id"],
                        "document":     "",
                    }
            return {
                "block_id":     result["ids"][0],
                "raw_content":  meta.get("raw_content", ""),
                "chapter_path": meta.get("chapter_path", ""),
                "page_range":   _safe_json_load(meta.get("page_range", "[]")),
                "bboxes":       _safe_json_load(meta.get("bboxes", "[]")),
                "block_type":   meta.get("block_type", "text"),
                "pdf_file_id":  meta.get("pdf_file_id", ""),
                "document":     result["documents"][0] if result["documents"] else "",
            }

        # 3. 降级到旧 blocks_collection
        result = self._blocks_col.get(ids=[block_id])
        if not result["ids"]:
            return None
        meta = result["metadatas"][0]
        return {
            "block_id":     result["ids"][0],
            "raw_content":  meta.get("raw_content", ""),
            "chapter_path": meta.get("chapter_path", ""),
            "page_range":   _safe_json_load(meta.get("page_range", "[]")),
            "bboxes":       _safe_json_load(meta.get("bboxes", "[]")),
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
                # child_chunks 写入，用于子→父聚合（此前遗漏会导致永远按子块 id 取父块失败）
                "parent_block_id": (meta.get("parent_block_id") or "").strip(),
                # toc 专有
                "title":        meta.get("title", ""),
                "page_idx":     meta.get("page_idx", 0),
            })
        return parsed

    def stats(self) -> dict:
        return {
            "toc_count":          self._toc_col.count(),
            "blocks_count":       self._blocks_col.count(),
            "child_chunks_count": self._child_col.count(),
            "parent_blocks_count": self._parent_col.count(),
            "persist_dir":        self._persist_dir,
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


def _chapter_path_matches_toc_filter(block_path: str, toc_hints: list[str]) -> bool:
    """
    TOC 命中节点多为短标题（如「5 字体…」「字体的基本要求」），
    而子块 chapter_path 常为「大节 > 小节」层级字符串。
    Chroma 的 metadata $in 精确匹配会把绝大部分正文子块全部过滤掉，
    导致只能命中少数路径恰好等于短标题的块（多为标题行）。
    """
    if not toc_hints:
        return True
    bp = (block_path or "").strip()
    if not bp:
        return False
    segments = [s.strip() for s in re.split(r"\s*>\s*", bp) if s.strip()]
    for hint in toc_hints:
        h = (hint or "").strip()
        if not h:
            continue
        if bp == h:
            return True
        if h in bp:
            return True
        if h in segments:
            return True
        if bp.startswith(h + " >") or bp.startswith(h + ">"):
            return True
    return False


def _apply_toc_filter_smart(
    child_hits: list[dict],
    chapter_paths: list[str] | None,
) -> list[dict]:
    """按 TOC 命中过滤；若过滤后最高分明显低于未过滤头部，回退为不过滤（减轻误杀）。"""
    if not chapter_paths or not child_hits:
        return child_hits
    filtered = [
        c for c in child_hits
        if _chapter_path_matches_toc_filter(c.get("chapter_path", ""), chapter_paths)
    ]
    if not filtered:
        return child_hits
    top_u = max(c["score"] for c in child_hits)
    top_f = max(c["score"] for c in filtered)
    if top_f + 0.06 < top_u:
        return child_hits
    return filtered


# ─────────────────────────────────────────────
# CLI 入口：重建索引
# ─────────────────────────────────────────────

def _auto_find_processed(data_dir: str) -> list[dict]:
    """
    在 data_dir/processed/ 下自动匹配所有文档的预处理文件。
    返回包含各路径的 dict 列表。
    """
    processed = Path(data_dir) / "processed"
    if not processed.exists():
        raise FileNotFoundError(f"processed 目录不存在：{processed}，请先运行 data_processor.py")

    entries: list[dict] = []
    for toc_file in sorted(processed.glob("*_toc.json")):
        pdf_id = toc_file.stem.replace("_toc", "")
        blocks_file  = processed / f"{pdf_id}_semantic_blocks.json"
        parent_file  = processed / f"{pdf_id}_parent_blocks.json"
        child_file   = processed / f"{pdf_id}_child_blocks.json"

        if not blocks_file.exists():
            print(f"  警告：找到 {toc_file.name} 但未找到对应的语义块文件，跳过。")
            continue

        entry = {
            "toc_path":    str(toc_file),
            "blocks_path": str(blocks_file),
            "parent_path": str(parent_file)  if parent_file.exists()  else None,
            "child_path":  str(child_file)   if child_file.exists()   else None,
        }
        entries.append(entry)

    if not entries:
        raise FileNotFoundError(f"在 {processed} 中未找到任何 *_toc.json 文件。")
    return entries


def _reset_chroma_collections(persist_dir: str) -> None:
    """删除持久化目录中的全部业务 collection，用于全量重建索引。"""
    client = chromadb.PersistentClient(
        path=persist_dir,
        settings=Settings(anonymized_telemetry=False),
    )
    for name in (
        TOC_COLLECTION_NAME,
        BLOCKS_COLLECTION_NAME,
        CHILD_CHUNKS_COLLECTION_NAME,
        PARENT_BLOCKS_COLLECTION_NAME,
    ):
        try:
            client.delete_collection(name)
            print(f"  已删除 collection: {name}")
        except Exception as e:
            print(f"  删除 {name} 时跳过或失败: {e}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="构建/更新向量索引")
    parser.add_argument("--toc",      default=None, help="目录 JSON 文件路径")
    parser.add_argument("--blocks",   default=None, help="语义块 JSON 文件路径")
    parser.add_argument("--parent",   default=None, help="父块 JSON 文件路径")
    parser.add_argument("--child",    default=None, help="子块 JSON 文件路径")
    parser.add_argument("--data_dir", default="./data",     help="数据目录（自动扫描 processed/），默认 ./data")
    parser.add_argument("--db_dir",   default="./chroma_db", help="ChromaDB 持久化目录")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="重建前清空向量库中的全部 collection（无需手动删文件夹；若仍报错请先关闭占用库的 API 服务）",
    )
    args = parser.parse_args()

    if args.reset:
        print(f"重置向量库（清空 collections）: {args.db_dir}")
        _reset_chroma_collections(args.db_dir)

    vs = VectorStore(persist_dir=args.db_dir)

    if args.toc and args.blocks:
        vs.build_index_from_files(
            toc_path    = args.toc,
            blocks_path = args.blocks,
            parent_path = args.parent,
            child_path  = args.child,
        )
    else:
        entries = _auto_find_processed(args.data_dir)
        print(f"自动发现 {len(entries)} 个文档待索引...")
        for entry in entries:
            print(f"  → {Path(entry['toc_path']).stem}")
            has_parent = entry["parent_path"] and entry["child_path"]
            if has_parent:
                print(f"    检测到父子块文件，将使用父子块架构索引。")
            vs.build_index_from_files(
                toc_path    = entry["toc_path"],
                blocks_path = entry["blocks_path"],
                parent_path = entry.get("parent_path"),
                child_path  = entry.get("child_path"),
            )

    print("索引统计:", vs.stats())
