"""
rag_engine.py
两级检索引擎 + 回答生成模块

流程：
1. 目录级意图检索（第一阶段）→ 获取 Top-K 相关章节
2. 章节范围内语义块精细检索（第二阶段）→ 获取 Top-N 语义块
3. 多模态感知：识别问题中的图片/表格关键词，优先检索对应类型
4. 引用去重与聚合
5. 调用 DeepSeek LLM 生成严格溯源回答
"""

import base64
import json
import re
from typing import Any, Generator

import requests

from config import (
    SILICONFLOW_API_KEY,
    LLM_MODEL, LLM_ENDPOINT, LLM_TEMPERATURE, LLM_MAX_TOKENS,
    TOP_K_TOC, TOP_K_BLOCKS,
)
from vector_store import VectorStore

# 触发多模态优先检索的关键词映射
MULTIMODAL_KEYWORDS = {
    "table": [
        "表", "表格", "数据表", "参数表", "对照表", "标准表",
        "tolerance table", "公差表", "配合表",
    ],
    "image": [
        "图", "示意图", "图示", "图例", "图解", "示例图",
        "插图", "原理图", "结构图",
    ],
}

# ─────────────────────────────────────────────
# 提示词模板
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """你是一位专业的机械工程技术文档助手，擅长解读 GB 标准、公差配合、几何公差等机械设计规范。

回答规则（必须严格遵守）：
1. 所有内容必须 100% 来自提供的参考材料，绝对禁止编造任何数据、标准号或技术参数
2. 每个事实点后必须标注对应的引用编号，格式为 [1]、[2] 等
3. 如果参考材料中包含表格，请在回答对应位置插入占位符：{{TABLE:block_id}}
4. 如果参考材料中包含图片，请在回答对应位置插入占位符：{{IMAGE:block_id}}
5. 如果参考材料不足以完整回答问题，明确说明"参考文档中未找到完整信息"，不得推测补充
6. 回答语言为中文，专业术语与原文保持一致
7. 回答结构：先给出简洁的直接答案，再列出详细说明，最后列出参考文献"""

ANSWER_PROMPT_TEMPLATE = """用户问题：{query}

参考材料（共 {n_refs} 条，请基于以下内容作答）：

{context}

请严格按照上述规则生成回答，确保每个陈述都有对应的引用编号。"""


def _build_context(blocks: list[dict]) -> str:
    """
    将检索到的语义块（含父块）格式化为 LLM 上下文。
    父块 raw_content 包含完整 HTML 表格和图片标记，不额外截断，
    让 LLM 能看到完整表格数据。
    """
    parts = []
    for i, block in enumerate(blocks, 1):
        page_range  = block.get("page_range", [])
        chapter     = block.get("chapter_path", "")
        block_type  = block.get("block_type", "text")
        block_id    = block.get("id", block.get("block_id", ""))
        raw_content = block.get("raw_content", block.get("document", ""))

        page_str = (
            f"第{page_range[0]+1}页"
            if len(page_range) == 1
            else f"第{page_range[0]+1}-{page_range[-1]+1}页"
        )

        # 父块类型映射
        type_label_map = {
            "table":      "[表格]",
            "table_unit": "[表格单元]",
            "image":      "[图片]",
            "title":      "[标题]",
            "section":    "[章节]",
            "list":       "[列表]",
        }
        type_label = type_label_map.get(block_type, "[文本]")

        # 父块 (table_unit / section) 包含完整表格 HTML，不截断（最多 6000 字符）
        # 普通文本块保留原有限制
        max_chars = 6000 if block_type in ("table_unit", "table") else 2000
        content_str = raw_content[:max_chars] + ("..." if len(raw_content) > max_chars else "")

        parts.append(
            f"[{i}] {type_label} 章节：{chapter} | {page_str} | block_id={block_id}\n"
            f"{content_str}"
        )
    return "\n\n---\n\n".join(parts)


# ─────────────────────────────────────────────
# 引用处理工具
# ─────────────────────────────────────────────

