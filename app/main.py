# =============================================================================
# NestEgg - app/main.py
# FastAPI application entrypoint. Registers routers, middleware, and lifespan.
# =============================================================================

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.database import lifespan
from app.routers import scenarios, projection, optimizer, social_security, tax, inputs


app = FastAPI(
    title="NestEgg API",
    description="Retirement planning calculator — projection, optimization, and tax modeling.",
    version="1.0.0",
    root_path="/api",
    lifespan=lifespan,
)

# ---------------------------------------------------------------------------
# CORS — allow the frontend dev server and production origin
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",  # Vite dev server
        "http://localhost:3000",
        "http://nestegg.local",  # Production self-hosted
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------------------------------------------------------------------
# Routers
# ---------------------------------------------------------------------------
app.include_router(scenarios.router, prefix="/v1")
app.include_router(inputs.router, prefix="/v1")
app.include_router(projection.router, prefix="/v1")
app.include_router(optimizer.router, prefix="/v1")
app.include_router(social_security.router, prefix="/v1")
app.include_router(tax.router, prefix="/v1")


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------
@app.get("/health", tags=["health"])
async def health():
    return {"status": "ok", "service": "nestegg-api"}
