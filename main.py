"""
main.py
FastAPI 后端服务

接口列表：
  POST /api/query          - 问答（支持 stream=true 流式响应）
  POST /api/retrieve       - 纯检索（不调用 LLM）
  POST /api/index          - 触发数据预处理 + 索引构建（pdf_id 可选，自动探测）
  GET  /api/pdfs           - 扫描 data/ 目录，返回所有可用 PDF 列表
  GET  /api/toc            - 获取文档目录
  GET  /api/block/{id}     - 按 block_id 获取块详情
  GET  /api/stats          - 向量库统计
  GET  /images/{filename}  - 图片静态服务
  GET  /pdf/{filename}     - PDF 静态服务
  GET  /                   - 前端入口页
"""

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import config
from config import CHROMA_DIR, FRONTEND_DIR

# 路径统一转为绝对路径，避免工作目录不同导致找不到文件
_BASE = Path(__file__).parent
DATA_DIR = (_BASE / config.DATA_DIR) if not config.DATA_DIR.is_absolute() else config.DATA_DIR
from data_processor import MinerUProcessor, detect_pdf_id
from rag_engine import RAGEngine
from vector_store import VectorStore, _auto_find_processed

# ─────────────────────────────────────────────
# 应用初始化
# ─────────────────────────────────────────────

config.validate()  # 启动时校验必要环境变量

