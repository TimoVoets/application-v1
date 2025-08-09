from fastapi import FastAPI
from rotate_pdf import router as rotate_router
from split_pdf import router as split_router
from prepare_pdf import router as prepare_router
from gmail_oauth import router as gmail_router

from dotenv import load_dotenv
load_dotenv()  

app = FastAPI()

# Routers
app.include_router(rotate_router)
app.include_router(split_router)
app.include_router(prepare_router)
app.include_router(gmail_router)

@app.get("/health")
async def health_check():
    return {"status": "ok"}
