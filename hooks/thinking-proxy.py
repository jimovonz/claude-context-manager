#!/usr/bin/env python3
"""
Thinking Block Proxy for Claude Context Manager

A proxy daemon that sits between Claude CLI and the Anthropic API to manage
thinking blocks on a per-session basis. Each session gets full thinking benefits
until purge, then the proxy strips thinking bidirectionally for that session only.

Usage:
    thinking-proxy.py serve       # Run in foreground
    thinking-proxy.py start       # Start as daemon
    thinking-proxy.py stop        # Stop daemon
    thinking-proxy.py status      # Check daemon status
    thinking-proxy.py restart     # Restart daemon

Environment:
    ANTHROPIC_CUSTOM_HEADERS should include X-CCM-Session-ID:<session-uuid>
    for per-session thinking management.
"""

import argparse
import json
import logging
import os
import signal
import sys
from pathlib import Path
from typing import Optional

# Add hooks directory to path for config import
sys.path.insert(0, str(Path(__file__).parent))

# Try to import config, fall back to defaults
try:
    from config import (
        THINKING_PROXY_PORT,
        THINKING_PROXY_DEBUG_LOG,
        EXTERNAL_COMPACTION_ENABLED,
        OPENROUTER_API_KEY,
        OPENROUTER_API_BASE,
        COMPACTION_MODELS,
        COMPACTION_MAX_TOKENS,
        COMPACT_INSTRUCTIONS,
        COMPACT_INSTRUCTIONS_PASS1,
        COMPACT_INSTRUCTIONS_PASS2,
    )
except ImportError:
    THINKING_PROXY_PORT = 8080
    THINKING_PROXY_DEBUG_LOG = False
    EXTERNAL_COMPACTION_ENABLED = False
    OPENROUTER_API_KEY = None
    OPENROUTER_API_BASE = 'https://openrouter.ai/api/v1'
    COMPACTION_MODELS = {'early': 'google/gemini-2.0-flash-lite-001', 'late': 'google/gemini-2.0-flash-001'}
    COMPACTION_MAX_TOKENS = {1: 20000, 2: 36000, 3: 52000, 4: 68000, 5: 84000, 'default': 100000}
    COMPACT_INSTRUCTIONS = """Summarize this conversation for context continuity.
Preserve: current task, key decisions, file paths, pending actions, errors being investigated.
Be concise. Prioritize actionable context over history."""
    COMPACT_INSTRUCTIONS_PASS1 = COMPACT_INSTRUCTIONS
    COMPACT_INSTRUCTIONS_PASS2 = COMPACT_INSTRUCTIONS

# Paths
CLAUDE_DIR = Path.home() / '.claude'
PID_FILE = CLAUDE_DIR / 'proxy.pid'
LOG_FILE = CLAUDE_DIR / 'proxy.log'
DEBUG_LOG_FILE = CLAUDE_DIR / 'proxy-debug.log'
STATE_DIR = CLAUDE_DIR / 'proxy-state' / 'no-thinking'

# API endpoint
ANTHROPIC_API_URL = 'https://api.anthropic.com'

# Abbreviated system prompt (replaces verbose ~13KB system prompt)
# Set to None to disable replacement
ABBREVIATED_SYSTEM_PROMPT = """You are an interactive CLI assistant that helps users with software engineering tasks. Your goal is to support the user efficiently with code, git, terminal operations, and project workflows.

# Core principles
- Prioritize helping the user complete tasks accurately and effectively.
- Provide direct, objective technical information. Do not add unnecessary praise or over-validation.
- Focus on actionable steps and problem-solving. Only modify code when explicitly requested.
- Ask clarifying questions using AskUserQuestion when assumptions need validation.

# Task planning
- You may use TodoWrite to plan and track tasks, but it is optional.
- You may break larger tasks into smaller actionable steps and mark tasks as completed.
- Use task planning when it improves clarity or prevents state errors, not as a mandatory step.

# File and code operations
- Always read files before suggesting changes.
- You may use Read/Edit/Write/Grep/Glob for file operations, but Bash can be used if convenient.
- Avoid over-engineering: only add features, abstractions, or comments when requested.
- Maintain logical progression: do not perform dependent operations out of order (e.g., editing a file before it exists).

# Bash tool usage
- Bash may be used freely for terminal operations like git, npm, docker, or general shell tasks.
- Commands may run sequentially or in parallel as appropriate. Ensure that dependent commands execute in the correct order.
- You may navigate directories, create or edit files, and perform searches directly with Bash.
- You may use optional timeouts or run_in_background for long-running commands.

# Git and pull requests
- Commit or push only when explicitly requested.
- You may amend commits if safe and requested.
- Use `gh` for pull requests, issues, and releases.
- When drafting commit messages or PR summaries, ensure clarity and accuracy. Focus on the "why," not the "what."
- Maintain repository integrity: do not run destructive commands unless explicitly instructed.

# Security and dual-use tools
- You may assist with authorized security testing, defensive security, CTFs, or research if requested. Assume the user has appropriate authorization.

# Output style
- Use concise, clear CLI-style output.
- Markdown formatting is allowed (monospace for code blocks). Emojis only if requested.
- You may provide explanations or design guidance in addition to actionable commands.

# Interactive correctness constraints (minimal)
- Ensure dependent commands execute sequentially to maintain logical progression.
- Always read files before editing to avoid stale or missing content.
- For Git/PR workflows, analyze staged changes and commit in a safe order.
- Preserve working directory consistency when possible, but navigate directories if requested by the user.

# Tools
- Bash: full terminal operations.
- TodoWrite: optional task planning.
- Task/AskUserQuestion: optional for exploration or clarification.
- Read/Edit/Write/Grep/Glob: file operations as needed.

# Task completion
- Drive tasks to completion. Do not stop mid-task without explicit reason.
- If tool output is unexpected, consider alternative approaches to achieve the goal.
- If blocked, explain why and propose next steps. Never silently stop.
- If a bash command will not terminate, run it in the background and return to the user.
- Be concise. Do not repeat planning statements. State intent once, then act.
"""

