"""Fakes for the HTTP layer, so the tests need no network."""

from __future__ import annotations

import json
from typing import Any

from calotype import Request, Response


class FakeTransport:
    """Records each request and answers from a queue."""

    def __init__(self, *answers: Response | Exception) -> None:
        self.answers = list(answers)
        self.requests: list[Request] = []

    def __call__(self, request: Request) -> Response:
        self.requests.append(request)
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def last(self) -> Request:
        return self.requests[-1]

    def last_json(self) -> Any:
        return json.loads(self.last.body or b"null")


def json_response(status: int, data: Any, headers: dict[str, str] | None = None) -> Response:
    return Response(status, {"content-type": "application/json", **(headers or {})}, json.dumps(data).encode())


def error_response(status: int, code: str, message: str, headers: dict[str, str] | None = None, **extra: Any) -> Response:
    return json_response(status, {"error": {"code": code, "message": message, **extra}}, headers)
