"""illustro - Anime illustration collection tagging + semantic search + analytics.

Pipeline: scan (import) -> tag (WD14 tagging + image embeddings) -> index (vector index) -> serve (search/analytics UI).
Fully incremental: re-running only processes new or changed files.
"""

__version__ = "0.1.0"
