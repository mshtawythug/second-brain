"""GraphRAG storage/traversal backends (spec §4 D10, §8).

:class:`GraphBackend` is the narrow Protocol; :class:`AgeBackend` is the default
Apache AGE implementation. A Neo4j/Memgraph kill-switch backend (spec §16) would
land here too, conforming to the same Protocol.
"""
from .age import AgeBackend
from .base import GraphBackend, PersonScope, TraversalHit

__all__ = ["AgeBackend", "GraphBackend", "PersonScope", "TraversalHit"]
