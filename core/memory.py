"""Compatibility facade for legacy memory imports.

New code should depend on explicit services instead:
- workspace_service for workspace initialization
- profile_service for user.md
- record_service for post formatting/history
- todo_service for todos
"""

from __future__ import annotations

from core import db, profile_service, record_service, todo_service, workspace_service

BASE_DIR = str(db.BASE_DIR)
WORKSPACE_DIR = str(db.WORKSPACE_DIR)
USER_MD_PATH = profile_service.USER_MD_PATH
CONTEXT_POST_COUNT = record_service.CONTEXT_POST_COUNT
DEFAULT_USER_MD = profile_service.DEFAULT_USER_MD

init_workspace = workspace_service.init_workspace
save_post = record_service.save_post
format_post = record_service.format_post
read_recent_posts = record_service.read_recent_posts
read_profile = profile_service.read_profile
write_profile = profile_service.write_profile
load_todos = todo_service.load_todos
