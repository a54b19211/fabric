# -*- coding: utf-8 -*-
"""FastAPI 应用入口"""
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from api.exception_logger import ExceptionLogger
from api.routers import agent, documents, extraction, history, measurements, research, review, weknora

app = FastAPI(
    title="ZK Data Fabric 审核后台 API",
    description="医药数据采集与知识图谱审核系统",
    version="1.0.0",
)


@app.on_event("startup")
def _register_review_listeners():
    """API 重启时 Worker 可能仍在执行，不能清理队列或重置任务。"""
    import graph.db.review_listeners  # noqa: F401

@app.on_event("startup")
def _validate_source_configuration():
    """A configuration the service cannot honour stops it at start, not at the first request: the index targets must not
    name a retired source."""
    from store import index_publication

    index_publication.targets()


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
app.include_router(research.router)
app.include_router(history.router)
app.include_router(measurements.router)


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
