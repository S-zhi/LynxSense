"""任务执行器（方案 A：后台线程池）。

API 层 POST/retry 时调 enqueue_pipeline(task_id)，本模块把任务丢进线程池异步执行，
HTTP 请求立即返回。执行过程中通过 on_event 把状态/进度写回 SQLite，
SSE 端点轮询库表即可拿到实时进度。

把"执行"隔离在这一处：API 层不感知用线程还是队列，
将来换 RQ + Redis 只改本文件的 enqueue_pipeline / 提交方式。
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor

from src.config import settings
from src.service.orchestrator import PipelineEvent, PipelineParams, run_pipeline
from src.service.asset_resolver import ResourceError
from src.store import STATUSES, TaskStore

logger = logging.getLogger(__name__)

# 线程池 + 独立 store 实例（SQLite WAL 支持多连接并发；各操作短连接）
_executor = ThreadPoolExecutor(
    max_workers=settings.pipeline_workers,
    thread_name_prefix="pipeline",
)
_store = TaskStore(settings.db_path)
_RECOVERABLE_STATUSES = set(STATUSES) - {"SUCCESS", "FAILED"}


def enqueue_pipeline(task_id: str) -> None:
    """提交一个任务去后台执行（不阻塞调用方）。"""
    _executor.submit(_run, task_id)
    logger.info("已入队: %s", task_id)


def recover_interrupted_tasks() -> list[str]:
    """服务启动时重新提交未完成任务，避免任务永久停留在处理中。"""
    recovered: list[str] = []
    for rec in _store.list():
        if rec.status not in _RECOVERABLE_STATUSES:
            continue
        enqueue_pipeline(rec.id)
        recovered.append(rec.id)
    return recovered


def _run(task_id: str) -> None:
    """线程内执行：读记录 → 跑五步 → 进度写库。"""
    rec = _store.get(task_id)
    if rec is None:
        logger.warning("任务不存在，跳过执行: %s", task_id)
        return

    params = PipelineParams(
        task_id=rec.id,
        url=rec.url,
        source_lang=rec.source_lang,
        target_lang=rec.target_lang,
        mode=rec.mode,
        burn=rec.burn,
        model=rec.model,
        engine=rec.engine,
        source_type=rec.source_type,
        need_subtitle=bool(rec.need_subtitle),
        title=rec.title,
    )

    def on_event(ev: PipelineEvent) -> None:
        fields: dict = {
            "status": ev.status,
            "progress": ev.progress,
            "current_step": ev.current_step,
        }
        if ev.title is not None:
            fields["title"] = ev.title
        if ev.error is not None:
            fields["error"] = ev.error
        if ev.outputs:
            fields["output_video"] = ev.outputs.get("video")
            fields["output_subtitle"] = ev.outputs.get("subtitle")
        _store.update(task_id, **fields)

    try:
        run_pipeline(params, on_event, api_key=settings.deepseek_api_key)
    except ResourceError as e:
        logger.error("任务由于资源异常执行失败: %s - %s", task_id, str(e))
        cur = _store.get(task_id)
        if cur is not None and cur.status != "FAILED":
            _store.update(task_id, status="FAILED", error=str(e))
    except Exception:
        # run_pipeline 失败时已通过 on_event 写过 FAILED；这里兜底再确保一次
        logger.exception("流水线执行失败: %s", task_id)
        cur = _store.get(task_id)
        if cur is not None and cur.status != "FAILED":
            _store.update(task_id, status="FAILED", error="执行异常")
