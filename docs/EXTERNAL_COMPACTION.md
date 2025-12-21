# External Compaction System

## Overview

Route Claude Code's automatic compaction requests to an external LLM (Gemini Flash) instead of using Claude for self-summarization. This recovers the 22.5k token buffer Claude reserves for compaction and enables smarter, cheaper summarization with artefact extraction.

## Why External Compaction

### Current Claude Behavior
- Claude reserves 22.5k tokens as compaction buffer
- Same 200k context model summarizes itself
- Fixed 20k output limit for summaries
- Expensive (Opus/Sonnet pricing for summarization)

### External Compaction Benefits
- Recovers 22.5k buffer (12.6% more usable context)
- Cheaper model for summarization task
- Variable output size (up to 64k with Gemini Flash)
- Single-pass artefact extraction + distillation
- Guaranteed artefact preservation via programmatic append

## Target Models

### API: OpenRouter

Use OpenRouter for flexibility - single API, access to all providers:
- Unified OpenAI-compatible API format
- Switch models without code changes
- Fallback options if provider is down
- Single API key management

### Model Strategy

| Model | Context | Output Limit | Use Case | OpenRouter ID |
|-------|---------|--------------|----------|---------------|
| Gemini 3 Flash Preview | 1M+ | 64k | All compactions | `google/gemini-3-flash-preview` |

### Why Gemini 3 Flash

- **64k output limit** - Sufficient for comprehensive distillations
- **Cost-effective** - Cheaper than Pro variants
- **Fast** - Low latency for streaming responses
- **High quality** - Follows structured prompts well

### Flexibility via OpenRouter

Can easily swap models based on experimentation:
- `google/gemini-3-pro-preview` - Higher capability, more expensive
- `anthropic/claude-3-haiku` - If staying in Anthropic ecosystem preferred
- Any model with sufficient context window and output limit

## Single-Pass Distillation

The system uses a single-pass approach that combines artefact extraction and distillation:

### How It Works

1. **Single prompt** instructs the LLM to:
   - First extract all execution-critical artefacts (commands, paths, errors, code)
   - Then write a comprehensive distillation using those artefacts

2. **Artefact extraction** from output:
   - Proxy parses the LLM's output to find the ARTEFACTS section
   - Stores artefacts for next compaction's delta mode

3. **Programmatic append**:
   - After LLM response, proxy appends the extracted artefacts
   - Guarantees artefact preservation regardless of LLM verbosity

### Benefits

- **One API call** instead of two (faster, cheaper)
- **Artefacts in context** when LLM writes distillation
- **Guaranteed preservation** via append (never lost)
- **Delta mode** for subsequent compactions (only output changes)

## Compaction Detection

Claude Code injects specific content when `/compact` is triggered:

### System Prompt Signal
```json
{
  "system": [
    {"type": "text", "text": "You are Claude Code..."},
    {"type": "text", "text": "You are a helpful AI assistant tasked with summarizing conversations."}
  ]
}
```

### Detection Code
```python
def is_compaction_request(self, body: dict) -> bool:
    """Detect compaction by Claude Code's injected system prompt."""
    system = body.get('system', [])
    for block in system:
        if isinstance(block, dict):
            if 'summarizing conversations' in block.get('text', ''):
                return True
    return False
```

## Progressive Compaction Strategy

### Early Compactions (1-5): GPT-5 Nano
- Content is low-density (verbose tool outputs, boilerplate)
- Easy compression task
- Generous output budget (60-80k tokens)
- 15B model sufficient with relaxed constraints

### Later Compactions (6-10): Larger Model (Optional)
- Content is high-density (already distilled)
- Harder compression task
- May need more capable model (Gemini Pro, etc.)
- Tighter output budget as information density increases

### Adaptive Summary Sizing
```
Compaction 1: 200k → 80k (gentle, preserve everything)
Compaction 2: 180k → 70k (still relaxed)
Compaction 3: 170k → 65k (manageable)
...
Compaction 8+: Density requires more aggressive compression
```

**Key insight:** Trade tokens (cheap) for parameters (expensive). Small model + generous space = comparable quality to large model + constrained space.

## Thinking Block Management

### The Signature Problem
- Claude's thinking blocks have cryptographic signatures
- External models cannot generate valid signatures
- Mixing signed/unsigned thinking blocks violates API requirements

### Lifecycle

```
Session Start
    ↓
Full thinking preserved (valid signatures)
    ↓
Normal work...
    ↓
Compaction 1 triggered
    ↓
├─ Strip thinking from history (can't send to external model)
├─ Route to GPT-5 Nano
├─ Receive summary (no thinking blocks)
├─ Mark session for no-thinking mode
└─ Return summary to Claude Code
    ↓
Session continues (thinking stripped from all subsequent responses)
    ↓
Compaction 2+ (already in no-thinking mode)
```

### Key Points
- Full thinking quality preserved until first compaction
- Thinking only stripped when it would be summarized anyway
- No quality degradation during actual work
- After compaction 1: session operates in no-thinking mode permanently