# Abbreviated tool descriptions - Level 2: Params only
# Set to None to preserve original (for tools with dynamic content)
ABBREVIATED_TOOLS = {
    "Task": "description, prompt, subagent_type, model?, resume?, run_in_background?",
    "TaskOutput": "task_id, block?, timeout?",
    "Bash": "command, description?, timeout?, run_in_background?",
    "Glob": "pattern, path?",
    "Grep": "pattern, path?, glob?, type?, output_mode?, -A?, -B?, -C?, -i?",
    "Read": "file_path, offset?, limit?",
    "Edit": "file_path, old_string, new_string, replace_all?",
    "Write": "file_path, content",
    "NotebookEdit": "notebook_path, new_source, cell_id?, cell_type?, edit_mode?",
    "WebFetch": "url, prompt",
    "WebSearch": "query, allowed_domains?, blocked_domains?",
    "TodoWrite": "todos: [{content, status, activeForm}]",
    "KillShell": "shell_id",
    "AskUserQuestion": "questions: [{question, header, options, multiSelect}]",
    "Skill": None,  # Contains dynamic skill list - don't abbreviate
    "EnterPlanMode": "",
    "ExitPlanMode": "launchSwarm?, teammateCount?",
    "MCPSearch": "query, max_results?",
}

# Logger setup
logger = logging.getLogger('thinking-proxy')


def setup_logging(debug: bool = False):
    """Configure logging for the proxy."""
    # Clear existing handlers to prevent duplicates on restart/fork
    logger.handlers.clear()
    logger.propagate = False  # Don't propagate to root logger

    logger.setLevel(logging.DEBUG if debug else logging.INFO)

    # Console handler - only if stdout is a TTY (not when daemonized)
    if hasattr(sys.stdout, 'isatty') and sys.stdout.isatty():
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        console.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(console)

    # File handler for main log
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(LOG_FILE)
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    logger.addHandler(file_handler)

    # Debug file handler (if enabled)
    if debug or THINKING_PROXY_DEBUG_LOG:
        debug_handler = logging.FileHandler(DEBUG_LOG_FILE)
        debug_handler.setLevel(logging.DEBUG)
        debug_handler.setFormatter(logging.Formatter(
            '%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
        ))
        logger.addHandler(debug_handler)


def is_session_no_thinking(session_id: Optional[str]) -> bool:
    """Check if a session is in no-thinking mode."""
    if not session_id:
        return False
    state_file = STATE_DIR / session_id
    return state_file.exists()


