"""Create a compact, print-friendly PDF from Telegram delivery-note photographs.

Every input image is a Telegram file owned by the incoming user and fetched
with server credentials. This is document packaging, not OCR or confirmation
that stock was actually received.
"""
from io import BytesIO
from PIL import Image, UnidentifiedImageError

MAX_PAGES = 6
MAX_PIXELS = 24_000_000
MAX_EDGE = (1650, 2350)
Image.MAX_IMAGE_PIXELS = MAX_PIXELS


def photos_to_pdf(photo_bytes):
    if not photo_bytes or len(photo_bytes)>MAX_PAGES:
        raise ValueError("Для PDF потрібно від 1 до 6 фотографій.")
    pages=[]
    try:
        for raw in photo_bytes:
            try:
                with Image.open(BytesIO(raw)) as source:
                    if source.width*source.height>MAX_PIXELS:
                        raise ValueError("Фото занадто велике за роздільністю.")
                    source.verify()
                with Image.open(BytesIO(raw)) as source:
                    source.thumbnail(MAX_EDGE,Image.Resampling.LANCZOS)
                    page=source.convert("RGB")
                    pages.append(page)
            except (UnidentifiedImageError,OSError):
                raise ValueError("Не вдалося прочитати одне з фото накладної.")
        result=BytesIO()
        pages[0].save(result,format="PDF",save_all=True,
                      append_images=pages[1:],resolution=150.0)
        pdf=result.getvalue()
        if not pdf.startswith(b"%PDF-"):
            raise ValueError("Не вдалося зібрати PDF.")
        if len(pdf)>18_000_000:
            raise ValueError("Зібраний PDF завеликий. Поділи накладні на дві частини.")
        return pdf
    finally:
        for page in pages:
            page.close()
