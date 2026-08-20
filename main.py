from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import settings
from routers import (
    auth, users, water_locations, households, analytics,
    reports, notifications, inspections, resident_reports, forecast, map
)

app = FastAPI(title="WaterWatch API", version="2.0.0")

# CORS Setup
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Root endpoint
@app.get("/")
def root():
    return {"message": "WaterWatch API is running"}

# Include Routers
app.include_router(auth.router)
app.include_router(users.router)
app.include_router(water_locations.router)
app.include_router(households.router)
app.include_router(analytics.router)
app.include_router(reports.router)
app.include_router(notifications.router)
app.include_router(inspections.router)
app.include_router(resident_reports.router)
app.include_router(forecast.router)
app.include_router(map.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
