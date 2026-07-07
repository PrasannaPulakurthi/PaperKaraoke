"""Local dashboard server for PaperKaraoke.

Pick or upload a PDF, watch narration generate with a live progress bar, then
play it back with a highlight that follows the spoken word on the PDF.

Zero extra dependencies (stdlib ThreadingHTTPServer). Generation runs in a
background thread; the browser polls /api/progress so the user never stares at
a frozen page. Generated audio + timing are persisted under voices/.

Run (from the DeepLearning env, needs internet the first time per PDF):
    python dashboard/server.py
then open http://127.0.0.1:8000
"""
import os
import re
import json
import mimetypes
import threading
from urllib.parse import urlparse, parse_qs, unquote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import narrate

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.normpath(os.path.join(_HERE, ".."))      # workspace PDFs live here
UPLOADS = os.path.join(ROOT, "uploads")                 # user-uploaded PDFs
OUT_DIR = narrate.OUT_DIR                                # voices/ : generated audio

MAX_UPLOAD = 60 * 1024 * 1024                            # 60 MB safety cap

# ---- background generation jobs, keyed by pdf stem ----
# Each job holds page entries as they finish (any order, via bounded
# concurrency). The browser polls /api/progress for the lightweight status +
# ready page indices, then fetches each page's events once from /api/page.
_jobs = {}
_jobs_lock = threading.Lock()


def _new_job():
    return {"state": "queued", "pct": 0, "msg": "Queued…", "error": None,
            "n_pages": None, "page_sizes": None, "pdf_url": None, "pages": {}}


def _progress_view(stem):
    """Lightweight status for polling (no per-page events)."""
    with _jobs_lock:
        j = _jobs.get(stem)
        if not j:
            return {"state": "idle", "pct": 0, "msg": ""}
        return {"state": j["state"], "pct": j["pct"], "msg": j["msg"],
                "error": j["error"], "n_pages": j["n_pages"],
                "page_sizes": j["page_sizes"], "pdf_url": j["pdf_url"],
                "ready": sorted(j["pages"].keys())}


def _page_view(stem, page):
    with _jobs_lock:
        j = _jobs.get(stem)
        if j and page in j["pages"]:
            return j["pages"][page]
    return None


def _run_job(pdf_path, pdf_name, stem):
    try:
        def on_start(page_sizes, n_pages):
            with _jobs_lock:
                j = _jobs[stem]
                j.update(state="running", n_pages=n_pages, page_sizes=page_sizes,
                         pdf_url="/pdf/" + pdf_name)

        def on_page(entry):
            e = dict(entry)
            if e.get("audio"):
                e["audio_url"] = "/audio/" + e["audio"]
            with _jobs_lock:
                _jobs[stem]["pages"][e["page"]] = e

        def progress(pct, msg):
            with _jobs_lock:
                _jobs[stem].update(pct=round(pct, 4), msg=msg)

        narrate.generate_pages(pdf_path, out_dir=OUT_DIR, on_start=on_start,
                               on_page=on_page, progress=progress)
        with _jobs_lock:
            _jobs[stem].update(state="done", pct=1.0, msg="Ready")
    except Exception as e:
        with _jobs_lock:
            _jobs[stem].update(state="error", msg="Failed", error=repr(e))


def _start_job(pdf_name):
    """Start (or reuse) generation for pdf_name; return the lightweight view."""
    pdf_path = _find_pdf(pdf_name)
    if not pdf_path:
        return {"state": "error", "error": "unknown pdf"}
    stem = os.path.splitext(pdf_name)[0]
    with _jobs_lock:
        cur = _jobs.get(stem)
        reuse = cur is not None and cur["state"] in ("running", "queued", "done")
        if not reuse:
            _jobs[stem] = _new_job()      # (re)generate; generate_pages caches on disk
    if not reuse:
        threading.Thread(target=_run_job, args=(pdf_path, pdf_name, stem),
                         daemon=True).start()
    return _progress_view(stem)


# ---- pdf discovery / path safety ----
def _list_pdfs():
    names = set()
    for d in (ROOT, UPLOADS):
        if os.path.isdir(d):
            for f in os.listdir(d):
                if f.lower().endswith(".pdf") and os.path.isfile(os.path.join(d, f)):
                    names.add(f)
    return sorted(names)


