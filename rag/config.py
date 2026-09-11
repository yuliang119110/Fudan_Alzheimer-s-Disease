"""
RAG 论文库 — 全局配置
通用文档解析 + 切分流水线，独立于 EENTagent / bgem3

数据流：
    PDF → MinerU 精准版 API → parsed/<slug>/{full.md, *_content_list.json, images/}
        → chunk_doc.py → chunks/<slug>.json
"""

import os
from pathlib import Path

# ============================================================
# .env 加载（极简实现，避免额外依赖）
# ============================================================
# 优先使用已存在的环境变量；否则从项目根的 .env 读取。
# .env 必须被 .gitignore 忽略 —— 切勿提交密钥。

def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip().strip('"').strip("'")
        if k and v and k not in os.environ:
            os.environ[k] = v


ROOT_DIR = Path(__file__).parent.resolve()
_load_dotenv(ROOT_DIR / ".env")

# ============================================================
# 目录
# ============================================================
PARSED_DIR = ROOT_DIR / "parsed"      # MinerU 原始产物（按 slug 分目录）
CHUNKS_DIR = ROOT_DIR / "chunks"      # 切分后的 chunk 集
DOCS_DIR = ROOT_DIR / "docs"          # 原始 PDF 归档（可选）

for _d in (PARSED_DIR, CHUNKS_DIR, DOCS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

# ============================================================
# MinerU 精准版 API（需 Token）
# ============================================================
# 密钥一律走环境变量 / .env，仓库内不保留任何明文。
# 获取：https://mineru.net  → API 管理页
# 配置：复制 .env.example 为 .env 填入，或 setx MINERU_TOKEN "sk-..."
MINERU_TOKEN = os.environ.get("MINERU_TOKEN", "")
MINERU_BASE = "https://mineru.net/api/v4"

# 解析参数
MINERU_MODEL = "pipeline"   # pipeline（数字版PDF，快）/ vlm（扫描版、复杂版面，慢但强）
MINERU_OCR = False          # 有文本层的 PDF 设 False，省时且不引入 OCR 误差
MINERU_LANG = "ch"          # ch / en / auto
MINERU_TABLE = True
MINERU_FORMULA = False

# 轮询
POLL_INTERVAL = 15          # 秒
POLL_MAX_WAIT = 3600        # 秒（单篇论文通常 1-5 分钟）

# ============================================================
# Chunk 参数
# ============================================================
CHUNK_TOKENS = 600          # 目标 token 数（tiktoken cl100k_base 计算）
CHUNK_OVERLAP = 100         # 重叠 token 数
MIN_CHUNK_CHARS = 120       # 小于此字符数的碎片合并进邻块（避免 7 字符噪声块）

# 不参与切分的章节（标题正则，忽略大小写）
DROP_SECTIONS = [
    r"^references?$",
    r"^bibliography$",
    r"^literature cited$",
    r"^参考文献$",
    r"^致谢$",
    r"^acknowledg(e)?ments?$",
    r"^author contributions$",
    r"^conflicts? of interest",
    r"^funding$",
    r"^supporting information$",
    r"^orcid( i[dD])?$",
    r"^data availability",
    r"^supplementary",
]

# 直接丢弃的内容块类型（MinerU content_list 的 type 字段）
# 实测结论（来自《耳畸形整复外科学》与 ALZ-22-e71800 两份 content_list.json）：
#   标题 = type:"text" + text_level    ← 不是 header！
#   正文 = type:"text" + text_level:null
#   header/footer/page_number = 页眉页脚（running head、'17 of 20'）→ 噪声，丢弃
#   aside_text = 表格续表标记（'TABLE2(Continued)'）→ 噪声，丢弃
DROP_BLOCK_TYPES = {
    "page_number",     # 页脚页码，如 '17 of 20'
    "page_footnote",   # 页脚注释
    "footer",          # 页脚（出版社名等）
    "header",          # 页眉（running head，如 'GABB ET AL.'）
    "aside_text",      # 旁注/续表标记
}

# 参考文献条目的识别方式（两种版式都遇到过）：
#   vlm/书籍版： type == "ref_text"
#   pipeline/论文版：type == "list" + sub_type == "ref_text"，正文在 list_items 里
REF_BLOCK_TYPE = "ref_text"
REF_SUBTYPE = "ref_text"

# ============================================================
# DeepSeek API（密钥同样走环境变量 / .env）
# ============================================================
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = "deepseek-chat"
DEEPSEEK_TEMPERATURE = 0.3
DEEPSEEK_MAX_TOKENS = 4096

def require(env_name: str, purpose: str) -> str:
    """取必需的环境变量，缺失时给出可操作的报错（而不是 401）"""
    val = os.environ.get(env_name, "").strip()
    if not val:
        raise SystemExit(
            f"\n[配置缺失] 环境变量 {env_name} 未设置 —— 用于 {purpose}\n"
            f"  方式一：复制 .env.example 为 .env，填入 {env_name}=...\n"
            f"  方式二：setx {env_name} \"你的key\"  然后重开终端\n"
        )
    return val


# ============================================================
# 分页与引用
# ============================================================
# MinerU 的 page_idx 从 0 开始，转为人类可读页码时 +1
PAGE_OFFSET = 1
