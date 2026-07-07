# 🎤 PaperKaraoke

**Karaoke for research papers.** Open a PDF, press play, and listen to it read
aloud while a highlight box follows the spoken word across the page — with a
continuous, seekable timeline and zoom.

> Narration is generated **one page at a time and in parallel**, so playback can
> start on page 1 within seconds while the rest of the paper is still being
> synthesized.

<!-- Add a screen recording here — it's what sells it:
![demo](docs/demo.gif) -->

## Features

- 📄 **Pick or upload** any PDF from your computer.
- 🗣️ **Neural narration** via [`edge-tts`](https://github.com/rany2/edge-tts)
  (Microsoft voices, no API key).
- ✨ **Word-synced highlight** drawn on the actual PDF page.
- ⏳ **Per-page streaming** with a live progress bar — no blank waiting.
- ▶️ **Continuous playback** across pages, with a seekable slider.
- 🔍 **Zoom** (buttons, `Ctrl/⌘ + wheel`, `Ctrl/⌘ + +/-/0`) — pages re-render crisp.
- 🐢🐇 **Playback speed** control.
- 💾 **Cached** to `voices/` so a paper opens instantly the second time.

## How it works

1. **PyMuPDF** extracts each page's words *with their bounding boxes*.
2. **edge-tts** synthesizes narration for each page (a few pages concurrently).
3. Microsoft's endpoint only reports **sentence** timing, so word timing is
   interpolated within each sentence (weighted by word length) and attached to
   the boxes from step 1.
4. The browser renders the PDF with **PDF.js** and moves a highlight overlay to
   the active word as the audio plays.

The server is pure Python standard library (`http.server`); PDF.js loads from a
CDN. The only dependencies are PyMuPDF and edge-tts.

## Requirements

- Python 3.8+
- Internet connection (edge-tts streams from Microsoft's cloud)

```bash
pip install -r requirements.txt
```

## Run

```bash
python dashboard/server.py
# open http://127.0.0.1:8000
```

Select a paper or click **Upload PDF…**, watch the progress bar, and press ▶.
The first generation of a paper needs internet; after that it loads from
`voices/`.

## Notes & limitations

- **Word timing is approximate** within a sentence — edge-tts no longer emits
  per-word boundaries, so sub-sentence timing is interpolated. For exact word
  timing you'd add a forced aligner (e.g. WhisperX) over the generated audio.
- **Dense / two-column pages**: word reading order can occasionally make the
  highlight jump; the audio and slider stay correct regardless.
- **GPU doesn't help**: edge-tts runs in Microsoft's cloud, so the speed levers
  are per-page streaming and parallel requests, not local compute.

## Project layout

```
dashboard/
  server.py     # stdlib HTTP server: upload, per-page job manager, progress API
  narrate.py    # PDF -> word boxes -> per-page edge-tts -> timing manifest
  index.html    # viewer + player + zoom UI
  app.js        # PDF.js render, playlist playback, highlight sync, zoom
  style.css
requirements.txt
```

## License

MIT — see `LICENSE` (add one if you want this to be reusable).