def _find_pdf(name):
    name = os.path.basename(unquote(name))
    for d in (ROOT, UPLOADS):
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _safe_name(name):
    name = os.path.basename(unquote(name or "")).strip()
    name = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    # ---- response helpers ----
    def _send(self, code, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _file(self, path, ctype=None, allow_range=False):
        if not path or not os.path.isfile(path):
            return self._send(404, "not found", "text/plain")
        ctype = ctype or (mimetypes.guess_type(path)[0] or "application/octet-stream")
        size = os.path.getsize(path)
        rng = self.headers.get("Range") if allow_range else None
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1)) if m and m.group(1) else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            length = max(0, end - start + 1)
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            if self.command != "HEAD":
                with open(path, "rb") as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
            return
        with open(path, "rb") as f:
            data = f.read()
        self._send(200, data, ctype, {"Accept-Ranges": "bytes"} if allow_range else None)

    # ---- routing ----
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p in ("/", "/index.html"):
                return self._file(os.path.join(_HERE, "index.html"), "text/html; charset=utf-8")
            if p in ("/app.js", "/style.css"):
                ctype = "text/javascript" if p.endswith(".js") else "text/css"
                return self._file(os.path.join(_HERE, p.lstrip("/")), ctype)
            if p == "/api/pdfs":
                return self._json({"pdfs": _list_pdfs()})
            if p == "/api/progress":
                stem = os.path.splitext(os.path.basename(unquote(q.get("pdf", [""])[0])))[0]
                return self._json(_progress_view(stem))
            if p == "/api/page":
                stem = os.path.splitext(os.path.basename(unquote(q.get("pdf", [""])[0])))[0]
                try:
                    page = int(q.get("page", ["-1"])[0])
                except ValueError:
                    page = -1
                entry = _page_view(stem, page)
                return self._json(entry) if entry else self._json({"error": "not ready"}, 404)
            if p.startswith("/pdf/"):
                return self._file(_find_pdf(p[len("/pdf/"):]), "application/pdf", allow_range=True)
            if p.startswith("/audio/"):
                name = os.path.basename(unquote(p[len("/audio/"):]))
                return self._file(os.path.join(OUT_DIR, name), "audio/mpeg", allow_range=True)
            return self._send(404, "not found", "text/plain")
        except BrokenPipeError:
            pass
        except Exception as e:
            self._json({"error": repr(e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        p = u.path
        q = parse_qs(u.query)
        try:
            if p == "/api/upload":
                return self._upload(q.get("name", [""])[0])
            if p == "/api/generate":
                name = unquote(q.get("pdf", [""])[0])
                if not _find_pdf(name):
                    return self._json({"error": "unknown pdf"}, 400)
                return self._json(_start_job(name))
            return self._send(404, "not found", "text/plain")
        except Exception as e:
            self._json({"error": repr(e)}, 500)

    def _upload(self, raw_name):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0:
            return self._json({"error": "empty upload"}, 400)
        if length > MAX_UPLOAD:
            return self._json({"error": "file too large"}, 413)
        name = _safe_name(raw_name)
        os.makedirs(UPLOADS, exist_ok=True)
        dest = os.path.join(UPLOADS, name)
        # stream body to disk
        remaining = length
        with open(dest, "wb") as f:
            while remaining > 0:
                chunk = self.rfile.read(min(1 << 16, remaining))
                if not chunk:
                    break
                f.write(chunk)
                remaining -= len(chunk)
        # sanity: must look like a PDF
        with open(dest, "rb") as f:
            head = f.read(5)
        if head[:4] != b"%PDF":
            os.remove(dest)
            return self._json({"error": "not a PDF file"}, 400)
        return self._json({"pdf": name, "stem": os.path.splitext(name)[0]})


def main():
    port = int(os.environ.get("PORT", "8000"))
    os.makedirs(OUT_DIR, exist_ok=True)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"PaperKaraoke on http://127.0.0.1:{port}")
    print(f"  PDFs from: {ROOT}  (+ uploads/)")
    print(f"  Audio to:  {OUT_DIR}")
    print("Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
