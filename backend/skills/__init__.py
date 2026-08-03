"""
Skill Learning System.

A Skill is a named, reusable, replayable workflow the agent has learned --
from natural language, Teach Mode, a browser demonstration, an imported
recorded workflow, or a user correction. This package owns:

- backend.skills.library   -- SkillService: the Skill Library (CRUD,
  versioning, import/export/share, usage stats, pending "save as skill?"
  suggestions).
- backend.skills.matcher   -- SkillMatcher: "search the Skill Library before
  planning any task" semantic + keyword matching.
- backend.skills.runner    -- SkillRunner: deterministic replay of a
  matched skill's workflow against a live BrowserEngine.
- backend.skills.teach     -- TeachModeManager: interactive, chat-driven
  Teach Mode sessions, plus natural-language skill authoring and
  correction parsing (all via the LLM).

Nothing here changes how AgentLoop, TaskQueueService, ChatEngine, the
Telegram bot, or BrowserEngine work internally -- this package only
composes them, the same way backend/plugins and backend/memory do.
"""
