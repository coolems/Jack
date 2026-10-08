"""Init package for ZZZ initial init (fresh-clone setup).

The entry point is utils/zzz_init.py (run by ZZZ_initial_init.bat), which re-exports
main() from this package. Every module here is STDLIB-ONLY - the whole init must run on
a bare Python >= 3.10 before any venv exists, so no third-party imports anywhere in here.

Module map:
    constants.py   verified URLs / sizes / quant table (HuggingFace + GitHub)
    errors.py      InitError
    paths.py       repo ROOT + CLIENT_UI_PORT reader (no config-package import allowed)
    ui.py          console output + interactive ask helpers + tiny file helpers
    gpu.py         nvidia-smi detection + VRAM tier table
    downloads.py   resumable HTTP download engine + flat zip extraction
    certs.py       CLIENT UI TLS certificate generation
    llama.py       llama.cpp binary install (pinned build)
    models.py      model selection menu + model/mmproj/OCR downloads
    wiring.py      config wiring (API keys, profiles, context window, client settings)
    main.py        the step-by-step init flow
"""
