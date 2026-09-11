"""
通用文档切分器
parsed/<slug>/<uuid>_content_list.json → chunks/<slug>.json

设计要点（相对 EENTagent/scripts/04_chunk.py 的改进）：
  1. 用 content_list.json 而非正则解析 Markdown → 每个块自带 page_idx，页码真实可用
  2. 标题识别用 type=="text" + text_level（实测结论，不是 type=="header"）
  3. 丢弃页眉页脚（header/footer/page_number/page_footnote）与参考文献（ref_text）
  4. 按"章节"分组切分，不跨节合并 → chunk 的 section 归属干净
  5. 碎片合并：小于 MIN_CHUNK_CHARS 的块并入邻块，消除 7 字符噪声块
  6. token_count 用 tiktoken 真实计算（中英混合都准），不再是 len(字符数)
  7. 每块记录 page_start/page_end + 图片 + 表格标记

用法：
    python chunk_doc.py <slug>
    python chunk_doc.py <slug> --preview 5     # 打印前 5 块
    python chunk_doc.py <slug> --tokens 800    # 覆盖目标 token 数
"""

import html
import json
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    PARSED_DIR,
    CHUNKS_DIR,
    CHUNK_TOKENS,
    CHUNK_OVERLAP,
    MIN_CHUNK_CHARS,
    DROP_SECTIONS,
    DROP_BLOCK_TYPES,
    REF_BLOCK_TYPE,
    REF_SUBTYPE,
    PAGE_OFFSET,
)

# ------------------------------------------------------------
# tokenizer（tiktoken 不可用时回退到字符估算）
# ------------------------------------------------------------
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text, disallowed_special=()))
except Exception:
    def count_tokens(text: str) -> int:
        """粗略估算：中文 ~1.5 字符/token，其他 ~4 字符/token"""
        cjk = len(re.findall(r"[一-鿿]", text))
        return int(cjk / 1.5 + (len(text) - cjk) / 4)


_DROP_RE = [re.compile(p, re.IGNORECASE) for p in DROP_SECTIONS]


def is_drop_section(title: str) -> bool:
    t = title.strip()
    return any(r.search(t) for r in _DROP_RE)


# ------------------------------------------------------------
# content_list → 结构化单元
# ------------------------------------------------------------

def html_table_to_text(table_html: str) -> str:
    """HTML 表格 → 可读纯文本（行内单元格用 | 分隔）"""
    if not table_html:
        return ""
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table_html, re.S | re.I)
    out = []
    for row in rows:
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S | re.I)
        cells = [re.sub(r"<[^>]+>", "", c) for c in cells]
        cells = [html.unescape(c).strip().replace("\n", " ") for c in cells]
        if any(cells):
            out.append(" | ".join(cells))
    return "\n".join(out)


def load_content_list(slug: str) -> tuple[list[dict], Path]:
    """定位 parsed/<slug>/ 下的 content_list.json"""
    doc_dir = PARSED_DIR / slug
    if not doc_dir.exists():
        raise FileNotFoundError(f"解析目录不存在: {doc_dir}（先跑 parse_mineru.py）")

    candidates = sorted(doc_dir.glob("*_content_list.json"))
    if not candidates:
        raise FileNotFoundError(
            f"{doc_dir} 下没有 *_content_list.json → 拿不到页码，"
            f"请确认 MinerU 解析成功"
        )
    # 多个时取最大的（=内容最全的那份）
    path = max(candidates, key=lambda p: p.stat().st_size)
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f), path


def _block_text(e: dict) -> str:
    """取块的可见文本：优先 text，其次 list_items（参考文献/列表用这个字段）"""
    t = e.get("text")
    if isinstance(t, str) and t.strip():
        return t.strip()
    items = e.get("list_items")
    if isinstance(items, list):
        return "\n".join(str(i).strip() for i in items if str(i).strip())
    return ""


def _is_ref_block(e: dict) -> bool:
    """参考文献条目：type=ref_text（书籍版）或 type=list+sub_type=ref_text（论文版）"""
    return e.get("type") == REF_BLOCK_TYPE or e.get("sub_type") == REF_SUBTYPE


