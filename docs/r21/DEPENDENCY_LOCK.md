# R21 dependency lock and platform compatibility (WP-09)

## Files

| File | Purpose |
| --- | --- |
| `requirements.txt` | core service (unchanged R2 set, range pins) |
| `requirements-optional.txt` | knowledge-engine extras (spreadsheet, image/OCR) |
| `constraints-r21.lock` | exact versions R21 was implemented and tested against |

Install:

```bash
python3 -m pip install -r requirements.txt -c constraints-r21.lock
python3 -m pip install -r requirements-optional.txt -c constraints-r21.lock
```

## What the lock actually covers

* Every package listed is a **pure-Python or multiplatform wheel** at that version, so the same
  pin resolves on Linux x86_64, Linux ARM64, Windows and macOS.
* The compiled-core packages (PyMuPDF, pydantic-core, lxml, Pillow) are pinned to versions with
  published wheels for all four families. If a platform lacks a wheel, pip builds from source;
  that is a host prerequisite, not a supported claim.
* `pytesseract` is deliberately commented out: it is a thin wrapper, and OCR also needs the
  `tesseract` binary, which pip cannot provide. `VisionEngine` reports
  `ocr_fallback=false`/`ocr_unavailable` until a host provisions both.

## Platform matrix

| Platform | Status | Notes |
| --- | --- | --- |
| Linux x86_64, CPython 3.12 | **tested** (R21 sandbox) | full suite passes |
| Linux x86_64, CPython 3.11 | expected (deployment target) | WO-R2-CLOSE records 3.11.2 for the deployed AGTools build; no 3.12-only syntax is used in `app/` |
| Linux ARM64 (Raspberry Pi OS 64-bit, CPython 3.11) | **expected, not verified here** | wheels exist for all pinned packages; source builds need `build-essential libffi-dev python3-dev zlib1g-dev libjpeg-dev`. LibreOffice rendering is optional and unrelated to the knowledge engine |
| Windows 10/11, CPython 3.11/3.12 | **expected, not verified here** | wheels exist; the artifact store uses `msvcrt` locking when `fcntl` is absent, and `os.replace` for atomic replacement. Path separators are normalised through `pathlib` |
| macOS (arm64), CPython 3.12 | expected | wheels exist; not part of the deployment set |

The store's locking is the only platform-sensitive code path:

```python
try:    import fcntl      # POSIX
except ImportError:  fcntl = None
try:    import msvcrt     # Windows
except ImportError:  msvcrt = None
```

Without either, the in-process lock still serialises threads; cross-process exclusion then relies
on the deployment running one writer process (documented, not assumed silently).

## Verification performed

| Check | Result |
| --- | --- |
| `python3 -m pytest -q` (AGTools) on the test host | see `docs/r21/MIGRATION_ROLLBACK.md` for the recorded run: R21 modules all pass; 2 pre-existing legacy failures remain (root page copy, external web-search test) |
| imports of every engine module with the pinned set | PDF/DOCX/XLSX/PPTX/text all available in the manifest |
| missing-dependency behaviour | `_module_available()` marks a format `available=false` with `unavailable_reason`, so a partial install degrades per format instead of failing at startup |
| lock regeneration | manual; the generation environment is recorded in the lock header |

## Rollback of the lock

The lock file is additive: removing `-c constraints-r21.lock` restores the pre-R21 range
resolution. No package was upgraded in place by this work, and no R2-only dependency changed.
