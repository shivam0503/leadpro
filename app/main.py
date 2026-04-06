"""
app/main.py — LeadPro SaaS Multi-tenant
"""

from pathlib import Path
from fastapi.responses import HTMLResponse, FileResponse
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse

from app.core.config import settings
from app.core.errors import install_error_handlers
from app.core.middleware import request_id_middleware, rate_limit_middleware
from app.services.database import startup
from app.api.routes import router as api_router

# ── Base Paths ─────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

app = FastAPI(
    title=settings.APP_NAME,
    description="LeadPro AI CRM — Multi-tenant SaaS",
    version="2.0.0",
    root_path=settings.APP_PREFIX
)

# Trust X-Forwarded-Proto / X-Forwarded-Host from reverse proxies (nginx, Caddy, etc.)
# so that request.base_url returns the public URL, not http://localhost:PORT
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts="*")

# ── Middleware ─────────────────────────────────────────────────────────────────
app.middleware("http")(rate_limit_middleware)
app.middleware("http")(request_id_middleware)
install_error_handlers(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Company slug middleware ────────────────────────────────────────────────────
@app.middleware("http")
async def inject_company_slug(request: Request, call_next):
    from app.core.security import decode_token

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            payload = decode_token(auth[7:])
            slug = payload.get("company_slug")
            if slug:
                request.state.company_slug = slug
        except Exception:
            pass

    header_slug = request.headers.get("X-Company-Slug")
    if header_slug and not getattr(request.state, "company_slug", None):
        request.state.company_slug = header_slug

    return await call_next(request)

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def on_startup():
    startup()

# ── API Routes ────────────────────────────────────────────────────────────────
app.include_router(api_router, prefix="/api")

# ── Root ──────────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
def root():
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return FileResponse(str(html_path))
    return HTMLResponse("<h1>LeadPro AI CRM</h1>")

# ── Static UI (must be last!) ──────────────────────────────────────────────────
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")

# ── OpenAPI Auth ──────────────────────────────────────────────────────────────
from fastapi.openapi.utils import get_openapi

def custom_openapi():
    if app.openapi_schema:
        return app.openapi_schema
    schema = get_openapi(
        title="LeadPro AI CRM",
        version="2.0.0",
        description="""
## LeadPro AI CRM — Multi-tenant SaaS

### Quick Start
1. `POST /api/v1/auth/bootstrap` — create super admin
2. `POST /api/v1/auth/super-login` — get super admin token
3. `POST /api/v1/companies` — create your first client company
4. `POST /api/v1/companies/{slug}/admin` — create company admin user
5. `POST /api/v1/auth/login` — company user login (include company_slug)

### Authentication
All endpoints require Bearer JWT. Include `company_slug` in login for company users.
        """,
        routes=app.routes,
    )
    schema["components"]["securitySchemes"] = {
        "BearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
    }
    schema["security"] = [{"BearerAuth": []}]
    app.openapi_schema = schema
    return app.openapi_schema

app.openapi = custom_openapi
