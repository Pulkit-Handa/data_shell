"""Small ASGI middleware for request-body limits.

Starlette's UploadFile spools multipart bodies before endpoint code runs, so a
limit implemented only while copying UploadFile chunks is too late to protect
against an oversized multipart request. This middleware rejects oversized
Content-Length values early and also counts chunked bodies as they are received.
"""
from __future__ import annotations

import json
import asyncio
from typing import Any, Awaitable, Callable

ASGIApp = Callable[[dict[str, Any], Callable[..., Awaitable[dict[str, Any]]], Callable[..., Awaitable[None]]], Awaitable[None]]


class _BodyTooLarge(Exception):
    pass


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_bytes: int, max_concurrent_requests: int = 2, max_control_body_bytes: int | None = None, preflight_auth=None) -> None:
        if max_body_bytes <= 0:
            raise ValueError("max_body_bytes must be positive")
        if max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests must be positive")
        if max_control_body_bytes is None:
            max_control_body_bytes = min(int(max_body_bytes), 1024 * 1024)
        if max_control_body_bytes <= 0 or max_control_body_bytes > max_body_bytes:
            raise ValueError("max_control_body_bytes must be positive and no greater than max_body_bytes")
        self.app = app
        self.max_body_bytes = int(max_body_bytes)
        self.max_control_body_bytes = int(max_control_body_bytes)
        self._request_slots = asyncio.Semaphore(max_concurrent_requests)
        self.preflight_auth = preflight_auth

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http" or scope.get("method", "GET").upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        path = str(scope.get("path", ""))
        request_limit = self.max_body_bytes
        if path in {"/api/v1/contracts", "/api/v1/admin/keys"}:
            request_limit = self.max_control_body_bytes
        content_length = headers.get(b"content-length")
        if content_length:
            try:
                declared_length = int(content_length)
            except (TypeError, ValueError):
                await self._reject(send, 400, "Invalid Content-Length header")
                return
            if declared_length < 0:
                await self._reject(send, 400, "Invalid Content-Length header")
                return
            if declared_length > request_limit:
                await self._reject(send, 413, "Request body exceeds the configured size limit")
                return

        if self.preflight_auth is not None:
            auth_status, metadata, auth_detail = self.preflight_auth(scope)
            if auth_status is not None:
                await self._reject(send, auth_status, auth_detail)
                return
            if metadata is not None:
                scope.setdefault("state", {})["preauthenticated_client"] = metadata

        if self._request_slots.locked():
            await self._reject(send, 503, "Request capacity is currently exhausted; retry later")
            return

        total = 0
        response_started = False

        async def counted_receive():
            nonlocal total
            message = await receive()
            if message.get("type") == "http.request":
                total += len(message.get("body", b""))
                if total > request_limit:
                    raise _BodyTooLarge
            return message

        async def tracked_send(message):
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        try:
            async with self._request_slots:
                await self.app(scope, counted_receive, tracked_send)
        except _BodyTooLarge:
            # Request parsers consume the complete body before endpoint handlers
            # emit a response. If a downstream app has already started a response,
            # do not attempt an invalid second response.
            if not response_started:
                await self._reject(send, 413, "Request body exceeds the configured size limit")

    @staticmethod
    async def _reject(send, status_code: int, detail: str) -> None:
        body = json.dumps({"detail": detail}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": status_code,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})
