Run the session purge tool to reduce context usage by removing thinking blocks and truncating large tool outputs.

Execute:
```bash
~/.claude/hooks/claude-session-purge.py --current --verbose
```

After running, report the results (bytes saved, blocks removed, repairs made).

If the purge was successful and freed significant space, acknowledge that context pressure should be reduced.
