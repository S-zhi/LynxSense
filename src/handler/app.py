"""FastAPI 应用装配：创建 app、配置 CORS、挂载各业务路由。

启动：
    uv run uvicorn src.handler.app:app --reload --port 8000
    文档： http://localhost:8000/docs
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.config import settings

from src.handler import health, srt, subtitle_editor, tasks

from src.handler.deps import get_store

logger = logging.getLogger(__name__)



def create_app() -> FastAPI:
    app = FastAPI(title="Subtitles AI API", version="0.1.0")

    # 本机工作台：只允许配置中的前端来源访问 API。
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.cors_allow_origins),
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )

    # 按业务挂载路由，后续新增业务在此 include 即可
    app.include_router(tasks.router)
    app.include_router(srt.router)
    app.include_router(subtitle_editor.router)
    app.include_router(health.router)

    @app.on_event("startup")
    def _scan_missing_terminal() -> None:
        """启动时扫一遍终态 SUCCESS 任务，丢失资源的降级为 MISSING。

        解决问题：服务重启后，磁盘产物已不在的"成功"任务不再被当作可用。
        该操作幂等；运行中任务（status != SUCCESS）不会被触碰。
        """
        downgraded = tasks.scan_missing_terminal(get_store(), data_dir=settings.data_dir)
        if downgraded:
            logger.warning(
                "启动扫描：以下任务产物已丢失，已降级为 MISSING: %s", downgraded
            )

    return app


app = create_app()