## Architecture

### Request Flow
```
Claude Code → Proxy → Detect compaction?
                         ├─ No  → Forward to Anthropic API
                         └─ Yes → Transform & route to OpenAI API
                                     ↓
                              GPT-5 Nano processes
                                     ↓
                              Transform response back
                                     ↓
                              Mark session no-thinking
                                     ↓
                              Return to Claude Code
```

### Components

1. **Detection Layer**
   - Pattern match on system prompt
   - Identify compaction requests

2. **Request Transformer**
   - Claude messages → OpenAI messages
   - Strip thinking blocks from history
   - Preserve compaction prompt

3. **Response Transformer**
   - OpenAI response → Claude SSE format
   - Stream back to Claude Code

4. **Session State Manager**
   - Track post-compaction sessions
   - Enable thinking block filtering

## Implementation Outline

```python
class ExternalCompactionHandler:
    def __init__(self, openrouter_api_key: str):
        self.api_key = openrouter_api_key
        self.api_base = 'https://openrouter.ai/api/v1'
        self.compacted_sessions: set[str] = set()
        self.compaction_count: dict[str, int] = {}  # session_id -> count

        # Model configuration
        self.models = {
            'early': 'google/gemini-flash-lite',   # Compactions 1-5
            'late': 'google/gemini-3-flash',       # Compactions 6-10
        }

    def is_compaction_request(self, body: dict) -> bool:
        """Detect compaction by system prompt."""
        system = body.get('system', [])
        for block in system:
            if isinstance(block, dict):
                if 'summarizing conversations' in block.get('text', ''):
                    return True
        return False

    def strip_thinking_blocks(self, messages: list) -> list:
        """Remove thinking blocks from message history."""
        for msg in messages:
            if msg.get('role') == 'assistant':
                content = msg.get('content', [])
                if isinstance(content, list):
                    msg['content'] = [
                        block for block in content
                        if block.get('type') != 'thinking'
                    ]
        return messages

    def claude_to_openai(self, body: dict, model: str, compaction_num: int) -> dict:
        """Transform Claude request to OpenAI/OpenRouter format."""
        messages = self.strip_thinking_blocks(body.get('messages', []))

        # Convert message format to OpenAI format
        openai_messages = []

        # Add system prompt first
        system_text = ''
        for block in body.get('system', []):
            if isinstance(block, dict):
                system_text += block.get('text', '') + '\n'
        if system_text:
            openai_messages.append({'role': 'system', 'content': system_text.strip()})

        # Convert conversation messages
        for msg in messages:
            role = msg['role']  # 'user' or 'assistant' work directly
            content = msg.get('content', [])

            # Flatten content blocks to text
            if isinstance(content, list):
                text_parts = []
                for block in content:
                    if isinstance(block, dict) and block.get('type') == 'text':
                        text_parts.append(block.get('text', ''))
                    elif isinstance(block, str):
                        text_parts.append(block)
                text = '\n'.join(text_parts)
            else:
                text = content

            openai_messages.append({'role': role, 'content': text})

        # Generous early, tighter later (but never too aggressive)
        max_tokens = max(80000 - (compaction_num * 5000), 50000)

        return {
            'model': model,
            'messages': openai_messages,
            'max_tokens': max_tokens,
            'stream': True
        }

    def openai_to_claude_sse(self, chunk: dict) -> bytes:
        """Transform OpenAI streaming chunk to Claude SSE format."""
        choices = chunk.get('choices', [])
        if not choices:
            return b''

        delta = choices[0].get('delta', {})
        text = delta.get('content', '')

        if not text:
            return b''

        # Format as Claude SSE
        claude_event = {
            'type': 'content_block_delta',
            'index': 0,
            'delta': {'type': 'text_delta', 'text': content}
        }

        return f'event: content_block_delta\ndata: {json.dumps(claude_event)}\n\n'.encode()

    def select_model(self, session_id: str) -> str:
        """Select model based on compaction count."""
        count = self.compaction_count.get(session_id, 0) + 1
        self.compaction_count[session_id] = count

        if count <= 5:
            return self.models['early']
        else:
            return self.models['late']

    async def handle_compaction(self, session_id: str, body: dict) -> AsyncIterator[bytes]:
        """Route compaction to external model via OpenRouter."""
        model = self.select_model(session_id)
        count = self.compaction_count[session_id]

        logger.info(f"Compaction {count} for session {session_id} using {model}")

        # Transform request for OpenRouter (OpenAI-compatible format)
        openrouter_request = self.claude_to_openai(body, model, count)

        # Send to OpenRouter
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f'{self.api_base}/chat/completions',
                headers={
                    'Authorization': f'Bearer {self.api_key}',
                    'HTTP-Referer': 'https://github.com/anthropics/claude-code',
                    'X-Title': 'Claude Code CCM',
                },
                json=openrouter_request
            ) as response:
                # Stream response back in Claude format
                yield self._message_start_event()
                yield self._content_block_start_event()

                async for line in response.content:
                    if line.startswith(b'data: '):
                        data = line[6:].strip()
                        if data == b'[DONE]':
                            break
                        chunk = json.loads(data)
                        claude_chunk = self.openai_to_claude_sse(chunk)
                        if claude_chunk:
                            yield claude_chunk

                yield self._content_block_stop_event()
                yield self._message_stop_event()

        # Mark session for no-thinking
        self.compacted_sessions.add(session_id)

    def is_post_compaction_session(self, session_id: str) -> bool:
        """Check if session has been externally compacted."""
        return session_id in self.compacted_sessions
```

