# -*- coding: utf-8 -*-
"""FastAPI 应用入口"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.routers import documents, extraction, review, weknora, agent
from core_engine.exception_logger import ExceptionLogger

app = FastAPI(
    title="ZK Data Fabric 审核后台 API",
    description="医药数据采集与知识图谱审核系统",
    version="1.0.0",
)


@app.on_event("startup")
def _startup_recovery():
    """启动时清空 DB 残留状态 + Redis 残留队列，完全重置为干净状态。"""
    from db.repository import reset_stale_extractions
    from core_engine.task_queue import clear_extraction_redis

    reset_stale_extractions()
    clear_extraction_redis()

    # 注册 review_status 直写防护监听器
    import db.review_listeners  # noqa: F401

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
app.include_router(agent.router)


# ── 统一错误处理 ────────────────────────────────────────────────────────
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    ExceptionLogger.log(f"[API] 未处理异常 url={request.url}")
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