def entries_to_units(entries: list[dict], drop_refs: bool = True) -> list[dict]:
    """
    把 content_list 条目转成待切分单元。
    返回 [{text, page, section, level, kind}]
    """
    units = []
    # 章节栈：level → title，用于拼出 "3 RESULTS > 3.3.1 Life Impact" 这样的路径
    sec_stack: dict[int, str] = {}
    dropping = False          # 是否处于被丢弃的章节（参考文献等）
    drop_at_level = 99        # 在哪个标题层级进入的丢弃状态

    def cur_section() -> str:
        """
        最具体的标题（最深一层）。
        实测：MinerU 常把整篇论文的标题标为 level 1、其余全标 level 2，
        若返回完整路径会得到 '超长论文标题 > 3.3 Main findings' 这种噪声，
        故 section 只取最深层，完整链路另存 section_path。
        """
        if not sec_stack:
            return ""
        return sec_stack[max(sec_stack)]

    def cur_section_path() -> str:
        return " > ".join(sec_stack[k] for k in sorted(sec_stack))

    for e in entries:
        etype = e.get("type", "")
        page = e.get("page_idx", 0) + PAGE_OFFSET

        # --- 页眉页脚旁注：始终丢弃 ---
        if etype in DROP_BLOCK_TYPES:
            continue

        # --- 标题 ---
        if etype == "text" and e.get("text_level"):
            level = int(e["text_level"])
            title = (e.get("text") or "").strip()
            if not title:
                continue
            # 更新章节栈：清掉比当前更深或同级的旧标题
            for k in [k for k in sec_stack if k >= level]:
                del sec_stack[k]
            sec_stack[level] = title

            if dropping and level <= drop_at_level:
                dropping = False          # 遇到同级/更高级标题，退出丢弃状态
            if is_drop_section(title):
                dropping = True
                drop_at_level = level
                continue
            if dropping:
                continue

            units.append({
                "text": "#" * level + " " + title,
                "page": page,
                "section": cur_section(),
                "section_path": cur_section_path(),
                "level": level,
                "kind": "heading",
            })
            continue

        # --- 被丢弃章节内的内容 ---
        if dropping:
            continue

        # --- 参考文献条目 ---
        if _is_ref_block(e):
            if drop_refs:
                continue
            text = _block_text(e)
            if text:
                units.append({
                    "text": text, "page": page, "section": cur_section(), "section_path": cur_section_path(),
                    "level": None, "kind": "reference",
                })
            continue

        # --- 表格 ---
        if etype == "table":
            parts = []
            for cap in e.get("table_caption") or []:
                if cap.strip():
                    parts.append(f"[表] {cap.strip()}")
            body = html_table_to_text(e.get("table_body", ""))
            if body:
                parts.append(body)
            for fn in e.get("table_footnote") or []:
                if fn.strip():
                    parts.append(f"注: {fn.strip()}")
            text = "\n".join(parts).strip()
            if not text:
                img = e.get("img_path", "")
                if img:
                    text = f"![表格]({img})"
            if text:
                units.append({
                    "text": text, "page": page, "section": cur_section(), "section_path": cur_section_path(),
                    "level": None, "kind": "table",
                })
            continue

        # --- 图片 / 图表 ---
        if etype in ("image", "chart"):
            img = e.get("img_path", "")
            caps = [
                c.strip()
                for c in (e.get("image_caption") or e.get("chart_caption") or [])
                if c.strip()
            ]
            label = caps[0] if caps else ("图表" if etype == "chart" else "图片")
            desc = (e.get("content") or "").strip()
            text = f"![{label}]({img})" if img else ""
            if desc:
                text = f"{text}\n{desc}" if text else desc
            if text:
                units.append({
                    "text": text, "page": page, "section": cur_section(), "section_path": cur_section_path(),
                    "level": None, "kind": "image",
                })
            continue

        # --- 列表 / 普通正文 ---
        if etype in ("text", "list"):
            text = _block_text(e)
            if text:
                units.append({
                    "text": text, "page": page, "section": cur_section(), "section_path": cur_section_path(),
                    "level": None,
                    "kind": "list" if etype == "list" else "text",
                })

    return units