def _dedup_and_sort_blocks(blocks: list[dict]) -> list[dict]:
    """去除重复块，按页码和位置排序。"""
    seen: set[str] = set()
    unique: list[dict] = []
    for b in blocks:
        bid = b.get("id", b.get("block_id", ""))
        if bid not in seen:
            seen.add(bid)
            unique.append(b)

    unique.sort(key=lambda x: (
        min(x.get("page_range", [0])),
        x.get("bboxes", [{"bbox": [0]}])[0].get("bbox", [0])[1],
    ))
    return unique


def _build_pdf_ref_url(block: dict) -> str:
    """生成 PDF 精确跳转链接，bboxes 数组 base64 编码。"""
    pdf_id     = block.get("pdf_file_id", "unknown")
    page_range = block.get("page_range", [0])
    bboxes     = block.get("bboxes", [])
    pages_str  = ",".join(str(p) for p in page_range)
    bboxes_b64 = base64.b64encode(json.dumps(bboxes).encode()).decode()
    return f"pdf://{pdf_id}?pages={pages_str}&bboxes={bboxes_b64}"


def _detect_multimodal_intent(query: str) -> str | None:
    """检测查询中是否有图片/表格意图，返回 'table' / 'image' / None。"""
    for btype, keywords in MULTIMODAL_KEYWORDS.items():
        for kw in keywords:
            if kw in query:
                return btype
    return None


# ─────────────────────────────────────────────
# LLM 调用
# ─────────────────────────────────────────────