app = FastAPI(
    title="机械手册 RAG 系统",
    description="基于 MinerU 解析结果的高精度 RAG 系统",
    version="1.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局单例
_vector_store: VectorStore | None = None
_rag_engine:   RAGEngine   | None = None


def get_vs() -> VectorStore:
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore(persist_dir=str(CHROMA_DIR))
    return _vector_store


def get_engine() -> RAGEngine:
    global _rag_engine
    if _rag_engine is None:
        _rag_engine = RAGEngine(vector_store=get_vs())
    return _rag_engine


# ─────────────────────────────────────────────
# Pydantic 模型
# ─────────────────────────────────────────────

class QueryRequest(BaseModel):
    query:       str
    pdf_file_id: str | None = None
    stream:      bool = False
    top_k_toc:   int  = 3
    top_k_blocks: int = 5


class RetrieveRequest(BaseModel):
    query:       str
    pdf_file_id: str | None = None
    top_k_toc:   int = 3
    top_k_blocks: int = 5


class IndexRequest(BaseModel):
    pdf_id:    str | None = None   # 不填则自动从 input_dir 探测
    input_dir: str = "./data"      # 默认使用 ./data 目录


# ─────────────────────────────────────────────
# API 路由
# ─────────────────────────────────────────────

@app.post("/api/query")
async def query_endpoint(req: QueryRequest):
    """
    RAG 问答接口。
    stream=true 时返回 SSE 流式响应；否则返回完整 JSON。
    """
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")

    engine = get_engine()

    if req.stream:
        result = engine.answer(
            query=req.query,
            pdf_file_id=req.pdf_file_id,
            stream=True,
        )
        refs    = result["references"]
        blocks  = result["blocks"]
        gen     = result["stream"]

        async def event_generator():
            # 先发送引用元数据
            meta_event = json.dumps({
                "type":       "meta",
                "references": refs,
                "blocks":     _serialize_blocks(blocks),
            }, ensure_ascii=False)
            yield f"data: {meta_event}\n\n"

            # 逐 token 发送
            for token in gen:
                chunk_event = json.dumps({"type": "token", "content": token}, ensure_ascii=False)
                yield f"data: {chunk_event}\n\n"

            yield "data: {\"type\": \"done\"}\n\n"

        return StreamingResponse(event_generator(), media_type="text/event-stream")

    # 非流式
    result = engine.answer(
        query=req.query,
        pdf_file_id=req.pdf_file_id,
        stream=False,
    )
    return JSONResponse({
        "query":      result["query"],
        "answer":     result["answer"],
        "references": result["references"],
        "blocks":     _serialize_blocks(result["blocks"]),
    })


@app.post("/api/retrieve")
async def retrieve_endpoint(req: RetrieveRequest):
    """纯检索接口，不调用 LLM，返回相关语义块。"""
    if not req.query.strip():
        raise HTTPException(status_code=400, detail="query 不能为空")

    engine = get_engine()
    result = engine.retrieve(
        query=req.query,
        pdf_file_id=req.pdf_file_id,
        top_k_toc=req.top_k_toc,
        top_k_blocks=req.top_k_blocks,
    )
    return JSONResponse({
        "query":         result["query"],
        "toc_results":   result["toc_results"],
        "chapter_paths": result["chapter_paths"],
        "blocks":        _serialize_blocks(result["blocks"]),
        "modal_filter":  result["modal_filter"],
    })


@app.post("/api/index")
async def index_endpoint(req: IndexRequest):
    """
    触发数据预处理 + 向量索引构建。
    - input_dir 默认 ./data，也可传绝对路径
    - pdf_id 不填时自动从 input_dir 中探测 *_origin.pdf
    """
    input_path = Path(req.input_dir)
    if not input_path.is_absolute():
        input_path = Path(__file__).parent / req.input_dir

    if not input_path.exists():
        raise HTTPException(status_code=404, detail=f"目录不存在: {input_path}")

    block_list = input_path / "block_list.json"
    if not block_list.exists():
        raise HTTPException(status_code=404, detail=f"未找到 block_list.json: {block_list}")

    # 自动探测 pdf_id
    try:
        pdf_id = req.pdf_id or detect_pdf_id(str(input_path))
    except FileNotFoundError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # 预处理
    processed_dir = input_path / "processed"
    processor = MinerUProcessor(pdf_id=pdf_id, input_dir=str(input_path))
    processor.save(output_dir=str(processed_dir))

    toc_path    = processed_dir / f"{pdf_id}_toc.json"
    blocks_path = processed_dir / f"{pdf_id}_semantic_blocks.json"
    parent_path = processed_dir / f"{pdf_id}_parent_blocks.json"
    child_path  = processed_dir / f"{pdf_id}_child_blocks.json"

    # 索引（若父子块文件存在，一并索引）
    vs = get_vs()
    vs.build_index_from_files(
        toc_path    = str(toc_path),
        blocks_path = str(blocks_path),
        parent_path = str(parent_path) if parent_path.exists() else None,
        child_path  = str(child_path)  if child_path.exists()  else None,
    )
    stats = vs.stats()

    return JSONResponse({
        "status":       "success",
        "pdf_id":       pdf_id,
        "toc_path":     str(toc_path),
        "blocks_path":  str(blocks_path),
        "parent_path":  str(parent_path) if parent_path.exists() else None,
        "child_path":   str(child_path)  if child_path.exists()  else None,
        "vector_stats": stats,
    })


@app.get("/api/pdfs")
async def list_pdfs():
    """
    扫描 DATA_DIR，返回所有已存在 *_origin.pdf 的文档列表。
    同时标记该文档是否已完成索引（processed/ 目录存在对应文件）。
    """
    data_dir = Path(__file__).parent / "data"
    if not data_dir.exists():
        return JSONResponse({"pdfs": []})

    pdfs = []
    for pdf_file in sorted(data_dir.glob("*_origin.pdf")):
        pdf_id = pdf_file.stem.replace("_origin", "")
        processed_toc = data_dir / "processed" / f"{pdf_id}_toc.json"
        pdfs.append({
            "id":       pdf_id,
            "name":     pdf_id,           # 前端可自定义显示名
            "filename": pdf_file.name,
            "indexed":  processed_toc.exists(),
        })

    return JSONResponse({"pdfs": pdfs})


@app.get("/api/toc")
async def get_toc(pdf_file_id: str | None = None):
    """获取文档目录结构。"""
    vs = get_vs()
    where = {"pdf_file_id": pdf_file_id} if pdf_file_id else None
    results = vs._toc_col.get(where=where, include=["metadatas"])
    toc_items = []
    for i, mid in enumerate(results["ids"]):
        meta = results["metadatas"][i]
        toc_items.append({
            "toc_id":      mid,
            "block_id":    meta.get("block_id", ""),
            "title":       meta.get("title", ""),
            "level":       meta.get("level", 1),
            "page_idx":    meta.get("page_idx", 0),
            "bbox":        _safe_json(meta.get("bbox", "[]")),
            "pdf_file_id": meta.get("pdf_file_id", ""),
        })
    toc_items.sort(key=lambda x: x["page_idx"])
    return JSONResponse({"toc": toc_items, "count": len(toc_items)})


@app.get("/api/block/{block_id}")
async def get_block(block_id: str):
    """按 block_id 获取语义块详情。"""
    vs = get_vs()
    block = vs.get_block_by_id(block_id)
    if not block:
        raise HTTPException(status_code=404, detail=f"block_id 不存在: {block_id}")
    return JSONResponse(block)


@app.get("/api/stats")
async def get_stats():
    """获取向量库统计信息。"""
    vs = get_vs()
    return JSONResponse(vs.stats())


# ─────────────────────────────────────────────
# 静态文件服务
# ─────────────────────────────────────────────

@app.get("/images/{filename:path}")
async def serve_image(filename: str):
    """服务 MinerU 解析提取的图片文件。"""
    img_path = DATA_DIR / "images" / filename
    if not img_path.exists():
        raise HTTPException(status_code=404, detail=f"图片不存在: {filename}")
    return FileResponse(str(img_path))


@app.get("/pdf/{filename:path}")
async def serve_pdf(filename: str):
    """服务 PDF 文件（供 PDF.js 加载）。"""
    pdf_path = DATA_DIR / filename
    if not pdf_path.exists():
        raise HTTPException(status_code=404, detail=f"PDF 不存在: {filename}")
    return FileResponse(str(pdf_path), media_type="application/pdf")


# 前端静态文件（必须在所有 API 路由之后挂载）
_frontend_built = FRONTEND_DIR.exists()
if _frontend_built:
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")

    @app.get("/{full_path:path}")
    async def serve_frontend(full_path: str):
        # SPA 回退：所有非 API 路径返回 index.html
        index = FRONTEND_DIR / "index.html"
        if index.exists():
            return FileResponse(str(index))
        raise HTTPException(status_code=404, detail="前端未构建，请先执行 npm run build")
else:
    @app.get("/")
    async def root():
        return JSONResponse({
            "message": "机械手册 RAG 后端服务运行中",
            "docs":    "/docs",
            "note":    "前端请在 frontend/ 目录执行 npm install && npm run build",
        })


# ─────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────

def _serialize_blocks(blocks: list[dict]) -> list[dict]:
    """确保所有字段可 JSON 序列化。"""
    safe = []
    for b in blocks:
        safe.append({
            "block_id":     b.get("id", b.get("block_id", "")),
            "chapter_path": b.get("chapter_path", ""),
            "page_range":   b.get("page_range", []),
            "bboxes":       b.get("bboxes", []),
            "block_type":   b.get("block_type", "text"),
            "raw_content":  (b.get("raw_content", b.get("document", "")))[:500],
            "score":        b.get("score", 0),
            "pdf_file_id":  b.get("pdf_file_id", ""),
        })
    return safe


def _safe_json(s):
    if isinstance(s, (list, dict)):
        return s
    try:
        return json.loads(s)
    except Exception:
        return []


# ─────────────────────────────────────────────
# 启动入口
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
        log_level="info",
    )
