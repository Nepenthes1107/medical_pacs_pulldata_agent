"""Compatibility facade preserving the legacy loop module's patchable symbols."""
from src.agents.pacs.nodes import loop as _loop
from src.agents.pacs.nodes.loop import *

_fingerprint = _loop._fingerprint
_observation_signature = _loop._observation_signature
_run_tool_call = _loop._run_tool_call
_pending_with_message = _loop._pending_with_message
_tool_result_message = _loop._tool_result_message
