"""
data_processor.py
MinerU 解析产物预处理模块

功能：
1. 跨页内容自动修复（合并跨页表格/文本）
2. 结构化目录提取
3. 语义块标准化与元数据附加
"""

import json
import re
import uuid
import argparse
from pathlib import Path
from typing import Any
from html.parser import HTMLParser


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

class _TableTextExtractor(HTMLParser):
    """从 HTML 表格中提取纯文本，用于嵌入摘要。"""
    def __init__(self):
        super().__init__()
        self.texts: list[str] = []

    def handle_data(self, data: str):
        text = data.strip()
        if text:
            self.texts.append(text)

    def get_text(self) -> str:
        return " | ".join(self.texts)


def html_table_to_text(html: str) -> str:
    parser = _TableTextExtractor()
    parser.feed(html)
    return parser.get_text()


def normalize_bbox(bbox: list[float], page_size: list[float]) -> list[float]:
    """将绝对像素坐标转为 0-1 归一化坐标。"""
    w, h = page_size
    if w == 0 or h == 0:
        return bbox
    x1, y1, x2, y2 = bbox
    return [round(x1 / w, 4), round(y1 / h, 4), round(x2 / w, 4), round(y2 / h, 4)]


def merge_html_tables(html_parts: list[str]) -> str:
    """
    将多段 <table>…</table> 合并为一张表（去掉中间段的 <table><tr><td>表头</td></tr> 并拼接 tbody）。
    简单策略：保留第一段所有行 + 后续段中 <tr> 行（跳过重复表头行）。
    """
    if not html_parts:
        return ""
    if len(html_parts) == 1:
        return html_parts[0]

    all_rows: list[str] = []
    row_pattern = re.compile(r"<tr>.*?</tr>", re.DOTALL)

    for i, part in enumerate(html_parts):
        if not part.strip():
            continue
        rows = row_pattern.findall(part)
        if i == 0:
            all_rows.extend(rows)
        else:
            # 跳过与第一段相同的表头行（简单按文本去重首行）
            for row in rows:
                if row not in all_rows[:3]:   # 只对头部做去重
                    all_rows.append(row)

    return "<table>" + "".join(all_rows) + "</table>"


# ─────────────────────────────────────────────
# 核心处理类
# ─────────────────────────────────────────────

