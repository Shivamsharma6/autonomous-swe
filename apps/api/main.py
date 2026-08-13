from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from apps.api.routes import router

app = FastAPI(title="Autonomous Software Engineering Control Plane API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(router)
