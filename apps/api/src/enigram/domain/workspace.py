"""Workspace names: a workspace holds one project's documents, repositories, notes, jobs, query
log and daily budget."""

import re

from enigram.domain.errors import EnigramError

# Safe as a folder and a URL segment; no "_", which `index --pages` uses for <ws>_dev.
WORKSPACE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


def check_workspace_name(name: str) -> str:
    if not WORKSPACE_NAME.fullmatch(name):
        raise EnigramError(
            "A workspace name is 1-32 lowercase letters, digits or '-', "
            f"starting with a letter or digit; got {name!r}"
        )
    return name
