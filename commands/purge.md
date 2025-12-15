Run the session purge tool to reduce context usage by removing thinking blocks and truncating large tool outputs.

Execute:
```bash
~/.claude/hooks/claude-session-purge.py --current --verbose --restart
```

After running, report the results (bytes saved, blocks removed, repairs made).

Note: --restart will auto-kill and resume Claude in 3 seconds to apply the purged context. The session will restart automatically.
