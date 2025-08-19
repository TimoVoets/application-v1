from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging

from rotate_pdf import router as rotate_router
from split_pdf import router as split_router
from prepare_pdf import router as prepare_router
from gmail_oauth import router as email_router  # jouw OAuth/router file

from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI()

# CORS: alleen productie-frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://dochero.nl",
    ],
    allow_credentials=False,  # zet True alleen als je cookies/credentials mee stuurt
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization"],
    max_age=600,
)

# Routers
app.include_router(rotate_router)
app.include_router(split_router)
app.include_router(prepare_router)
app.include_router(email_router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}
