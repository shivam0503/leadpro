from fastapi import APIRouter

from app.api.v1.auth import router as auth_router
from app.api.v1.companies import router as companies_router
from app.api.v1.users import router as users_router
from app.api.v1.crm import router as crm_router
from app.api.v1.dashboard import router as dashboard_router
from app.api.v1.health import router as health_router
from app.api.v1.capture import router as capture_router

router = APIRouter()

# Auth — no company scoping needed
router.include_router(auth_router, prefix="/v1")

# Super admin — company management
router.include_router(companies_router, prefix="/v1")

# Lead capture + webhooks (public — no auth)
router.include_router(capture_router, prefix="/v1")

# Company-scoped routes
router.include_router(users_router, prefix="/v1")
router.include_router(crm_router, prefix="/v1")
router.include_router(dashboard_router, prefix="/v1")
router.include_router(health_router, prefix="/v1")
