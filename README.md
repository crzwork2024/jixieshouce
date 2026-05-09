# 机械手册 RAG 系统

基于 MinerU 解析结果的高精度 RAG 系统，支持精确 PDF 位置引用、图文表完整展示、跨页内容自动修复、目录级意图检索、**父子块架构**（搜索子块、返回完整父块含整张表格和图片）。

## 技术栈

| 层次 | 技术 |
|------|------|
| 后端 | Python 3.11+ · FastAPI · ChromaDB |
| 嵌入 | BAAI/bge-large-zh-v1.5（硅基流动 API） |
| LLM  | DeepSeek-R1-0528-Qwen3-8B（硅基流动 API） |
| 前端 | Vue 3 · Element Plus · PDF.js |

---

## 目录结构

```
rag_system/                      # 本系统根目录
├── data/                        # MinerU 解析产物（已加入 .gitignore，不提交）
│   ├── block_list.json          # 跨页合并关系
│   ├── full.md                  # 完整 Markdown 文档
│   ├── images/                  # 提取的图片资源
│   ├── *.pdf                    # 原始 PDF
│   └── processed/               # 预处理输出（自动生成）
│       ├── *_toc.json
│       ├── *_semantic_blocks.json   # 细粒度语义块（子块）
│       ├── *_parent_blocks.json     # 父块（聚合后完整语义单元）
│       └── *_child_blocks.json      # 子块（含 parent_block_id，用于向量搜索）
├── data_processor.py            # 数据预处理：跨页合并、目录提取、语义块生成、父子块聚合
├── vector_store.py              # ChromaDB 向量索引：构建与父子块两级检索
├── rag_engine.py                # RAG 引擎：两级检索 + LLM 生成
├── main.py                      # FastAPI 后端服务
├── requirements.txt             # Python 依赖
├── .env.example                 # 环境变量模板（复制为 .env 后填入真实 Key）
├── .env                         # 实际环境变量（已加入 .gitignore，不提交）
├── .gitignore
├── chroma_db/                   # ChromaDB 持久化数据（自动创建，不提交）
└── frontend/
    ├── index.html
    ├── package.json
    ├── vite.config.js
    └── src/
        ├── main.js
        ├── App.vue              # 三栏主布局（目录 / PDF / 问答）
        ├── style.css
        └── components/
            ├── PdfViewer.vue    # PDF 渲染 + 高亮跳转
            ├── TocPanel.vue     # 目录导航
            └── ChatPanel.vue    # 问答 + 参考文献引用联动
```

---

## 快速开始

### 第一步：创建虚拟环境 & 安装依赖

```bash
cd rag_system

# 创建虚拟环境
python -m venv .venv

# 激活（Windows PowerShell）
.venv\Scripts\activate
# 激活（macOS / Linux）
source .venv/bin/activate

# 安装依赖
pip install -r requirements.txt
```

---

### 第二步：配置环境变量（.env）

```bash
# Windows
copy .env.example .env

# macOS / Linux
cp .env.example .env
```

用编辑器打开 `.env`，**只需修改一行**：

```dotenv
SILICONFLOW_API_KEY=sk-你的真实Key
```

其余项已在 `.env.example` 中预设了合理默认值，按需修改即可。

> **配置说明**
> - `.env.example` 是唯一的配置参考，所有可用项及默认值都在里面
> - `config.py` 只负责读取和校验，不含任何默认值
> - `.env` 已加入 `.gitignore`，不会被提交到 Git

---

### 第三步：数据预处理 + 索引构建

将 MinerU 解析产物放入 `rag_system/data/` 目录下（含 `block_list.json`、`images/`、`*_origin.pdf`）。

```bash
# 在 rag_system/ 目录下执行（已激活虚拟环境）

# 3a. 预处理（自动探测 pdf_id，生成细粒度块 + 父子块 JSON）
python data_processor.py

# 3b. 构建向量索引（自动扫描 data/processed/，自动识别父子块文件）
python vector_store.py
```

两条命令均**无需填写任何文件名**，更换数据后直接重新执行即可。

预处理会输出四个文件：

