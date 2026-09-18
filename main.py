from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from core.config import settings
from routers import (
    auth, users, water_locations, households, analytics,
    reports, notifications, inspections, resident_reports, forecast, map, hazards, weather
)

app = FastAPI(title="WaterWatch API", version="2.0.0")

from services.heatmap_service import start_heatmap_scheduler, stop_heatmap_scheduler

@app.on_event("startup")
async def on_startup():
    start_heatmap_scheduler()

@app.on_event("shutdown")
async def on_shutdown():
    stop_heatmap_scheduler()

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
app.include_router(hazards.router)
app.include_router(weather.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.PORT,
        reload=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
