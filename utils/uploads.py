import logging
from fastapi import UploadFile


async def read_upload_file(
    file: UploadFile, logger: logging.Logger, action: str = "PDF ontvangen"
) -> bytes:
    contents = await file.read()
    size_mb = len(contents) / 1024 / 1024
    logger.info(f"{action}: {file.filename} ({size_mb:.2f} MB)")
    return contents