| 文件 | 说明 |
|------|------|
| `*_toc.json` | 目录树（第一阶段检索用） |
| `*_semantic_blocks.json` | 细粒度语义块（93 个，向后兼容） |
| `*_parent_blocks.json` | 父块（聚合后，含完整表格 HTML / 图片） |
| `*_child_blocks.json` | 子块（含 `parent_block_id`，向量搜索用） |

索引构建后 ChromaDB 包含四个 collection：

| Collection | 说明 |
|------------|------|
| `toc_nodes` | 目录节点嵌入 |
| `semantic_blocks` | 细粒度块嵌入（降级兼容） |
| `child_chunks` | 子块嵌入（主搜索 collection） |
| `parent_blocks` | 父块元数据（存储完整内容，不嵌入） |

如需手动指定（多数据源场景）：

```bash
python data_processor.py --pdf_id 自定义ID --input_dir ./data
python vector_store.py --toc    ./data/processed/自定义ID_toc.json \
                       --blocks ./data/processed/自定义ID_semantic_blocks.json \
                       --parent ./data/processed/自定义ID_parent_blocks.json \
                       --child  ./data/processed/自定义ID_child_blocks.json
```

> **也可以通过前端 UI 的「导入文档」按钮一键完成**以上两步（同样无需填写 PDF ID）。

---

### 第四步：前端安装 & 构建

```bash
cd frontend
npm install
npm run build      # 生产构建，输出到 frontend/dist/
cd ..
```

开发模式（热重载，无需 build）：

```bash
cd frontend
npm run dev        # 启动在 http://localhost:5173，自动代理 API 到 8000 端口
```

---

### 第五步：启动后端服务

```bash
# 在 rag_system/ 目录下（已激活虚拟环境）
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

打开浏览器访问：**http://localhost:8000**

> 前端开发模式时访问：http://localhost:5173

---

## 核心功能说明

### 父子块架构

系统将细粒度内容块聚合为更大的"父块"，实现"搜小、返大"：

```
原始细粒度块（title / table_caption / table / image / text）
        │
        ▼  _build_parent_groups()
┌───────────────────────────────────────────┐
│  section 父块（一个标题下的全部内容）       │
│  table_unit 父块（表格标题 + 表体 + 图片） │
└───────────────────────────────────────────┘
        │
        ├─ child_chunks（嵌入搜索，小片段）
        └─ parent_blocks（存储完整 raw_content）

检索时：向量搜索 child_chunks
        → 按 parent_block_id 去重
        → 拉取完整父块（含完整 HTML 表格 / 图片标记）
        → 传给 LLM / 返回前端
```

**触发新父块的规则**：
- 遇到「大节」`title`（如 `第 X 章…`、行首数字节号 `5 字体…`、`1.2 …`）→ 新建 `section` 父块；其它 `title`（如「字体的基本要求」）并入当前 `section`，避免「只有大标题、正文在下一块」
- 遇到 `table` 或「表 N」格式文本 → 按现有逻辑处理 `table_unit`（与 `section` 的包含关系不变）

### 两级检索流程

```
用户问题
  │
  ▼
[第一阶段] 目录级意图检索（toc_nodes）
  向量相似度匹配 → Top-3 相关章节
  │
  ▼
[第二阶段] 父子块精细检索
  向量搜索 child_chunks（子块）
  → 按 parent_block_id 去重
  → 拉取完整父块（含完整表格 HTML）
  + 关键词重排序（BM25-like 融合）
  │
  ▼
多模态感知：识别"表格"/"图片"关键词 → 优先返回对应类型块
  │
  ▼
引用去重聚合 → 构建上下文（完整表格，最多 6000 字符）
  │
  ▼
DeepSeek LLM 生成回答（含 {{TABLE:id}} / {{IMAGE:id}} 占位符）
  │
  ▼
