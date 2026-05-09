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

    # 识别表格标题的正则（如"表 2⁃1⁃1"、"表1-1"、"表 1"）
    TABLE_CAPTION_RE = re.compile(r"^表[\s\u2003]*[\d\-⁃一二三四五六七八九十]+")

    # 「大节」起点：在此处 flush 并新建 section 父块。MinerU 常把小节题也标成 title，
    # 若每个 title 都切段会产生「只有章节名、正文在下一块」的空壳父块。
    MAJOR_SECTION_TITLE_RE = re.compile(
        r"^(?:"
        r"第\s*[一二三四五六七八九十百千零〇两\d]+\s*章"  # 第 2 章 …
        r"|"
        r"\d+(?:\.\d+){1,3}[\s\.、．]"  # 1.2 / 2.3.1 等多级节号
        r"|"
        r"\d+[\s\u3000]+(?=\S)"  # 「5 字体…」「4 比例 …」行首阿拉伯数字节号
        r")",
        re.UNICODE,
    )

    def __init__(self, pdf_id: str, input_dir: str):
        self.pdf_id    = pdf_id
        self.input_dir = Path(input_dir)
        self.block_list_path = self.input_dir / "block_list.json"

        self._raw_pages: list[list[dict]] = []   # pdfData 原始页列表
        self._merge_map: dict[str, dict]  = {}   # id -> 合并后块
        self._toc: list[dict]             = []   # 目录树
        self._semantic_blocks: list[dict] = []   # 细粒度语义块列表（子块）
        self._parent_blocks:   list[dict] = []   # 聚合后父块列表
        self._child_blocks:    list[dict] = []   # 携带 parent_block_id 的子块列表
        self._chapter_stack: list[str]    = []   # 当前章节路径栈

    # ────────────────────────────────────────
    # 公开接口
    # ────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        """完整执行预处理流程，返回处理结果。"""
        print(f"[1/5] 加载 block_list.json ...")
        self._load_block_list()

        print(f"[2/5] 处理跨页合并关系 ...")
        self._process_merge_connections()

        print(f"[3/5] 提取目录结构 ...")
        self._extract_toc()

        print(f"[4/5] 生成细粒度语义块 ...")
        self._build_semantic_blocks()

        print(f"[5/5] 聚合父子块 ...")
        self._build_parent_groups()

        result = {
            "pdf_id": self.pdf_id,
            "toc": self._toc,
            "semantic_blocks": self._semantic_blocks,
            "parent_blocks": self._parent_blocks,
            "child_blocks": self._child_blocks,
            "total_blocks": len(self._semantic_blocks),
            "total_pages": len(self._raw_pages),
        }
        print(
            f"完成！共生成 {len(self._semantic_blocks)} 个细粒度块，"
            f"{len(self._parent_blocks)} 个父块，{len(self._child_blocks)} 个子块，"
            f"{len(self._toc)} 个目录节点。"
        )
        return result

    def save(self, output_dir: str | None = None) -> None:
        """将处理结果保存为 JSON 文件。"""
        out = Path(output_dir) if output_dir else self.input_dir / "processed"
        out.mkdir(parents=True, exist_ok=True)

        result = self.run()

        toc_path          = out / f"{self.pdf_id}_toc.json"
        blocks_path       = out / f"{self.pdf_id}_semantic_blocks.json"
        parent_path       = out / f"{self.pdf_id}_parent_blocks.json"
        child_path        = out / f"{self.pdf_id}_child_blocks.json"

        toc_path.write_text(
            json.dumps(result["toc"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        blocks_path.write_text(
            json.dumps(result["semantic_blocks"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        parent_path.write_text(
            json.dumps(result["parent_blocks"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        child_path.write_text(
            json.dumps(result["child_blocks"], ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"目录保存至: {toc_path}")
        print(f"细粒度语义块保存至: {blocks_path}")
        print(f"父块保存至: {parent_path}")
        print(f"子块（带 parent_block_id）保存至: {child_path}")

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
            rel = self._normalize_mineru_img_rel(block.get("img_path", ""))
            raw_content = f"![图片](images/{rel})" if rel else "![图片]"
            search_text = f"图片: {rel}" if rel else "图片"
        else:
            text = block.get("text", "").strip()
            if not text:
                return None
            raw_content = text
            search_text = text

        sem = {
            "block_id":      block.get("id", str(uuid.uuid4())),
            "chapter_path":  self._current_chapter_path(),
            "page_range":    [pidx],
            "bboxes":        [{"page_idx": pidx, "bbox": nbbox}],
            "block_type":    self._normalize_type(btype),
            "raw_content":   raw_content,
            "search_text":   search_text,
            "pdf_file_id":   self.pdf_id,
        }
        if btype in self.TITLE_TYPES:
            sem["title_level"] = block.get("level", 1)
        return sem

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

    @staticmethod
    def _normalize_mineru_img_rel(img_path: str) -> str:
        """统一为 images/ 下的相对文件名，避免出现 images//xxx。"""
        p = (img_path or "").strip().replace("\\", "/")
        while "//" in p:
            p = p.replace("//", "/")
        p = p.lstrip("/")
        if p.lower().startswith("images/"):
            p = p[7:].lstrip("/")
        return p

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

    # ────────────────────────────────────────
    # 父子块聚合
    # ────────────────────────────────────────

    def _is_table_unit_trigger(self, block: dict) -> bool:
        """判断此块是否触发新的 table_unit 父块。"""
        btype = block.get("block_type", "")
        if btype == "table":
            return True
        if btype == "text" and self.TABLE_CAPTION_RE.match(block.get("raw_content", "")):
            return True
        return False

    def _is_section_trigger(self, block: dict) -> bool:
        """是否为标题块（MinerU title）。"""
        return block.get("block_type") == "title"

    def _is_major_section_title(self, block: dict) -> bool:
        """
        是否视为「大节」起点（需要 flush 后新建 section）。
        不匹配的行仍可能是 title（如「字体的基本要求」），应并入当前 section。
        """
        if block.get("block_type") != "title":
            return False
        text = block.get("raw_content", "").strip().lstrip("#").strip()
        return bool(self.MAJOR_SECTION_TITLE_RE.match(text))

    def _build_parent_groups(self) -> None:
        """
        将细粒度 _semantic_blocks 聚合为父块（parent_blocks）和带 parent_block_id 的子块（child_blocks）。

        触发新父块的条件（按顺序扫描）：
        - 遇到「大节」title（第 X 章 / 行首数字节号 / 多级节号 1.2）→ 新建 section 父块
        - 其它 title（小节题）→ 并入当前 section，不断开
        - 遇到 table / table_caption 文本 → 按 table_unit 规则处理（见分支逻辑）

        同一父块内收集紧随其后的 table、image、text、list，
        直到遇到下一个大节 title 或 table_unit 边界为止。
        """
        self._parent_blocks = []
        self._child_blocks  = []

        # 当前父块缓冲区
        current_parent_id:   str | None = None
        current_parent_type: str | None = None
        current_children:    list[dict] = []
        current_chapter:     str        = ""

        def flush_parent() -> None:
            nonlocal current_parent_id, current_parent_type, current_children, current_chapter
            if not current_children or current_parent_id is None:
                return

            # 合并所有子块的 bboxes 和 page_range
            all_bboxes: list[dict] = []
            page_set: set[int] = set()
            parts: list[str] = []

            for child in current_children:
                all_bboxes.extend(child.get("bboxes", []))
                page_set.update(child.get("page_range", []))
                btype = child.get("block_type", "")
                if btype == "table":
                    parts.append(child["raw_content"])   # 保留完整 HTML
                elif btype == "image":
                    parts.append(child["raw_content"])   # ![图片](...)
                else:
                    text = child.get("raw_content", "").strip()
                    if text:
                        parts.append(text)

            raw_content   = "\n".join(parts)
            # search_text 只用纯文本摘要（截短，以免过长）
            text_parts = []
            for child in current_children:
                btype = child.get("block_type", "")
                if btype == "table":
                    text_parts.append(child.get("search_text", "")[:300])
                elif btype == "image":
                    pass
                else:
                    text_parts.append(child.get("search_text", child.get("raw_content", ""))[:200])
            search_text   = " ".join(t for t in text_parts if t)

            parent_block = {
                "parent_block_id":  current_parent_id,
                "parent_type":      current_parent_type,
                "chapter_path":     current_chapter,
                "page_range":       sorted(page_set),
                "bboxes":           all_bboxes,
                "raw_content":      raw_content,
                "search_text":      search_text,
                "child_block_ids":  [c["block_id"] for c in current_children],
                "pdf_file_id":      self.pdf_id,
            }
            self._parent_blocks.append(parent_block)

            # 标记子块
            for child in current_children:
                child_with_parent = dict(child)
                child_with_parent["parent_block_id"] = current_parent_id
                self._child_blocks.append(child_with_parent)

            current_parent_id   = None
            current_parent_type = None
            current_children    = []

        for block in self._semantic_blocks:
            btype = block.get("block_type", "")

            if self._is_section_trigger(block):
                if self._is_major_section_title(block):
                    flush_parent()
                    current_parent_id   = str(uuid.uuid4())
                    current_parent_type = "section"
                    current_chapter     = block.get("chapter_path", "")
                    current_children    = [block]
                else:
                    if current_parent_id is None:
                        current_parent_id   = str(uuid.uuid4())
                        current_parent_type = "section"
                        current_chapter     = block.get("chapter_path", "")
                        current_children    = [block]
                    else:
                        current_children.append(block)

            elif self._is_table_unit_trigger(block):
                # 若当前父块是 section，不 flush（把 table 收入 section 内）
                # 若当前父块是 table_unit，则先 flush 再新建
                if current_parent_type == "table_unit":
                    flush_parent()
                    current_parent_id   = str(uuid.uuid4())
                    current_parent_type = "table_unit"
                    current_chapter     = block.get("chapter_path", "")
                    current_children    = [block]
                elif current_parent_type == "section":
                    # 在 section 内部遇到 table，直接收入 section
                    current_children.append(block)
                else:
                    # 还没有父块，新建 table_unit
                    current_parent_id   = str(uuid.uuid4())
                    current_parent_type = "table_unit"
                    current_chapter     = block.get("chapter_path", "")
                    current_children    = [block]

            else:
                if current_parent_id is None:
                    # 文档开头无标题的孤立块，新建 section 父块
                    current_parent_id   = str(uuid.uuid4())
                    current_parent_type = "section"
                    current_chapter     = block.get("chapter_path", "")
                    current_children    = [block]
                else:
                    current_children.append(block)

        flush_parent()


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
