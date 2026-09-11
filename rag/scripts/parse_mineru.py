"""
MinerU 精准版 API — 通用文档解析
PDF → 上传 → 云端解析 → 下载解压 → parsed/<slug>/

产出（MinerU 精准版 zip 内容）：
    full.md                      全书/全文 Markdown
    <uuid>_content_list.json     ★ 结构化内容块，带 page_idx（页码的来源）
    <uuid>_model.json            版面模型原始输出
    layout.json                  版面分析
    images/                      抽取出的图片

用法：
    python parse_mineru.py <pdf路径> [<pdf路径2> ...]
    python parse_mineru.py paper.pdf --ocr          # 扫描版强制 OCR
    python parse_mineru.py paper.pdf --model vlm    # 复杂版面用 vlm
"""

import io
import re
import sys
import time
import zipfile
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    MINERU_BASE,
    MINERU_MODEL,
    MINERU_OCR,
    MINERU_LANG,
    MINERU_TABLE,
    MINERU_FORMULA,
    POLL_INTERVAL,
    POLL_MAX_WAIT,
    PARSED_DIR,
    require,
)


def _headers() -> dict:
    token = require("MINERU_TOKEN", "MinerU 精准版 API 鉴权")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


# ============================================================
# API 调用
# ============================================================

def create_batch(files_info: list[dict], model: str, ocr: bool) -> dict:
    """POST /file-urls/batch — 创建批处理任务，返回 batch_id 和预签名上传地址"""
    payload = {
        "files": files_info,
        "model_version": model,
        "is_ocr": ocr,
        "enable_formula": MINERU_FORMULA,
        "enable_table": MINERU_TABLE,
        "language": MINERU_LANG,
    }
    resp = requests.post(
        f"{MINERU_BASE}/file-urls/batch",
        headers=_headers(),
        json=payload,
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if data.get("code") != 0:
        raise RuntimeError(f"创建任务失败: {data}")
    return data["data"]


def upload_files(file_urls: list[str], file_paths: list[Path]) -> None:
    """PUT 上传到预签名 URL（注意 Content-Type 必须为空字符串）"""
    for url, path in zip(file_urls, file_paths):
        size_mb = path.stat().st_size / 1024 / 1024
        print(f"   [UPLOAD] {path.name} ({size_mb:.1f} MB)...", end=" ", flush=True)
        with open(path, "rb") as f:
            resp = requests.put(url, data=f, headers={"Content-Type": ""}, timeout=1200)
        resp.raise_for_status()
        print("[OK]")


def poll_batch(batch_id: str) -> list[dict]:
    """GET /extract-results/batch/{batch_id} — 轮询直到全部 done"""
    start = time.time()
    last_states = {}
    while time.time() - start < POLL_MAX_WAIT:
        resp = requests.get(
            f"{MINERU_BASE}/extract-results/batch/{batch_id}",
            headers=_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()

        if data.get("code") != 0:
            print(f"   [WARN] 查询异常: {data.get('msg', data)}")
            time.sleep(POLL_INTERVAL)
            continue

        results = data.get("data", {}).get("extract_result", [])
        if not results:
            print("   [WARN] 结果为空，继续等待...")
            time.sleep(POLL_INTERVAL)
            continue

        elapsed = int(time.time() - start)
        states = {r.get("file_name", "?"): r.get("state", "unknown") for r in results}
        if states != last_states:
            for fname, state in states.items():
                print(f"   [STATUS] {fname}: {state}  [{elapsed}s]")
            last_states = states.copy()

        if all(s == "done" for s in states.values()):
            return results
        if any(s == "failed" for s in states.values()):
            msgs = [
                f"{r.get('file_name')}: {r.get('err_msg', '?')}"
                for r in results
                if r.get("state") == "failed"
            ]
            raise RuntimeError(f"解析失败: {'; '.join(msgs)}")

        time.sleep(POLL_INTERVAL)

    raise TimeoutError(f"超时: batch_id={batch_id} 超过 {POLL_MAX_WAIT}s 未完成")


def download_and_extract(zip_url: str, output_dir: Path) -> None:
    """下载结果 zip 并解压"""
    print(f"   [DOWNLOAD] → {output_dir.name}/ ...", end=" ", flush=True)
    resp = requests.get(zip_url, timeout=600)
    resp.raise_for_status()
    output_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
        zf.extractall(output_dir)
    n = len(list(output_dir.rglob("*")))
    print(f"[OK] {n} 个文件")


# ============================================================
# 工具
# ============================================================

def slugify(pdf_path: Path) -> str:
    """从文件名生成 slug，如 ALZ-22-e71800.pdf → ALZ-22-e71800"""
    s = pdf_path.stem
    s = re.sub(r"[^\w\-]+", "-", s, flags=re.UNICODE).strip("-")
    return s or "doc"


def probe_pdf(pdf_path: Path) -> dict:
    """本地探测页数 / 是否含文本层，用于决定 is_ocr 和参数校验"""
    try:
        import pymupdf
    except ImportError:
        return {}

    with pymupdf.open(pdf_path) as doc:
        pages = doc.page_count
        text_chars = sum(len(p.get_text().strip()) for p in doc)
    return {
        "pages": pages,
        "text_chars": text_chars,
        "has_text_layer": text_chars > pages * 100,   # 平均每页 >100 字符视为有文本层
    }


# ============================================================
# 主流程
# ============================================================

def parse(pdf_paths: list[Path], model: str, ocr: bool) -> None:
    print("=" * 64)
    print("  MinerU 精准版 — 文档解析")
    print(f"  模型: {model} | OCR: {ocr} | 表格: {MINERU_TABLE} | 语言: {MINERU_LANG}")
    print("=" * 64)

    # 上传前先探测，给出参数建议
    for p in pdf_paths:
        if not p.exists():
            raise FileNotFoundError(f"PDF 不存在: {p}")
        info = probe_pdf(p)
        if info:
            hint = "有文本层" if info["has_text_layer"] else "无文本层(需 OCR)"
            print(f"[PDF] {p.name}: {info['pages']} 页, {info['text_chars']} 字符 → {hint}")
            if not info["has_text_layer"] and not ocr:
                print(f"   [WARN] 该 PDF 无文本层，建议加 --ocr 重跑")

    files_info = [{"name": p.name, "data_id": slugify(p)} for p in pdf_paths]

    # Step 1: 创建任务
    batch_data = create_batch(files_info, model=model, ocr=ocr)
    batch_id = batch_data["batch_id"]
    file_urls = batch_data["file_urls"]
    print(f"\n[1/3] 任务已创建 batch_id={batch_id}")

    # Step 2: 上传
    print("[2/3] 上传文件...")
    upload_files(file_urls, pdf_paths)

    # Step 3: 等待
    print(f"[3/3] 等待云端解析（轮询间隔 {POLL_INTERVAL}s）...")
    results = poll_batch(batch_id)

    # Step 4: 下载
    print("\n下载结果...")
    for r in results:
        zip_url = r.get("full_zip_url", "")
        fname = r.get("file_name", "unknown")
        if not zip_url:
            print(f"   [WARN] 无下载链接: {fname}")
            continue
        out_dir = PARSED_DIR / slugify(Path(fname))
        download_and_extract(zip_url, out_dir)

        # 校验关键产物
        md = list(out_dir.glob("*.md"))
        cl = list(out_dir.glob("*_content_list.json"))
        print(f"   [CHECK] full.md: {'OK' if md else '缺失'}"
              f" | content_list.json: {'OK' if cl else '缺失'}")
        if not cl:
            print("   [WARN] 缺 content_list.json → 后续切分将拿不到页码，"
                  "可能是 model_version=pipeline 的旧版本产出")

    print(f"\n{'=' * 64}")
    print(f"  完成 → {PARSED_DIR}")
    print(f"  下一步: python chunk_doc.py {slugify(pdf_paths[0])}")
    print("=" * 64)


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    model = MINERU_MODEL
    ocr = MINERU_OCR

    if "--model" in sys.argv:
        model = sys.argv[sys.argv.index("--model") + 1]
    if "--ocr" in sys.argv:
        ocr = True

    if not args:
        print(__doc__)
        sys.exit(1)

    parse([Path(a).resolve() for a in args], model=model, ocr=ocr)
