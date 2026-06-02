from __future__ import annotations

import base64
import hashlib
from io import BytesIO
import os
from pathlib import Path
from typing import cast

import fitz

from models.schemas import ExtractionPage


def _ocr_page_local(page, dpi: int = 180) -> tuple[str, str | None]:
    try:
        from PIL import Image
        import pytesseract
    except Exception as exc:  # pragma: no cover - depends on local install
        return "", f"OCR unavailable: {exc}"

    try:
        pix = page.get_pixmap(dpi=dpi)
        image = Image.open(BytesIO(pix.tobytes("png")))
        return pytesseract.image_to_string(image), None
    except Exception as exc:  # pragma: no cover - depends on local install
        return "", f"OCR failed: {exc}"


def _page_cache_key(path: Path, page_num: int, dpi: int, model: str) -> str:
    stat = path.stat()
    raw = f"{path.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{page_num}|{dpi}|{model}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _read_cache(cache_dir: Path | None, cache_key: str) -> str | None:
    if not cache_dir:
        return None
    path = cache_dir / f"{cache_key}.txt"
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _write_cache(cache_dir: Path | None, cache_key: str, text: str) -> None:
    if not cache_dir or not text.strip():
        return
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / f"{cache_key}.txt").write_text(text, encoding="utf-8")


def _ocr_page_groq(
    page,
    *,
    api_key: str,
    model: str,
    dpi: int,
) -> tuple[str, str | None]:
    try:
        from groq import Groq
    except Exception as exc:  # pragma: no cover - depends on local install
        return "", f"Groq OCR unavailable: {exc}"

    try:
        pix = page.get_pixmap(dpi=dpi, alpha=False)
        image_b64 = base64.b64encode(pix.tobytes("jpeg", jpg_quality=82)).decode("ascii")
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an OCR engine for synthetic clinical source-note images. "
                        "Return only text visible on the page. Preserve headings, tables, dates, "
                        "medication names, doses, and pending-result wording. Do not summarize, infer, "
                        "or add facts that are not visible."
                    ),
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract the visible text from this page. If unreadable, return OCR_UNREADABLE.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                        },
                    ],
                },
            ],
            temperature=0,
        )
        text = (response.choices[0].message.content or "").strip()
        if text == "OCR_UNREADABLE":
            return "", "Groq OCR reported the page as unreadable."
        return text, None
    except Exception as exc:  # pragma: no cover - external API
        return "", f"Groq OCR failed: {exc}"


def extract_pdf_pages(
    pdf_path: str | Path,
    *,
    enable_ocr: bool = True,
    ocr_provider: str = "auto",
    groq_api_key: str | None = None,
    groq_vision_model: str = "meta-llama/llama-4-scout-17b-16e-instruct",
    dpi: int = 150,
    max_ocr_pages: int | None = None,
    cache_dir: str | Path | None = None,
) -> list[ExtractionPage]:
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")

    pages: list[ExtractionPage] = []
    cache_path = Path(cache_dir) if cache_dir else None
    groq_api_key = groq_api_key or os.getenv("GROQ_API_KEY")

    with fitz.open(path) as doc:
        for page_index in range(doc.page_count):
            page_num = page_index + 1
            page = doc.load_page(page_index)
            text = cast(str, page.get_text("text")).strip()
            method = "embedded_text"
            status = "ok" if text else "empty"
            error = None

            if not text and enable_ocr and (max_ocr_pages is None or page_num <= max_ocr_pages):
                provider = ocr_provider.lower()
                if provider == "auto":
                    provider = "groq" if groq_api_key else "local"

                cache_key = _page_cache_key(path, page_num, dpi, f"{provider}:{groq_vision_model}")
                cached_text = _read_cache(cache_path, cache_key)
                if cached_text is not None:
                    text = cached_text.strip()
                    method = f"{provider}_ocr_cache"
                elif provider == "groq":
                    if not groq_api_key:
                        error = "Groq OCR requested but GROQ_API_KEY is not set."
                        text = ""
                    else:
                        text, error = _ocr_page_groq(
                            page,
                            api_key=groq_api_key,
                            model=groq_vision_model,
                            dpi=dpi,
                        )
                        _write_cache(cache_path, cache_key, text)
                    method = "groq_ocr"
                elif provider == "local":
                    text, error = _ocr_page_local(page, dpi=dpi)
                    _write_cache(cache_path, cache_key, text)
                    method = "local_ocr"
                elif provider in {"none", "off"}:
                    text = ""
                    error = "OCR disabled by provider setting."
                    method = "ocr_disabled"
                else:
                    text = ""
                    error = f"Unsupported OCR provider: {ocr_provider}"
                    method = "ocr"

                text = text.strip()
                status = "ok" if text else "failed"
            elif not text and enable_ocr and max_ocr_pages is not None and page_num > max_ocr_pages:
                method = "ocr_skipped"
                status = "skipped"
                error = f"Skipped OCR because max_ocr_pages={max_ocr_pages}."

            pages.append(
                ExtractionPage(
                    file=str(path),
                    page=page_num,
                    text=text,
                    method=method,
                    status=status,
                    error=error,
                )
            )
    return pages


def extract_pdf_text(pdf_path: str) -> str:
    pages = extract_pdf_pages(pdf_path)
    chunks = []
    for page in pages:
        chunks.append(f"\n\n===== PAGE {page.page} ({page.method}/{page.status}) =====\n\n{page.text}")
    return "".join(chunks)
