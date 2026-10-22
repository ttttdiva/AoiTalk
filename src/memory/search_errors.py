"""Sanitized explicit-retrieval failures, without query or provider payloads."""


class MemorySearchUnavailable(RuntimeError):
    CODES = {
        "disabled",
        "index_unavailable",
        "encoder_unavailable",
        "backend_failed",
        "timeout",
    }

    def __init__(self, code: str = "backend_failed"):
        self.code = code if code in self.CODES else "backend_failed"
        super().__init__(self.code)
