import io
import json
import shutil
import threading
import unittest
import zipfile

from jabber import auth_local, extract, files
from jabber.backends import Chunk
from tests.test_api import events
from tests.test_server import Client, FakeBackend, multi_cfg, start, stop


def make_docx(paragraphs, table=None):
    ns = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{c}</w:t></w:r></w:p></w:tc>" for c in r) + "</w:tr>" for r in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("word/document.xml", f'<w:document {ns}><w:body>{body}</w:body></w:document>')
    return b.getvalue()


def make_xlsx():
    m = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        z.writestr("xl/workbook.xml", f'<workbook {m} xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                   '<sheets><sheet name="Budget" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                   '<Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst {m}><si><t>Item</t></si><si><t>Pumps</t></si></sst>')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet {m}><sheetData>'
                   '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="C1"><v>99</v></c></row>'
                   '<row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>1250.5</v></c></row></sheetData></worksheet>')
    return b.getvalue()


def make_pptx():
    a = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
    b = io.BytesIO()
    with zipfile.ZipFile(b, "w") as z:
        for i, t in ((1, "Kickoff plan"), (2, "Risks")):
            z.writestr(f"ppt/slides/slide{i}.xml", f'<p:sld xmlns:p="x" {a}><a:p><a:r><a:t>{t}</a:t></a:r></a:p></p:sld>')
    return b.getvalue()


