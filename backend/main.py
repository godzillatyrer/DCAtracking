"""
FastAPI Application — main entry point.

Serves the REST API and React frontend (static files).
"""

import logging
import os
import sys

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from backend.database import Base, engine
from backend.api.dashboard import router as dashboard_router
from backend.api.tokens import router as tokens_router
from backend.api.wallets import router as wallets_router
from backend.api.alerts import router as alerts_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# Create tables on startup
Base.metadata.create_all(bind=engine)

app = FastAPI(
    title="BSC Pump Scanner",
    description="Real-time BSC token pump detection scanner",
    version="1.0.0",
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routes
app.include_router(dashboard_router)
app.include_router(tokens_router)
app.include_router(wallets_router)
app.include_router(alerts_router)


@app.get("/api/health")
def health_check():
    """Health check endpoint for Render."""
    return {"status": "healthy", "service": "bsc-pump-scanner"}


# Serve React frontend (production)
frontend_build = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend", "build")
if os.path.isdir(frontend_build):
    app.mount("/static", StaticFiles(directory=os.path.join(frontend_build, "static")), name="static")

    @app.get("/{full_path:path}")
    async def serve_react(full_path: str):
        """Serve React SPA for any non-API route."""
        file_path = os.path.join(frontend_build, full_path)
        if os.path.isfile(file_path):
            return FileResponse(file_path)
        return FileResponse(os.path.join(frontend_build, "index.html"))
