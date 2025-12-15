#!/bin/bash
# Claude Code setup - sourced to enable 'c' alias and configure environment
#
# Usage:
#   source ~/.claude/setup.sh              # Use defaults
#   COMPACT_PCT=0.5 source ~/.claude/setup.sh  # Custom compact threshold
#
# To make permanent, add to your ~/.bashrc or ~/.zshrc:
#   source ~/.claude/setup.sh
#
# Or just add the alias directly:
#   alias c='claude --dangerously-skip-permissions'

# Configuration (can be overridden before sourcing)
: "${COMPACT_PCT:=80}"                     # Auto-compact threshold (percent)
: "${SKIP_PERMISSIONS:=true}"              # Enable --dangerously-skip-permissions

# Set environment variables
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="$COMPACT_PCT"

# Create the claude alias
if [[ "$SKIP_PERMISSIONS" == "true" ]]; then
    alias c='claude --dangerously-skip-permissions'
else
    alias c='claude'
fi

# Status message (only shown when sourced interactively)
if [[ $- == *i* ]]; then
    echo "Claude Code configured:"
    echo "  Alias: c -> claude${SKIP_PERMISSIONS:+ --dangerously-skip-permissions}"
    echo "  Auto-compact threshold: ${COMPACT_PCT}"
fi
