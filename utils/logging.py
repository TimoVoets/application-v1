import logging


def get_logger(name: str) -> logging.Logger:
    if not logging.getLogger().hasHandlers():
        logging.basicConfig(level=logging.INFO)
    return logging.getLogger(name)
