"""
最简 RAG Agent —— 单篇论文直接全量入 context

为什么不需要向量检索：
    ALZ-22-e71800 全文切完只有 29 个 chunk ≈ 14k tokens，
    DeepSeek 上下文完全放得下。所以最简做法就是
    「把整篇论文塞进 prompt，直接问」—— 没有 embedding、
    没有 Qdrant、没有 bgem3 子进程，零检索误差。

    论文变多（>10 篇 / >100k tokens）时再上向量检索。

上下文缓存友好：
    论文正文放在固定的前缀位置（system + 首个 user/assistant 对），
    提问只追加在后面。DeepSeek 的 prompt cache 会命中这个固定前缀，
    多轮追问的输入成本大幅下降。

用法：
    python agent.py                              # 交互模式
    python agent.py "MCI 核心结局集包括哪些维度？"   # 单次提问
    python agent.py --doc ALZ-22-e71800          # 指定文档
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from openai import OpenAI

from config import (
    CHUNKS_DIR,
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MODEL,
    DEEPSEEK_TEMPERATURE,
    DEEPSEEK_MAX_TOKENS,
    require,
)

SYSTEM_PROMPT = """你是一名严谨的科研文献助手，基于用户提供的论文全文回答问题。

## 规则
1. **只依据论文内容回答**，不得引入论文之外的医学知识、数据或文献。
2. **必须标注出处页码**，格式 `(P12)` 或 `(P12-14)`，页码取自片段开头的标注。
3. 论文未涉及的内容，明确说"论文未涉及此内容"，不要臆测。
4. 涉及临床决策、药物剂量时，末尾提醒：以上仅为文献内容，临床决策需由执业医师结合患者情况判断。
5. 回答用中文；专业术语可保留英文原词。结构清晰，重点突出，不要空话。"""

ACK = "已读取全文，请提问。"

# token 计数（与 chunk_doc.py 同一口径）
try:
    import tiktoken
    _ENC = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_ENC.encode(text, disallowed_special=()))
except Exception:
    def count_tokens(text: str) -> int:
        return int(len(text) / 3.5)


# ------------------------------------------------------------
# 装载
# ------------------------------------------------------------

def load_doc(slug: str = None) -> tuple[str, list[dict]]:
    """读取 chunks/<slug>.json，返回 (slug, chunks)"""
    files = sorted(CHUNKS_DIR.glob("*.json"))
    if not files:
        raise FileNotFoundError(f"{CHUNKS_DIR} 下没有 chunk 文件，先跑 chunk_doc.py")
    if slug:
        path = CHUNKS_DIR / f"{slug}.json"
        if not path.exists():
            avail = ", ".join(p.stem for p in files)
            raise FileNotFoundError(f"找不到 {slug}.json，现有: {avail}")
    else:
        path = files[0]

    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["doc"]["slug"], data["chunks"]


def build_context(chunks: list[dict]) -> str:
    """把 chunk 拼成带页码标注的全文"""
    parts = []
    for c in chunks:
        page = f"P{c['page_start']}" if c["page_start"] == c["page_end"] \
            else f"P{c['page_start']}-{c['page_end']}"
        head = f"【{page} · {c['section'] or '正文'}】"
        parts.append(f"{head}\n{c['text']}")
    return "\n\n".join(parts)


# ------------------------------------------------------------
# 提问
# ------------------------------------------------------------

def make_client() -> OpenAI:
    key = require("DEEPSEEK_API_KEY", "调用 DeepSeek 生成回答")
    return OpenAI(api_key=key, base_url=DEEPSEEK_BASE_URL)


def build_messages(context: str, history: list[dict], question: str = None) -> list[dict]:
    """
    固定前缀（可被 prompt cache 命中）+ 历史 + 本轮提问
    """
    msgs = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"以下是论文全文，共 {context.count('【')} 个片段：\n\n{context}"},
        {"role": "assistant", "content": ACK},
    ]
    msgs.extend(history)
    if question:
        msgs.append({"role": "user", "content": question})
    return msgs


def ask(client: OpenAI, messages: list[dict], stream: bool = True) -> str:
    resp = client.chat.completions.create(
        model=DEEPSEEK_MODEL,
        messages=messages,
        temperature=DEEPSEEK_TEMPERATURE,
        max_tokens=DEEPSEEK_MAX_TOKENS,
        stream=stream,
    )
    if not stream:
        return resp.choices[0].message.content

    out = []
    for chunk in resp:
        delta = chunk.choices[0].delta.content
        if delta:
            out.append(delta)
            print(delta, end="", flush=True)
    print()
    return "".join(out)


# ------------------------------------------------------------
# 主流程
# ------------------------------------------------------------

def main():
    argv = sys.argv[1:]
    slug = None
    if "--doc" in argv:
        i = argv.index("--doc")
        slug = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    question = " ".join(argv).strip() or None

    doc_slug, chunks = load_doc(slug)
    context = build_context(chunks)
    est = count_tokens(context)

    print("=" * 64)
    print(f"  文档: {doc_slug}")
    print(f"  片段: {len(chunks)} 个 | 上下文 {est:,} tokens（全量直送，无检索）")
    print(f"  模型: {DEEPSEEK_MODEL}")
    print("=" * 64)

    client = make_client()

    # 单次提问
    if question:
        print(f"\n> {question}\n")
        ask(client, build_messages(context, [], question))
        return

    # 交互模式
    history: list[dict] = []
    print("\n交互模式：输入问题回车；exit 退出；clear 清空历史\n")
    while True:
        try:
            q = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not q:
            continue
        if q.lower() in ("exit", "quit", "q"):
            break
        if q.lower() == "clear":
            history.clear()
            print("[已清空对话历史，论文上下文保留]\n")
            continue

        print()
        answer = ask(client, build_messages(context, history, q))
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": answer})
        print()


if __name__ == "__main__":
    main()
