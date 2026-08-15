"""FastAPI 应用装配：创建 app、配置 CORS、挂载各业务路由 + 前端静态文件。

启动：
    uv run uvicorn src.handler.app:app --reload --port 8000
    API 文档：http://localhost:8000/docs
    前端页面：http://localhost:8000/
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from src.config import settings


from src.handler import health, srt, subtitle_editor, tasks ,storage

from src.handler.deps import get_store
from src.service.runner import recover_interrupted_tasks

logger = logging.getLogger(__name__)

# 项目根目录下的 web 前端目录（向上两级即 src/handler/app.py -> src/handler -> 项目根）
_WEB_DIR = Path(__file__).resolve().parents[2] / "web"



def create_app() -> FastAPI:
    app = FastAPI(title="Subtitles AI API", version="0.1.0")

    # 本机工作台：只允许配置中的前端来源访问 API。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET", "POST", "DELETE", "PUT", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # 按业务挂载路由，后续新增业务在此 include 即可
    app.include_router(tasks.router)
    app.include_router(srt.router)
    app.include_router(storage.router)
    app.include_router(subtitle_editor.router)
    app.include_router(health.router)

    # 最后挂载前端静态文件（必须放在 API router 之后，否则会拦截 /api/*）。
    # html=True 让根路径直接返回 web/index.html，避免再开一个 http.server。
    if _WEB_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_WEB_DIR), html=True), name="web")
    else:
        logger.warning("前端目录不存在: %s，仅暴露 API", _WEB_DIR)

    @app.on_event("startup")
    def _scan_missing_terminal() -> None:
        """启动时校验终态资源，并恢复所有未完成任务。

        SUCCESS 任务的磁盘产物已不在时降级为 MISSING；PENDING 和处理中任务
        重新入队，由流水线根据已有中间产物断点续跑。两项操作都可重复执行。
        """
        downgraded = tasks.scan_missing_terminal(get_store(), data_dir=settings.data_dir)
        if downgraded:
            logger.warning(
                "启动扫描：以下任务产物已丢失，已降级为 MISSING: %s", downgraded
            )

        recovered = recover_interrupted_tasks()
        if recovered:
            logger.warning("启动恢复：以下未完成任务已重新入队: %s", recovered)

    return app


app = create_app()
