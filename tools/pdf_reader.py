from __future__ import annotations
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _cache_key(file_path: Path, page_num: int, dpi: int, model: str) -> str:
    stat = file_path.stat()
    raw = f"{file_path}|{stat.st_mtime}|{stat.st_size}|{page_num}|{dpi}|{model}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _ocr_page_groq(image_bytes: bytes, vision_model: str, groq_client) -> str:
    import base64
    b64 = base64.b64encode(image_bytes).decode()
    response = groq_client.chat.completions.create(
        model=vision_model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}},
                {"type": "text", "text": (
                    "You are a medical document OCR system. "
                    "Transcribe ALL text from this medical document image exactly as it appears. "
                    "Preserve structure, labels, values, and any handwritten text. "
                    "Do not summarize or interpret — only transcribe."
                )},
            ],
        }],
        max_tokens=2000,
        temperature=0,
    )
    return response.choices[0].message.content or ""


def _ocr_page_groq_with_retry(
    image_bytes: bytes,
    vision_model: str,
    groq_client,
    max_retries: int = 5,
) -> str:
    """Call Groq OCR with exponential backoff on rate limit errors."""
    import groq as groq_module
    delay = 3
    for attempt in range(max_retries):
        try:
            return _ocr_page_groq(image_bytes, vision_model, groq_client)
        except groq_module.RateLimitError:
            if attempt < max_retries - 1:
                print(f"    [Rate limit] waiting {delay}s before retry {attempt+2}/{max_retries}...")
                time.sleep(delay)
                delay *= 2  # exponential backoff
            else:
                raise
        except Exception:
            raise
    return ""


def extract_pdf_pages(
    pdf_path: Path,
    enable_ocr: bool = True,
    ocr_provider: str = "auto",
    vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct",
    ocr_dpi: int = 150,
    max_ocr_pages: Optional[int] = None,
    cache_dir: Optional[Path] = None,
    ocr_delay: float = 2.0,   # seconds between OCR calls to avoid rate limits
) -> list[dict]:
    import fitz

    pages = []
    groq_client = None

    if enable_ocr and ocr_provider in ("groq", "auto"):
        api_key = os.getenv("GROQ_API_KEY")
        if api_key:
            try:
                from groq import Groq
                groq_client = Groq(api_key=api_key)
            except Exception as exc:
                logger.warning(f"Could not init Groq client: {exc}")
        else:
            logger.warning("GROQ_API_KEY not set")

    try:
        doc = fitz.open(str(pdf_path))
    except Exception as exc:
        logger.error(f"Cannot open PDF {pdf_path}: {exc}")
        return [{"file": pdf_path.name, "page": 0, "text": "",
                 "method": "error", "status": "error", "error": str(exc)}]

    ocr_count = 0
    total_pages = len(doc)

    for page_num in range(total_pages):
        page = doc[page_num]
        text = page.get_text("text").strip()
        method = "embedded_text"

        if not text and enable_ocr:
            if max_ocr_pages is not None and ocr_count >= max_ocr_pages:
                pages.append({
                    "file": pdf_path.name, "page": page_num + 1,
                    "text": "", "method": "skipped_ocr_limit",
                    "status": "empty", "error": f"OCR limit ({max_ocr_pages}) reached",
                })
                continue

            # Check cache first
            cache_file = None
            if cache_dir:
                cache_dir.mkdir(parents=True, exist_ok=True)
                ck = _cache_key(pdf_path, page_num, ocr_dpi, vision_model)
                cache_file = cache_dir / f"{ck}.txt"
                if cache_file.exists():
                    text = cache_file.read_text(encoding="utf-8")
                    method = "ocr_cached"
                    print(f"    [Cache] p{page_num+1}/{total_pages} ✓")

            if not text:
                # Render page to image
                try:
                    mat = fitz.Matrix(ocr_dpi / 72, ocr_dpi / 72)
                    pix = page.get_pixmap(matrix=mat)
                    img_bytes = pix.tobytes("png")
                except Exception as exc:
                    pages.append({
                        "file": pdf_path.name, "page": page_num + 1,
                        "text": "", "method": "render_error",
                        "status": "error", "error": str(exc),
                    })
                    continue

                print(f"    [OCR] p{page_num+1}/{total_pages} ...", end=" ", flush=True)

                if groq_client and ocr_provider in ("groq", "auto"):
                    try:
                        # Polite delay between calls
                        if ocr_count > 0:
                            time.sleep(ocr_delay)
                        text = _ocr_page_groq_with_retry(img_bytes, vision_model, groq_client)
                        method = "ocr_groq"
                        print("✓")
                    except Exception as exc:
                        print(f"✗ ({exc})")
                        logger.warning(f"Groq OCR failed p{page_num+1}: {exc}")
                        text = ""
                        method = "ocr_failed"

                ocr_count += 1

                # Save to cache
                if cache_file and text:
                    cache_file.write_text(text, encoding="utf-8")

        status = "ok" if text.strip() else "empty"
        pages.append({
            "file": pdf_path.name, "page": page_num + 1,
            "text": text, "method": method,
            "status": status, "error": None,
        })

    doc.close()
    return pages