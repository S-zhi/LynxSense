"""任务执行器（方案 A：后台线程池）。

API 层 POST/retry 时调 enqueue_pipeline(task_id)，本模块把任务丢进线程池异步执行，
HTTP 请求立即返回。执行过程中通过 on_event 把状态/进度写回 SQLite，
SSE 端点轮询库表即可拿到实时进度。

把"执行"隔离在这一处：API 层不感知用线程还是队列，
将来换 RQ + Redis 只改本文件的 enqueue_pipeline / 提交方式。
"""

from __future__ import annotations

import logging
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor

from src.config import settings
from src.service.orchestrator import PipelineEvent, PipelineParams, run_pipeline
from src.service.asset_resolver import ResourceError
from src.store import STATUSES, TaskStore, TranslationEngineStore

logger = logging.getLogger(__name__)

# 线程池状态（延迟/懒构造 + 动态扩缩容）
_executor: ThreadPoolExecutor | None = None
_current_max_workers: int = 0
_executor_lock = threading.Lock()

_store = TaskStore(settings.db_path)


def get_executor() -> ThreadPoolExecutor:
    """获取或平滑重构后台任务线程池。

    当 settings.pipeline_workers 发生变化时，关闭旧线程池（等已有任务跑完），
    并按新配置创建新线程池。
    """
    global _executor, _current_max_workers
    target_workers = settings.pipeline_workers
    with _executor_lock:
        if _executor is None or (
            hasattr(_executor, "_max_workers") and _current_max_workers != target_workers
        ):
            old_executor = _executor
            _executor = ThreadPoolExecutor(
                max_workers=target_workers,
                thread_name_prefix="pipeline",
            )
            _current_max_workers = target_workers
            if old_executor is not None and hasattr(old_executor, "shutdown"):
                try:
                    old_executor.shutdown(wait=False)
                except Exception as e:
                    logger.warning("关闭旧线程池失败: %s", e)
            logger.info("流水线线程池就绪/已更新: max_workers=%d", target_workers)
        return _executor


def shutdown_executor(wait: bool = True) -> None:
    """关闭当前线程池。"""
    global _executor, _current_max_workers
    with _executor_lock:
        if _executor is not None:
            if hasattr(_executor, "shutdown"):
                try:
                    _executor.shutdown(wait=wait)
                except Exception as e:
                    logger.warning("关闭线程池失败: %s", e)
            _executor = None
            _current_max_workers = 0
            logger.info("流水线线程池已关闭")
_RECOVERABLE_STATUSES = set(STATUSES) - {"SUCCESS", "FAILED", "CANCELLED"}
_engine_store = TranslationEngineStore(settings.db_path)

_procs: dict[str, list[subprocess.Popen]] = {}
_procs_lock = threading.Lock()


def register_process(task_id: str, proc: subprocess.Popen) -> None:
    """注册运行中的子进程（如 ffmpeg），以便任务取消时终止。"""
    with _procs_lock:
        _procs.setdefault(task_id, []).append(proc)


def unregister_process(task_id: str, proc: subprocess.Popen) -> None:
    """移除已退出的子进程。"""
    with _procs_lock:
        if task_id in _procs:
            try:
                _procs[task_id].remove(proc)
            except ValueError:
                pass
            if not _procs[task_id]:
                del _procs[task_id]


def cancel_pipeline(task_id: str) -> bool:
    """取消运行中的任务，终止其关联子进程并更新状态为 CANCELLED。"""
    rec = _store.get(task_id)
    if rec is None:
        return False

    _store.update(task_id, status="CANCELLED", error="用户取消")

    with _procs_lock:
        procs = _procs.pop(task_id, [])

    for proc in procs:
        try:
            proc.terminate()
        except Exception as e:
            logger.warning("终止子进程失败: task=%s, err=%s", task_id, e)

    logger.info("任务已取消: %s", task_id)
    return True


def enqueue_pipeline(task_id: str) -> None:
    """提交一个任务去后台执行（不阻塞调用方）。"""
    get_executor().submit(_run, task_id)
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
    if rec.status == "CANCELLED":
        logger.info("任务已被取消，跳过执行: %s", task_id)
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
        cur = _store.get(task_id)
        if cur is not None and cur.status == "CANCELLED":
            return
        fields: dict = {
            "status": ev.status,
            "progress": ev.progress,
            "current_step": ev.current_step,
        }
        if ev.title is not None:
            fields["title"] = ev.title
        if ev.error is not None:
            fields["error"] = ev.error
        if ev.error_code is not None:
            fields["error_code"] = ev.error_code
        if ev.outputs:
            fields["output_video"] = ev.outputs.get("video")
            fields["output_subtitle"] = ev.outputs.get("subtitle")
        _store.update(task_id, **fields)

    engine_config = None
    if rec.engine != "deepseek":
        engine_config = _engine_store.get(rec.engine)
        if engine_config is None:
            _store.update(task_id, status="FAILED", error="翻译引擎配置不存在", error_code="engine_not_found")
            return
    try:
        pipeline_kwargs = {
            "api_key": settings.deepseek_api_key if rec.engine == "deepseek" else None,
        }
        # 仅在新引擎配置存在时传入扩展参数，保持旧版测试/调用方兼容。
        if engine_config is not None:
            pipeline_kwargs["engine_config"] = engine_config
        run_pipeline(params, on_event, **pipeline_kwargs)
    except ResourceError as e:
        cur = _store.get(task_id)
        if cur is not None and cur.status == "CANCELLED":
            logger.info("任务已被取消: %s", task_id)
            return
        logger.error("任务由于资源异常执行失败: %s - %s", task_id, str(e))
        if cur is not None and cur.status != "FAILED":
            _store.update(task_id, status="FAILED", error=str(e), error_code=getattr(e, "code", "resource_error"))
    except Exception as exc:
        cur = _store.get(task_id)
        if cur is not None and cur.status == "CANCELLED":
            logger.info("任务已被取消: %s", task_id)
            return
        logger.exception("流水线执行失败: %s", task_id)
        if cur is not None and cur.status != "FAILED":
            err_code = getattr(exc, "code", "execution_error")
            _store.update(task_id, status="FAILED", error=str(exc) or "执行异常", error_code=err_code)
    finally:
        with _procs_lock:
            _procs.pop(task_id, None)
