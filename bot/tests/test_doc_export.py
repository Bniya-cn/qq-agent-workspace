"""教案 docx / pdf 导出。直接加载 doc_service，避免拉起 NoneBot 插件。"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from xml.etree import ElementTree as ET

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

FIXTURE = """\
1. **正数的定义**：大于 0 的数，如 \\(\\frac{1}{2}\\)。
------
| 知识点 | 说明 |
| --- | --- |
| 数轴 | 三要素 |

```markdown
| 题型 | 分值 |
| --- | --- |
| 选择 | 5 |
```

\\[
|a| = \\begin{cases}
a, & a > 0 \\\\
0, & a = 0 \\\\
-a, & a < 0
\\end{cases}
\\]

```latex
\\begin{tikzpicture}[scale=1.2]
\\draw (-3,0) -- (3,0);
\\end{tikzpicture}
```

- 数轴上的点
"""


def _load_doc_service():
    path = Path(__file__).resolve().parents[1] / "src/plugins/agent_core/doc_service.py"
    spec = importlib.util.spec_from_file_location("doc_service_under_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


doc = _load_doc_service()


def _document_root(path: Path) -> ET.Element:
    with zipfile.ZipFile(path) as archive:
        return ET.fromstring(archive.read("word/document.xml"))


def _paragraph_text(root: ET.Element) -> str:
    parts: list[str] = []
    for paragraph in root.findall(f".//{W}p"):
        parts.append("".join(node.text or "" for node in paragraph.findall(f".//{W}t")))
    return "\n".join(parts)


def _table_text(root: ET.Element) -> str:
    parts: list[str] = []
    for table in root.findall(f".//{W}tbl"):
        parts.append("".join(node.text or "" for node in table.findall(f".//{W}t")))
    return "\n".join(parts)


class DocExportTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._exports = doc.EXPORTS_DIR
        self._tmp_exports = doc.TMP_EXPORT_DIR
        doc.EXPORTS_DIR = root / "exports"
        doc.TMP_EXPORT_DIR = root / "tmp"

    def tearDown(self) -> None:
        doc.EXPORTS_DIR = self._exports
        doc.TMP_EXPORT_DIR = self._tmp_exports
        self._tmp.cleanup()

    def test_prepare_unwraps_only_table_fences(self) -> None:
        prepared = doc.prepare_markdown(FIXTURE)
        self.assertNotIn("```markdown", prepared)
        self.assertIn("```latex", prepared)
        self.assertIn("| --- | --- |", prepared)
        self.assertNotIn("------", prepared)
        self.assertIn("tikzpicture", prepared)

    def test_markdown_file_keeps_source(self) -> None:
        path = doc.generate_markdown_file("有理数", FIXTURE, "amiya")
        text = path.read_text(encoding="utf-8")
        self.assertIn("```markdown", text)
        self.assertIn("```latex", text)
        self.assertIn("------", text)
        self.assertIn("**正数的定义**", text)

    def test_fallback_docx_repairs_table_and_bold(self) -> None:
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent-pandoc-path"}):
            path = doc.generate_docx_file("有理数", FIXTURE, "amiya")
        root = _document_root(path)
        text = _paragraph_text(root)
        tables = _table_text(root)
        self.assertNotIn("oMath", ET.tostring(root, encoding="unicode"))
        self.assertGreaterEqual(len(root.findall(f".//{W}tbl")), 2)
        self.assertIn("正数的定义", text)
        self.assertNotIn("**", text)
        self.assertIn("三要素", tables)
        self.assertIn("选择", tables)
        self.assertNotIn("tikzpicture", tables)
        self.assertIn("tikzpicture", text)
        self.assertNotIn("------", text)
        self.assertIn("• 数轴上的点", text)

    def test_pdf_strips_bold_markers(self) -> None:
        path = doc.generate_pdf_file("有理数", "这是**加粗**内容\n------\n| 知识点 | 说明 |\n| --- | --- |\n", "amiya")
        data = path.read_bytes()

        def encoded(text: str) -> bytes:
            # PDF 正文把字符写成 UTF-16-BE 的十六进制，不是原始字节。
            return text.encode("utf-16-be").hex().upper().encode("ascii")

        self.assertNotIn(encoded("\\1"), data)
        self.assertIn(encoded("加粗"), data)
        self.assertNotIn(encoded("**"), data)
        self.assertNotIn(encoded("------"), data)
        self.assertIn(encoded("知识点"), data)

    def _with_dummy_reference(self):
        dummy = Path(self._tmp.name) / "reference.docx"
        dummy.write_bytes(b"reference")
        return mock.patch.object(doc, "REFERENCE_DOCX", dummy)

    def test_invalid_pandoc_output_falls_back(self) -> None:
        def fake_run(command, input, capture_output, timeout, check):  # noqa: A002
            Path(command[command.index("-o") + 1]).write_bytes(b"not-a-docx")
            return subprocess.CompletedProcess(command, 0, b"", b"bad")

        with self._with_dummy_reference(), mock.patch.object(doc.shutil, "which", return_value="/usr/bin/pandoc"), mock.patch.object(
            doc.subprocess, "run", side_effect=fake_run
        ):
            path = doc.generate_docx_file("有理数", FIXTURE, "amiya")
        self.assertTrue(zipfile.is_zipfile(path))
        root = _document_root(path)
        self.assertNotIn("oMath", ET.tostring(root, encoding="unicode"))
        self.assertGreaterEqual(len(root.findall(f".//{W}tbl")), 2)

    def test_pandoc_timeout_falls_back(self) -> None:
        def fake_run(command, input, capture_output, timeout, check):  # noqa: A002
            Path(command[command.index("-o") + 1]).write_bytes(b"partial")
            raise subprocess.TimeoutExpired(command, timeout)

        with self._with_dummy_reference(), mock.patch.object(doc.shutil, "which", return_value="/usr/bin/pandoc"), mock.patch.object(
            doc.subprocess, "run", side_effect=fake_run
        ):
            path = doc.generate_docx_file("有理数", FIXTURE, "amiya")
        root = _document_root(path)
        self.assertIn("正数的定义", _paragraph_text(root))
        self.assertNotIn("oMath", ET.tostring(root, encoding="unicode"))

    def test_pandoc_writes_omml(self) -> None:
        if shutil.which("pandoc") is None or not doc.REFERENCE_DOCX.is_file():
            self.skipTest("本机没有可用的 pandoc 或参考文档")
        path = doc.generate_docx_file("有理数", FIXTURE, "amiya")
        root = _document_root(path)
        xml = ET.tostring(root, encoding="unicode")
        text = _paragraph_text(root)
        self.assertIn("oMath", xml)
        self.assertNotIn("**", text)
        self.assertIn("tikzpicture", text)
        self.assertNotIn("tikzpicture", _table_text(root))
        self.assertNotIn("w:drawing", xml)


if __name__ == "__main__":
    unittest.main()