def make_pdf(text):
    stream = f"BT /F1 24 Tf 72 700 Td ({text}) Tj ET"
    return (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj\n"
            + f"4 0 obj<</Length {len(stream)}>>stream\n{stream}\nendstream endobj\n".encode()
            + b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj\ntrailer<</Root 1 0 R/Size 6>>\n%%EOF\n")


PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x02\x00\x00\x00\x03\x08\x02\x00\x00\x00" + b"\x00" * 20)


class ExtractTests(unittest.TestCase):
    def test_docx_with_table(self):
        text, _ = extract.extract("a.docx", make_docx(["Hello report", "Second"], [["Name", "Qty"], ["Valve", "7"]]))
        for n in ("Hello report", "Second", "Name | Qty", "Valve | 7"):
            self.assertIn(n, text)

    def test_xlsx_sparse_cells_and_shared_strings(self):
        text, _ = extract.extract("b.xlsx", make_xlsx())
        self.assertIn("## Sheet: Budget", text)
        self.assertIn("Item\t\t99", text)
        self.assertIn("Pumps\t1250.5", text)

    def test_epub_in_reading_order(self):
        b = io.BytesIO()
        with zipfile.ZipFile(b, "w") as z:
            z.writestr("META-INF/container.xml", '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
                       '<rootfile full-path="OEBPS/content.opf"/></rootfiles></container>')
            z.writestr("OEBPS/content.opf", '<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
                       '<metadata><dc:title>Pump Manual</dc:title></metadata><manifest>'
                       '<item id="a" href="ch1.xhtml"/><item id="b" href="text/ch2.xhtml"/><item id="c" href="cover.jpg"/></manifest>'
                       '<spine><itemref idref="b"/><itemref idref="a"/></spine></package>')
            z.writestr("OEBPS/ch1.xhtml", "<html><head><title>x</title></head><body><h1>One</h1><p>First chapter &amp; text</p></body></html>")
            z.writestr("OEBPS/text/ch2.xhtml", "<html><body><p>Second chapter</p><script>bad()</script></body></html>")
        text, _ = extract.extract("book.epub", b.getvalue())
        self.assertIn("# Pump Manual", text)
        self.assertLess(text.index("Second chapter"), text.index("First chapter & text"))
        self.assertNotIn("bad()", text)
        with self.assertRaisesRegex(ValueError, "damaged|valid EPUB"):
            extract.extract("x.epub", b"junk")

    def test_pptx(self):
        text, _ = extract.extract("c.pptx", make_pptx())
        self.assertIn("## Slide 1\nKickoff plan", text)
        self.assertIn("## Slide 2\nRisks", text)

    @unittest.skipUnless(shutil.which("pdftotext"), "needs pdftotext")
    def test_pdf_text(self):
        text, _ = extract.extract("d.pdf", make_pdf("Quarterly revenue was 4200"))
        self.assertIn("Quarterly revenue was 4200", text)

    def test_damaged_and_unsupported_are_clear(self):
        with self.assertRaisesRegex(ValueError, "damaged"):
            extract.extract("x.docx", b"not a zip")
        with self.assertRaisesRegex(ValueError, "cannot be read"):
            extract.extract("x.exe", b"MZ")

    def test_zip_bomb_part_refused(self):
        old = extract.MAX_PART_BYTES
        extract.MAX_PART_BYTES = 100
        try:
            with self.assertRaisesRegex(ValueError, "safety cap"):
                extract.extract("a.docx", make_docx(["x" * 1000]))
        finally:
            extract.MAX_PART_BYTES = old

    def test_binary_text_refused(self):
        with self.assertRaisesRegex(ValueError, "binary"):
            extract.extract("a.txt", b"abc\x00def")

    def test_image_validation(self):
        self.assertEqual(files.validate_image(PNG), (2, 3))
        with self.assertRaises(ValueError):
            files.validate_image(b"GIF89a" + b"\xff\xff\xff\xff")  # 65535x65535
        with self.assertRaises(ValueError):
            files.validate_image(b"hello")


class Scripted(FakeBackend):
    """First answer asks for tools; once tool results are present, answers using them."""
    def __init__(self, script):
        super().__init__()
        self.script = script
        self.offered = []

    def chat_stream(self, model, messages, tools=None, options=None, think=False):
        self.calls.append((model, messages))
        self.offered.append(bool(tools))
        n_tool = sum(1 for m in messages if m["role"] == "tool")
        if tools and n_tool < len(self.script):
            yield Chunk(tool_calls=[self.script[n_tool]])
            yield Chunk(done=True, stats={"prompt_tokens": 10, "completion_tokens": 5, "eval_ms": 100})
            return
        last = [m["content"] for m in messages if m["role"] == "tool"]
        yield Chunk("Answer from: " + " || ".join(last)[:300])
        yield Chunk(done=True, stats={"prompt_tokens": 20, "completion_tokens": 7, "eval_ms": 100})


class FileApiTests(unittest.TestCase):
    def setUp(self):
        auth_local.SCRYPT["n"] = 2**10
        self.fb = Scripted([])
        self.cfg = multi_cfg(default_model="m1", user_quota_bytes=3 * 1024 * 1024, max_upload_bytes=2 * 1024 * 1024)
        self.srv, self.app = start(self.cfg, self.fb)
        self.addCleanup(stop, self.srv)
        self.app.local.create_user("alice", "correct horse battery")
        self.app.local.create_user("bob", "correct horse battery")
        self.c = self.login("alice")
        self.pid = self.c.json("POST", "/api/projects", {"name": "P"})[1]["id"]

    def login(self, u):
        c = Client(self.srv.server_port)
        c.json("POST", "/api/login", {"username": u, "password": "correct horse battery"})
        return c

    def upload(self, name, data, c=None, pid=None, chunk=1000):
        c, pid = c or self.c, pid or self.pid
        s, r = c.json("POST", f"/api/projects/{pid}/upload/begin", {"path": name, "size": len(data)})
        if s != 200:
            return s, r
        uid, off = r["upload_id"], 0
        while off < len(data) or off == 0:
            piece = data[off:off + chunk]
            s, r, t = c.req("POST", f"/api/uploads/{uid}/chunk?offset={off}", raw=piece, headers={"Content-Type": "application/octet-stream"})
            if s != 200:
                return s, json.loads(t)
            off += len(piece)
            if not data:
                break
        return c.json("POST", f"/api/uploads/{uid}/finish", {})

    def test_chunked_text_upload_list_read(self):
        data = ("line one\nrevenue 4200\n" * 300).encode()
        s, r = self.upload("notes/q1.txt", data, chunk=777)
        self.assertEqual((s, r["written_paths"]), (200, ["notes/q1.txt"]))
        s, r = self.c.json("GET", f"/api/projects/{self.pid}/files?path=.")
        self.assertEqual(r["entries"], ["notes/"])
        s, r = self.c.json("GET", f"/api/projects/{self.pid}/file?path=notes/q1.txt")
        self.assertTrue(r["content"].startswith("line one"))
        self.assertTrue(self.c.json("GET", f"/api/projects/{self.pid}")[1]["has_files"])

    def test_office_docs_become_extracted_text(self):
        s, r = self.upload("Report.docx", make_docx(["Findings: pump failure at 3am"]))
        self.assertEqual(r["written_paths"], ["Report.docx.extracted.txt"])
        s, r = self.upload("Budget.xlsx", make_xlsx())
        self.assertEqual(s, 200)
        s, r = self.c.json("GET", f"/api/projects/{self.pid}/file?path=Report.docx.extracted.txt")
        self.assertIn("pump failure", r["content"])

    def test_bad_inputs_refused_with_reasons(self):
        for name, data, code in (("../escape.txt", b"x", 400), ("/etc/passwd", b"x", 415), (".hidden", b"x", 400),
                                 ("tool.exe", b"MZ", 415), ("bin.txt", b"a\x00b", 415), ("fake.png", b"nope", 415),
                                 ("broken.docx", b"not a zip", 422)):
            s, r = self.upload(name, data)
            self.assertEqual(s, code, (name, s, r))
            self.assertIn("error", r)
        s, r = self.c.json("POST", f"/api/projects/{self.pid}/upload/begin", {"path": "big.txt", "size": 3 * 1024 * 1024})
        self.assertEqual(s, 413)
        self.assertEqual(self.c.json("GET", f"/api/projects/{self.pid}/files")[1]["entries"], [])

    def test_quota_enforced_across_files(self):
        self.upload("a.txt", b"a" * (1024 * 1024 + 500_000))
        self.upload("b.txt", b"b" * (1024 * 1024 + 400_000))
        s, r = self.c.json("POST", f"/api/projects/{self.pid}/upload/begin", {"path": "c.txt", "size": 600_000})
        self.assertEqual(s, 413)
        self.assertIn("quota", r["error"])

    def test_chunk_protocol_errors(self):
        s, r = self.c.json("POST", f"/api/projects/{self.pid}/upload/begin", {"path": "x.txt", "size": 10})
        uid = r["upload_id"]
        hdr = {"Content-Type": "application/octet-stream"}
        s, r, t = self.c.req("POST", f"/api/uploads/{uid}/chunk?offset=5", raw=b"12345", headers=hdr)
        self.assertEqual(s, 409)
        self.c.req("POST", f"/api/uploads/{uid}/chunk?offset=0", raw=b"12345", headers=hdr)
        s, r = self.c.json("POST", f"/api/uploads/{uid}/finish", {})
        self.assertEqual(s, 409)  # incomplete
        s, r, t = self.c.req("POST", f"/api/uploads/{uid}/chunk?offset=0", raw=b"12345", headers=hdr)
        self.assertEqual(s, 404)  # finish discarded it

    def test_other_users_cannot_touch_my_files_or_uploads(self):
        self.upload("secret.txt", b"alice only")
        bob = self.login("bob")
        self.assertEqual(bob.json("GET", f"/api/projects/{self.pid}/files")[0], 404)
        self.assertEqual(bob.json("POST", f"/api/projects/{self.pid}/upload/begin", {"path": "x.txt", "size": 1})[0], 404)
        s, r = self.c.json("POST", f"/api/projects/{self.pid}/upload/begin", {"path": "y.txt", "size": 3})
        s, r, t = bob.req("POST", f"/api/uploads/{r['upload_id']}/chunk?offset=0", raw=b"abc", headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(s, 404)

    def test_unauthenticated_refused(self):
        anon = Client(self.srv.server_port)
        self.assertEqual(anon.json("GET", f"/api/projects/{self.pid}/files")[0], 401)

    def test_traversal_in_read_and_list(self):
        self.upload("ok.txt", b"hi")
        for p in ("../../auth.db", "/etc/passwd", "..%2f..%2fauth.db"):
            s, r = self.c.json("GET", f"/api/projects/{self.pid}/file?path={p}")
            self.assertIn(s, (400, 404), p)
            self.assertNotIn("root:", json.dumps(r))

    def test_clear_files_and_project_delete(self):
        self.upload("a.txt", b"hello")
        s, r = self.c.json("POST", f"/api/projects/{self.pid}/clear-files", {})
        self.assertFalse(r["has_files"])
        self.assertEqual(self.c.json("GET", f"/api/projects/{self.pid}/files")[1]["entries"], [])
        self.upload("b.txt", b"hello")
        self.c.json("DELETE", f"/api/projects/{self.pid}")
        self.assertEqual(self.app.files.usage(__import__("jabber.store", fromlist=["x"]).ns_for(self.app.local.id_map().popitem()[0])), 0)

    def test_search_endpoint_logic(self):
        self.upload("a.txt", b"alpha\nThe Budget is 5\nomega")
        self.upload("sub/b.txt", b"nothing\nbudget again")
        from jabber import store
        ns = store.ns_for([i for i, u in self.app.local.id_map().items() if u == "alice"][0])
        hits = self.app.files.search(ns, self.pid, "BUDGET")["matches"]
        self.assertEqual(sorted((h["path"], h["line"]) for h in hits), [("a.txt", 2), ("sub/b.txt", 2)])

    # ---- model tools
    def chat(self, cid, msg):
        s, r, t = self.c.req("POST", f"/api/conversations/{cid}/chat", {"message": msg})
        return s, events(t)

    def test_model_reads_files_with_tools_and_log_has_no_content(self):
        self.upload("Report.docx", make_docx(["Findings: pump failure at 3am"]))
        self.fb.script = [{"name": "list_project_files", "args": {}},
                          {"name": "read_project_file", "args": {"path": "Report.docx.extracted.txt"}}]
        cid = self.c.json("POST", "/api/conversations", {"project_id": self.pid})[1]["id"]
        s, ev = self.chat(cid, "what happened?")
        self.assertEqual(s, 200)
        kinds = [e["type"] for e in ev]
        self.assertEqual(kinds.count("tool"), 2)
        text = "".join(e["text"] for e in ev if e["type"] == "content_delta")
        self.assertIn("pump failure at 3am", text)
        first_sys = self.fb.calls[0][1][0]
        self.assertEqual(first_sys["role"], "system")
        self.assertIn("Report.docx.extracted.txt", first_sys["content"])
        conv = self.c.json("GET", f"/api/conversations/{cid}")[1]
        self.assertEqual([t["name"] for t in conv["messages"][-1]["tools"]], ["list_project_files", "read_project_file"])
        with open(self.app.telemetry.path) as fh:
            self.assertNotIn("pump failure", fh.read())
        e = [x for x in self.app.telemetry.read() if x["kind"] == "chat"][-1]
        self.assertEqual((e["tool_calls"], e["completion_tokens"]), (2, 5 + 5 + 7))

    def test_tool_cannot_escape_project(self):
        self.upload("ok.txt", b"fine")
        self.fb.script = [{"name": "read_project_file", "args": {"path": "../../auth.db"}}]
        cid = self.c.json("POST", "/api/conversations", {"project_id": self.pid})[1]["id"]
        s, ev = self.chat(cid, "go")
        text = "".join(e["text"] for e in ev if e["type"] == "content_delta")
        self.assertIn("outside the project", text)

    def test_no_tools_without_project_files_or_when_disabled(self):
        cid = self.c.json("POST", "/api/conversations", {})[1]["id"]
        self.chat(cid, "hi")
        self.assertEqual(self.fb.offered[-1], False)
        self.upload("a.txt", b"x")
        cid2 = self.c.json("POST", "/api/conversations", {"project_id": self.pid})[1]["id"]
        self.chat(cid2, "hi")
        self.assertEqual(self.fb.offered[-1], True)
        self.app.cfg.tools = False
        self.chat(cid2, "again")
        self.assertEqual(self.fb.offered[-1], False)

    def test_attachment_note(self):
        cid = self.c.json("POST", "/api/conversations", {})[1]["id"]
        s, r = self.c.json("POST", f"/api/conversations/{cid}/attachment-note", {"filenames": ["a.pdf.extracted.txt", "b.png"]})
        self.assertEqual(s, 200)
        self.assertIn("Attached files: a.pdf.extracted.txt, b.png", r["messages"][-1]["content"])
        self.assertEqual(self.c.json("POST", f"/api/conversations/{cid}/attachment-note", {"filenames": []})[0], 400)

    def test_features_advertised(self):
        f = self.c.json("GET", "/api/me")[1]["features"]
        self.assertTrue(f["files"])
        self.assertIn("pdf", f["extract"])


if __name__ == "__main__":
    unittest.main()
