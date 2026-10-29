from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api import chat, llm, projects, query
from .config import ALLOWED_ORIGINS, ensure_layout


ensure_layout()

app = FastAPI(
    title="NavelMaker 2 Local API",
    version="0.1.0",
    description="网站前端使用的本地小说世界处理与模拟 API。",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(projects.router)
app.include_router(query.router)
app.include_router(chat.router)
app.include_router(llm.router)


@app.get("/")
def root():
    return {
        "name": "NavelMaker 2 Local API",
        "status": "ok",
        "docs": "/docs",
    }


@app.get("/health")
def health():
    return {"status": "ok"}
