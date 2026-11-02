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
    The token counters only add up what this attempt used; they feed the
    usage log and never limit anything.
    """

    def __init__(self, limit: int, used: int = 0) -> None:
        self.limit = limit
        self.used = used
        self.input_tokens = 0
        self.output_tokens = 0
        self.thought_tokens = 0

    def consume(self) -> None:
        """Reserve one request; raise ``VisionBudgetExceededError`` if none is left."""
        if self.used >= self.limit:
            raise VisionBudgetExceededError(
                f"Gemini request limit reached ({self.limit} per job)."
            )
        self.used += 1

    def add_usage(
        self,
        input_tokens: int | None,
        output_tokens: int | None,
        thought_tokens: int | None,
    ) -> None:
        """Add the token numbers of one answer (``None`` counts as zero)."""
        self.input_tokens += input_tokens or 0
        self.output_tokens += output_tokens or 0
        self.thought_tokens += thought_tokens or 0