前端替换占位符为实际 HTML 表格 / <img> 标签
```

### PDF 精确高亮

每个引用块携带完整 `bboxes` 信息（归一化 0-1 坐标），点击回答中的引用编号 `[1]` 或参考文献卡片时：

1. PDF 阅读器自动跳转到对应页码
2. 对应区域显示黄色半透明高亮矩形
3. 参考文献卡片同步高亮

### 跨页表格自动修复

`block_list.json` 中的 `mergeConnections` 字段记录了跨页合并关系：

```json
{"id": "28e91107-...", "blocks": ["0-4", "1-2", "2-2"], "type": "merge"}
```

系统自动将分布在多页的表格 HTML 合并为完整单表，并保留所有页码和 bbox 元数据。

---

## API 接口文档

启动后访问 Swagger UI：**http://localhost:8000/docs**

| 方法 | 路径 | 说明 |
|------|------|------|
| POST | `/api/query` | RAG 问答（支持 `stream=true` 流式响应） |
| POST | `/api/retrieve` | 纯检索（不调用 LLM） |
| POST | `/api/index` | 触发预处理 + 索引构建 |
| GET  | `/api/toc` | 获取文档目录 |
| GET  | `/api/block/{id}` | 按 block_id 获取块详情 |
| GET  | `/api/stats` | 向量库统计 |
| GET  | `/images/{filename}` | 图片资源静态服务 |
| GET  | `/pdf/{filename}` | PDF 文件静态服务 |

### 问答接口示例

```bash
curl -X POST http://localhost:8000/api/query \
  -H "Content-Type: application/json" \
  -d '{
    "query": "什么是最大实体要求（MMR）？",
    "pdf_file_id": "d7beac18-1835-4af4-8463-6ac1b1315e50",
    "stream": false
  }'
```

响应示例：

```json
{
  "query": "什么是最大实体要求（MMR）？",
  "answer": "最大实体要求（MMR）是指...[1] 其符号为 M...[1]\n\n<!-- EMBED_TABLE:uuid -->",
  "references": [
    {
      "index": 1,
      "block_id": "uuid-父块ID",
      "chapter": "第③章 几何公差 > 1 术语与定义",
      "page_label": "第1-3页",
      "block_type": "table_unit",
      "preview": "最大实体要求(MMR) | 尺寸要素的非理想要素不违反...",
      "raw_content": "<table>...</table>",
      "has_html_table": true,
      "img_paths": [],
      "pdf_ref_url": "pdf://d7beac18...?pages=0,1,2&bboxes=eyJiYm94..."
    }
  ]
}
```

前端会将回答中的 `<!-- EMBED_TABLE:uuid -->` 替换为 `references[0].raw_content` 里的完整 HTML 表格，`<!-- EMBED_IMAGE:uuid -->` 替换为实际图片标签。

---

## 常见问题

**Q: 嵌入生成很慢？**  
A: bge-large-zh-v1.5 是大模型，首次索引会批量调用 API。建议文档不超过 500 页。

**Q: PDF 显示空白？**  
A: 检查 PDF 文件是否存在于 `data/` 目录下，文件名格式为 `{pdf_id}_origin.pdf`。

**Q: 提示 SILICONFLOW_API_KEY 未设置？**  
A: 确认 `.env` 文件存在于 `rag_system/` 目录下，且 Key 已正确填写（不含多余空格）。

**Q: 回答不准确？**  
A: 可适当增大 `top_k_blocks` 参数（最大建议 10），或先通过 `/api/retrieve` 接口验证检索质量。

**Q: ChromaDB 报错？**  
A: 删除 `chroma_db/` 目录后重新执行第三步的索引构建命令（两条命令都要重跑）。

**Q: 回答中看不到完整表格？**  
A: 确认已重新执行第三步（`data_processor.py` + `vector_store.py`）以生成父子块索引。若 `child_chunks_count` 为 0，说明旧索引未清除，删除 `chroma_db/` 后重建。

**Q: 父块聚合结果不理想？**  
A: 可调整 `data_processor.py` 中的 `TABLE_CAPTION_RE` 正则，或在 `_build_parent_groups()` 里调整触发新父块的条件。

**Q: 参考来源仍然是零碎小节而不是整段父块？**  
A: 需使用最新代码：`vector_store` 会从检索结果中读取 `parent_block_id` 以拉回完整父块；TOC 阶段用层级 `chapter_path` 过滤时已改为与「短标题」兼容的匹配逻辑。无需重建向量库，重启后端即可。

**Q: `npm install` 失败？**  
A: 确认 Node.js >= 18，可用 `node -v` 检查版本。