def _call_llm(messages: list[dict], stream: bool = False) -> Any:
    if not SILICONFLOW_API_KEY:
        raise EnvironmentError("请设置环境变量 SILICONFLOW_API_KEY")

    payload = {
        "model": LLM_MODEL,
        "messages": messages,
        "temperature": LLM_TEMPERATURE,
        "max_tokens": LLM_MAX_TOKENS,
        "stream": stream,
    }
    resp = requests.post(
        LLM_ENDPOINT,
        headers={
            "Authorization": f"Bearer {SILICONFLOW_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=120,
        stream=stream,
    )
    resp.raise_for_status()
    return resp


def _stream_llm(messages: list[dict]) -> Generator[str, None, None]:
    """流式调用 LLM，逐 token yield。"""
    resp = _call_llm(messages, stream=True)
    for line in resp.iter_lines():
        if not line:
            continue
        line = line.decode("utf-8")
        if line.startswith("data: "):
            data = line[6:]
            if data == "[DONE]":
                break
            try:
                chunk = json.loads(data)
                delta = chunk["choices"][0]["delta"]
                content = delta.get("content", "")
                if content:
                    yield content
            except Exception:
                continue


# ─────────────────────────────────────────────
# RAG 引擎
# ─────────────────────────────────────────────

class RAGEngine:
    def __init__(self, vector_store: VectorStore):
        self.vs = vector_store

    # ────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────

    def retrieve(
        self,
        query: str,
        pdf_file_id: str | None = None,
        top_k_toc: int = TOP_K_TOC,
        top_k_blocks: int = TOP_K_BLOCKS,
    ) -> dict:
        """
        执行两级检索，返回检索结果（不含 LLM 生成回答）。
        """
        # 多模态意图检测
        modal_filter = _detect_multimodal_intent(query)

        # 第一阶段：目录检索
        toc_results = self.vs.query_toc(
            query=query, top_k=top_k_toc, pdf_file_id=pdf_file_id
        )
        chapter_paths = list({r["chapter_path"] for r in toc_results if r["chapter_path"]})

        # 第二阶段：语义块检索
        # 含「表/图」等多模态词时：单独放大 table/image 通道的 top_k，避免大块 section 挤掉目标表
        modal_k = max(top_k_blocks, 8) if modal_filter else top_k_blocks
        text_k = top_k_blocks + 2 if modal_filter else top_k_blocks

        blocks: list[dict] = []
        if modal_filter:
            modal_blocks = self.vs.query_blocks(
                query=query,
                chapter_paths=chapter_paths or None,
                top_k=modal_k,
                block_type_filter=modal_filter,
                pdf_file_id=pdf_file_id,
            )
            blocks.extend(modal_blocks)

        text_blocks = self.vs.query_blocks(
            query=query,
            chapter_paths=chapter_paths or None,
            top_k=text_k,
            block_type_filter=None,
            pdf_file_id=pdf_file_id,
        )
        blocks.extend(text_blocks)

        blocks = _dedup_and_sort_blocks(blocks)
        if modal_filter == "table":
            tbl_types = ("table", "table_unit")
            tbl = [b for b in blocks if b.get("block_type") in tbl_types]
            rest = [b for b in blocks if b.get("block_type") not in tbl_types]
            blocks = tbl[:4] + rest
        elif modal_filter == "image":
            img = [b for b in blocks if b.get("block_type") == "image"]
            rest = [b for b in blocks if b.get("block_type") != "image"]
            blocks = img[:4] + rest
        blocks = blocks[:top_k_blocks]

        return {
            "query":         query,
            "toc_results":   toc_results,
            "chapter_paths": chapter_paths,
            "blocks":        blocks,
            "modal_filter":  modal_filter,
        }

    def answer(
        self,
        query: str,
        pdf_file_id: str | None = None,
        stream: bool = False,
    ) -> dict:
        """
        完整 RAG 问答流程：检索 → 构建上下文 → LLM 生成 → 后处理。
        """
        retrieval = self.retrieve(query=query, pdf_file_id=pdf_file_id)
        blocks = retrieval["blocks"]

        if not blocks:
            return {
                "query":      query,
                "answer":     "参考文档中未找到与该问题相关的内容，请尝试换用其他关键词。",
                "references": [],
                "blocks":     [],
            }

        context = _build_context(blocks)
        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": ANSWER_PROMPT_TEMPLATE.format(
                    query=query,
                    n_refs=len(blocks),
                    context=context,
                ),
            },
        ]

        if stream:
            # 流式模式：返回生成器，调用方自行处理
            return {
                "query":      query,
                "stream":     _stream_llm(messages),
                "references": self._build_references(blocks),
                "blocks":     blocks,
            }

        # 非流式
        resp = _call_llm(messages, stream=False)
        raw_answer = resp.json()["choices"][0]["message"]["content"]
        processed_answer = self._post_process_answer(raw_answer, blocks)
        refs = self._build_references(blocks)
        refs = filter_references_by_answer_citations(processed_answer, refs)

        return {
            "query":      query,
            "answer":     processed_answer,
            "references": refs,
            "blocks":     blocks,
        }

    # ────────────────────────────────────────
    # 后处理
    # ────────────────────────────────────────

    def _build_references(self, blocks: list[dict]) -> list[dict]:
        """为每个引用块生成标准参考文献条目（含完整 raw_content 供前端渲染）。"""
        refs = []
        for i, block in enumerate(blocks, 1):
            page_range  = block.get("page_range", [0])
            chapter     = block.get("chapter_path", "")
            block_type  = block.get("block_type", "text")
            raw_content = block.get("raw_content", block.get("document", ""))
            block_id    = block.get("id", block.get("block_id", ""))

            # 预览文本（供 tooltip / 折叠显示）
            is_table = block_type in ("table", "table_unit")
            is_image = block_type == "image"
            if is_table:
                preview = _table_preview(raw_content)
            elif is_image:
                preview = "[图片内容]"
            else:
                preview = raw_content[:300].strip()

            page_label = (
                f"第{page_range[0]+1}页"
                if len(page_range) == 1
                else f"第{page_range[0]+1}-{page_range[-1]+1}页"
            )

            # 判断 raw_content 中是否含 HTML 表格
            has_html_table = "<table" in raw_content.lower() if raw_content else False
            # 提取 Markdown 图片路径，规范化为 images/ 下的相对文件名（无双斜杠）
            img_paths = _normalized_img_paths_from_raw(raw_content)

            refs.append({
                "index":         i,
                "block_id":      block_id,
                "chapter":       chapter,
                "page_label":    page_label,
                "block_type":    block_type,
                "preview":       preview,
                "raw_content":   raw_content,   # 完整内容，前端用于渲染 HTML 表格和图片
                "has_html_table": has_html_table,
                "img_paths":     img_paths,
                "pdf_ref_url":   _build_pdf_ref_url(block),
                "page_range":    page_range,
                "bboxes":        block.get("bboxes", []),
                "pdf_file_id":   block.get("pdf_file_id", ""),
            })
        return refs

    def _post_process_answer(self, raw: str, blocks: list[dict]) -> str:
        """
        将 LLM 输出中的 {{TABLE:id}} / {{IMAGE:id}} 占位符转换为
        前端可识别的特殊 HTML 注释标记（<!-- EMBED_TABLE:id --> / <!-- EMBED_IMAGE:id -->）。
        前端 ChatPanel 会将这些标记替换为实际渲染内容。
        """
        out = re.sub(
            r"\{\{TABLE:([^}]+)\}\}",
            lambda m: f"\n\n<!-- EMBED_TABLE:{m.group(1)} -->\n\n",
            raw,
        )
        out = re.sub(
            r"\{\{IMAGE:([^}]+)\}\}",
            lambda m: f"\n\n<!-- EMBED_IMAGE:{m.group(1)} -->\n\n",
            out,
        )
        return out.strip()


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _normalized_img_paths_from_raw(raw: str | None) -> list[str]:
    """从 raw_content 中解析 ![alt](url)，规范化为 images/ 目录下的相对文件名。"""
    out: list[str] = []
    seen: set[str] = set()
    for m in re.finditer(r"!\[[^\]]*\]\(([^)]+)\)", raw or ""):
        u = m.group(1).strip().strip("\"'")
        u = u.replace("\\", "/")
        while "//" in u:
            u = u.replace("//", "/")
        u = u.lstrip("/")
        if u.lower().startswith("images/"):
            u = u[7:].lstrip("/")
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


