# -*- coding: utf-8 -*-
"""FastAPI 应用入口"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from loguru import logger

from api.routers import documents, extraction, review, weknora

app = FastAPI(
    title="ZK Data Fabric 审核后台 API",
    description="医药数据采集与知识图谱审核系统",
    version="1.0.0",
)

# ── CORS（前端本地开发需要）──────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:3000", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── 路由注册 ────────────────────────────────────────────────────────────
app.include_router(documents.router)
app.include_router(extraction.router)
app.include_router(review.router)
app.include_router(weknora.router)


# ── 统一错误处理 ────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"[API] 未处理异常: {request.url} | {exc}")
    return JSONResponse(
        status_code=500,
        content={"error": str(exc), "code": 500},
    )


@app.get("/health")
def health():
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
