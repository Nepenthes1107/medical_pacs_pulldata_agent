"""Compatibility facade for the PACS context module."""
from src.agents.pacs import context as _context

settings = _context.settings
_summarize = _context._summarize
_recent_start = _context._recent_start
_leading_group = _context._leading_group
count_tokens = _context.count_tokens
def manage_context(state):
    _context.settings = settings
    _context._summarize = _summarize
    return _context.manage_context(state)
capture_response = _context.capture_response
new_user_message = _context.new_user_message

__all__ = ["manage_context", "capture_response", "new_user_message", "count_tokens"]