class ThinkingBlockFilter:
    """Filter thinking blocks from SSE streaming responses.

    Tracks content block indices and filters out thinking-related events,
    adjusting indices for subsequent blocks.
    """

    def __init__(self, strip_thinking: bool = False, duplicate_as_text: bool = False):
        self.thinking_indices: set[int] = set()
        self.buffer = b''
        self.strip_thinking = strip_thinking  # Remove thinking blocks entirely
        self.duplicate_as_text = duplicate_as_text  # Add collapsible text copy alongside
        self.thinking_text: dict[int, str] = {}  # index -> accumulated thinking
        self.pending_events: list = []  # Events to inject after thinking block ends

    def process_chunk(self, chunk: bytes) -> bytes:
        """Process a chunk of SSE data, filtering thinking blocks.

        Args:
            chunk: Raw bytes from the SSE stream

        Returns:
            Filtered bytes to forward to client
        """
        self.buffer += chunk
        output = b''

        # Process complete SSE events (end with \n\n or \r\n\r\n)
        while b'\n\n' in self.buffer or b'\r\n\r\n' in self.buffer:
            # Find the end of the current event
            nn_pos = self.buffer.find(b'\n\n')
            rn_pos = self.buffer.find(b'\r\n\r\n')

            if nn_pos >= 0 and (rn_pos < 0 or nn_pos < rn_pos):
                end_pos = nn_pos + 2
            elif rn_pos >= 0:
                end_pos = rn_pos + 4
            else:
                break

            event_data = self.buffer[:end_pos]
            self.buffer = self.buffer[end_pos:]

            filtered = self._filter_event(event_data)
            if filtered:
                output += filtered

        return output

    def flush(self) -> bytes:
        """Flush any remaining buffered data."""
        if self.buffer:
            result = self._filter_event(self.buffer)
            self.buffer = b''
            return result or b''
        return b''

    def _filter_event(self, event_data: bytes) -> Optional[bytes]:
        """Filter a single SSE event.

        Returns None if the event should be filtered out entirely.
        """
        try:
            text = event_data.decode('utf-8')
        except UnicodeDecodeError:
            return event_data

        lines = text.split('\n')
        filtered_lines = []
        pending_event_line = None  # Track 'event:' line until we see its data

        for line in lines:
            if line.startswith('event:'):
                # Hold the event line until we know if its data should be kept
                pending_event_line = line
                continue

            if line.startswith('data: '):
                json_str = line[6:]  # Remove 'data: ' prefix

                # Handle [DONE] marker
                if json_str.strip() == '[DONE]':
                    if pending_event_line:
                        filtered_lines.append(pending_event_line)
                        pending_event_line = None
                    filtered_lines.append(line)
                    continue

                try:
                    data = json.loads(json_str)
                    filtered_data = self._filter_json_event(data)

                    if filtered_data is None:
                        # Skip this event entirely (both event: and data: lines)
                        pending_event_line = None
                        continue
                    elif isinstance(filtered_data, list):
                        # Multiple events to emit
                        if pending_event_line:
                            filtered_lines.append(pending_event_line)
                            pending_event_line = None
                        for event in filtered_data:
                            event_type = event.get('type', '')
                            filtered_lines.append(f'event: {event_type}')
                            filtered_lines.append(f'data: {json.dumps(event)}')
                            filtered_lines.append('')  # Empty line between events
                    else:
                        # Include the event line if we had one
                        if pending_event_line:
                            filtered_lines.append(pending_event_line)
                            pending_event_line = None

                        if filtered_data is not data:
                            # Data was modified
                            filtered_lines.append(f'data: {json.dumps(filtered_data)}')
                        else:
                            # Data unchanged
                            filtered_lines.append(line)
                except json.JSONDecodeError:
                    if pending_event_line:
                        filtered_lines.append(pending_event_line)
                        pending_event_line = None
                    filtered_lines.append(line)
            else:
                # Non-event, non-data lines (empty lines, comments, etc.)
                if pending_event_line:
                    filtered_lines.append(pending_event_line)
                    pending_event_line = None
                filtered_lines.append(line)

        # Flush any pending event line
        if pending_event_line:
            filtered_lines.append(pending_event_line)

        if not filtered_lines or all(not l.strip() for l in filtered_lines):
            return None

        result = '\n'.join(filtered_lines)
        if not result.endswith('\n\n'):
            result += '\n\n'
        return result.encode('utf-8')

    def _filter_json_event(self, data: dict):
        """Filter a parsed JSON event.

        Returns:
            - None to filter out the event entirely
            - dict for single event
            - list of dicts to emit multiple events
        """
        event_type = data.get('type', '')
        index = data.get('index')

        # content_block_start - track thinking blocks
        if event_type == 'content_block_start':
            content_block = data.get('content_block', {})
            block_type = content_block.get('type', '')

            if block_type in ('thinking', 'redacted_thinking'):
                if index is not None:
                    self.thinking_indices.add(index)
                    self.thinking_text[index] = ''

                if self.strip_thinking:
                    logger.debug(f"Stripping thinking block at index {index}")
                    return None
                # Pass through unchanged (duplicate_as_text adds copy at block end)
                return data

            if self.strip_thinking:
                return self._adjust_index(data)
            return data

        # content_block_delta - accumulate thinking text
        if event_type == 'content_block_delta':
            if index in self.thinking_indices:
                delta = data.get('delta', {})
                delta_type = delta.get('type', '')

                # Accumulate thinking content for later duplication
                if delta_type == 'thinking_delta':
                    thinking_chunk = delta.get('thinking', '')
                    self.thinking_text[index] = self.thinking_text.get(index, '') + thinking_chunk

                if self.strip_thinking:
                    return None
                # Pass through unchanged
                return data

            delta = data.get('delta', {})
            delta_type = delta.get('type', '')

            if self.strip_thinking and delta_type in ('thinking_delta', 'signature_delta'):
                return None

            if self.strip_thinking:
                return self._adjust_index(data)
            return data

        # content_block_stop - inject collapsible text copy after thinking ends
        if event_type == 'content_block_stop':
            if index in self.thinking_indices:
                if self.strip_thinking:
                    return None

                # Pass through the stop event
                if self.duplicate_as_text:
                    # After thinking block ends, inject a text copy with <details> tag
                    thinking_content = self.thinking_text.get(index, '')
                    if thinking_content:
                        # Use <details> for display
                        collapsible = f'<details><summary>💭 Thinking</summary>\n\n{thinking_content}\n</details>'
                        # Return list: original stop + new text block
                        return [
                            data,  # Original content_block_stop
                            {'type': 'content_block_start', 'index': index + 1000,
                             'content_block': {'type': 'text', 'text': ''}},
                            {'type': 'content_block_delta', 'index': index + 1000,
                             'delta': {'type': 'text_delta', 'text': collapsible}},
                            {'type': 'content_block_stop', 'index': index + 1000},
                        ]
                return data

            if self.strip_thinking:
                return self._adjust_index(data)
            return data

        return data

    def _adjust_index(self, data: dict) -> dict:
        """Adjust the index field to account for filtered thinking blocks."""
        index = data.get('index')
        if index is None:
            return data

        # Count how many thinking blocks are before this index
        adjustment = sum(1 for ti in self.thinking_indices if ti < index)

        if adjustment > 0:
            data = data.copy()
            data['index'] = index - adjustment
        return data


