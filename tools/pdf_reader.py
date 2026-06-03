from __future__ import annotations
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _cache_key(file_path: Path, page_num: int, dpi: int, model: str) -> str:
    stat = file_path.stat()
    raw = f"{file_path}|{stat.st_mtime}|{stat.st_size}|{page_num}|{dpi}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _ocr_page_groq(
    image_bytes: bytes,
    vision_model: str,
    groq_client,
) -> str:
    """Send page image to Groq vision model for OCR."""
    import base64
    b64 = base64.b64encode(image_bytes).decode()
    response = groq_client.chat.completions.create(
        model=vision_model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{b64}"},
                    },
                    {
                        "type": "text",
                        "text": (
                            "You are a medical document OCR system. "
                            "Transcribe ALL text from this medical document image exactly as it appears. "
                            "Preserve structure, labels, values, and any handwritten text. "
                            "Do not summarize or interpret — only transcribe."
                        ),
                    },
                ],
            }
        ],
        max_tokens=2000,
        temperature=0,
    )
    return response.choices[0].message.content or ""


def _ocr_page_local(image_bytes: bytes) -> str:
    """Use local Tesseract for OCR."""
    try:
        import pytesseract
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(image_bytes))
        return pytesseract.image_to_string(img)
    except Exception as exc:
        logger.warning(f"Local OCR failed: {exc}")
        return ""


def extract_pdf_pages(
    pdf_path: Path,
    enable_ocr: bool = True,
    ocr_provider: str = "auto",
    vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct",
    ocr_dpi: int = 150,
    max_ocr_pages: Optional[int] = None,
    cache_dir: Optional[Path] = None,
) -> list[dict]:
    """
    Extract text from every page of a PDF.
    Returns list of dicts matching ExtractionPage schema.
    """
    import fitz  # PyMuPDF

    pages = []
    groq_client = None

    # Initialise Groq client if needed
    if enable_ocr and ocr_provider in ("groq", "auto"):
        api_key = os.getenv("GROQ_API_KEY")
        if api_key:
            try:
                from groq import Groq
                groq_client = Groq(api_key=api_key)
            except Exception as exc:
                logger.warning(f"Could not init Groq client: {exc}")
        else:
            logger.warning("GROQ_API_KEY not set — falling back to local OCR")

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        logger.error(f"Cannot open PDF {pdf_path}: {exc}")
        return [{"file": pdf_path.name, "page": 0, "text": "", "method": "error", "status": "error", "error": str(exc)}]

    ocr_count = 0
    for page_num in range(len(doc)):
        page = doc[page_num]
        text = page.get_text("text").strip()
        method = "embedded_text"

        if not text and enable_ocr:
            # OCR this page
            if max_ocr_pages is not None and ocr_count >= max_ocr_pages:
                pages.append({
                    "file": pdf_path.name,
                    "page": page_num + 1,
                    "text": "",
                    "method": "skipped_ocr_limit",
                    "status": "empty",
                    "error": f"OCR page limit ({max_ocr_pages}) reached",
                })
                continue

            # Check cache
            cache_file = None
            if cache_dir:
                cache_dir.mkdir(parents=True, exist_ok=True)
                ck = _cache_key(pdf_path, page_num, ocr_dpi, vision_model)
                cache_file = cache_dir / f"{ck}.txt"
                if cache_file.exists():
                    text = cache_file.read_text(encoding="utf-8")
                    method = "ocr_cached"
                    logger.info(f"OCR cache hit: {pdf_path.name} p{page_num+1}")

            if not text or method == "embedded_text":
                # Render page to image
                try:
                    mat = fitz.Matrix(ocr_dpi / 72, ocr_dpi / 72)
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("png")
                except Exception as exc:
                    pages.append({
                        "file": pdf_path.name,
                        "page": page_num + 1,
                        "text": "",
                        "method": "ocr_render_error",
                        "status": "error",
                        "error": str(exc),
                    })
                    continue

                # Try OCR
                ocr_text = ""
                if ocr_provider == "groq" or (ocr_provider == "auto" and groq_client):
                    try:
                        ocr_text = _ocr_page_groq(img_bytes, vision_model, groq_client)
                        method = "ocr_groq"
                    except Exception as exc:
                        logger.warning(f"Groq OCR failed p{page_num+1}: {exc} — trying local")
                        ocr_text = _ocr_page_local(img_bytes)
                        method = "ocr_local_fallback"
                elif ocr_provider == "local" or (ocr_provider == "auto" and not groq_client):
                    ocr_text = _ocr_page_local(img_bytes)
                    method = "ocr_local"

                text = ocr_text.strip()
                ocr_count += 1

                # Save to cache
                if cache_file and text:
                    cache_file.write_text(text, encoding="utf-8")

        status = "ok" if text else "empty"
        pages.append({
            "file": pdf_path.name,
            "page": page_num + 1,
            "text": text,
            "method": method,
            "status": status,
            "error": None,
        })

    doc.close()
    return pages