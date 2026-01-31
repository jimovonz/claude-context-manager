"""Project Settings Extractor for CCM Compaction.

Extracts project metadata from conversation messages using regex/text parsing.
No LLM required - purely programmatic extraction for guaranteed preservation.

Extraction tiers:
  Tier 1 (high reliability): files accessed, commands run, project roots
  Tier 2 (medium reliability): git state, SSH/relay hosts, ports/endpoints
  Tier 3 (lower reliability): dependencies, env vars

Fuzzy deduplication:
  Compares extracted data against LLM output to avoid redundancy.
  Only includes items the LLM missed (gap-fill mode).
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse


@dataclass
class CoverageStats:
    """Track what was extracted vs what the LLM already covered."""
    files_total: int = 0
    files_new: int = 0
    commands_total: int = 0
    commands_new: int = 0
    hosts_total: int = 0
    hosts_new: int = 0
    endpoints_total: int = 0
    endpoints_new: int = 0
    ports_total: int = 0
    ports_new: int = 0
    config_total: int = 0
    config_new: int = 0

    @property
    def total_items(self) -> int:
        return (self.files_total + self.commands_total + self.hosts_total +
                self.endpoints_total + self.ports_total + self.config_total)

    @property
    def new_items(self) -> int:
        return (self.files_new + self.commands_new + self.hosts_new +
                self.endpoints_new + self.ports_new + self.config_new)

    @property
    def coverage_pct(self) -> float:
        """Percentage the LLM already covered."""
        if self.total_items == 0:
            return 100.0
        covered = self.total_items - self.new_items
        return (covered / self.total_items) * 100

    @property
    def gap_pct(self) -> float:
        """Percentage of new items (gaps the LLM missed)."""
        if self.total_items == 0:
            return 0.0
        return (self.new_items / self.total_items) * 100

    def summary(self) -> str:
        """One-line summary for header."""
        if self.total_items == 0:
            return "no items"
        return f"{self.new_items}/{self.total_items} new ({self.gap_pct:.0f}% gap-fill)"


@dataclass
class FileAccess:
    """Track file access with operation counts."""
    path: str
    reads: int = 0
    edits: int = 0
    writes: int = 0

    @property
    def ops_str(self) -> str:
        parts = []
        if self.reads:
            parts.append(f"Read ×{self.reads}")
        if self.edits:
            parts.append(f"Edit ×{self.edits}")
        if self.writes:
            parts.append(f"Write ×{self.writes}")
        return ", ".join(parts) if parts else "accessed"


@dataclass
class ProjectContext:
    """Extracted project context from conversation."""
    # Tier 1
    files: dict[str, FileAccess] = field(default_factory=dict)
    commands: list[str] = field(default_factory=list)
    project_roots: set[str] = field(default_factory=set)
    working_dirs: list[str] = field(default_factory=list)

    # Tier 2
    git_branch: Optional[str] = None
    git_status: Optional[str] = None  # e.g., "3 commits ahead"
    git_remote: Optional[str] = None
    ssh_hosts: set[str] = field(default_factory=set)
    relay_hosts: set[str] = field(default_factory=set)
    endpoints: set[str] = field(default_factory=set)
    ports: set[str] = field(default_factory=set)

    # Tier 3
    dependencies: dict[str, list[str]] = field(default_factory=dict)  # type -> [deps]
    env_vars: set[str] = field(default_factory=set)  # var names only, not values
    config_files: set[str] = field(default_factory=set)


class ProjectSettingsExtractor:
    """Extract project metadata from conversation messages via regex.

    Scans tool_use inputs (structured JSON) and tool_result outputs (regex patterns)
    to build a comprehensive project context for compaction preservation.
    """

    # File patterns that indicate project roots
    ROOT_MARKERS = {
        '.git', 'package.json', 'Cargo.toml', 'pyproject.toml',
        'requirements.txt', 'go.mod', 'Makefile', 'CLAUDE.md'
    }

    # Config file patterns
    CONFIG_PATTERNS = [
        r'\.env(\.\w+)?$',
        r'config\.(json|yaml|yml|toml|py)$',
        r'settings\.(json|yaml|yml|toml|py)$',
        r'credentials\.\w+$',
    ]

    # Dependency file mappings
    DEP_FILES = {
        'package.json': 'npm',
        'package-lock.json': 'npm',
        'requirements.txt': 'pip',
        'Pipfile': 'pipenv',
        'pyproject.toml': 'python',
        'Cargo.toml': 'cargo',
        'go.mod': 'go',
        'Gemfile': 'ruby',
    }

    def __init__(self):
        self.context = ProjectContext()
        self._seen_commands = set()  # Dedup commands

    def extract(self, messages: list) -> ProjectContext:
        """Extract project context from conversation messages.

        Args:
            messages: List of Claude API message dicts with role/content

        Returns:
            ProjectContext with all extracted metadata
        """
        self.context = ProjectContext()
        self._seen_commands = set()

        for msg in messages:
            content = msg.get('content', [])
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get('type', '')
                        if block_type == 'tool_use':
                            self._process_tool_use(block)
                        elif block_type == 'tool_result':
                            self._process_tool_result(block)
                    elif isinstance(block, str):
                        self._extract_from_text(block)
            elif isinstance(content, str):
                self._extract_from_text(content)

        # Post-processing
        self._infer_project_roots()
        self._deduplicate()

        return self.context

    def _process_tool_use(self, block: dict) -> None:
        """Extract from tool_use blocks (structured, high reliability)."""
        name = block.get('name', '')
        inp = block.get('input', {})

        # Tier 1: File operations
        if name == 'Read':
            path = inp.get('file_path', '')
            if path:
                self._record_file_access(path, 'read')

        elif name == 'Edit':
            path = inp.get('file_path', '')
            if path:
                self._record_file_access(path, 'edit')

        elif name == 'Write':
            path = inp.get('file_path', '')
            if path:
                self._record_file_access(path, 'write')

        # Tier 1: Commands
        elif name == 'Bash':
            cmd = inp.get('command', '')
            if cmd:
                self._process_command(cmd)

        # Tier 1: Search paths
        elif name in ('Grep', 'Glob'):
            path = inp.get('path', '')
            if path:
                self._add_working_dir(path)

    def _process_tool_result(self, block: dict) -> None:
        """Extract from tool_result blocks (regex on output, medium reliability)."""
        content = block.get('content', '')

        # Handle nested content structures
        if isinstance(content, list):
            for item in content:
                if isinstance(item, dict):
                    text = item.get('text', '')
                    if text:
                        self._extract_from_tool_output(text)
        elif isinstance(content, str):
            self._extract_from_tool_output(content)

    def _extract_from_tool_output(self, text: str) -> None:
        """Extract patterns from tool output text."""
        # Tier 2: Git state
        self._extract_git_state(text)

        # Tier 2: Endpoints and ports
        self._extract_endpoints(text)
        self._extract_ports(text)

        # Tier 3: Dependencies from file contents
        self._extract_dependencies(text)

        # Tier 3: Environment variables
        self._extract_env_vars(text)

    def _extract_from_text(self, text: str) -> None:
        """Extract patterns from assistant/user text blocks."""
        # Less reliable - only extract high-confidence patterns
        self._extract_endpoints(text)

    def _record_file_access(self, path: str, op_type: str) -> None:
        """Record a file access with operation type."""
        # Normalize path
        path = path.strip()
        if not path:
            return

        if path not in self.context.files:
            self.context.files[path] = FileAccess(path=path)

        fa = self.context.files[path]
        if op_type == 'read':
            fa.reads += 1
        elif op_type == 'edit':
            fa.edits += 1
        elif op_type == 'write':
            fa.writes += 1

        # Check for config files (Tier 3)
        basename = Path(path).name
        for pattern in self.CONFIG_PATTERNS:
            if re.match(pattern, basename, re.IGNORECASE):
                self.context.config_files.add(path)
                break

        # Check for dependency files (Tier 3)
        if basename in self.DEP_FILES:
            dep_type = self.DEP_FILES[basename]
            if dep_type not in self.context.dependencies:
                self.context.dependencies[dep_type] = []

    def _process_command(self, cmd: str) -> None:
        """Process a bash command for various extractions."""
        cmd = cmd.strip()
        if not cmd or cmd in self._seen_commands:
            return

        # Skip very short/trivial commands
        if len(cmd) < 3:
            return

        # Skip cd-only commands but extract the directory
        if cmd.startswith('cd '):
            dir_match = re.match(r'cd\s+([^\s;|&]+)', cmd)
            if dir_match:
                self._add_working_dir(dir_match.group(1))
            return

        # Record significant commands (filter out trivial ones)
        trivial_patterns = [
            r'^(ls|pwd|echo|cat|head|tail|wc|date|whoami)\b',
            r'^(true|false|:)$',
        ]
        is_trivial = any(re.match(p, cmd) for p in trivial_patterns)

        if not is_trivial:
            self._seen_commands.add(cmd)
            # Store up to 50 most recent commands
            if len(self.context.commands) < 50:
                self.context.commands.append(cmd)

        # Tier 2: SSH hosts
        ssh_match = re.search(r'\bssh\s+(?:-\S+\s+)*(?:(\S+)@)?(\S+)', cmd)
        if ssh_match:
            host = ssh_match.group(2)
            user = ssh_match.group(1)
            if user:
                self.context.ssh_hosts.add(f"{user}@{host}")
            else:
                self.context.ssh_hosts.add(host)

        # Tier 2: Relay pattern (from relay intercept)
        relay_match = re.match(r'^(\w+)\s+"(.+)"$', cmd)
        if relay_match:
            # Pattern like: pi "make -j4"
            host = relay_match.group(1)
            if host not in ('echo', 'cat', 'cd', 'export'):
                self.context.relay_hosts.add(host)

        # Tier 3: Environment variables
        export_match = re.search(r'\bexport\s+(\w+)=', cmd)
        if export_match:
            self.context.env_vars.add(export_match.group(1))

        # Extract working directory from cd within compound commands
        cd_in_cmd = re.search(r'cd\s+([^\s;|&]+)', cmd)
        if cd_in_cmd:
            self._add_working_dir(cd_in_cmd.group(1))

    def _add_working_dir(self, path: str) -> None:
        """Add a working directory if it looks like a real path."""
        path = path.strip().strip('"\'')
        if not path or path in ('.', '..'):
            return
        # Expand ~ but keep relative paths
        if path.startswith('~'):
            path = str(Path(path).expanduser())
        if path not in self.context.working_dirs:
            self.context.working_dirs.append(path)

    def _extract_git_state(self, text: str) -> None:
        """Extract git state from command outputs."""
        # Branch name from various sources
        branch_patterns = [
            r'On branch (\S+)',
            r'\* (\S+)',  # git branch output
            r'HEAD -> (\S+)',
        ]
        for pattern in branch_patterns:
            match = re.search(pattern, text)
            if match and not self.context.git_branch:
                self.context.git_branch = match.group(1)
                break

        # Ahead/behind status
        ahead_behind = re.search(r"Your branch is (ahead|behind).+?(\d+) commit", text)
        if ahead_behind:
            direction = ahead_behind.group(1)
            count = ahead_behind.group(2)
            self.context.git_status = f"{count} commits {direction}"

        # Remote
        remote_match = re.search(r'origin\s+(\S+)\s+\(fetch\)', text)
        if remote_match:
            self.context.git_remote = remote_match.group(1)

    def _extract_endpoints(self, text: str) -> None:
        """Extract API endpoints and URLs."""
        # HTTP(S) URLs
        url_pattern = r'https?://[^\s<>"\'{}|\\^`\[\])]+'
        for match in re.finditer(url_pattern, text):
            url = match.group(0).rstrip('.,;:')
            # Skip very long URLs (likely data)
            if len(url) < 200:
                self.context.endpoints.add(url)

    def _extract_ports(self, text: str) -> None:
        """Extract port references."""
        # localhost:PORT
        localhost_ports = re.findall(r'localhost:(\d{2,5})\b', text)
        for port in localhost_ports:
            self.context.ports.add(f"localhost:{port}")

        # 0.0.0.0:PORT or 127.0.0.1:PORT
        ip_ports = re.findall(r'(?:0\.0\.0\.0|127\.0\.0\.1):(\d{2,5})\b', text)
        for port in ip_ports:
            self.context.ports.add(f"localhost:{port}")

        # Standalone port mentions (less reliable)
        port_mentions = re.findall(r'\bport\s+(\d{2,5})\b', text, re.IGNORECASE)
        for port in port_mentions:
            self.context.ports.add(f"port {port}")

    def _extract_dependencies(self, text: str) -> None:
        """Extract dependencies from file contents."""
        # Python requirements.txt style
        if 'pip install' in text or re.search(r'^\w+[=<>]=', text, re.MULTILINE):
            deps = re.findall(r'^([\w-]+)[=<>]=?[\d.]+', text, re.MULTILINE)
            if deps:
                if 'pip' not in self.context.dependencies:
                    self.context.dependencies['pip'] = []
                self.context.dependencies['pip'].extend(deps[:20])  # Limit

        # package.json dependencies
        if '"dependencies"' in text or '"devDependencies"' in text:
            # Simple extraction of package names
            pkg_names = re.findall(r'"([@\w/-]+)":\s*"[\^~]?[\d.]+', text)
            if pkg_names:
                if 'npm' not in self.context.dependencies:
                    self.context.dependencies['npm'] = []
                self.context.dependencies['npm'].extend(pkg_names[:20])

    def _extract_env_vars(self, text: str) -> None:
        """Extract environment variable names (not values for security)."""
        # From .env file content: VAR_NAME=value
        env_vars = re.findall(r'^([A-Z][A-Z0-9_]+)=', text, re.MULTILINE)
        for var in env_vars:
            # Skip common noise
            if var not in ('PATH', 'HOME', 'USER', 'PWD', 'SHELL'):
                self.context.env_vars.add(var)

    def _infer_project_roots(self) -> None:
        """Infer project roots from accessed files."""
        # Find common path prefixes among files
        paths = list(self.context.files.keys())
        if not paths:
            return

        # Extract directory components
        dir_counts = defaultdict(int)
        for path in paths:
            try:
                p = Path(path)
                # Walk up the tree
                for parent in p.parents:
                    parent_str = str(parent)
                    if parent_str in ('/', '.'):
                        continue
                    dir_counts[parent_str] += 1
                    # Check for root markers
                    for marker in self.ROOT_MARKERS:
                        marker_path = parent / marker
                        if any(str(marker_path) in fp for fp in paths):
                            self.context.project_roots.add(parent_str)
            except Exception:
                pass

        # If no root markers found, use most common directory with 3+ files
        if not self.context.project_roots:
            for dir_path, count in sorted(dir_counts.items(), key=lambda x: -x[1]):
                if count >= 3:
                    self.context.project_roots.add(dir_path)
                    break

    def _deduplicate(self) -> None:
        """Deduplicate and clean up extracted data."""
        # Dedupe dependencies
        for dep_type in self.context.dependencies:
            self.context.dependencies[dep_type] = list(
                dict.fromkeys(self.context.dependencies[dep_type])
            )

        # Sort files by access count (most accessed first)
        self.context.files = dict(
            sorted(
                self.context.files.items(),
                key=lambda x: -(x[1].reads + x[1].edits * 2 + x[1].writes * 2)
            )
        )

    # =========================================================================
    # Fuzzy Deduplication Against LLM Output
    # =========================================================================

    def dedupe_against_llm(self, llm_output: str) -> tuple[ProjectContext, CoverageStats]:
        """Remove items from context that appear in LLM output.

        Uses fuzzy matching to detect when the LLM already captured an item.
        Returns a new ProjectContext with only gap-fill items, plus coverage stats.

        Args:
            llm_output: The LLM's distillation output text

        Returns:
            (deduped_context, coverage_stats)
        """
        ctx = self.context
        stats = CoverageStats()
        llm_lower = llm_output.lower()

        # Create new context for deduped items
        deduped = ProjectContext()

        # Files: keep if path/basename not found
        stats.files_total = len(ctx.files)
        for path, access in ctx.files.items():
            if not self._path_in_text(path, llm_lower):
                deduped.files[path] = access
                stats.files_new += 1

        # Commands: keep if command signature not found
        stats.commands_total = len(ctx.commands)
        for cmd in ctx.commands:
            if not self._command_in_text(cmd, llm_lower):
                deduped.commands.append(cmd)
                stats.commands_new += 1

        # SSH/Relay hosts: keep if hostname not found
        all_hosts = ctx.ssh_hosts | ctx.relay_hosts
        stats.hosts_total = len(all_hosts)
        for host in ctx.ssh_hosts:
            if not self._host_in_text(host, llm_lower):
                deduped.ssh_hosts.add(host)
                stats.hosts_new += 1
        for host in ctx.relay_hosts:
            if not self._host_in_text(host, llm_lower):
                deduped.relay_hosts.add(host)
                if host not in ctx.ssh_hosts:  # Don't double-count
                    stats.hosts_new += 1

        # Endpoints: keep if URL/domain not found
        stats.endpoints_total = len(ctx.endpoints)
        for endpoint in ctx.endpoints:
            if not self._endpoint_in_text(endpoint, llm_lower):
                deduped.endpoints.add(endpoint)
                stats.endpoints_new += 1

        # Ports: keep if port number not found
        stats.ports_total = len(ctx.ports)
        for port in ctx.ports:
            if not self._port_in_text(port, llm_lower):
                deduped.ports.add(port)
                stats.ports_new += 1

        # Config files: keep if not mentioned
        stats.config_total = len(ctx.config_files)
        for cf in ctx.config_files:
            if not self._path_in_text(cf, llm_lower):
                deduped.config_files.add(cf)
                stats.config_new += 1

        # Always include these (low redundancy risk, high value)
        deduped.project_roots = ctx.project_roots
        deduped.working_dirs = ctx.working_dirs
        deduped.git_branch = ctx.git_branch
        deduped.git_status = ctx.git_status
        deduped.git_remote = ctx.git_remote
        deduped.dependencies = ctx.dependencies
        deduped.env_vars = ctx.env_vars

        return deduped, stats

    def _path_in_text(self, path: str, text: str) -> bool:
        """Check if a file path appears in text (fuzzy match)."""
        path_lower = path.lower()

        # Check full path
        if path_lower in text:
            return True

        # Check basename (e.g., "main.py")
        try:
            basename = Path(path).name.lower()
            if len(basename) > 3 and basename in text:
                return True
        except Exception:
            pass

        # Check path without home directory
        if path_lower.startswith('/home/'):
            parts = path_lower.split('/', 3)
            if len(parts) > 3:
                relative = parts[3]  # After /home/user/
                if relative in text:
                    return True

        return False

    def _command_in_text(self, cmd: str, text: str) -> bool:
        """Check if a command appears in text (fuzzy match)."""
        cmd_lower = cmd.lower().strip()

        # Direct match
        if cmd_lower in text:
            return True

        # Check first significant token (e.g., "docker", "make", "npm")
        tokens = cmd_lower.split()
        if tokens:
            first_token = tokens[0]
            # Skip very common words
            if first_token not in ('cd', 'export', 'echo', 'cat') and len(first_token) > 2:
                # Look for token followed by similar arguments
                if first_token in text:
                    # Check if next 2-3 tokens also appear nearby
                    if len(tokens) > 1:
                        second = tokens[1] if len(tokens) > 1 else ""
                        if second and second in text:
                            return True

        # For compound commands, check key parts
        # e.g., "docker compose up" -> check "compose up"
        if ' ' in cmd_lower:
            # Get last 2-3 significant tokens
            parts = [t for t in tokens if len(t) > 2][-3:]
            if len(parts) >= 2:
                key_phrase = ' '.join(parts)
                if key_phrase in text:
                    return True

        return False

    def _host_in_text(self, host: str, text: str) -> bool:
        """Check if a hostname appears in text."""
        host_lower = host.lower()

        # Direct match
        if host_lower in text:
            return True

        # Strip user@ prefix
        if '@' in host_lower:
            hostname = host_lower.split('@')[1]
            if hostname in text:
                return True

        return False

    def _endpoint_in_text(self, endpoint: str, text: str) -> bool:
        """Check if an endpoint URL appears in text."""
        endpoint_lower = endpoint.lower()

        # Direct match
        if endpoint_lower in text:
            return True

        # Parse and check domain + path
        try:
            parsed = urlparse(endpoint_lower)
            domain = parsed.netloc
            path = parsed.path.rstrip('/')

            # Check domain
            if domain and domain in text:
                # If path is significant, check it too
                if path and len(path) > 3:
                    if path in text:
                        return True
                else:
                    return True  # Domain alone is enough
        except Exception:
            pass

        return False

    def _port_in_text(self, port_str: str, text: str) -> bool:
        """Check if a port reference appears in text."""
        # Extract port number
        port_match = re.search(r'(\d{2,5})', port_str)
        if port_match:
            port_num = port_match.group(1)
            # Look for port in various forms
            patterns = [
                f':{port_num}',
                f'port {port_num}',
                f'port: {port_num}',
            ]
            for pattern in patterns:
                if pattern in text:
                    return True
        return False

    def format(self, context: Optional[ProjectContext] = None, stats: Optional[CoverageStats] = None) -> str:
        """Format extracted context as appendable section.

        Args:
            context: ProjectContext to format (uses self.context if None)
            stats: Optional CoverageStats to show gap-fill percentage

        Returns:
            Formatted string section for appending to distillation
        """
        ctx = context or self.context

        # Check if there's anything to output
        has_content = (
            ctx.files or ctx.commands or ctx.ssh_hosts or ctx.relay_hosts or
            ctx.endpoints or ctx.ports or ctx.config_files or ctx.project_roots or
            ctx.git_branch
        )
        if not has_content:
            return ""

        # Header with coverage stats if available
        if stats and stats.total_items > 0:
            header = f"\n\n## PROJECT CONTEXT [gap-fill: {stats.summary()}]\n"
        else:
            header = "\n\n## PROJECT CONTEXT [auto-extracted]\n"
        lines = [header]

        # Project roots
        if ctx.project_roots:
            roots = ", ".join(sorted(ctx.project_roots))
            lines.append(f"ROOTS: {roots}")

        # Git state
        git_parts = []
        if ctx.git_branch:
            git_parts.append(ctx.git_branch)
        if ctx.git_status:
            git_parts.append(f"({ctx.git_status})")
        if ctx.git_remote:
            git_parts.append(f"← {ctx.git_remote}")
        if git_parts:
            lines.append(f"GIT: {' '.join(git_parts)}")

        # Working directories (if different from roots)
        work_dirs = [d for d in ctx.working_dirs if d not in ctx.project_roots]
        if work_dirs:
            lines.append(f"WORK DIRS: {', '.join(work_dirs[:5])}")

        lines.append("")  # Blank line

        # Files accessed (top 15)
        if ctx.files:
            lines.append("FILES (by activity):")
            for i, (path, fa) in enumerate(ctx.files.items()):
                if i >= 15:
                    remaining = len(ctx.files) - 15
                    lines.append(f"  ... and {remaining} more files")
                    break
                lines.append(f"  {path} — {fa.ops_str}")

        # Commands (top 20, grouped by type)
        if ctx.commands:
            lines.append("")
            lines.append("COMMANDS:")
            for cmd in ctx.commands[:20]:
                # Truncate very long commands
                display_cmd = cmd if len(cmd) < 100 else cmd[:97] + "..."
                lines.append(f"  {display_cmd}")
            if len(ctx.commands) > 20:
                lines.append(f"  ... and {len(ctx.commands) - 20} more")

        # SSH/Relay hosts
        all_hosts = ctx.ssh_hosts | ctx.relay_hosts
        if all_hosts:
            lines.append("")
            lines.append("REMOTE HOSTS:")
            for host in sorted(all_hosts):
                host_type = "relay" if host in ctx.relay_hosts else "ssh"
                lines.append(f"  {host} ({host_type})")

        # Endpoints
        if ctx.endpoints:
            lines.append("")
            lines.append("ENDPOINTS:")
            for endpoint in sorted(ctx.endpoints)[:10]:
                lines.append(f"  {endpoint}")
            if len(ctx.endpoints) > 10:
                lines.append(f"  ... and {len(ctx.endpoints) - 10} more")

        # Ports
        if ctx.ports:
            lines.append("")
            lines.append(f"PORTS: {', '.join(sorted(ctx.ports))}")

        # Config files
        if ctx.config_files:
            lines.append("")
            lines.append("CONFIG FILES:")
            for cf in sorted(ctx.config_files):
                lines.append(f"  {cf}")

        # Dependencies (summary)
        if ctx.dependencies:
            lines.append("")
            lines.append("DEPENDENCIES:")
            for dep_type, deps in ctx.dependencies.items():
                if deps:
                    dep_list = ", ".join(deps[:10])
                    suffix = f" (+{len(deps)-10} more)" if len(deps) > 10 else ""
                    lines.append(f"  {dep_type}: {dep_list}{suffix}")

        # Environment variables (names only)
        if ctx.env_vars:
            lines.append("")
            env_list = ", ".join(sorted(ctx.env_vars)[:15])
            suffix = f" (+{len(ctx.env_vars)-15} more)" if len(ctx.env_vars) > 15 else ""
            lines.append(f"ENV VARS: {env_list}{suffix}")

        lines.append("")  # Final newline

        return "\n".join(lines)


def extract_project_context(messages: list, llm_output: Optional[str] = None) -> str:
    """Convenience function to extract and format project context.

    When llm_output is provided, performs fuzzy deduplication to only include
    items that the LLM missed (gap-fill mode).

    Args:
        messages: List of Claude API message dicts
        llm_output: Optional LLM distillation output for deduplication

    Returns:
        Formatted project context section string (empty if nothing new)
    """
    extractor = ProjectSettingsExtractor()
    context = extractor.extract(messages)

    if llm_output:
        # Gap-fill mode: only include items LLM missed
        deduped, stats = extractor.dedupe_against_llm(llm_output)
        if stats.new_items == 0:
            return ""  # LLM covered everything
        return extractor.format(deduped, stats)
    else:
        # Full extraction mode (no LLM output to compare against)
        return extractor.format(context)