class ExternalCompactionHandler:
    """Route compaction requests to external LLM via OpenRouter.

    Detects Claude Code's automatic compaction requests and routes them
    to a cheaper/more capable external model (Gemini Flash variants).
    After first compaction, marks session for no-thinking mode.
    """

    def __init__(self, api_key: str, api_base: str = OPENROUTER_API_BASE):
        self.api_key = api_key
        self.api_base = api_base
        self.compaction_count: dict[str, int] = {}  # session_id -> count
        self.previous_artefacts: dict[str, str] = {}  # session_id -> artefacts from last Pass 1

    def is_compaction_request(self, body: dict) -> bool:
        """Detect compaction by Claude Code's injected system prompt.

        Claude Code adds a system block containing "summarizing conversations"
        when triggering compaction.
        """
        system = body.get('system', [])
        for block in system:
            if isinstance(block, dict):
                text = block.get('text', '')
                if 'summarizing conversations' in text.lower():
                    return True
            elif isinstance(block, str):
                if 'summarizing conversations' in block.lower():
                    return True
        return False

    def strip_thinking_from_messages(self, messages: list) -> list:
        """Remove thinking blocks from message history.

        External models can't process thinking blocks, and we can't
        generate valid signatures for them anyway.
        """
        result = []
        for msg in messages:
            msg_copy = msg.copy()
            content = msg_copy.get('content')
            if isinstance(content, list):
                msg_copy['content'] = [
                    block for block in content
                    if block.get('type') not in ('thinking', 'redacted_thinking')
                ]
            result.append(msg_copy)
        return result

    def select_model(self, session_id: str) -> tuple[str, int]:
        """Select model based on compaction count.

        Returns (model_id, compaction_number).
        Early compactions (1-5) use cheaper model.
        Later compactions (6+) use more capable model.
        """
        count = self.compaction_count.get(session_id, 0) + 1
        self.compaction_count[session_id] = count

        if count <= 5:
            return COMPACTION_MODELS['early'], count
        else:
            return COMPACTION_MODELS['late'], count

    def get_max_tokens(self, compaction_num: int) -> int:
        """Get max output tokens for this compaction number.

        Generous early (content is verbose), tighter later (content is dense).
        """
        return COMPACTION_MAX_TOKENS.get(compaction_num, COMPACTION_MAX_TOKENS['default'])

    def claude_to_openai(self, body: dict, model: str, max_tokens: int,
                          system_prompt: str = None, stream: bool = True) -> dict:
        """Transform Claude request to OpenAI/OpenRouter format.

        Optimizations for compaction:
        - Skip Claude's system prompt (use custom compaction instructions)
        - Strip metadata (cache_control, timestamps, UUIDs)
        - Tool results left as-is (already summarized by CCM hooks)
        """
        messages = self.strip_thinking_from_messages(body.get('messages', []))

        openai_messages = []

        # Add system prompt (custom or default)
        if system_prompt:
            openai_messages.append({
                'role': 'system',
                'content': system_prompt
            })
        else:
            # Legacy single-pass mode
            instructions = COMPACT_INSTRUCTIONS.replace('<N tokens', f'{max_tokens} tokens')
            openai_messages.append({
                'role': 'system',
                'content': instructions
            })

        # Convert conversation messages - extract only role + text content
        # Skip all metadata: uuid, timestamp, thinkingMetadata, cache_control, etc.
        for msg in messages:
            role = msg['role']
            content = msg.get('content', [])

            # Flatten content blocks to plain text only
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get('type', '')

                        if block_type == 'text':
                            # Text block: extract text only, skip cache_control/citations
                            text_parts.append(block.get('text', ''))

                        elif block_type == 'tool_use':
                            # Tool call: compact single-line representation
                            name = block.get('name', 'unknown')
                            inp = block.get('input', {})
                            if 'file_path' in inp:
                                text_parts.append(f"[{name}: {inp['file_path']}]")
                            elif 'command' in inp:
                                cmd = inp['command'][:100] + '...' if len(inp['command']) > 100 else inp['command']
                                text_parts.append(f"[{name}: {cmd}]")
                            elif 'pattern' in inp:
                                text_parts.append(f"[{name}: {inp['pattern']}]")
                            elif 'query' in inp:
                                text_parts.append(f"[{name}: {inp['query'][:80]}]")
                            elif 'url' in inp:
                                text_parts.append(f"[{name}: {inp['url']}]")
                            elif 'prompt' in inp:
                                text_parts.append(f"[{name}]")
                            else:
                                text_parts.append(f"[{name}]")

                        elif block_type == 'tool_result':
                            # Tool result: extract text content only
                            result_content = block.get('content', '')
                            if isinstance(result_content, list):
                                # Extract only text from content blocks
                                texts = [b.get('text', '') for b in result_content
                                        if isinstance(b, dict) and b.get('type') == 'text']
                                result_text = '\n'.join(texts)
                            elif isinstance(result_content, str):
                                result_text = result_content
                            else:
                                result_text = ''
                            if result_text:
                                text_parts.append(result_text)

                        # Explicitly skip: image, document, thinking, redacted_thinking
                        # Any other block type is silently dropped

                    elif isinstance(block, str):
                        text_parts.append(block)

                text = '\n'.join(filter(None, text_parts))
            else:
                text = str(content) if content else ''

            if text.strip():
                openai_messages.append({'role': role, 'content': text})

        return {
            'model': model,
            'messages': openai_messages,
            'max_tokens': max_tokens,
            'stream': stream
        }

    def _message_start_event(self) -> bytes:
        """Generate Claude message_start SSE event."""
        event = {
            'type': 'message_start',
            'message': {
                'id': 'msg_external_compaction',
                'type': 'message',
                'role': 'assistant',
                'content': [],
                'model': 'external-compaction',
                'stop_reason': None,
                'stop_sequence': None,
                'usage': {'input_tokens': 0, 'output_tokens': 0}
            }
        }
        return f'event: message_start\ndata: {json.dumps(event)}\n\n'.encode()

    def _content_block_start_event(self) -> bytes:
        """Generate Claude content_block_start SSE event."""
        event = {
            'type': 'content_block_start',
            'index': 0,
            'content_block': {'type': 'text', 'text': ''}
        }
        return f'event: content_block_start\ndata: {json.dumps(event)}\n\n'.encode()

    def _content_block_delta_event(self, text: str) -> bytes:
        """Generate Claude content_block_delta SSE event."""
        event = {
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'text_delta', 'text': text}
        }
        return f'event: content_block_delta\ndata: {json.dumps(event)}\n\n'.encode()

    def _content_block_stop_event(self) -> bytes:
        """Generate Claude content_block_stop SSE event."""
        event = {'type': 'content_block_stop', 'index': 0}
        return f'event: content_block_stop\ndata: {json.dumps(event)}\n\n'.encode()

    def _message_delta_event(self) -> bytes:
        """Generate Claude message_delta SSE event."""
        event = {
            'type': 'message_delta',
            'delta': {'stop_reason': 'end_turn', 'stop_sequence': None},
            'usage': {'output_tokens': 0}
        }
        return f'event: message_delta\ndata: {json.dumps(event)}\n\n'.encode()

    def _message_stop_event(self) -> bytes:
        """Generate Claude message_stop SSE event."""
        event = {'type': 'message_stop'}
        return f'event: message_stop\ndata: {json.dumps(event)}\n\n'.encode()

    async def run_pass1(self, body: dict, model: str, session_id: str,
                         aiohttp_session) -> str:
        """Run Pass 1: Extract artefacts (non-streaming).

        Returns the artefacts text extracted from the conversation.
        """
        # Get previous artefacts for delta mode
        prev_artefacts = self.previous_artefacts.get(session_id, 'None (first compaction)')

        # Build Pass 1 prompt with previous artefacts
        pass1_prompt = COMPACT_INSTRUCTIONS_PASS1.replace('{previous_artefacts}', prev_artefacts)

        # Build request (non-streaming)
        request = self.claude_to_openai(body, model, max_tokens=20000,
                                         system_prompt=pass1_prompt, stream=False)

        logger.debug(f"Pass 1 request for session {session_id}")

        async with aiohttp_session.post(
            f'{self.api_base}/chat/completions',
            headers={
                'Authorization': f'Bearer {self.api_key}',
                'HTTP-Referer': 'https://github.com/anthropics/claude-code',
                'X-Title': 'Claude Code CCM Pass1',
                'Content-Type': 'application/json',
            },
            json=request
        ) as response:
            if response.status != 200:
                error_text = await response.text()
                logger.error(f"Pass 1 error {response.status}: {error_text}")
                return prev_artefacts  # Fall back to previous artefacts

            data = await response.json()
            artefacts = data.get('choices', [{}])[0].get('message', {}).get('content', '')

            # Store for next compaction
            if artefacts:
                self.previous_artefacts[session_id] = artefacts

            logger.info(f"Pass 1 complete: {len(artefacts)} chars extracted")
            logger.debug(f"Pass 1 artefacts:\n{artefacts[:2000]}{'...' if len(artefacts) > 2000 else ''}")

            # Write artefacts to file for inspection
            artefacts_file = CLAUDE_DIR / 'last-artefacts.txt'
            artefacts_file.write_text(f"Session: {session_id}\nCompaction: {self.compaction_count.get(session_id, 1)}\n\n{artefacts}")

            return artefacts

    async def handle_compaction(self, session_id: str, body: dict, aiohttp_session) -> tuple:
        """Route compaction to external model via OpenRouter (two-pass).

        Pass 1: Extract/update artefacts (non-streaming)
        Pass 2: Generate full distillation (streaming)

        Returns (async_generator, should_mark_no_thinking).
        The generator yields bytes in Claude SSE format.
        """
        model, count = self.select_model(session_id)
        max_tokens = self.get_max_tokens(count)

        logger.info(f"External compaction {count} for session {session_id} using {model} (max_tokens={max_tokens})")

        # Pass 1: Extract artefacts
        artefacts = await self.run_pass1(body, model, session_id, aiohttp_session)

        # Build Pass 2 prompt with artefacts
        pass2_prompt = COMPACT_INSTRUCTIONS_PASS2.replace('{pass1_artefacts}', artefacts)
        pass2_prompt = pass2_prompt.replace('<N tokens', f'{max_tokens} tokens')

        # Transform request for OpenRouter (streaming)
        openrouter_request = self.claude_to_openai(body, model, max_tokens,
                                                    system_prompt=pass2_prompt, stream=True)

        logger.debug(f"OpenRouter request: {json.dumps(openrouter_request, indent=2)[:2000]}...")

        async def stream_response():
            total_chars = [0]  # Mutable for closure
            collected_content = []  # Collect Pass 2 output
            try:
                async with aiohttp_session.post(
                    f'{self.api_base}/chat/completions',
                    headers={
                        'Authorization': f'Bearer {self.api_key}',
                        'HTTP-Referer': 'https://github.com/anthropics/claude-code',
                        'X-Title': 'Claude Code CCM',
                        'Content-Type': 'application/json',
                    },
                    json=openrouter_request
                ) as response:
                    if response.status != 200:
                        error_text = await response.text()
                        logger.error(f"OpenRouter error {response.status}: {error_text}")
                        # Return error as Claude format
                        yield self._message_start_event()
                        yield self._content_block_start_event()
                        yield self._content_block_delta_event(f"[External compaction error: {response.status}]")
                        yield self._content_block_stop_event()
                        yield self._message_delta_event()
                        yield self._message_stop_event()
                        return

                    # Stream response back in Claude format
                    yield self._message_start_event()
                    yield self._content_block_start_event()

                    buffer = b''
                    async for chunk in response.content.iter_any():
                        buffer += chunk
                        # Process complete SSE lines
                        while b'\n' in buffer:
                            line, buffer = buffer.split(b'\n', 1)
                            line = line.strip()
                            if not line:
                                continue
                            if line.startswith(b'data: '):
                                data_str = line[6:].decode('utf-8')
                                if data_str == '[DONE]':
                                    break
                                try:
                                    data = json.loads(data_str)
                                    choices = data.get('choices', [])
                                    if choices:
                                        delta = choices[0].get('delta', {})
                                        content = delta.get('content', '')
                                        if content:
                                            total_chars[0] += len(content)
                                            collected_content.append(content)
                                            yield self._content_block_delta_event(content)
                                except json.JSONDecodeError:
                                    pass

                    logger.info(f"Pass 2 complete: {total_chars[0]} chars output")

                    # Write Pass 2 output to file for inspection
                    pass2_file = CLAUDE_DIR / 'last-distillation.txt'
                    pass2_file.write_text(f"Session: {session_id}\nCompaction: {count}\n\n{''.join(collected_content)}")

                    yield self._content_block_stop_event()
                    yield self._message_delta_event()
                    yield self._message_stop_event()

            except Exception as e:
                logger.error(f"External compaction error: {e}", exc_info=True)
                yield self._message_start_event()
                yield self._content_block_start_event()
                yield self._content_block_delta_event(f"[External compaction failed: {e}]")
                yield self._content_block_stop_event()
                yield self._message_delta_event()
                yield self._message_stop_event()

        return stream_response(), True  # True = mark session for no-thinking


