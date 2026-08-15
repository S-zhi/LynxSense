"""MCP 工具定义。

工具只编排业务 API，不直接访问 SQLite 或调用底层下载 / 翻译函数。
"""

from __future__ import annotations

import logging
from typing import Any, Literal
from urllib.parse import urlparse

from .business_client import BusinessApiClient, BusinessApiError

logger = logging.getLogger(__name__)


def _valid_video_url(url: str) -> bool:
    parsed = urlparse(url.strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _normalize_task(api: BusinessApiClient, data: dict[str, Any]) -> dict[str, Any]:
    outputs = data.get("outputs")
    if isinstance(outputs, dict):
        outputs = {
            key: api.absolute_url(value) if isinstance(value, str) else value
            for key, value in outputs.items()
        }
    return {
        "task_id": data.get("id"),
        "url": data.get("url"),
        "title": data.get("title"),
        "status": data.get("status"),
        "progress": data.get("progress", 0),
        "current_step": data.get("currentStep"),
        "error": data.get("error"),
        "resource_status": data.get("resourceStatus"),
        "outputs": outputs,
        "created_at": data.get("createdAt"),
        "updated_at": data.get("updatedAt"),
        "source_lang": data.get("sourceLang"),
        "target_lang": data.get("targetLang"),
        "mode": data.get("mode"),
        "burn": data.get("burn"),
        "need_subtitle": data.get("needSubtitle"),
    }


class SubtitleMcpTools:
    """可注入业务客户端的工具集合，便于单元测试。"""

    def __init__(self, api: BusinessApiClient) -> None:
        self.api = api

    async def check_subtitle_setup(self) -> dict[str, Any]:
        try:
            result = await self.api.readiness()
            if not result.get("ok"):
                result.setdefault(
                    "error_code",
                    "NOT_INITIALIZED"
                    if not result.get("initialized")
                    else "HARD_BURN_UNAVAILABLE",
                )
            return result
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP readiness 检查失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 检查业务服务时发生内部错误",
            }

    async def probe_video(self, url: str) -> dict[str, Any]:
        if not _valid_video_url(url):
            return {
                "ok": False,
                "error_code": "INVALID_URL",
                "message": "请输入以 http:// 或 https:// 开头的视频页面地址",
            }
        try:
            result = await self.api.probe_video(url.strip())
            return {
                "ok": bool(result.get("ok")),
                "probe": result,
                **({} if result.get("ok") else {"error_code": "PROBE_FAILED"}),
            }
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 视频探测失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 探测视频时发生内部错误",
            }

    async def start_subtitle_pipeline(
        self,
        url: str,
        *,
        source_lang: str = "auto",
        target_lang: str = "zh-CN",
        mode: Literal["mono", "bilingual"] = "mono",
        burn: Literal["hard", "soft"] = "hard",
        model: str = "small",
        need_subtitle: bool = True,
    ) -> dict[str, Any]:
        if not _valid_video_url(url):
            return {
                "ok": False,
                "error_code": "INVALID_URL",
                "message": "请输入以 http:// 或 https:// 开头的视频页面地址",
            }
        if mode not in {"mono", "bilingual"}:
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "mode 必须是 mono 或 bilingual"}
        if burn not in {"hard", "soft"}:
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "burn 必须是 hard 或 soft"}
        if not source_lang.strip() or not target_lang.strip() or not model.strip():
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "语言和模型参数不能为空"}

        try:
            readiness = await self.api.readiness()
            capabilities = readiness.get("capabilities", {})
            required_capability = "full_pipeline" if need_subtitle else "download"
            if not capabilities.get(required_capability, False):
                readiness.setdefault("error_code", "NOT_INITIALIZED")
                return readiness
            if need_subtitle and burn == "hard" and not capabilities.get("hard_burn", False):
                return {
                    "ok": False,
                    "error_code": "HARD_BURN_UNAVAILABLE",
                    "message": "当前 FFmpeg 不支持硬字幕滤镜，请安装带 libass 的 ffmpeg-full，或将 burn 设置为 soft",
                    "setup": readiness,
                }

            data = await self.api.create_task(
                {
                    "url": url.strip(),
                    "sourceLang": source_lang.strip(),
                    "targetLang": target_lang.strip(),
                    "mode": mode,
                    "burn": burn,
                    "model": model.strip(),
                    "engine": "deepseek",
                    "needSubtitle": need_subtitle,
                }
            )
            task = _normalize_task(self.api, data)
            return {
                "ok": True,
                **task,
                "message": "任务已创建，请使用 get_task_status 查询进度",
                "next_tool": "get_task_status",
            }
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 创建字幕流水线任务失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 创建任务时发生内部错误",
            }

    async def get_task_status(self, task_id: str) -> dict[str, Any]:
        if not task_id.strip():
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "task_id 不能为空"}
        try:
            return {"ok": True, **_normalize_task(self.api, await self.api.get_task(task_id.strip()))}
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 查询任务状态失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 查询任务时发生内部错误",
            }

    async def get_task_artifacts(self, task_id: str) -> dict[str, Any]:
        status = await self.get_task_status(task_id)
        if not status.get("ok"):
            return status
        if status.get("resource_status") == "MISSING":
            return {
                "ok": False,
                "error_code": "RESOURCE_MISSING",
                "task_id": task_id,
                "message": "任务产物已丢失，请重新运行任务",
            }
        if status.get("status") != "SUCCESS" or not status.get("outputs"):
            return {
                "ok": False,
                "error_code": "TASK_NOT_READY",
                "task_id": task_id,
                "status": status.get("status"),
                "progress": status.get("progress", 0),
                "message": "任务尚未成功完成，暂时没有可用产物",
            }

        filenames = {"video": f"{task_id}.mp4", "subtitle": f"{task_id}.srt"}
        artifacts = [
            {
                "type": kind,
                "filename": filenames.get(kind, f"{task_id}-{kind}"),
                "download_url": url,
            }
            for kind, url in status["outputs"].items()
        ]
        return {"ok": True, "task_id": task_id, "artifacts": artifacts}

    async def list_tasks(self, limit: int = 20) -> dict[str, Any]:
        if limit < 1 or limit > 100:
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "limit 必须在 1 到 100 之间"}
        try:
            data = await self.api.list_tasks()
            tasks = [_normalize_task(self.api, item) for item in data[:limit]]
            return {"ok": True, "count": len(tasks), "tasks": tasks}
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 查询任务列表失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 查询任务列表时发生内部错误",
            }

    async def retry_task(self, task_id: str) -> dict[str, Any]:
        if not task_id.strip():
            return {"ok": False, "error_code": "INVALID_ARGUMENT", "message": "task_id 不能为空"}
        try:
            task = _normalize_task(self.api, await self.api.retry_task(task_id.strip()))
            return {
                "ok": True,
                **task,
                "message": "失败任务已重新入队",
                "next_tool": "get_task_status",
            }
        except BusinessApiError as exc:
            return exc.to_result()
        except Exception:
            logger.exception("MCP 重试任务失败")
            return {
                "ok": False,
                "error_code": "INTERNAL_ERROR",
                "message": "MCP Server 重试任务时发生内部错误",
            }


