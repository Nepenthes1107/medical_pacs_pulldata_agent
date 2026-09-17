"""PACS domain agent package."""

# Keep imports lazy: legacy ``app.agent`` modules import domain state and nodes
# during compatibility loading, so eager graph imports create cycles.
__all__ = []