## Integration with Existing Proxy

The thinking-proxy.py already handles:
- Request/response interception
- Thinking block filtering (ThinkingBlockFilter class)
- Session state management (STATE_DIR)
- SSE streaming

Add external compaction as new capability:

```python
async def handle_request(self, request):
    body = await request.read()
    body_json = json.loads(body)
    session_id = get_session_id(dict(request.headers))

    # Check for compaction request
    if self.compaction_handler.is_compaction_request(body_json):
        logger.info(f"Routing compaction to external model for session {session_id}")
        response = web.StreamResponse()
        await response.prepare(request)

        async for chunk in self.compaction_handler.handle_compaction(session_id, body_json):
            await response.write(chunk)

        return response

    # Check if post-compaction (needs thinking filtered)
    if self.compaction_handler.is_post_compaction_session(session_id):
        # Enable thinking block filtering
        ...

    # Normal request handling
    ...
```

## Relationship to Other CCM Components

### Hooks + Cache + Stubs (Layer 1)
- Proactive: Prevents large outputs from consuming context
- Always active during session
- Unchanged by external compaction

### External Compaction (Layer 2)
- Reactive: Handles context overflow
- Automatic, triggered by Claude Code
- Replaces manual /purge for overflow handling

### /purge Command
- Becomes optional optimization
- Layer 1 (hooks) + Layer 2 (external compaction) handle most cases
- Still available for manual intervention if desired

## Autocompact Buffer Patch

The CLI has a bug where `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` uses `Math.min` which only allows
*lowering* the threshold (larger buffer), not *raising* it (smaller buffer).

### Patch Script

`~/.claude/hooks/patch-autocompact.py` patches `Math.min` → `Math.max` in the autocompact function.

```bash
# Check if patch needed
~/.claude/hooks/patch-autocompact.py --check

# Apply patch
~/.claude/hooks/patch-autocompact.py --patch

# Restore original
~/.claude/hooks/patch-autocompact.py --restore
```

### Wrapper Script

`~/.claude/hooks/c` automatically:
1. Patches the CLI if needed
2. Sets `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=95` (5% buffer)
3. Starts the thinking proxy
4. Runs claude

Update your alias:
```bash
alias c='~/.claude/hooks/c --dangerously-skip-permissions'
```

### Robust to Updates

The patch script uses heuristics to find the correct location:
- Searches for `AUTOCOMPACT_PCT_OVERRIDE` env var reference
- Looks for nearby `parseFloat` and `/100` (percentage calculation)
- Finds `Math.min(X,Y)}}return` pattern
- Caches patch state by file hash to avoid re-checking

If the CLI updates, the script re-detects and re-patches.

## Configuration

```python
# ~/.claude/hooks/config.py

# External compaction settings
EXTERNAL_COMPACTION_ENABLED = True

# API key loaded from ~/.claude/credentials.json or OPENROUTER_API_KEY env var
# credentials.json format:
# {
#   "openrouter": {
#     "api_key": "sk-or-v1-your-key-here"
#   }
# }

# Model selection by compaction number (OpenRouter model IDs)
COMPACTION_MODELS = {
    'early': 'google/gemini-3-flash-preview',   # Compactions 1-5
    'late': 'google/gemini-3-flash-preview',    # Compactions 6+
}

# Output token limits (scale up with compaction number)
COMPACTION_MAX_TOKENS = {
    1: 20000,
    2: 36000,
    3: 52000,
    4: 64000,
    5: 64000,
    'default': 64000  # Gemini 3 Flash caps at 64k
}
```

## Quality Considerations

### Compounding Effect
With 10 compactions per session:
- 95% quality per compaction → 60% preserved at compaction 10
- 98% quality per compaction → 82% preserved at compaction 10

### Mitigation Strategies
1. **Generous early output** - Less compression = less loss
2. **Structured prompting** - Smaller models follow explicit instructions well
3. **Model escalation** - Use larger model for later compactions
4. **Quality monitoring** - Track summary sizes and user-reported issues

## Future Enhancements

1. **Compaction counter** - Track number of compactions per session for model selection
2. **Density analysis** - Measure information density to adjust compression strategy
3. **Parallel compaction** - Pre-emptively compact in background before hitting limit
4. **Custom compaction prompts** - Allow user-defined summarization instructions
5. **Hybrid model routing** - Route different content types to specialized models
