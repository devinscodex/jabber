"""Document text extraction.

Pure-stdlib readers for text, DOCX, XLSX and PPTX; optional external tools for
the rest, detected at runtime:
  pdftotext / pdftoppm (poppler)  PDF text, and page images for OCR
  tesseract                       OCR for scanned PDF pages
  soffice (LibreOffice)           legacy .doc/.xls/.ppt, RTF and OpenDocument

extract() returns (text, notes). Notes say plainly what could not be read.
"""
import io
import os
import posixpath
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

MAX_PART_BYTES = 200 * 1024 * 1024   # decompressed size cap per XML part
MAX_SHEETS = 60
MAX_SLIDES = 2000
PDF_MAX_PAGES = 2000
OCR_MAX_PAGES = 40
OCR_PAGE_TIMEOUT = 60
LO_TIMEOUT = 180
MIN_PAGE_CHARS = 10
MIN_ALNUM_RATIO = 0.3
INDRA_BATCH = 20        # pages per indra-ocr process
INDRA_CORES = 4         # CPU cores indra may use, so uploads do not starve the chat backend
LOOP_MIN_WORDS = 60     # a page needs this many words before repetition is judged
LOOP_UNIQUE_MIN = 0.12  # unique-word share below this means a looping recognizer

TEXT_EXTS = {".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonl", ".xml", ".html", ".htm", ".yaml", ".yml",
             ".toml", ".ini", ".cfg", ".conf", ".log", ".py", ".js", ".ts", ".tsx", ".jsx", ".c", ".h", ".cpp", ".hpp",
             ".cs", ".java", ".go", ".rs", ".rb", ".php", ".sh", ".bash", ".ps1", ".psm1", ".bat", ".sql", ".css",
             ".scss", ".lua", ".r", ".swift", ".kt", ".tex", ".rst", ".eml", ".srt", ".vtt", ".diff", ".patch", ".env",
             ".gitignore", ".dockerfile", ".makefile", ".tf", ".bicep", ".gradle", ".properties"}
OOXML_EXTS = {".docx", ".xlsx", ".pptx", ".epub"}
LEGACY_EXTS = {".doc", ".xls", ".ppt", ".rtf", ".odt", ".ods", ".odp"}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_ocr_sem = threading.Semaphore(2)
_lo_sem = threading.Semaphore(1)


def _indra_bin():
    """Path of the optional indra-ocr tool (JABBER_INDRA_BIN overrides PATH), or None."""
    return os.environ.get("JABBER_INDRA_BIN") or shutil.which("indra-ocr")


def capabilities() -> dict:
    return {"pdf": bool(shutil.which("pdftotext")),
            "ocr": bool((shutil.which("tesseract") or _indra_bin()) and shutil.which("pdftoppm")),
            "indra": bool(_indra_bin()),
            "legacy_office": bool(shutil.which("soffice") or shutil.which("libreoffice"))}


def kind_of(name: str) -> str:
    ext = Path(name).suffix.lower()
    base = Path(name).name.lower()
    if ext in IMAGE_EXTS:
        return "image"
    if ext == ".pdf":
        return "pdf"
    if ext in OOXML_EXTS:
        return "ooxml"
    if ext in LEGACY_EXTS:
        return "legacy"
    if ext in TEXT_EXTS or base in ("makefile", "dockerfile", "readme", "license"):
        return "text"
    return "unknown"


def _read_part(z: zipfile.ZipFile, name: str) -> bytes:
    info = z.getinfo(name)
    if info.file_size > MAX_PART_BYTES:
        raise ValueError(f"{name} is {info.file_size:,} bytes unpacked, over the safety cap")
    return z.read(name)


