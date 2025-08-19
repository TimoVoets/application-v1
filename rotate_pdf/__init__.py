from fastapi import APIRouter, UploadFile, File, Response, HTTPException
from pdf2image import convert_from_bytes
from pdf2image.exceptions import PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError
from PIL import Image
from pytesseract import TesseractError
import pytesseract
import logging
import io
import time
import gc
from utils.logging import get_logger
from utils.uploads import read_upload_file

router = APIRouter()
logger = get_logger(__name__)

def detect_rotation_angle(image: Image.Image) -> int:
    try:
        osd = pytesseract.image_to_osd(image)
        for line in osd.splitlines():
            if "Rotate" in line:
                return int(line.split(":")[1].strip())
    except TesseractError as e:
        logger.exception("Fout bij rotatiedetectie", exc_info=e)
    return 0

def correct_image_rotation(pil_image: Image.Image, angle: int) -> Image.Image:
    return pil_image.rotate(-angle, expand=True) if angle in [90, 180, 270] else pil_image

@router.post("/rotate")
async def rotate_pdf(file: UploadFile = File(...)):
    start_time = time.perf_counter()

    try:
        contents = await read_upload_file(file, logger)

        images = convert_from_bytes(contents, dpi=150)
        output_buffer = io.BytesIO()
        first_page = True

        for i, img in enumerate(images):
            logger.info(f"Pagina {i + 1}/{len(images)} verwerken")
            angle = detect_rotation_angle(img)
            rotated = correct_image_rotation(img, angle).convert("RGB")

            temp_buffer = io.BytesIO()
            rotated.save(temp_buffer, format="PDF")
            temp_buffer.seek(0)

            if first_page:
                output_buffer.write(temp_buffer.read())
                first_page = False
            else:
                output_buffer.write(temp_buffer.read())

            del img, rotated, temp_buffer
            gc.collect()

        output_buffer.seek(0)
        logger.info(f"Verwerkingstijd: {time.perf_counter() - start_time:.2f} s")
        return Response(
            content=output_buffer.read(),
            media_type="application/pdf",
            headers={"Content-Disposition": "attachment; filename=rotated_output.pdf"}
        )

    except (PDFInfoNotInstalledError, PDFPageCountError, PDFSyntaxError) as e:
        logger.error("PDF-verwerking mislukt", exc_info=e)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception:
        logger.exception("Onverwachte fout bij roteren van PDF")
        raise HTTPException(status_code=500, detail="Internal server error")
