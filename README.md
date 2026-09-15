# Manhwa Recap Pipeline (skeleton)

A staged, file-based pipeline that turns a manga/manhwa PDF into a recap video.
Right now **only stage 1 is real** - every other stage is a working stub that
produces fake-but-valid output, so the whole chain runs end-to-end today. You
replace the stubs one at a time.

## The idea

Each stage is an isolated script that reads files + `manifest.json`, does its
job, writes files, and advances the manifest. Nothing is passed in memory. That
means: any stage can be run and inspected alone, a crash just resumes from the
last finished stage, and you can swap one stage's implementation without
touching the others. The `manifest.json` is the contract that holds it together.

```
PDF -> [1] pages -> [2] panels+OCR -> [3] context -> [4] clean art
    -> [5] script -> [6] voiceover -> [7] video
```

## Quickstart

```bash
python -m venv .venv && .venv\Scripts\activate     # Windows
pip install -r requirements.txt

python make_fixture.py        # creates a tiny test PDF at inputs/ch_001.pdf
python orchestrator.py        # runs the whole pipeline (stubs) end-to-end
```

Then open `output/ch_001/storyboard.html` in your browser - it shows every
panel in reading order with its (stub) script line, emotion, and duration. That
HTML is what stage 7 will replace with a real rendered video.

Useful commands:

```bash
python orchestrator.py                 # resume ch_001 where it left off
python orchestrator.py --from script   # re-run from stage 5 onward
python orchestrator.py --force         # re-run everything
python orchestrator.py --chapter ch_002
```

## Build order (replace stubs in this order)

| # | Stage | Runs on | Replace stub with |
|---|-------|---------|-------------------|
| 1 | PDF -> pages | laptop | done (PyMuPDF) |
| 5 | script | API | Gemini / Groq LLM (do an easy API stage early) |
| 3 | context + emotion | API | Gemini 2.5 Flash (vision) |
| 2 | panels + OCR | Kaggle GPU | Magi / MagiV2 |
| 4 | clean art | Kaggle GPU | IOPaint + LaMa |
| 6 | voiceover | Kaggle GPU | Chatterbox-Turbo / Higgs Audio V2 |
| 7 | video | laptop (NVENC) | MoviePy or Remotion |

Do the API stages (3, 5) before the GPU stages - they run on your laptop and
let you see real scripts fast. Each stub file has a header comment naming the
exact model/library to drop in.

## References for the real stages

- Magi / MagiV2 transcription model: github.com/ragavsachdeva/magi
- Magi -> read-along video reference: github.com/BinhPQ2/magi_functional
- Inpainting: IOPaint (LaMa)
- TTS (commercial-safe, emotional): Chatterbox-Turbo, Higgs Audio V2
- LLM/vision free tier: Google AI Studio (Gemini 2.5 Flash), Groq (Llama 3.3 70B)
- Free GPU: Kaggle Notebooks (~30 GPU hrs/week, T4/P100 16GB)

## A note on sources

Reading full dialogue over someone else's panels is the legally fragile part of
the recap format, regardless of how good the pipeline is. Test on the bundled
fixture; for a real channel, lean on original commentary and use licensed or
permission-cleared works. Not legal advice.
