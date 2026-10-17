"""Exceptions that drive node outcomes in the resolver."""


class BudgetExhausted(Exception):
    """An LLM call was refused because the run or node budget is spent. The node escalates."""


class RunCancelled(Exception):
    """The run's cancel flag was set. The node closes as cancelled."""


class NodeFailed(Exception):
    """A node could not continue, e.g. no valid JSON after the repair call. The node closes as failed."""


class LLMError(Exception):
    pass


class GitError(Exception):
    pass


class PatchError(Exception):
    pass