# ------------------------------------------------------------
# 单元 → chunk
# ------------------------------------------------------------

def _tail_tokens(text: str, n: int) -> str:
    """取文本末尾 n 个 token 对应的字符片段（用于 overlap）"""
    if n <= 0:
        return ""
    try:
        ids = _ENC.encode(text, disallowed_special=())
        return _ENC.decode(ids[-n:])
    except Exception:
        return text[-n * 2:]


def pack_units(units: list[dict], target: int, overlap: int) -> list[dict]:
    """
    把单元流贪心打包成 chunk。

    章节边界是**软断点**：只有在当前缓冲区已经攒够 min_section_tokens 时，
    遇到层级 <=2 的标题才断开。这样做的原因（实测）：
      教材里"第X节 / 一、 / （一）"层级混乱，若每个标题都强制断章，
      会产生大量 1 个 chunk 的碎节，进而产出 9 字符的噪声块。
      软断点让小节自然汇入相邻 chunk，大节仍能干净切分。
    """
    chunks = []
    buf: list[dict] = []
    buf_tokens = 0
    carry = ""                                  # 上一块的 overlap 尾巴
    min_section_tokens = max(target // 2, 1)

    def flush(keep_overlap: bool = True):
        """
        keep_overlap=False 用于章节边界断点：
        overlap 的意义是"接续被切断的连续叙述"，跨章节时反而把上一节的
        尾巴（如作者单位、上一小节结论）粘到新一节开头，造成语义污染。
        """
        nonlocal buf, buf_tokens, carry
        if not buf:
            return
        body = "\n\n".join(u["text"] for u in buf)
        text = (carry + "\n\n" + body).strip() if carry else body
        chunks.append({
            "text": text,
            "section": buf[0]["section"],
            "section_path": buf[0].get("section_path", buf[0]["section"]),
            "page_start": buf[0]["page"],
            "page_end": buf[-1]["page"],
            "kinds": sorted({u["kind"] for u in buf}),
        })
        carry = _tail_tokens(body, overlap) if keep_overlap else ""
        buf, buf_tokens = [], 0

    for u in units:
        t = count_tokens(u["text"])
        is_major_heading = (
            u["kind"] == "heading"
            and u["level"] is not None
            and u["level"] <= 2
        )
        if buf and is_major_heading and buf_tokens >= min_section_tokens:
            flush(keep_overlap=False)
        elif buf and buf_tokens + t > target:
            flush()
        buf.append(u)
        buf_tokens += t
        if buf_tokens >= target:
            flush()
    flush()
    return chunks


def merge_tiny(chunks: list[dict], min_chars: int) -> list[dict]:
    """
    兜底：把仍低于 min_chars 的 chunk 并入前一块（不限制同章节，
    因为碎片往往正是"自己独占一节"的那些）。保留前一节的 section 标签。
    """
    if not chunks:
        return []
    out = [chunks[0]]
    for c in chunks[1:]:
        prev = out[-1]
        if len(c["text"]) < min_chars:
            prev["text"] = prev["text"].rstrip() + "\n\n" + c["text"].lstrip()
            prev["page_end"] = max(prev["page_end"], c["page_end"])
            prev["kinds"] = sorted(set(prev["kinds"]) | set(c["kinds"]))
        else:
            out.append(c)
    # 首块过短则并入次块
    if len(out) > 1 and len(out[0]["text"]) < min_chars:
        out[1]["text"] = out[0]["text"].rstrip() + "\n\n" + out[1]["text"].lstrip()
        out[1]["page_start"] = min(out[0]["page_start"], out[1]["page_start"])
        out[1]["kinds"] = sorted(set(out[0]["kinds"]) | set(out[1]["kinds"]))
        out.pop(0)
    return out


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def chunk_doc(slug: str, target: int = CHUNK_TOKENS, overlap: int = CHUNK_OVERLAP,
              drop_refs: bool = True) -> dict:
    entries, cl_path = load_content_list(slug)
    units = entries_to_units(entries, drop_refs=drop_refs)
    chunks = pack_units(units, target, overlap)
    chunks = merge_tiny(chunks, MIN_CHUNK_CHARS)

    # 编号 + 统计字段
    pages = sorted({e.get("page_idx", 0) for e in entries})
    for i, c in enumerate(chunks):
        c["chunk_id"] = i
        c["token_count"] = count_tokens(c["text"])
        c["char_count"] = len(c["text"])
        c["has_table"] = "table" in c["kinds"]
        c["has_image"] = "image" in c["kinds"]
        c["doc"] = slug

    result = {
        "doc": {
            "slug": slug,
            "source": cl_path.name,
            "page_count": (max(pages) + 1) if pages else 0,
            "chunked_at": datetime.now().isoformat(timespec="seconds"),
            "params": {
                "target_tokens": target,
                "overlap_tokens": overlap,
                "min_chunk_chars": MIN_CHUNK_CHARS,
                "drop_references": drop_refs,
            },
        },
        "chunks": chunks,
    }

    out_path = CHUNKS_DIR / f"{slug}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    result["_out_path"] = str(out_path)
    return result


def print_stats(res: dict) -> None:
    chunks = res["chunks"]
    if not chunks:
        print("[WARN] 没有产出任何 chunk")
        return
    toks = [c["token_count"] for c in chunks]
    chars = [c["char_count"] for c in chunks]
    pages = [c["page_start"] for c in chunks]

    print(f"\n{'=' * 64}")
    print(f"  文档: {res['doc']['slug']}  ({res['doc']['page_count']} 页)")
    print(f"  Chunk 数: {len(chunks)}")
    print(f"  Token: min={min(toks)} 中位={sorted(toks)[len(toks)//2]} max={max(toks)}"
          f" 合计={sum(toks)}")
    print(f"  字符: min={min(chars)} 中位={sorted(chars)[len(chars)//2]} max={max(chars)}")
    print(f"  碎片(<200字符): {sum(1 for x in chars if x < 200)}")
    print(f"  页码覆盖: P{min(pages)} ~ P{max(c['page_end'] for c in chunks)}")
    print(f"  含表格: {sum(1 for c in chunks if c['has_table'])}"
          f" | 含图片: {sum(1 for c in chunks if c['has_image'])}")
    print(f"\n  章节分布:")
    sec_count: dict[str, int] = {}
    for c in chunks:
        sec_count[c["section"]] = sec_count.get(c["section"], 0) + 1
    for sec, n in sec_count.items():
        print(f"    [{n:>3}] {sec[:78]}")
    print(f"\n  输出: {res['_out_path']}")
    print("=" * 64)


def print_preview(res: dict, n: int = 5) -> None:
    print(f"\n{'#' * 64}\n# 前 {n} 个 chunk 预览\n{'#' * 64}")
    for c in res["chunks"][:n]:
        print(f"\n{'─' * 64}")
        print(f"[chunk {c['chunk_id']}] {c['token_count']} tok | "
              f"P{c['page_start']}-P{c['page_end']} | {c['section'][:60]}")
        print(f"{'─' * 64}")
        print(c["text"][:900])
        if len(c["text"]) > 900:
            print(f"... (共 {c['char_count']} 字符)")


if __name__ == "__main__":
    argv = sys.argv[1:]
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        print(__doc__)
        sys.exit(1)

    slug_arg = args[0]
    target = int(argv[argv.index("--tokens") + 1]) if "--tokens" in argv else CHUNK_TOKENS
    keep_refs = "--keep-refs" in argv

    res = chunk_doc(slug_arg, target=target, drop_refs=not keep_refs)
    print_stats(res)
    if "--preview" in argv:
        idx = argv.index("--preview")
        n = int(argv[idx + 1]) if idx + 1 < len(argv) and argv[idx + 1].isdigit() else 3
        print_preview(res, n)
