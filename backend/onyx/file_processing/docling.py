from typing import IO
from typing import Any

import requests

from onyx.configs.app_configs import DOCLING_OCR_PRESET
from onyx.configs.app_configs import DOCLING_REQUEST_TIMEOUT
from onyx.configs.app_configs import DOCLING_SERVE_URL
from onyx.utils.logger import setup_logger

logger = setup_logger()

# Per-engine OCR language, omitted for engines not listed here.
#
# Tesseract needs an explicit ISO-639-2 code. EasyOCR is deliberately absent: it
# takes ISO-639-1, and passing tesseract's "eng" fails inside the docling-serve
# task, surfacing as an opaque 404 "Task result not found". Passing "en" works but
# selects the English-only recognition model, which drops document headings that
# docling's default (latin) model reads correctly -- so let docling choose.
_OCR_LANG_BY_PRESET: dict[str, str] = {"tesseract": "eng"}


def get_docling_url() -> str | None:
    """Base URL of a docling-serve instance, or None when OCR fallback is off."""
    return DOCLING_SERVE_URL or None


def docling_to_text(file: IO[Any], file_name: str) -> str:
    """OCR a document via docling-serve and return its markdown.

    Only called when native extraction yields nothing (i.e. a scan). Markdown is
    requested rather than plain text so table structure survives into chunking.
    """
    base_url = get_docling_url()
    if not base_url:
        raise ValueError("DOCLING_SERVE_URL is not configured")

    data = {
        "to_formats": "md",
        "do_ocr": "true",
        "force_ocr": "true",
        "image_export_mode": "placeholder",
        # ocr_engine is deprecated in docling-serve and silently loses to
        # ocr_preset, whose "auto" default selects RapidOCR's Chinese models
        # and drops spaces between English words.
        "ocr_preset": DOCLING_OCR_PRESET,
    }
    ocr_lang = _OCR_LANG_BY_PRESET.get(DOCLING_OCR_PRESET)
    if ocr_lang:
        data["ocr_lang"] = ocr_lang

    file.seek(0)
    response = requests.post(
        f"{base_url.rstrip('/')}/v1/convert/file",
        files={"files": (file_name, file.read(), "application/pdf")},
        data=data,
        timeout=DOCLING_REQUEST_TIMEOUT,
    )
    response.raise_for_status()

    payload = response.json()
    status = payload.get("status")
    if status not in ("success", "partial_success"):
        raise ValueError(f"docling-serve returned status {status}: {payload.get('errors')}")

    return payload.get("document", {}).get("md_content") or ""