def _t(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ---------------------------------------------------------------- DOCX
def extract_docx(raw: bytes):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        root = ET.fromstring(_read_part(z, "word/document.xml"))

    def para(p):
        out = []
        for el in p.iter():
            n = _t(el.tag)
            if n == "t" and el.text:
                out.append(el.text)
            elif n in ("tab",):
                out.append("\t")
            elif n in ("br", "cr"):
                out.append("\n")
        return "".join(out)

    body = next((c for c in root if _t(c.tag) == "body"), root)
    lines = []
    for child in body:
        n = _t(child.tag)
        if n == "p":
            lines.append(para(child))
        elif n == "tbl":
            for row in child.iter():
                if _t(row.tag) == "tr":
                    cells = [" ".join(para(p) for p in c if _t(p.tag) == "p") for c in row if _t(c.tag) == "tc"]
                    lines.append(" | ".join(x.strip() for x in cells))
            lines.append("")
    return "\n".join(lines).strip() + "\n", ["headers, footers, comments and text boxes are not included"]


# ---------------------------------------------------------------- XLSX
def _col_index(ref: str) -> int:
    n = 0
    for ch in re.match(r"[A-Z]+", ref).group(0):
        n = n * 26 + ord(ch) - 64
    return n - 1


def extract_xlsx(raw: bytes):
    notes = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = set(z.namelist())
        shared = []
        if "xl/sharedStrings.xml" in names:
            for si in ET.fromstring(_read_part(z, "xl/sharedStrings.xml")):
                shared.append("".join(t.text or "" for t in si.iter() if _t(t.tag) == "t"))
        wb = ET.fromstring(_read_part(z, "xl/workbook.xml"))
        rels = {}
        if "xl/_rels/workbook.xml.rels" in names:
            for r in ET.fromstring(_read_part(z, "xl/_rels/workbook.xml.rels")):
                rels[r.get("Id")] = r.get("Target")
        sheets = []
        for s in wb.iter():
            if _t(s.tag) == "sheet":
                rid = next((v for k, v in s.attrib.items() if k.endswith("}id")), None)
                target = rels.get(rid, "")
                target = target.lstrip("/")
                path = target if target.startswith("xl/") else "xl/" + target
                sheets.append((s.get("name", "Sheet"), path))
        if len(sheets) > MAX_SHEETS:
            notes.append(f"only the first {MAX_SHEETS} of {len(sheets)} sheets were read")
            sheets = sheets[:MAX_SHEETS]
        out = []
        for name, path in sheets:
            if path not in names:
                notes.append(f"sheet {name!r} could not be found in the file")
                continue
            out.append(f"## Sheet: {name}")
            for row in ET.fromstring(_read_part(z, path)).iter():
                if _t(row.tag) != "row":
                    continue
                cells = {}
                for c in row:
                    if _t(c.tag) != "c":
                        continue
                    t = c.get("t")
                    v = next((x.text for x in c if _t(x.tag) == "v"), None)
                    if t == "s" and v is not None:
                        try:
                            v = shared[int(v)]
                        except (ValueError, IndexError):
                            v = ""
                    elif t == "inlineStr":
                        v = "".join(x.text or "" for x in c.iter() if _t(x.tag) == "t")
                    elif t == "b":
                        v = "TRUE" if v == "1" else "FALSE"
                    if v not in (None, ""):
                        cells[_col_index(c.get("r", "A1"))] = str(v).replace("\t", " ").replace("\n", " ")
                if cells:
                    out.append("\t".join(cells.get(i, "") for i in range(max(cells) + 1)))
            out.append("")
    notes.append("dates appear as serial numbers and formulas as their last computed value")
    return "\n".join(out), notes


# ---------------------------------------------------------------- PPTX
def extract_pptx(raw: bytes):
    out = []
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        slides = sorted((n for n in z.namelist() if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))[:MAX_SLIDES]
        for i, n in enumerate(slides, 1):
            out.append(f"## Slide {i}")
            for p in ET.fromstring(_read_part(z, n)).iter():
                if _t(p.tag) == "p":
                    text = "".join(t.text or "" for t in p.iter() if _t(t.tag) == "t").strip()
                    if text:
                        out.append(text)
            note = f"ppt/notesSlides/notesSlide{re.findall(r'[0-9]+', n)[-1]}.xml"
            if note in z.namelist():
                nt = [" ".join(t.text or "" for t in p.iter() if _t(t.tag) == "t").strip()
                      for p in ET.fromstring(_read_part(z, note)).iter() if _t(p.tag) == "p"]
                nt = [x for x in nt if x and not x.isdigit()]
                if nt:
                    out.append("Notes: " + " ".join(nt))
            out.append("")
    return "\n".join(out), ["pictures and charts are not described"]


# ---------------------------------------------------------------- EPUB
class _Text(HTMLParser):
    BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "section", "pre"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out, self.skip, self.title = [], 0, ""

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style", "head"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style", "head"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def extract_epub(raw: bytes):
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        names = set(z.namelist())
        if "META-INF/container.xml" not in names:
            raise ValueError("not a valid EPUB (no container.xml)")
        cont = ET.fromstring(_read_part(z, "META-INF/container.xml"))
        opf_path = next((e.get("full-path") for e in cont.iter() if _t(e.tag) == "rootfile"), None)
        if not opf_path or opf_path not in names:
            raise ValueError("not a valid EPUB (package file missing)")
        opf = ET.fromstring(_read_part(z, opf_path))
        base = posixpath.dirname(opf_path)
        manifest = {e.get("id"): e.get("href") for e in opf.iter() if _t(e.tag) == "item"}
        title = next(((e.text or "").strip() for e in opf.iter() if _t(e.tag) == "title"), "")
        order = [e.get("idref") for e in opf.iter() if _t(e.tag) == "itemref"]
        if not order:
            order = list(manifest)
        out, notes = ([f"# {title}", ""] if title else []), []
        chapters = 0
        for idref in order:
            href = manifest.get(idref)
            if not href or not href.lower().split("#")[0].endswith((".xhtml", ".html", ".htm", ".xml")):
                continue
            path = posixpath.normpath(posixpath.join(base, href.split("#")[0].replace("%20", " ")))
            if path not in names:
                continue
            parser = _Text()
            parser.feed(_read_part(z, path).decode("utf-8", "replace"))
            text = re.sub(r"\n\s*\n+", "\n\n", "".join(parser.out)).strip()
            if text:
                chapters += 1
                out.append(f"## Section {chapters}\n{text}\n")
        if not chapters:
            notes.append("no readable text was found (it may be image-based or DRM-protected)")
    return "\n".join(out), notes + ["images are not described"]


# ---------------------------------------------------------------- PDF
def _page_thin(text: str) -> bool:
    s = text.strip()
    if len(s) < MIN_PAGE_CHARS:
        return True
    return sum(c.isalnum() for c in s) / len(s) < MIN_ALNUM_RATIO


def _run(cmd, timeout, input_bytes=None):
    return subprocess.run(cmd, input=input_bytes, capture_output=True, timeout=timeout, check=False)


def extract_pdf(raw: bytes):
    if not shutil.which("pdftotext"):
        raise ValueError("PDF text extraction needs pdftotext (poppler-utils), which is not installed")
    notes = []
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "in.pdf"
        src.write_bytes(raw)
        full = _run(["pdftotext", "-layout", str(src), "-"], 300)
        if full.returncode != 0:
            raise ValueError("pdftotext could not read this PDF: " + full.stderr.decode("utf-8", "replace")[:200])
        pages = full.stdout.decode("utf-8", "replace").split("\f")
        if pages and not pages[-1].strip():
            pages.pop()
        if len(pages) > PDF_MAX_PAGES:
            notes.append(f"only the first {PDF_MAX_PAGES} of {len(pages)} pages were read")
            pages = pages[:PDF_MAX_PAGES]
        can_ocr = capabilities()["ocr"]
        thin = [i for i, p in enumerate(pages) if _page_thin(p)]
        ocr_done = 0
        if thin and can_ocr:
            todo = thin[:OCR_MAX_PAGES]
            texts, engines = _ocr_pages(src, [i + 1 for i in todo], td)
            for i in todo:
                text = texts.get(i + 1, "")
                if text and not _page_thin(text) and not _looping(text):
                    pages[i] = text
                    ocr_done += 1
            if len(thin) > OCR_MAX_PAGES:
                notes.append(f"{len(thin) - OCR_MAX_PAGES} more scanned pages were not OCR'd (limit {OCR_MAX_PAGES})")
            if len(todo) > ocr_done:
                notes.append(f"{len(todo) - ocr_done} scanned page(s) could not be read reliably")
            if ocr_done:
                used = " and ".join(sorted(set(engines.values()) - {""})) or "OCR"
                notes.append(f"{ocr_done} scanned page(s) were read with OCR ({used}) and may contain errors")
        elif thin:
            notes.append(f"{len(thin)} page(s) look scanned or image-only; install tesseract or indra-ocr to read them")
    return "\n".join(f"--- page {i} ---\n{p.strip()}\n" for i, p in enumerate(pages, 1)), notes


def _looping(text: str) -> bool:
    """OCR output that is mostly one repeated word is a recognizer failure, not content."""
    w = text.split()
    return len(w) > LOOP_MIN_WORDS and len(set(w)) / len(w) < LOOP_UNIQUE_MIN


def _rasterize(pdf: Path, page: int, td: str):
    prefix = str(Path(td) / f"p{page}")
    r = _run(["pdftoppm", "-f", str(page), "-l", str(page), "-r", "200", "-png", str(pdf), prefix], OCR_PAGE_TIMEOUT)
    imgs = sorted(Path(td).glob(f"p{page}*.png"))
    return imgs[0] if r.returncode == 0 and imgs else None


def _tesseract(img: Path) -> str:
    t = _run(["tesseract", str(img), "stdout"], OCR_PAGE_TIMEOUT)
    return t.stdout.decode("utf-8", "replace") if t.returncode == 0 else ""


def _indra(imgs):
    """{image path: text} for a batch of page images; one process per batch (the model loads once)."""
    out, res = tempfile.mkdtemp(), {}
    try:
        for k in range(0, len(imgs), INDRA_BATCH):
            chunk = imgs[k:k + INDRA_BATCH]
            _run([_indra_bin(), *map(str, chunk), "--out", out, "--cores", str(INDRA_CORES)],
                 min(OCR_PAGE_TIMEOUT * len(chunk), 30 + 5 * len(chunk)))
            for p in chunk:
                f = Path(out) / (p.stem + ".md")
                res[p] = f.read_text("utf-8", "replace") if f.exists() else ""
    except subprocess.TimeoutExpired:
        pass
    finally:
        shutil.rmtree(out, ignore_errors=True)
    return res


def _ocr_pages(pdf: Path, pages, td: str):
    """({page: text}, {page: engine}). Indra first when installed, tesseract for pages it fails on."""
    texts, engines = {}, {}
    with _ocr_sem:
        try:
            imgs = {p: _rasterize(pdf, p, td) for p in pages}
            have = {p: i for p, i in imgs.items() if i}
            if _indra_bin():
                got = _indra(list(have.values()))
                for p, img in have.items():
                    t = got.get(img, "")
                    if t and not _page_thin(t) and not _looping(t):
                        texts[p], engines[p] = t, "indra"
            if shutil.which("tesseract"):
                for p, img in have.items():
                    if p not in texts:
                        t = _tesseract(img)
                        if t and not _page_thin(t) and not _looping(t):
                            texts[p], engines[p] = t, "tesseract"
        except subprocess.TimeoutExpired:
            pass
        finally:
            for f in Path(td).glob("p*.png"):
                f.unlink(missing_ok=True)
    return texts, engines


# ---------------------------------------------------------------- legacy via LibreOffice
def extract_legacy(name: str, raw: bytes):
    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        raise ValueError(f"{Path(name).suffix} files need LibreOffice, which is not installed on this server")
    ext = Path(name).suffix.lower()
    target = {".doc": "docx", ".rtf": "docx", ".odt": "docx", ".xls": "xlsx", ".ods": "xlsx", ".ppt": "pptx", ".odp": "pptx"}[ext]
    with _lo_sem, tempfile.TemporaryDirectory() as td:
        src = Path(td) / ("in" + ext)
        src.write_bytes(raw)
        profile = Path(td) / "profile"
        r = _run([exe, f"-env:UserInstallation=file://{profile}", "--headless", "--convert-to", target,
                  "--outdir", td, str(src)], LO_TIMEOUT)
        out = Path(td) / ("in." + target)
        if r.returncode != 0 or not out.exists():
            raise ValueError("LibreOffice could not convert this file")
        data = out.read_bytes()
    return {"docx": extract_docx, "xlsx": extract_xlsx, "pptx": extract_pptx}[target](data)


# ---------------------------------------------------------------- entry
def extract(name: str, raw: bytes):
    """(text, notes). Raises ValueError with a readable reason on failure."""
    kind, ext = kind_of(name), Path(name).suffix.lower()
    try:
        if kind == "text":
            if b"\x00" in raw[:8192]:
                raise ValueError("this looks like a binary file, not text")
            return raw.decode("utf-8", "replace"), []
        if kind == "ooxml":
            return {".docx": extract_docx, ".xlsx": extract_xlsx, ".pptx": extract_pptx, ".epub": extract_epub}[ext](raw)
        if kind == "pdf":
            return extract_pdf(raw)
        if kind == "legacy":
            return extract_legacy(name, raw)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise ValueError(f"the file is damaged or not a real {ext} document ({type(e).__name__})")
    except subprocess.TimeoutExpired:
        raise ValueError("reading this file took too long")
    raise ValueError(f"{('%s files' % ext) if ext else 'this kind of file'} cannot be read")
