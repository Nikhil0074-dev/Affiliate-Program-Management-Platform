from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from . import models  # noqa: F401  (register tables)
from .api import affiliates, analytics, auth, catalog, finance, tracking
from .core.config import settings
from .database import Base, engine
from .utils import AppError


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(title="Affiliate Program Management Platform", version="1.0.0", lifespan=lifespan)
if settings.cors_origins:
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])


@app.exception_handler(AppError)
async def app_error(_: Request, exc: AppError):
    return JSONResponse({"detail": exc.detail}, status_code=exc.status)


@app.exception_handler(IntegrityError)
async def integrity_error(_: Request, exc: IntegrityError):
    return JSONResponse({"detail": "Conflict: the record already exists or violates a constraint"}, status_code=409)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    msgs = [f"{'.'.join(str(x) for x in e['loc'][1:])}: {e['msg']}" for e in exc.errors()]
    return JSONResponse({"detail": "; ".join(msgs) or "Invalid request"}, status_code=422)


@app.get("/api/health")
def health():
    return {"status": "ok"}


for r in (auth.router, affiliates.router, catalog.router, tracking.router, finance.router, analytics.router):
    app.include_router(r)

_frontend = Path(__file__).resolve().parents[2] / "frontend"
if _frontend.is_dir():
    app.mount("/", StaticFiles(directory=_frontend, html=True), name="frontend")
