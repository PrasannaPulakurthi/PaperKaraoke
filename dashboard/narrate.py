"""Generate narration MP3 + per-word timing (with on-page boxes) for a PDF.

edge-tts no longer emits per-word ``WordBoundary`` events (Microsoft's endpoint
only sends ``SentenceBoundary`` now). So we anchor timing on sentences and
*interpolate* per-word timing within each sentence, weighted by word length.
The highlight is therefore sentence-accurate and word-smooth, but sub-sentence
word times are approximate.

Key idea (see the plan): build the narration text directly from PyMuPDF word
tokens so every token already carries its page + bounding box, then map the
spoken sentences back onto contiguous runs of those tokens.

Usage (standalone):
    python narrate.py "<paper>.pdf"
"""
import os
import re
import sys
import json
import asyncio

import fitz          # PyMuPDF
import edge_tts

VOICE = "en-US-AndrewNeural"   # matches make_audio.py
RATE  = "+0%"

_HERE = os.path.dirname(os.path.abspath(__file__))
# generated narration (mp3 + timing json) is persisted here
OUT_DIR = os.path.normpath(os.path.join(_HERE, "..", "voices"))
CACHE_DIR = OUT_DIR   # backwards-compatible alias

# reuse make_audio.clean()'s intent, applied at the token/line level
HEADER_RE     = re.compile(r"IEEE INTERNATIONAL WORKSHOP ON MACHINE LEARNING", re.I)
REFERENCES_RE = re.compile(r"^\s*\d{0,2}\.?\s*REFERENCES\s*$", re.I)
_CHARFIX = {"ﬁ": "fi", "ﬂ": "fl", "–": "-", "—": "-", "�": "-"}


def _fix_chars(s):
    for k, v in _CHARFIX.items():
        s = s.replace(k, v)
    return s


def _norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _merge_hyphen(tokens):
    """Merge line-break hyphenation: "regres-" + "sion" -> "regression"."""
    merged = []
    i = 0
    while i < len(tokens):
        tk = tokens[i]
        if tk["text"].endswith("-") and len(tk["text"]) > 1 and i + 1 < len(tokens):
            joined = dict(tk)
            joined["text"] = tk["text"][:-1] + tokens[i + 1]["text"]
            merged.append(joined)   # keep the first part's box
            i += 2
        else:
            merged.append(tk)
            i += 1
    return merged


def extract_pages(pdf_path):
    """Return (pages, page_sizes).

    pages: list (one per PDF page) of token lists, each token
           {"text", "page", "bbox":[x0,y0,x1,y1]} in reading order, with the
           conference header / references stripped and hyphenation merged.
           Pages after the REFERENCES heading (and figure-only pages) are [].
    page_sizes: [{"w","h"}] in PDF points, one per page (for canvas scaling).
    """
    doc = fitz.open(pdf_path)
    page_sizes = []
    pages = []
    stop = False
    for pno in range(doc.page_count):
        page = doc[pno]
        page_sizes.append({"w": page.rect.width, "h": page.rect.height})
        toks = []
        if not stop:
            words = page.get_text("words")  # (x0,y0,x1,y1, word, block, line, wordno)
            words.sort(key=lambda w: (w[5], w[6], w[7]))  # block, line, word order
            lines = {}
            for w in words:
                lines.setdefault((w[5], w[6]), []).append(w)
            for key in sorted(lines.keys()):
                lw = lines[key]
                line_text = " ".join(t[4] for t in lw)
                if HEADER_RE.search(line_text):
                    continue
                if REFERENCES_RE.match(line_text.strip()):
                    stop = True
                    break
                for t in lw:
                    toks.append({
                        "text": _fix_chars(t[4]),
                        "page": pno,
                        "bbox": [round(t[0], 1), round(t[1], 1),
                                 round(t[2], 1), round(t[3], 1)],
                    })
            toks = _merge_hyphen(toks)
        pages.append(toks)
    doc.close()
    return pages, page_sizes


def extract_tokens(pdf_path):
    """Flat (tokens, page_sizes) — kept for the single-file generate()/CLI."""
    pages, page_sizes = extract_pages(pdf_path)
    return [t for pg in pages for t in pg], page_sizes