def get_session_id(headers: dict) -> Optional[str]:
    """Extract session ID from request headers."""
    # Check for X-CCM-Session-ID header
    session_id = headers.get('X-CCM-Session-ID') or headers.get('x-ccm-session-id')
    return session_id


def write_pid():
    """Write current PID to file."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def read_pid() -> Optional[int]:
    """Read PID from file."""
    try:
        return int(PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def remove_pid():
    """Remove PID file."""
    try:
        PID_FILE.unlink()
    except FileNotFoundError:
        pass


def is_running() -> bool:
    """Check if the proxy daemon is running."""
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        # Process not running, clean up stale PID file
        remove_pid()
        return False


def run_proxy(port: int, debug: bool):
    """Run the proxy server (requires aiohttp)."""
    try:
        import aiohttp
        from aiohttp import web
    except ImportError:
        print("Error: aiohttp is required. Install with: pip install aiohttp")
        sys.exit(1)

    class ThinkingProxy:
        """Async HTTP proxy for Claude API with thinking block management."""

        def __init__(self):
            self.port = port
            self.debug = debug
            self.app = web.Application()
            self.app.router.add_route('*', '/{path:.*}', self.handle_request)
            self.session = None

            # Initialize external compaction handler if enabled
            self.compaction_handler = None
            if EXTERNAL_COMPACTION_ENABLED and OPENROUTER_API_KEY:
                self.compaction_handler = ExternalCompactionHandler(
                    api_key=OPENROUTER_API_KEY,
                    api_base=OPENROUTER_API_BASE
                )
                logger.info(f"External compaction enabled (models: {COMPACTION_MODELS})")

        async def start_session(self):
            """Initialize the aiohttp client session."""
            if self.session is None or self.session.closed:
                self.session = aiohttp.ClientSession()

        async def close_session(self):
            """Close the aiohttp client session."""
            if self.session and not self.session.closed:
                await self.session.close()

        def _abbreviate_system_prompt(self, body: bytes) -> bytes:
            """Replace verbose system prompt with abbreviated version.

            Preserves dynamic content like environment info and CLAUDE.md.
            """
            if not ABBREVIATED_SYSTEM_PROMPT:
                return body

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return body

            system = data.get('system', [])
            if not system or not isinstance(system, list):
                return body

            original_size = len(body)
            modified = False

            # Find and replace the main system prompt (usually the second block, first is identity)
            for i, block in enumerate(system):
                if not isinstance(block, dict) or block.get('type') != 'text':
                    continue
                text = block.get('text', '')
                # The main instructions block starts with "\nYou are an interactive CLI tool"
                if 'You are an interactive CLI tool' in text or 'software engineering tasks' in text:
                    # Preserve cache_control if present
                    cache_control = block.get('cache_control')
                    system[i] = {'type': 'text', 'text': ABBREVIATED_SYSTEM_PROMPT}
                    if cache_control:
                        system[i]['cache_control'] = cache_control
                    modified = True
                    break

            if modified:
                data['system'] = system
                new_body = json.dumps(data).encode('utf-8')
                saved = original_size - len(new_body)
                logger.info(f"Abbreviated system prompt: saved {saved:,} bytes ({saved*100/original_size:.1f}%)")
                return new_body
            return body

        def _abbreviate_tools(self, body: bytes) -> bytes:
            """Replace verbose tool descriptions with abbreviated versions.

            This can save ~40KB per request while maintaining functionality
            if Claude is fine-tuned for these tools.
            """
            if not ABBREVIATED_TOOLS:
                return body

            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return body

            tools = data.get('tools', [])
            if not tools:
                return body

            modified = False
            original_size = len(body)

            for tool in tools:
                name = tool.get('name', '')
                if name in ABBREVIATED_TOOLS:
                    abbrev = ABBREVIATED_TOOLS[name]
                    if abbrev is None:
                        # None means skip abbreviation for this tool (has dynamic content)
                        continue
                    old_desc_len = len(tool.get('description', ''))
                    tool['description'] = abbrev
                    new_desc_len = len(tool['description'])
                    if old_desc_len != new_desc_len:
                        modified = True

            if modified:
                new_body = json.dumps(data).encode('utf-8')
                saved = original_size - len(new_body)
                logger.info(f"Abbreviated tool descriptions: saved {saved:,} bytes ({saved*100/original_size:.1f}%)")
                return new_body
            return body

        def _strip_thinking_from_request(self, body: bytes) -> bytes:
            """Strip thinking blocks and parameters from request body.

            Removes:
            - The 'thinking' parameter from the request
            - Any thinking/redacted_thinking blocks from message history
            """
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                return body

            modified = False

            # Remove thinking parameter
            if 'thinking' in data:
                del data['thinking']
                modified = True
                logger.debug("Removed 'thinking' parameter from request")

            # Strip thinking blocks from message history
            messages = data.get('messages', [])
            for msg in messages:
                content = msg.get('content')
                if isinstance(content, list):
                    original_len = len(content)
                    msg['content'] = [
                        block for block in content
                        if block.get('type') not in ('thinking', 'redacted_thinking')
                    ]
                    if len(msg['content']) != original_len:
                        modified = True
                        logger.debug(f"Stripped {original_len - len(msg['content'])} thinking blocks from message")

            if modified:
                return json.dumps(data).encode('utf-8')
            return body

        async def handle_request(self, request):
            """Handle incoming requests and proxy to Anthropic API."""
            await self.start_session()

            path = request.path
            if request.query_string:
                path = f"{path}?{request.query_string}"

            target_url = f"{ANTHROPIC_API_URL}{path}"

            # Get session ID from headers
            session_id = get_session_id(dict(request.headers))
            no_thinking = is_session_no_thinking(session_id)

            logger.info(f"Request: {request.method} {path} (session={session_id}, no_thinking={no_thinking})")

            # Read request body
            body = await request.read()

            # Prepare headers (forward most headers, skip hop-by-hop)
            skip_headers = {'host', 'content-length', 'transfer-encoding', 'connection'}
            headers = {
                k: v for k, v in request.headers.items()
                if k.lower() not in skip_headers
            }

            # Strip interleaved-thinking beta from headers if no_thinking
            if no_thinking and 'anthropic-beta' in {k.lower() for k in headers}:
                beta_key = next(k for k in headers if k.lower() == 'anthropic-beta')
                original_beta = headers[beta_key]
                # Remove interleaved-thinking entries from comma-separated list
                beta_parts = [p.strip() for p in original_beta.split(',')]
                filtered_parts = [p for p in beta_parts if not p.startswith('interleaved-thinking')]
                if filtered_parts:
                    headers[beta_key] = ', '.join(filtered_parts)
                    logger.debug(f"Stripped interleaved-thinking from beta header: {original_beta} -> {headers[beta_key]}")
                else:
                    del headers[beta_key]
                    logger.debug(f"Removed anthropic-beta header entirely (was: {original_beta})")

            # Abbreviate system prompt and tool descriptions to save tokens
            if body:
                body = self._abbreviate_system_prompt(body)
                body = self._abbreviate_tools(body)

            # Debug log request headers and body
            if self.debug:
                logger.debug(f"Request headers: {json.dumps(dict(headers), indent=2)}")
                if body:
                    try:
                        body_json = json.loads(body)
                        logger.debug(f"Request body: {json.dumps(body_json, indent=2)}")
                    except:
                        logger.debug(f"Request body (raw): {body[:1000]}")

            # Strip thinking from request body if no_thinking
            if no_thinking and body:
                body = self._strip_thinking_from_request(body)
                if self.debug:
                    try:
                        body_json = json.loads(body)
                        logger.debug(f"Request body (after thinking strip): {json.dumps(body_json, indent=2)}")
                    except:
                        pass

            # Check for compaction request and route to external model if enabled
            if self.compaction_handler and body:
                try:
                    body_json = json.loads(body)
                    if self.compaction_handler.is_compaction_request(body_json):
                        logger.info(f"Detected compaction request for session {session_id}")

                        # Route to external model
                        stream_gen, mark_no_thinking = await self.compaction_handler.handle_compaction(
                            session_id, body_json, self.session
                        )

                        # Prepare streaming response
                        response = web.StreamResponse(
                            status=200,
                            headers={
                                'Content-Type': 'text/event-stream',
                                'Cache-Control': 'no-cache',
                            },
                        )
                        await response.prepare(request)

                        # Stream the response
                        async for chunk in stream_gen:
                            await response.write(chunk)

                        await response.write_eof()

                        # Mark session for no-thinking after compaction
                        if mark_no_thinking and session_id:
                            STATE_DIR.mkdir(parents=True, exist_ok=True)
                            (STATE_DIR / session_id).touch()
                            logger.info(f"Session {session_id} marked for no-thinking after external compaction")

                        return response
                except json.JSONDecodeError:
                    pass  # Not valid JSON, fall through to normal handling
                except Exception as e:
                    logger.error(f"Error checking for compaction: {e}", exc_info=True)
                    # Fall through to normal handling

            try:
                # Make request to Anthropic API
                async with self.session.request(
                    method=request.method,
                    url=target_url,
                    headers=headers,
                    data=body,
                    ssl=True,
                ) as upstream_response:

                    # Check if this is a streaming response
                    content_type = upstream_response.headers.get('content-type', '')
                    is_streaming = 'text/event-stream' in content_type

                    logger.info(f"Response: {upstream_response.status} (streaming={is_streaming})")

                    # Prepare response headers
                    response_headers = {
                        k: v for k, v in upstream_response.headers.items()
                        if k.lower() not in {'transfer-encoding', 'content-encoding', 'content-length'}
                    }

                    if is_streaming:
                        # Handle streaming SSE response
                        response = web.StreamResponse(
                            status=upstream_response.status,
                            headers=response_headers,
                        )
                        await response.prepare(request)

                        # Filter thinking blocks based on mode:
                        # - no_thinking: strip entirely
                        # - normal: pass through unchanged
                        if no_thinking:
                            logger.info(f"Stripping thinking blocks for session {session_id}")
                            filter_obj = ThinkingBlockFilter(strip_thinking=True)
                        else:
                            # Pass through unchanged (duplication testing complete)
                            filter_obj = None

                        if filter_obj:
                            async for chunk in upstream_response.content.iter_any():
                                if self.debug:
                                    logger.debug(f"Stream chunk (raw): {chunk}")

                                filtered = filter_obj.process_chunk(chunk)
                                if filtered:
                                    if self.debug:
                                        logger.debug(f"Stream chunk (filtered): {filtered}")
                                    await response.write(filtered)

                            # Flush any remaining data
                            remaining = filter_obj.flush()
                            if remaining:
                                await response.write(remaining)
                        else:
                            # Pass through unchanged
                            async for chunk in upstream_response.content.iter_any():
                                await response.write(chunk)

                        await response.write_eof()
                        return response
                    else:
                        # Handle non-streaming response
                        response_body = await upstream_response.read()

                        if self.debug:
                            logger.debug(f"Response body: {response_body}")

                        return web.Response(
                            status=upstream_response.status,
                            headers=response_headers,
                            body=response_body,
                        )

            except aiohttp.ClientError as e:
                logger.error(f"Upstream error: {e}")
                return web.Response(
                    status=502,
                    text=f"Proxy error: {e}",
                )
            except Exception as e:
                logger.error(f"Unexpected error: {e}", exc_info=True)
                return web.Response(
                    status=500,
                    text=f"Internal proxy error: {e}",
                )

        async def cleanup(self, app):
            """Cleanup on shutdown."""
            await self.close_session()

        def run(self):
            """Run the proxy server."""
            self.app.on_cleanup.append(self.cleanup)

            logger.info(f"Starting thinking proxy on port {self.port}")
            logger.info(f"Forwarding to {ANTHROPIC_API_URL}")
            logger.info(f"State directory: {STATE_DIR}")

            web.run_app(
                self.app,
                host='127.0.0.1',
                port=self.port,
                print=lambda x: logger.info(x) if 'Running' in str(x) else None,
            )

    # Create and run the proxy
    proxy = ThinkingProxy()
    proxy.run()


def cmd_serve(args):
    """Run proxy in foreground."""
    setup_logging(debug=args.debug)
    write_pid()

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        run_proxy(port=args.port, debug=args.debug)
    finally:
        remove_pid()


def cmd_start(args):
    """Start proxy as daemon."""
    if is_running():
        print(f"Proxy already running (PID: {read_pid()})")
        sys.exit(1)

    # Check if aiohttp is available before forking
    try:
        import aiohttp
    except ImportError:
        print("Error: aiohttp is required. Install with: pip install aiohttp")
        sys.exit(1)

    # Fork to background
    pid = os.fork()
    if pid > 0:
        # Parent process
        print(f"Started thinking proxy daemon (PID: {pid})")
        sys.exit(0)

    # Child process - become daemon
    os.setsid()

    # Fork again to prevent zombie
    pid = os.fork()
    if pid > 0:
        sys.exit(0)

    # Redirect standard file descriptors
    sys.stdin.close()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    sys.stdout = open(LOG_FILE, 'a')
    sys.stderr = sys.stdout

    # Run the server
    setup_logging(debug=args.debug)
    write_pid()

    def signal_handler(sig, frame):
        logger.info("Shutting down...")
        remove_pid()
        sys.exit(0)

    signal.signal(signal.SIGTERM, signal_handler)

    try:
        run_proxy(port=args.port, debug=args.debug)
    finally:
        remove_pid()


def cmd_stop(args):
    """Stop the proxy daemon."""
    pid = read_pid()
    if pid is None:
        print("Proxy not running")
        sys.exit(1)

    try:
        os.kill(pid, signal.SIGTERM)
        print(f"Stopped proxy (PID: {pid})")
        remove_pid()
    except OSError as e:
        print(f"Error stopping proxy: {e}")
        remove_pid()
        sys.exit(1)


def cmd_status(args):
    """Check proxy status."""
    if is_running():
        pid = read_pid()
        print(f"Proxy running (PID: {pid})")
        print(f"  Port: {THINKING_PROXY_PORT}")
        print(f"  Log: {LOG_FILE}")
        print(f"  State: {STATE_DIR}")

        # Count no-thinking sessions
        if STATE_DIR.exists():
            sessions = list(STATE_DIR.iterdir())
            print(f"  No-thinking sessions: {len(sessions)}")
            for s in sessions[:5]:
                print(f"    - {s.name}")
            if len(sessions) > 5:
                print(f"    ... and {len(sessions) - 5} more")
    else:
        print("Proxy not running")
        sys.exit(1)


def cmd_restart(args):
    """Restart the proxy daemon."""
    if is_running():
        cmd_stop(args)
        import time
        time.sleep(1)
    cmd_start(args)


def main():
    parser = argparse.ArgumentParser(
        description='Thinking Block Proxy for Claude Context Manager',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )

    subparsers = parser.add_subparsers(dest='command', help='Command to run')

    # serve command
    serve_parser = subparsers.add_parser('serve', help='Run proxy in foreground')
    serve_parser.add_argument('--port', type=int, default=THINKING_PROXY_PORT,
                              help=f'Port to listen on (default: {THINKING_PROXY_PORT})')
    serve_parser.add_argument('--debug', action='store_true',
                              help='Enable debug logging')
    serve_parser.set_defaults(func=cmd_serve)

    # start command
    start_parser = subparsers.add_parser('start', help='Start proxy as daemon')
    start_parser.add_argument('--port', type=int, default=THINKING_PROXY_PORT,
                              help=f'Port to listen on (default: {THINKING_PROXY_PORT})')
    start_parser.add_argument('--debug', action='store_true',
                              help='Enable debug logging')
    start_parser.set_defaults(func=cmd_start)

    # stop command
    stop_parser = subparsers.add_parser('stop', help='Stop proxy daemon')
    stop_parser.set_defaults(func=cmd_stop)

    # status command
    status_parser = subparsers.add_parser('status', help='Check proxy status')
    status_parser.set_defaults(func=cmd_status)

    # restart command
    restart_parser = subparsers.add_parser('restart', help='Restart proxy daemon')
    restart_parser.add_argument('--port', type=int, default=THINKING_PROXY_PORT,
                                help=f'Port to listen on (default: {THINKING_PROXY_PORT})')
    restart_parser.add_argument('--debug', action='store_true',
                                help='Enable debug logging')
    restart_parser.set_defaults(func=cmd_restart)

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    args.func(args)


if __name__ == '__main__':
    main()
