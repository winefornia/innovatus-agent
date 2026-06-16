"""Tasting-room coordinator agent (Vertex ADK migration — parallel to LangGraph).

Importing `agent` is deferred so the rest of the codebase (which does NOT yet
depend on google-adk) keeps importing cleanly until the migration cuts over.
"""
