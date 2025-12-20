#!/bin/bash
# Claude Code setup - sourced to enable 'c' function and configure environment
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
: "${COMPACT_PCT:=95}"                     # Auto-compact threshold (percent) - 95% = 5% buffer
: "${SKIP_PERMISSIONS:=true}"              # Enable --dangerously-skip-permissions
: "${USE_THINKING_PROXY:=true}"            # Route through thinking proxy
: "${THINKING_PROXY_PORT:=8080}"           # Proxy port
: "${USE_PATCHED_CLI:=true}"               # Use patched CLI (~/.claude/patched-cli.js)

# Set environment variables
export CLAUDE_AUTOCOMPACT_PCT_OVERRIDE="$COMPACT_PCT"

# NOTE: ANTHROPIC_BASE_URL is set inside c() function only, not globally.
# This ensures 'claude' works directly while 'c' routes through proxy.

# Helper: find session ID from --resume/-r argument or most recent session
_ccm_find_session_id() {
    local session_id=""
    local args=("$@")

    # Parse args to find explicit session ID from --resume/-r
    local i=0
    while [[ $i -lt ${#args[@]} ]]; do
        case "${args[i]}" in
            --resume=*)
                session_id="${args[i]#--resume=}"
                break
                ;;
            -r=*)
                session_id="${args[i]#-r=}"
                break
                ;;
            --resume|-r)
                if [[ $((i+1)) -lt ${#args[@]} ]]; then
                    session_id="${args[$((i+1))]}"
                    break
                fi
                ;;
        esac
        ((i++))
    done

    # If no explicit session ID, find most recent for current directory
    if [[ -z "$session_id" ]]; then
        local project_path
        project_path=$(pwd | sed 's|/|-|g')
        [[ "$project_path" != -* ]] && project_path="-$project_path"
        local sessions_dir="$HOME/.claude/projects$project_path"

        if [[ -d "$sessions_dir" ]]; then
            # Find most recent .jsonl file (excluding backups and agent files)
            session_id=$(find "$sessions_dir" -maxdepth 1 -name '*.jsonl' \
                -not -name '*backup*' -not -name 'agent-*' \
                -printf '%T@ %f\n' 2>/dev/null | sort -rn | head -1 | awk '{print $2}' | sed 's/\.jsonl$//')
        fi
    fi

    echo "$session_id"
}

# Claude launcher function with session ID header injection
# Remove any existing alias to allow function definition
unalias c 2>/dev/null

c() {
    local session_id
    session_id=$(_ccm_find_session_id "$@")

    # Route through thinking proxy if enabled (clear if disabled to prevent leakage)
    if [[ "$USE_THINKING_PROXY" == "true" ]]; then
        export ANTHROPIC_BASE_URL="http://127.0.0.1:${THINKING_PROXY_PORT}"
    else
        unset ANTHROPIC_BASE_URL
    fi

    # Set session ID header for thinking proxy (clear if no session to prevent leakage)
    if [[ -n "$session_id" ]]; then
        export ANTHROPIC_CUSTOM_HEADERS="X-CCM-Session-ID:$session_id"
    else
        unset ANTHROPIC_CUSTOM_HEADERS
    fi

    # Build and execute claude command
    if [[ "$USE_PATCHED_CLI" == "true" ]] && [[ -f "$HOME/.claude/patched-cli.js" ]]; then
        # Use patched CLI
        if [[ "$SKIP_PERMISSIONS" == "true" ]]; then
            node "$HOME/.claude/patched-cli.js" --dangerously-skip-permissions "$@"
        else
            node "$HOME/.claude/patched-cli.js" "$@"
        fi
    else
        # Fall back to system claude
        if [[ "$SKIP_PERMISSIONS" == "true" ]]; then
            claude --dangerously-skip-permissions "$@"
        else
            claude "$@"
        fi
    fi
}

# Status message (only shown when sourced interactively)
if [[ $- == *i* ]]; then
    echo "Claude Code configured:"
    if [[ "$USE_PATCHED_CLI" == "true" ]] && [[ -f "$HOME/.claude/patched-cli.js" ]]; then
        echo "  Function: c -> patched-cli.js${SKIP_PERMISSIONS:+ --dangerously-skip-permissions}"
    else
        echo "  Function: c -> claude${SKIP_PERMISSIONS:+ --dangerously-skip-permissions}"
    fi
    echo "  Auto-compact threshold: ${COMPACT_PCT}% (buffer: $((100-COMPACT_PCT))%)"
    if [[ "$USE_THINKING_PROXY" == "true" ]]; then
        echo "  Thinking proxy: http://127.0.0.1:${THINKING_PROXY_PORT}"
    fi
fi