def filter_references_by_answer_citations(answer_text: str, references: list[dict]) -> list[dict]:
    """
    仅保留回答中实际引用到的参考文献条目：
    - 角标 [1]、[2] …
    - 正文中的图表占位（post 后为 <!-- EMBED_*:block_id -->，或未转换的 {{TABLE:id}} / {{IMAGE:id}}）

    若未检测到任何引用符号且无任何占位符，则返回原列表（避免模型漏标时侧边栏为空）。
    """
    if not references or not (answer_text or "").strip():
        return list(references)

    cited_idx = {int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", answer_text)}
    embed_ids: set[str] = set()
    embed_ids.update(re.findall(r"<!--\s*EMBED_(?:TABLE|IMAGE):([^\s>]+)\s*-->", answer_text))
    embed_ids.update(re.findall(r"\{\{IMAGE:([^}]+)\}\}", answer_text))
    embed_ids.update(re.findall(r"\{\{TABLE:([^}]+)\}\}", answer_text))

    has_signal = bool(cited_idx or embed_ids)
    if not has_signal:
        return list(references)

    by_idx = {r["index"]: r for r in references}
    picked: dict[str, dict] = {}

    for idx in sorted(cited_idx):
        r = by_idx.get(idx)
        if r:
            picked[r["block_id"]] = r

    if embed_ids:
        by_block = {r["block_id"]: r for r in references}
        for bid in embed_ids:
            if bid in by_block:
                picked[bid] = by_block[bid]

    if not picked:
        return list(references)

    out = list(picked.values())
    out.sort(key=lambda r: r["index"])
    return out


def _table_preview(html: str, max_rows: int = 3) -> str:
    """提取表格前几行作为预览文本。"""
    rows = re.findall(r"<tr>(.*?)</tr>", html, re.DOTALL)
    preview_rows = rows[:max_rows]
    cells_per_row = []
    for row in preview_rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.DOTALL)
        clean = [re.sub(r"<[^>]+>", "", c).strip() for c in cells]
        cells_per_row.append(" | ".join(clean[:5]))
    return "\n".join(cells_per_row)