async def _stream(text, mp3_path, on_frac=None):
    """Stream TTS to mp3_path; return list of sentence events.

    on_frac(frac) is called with a 0..1 estimate of synthesis progress, derived
    from how much of the input text the sentence events have covered so far.
    """
    comm = edge_tts.Communicate(text, VOICE, rate=RATE)
    sents = []
    total = max(1, len(text))
    consumed = 0
    with open(mp3_path, "wb") as f:
        async for ch in comm.stream():
            if ch["type"] == "audio":
                f.write(ch["data"])
            elif ch["type"] in ("SentenceBoundary", "WordBoundary"):
                sents.append({"offset": ch["offset"],
                              "duration": ch["duration"],
                              "text": ch["text"]})
                consumed += len(ch["text"]) + 1
                if on_frac:
                    on_frac(min(1.0, consumed / total))
    return sents


def build_events(sent_events, tokens):
    """Map each spoken sentence onto a contiguous run of tokens, then spread the
    sentence's [offset, offset+duration] across those tokens by word length."""
    events = []
    cursor = 0
    ntok = len(tokens)
    for se in sent_events:
        words = se["text"].split()
        if not words:
            continue
        # resync the cursor if we've drifted (edge-tts may merge/split a token)
        if cursor < ntok and _norm(tokens[cursor]["text"]) != _norm(words[0]):
            for j in range(cursor, min(cursor + 20, ntok)):
                if _norm(tokens[j]["text"]) == _norm(words[0]):
                    cursor = j
                    break
        start = cursor
        end = min(cursor + len(words), ntok)
        run = tokens[start:end]
        cursor = end
        if not run:
            continue
        t0 = se["offset"] / 10000.0            # 100ns units -> ms
        dur = se["duration"] / 10000.0
        weights = [max(1, len(t["text"])) for t in run]
        total = sum(weights)
        acc = 0
        for t, w in zip(run, weights):
            a = t0 + dur * acc / total
            acc += w
            b = t0 + dur * acc / total
            x0, y0, x1, y1 = t["bbox"]
            events.append({"t0": round(a, 1), "t1": round(b, 1), "page": t["page"],
                           "x0": x0, "y0": y0, "x1": x1, "y1": y1})
    return events


def generate(pdf_path, out_dir=None, progress=None, force=False):
    """Produce <out_dir>/<stem>.mp3 and <stem>.json; return the json path.
    Skips work if both already exist (unless force).

    progress(pct, msg) is an optional callback (pct is 0..1) so a UI can show
    generation progress instead of a blank wait.
    """
    def report(pct, msg):
        if progress:
            progress(pct, msg)

    out_dir = out_dir or OUT_DIR
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    mp3 = os.path.join(out_dir, stem + ".mp3")
    js  = os.path.join(out_dir, stem + ".json")
    if not force and os.path.exists(mp3) and os.path.exists(js):
        report(1.0, "Ready (cached)")
        return js

    report(0.02, "Reading PDF…")
    tokens, page_sizes = extract_tokens(pdf_path)
    text = " ".join(t["text"] for t in tokens)
    if not text.strip():
        raise RuntimeError("No extractable text in PDF (is it a scanned image?)")

    report(0.05, "Synthesizing narration…")
    # map synthesis fraction into the 5%..95% band of overall progress
    def on_frac(frac):
        report(0.05 + 0.90 * frac, "Synthesizing narration… %d%%" % int(frac * 100))

    # Render to temp files and atomically rename, so a partial render is never
    # served and two concurrent runs can't truncate a file mid-stream.
    tmp_mp3 = mp3 + ".%d.part" % os.getpid()
    tmp_js = js + ".%d.part" % os.getpid()
    sents = asyncio.run(_stream(text, tmp_mp3, on_frac=on_frac))

    report(0.97, "Aligning words…")
    events = build_events(sents, tokens)
    duration_ms = max((e["t1"] for e in events), default=0)
    data = {"stem": stem, "page_sizes": page_sizes,
            "duration_ms": duration_ms, "events": events}
    with open(tmp_js, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp_mp3, mp3)   # atomic on the same filesystem
    os.replace(tmp_js, js)
    report(1.0, "Ready")
    return js


def _write_manifest(path, stem, page_sizes, n_pages, ready, complete):
    """Atomically write the growing per-page manifest."""
    pages = [ready[i] for i in sorted(ready)]
    data = {"stem": stem, "page_sizes": page_sizes, "n_pages": n_pages,
            "complete": complete, "pages": pages}
    tmp = path + ".%d.part" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, path)


