import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from dialos import extract

BLANK_PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R 4 0 R]/Count 2>>endobj\n"
             b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n4 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
             b"trailer<</Root 1 0 R/Size 5>>\n%%EOF\n")
WORDS = " ".join(f"word{i}" for i in range(40))


def fake_indra(directory, body):
    """A stand-in indra-ocr: writes <image stem>.md into --out for every input image."""
    path = Path(directory) / "indra-ocr"
    path.write_text("#!/bin/sh\nout=''\nimgs=''\nwhile [ $# -gt 0 ]; do\n"
                    "  case \"$1\" in --out) out=\"$2\"; shift 2;; --cores) shift 2;; *) imgs=\"$imgs $1\"; shift;; esac\n"
                    "done\nmkdir -p \"$out\"\nfor i in $imgs; do b=$(basename \"$i\" .png)\n" + body + "done\n")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return str(path)


@unittest.skipUnless(shutil.which("pdftotext") and shutil.which("pdftoppm"), "needs poppler")
class IndraOcr(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.td, True)

    def run_pdf(self, body, tesseract=None):
        exe = fake_indra(self.td, body)
        patches = [mock.patch.dict(os.environ, {"DIALOS_INDRA_BIN": exe})]
        if tesseract is not None:
            patches.append(mock.patch.object(extract, "_tesseract", lambda img: tesseract))
        else:
            patches.append(mock.patch.object(extract, "_tesseract", lambda img: ""))
        for p in patches:
            p.start(); self.addCleanup(p.stop)
        return extract.extract("scan.pdf", BLANK_PDF)

    def test_capabilities_reports_indra(self):
        exe = fake_indra(self.td, "")
        with mock.patch.dict(os.environ, {"DIALOS_INDRA_BIN": exe}):
            self.assertTrue(extract.capabilities()["indra"])
        with mock.patch.dict(os.environ, {"DIALOS_INDRA_BIN": ""}), mock.patch("shutil.which", return_value=None):
            self.assertFalse(extract.capabilities()["indra"])
            self.assertFalse(extract.capabilities()["ocr"])

    def test_scanned_pages_read_with_indra(self):
        text, notes = self.run_pdf(f'echo "{WORDS}" > "$out/$b.md"\n')
        self.assertEqual(text.count("word39"), 2)
        self.assertTrue(any("2 scanned page(s) were read with OCR (indra)" in n for n in notes), notes)

    def test_looping_output_falls_back_to_tesseract(self):
        loop = " ".join(["ගල්ල"] * 200)
        text, notes = self.run_pdf(f'echo "{loop}" > "$out/$b.md"\n', tesseract=WORDS)
        self.assertNotIn("ගල්ල", text)
        self.assertIn("word39", text)
        self.assertTrue(any("(tesseract)" in n for n in notes), notes)

    def test_unreadable_pages_are_reported_not_dropped(self):
        text, notes = self.run_pdf("")          # indra produces nothing, tesseract empty
        self.assertIn("--- page 2 ---", text)
        self.assertTrue(any("2 scanned page(s) could not be read reliably" in n for n in notes), notes)

    def test_page_cap_still_applies(self):
        with mock.patch.object(extract, "OCR_MAX_PAGES", 1):
            _, notes = self.run_pdf(f'echo "{WORDS}" > "$out/$b.md"\n')
        self.assertTrue(any("1 more scanned pages were not OCR'd (limit 1)" in n for n in notes), notes)

    def test_one_process_for_the_whole_batch(self):
        count = Path(self.td) / "calls"
        body = f'echo "{WORDS}" > "$out/$b.md"\n'
        exe = fake_indra(self.td, body)
        Path(exe).write_text(Path(exe).read_text().replace("mkdir -p", f"echo x >> {count}\nmkdir -p", 1))
        with mock.patch.dict(os.environ, {"DIALOS_INDRA_BIN": exe}):
            extract.extract("scan.pdf", BLANK_PDF)
        self.assertEqual(len(count.read_text().split()), 1)


class Looping(unittest.TestCase):
    def test_detects_repetition_only_on_long_pages(self):
        self.assertTrue(extract._looping(" ".join(["a"] * 100)))
        self.assertFalse(extract._looping(" ".join(["a"] * 20)))
        self.assertFalse(extract._looping(" ".join(f"w{i}" for i in range(100))))
        self.assertFalse(extract._looping("Привет мир " * 5))      # non-Latin text is not judged by script


if __name__ == "__main__":
    unittest.main()
