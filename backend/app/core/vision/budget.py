"""Hard ceiling on the number of Gemini requests a single job may make."""


class VisionBudgetExceededError(Exception):
    """Raised when a job has used up its Gemini request allowance.

    This is a deliberate exception to the "vision providers never raise"
    rule: swallowing it would let a runaway job keep spending, so it must
    reach the worker, which fails the job without queueing a retry.
    """


class VisionBudget:
    """Counts Gemini requests (retries included) against a fixed limit.

    ``used`` can start above zero: the worker restores the count a job has
    already spent in earlier attempts, so a retry cannot reset the ceiling.
    """

    def __init__(self, limit: int, used: int = 0) -> None:
        self.limit = limit
        self.used = used

    def consume(self) -> None:
        """Reserve one request; raise ``VisionBudgetExceededError`` if none is left."""
        if self.used >= self.limit:
            raise VisionBudgetExceededError(
                f"Gemini request limit reached ({self.limit} per job)."
            )
        self.used += 1
