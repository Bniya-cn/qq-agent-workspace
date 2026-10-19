"""文档生成与 QQ 文件投递服务 (Markdown / Word .docx / 中文排版 .pdf)。

.docx 优先调用本机 Pandoc，把定界公式写成 Word 可编辑公式。Pandoc 不可用或失败时，
退回标准库 OpenXML，公式保持原文。PDF 仍由标准库生成。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import ipaddress
import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
import zipfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional, Awaitable, Callable
from urllib.parse import urljoin, urlsplit
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

try:
    from nonebot import logger
except ImportError:
    import logging as _logging
    logger = _logging.getLogger("doc_service")

BOT_ROOT = Path(__file__).resolve().parents[3]
EXPORTS_DIR = BOT_ROOT / "data" / "exports"
TMP_EXPORT_DIR = Path("/tmp/qq_agent_exports")
REFERENCE_DOCX = Path(__file__).resolve().parent / "assets" / "reference.docx"
PANDOC_FROM = "markdown+pipe_tables+tex_math_dollars+tex_math_single_backslash"
PANDOC_TIMEOUT_SEC = 30
_CHAR_LABELS = {"amiya": "阿米娅 (Amiya)", "kaltsit": "凯尔希 (Kal'tsit)", "dania": "达妮娅 (Dania)"}
_TABLE_SEP_CELL_RE = re.compile(r"^:?-{3,}:?$")
_HR_RE = re.compile(r"^[-=]{3,}$")
_FENCE_OPEN_RE = re.compile(r"^```([A-Za-z0-9_-]*)\s*$")
_UNWRAP_FENCE_LANGS = frozenset({"", "markdown", "md", "table"})

# 当单次回复超过该字数时，自动在发送精简导读的同时附带完整文档文件（调高至 1800 字，防止普通中长对话误触发文件导出）
AUTO_FILE_CHAR_THRESHOLD = 1800

_EXPORT_CMD_PREFIXES = ("/doc", "/pdf", "/docx", "/word", "/md", "/markdown", "/export")
_EXPORT_KEYWORDS = (
    "导出pdf", "生成pdf", "转pdf", "转成pdf", "发个pdf", "输出pdf",
    "导出word", "生成word", "转word", "转成word", "发个word", "输出word",
    "导出docx", "生成docx", "转docx", "转成docx", "发个docx", "输出docx",
    "导出md", "生成md", "转md", "转成md", "导出markdown", "生成markdown",
    "做成文档", "生成文档", "导出文档", "发个文档", "做成文件", "生成文件", "导出文件", "发个文件", "用文件发", "以文件形式",
    "docx格式", "pdf格式", "word格式", "md格式", "markdown格式",
)


def detect_requested_formats(user_text: str, reply_len: int = 0, is_long_task: bool = False) -> list[str]:
    """检测用户需要导出的文件格式列表（返回如 ['pdf'], ['docx'], ['md']）。
    - 若用户显式点名了 pdf / docx / md，按点名格式返回；
    - 若用户说了“做成文件/导出文档”但未指定后缀，默认同时生成 ['docx', 'pdf']；
    - 若未显式要求文件，但回复长度超过 AUTO_FILE_CHAR_THRESHOLD (650字)，自动生成 ['docx'] 突破 QQ 长度限制。
    """
    s = (user_text or "").strip().lower()
    explicit_trigger = s.startswith(_EXPORT_CMD_PREFIXES) or any(k in s for k in _EXPORT_KEYWORDS)
    if not explicit_trigger and re.search(
        r"(?:导出|生成|写|做成|转成|转为|保存为|输出|发我|发个|出一份|做一份|制作).{0,24}(?:pdf|word|docx|markdown|md|文档|文件)",
        s,
        re.IGNORECASE,
    ):
        explicit_trigger = True

    formats: list[str] = []
    if explicit_trigger:
        if "pdf" in s or s.startswith("/pdf"):
            formats.append("pdf")
        if any(w in s for w in ("word", "docx", "/word", "/docx")):
            formats.append("docx")
        if any(w in s for w in ("markdown", ".md", "md文件", "导出md", "生成md", "转md", "/md", "md格式")):
            formats.append("md")
        if not formats:
            # 用户只说“生成文档/做成文件发我”，默认提供最实用的 Word (.docx) + PDF (.pdf)
            formats = ["docx", "pdf"]
        return formats

    # 超长回复自动转文档保护（突破 QQ 字数框限制）
    if reply_len >= AUTO_FILE_CHAR_THRESHOLD or (is_long_task and reply_len >= 480):
        return ["docx"]
    return []


def is_export_followup(text: str) -> bool:
    """只匹配不包含新撰写主题的简短导出指令。"""
    return bool(re.fullmatch(
        r"(?:请|帮我|给我)?\s*(?:/export|/docx|/word|/pdf|/md|导出|转成|转为|保存为|发文件|发给我)"
        r"\s*(?:(?:为|成|一份|个|一下|上面|刚才|这份|的)\s*)*"
        r"(?:pdf|docx|word|markdown|md|文档|文件)?(?:格式)?(?:吧|给我|发我)?[。！!\s]*",
        (text or "").strip().lower(),
    ))


DOCUMENT_PROMPT = (
    "\n【文件交付要求】请现在直接输出可写入文档的完整 Markdown 正文，标题和内容均需实际给出。"
    "文件生成及 QQ 上传由本地后端自动执行，你只负责输出完整正文。不要询问是否导出，不要仅介绍将要写什么，"
    "严禁调用云端沙箱生成文件，严禁虚构附件、下载链接、文件路径或声称已完成编译上传。"
)


def is_placeholder_document(content: str) -> bool:
    """拦截只有交付宣称与询问、没有正文的回复，避免把空壳介绍打包成文件。"""
    if not content or len(content.strip()) < 40:
        return True
    if "沙箱的文件目录在本次任务中被回收" in content and len(content.strip()) < 220:
        return True
    return bool(re.search(r"(?:需要|要不要|是否|如果).*?(?:导出|生成.{0,6}文件)", content, re.S)) and not re.search(
        r"(?m)^\s*(?:#{1,6}\s|[一二三四五六七八九十]+[、．.]|\d+[、．.]|[-*]\s)", content
    )


def extract_doc_title(user_text: str, content: str) -> str:
    """从 Markdown 一级标题或用户指令中提取干净的文档标题。"""
    bad_chars = set("*`#_~[]()<>:/\\|?*")
    for line in (content or "").splitlines()[:10]:
        ls = line.strip()
        if ls.startswith("# ") or ls.startswith("## "):
            raw_t = "".join(ch for ch in ls.lstrip("#").strip() if ch not in bad_chars and ch not in "\r\n\t").strip()
            if 2 <= len(raw_t) <= 36:
                return raw_t
    clean_u = re.sub(r"^/[a-zA-Z]+\s*", "", (user_text or "").strip())
    clean_u = "".join(ch for ch in clean_u if ch not in bad_chars and ch not in "\r\n\t").strip()
    if len(clean_u) > 24:
        clean_u = clean_u[:24]
    return clean_u or "罗德岛工作文档"


def _safe_filename(title: str, ext: str) -> str:
    clean = "".join(ch if (ch.isalnum() or ch in "_-") else "_" for ch in (title or "")).strip("_") or "罗德岛工作文档"
    ts = datetime.now().strftime("%m%d_%H%M%S")
    ext_c = ext.lstrip(".")
    return f"{clean[:26]}_{ts}_{uuid.uuid4().hex[:8]}.{ext_c}"


def _char_label(character: str) -> str:
    return _CHAR_LABELS.get(character, character)


def _is_table_line(stripped: str) -> bool:
    return stripped.startswith("|") and stripped.endswith("|") and len(stripped) > 2


def _is_table_separator(stripped: str) -> bool:
    if not _is_table_line(stripped):
        return False
    cells = [cell.strip() for cell in stripped.strip("|").split("|")]
    return bool(cells) and all(_TABLE_SEP_CELL_RE.fullmatch(cell) for cell in cells)


def prepare_markdown(content: str) -> str:
    """整理即将写入 Word 的 Markdown。

    只脱掉「里面全是表格」的代码围栏，避免表格被写成等宽代码。
    标明公式、TikZ 或程序的围栏保持代码块。纯横线行丢掉，表格分隔行保留。
    """
    lines = (content or "").splitlines()
    output: list[str] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].strip()
        fence = _FENCE_OPEN_RE.match(stripped)
        if fence is None:
            if _HR_RE.fullmatch(stripped):
                index += 1
                continue
            output.append(lines[index])
            index += 1
            continue

        body: list[str] = []
        cursor = index + 1
        closed = False
        while cursor < len(lines):
            if lines[cursor].strip().startswith("```"):
                closed = True
                break
            body.append(lines[cursor])
            cursor += 1
        language = fence.group(1).lower()
        nonempty = [line.strip() for line in body if line.strip()]
        table_only = bool(nonempty) and all(_is_table_line(line) or _is_table_separator(line) for line in nonempty)
        if closed and language in _UNWRAP_FENCE_LANGS and table_only:
            output.extend(body)
            index = cursor + 1
            continue
        output.append(lines[index])
        if not closed:
            index += 1
            continue
        output.extend(body)
        output.append(lines[cursor])
        index = cursor + 1
    return "\n".join(output)


def _markdown_document(title: str, content: str, character: str) -> str:
    """拼上与 Markdown 导出相同的干员和时间抬头。"""
    label = _char_label(character)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    body = (content or "").strip()
    if not body.startswith("# "):
        header = f"# {title}\n\n> **生成干员**：{label}  |  **生成时间**：{now_str}\n\n"
    else:
        header = f"> **生成干员**：{label}  |  **生成时间**：{now_str}\n\n"
    return header + body + "\n"


def _valid_docx(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 64:
        return False
    try:
        with zipfile.ZipFile(path) as archive:
            return "word/document.xml" in archive.namelist()
    except zipfile.BadZipFile:
        return False


def _pandoc_docx(markdown: str, out_path: Path) -> None:
    """用 Pandoc 把定界公式写成 Word OMML。不传 --mathml。"""
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise FileNotFoundError("pandoc 不在 PATH")
    if not REFERENCE_DOCX.is_file():
        raise FileNotFoundError(f"缺少参考文档 {REFERENCE_DOCX}")
    command = [
        pandoc,
        "--from",
        PANDOC_FROM,
        "--to",
        "docx",
        "--reference-doc",
        str(REFERENCE_DOCX),
        "-o",
        str(out_path),
    ]
    try:
        completed = subprocess.run(
            command,
            input=markdown.encode("utf-8"),
            capture_output=True,
            timeout=PANDOC_TIMEOUT_SEC,
            check=False,
        )
    except subprocess.TimeoutExpired:
        out_path.unlink(missing_ok=True)
        raise
    if completed.returncode != 0 or not _valid_docx(out_path):
        detail = (completed.stderr or b"").decode("utf-8", errors="replace").strip()
        out_path.unlink(missing_ok=True)
        raise RuntimeError(detail[:500] or "pandoc 未写出合法 docx")


def generate_markdown_file(title: str, content: str, character: str = "amiya") -> Path:
    """生成带元信息头的标准 UTF-8 Markdown (.md) 文件。"""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    TMP_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = _safe_filename(title, "md")
    out_path = EXPORTS_DIR / fname
    full_md = _markdown_document(title, content, character)
    out_path.write_text(full_md, encoding="utf-8")
    if out_path.resolve() != (TMP_EXPORT_DIR / fname).resolve():
        shutil.copy2(out_path, TMP_EXPORT_DIR / fname)
    return out_path


def _build_runs_xml(line_text: str, size_half_pt: int = 22, default_bold: bool = False, color: str = "1E293B") -> str:
    """解析行内 **加粗** 与 `代码` 标记，生成 Word OpenXML <w:r> 序列。"""
    tokens = re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", line_text)
    runs: list[str] = []
    for tok in tokens:
        if not tok:
            continue
        is_bold = default_bold
        r_color = color
        r_font = "PingFang SC"
        shd = ""
        text_val = tok
        if tok.startswith("**") and tok.endswith("**") and len(tok) > 4:
            is_bold = True
            text_val = tok[2:-2]
        elif tok.startswith("`") and tok.endswith("`") and len(tok) > 2:
            r_font = "Menlo"
            r_color = "0369A1"
            shd = '<w:shd w:val="clear" w:color="auto" w:fill="F1F5F9"/>'
            text_val = tok[1:-1]
        b_tag = "<w:b/><w:bCs/>" if is_bold else ""
        escaped = escape(text_val)
        runs.append(
            f'<w:r><w:rPr><w:rFonts w:ascii="{r_font}" w:hAnsi="{r_font}" w:eastAsia="PingFang SC"/>'
            f'{b_tag}<w:color w:val="{r_color}"/><w:sz w:val="{size_half_pt}"/><w:szCs w:val="{size_half_pt}"/>{shd}</w:rPr>'
            f'<w:t xml:space="preserve">{escaped}</w:t></w:r>'
        )
    return "".join(runs)


def _build_table_xml(rows: list[list[str]]) -> str:
    """将 Markdown 表格行转换为带边框与表头底色的 Word OpenXML <w:tbl>。"""
    if not rows:
        return ""
    tbl_pr = (
        "<w:tblPr>"
        '<w:tblW w:w="5000" w:type="pct"/>'
        "<w:tblBorders>"
        '<w:top w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:left w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:bottom w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:right w:val="single" w:sz="6" w:space="0" w:color="CBD5E1"/>'
        '<w:insideH w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        '<w:insideV w:val="single" w:sz="4" w:space="0" w:color="E2E8F0"/>'
        "</w:tblBorders>"
        "</w:tblPr>"
    )
    tr_list: list[str] = []
    for r_idx, row in enumerate(rows):
        is_header = r_idx == 0
        shd = '<w:shd w:val="clear" w:color="auto" w:fill="F1F5F9"/>' if is_header else ""
        tc_list: list[str] = []
        for cell in row:
            runs = _build_runs_xml(cell.strip(), size_half_pt=20, default_bold=is_header, color="0F172A")
            tc_list.append(
                f"<w:tc><w:tcPr>{shd}<w:tcMar>"
                '<w:top w:w="80" w:type="dxa"/><w:bottom w:w="80" w:type="dxa"/>'
                '<w:left w:w="120" w:type="dxa"/><w:right w:w="120" w:type="dxa"/>'
                "</w:tcMar></w:tcPr>"
                f'<w:p><w:pPr><w:spacing w:before="40" w:after="40"/></w:pPr>{runs}</w:p></w:tc>'
            )
        tr_list.append(f"<w:tr>{''.join(tc_list)}</w:tr>")
    return f"<w:tbl>{tbl_pr}{''.join(tr_list)}</w:tbl>"


def _write_docx_fallback(out_path: Path, title: str, content: str, character: str) -> None:
    """Pandoc 不可用时的 OpenXML 写入。公式保持原文，表格和加粗仍按 Markdown 还原。"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
