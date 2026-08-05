"""Small source fixture used by the no-model publish Action smoke."""


def repository_name() -> str:
    """Return a deterministic value that BM25 can index."""

    return "CodeNib"
