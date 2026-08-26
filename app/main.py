from fastapi import FastAPI, Depends
from fastapi.middleware.cors import CORSMiddleware
import logging
import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse

from app.database import create_tables
from app.routers import client, upload, overview, portfolio, performance, risk, insights

logging.basicConfig(level=logging.INFO)

app = FastAPI(
    title="WealthView Lite API",
    description="WealthView Lite - Portfolio Analytics and Management API",
    version="2.0.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    create_tables()


# Include routers with /api prefix
app.include_router(client.router, prefix="/api")
app.include_router(upload.router, prefix="/api")
app.include_router(overview.router, prefix="/api")
app.include_router(portfolio.router, prefix="/api")
app.include_router(performance.router, prefix="/api")
app.include_router(risk.router, prefix="/api")
app.include_router(insights.router, prefix="/api")

# Mount static frontend
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/static/clients.html")


@app.get("/api/health")
def health():
    return {"status": "healthy"}