</Types>"""
    rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

    body_elements: list[str] = []

    def add_para(
        text: str,
        *,
        size_half_pt: int = 22,
        bold: bool = False,
        color: str = "1E293B",
        before: int = 100,
        after: int = 100,
        fill_bg: str = "",
        left_indent: int = 0,
    ) -> None:
        shd = f'<w:shd w:val="clear" w:color="auto" w:fill="{fill_bg}"/>' if fill_bg else ""
        ind = f'<w:ind w:left="{left_indent}"/>' if left_indent > 0 else ""
        runs = _build_runs_xml(text, size_half_pt=size_half_pt, default_bold=bold, color=color)
        body_elements.append(
            f'<w:p><w:pPr><w:spacing w:before="{before}" w:after="{after}" w:line="320" w:lineRule="auto"/>{shd}{ind}</w:pPr>'
            f"{runs}</w:p>"
        )

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 顶部文档主标题与元信息副标题
    add_para(title, size_half_pt=36, bold=True, color="0F172A", before=120, after=80)
    add_para(
        f"生成干员：{_char_label(character)}   |   导出时间：{now_str}",
        size_half_pt=18,
        bold=False,
        color="64748B",
        before=0,
        after=200,
    )

    lines = (content or "").splitlines()
    in_code = False
    table_buffer: list[list[str]] = []

    def flush_table() -> None:
        nonlocal table_buffer
        if table_buffer:
            body_elements.append(_build_table_xml(table_buffer))
            table_buffer = []

    for raw_line in lines:
        s = raw_line.rstrip()
        stripped = s.strip()

        if stripped.startswith("```"):
            flush_table()
            in_code = not in_code
            continue

        if in_code:
            add_para(s or " ", size_half_pt=19, bold=False, color="0F172A", before=20, after=20, fill_bg="F1F5F9", left_indent=240)
            continue

        # 识别 Markdown 表格行
        if _is_table_line(stripped):
            if _is_table_separator(stripped):
                continue
            cells = [cell.strip() for cell in stripped.strip("|").split("|")]
            table_buffer.append(cells)
            continue
        else:
            flush_table()

        if not stripped or _HR_RE.fullmatch(stripped):
            continue

        if stripped.startswith("# "):
            h_txt = stripped[2:].strip()
            if h_txt != title:
                add_para(h_txt, size_half_pt=32, bold=True, color="0369A1", before=220, after=100)
        elif stripped.startswith("## "):
            add_para(stripped[3:].strip(), size_half_pt=28, bold=True, color="0F172A", before=180, after=80)
        elif stripped.startswith("### "):
            add_para(stripped[4:].strip(), size_half_pt=24, bold=True, color="1E3A8A", before=140, after=60)
        elif stripped.startswith("#### "):
            add_para(stripped[5:].strip(), size_half_pt=22, bold=True, color="334155", before=120, after=60)
        elif stripped.startswith(("> ", "＞")):
            q_txt = stripped.lstrip(">＞ ").strip()
            add_para(q_txt, size_half_pt=21, bold=False, color="475569", before=60, after=60, fill_bg="F8FAFC", left_indent=320)
        elif re.match(r"^[-*•]\s+", stripped):
            item_txt = "• " + re.sub(r"^[-*•]\s+", "", stripped)
            add_para(item_txt, size_half_pt=22, bold=False, color="1E293B", before=60, after=60, left_indent=280)
        elif re.match(r"^\d+\.\s+", stripped):
            add_para(stripped, size_half_pt=22, bold=False, color="1E293B", before=60, after=60, left_indent=240)
        else:
            add_para(stripped, size_half_pt=22, bold=False, color="1E293B", before=80, after=80)

    flush_table()

    doc_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        + "".join(body_elements)
        + '<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
        '<w:pgMar w:top="1440" w:right="1440" w:bottom="1440" w:left="1440"/></w:sectPr>'
        "</w:body></w:document>"
    )

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", rels)
        zf.writestr("word/document.xml", doc_xml)


def generate_docx_file(title: str, content: str, character: str = "amiya") -> Path:
    """生成 Word 文件。有 Pandoc 时公式写成可编辑公式，否则退回手写 OpenXML。"""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    TMP_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = _safe_filename(title, "docx")
    out_path = EXPORTS_DIR / fname
    prepared = prepare_markdown(content)
    try:
        _pandoc_docx(_markdown_document(title, prepared, character), out_path)
    except Exception as exc:
        # Pandoc 中断时不保留半成品，整份改由手写写入器重做，公式退回原文。
        logger.warning(f"Pandoc 导出失败，公式保持原文: {exc}")
        out_path.unlink(missing_ok=True)
        _write_docx_fallback(out_path, title, prepared, character)
    if out_path.resolve() != (TMP_EXPORT_DIR / fname).resolve():
        shutil.copy2(out_path, TMP_EXPORT_DIR / fname)
    return out_path


def _visual_width(s: str) -> float:
    w = 0.0
    for ch in s:
        w += 1.0 if ord(ch) > 127 else 0.55
    return w


def _wrap_line_by_visual_width(line: str, max_units: float = 37.0) -> list[str]:
    if not line:
        return [""]
    out: list[str] = []
    cur = ""
    cur_w = 0.0
    for ch in line:
        cw = 1.0 if ord(ch) > 127 else 0.55
        if cur_w + cw > max_units and cur:
            out.append(cur)
            cur = ch
            cur_w = cw
        else:
            cur += ch
            cur_w += cw
    if cur:
        out.append(cur)
    return out


def generate_pdf_file(title: str, content: str, character: str = "amiya") -> Path:
    """生成带中文排版、标题高亮与多页自动分页的原生 A4 PDF (.pdf) 文件。"""
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    TMP_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    fname = _safe_filename(title, "pdf")
    out_path = EXPORTS_DIR / fname

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 组装渲染行列表: (text, font_size, rgb_tuple, extra_spacing_before, extra_spacing_after)
    render_items: list[tuple[str, float, tuple[float, float, float], float, float]] = [
        (title, 17.0, (0.06, 0.09, 0.16), 0.0, 6.0),
        (f"生成干员：{_char_label(character)}   |   导出时间：{now_str}", 9.5, (0.39, 0.45, 0.55), 0.0, 14.0),
    ]

    for raw_line in (content or "").splitlines():
        s = raw_line.strip()
        if not s or _HR_RE.fullmatch(s) or s.startswith("```"):
            render_items.append(("", 8.0, (0.12, 0.16, 0.23), 0.0, 4.0))
            continue
        clean_s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s).replace("`", "")
        if clean_s.startswith("# "):
            h = clean_s[2:].strip()
            if h != title:
                for sub in _wrap_line_by_visual_width(h, 29.0):
                    render_items.append((sub, 14.5, (0.01, 0.41, 0.63), 8.0, 4.0))
        elif clean_s.startswith("## "):
            for sub in _wrap_line_by_visual_width(clean_s[3:].strip(), 32.0):
                render_items.append((sub, 13.0, (0.06, 0.09, 0.16), 6.0, 3.0))
        elif clean_s.startswith("### "):
            for sub in _wrap_line_by_visual_width(clean_s[4:].strip(), 34.0):
                render_items.append((sub, 11.5, (0.12, 0.23, 0.54), 4.0, 2.0))
        elif _is_table_line(clean_s):
            if _is_table_separator(clean_s):
                continue
            cells = [c.strip() for c in clean_s.strip("|").split("|")]
            row_str = "  |  ".join(cells)
            for sub in _wrap_line_by_visual_width(row_str, 37.0):
                render_items.append((sub, 9.8, (0.15, 0.20, 0.28), 1.0, 1.5))
        else:
            for sub in _wrap_line_by_visual_width(clean_s, 37.5):
                render_items.append((sub, 10.5, (0.12, 0.16, 0.23), 0.0, 2.5))

    pages_cmds: list[list[str]] = []
    cur_cmds: list[str] = [
        # 顶部装饰分隔线
        "0.22 0.74 0.97 RG 1.2 w 48 760 m 547 760 l S"
    ]
    y = 795.0

    for text_line, fsize, (r, g, b), sp_before, sp_after in render_items:
        y -= sp_before
        if y < 62.0:
            pages_cmds.append(cur_cmds)
            cur_cmds = []
            y = 785.0
        if text_line:
            hex_str = text_line.encode("utf-16-be").hex().upper()
            cur_cmds.append(
                f"BT {r:.2f} {g:.2f} {b:.2f} rg /F1 {fsize:.1f} Tf 48 {y:.1f} Td <{hex_str}> Tj ET"
            )
        y -= (fsize * 1.45 + sp_after)

    if cur_cmds or not pages_cmds:
        pages_cmds.append(cur_cmds)

    total_pages = len(pages_cmds)
    for idx, cmds in enumerate(pages_cmds, 1):
        footer = f"— 第 {idx} / {total_pages} 页 · PRTS Document Service —"
        f_hex = footer.encode("utf-16-be").hex().upper()
        cmds.append(f"BT 0.55 0.60 0.68 rg /F1 8.5 Tf 210 32 Td <{f_hex}> Tj ET")

    objs: list[bytes] = []

    def add_obj(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font_id = add_obj(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /STSong-Light /Encoding /UniGB-UTF16-H "
        b"/DescendantFonts [ << /Type /Font /Subtype /CIDFontType0 /BaseFont /STSong-Light "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (GB1) /Supplement 4 >> >> ] >>"
    )
    page_ids: list[int] = []
    for cmds in pages_cmds:
        stream_bytes = "\n".join(cmds).encode("ascii")
        cid = add_obj(f"<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"\nendstream")
        pid = add_obj(
            f"<< /Type /Page /Parent PAGES_REF /MediaBox [0 0 595 842] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {cid} 0 R >>".encode("ascii")
        )
        page_ids.append(pid)

    kids_str = " ".join(f"{pid} 0 R" for pid in page_ids)
    pages_id = add_obj(f"<< /Type /Pages /Kids [{kids_str}] /Count {len(page_ids)} >>".encode("ascii"))
    catalog_id = add_obj(f"<< /Type /Catalog /Pages {pages_id} 0 R >>".encode("ascii"))

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: list[int] = []
    for i, body in enumerate(objs, 1):
        body_fixed = body.replace(b"PAGES_REF", f"{pages_id} 0 R".encode("ascii"))
        offsets.append(len(out))
        out.extend(f"{i} 0 obj\n".encode("ascii") + body_fixed + b"\nendobj\n")
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(
        f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_id} 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    out_path.write_bytes(bytes(out))
    if out_path.resolve() != (TMP_EXPORT_DIR / fname).resolve():
        shutil.copy2(out_path, TMP_EXPORT_DIR / fname)
    return out_path


def export_documents(
    title: str,
    content: str,
    formats: list[str],
    character: str = "amiya",
) -> list[Path]:
    """按请求的格式列表生成对应文档文件，返回生成的文件路径列表。"""
    generated: list[Path] = []
    seen: set[str] = set()
    for fmt in formats:
        f_low = fmt.lower().lstrip(".")
        if f_low in seen:
            continue
        seen.add(f_low)
        try:
            if f_low in {"md", "markdown"}:
                generated.append(generate_markdown_file(title, content, character))
            elif f_low in {"docx", "word", "doc"}:
                generated.append(generate_docx_file(title, content, character))
            elif f_low == "pdf":
                generated.append(generate_pdf_file(title, content, character))
        except Exception as exc:
            logger.warning(f"生成 {f_low} 文档失败: {exc}")
    return generated


_file_renderer: Callable[[str, str, list[str]], Awaitable[dict[str, bytes]]] | None = None


def configure_file_renderer(renderer: Callable[[str, str, list[str]], Awaitable[dict[str, bytes]]]) -> None:
    global _file_renderer
    if _file_renderer is not None and _file_renderer is not renderer:
        raise RuntimeError("文件渲染依赖只能在启动时注入一次")
    _file_renderer = renderer


async def prepare_documents(title: str, content: str, formats: list[str], character: str = "amiya") -> list[Path]:
    """复用已校验产物，百炼优先制作文件，缺失或无效格式由本地补齐。"""
    from . import conversation_service as conversation

    aliases = {"word": "docx", "doc": "docx", "markdown": "md"}
    requested = list(dict.fromkeys(aliases.get(fmt.lower().lstrip("."), fmt.lower().lstrip(".")) for fmt in formats))
    requested = [fmt for fmt in requested if fmt in {"docx", "pdf", "md"}]
    if not requested:
        return []
    digest = hashlib.sha256(content.encode()).hexdigest()
    session = conversation.current()
    previous = (session.document or {}) if session else {}
    prepared: dict[str, Path] = {}
    provenance: dict[str, str] = {}
    if previous.get("content_sha256") == digest:
        for item in previous.get("files", []):
            try:
                path = Path(item["path"])
                if (
                    item["format"] in requested
                    and path.resolve().parent == EXPORTS_DIR.resolve()
                    and path.is_file()
                    and hashlib.sha256(path.read_bytes()).hexdigest() == item["sha256"]
                ):
                    prepared[item["format"]] = path
                    provenance[item["format"]] = item.get("renderer", "local")
            except (KeyError, OSError, TypeError):
                continue

    missing = [fmt for fmt in requested if fmt not in prepared]
    if missing and _file_renderer is not None:
        try:
            cloud_files = await _file_renderer(title, content, missing)
            EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
            for fmt, raw in cloud_files.items():
                if fmt in missing:
                    path = EXPORTS_DIR / _safe_filename(title, fmt)
                    path.write_bytes(raw)
                    prepared[fmt] = path
                    provenance[fmt] = "bailian"
        except Exception as exc:
            logger.warning(f"Degradation stage=bailian_file_renderer type={type(exc).__name__} fallback=local")
    missing = [fmt for fmt in requested if fmt not in prepared]
    if missing:
        generated_paths = await asyncio.to_thread(export_documents, title, content, missing, character)
        for path in generated_paths:
            fmt_key = path.suffix.lower().lstrip(".")
            prepared[fmt_key] = path
            provenance[fmt_key] = "local"

    if session:
        current_document = session.document or {}
        prompt = current_document.get("prompt", title) if current_document.get("content") == content else title
        document = dict(
            current_document,
            content=content,
            prompt=prompt,
            formats=requested,
            complete=True,
            content_sha256=digest,
        )
        document["files"] = [
            {
                "format": fmt,
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "renderer": provenance.get(fmt, "local"),
            }
            for fmt, path in prepared.items()
            if path.is_file()
        ]
        try:
            conversation.store.checkpoint(session, document)
        except Exception as exc:
            logger.warning(f"保存文档产物信息失败: {type(exc).__name__}")
    return [prepared[fmt] for fmt in requested if fmt in prepared]


async def send_file_to_qq(
    bot: Any,
    event: Any,
    file_path: Path,
    group_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> bool:
    """将生成的文档文件发送至 QQ 群聊或私聊：
    1. 使用纯 ASCII 临时文件名 (/tmp/qq_agent_exports/qq_doc_<ts>.<ext>) 规避 macOS NTQQ 对中文路径解析卡死，同时通过 name=fname 保留中文展示文件名；
    2. 依次尝试 ASCII 路径、base64://、file:// 与 MessageSegment(file)，单种方式超时自动切换下一通道，不提前中断整条投递链。
    """
    from nonebot.adapters.onebot.v11 import MessageSegment

    if not file_path.is_file():
        return False
    if not group_id and not user_id:
        logger.warning("文件缺少 QQ 投递目的地，保留本地产物")
        return False
    fname = file_path.name
    ext = file_path.suffix or ".docx"
    ascii_tmp_name = f"qq_doc_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}{ext}"
    tmp_copy = TMP_EXPORT_DIR / ascii_tmp_name
    try:
        TMP_EXPORT_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, tmp_copy)
        tmp_copy.chmod(0o644)
    except Exception:
        tmp_copy = file_path

    raw_bytes = file_path.read_bytes()
    b64_uri = "base64://" + base64.b64encode(raw_bytes).decode("ascii")

    # 1. 尝试调用 OneBot v11 标准 upload_group_file / upload_private_file（ASCII /tmp 路径 -> base64:// -> file:// -> 原路径）
    candidates = [str(tmp_copy.resolve()), b64_uri, f"file://{tmp_copy.resolve()}", str(file_path.resolve())]
    for idx, file_arg in enumerate(candidates):
        try:
            if group_id:
                await asyncio.wait_for(
                    bot.call_api("upload_group_file", group_id=int(group_id), file=file_arg, name=fname),
                    timeout=25.0,
                )
            elif user_id:
                await asyncio.wait_for(
                    bot.call_api("upload_private_file", user_id=int(user_id), file=file_arg, name=fname),
                    timeout=25.0,
                )
            logger.info(f"成功上传文档至 QQ: {fname} ({len(raw_bytes)} bytes, via=#{idx + 1})")
            return True
        except (asyncio.TimeoutError, ConnectionError) as exc:
            logger.warning(f"文档 {fname} 第 {idx + 1} 种方式上传超时 ({type(exc).__name__})，尝试下一通道...")
            if idx == 0 and b64_uri in candidates:
                continue
            return False
        except Exception as exc:
            logger.debug(f"upload_file ({file_arg[:30]}...) 尝试未通过: {exc}")
            continue

    # 2. 兜底使用 MessageSegment("file", ...) 发送
    if event is not None:
        for seg_file in (str(tmp_copy.resolve()), b64_uri):
            try:
                seg = MessageSegment("file", {"file": seg_file, "name": fname})
                await asyncio.wait_for(bot.send(event, seg), timeout=25.0)
                logger.info(f"通过 MessageSegment(file) 成功发送文档: {fname}")
                return True
            except Exception as exc:
                logger.warning(f"MessageSegment 发送文档 {fname} 尝试失败: {exc}")
    return False


def list_exported_files(limit: int = 25) -> list[dict[str, Any]]:
    """列出最近导出的文档供 Web 控制台下载。"""
    if not EXPORTS_DIR.is_dir():
        return []
    files = sorted(
        [p for p in EXPORTS_DIR.iterdir() if p.is_file() and p.suffix.lower() in {".pdf", ".docx", ".md"}],
        key=lambda x: x.stat().st_mtime,
        reverse=True,
    )
    out = []
    for p in files[:limit]:
        st = p.stat()
        out.append(
            {
                "name": p.name,
                "ext": p.suffix.lower().lstrip("."),
                "size_kb": round(st.st_size / 1024.0, 1),
                "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%m-%d %H:%M:%S"),
                "url": f"/api/exports/{p.name}",
            }
        )
    return out