# how many pages to synthesize concurrently (edge-tts is network-bound; a few
# parallel requests cut total time without tripping rate limits)
CONCURRENCY = 4


def generate_pages(pdf_path, out_dir=None, on_start=None, on_page=None,
                   progress=None, force=False, concurrency=CONCURRENCY):
    """Generate narration one page at a time, streaming each page as it's ready.

    Each page becomes its own <stem>.p<N>.mp3 with page-local word timing, so
    the viewer can start on page 1 while later pages are still synthesizing.
    Pages are synthesized with bounded concurrency for speed.

    Callbacks:
      on_start(page_sizes, n_pages) — as soon as the PDF is parsed
      on_page(entry)                — each time a page finishes (any order)
      progress(pct, msg)            — 0..1 overall progress
    Returns the manifest json path.
    """
    out_dir = out_dir or OUT_DIR
    stem = os.path.splitext(os.path.basename(pdf_path))[0]
    os.makedirs(out_dir, exist_ok=True)
    manifest_path = os.path.join(out_dir, stem + ".manifest.json")

    # cache: reuse a complete manifest whose audio files all still exist
    if not force and os.path.exists(manifest_path):
        try:
            with open(manifest_path, encoding="utf-8") as f:
                m = json.load(f)
            ok = m.get("complete") and all(
                (not p.get("audio")) or os.path.exists(os.path.join(out_dir, p["audio"]))
                for p in m["pages"])
            if ok:
                if on_start:
                    on_start(m["page_sizes"], m["n_pages"])
                for p in m["pages"]:
                    if on_page:
                        on_page(p)
                if progress:
                    progress(1.0, "Ready (cached)")
                return manifest_path
        except Exception:
            pass  # fall through and regenerate

    pages_tokens, page_sizes = extract_pages(pdf_path)
    n = len(page_sizes)
    if on_start:
        on_start(page_sizes, n)
    if progress:
        progress(0.0, f"0/{n} pages ready")

    ready = {}
    done = 0

    async def run():
        nonlocal done
        sem = asyncio.Semaphore(concurrency)

        async def do_page(p):
            nonlocal done
            toks = pages_tokens[p]
            text = " ".join(t["text"] for t in toks).strip()
            if text:
                async with sem:
                    mp3name = "%s.p%d.mp3" % (stem, p)
                    mp3 = os.path.join(out_dir, mp3name)
                    tmp = mp3 + ".%d.part" % os.getpid()
                    sents = await _stream(text, tmp)
                    os.replace(tmp, mp3)
                events = build_events(sents, toks)
                entry = {"page": p, "audio": mp3name,
                         "duration_ms": max((e["t1"] for e in events), default=0),
                         "events": events, "empty": False}
            else:
                entry = {"page": p, "audio": None, "duration_ms": 0,
                         "events": [], "empty": True}
            ready[p] = entry
            done += 1
            if progress:
                progress(done / max(1, n), "%d/%d pages ready" % (done, n))
            _write_manifest(manifest_path, stem, page_sizes, n, ready,
                            complete=(done == n))
            if on_page:
                on_page(entry)

        await asyncio.gather(*(do_page(p) for p in range(n)))

    asyncio.run(run())
    _write_manifest(manifest_path, stem, page_sizes, n, ready, complete=True)
    if progress:
        progress(1.0, "Ready")
    return manifest_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: python narrate.py <paper>.pdf [--pages] [--force]")
        raise SystemExit(1)
    pdf = sys.argv[1]
    force = "--force" in sys.argv[2:]
    if "--pages" in sys.argv[2:]:
        def on_pg(e):
            print("  page %d ready: %d words, %.1fs%s"
                  % (e["page"], len(e["events"]), e["duration_ms"] / 1000,
                     " (no text)" if e["empty"] else ""))
        mpath = generate_pages(pdf, force=force, on_page=on_pg)
        print("wrote", mpath)
    else:
        js = generate(pdf, force=force)
        with open(js, encoding="utf-8") as f:
            data = json.load(f)
        ev = data["events"]
        print(f"wrote {js}")
        print(f"pages={len(data['page_sizes'])} events={len(ev)} "
              f"duration={data['duration_ms']/1000:.1f}s")
        if ev:
            print("first:", ev[0])
            print("last :", ev[-1])
