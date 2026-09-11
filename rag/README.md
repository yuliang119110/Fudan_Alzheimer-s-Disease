# RAG 论文库 · 最小可用 Agent

把 PDF 论文变成**带真实页码引用**的问答 Agent。三步流水线，零向量库依赖。

```
PDF ──parse_mineru.py──► parsed/<slug>/    MinerU 云端解析（结构化 + 页码）
                          │
                          └──chunk_doc.py──► chunks/<slug>.json   带页码/章节的 chunk
                                                    │
                                                    └──agent.py──► 问答（页码可溯源）
```

## 快速开始

```bash
# 0. 装依赖（只需 4 个）
pip install requests openai tiktoken pymupdf

# 1. 配置密钥
cp .env.example .env      # 填入 MINERU_TOKEN 和 DEEPSEEK_API_KEY

# 2. 解析 PDF
python scripts/parse_mineru.py paper.pdf

# 3. 切分
python scripts/chunk_doc.py paper

# 4. 提问
python agent.py "这篇综述纳入了哪些类型的综述？"
python agent.py                      # 交互模式，支持多轮追问
```

## 三个脚本

### `scripts/parse_mineru.py` — PDF → 结构化内容

调 MinerU 精准版 API（`/api/v4/file-urls/batch`）：创建任务 → 上传 → 轮询 → 下载解压。

| 参数 | 说明 |
|---|---|
| `--ocr` | 扫描版 PDF 强制 OCR（默认关；上传前会探测文本层并提示） |
| `--model vlm` | 复杂版面用 vlm，默认 `pipeline`（数字版 PDF 更快） |

产出里**最关键的是 `<uuid>_content_list.json`** —— 每个内容块自带 `page_idx`，这是页码引用的唯一来源。

### `scripts/chunk_doc.py` — 结构化内容 → chunk

```bash
python scripts/chunk_doc.py <slug> --preview 5      # 预览前 5 块
python scripts/chunk_doc.py <slug> --tokens 800     # 改目标 token 数
python scripts/chunk_doc.py <slug> --keep-refs      # 保留参考文献
```

相对传统"正则切 Markdown"做法的改进（均为实测结论）：

1. **页码真实可用** —— 取自 `content_list.json` 的 `page_idx`，而非在 Markdown 里正则找 `P152`
2. **标题识别正确** —— MinerU 的标题是 `type:"text"` + `text_level`，**不是** `type:"header"`（后者是页眉）
3. **噪声剥离** —— 页眉（`GABB ET AL.`）、页脚（`17 of 20`）、旁注、参考文献整节丢弃
4. **章节软断点** —— 小节自然汇入相邻 chunk，避免碎节产出 7~9 字符的噪声块
5. **章节边界不带 overlap** —— 防跨节语义污染（如把作者单位粘到摘要开头）
6. **token 用 tiktoken 真实计算**，不再是 `len(字符数)`

### `agent.py` — 问答

**为什么不做向量检索**：单篇论文切完约 30 块 / 14k tokens，DeepSeek 上下文完全放得下。全量直送**零检索误差**，也省掉 embedding、向量库、模型子进程三套依赖。

论文变多（>10 篇或 >100k tokens）时再上向量检索。

固定前缀（论文正文）放在 messages 前部，DeepSeek 的 prompt cache 会命中，多轮追问的输入成本大幅下降。

## 输出格式

`chunks/<slug>.json`：

```json
{
  "doc": { "slug": "...", "page_count": 20, "params": { ... } },
  "chunks": [
    {
      "chunk_id": 10,
      "text": "## 3.3 Main findings\n\n...",
      "section": "3.3 Main findings",
      "section_path": "论文标题 > 3.3 Main findings",
      "page_start": 6, "page_end": 6,
      "token_count": 799, "char_count": 3043,
      "has_table": true, "has_image": false,
      "kinds": ["table", "text"]
    }
  ]
}
```

## 已知限制

- **多 part 的书**页码会重置（每个 part 的 `page_idx` 从 0 重数），需自行累加偏移
- **表格/图注文字可能丢空格**：MinerU 对字母级定位的图注会输出 `TABLE2Outcomesidentified...` 或 `F I G U R E 3`，表格**主体**（`table_body`）转换正常
- `chunks/*.json` 含论文正文，**注意版权** —— 公开仓库慎传
- 密钥仅通过 `.env` / 环境变量提供，仓库内不含任何明文
