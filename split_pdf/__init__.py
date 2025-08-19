from fastapi import APIRouter, UploadFile, File, Form, Response, HTTPException
from io import BytesIO
import zipfile
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image
from pyzbar.pyzbar import decode as decode_barcode
import PyPDF2
import time
import logging
import gc
from utils.logging import get_logger
from utils.uploads import read_upload_file

Image.MAX_IMAGE_PIXELS = None
router = APIRouter()
logger = get_logger(__name__)

def extract_text(image: Image.Image):
    return pytesseract.image_to_string(image)

def get_barcodes(image: Image.Image):
    gray = image.convert("L")
    resized = gray.resize((gray.width * 2, gray.height * 2), Image.LANCZOS)
    bw = resized.point(lambda x: 0 if x < 128 else 255, '1')
    return [b.data.decode('utf-8') for b in decode_barcode(bw)]

@router.post("/split")
async def split_pdf(
    file: UploadFile = File(...),
    split_size: int = Form(None),
    keyword: str = Form(None),
    barcode: bool = Form(False)
):
    if sum([split_size is not None, bool(keyword), barcode]) != 1:
        raise HTTPException(status_code=400, detail="Kies één splitsoptie.")

    start = time.perf_counter()
    contents = await read_upload_file(file, logger)

    reader = PyPDF2.PdfReader(BytesIO(contents))
    total_pages = len(reader.pages)
    logger.info(f"{total_pages} pagina's gevonden")
    zip_buffer = BytesIO()

    with zipfile.ZipFile(zip_buffer, "w") as zip_file:
        if keyword or barcode:
            dpi = 300 if barcode else 150
            split_points = []
            for i in range(total_pages):
                writer = PyPDF2.PdfWriter()
                writer.add_page(reader.pages[i])
                buffer = BytesIO()
                writer.write(buffer)
                buffer.seek(0)
                image = convert_from_bytes(buffer.read(), dpi=dpi)[0]

                if keyword and keyword.lower() in extract_text(image).lower():
                    split_points.append(i)
                elif barcode and get_barcodes(image):
                    split_points.append(i)

                del image
                gc.collect()

            if not split_points:
                zip_file.writestr("full.pdf", contents)
            else:
                split_start = 0
                for sp in split_points + [total_pages]:
                    writer = PyPDF2.PdfWriter()
                    for i in range(split_start, sp):
                        writer.add_page(reader.pages[i])
                    buf = BytesIO()
                    writer.write(buf)
                    buf.seek(0)
                    zip_file.writestr(f"pages_{split_start+1}_{sp}.pdf", buf.read())
                    split_start = sp

        else:  # split_size
            for start_page in range(0, total_pages, split_size):
                end = min(start_page + split_size, total_pages)
                writer = PyPDF2.PdfWriter()
                for i in range(start_page, end):
                    writer.add_page(reader.pages[i])
                buf = BytesIO()
                writer.write(buf)
                buf.seek(0)
                zip_file.writestr(f"pages_{start_page+1}_{end}.pdf", buf.read())

    zip_buffer.seek(0)
    logger.info(f"Verwerkingstijd: {time.perf_counter() - start:.2f} s")
    return Response(
        content=zip_buffer.read(),
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=split_pages.zip"}
    )
