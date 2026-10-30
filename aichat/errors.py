from __future__ import annotations


class ApiError(Exception):
    """统一的业务错误：HTTP 状态 + 机器可读错误码 + 说明。"""

    def __init__(self, status: int, code: str, message: str = "", **extra):
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code
        self.extra = extra

    def body(self) -> dict:
        return {"error": {"code": self.code, "message": self.message, **self.extra}}