def register_tools(server: Any, api: BusinessApiClient | None = None) -> SubtitleMcpTools:
    """把工具注册到 MCP Server，同时返回可直接测试的工具集合。"""
    service = SubtitleMcpTools(api or BusinessApiClient())

    @server.tool()
    async def check_subtitle_setup() -> dict[str, Any]:
        """检查业务服务、密钥、FFmpeg 和存储目录是否已初始化。"""
        return await service.check_subtitle_setup()

    @server.tool()
    async def probe_video(url: str) -> dict[str, Any]:
        """在不下载媒体文件的情况下检查视频 URL 是否可解析和下载。"""
        return await service.probe_video(url)

    @server.tool()
    async def start_subtitle_pipeline(
        url: str,
        source_lang: str = "auto",
        target_lang: str = "zh-CN",
        mode: Literal["mono", "bilingual"] = "mono",
        burn: Literal["hard", "soft"] = "hard",
        model: str = "small",
        need_subtitle: bool = True,
    ) -> dict[str, Any]:
        """异步启动下载、语音识别、字幕翻译和字幕烧录流水线。"""
        return await service.start_subtitle_pipeline(
            url,
            source_lang=source_lang,
            target_lang=target_lang,
            mode=mode,
            burn=burn,
            model=model,
            need_subtitle=need_subtitle,
        )

    @server.tool()
    async def get_task_status(task_id: str) -> dict[str, Any]:
        """查询任务状态、进度、当前阶段和错误信息。"""
        return await service.get_task_status(task_id)

    @server.tool()
    async def get_task_artifacts(task_id: str) -> dict[str, Any]:
        """获取成功任务的视频和译文字幕下载地址。"""
        return await service.get_task_artifacts(task_id)

    @server.tool()
    async def list_tasks(limit: int = 20) -> dict[str, Any]:
        """列出最近的字幕处理任务。"""
        return await service.list_tasks(limit)

    @server.tool()
    async def retry_task(task_id: str) -> dict[str, Any]:
        """重新执行一个失败的字幕处理任务。"""
        return await service.retry_task(task_id)

    return service
