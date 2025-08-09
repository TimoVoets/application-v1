from fastapi import APIRouter, UploadFile, File, Response, HTTPException
from io import BytesIO
import PyPDF2
from pdf2image import convert_from_bytes
from PIL import Image, ImageOps
import numpy as np
import cv2
import logging
import gc

router = APIRouter()
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
Image.MAX_IMAGE_PIXELS = None

def deskew(pil_image: Image.Image) -> Image.Image:
    image = np.array(pil_image.convert("L"))
    _, thresh = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    coords = np.column_stack(np.where(thresh > 0))
    if coords.size == 0:
        return pil_image
    angle = cv2.minAreaRect(coords)[-1]
    angle = -(90 + angle) if angle < -45 else -angle
    M = cv2.getRotationMatrix2D((image.shape[1]//2, image.shape[0]//2), angle, 1.0)
    rotated = cv2.warpAffine(image, M, (image.shape[1], image.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return Image.fromarray(rotated)

@router.post("/prepare")
async def prepare_pdf(file: UploadFile = File(...)):
    contents = await file.read()
    logger.info(f"Ontvangen: {file.filename} ({len(contents)/1024/1024:.2f} MB)")

    reader = PyPDF2.PdfReader(BytesIO(contents))
    if not reader.pages:
        raise HTTPException(status_code=400, detail="Geen pagina's gevonden")

    writer = PyPDF2.PdfWriter()
    writer.add_page(reader.pages[0])
    buffer = BytesIO()
    writer.write(buffer)
    buffer.seek(0)

    image = convert_from_bytes(buffer.read(), dpi=200, fmt="ppm")[0]
    gray = ImageOps.grayscale(image)
    corrected = deskew(gray)

    pdf_buffer = BytesIO()
    corrected.convert("RGB").save(pdf_buffer, format="PDF")
    pdf_buffer.seek(0)

    del image, gray, corrected
    gc.collect()

    return Response(
        content=pdf_buffer.read(),
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename=page1_{file.filename}"}
    )
