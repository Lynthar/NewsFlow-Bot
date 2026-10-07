"""``${ENV_VAR}`` references in configured values, resolved from the process environment
when the value is used, so the secret itself stays out of the YAML, the database and the
logs."""

import os
import re

_ENV_REF_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_env_refs(value: str, where: str) -> str:
    """`value` with every ``${VAR}`` replaced by that variable's value.

    Raises:
        ValueError: a referenced variable is unset. The message names `where` and the
            variable, never a value — these are tokens more often than not.
    """

    def expand(match: re.Match[str]) -> str:
        resolved = os.environ.get(match.group(1))
        if resolved is None:
            raise ValueError(
                f"{where} references environment variable {match.group(1)!r}, which is not set"
            )
        return resolved

    return _ENV_REF_RE.sub(expand, value)