class MinerUProcessor:
    """
    MinerU 多粒度解析产物处理器。

    输入目录结构：
        <input_dir>/
        ├── block_list.json          # 包含 pdfData + mergeConnections
        ├── full.md
        └── images/
    """

    SKIP_TYPES = {"aside_text", "page_number", "discarded"}
    TITLE_TYPES = {"title"}
    TABLE_TYPES = {"table_body", "table"}
    IMAGE_TYPES = {"image", "image_body"}
    TEXT_TYPES  = {"text", "list", "table_caption"}

    def __init__(self, pdf_id: str, input_dir: str):
        self.pdf_id    = pdf_id
        self.input_dir = Path(input_dir)
        self.block_list_path = self.input_dir / "block_list.json"

        self._raw_pages: list[list[dict]] = []   # pdfData 原始页列表
        self._merge_map: dict[str, dict]  = {}   # id -> 合并后块
        self._toc: list[dict]             = []   # 目录树
        self._semantic_blocks: list[dict] = []   # 最终语义块列表
        self._chapter_stack: list[str]    = []   # 当前章节路径栈

    # ────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        """完整执行预处理流程，返回处理结果。"""
        print(f"[1/4] 加载 block_list.json ...")
        self._load_block_list()

        print(f"[2/4] 处理跨页合并关系 ...")
        self._process_merge_connections()

        print(f"[3/4] 提取目录结构 ...")
        self._extract_toc()

        print(f"[4/4] 生成语义块 ...")
        self._build_semantic_blocks()

        result = {
            "pdf_id": self.pdf_id,
            "toc": self._toc,
            "semantic_blocks": self._semantic_blocks,
            "total_blocks": len(self._semantic_blocks),
            "total_pages": len(self._raw_pages),
        }
        print(f"完成！共生成 {len(self._semantic_blocks)} 个语义块，{len(self._toc)} 个目录节点。")
        return result

    def save(self, output_dir: str | None = None) -> None:
        """将处理结果保存为 JSON 文件。"""
        out = Path(output_dir) if output_dir else self.input_dir / "processed"
        out.mkdir(parents=True, exist_ok=True)

        result = self.run()

        toc_path    = out / f"{self.pdf_id}_toc.json"
        blocks_path = out / f"{self.pdf_id}_semantic_blocks.json"

        toc_path.write_text(
            json.dumps(result["toc"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        blocks_path.write_text(
            json.dumps(result["semantic_blocks"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"目录保存至: {toc_path}")
        print(f"语义块保存至: {blocks_path}")

    # ────────────────────────────────────────
    # 内部实现
    # ────────────────────────────────────────

    def _load_block_list(self):
        with open(self.block_list_path, encoding="utf-8") as f:
            data = json.load(f)
        self._raw_pages = data.get("pdfData", [])
        self._merge_connections = data.get("mergeConnections", [])

    def _process_merge_connections(self):
        """
        根据 mergeConnections 将跨页块收集并合并。
        合并结果写入 self._merge_map，key 为合并块的唯一 id。
        同时在原始块中标记 is_merged_child=True，避免重复处理。
        """
        # 构建 block_position -> block 的快速索引
        pos_index: dict[str, dict] = {}
        for page in self._raw_pages:
            for block in page:
                pos = block.get("block_position", "")
                if pos:
                    pos_index[pos] = block

        for conn in self._merge_connections:
            merge_id   = conn["id"]
            block_keys = conn["blocks"]   # e.g. ["0-4", "1-2", "2-2"]
            conn_type  = conn.get("type", "merge")

            if conn_type != "merge":
                continue

            parts = [pos_index[k] for k in block_keys if k in pos_index]
            if not parts:
                continue

            first = parts[0]
            block_type = first.get("type", "")

            # 收集所有页的 bbox（归一化）
            bboxes = []
            page_range = []
            for p in parts:
                pidx = p.get("page_idx", 0)
                bbox = p.get("bbox", [0, 0, 0, 0])
                psize = p.get("page_size", [1, 1])
                bboxes.append({
                    "page_idx": pidx,
                    "bbox": normalize_bbox(bbox, psize)
                })
                if pidx not in page_range:
                    page_range.append(pidx)

            # 合并表格 HTML
            if block_type in self.TABLE_TYPES:
                html_parts = [p.get("table_body", "") or p.get("text", "") for p in parts]
                merged_html = merge_html_tables([h for h in html_parts if h])
                merged_text = html_table_to_text(merged_html)
                merged_block = {
                    "id": merge_id,
                    "type": "table",
                    "page_range": page_range,
                    "bboxes": bboxes,
                    "content": merged_html,
                    "search_text": merged_text,
                    "is_merged": True,
                }
            else:
                # 合并文本段
                texts = [p.get("text", "").strip() for p in parts if p.get("text", "").strip()]
                merged_text = "\n".join(texts)
                merged_block = {
                    "id": merge_id,
                    "type": block_type,
                    "page_range": page_range,
                    "bboxes": bboxes,
                    "content": merged_text,
                    "search_text": merged_text,
                    "is_merged": True,
                }

            self._merge_map[merge_id] = merged_block

            # 标记子块已被合并
            for p in parts:
                p["_is_merged_child"] = True
                p["_merge_parent_id"] = merge_id

    def _extract_toc(self):
        """
        从所有 title 类型块中提取章节层级，生成目录树。
        层级由 block 的 level 字段决定（默认为 1）。
        """
        toc_entries: list[dict] = []
        seen_ids: set[str] = set()

        for page in self._raw_pages:
            for block in page:
                if block.get("type") not in self.TITLE_TYPES:
                    continue
                if block.get("is_discarded"):
                    continue
                bid = block.get("id", "")
                if bid in seen_ids:
                    continue
                seen_ids.add(bid)

                level  = block.get("level", 1)
                text   = block.get("text", "").strip().lstrip("#").strip()
                pidx   = block.get("page_idx", 0)
                bbox   = block.get("bbox", [0, 0, 0, 0])
                psize  = block.get("page_size", [1, 1])

                toc_entries.append({
                    "toc_id":    str(uuid.uuid4()),
                    "block_id":  bid,
                    "title":     text,
                    "level":     level,
                    "page_idx":  pidx,
                    "bbox":      normalize_bbox(bbox, psize),
                    "pdf_file_id": self.pdf_id,
                    "search_text": text,
                })

        self._toc = toc_entries

    def _get_chapter_path(self, current_level: int, title: str) -> str:
        """维护章节路径栈，返回当前块的完整章节路径。"""
        # 调整栈深度以匹配当前层级
        while len(self._chapter_stack) >= current_level:
            self._chapter_stack.pop()
        self._chapter_stack.append(title)
        return " > ".join(self._chapter_stack)

    def _current_chapter_path(self) -> str:
        return " > ".join(self._chapter_stack) if self._chapter_stack else "未分类"

    def _build_semantic_blocks(self):
        """
        遍历所有原始页面块，生成语义完整的语义块列表。
        跨页合并块以 merge_map 中的结果为准。
        """
        emitted_merge_ids: set[str] = set()
        self._chapter_stack = []

        for page in self._raw_pages:
            for block in page:
                btype = block.get("type", "")

                # 跳过丢弃块、页码、边注
                if block.get("is_discarded") or btype in self.SKIP_TYPES:
                    continue

                # 更新章节路径
                if btype in self.TITLE_TYPES:
                    level = block.get("level", 1)
                    title = block.get("text", "").strip().lstrip("#").strip()
                    self._get_chapter_path(level, title)

                # 如果是合并子块，输出合并父块（仅一次）
                merge_parent_id = block.get("_merge_parent_id")
                if merge_parent_id:
                    if merge_parent_id not in emitted_merge_ids:
                        emitted_merge_ids.add(merge_parent_id)
                        merged = self._merge_map[merge_parent_id]
                        sem = self._make_semantic_block_from_merged(merged)
                        self._semantic_blocks.append(sem)
                    continue

                # 普通块直接转换
                sem = self._make_semantic_block_from_raw(block)
                if sem:
                    self._semantic_blocks.append(sem)

    def _make_semantic_block_from_raw(self, block: dict) -> dict | None:
        btype = block.get("type", "")
        pidx  = block.get("page_idx", 0)
        bbox  = block.get("bbox", [0, 0, 0, 0])
        psize = block.get("page_size", [1, 1])
        nbbox = normalize_bbox(bbox, psize)

        if btype in self.TITLE_TYPES:
            text = block.get("text", "").strip().lstrip("#").strip()
            raw_content = text
            search_text = text
        elif btype in self.TABLE_TYPES:
            html = block.get("table_body", "") or block.get("text", "")
            raw_content = html
            search_text = html_table_to_text(html)
        elif btype in self.IMAGE_TYPES:
            img_path = block.get("img_path", "")
            raw_content = f"![图片](images/{img_path})" if img_path else "![图片]"
            search_text = f"图片: {img_path}"
        else:
            text = block.get("text", "").strip()
            if not text:
                return None
            raw_content = text
            search_text = text

        return {
            "block_id":      block.get("id", str(uuid.uuid4())),
            "chapter_path":  self._current_chapter_path(),
            "page_range":    [pidx],
            "bboxes":        [{"page_idx": pidx, "bbox": nbbox}],
            "block_type":    self._normalize_type(btype),
            "raw_content":   raw_content,
            "search_text":   search_text,
            "pdf_file_id":   self.pdf_id,
        }

    def _make_semantic_block_from_merged(self, merged: dict) -> dict:
        btype = merged.get("type", "text")
        return {
            "block_id":     merged["id"],
            "chapter_path": self._current_chapter_path(),
            "page_range":   merged["page_range"],
            "bboxes":       merged["bboxes"],
            "block_type":   self._normalize_type(btype),
            "raw_content":  merged["content"],
            "search_text":  merged.get("search_text", merged["content"]),
            "pdf_file_id":  self.pdf_id,
            "is_merged":    True,
        }

    def _normalize_type(self, btype: str) -> str:
        if btype in self.TITLE_TYPES:
            return "title"
        if btype in self.TABLE_TYPES:
            return "table"
        if btype in self.IMAGE_TYPES:
            return "image"
        if btype == "list":
            return "list"
        return "text"


# ─────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────

def detect_pdf_id(input_dir: str) -> str:
    """
    自动从 input_dir 中探测 pdf_id。
    规则：寻找 *_origin.pdf，取文件名去掉 _origin.pdf 后缀作为 pdf_id。
    若找不到，再尝试 *_content_list.json。
    """
    d = Path(input_dir)

    # 优先匹配 *_origin.pdf
    for p in sorted(d.glob("*_origin.pdf")):
        return p.stem.replace("_origin", "")

    # 其次匹配 *_content_list.json
    for p in sorted(d.glob("*_content_list.json")):
        return p.stem.replace("_content_list", "")

    raise FileNotFoundError(
        f"在 {input_dir} 中未找到 *_origin.pdf 或 *_content_list.json，"
        "请通过 --pdf_id 手动指定。"
    )


def main():
    parser = argparse.ArgumentParser(description="MinerU RAG 数据预处理")
    parser.add_argument(
        "--pdf_id", default=None,
        help="PDF 唯一标识符（不填则自动从 input_dir 中探测）"
    )
    parser.add_argument(
        "--input_dir", default="./data",
        help="MinerU 解析产物目录（含 block_list.json），默认 ./data"
    )
    parser.add_argument(
        "--output_dir", default=None,
        help="处理结果输出目录（默认 input_dir/processed）"
    )
    args = parser.parse_args()

    pdf_id = args.pdf_id or detect_pdf_id(args.input_dir)
    print(f"使用 pdf_id: {pdf_id}")

    processor = MinerUProcessor(pdf_id=pdf_id, input_dir=args.input_dir)
    processor.save(output_dir=args.output_dir)


if __name__ == "__main__":
    main()
