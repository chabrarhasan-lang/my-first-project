class BridgeFault(Exception):
    """A deliberately non-sensitive error safe to expose in a run summary."""

    def __init__(self, code, status=502, retryable=False, metrics=None):
        super().__init__(code)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.metrics = metrics or {}
