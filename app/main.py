from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import logging
import os
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from fastapi.responses import JSONResponse

from app.database import create_tables
from app.auth import COOKIE_NAME, read_session
from app.routers import auth, client, upload, overview, portfolio, performance, risk, insights, market, analysis

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    create_tables()
    # Nightly fundamentals refresh at 01:00 IST. Set VIKABH_SCHEDULER=off when
    # running a second instance against the same database, so two processes do
    # not fetch the same securities over each other.
    if os.getenv("VIKABH_SCHEDULER", "on").strip().lower() not in ("off", "0", "false"):
        from app.scheduler import start as start_scheduler
        start_scheduler()
    yield


app = FastAPI(
    title="VIKABH Wealth Intelligence API",
    description="VIKABH - Portfolio Analytics and Wealth Intelligence API",
    version="2.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def protect_api(request, call_next):
    path = request.url.path
    if not path.startswith("/api/") or path in {
        "/api/auth/login", "/api/auth/signup", "/api/auth/logout", "/api/health"
    }:
        return await call_next(request)

    user = read_session(request.cookies.get(COOKIE_NAME))
    if not user:
        return JSONResponse({"detail": "Please sign in."}, status_code=401)
    request.state.session = user

    parts = path.strip("/").split("/")
    method = request.method
    admin_only = (
        path in {"/api/client", "/api/client/", "/api/market/import", "/api/refresh/run"}
        or path.startswith("/api/refresh/")
        or path.endswith("/access")
        or (len(parts) == 3 and parts[1] == "client" and method == "DELETE")
    )
    if admin_only and user["role"] != "admin":
        return JSONResponse({"detail": "Administrator access required."}, status_code=403)
    if path in {"/api/client", "/api/client/"} and method == "GET" and user["role"] != "admin":
        return JSONResponse({"detail": "Administrator access required."}, status_code=403)

    client_id = None
    if len(parts) == 3 and parts[1] in {"client", "overview", "portfolio", "performance", "risk", "insights"}:
        try:
            client_id = int(parts[2])
        except ValueError:
            pass
    elif len(parts) == 4 and parts[1] == "market" and parts[2] == "coverage":
        try:
            client_id = int(parts[3])
        except ValueError:
            pass

    if client_id is not None and user["role"] != "admin" and client_id != user.get("client_id"):
        return JSONResponse({"detail": "You can only access your own portfolio."}, status_code=403)
    return await call_next(request)


# Include routers with /api prefix
app.include_router(auth.router, prefix="/api")
app.include_router(client.router, prefix="/api")
app.include_router(upload.router, prefix="/api")
app.include_router(overview.router, prefix="/api")
app.include_router(portfolio.router, prefix="/api")
app.include_router(performance.router, prefix="/api")
app.include_router(risk.router, prefix="/api")
app.include_router(insights.router, prefix="/api")
app.include_router(market.router, prefix="/api")
app.include_router(analysis.router, prefix="/api")

# Mount static frontend
frontend_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")
if os.path.exists(frontend_dir):
    app.mount("/static", StaticFiles(directory=frontend_dir), name="static")


@app.get("/")
def root():
    return RedirectResponse(url="/static/login.html")


@app.get("/api/health")
def health():
    return {"status": "healthy"}


@app.get("/api/refresh/status", tags=["Refresh"])
def refresh_status():
    """When the nightly fundamentals refresh last ran and when it next will."""
    from app.scheduler import status
    return status()


@app.post("/api/refresh/run", tags=["Refresh"])
def refresh_run():
    """Run the nightly refresh now, without waiting for 01:00 IST."""
    from app.scheduler import run_now
    return run_now()
