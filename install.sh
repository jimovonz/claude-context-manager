#!/bin/bash
# Claude Context Manager - Remote Installer
# Usage: curl -fsSL https://raw.githubusercontent.com/jimovonz/claude-context-manager/main/install.sh | bash

set -e

CCM_DIR="${HOME}/.claude-ccm"
REPO="https://github.com/jimovonz/claude-context-manager.git"

echo "Installing Claude Context Manager..."

# Clone or update
if [ -d "$CCM_DIR" ]; then
    echo "Updating existing installation..."
    git -C "$CCM_DIR" pull --quiet
else
    echo "Cloning repository..."
    git clone --quiet "$REPO" "$CCM_DIR"
fi

# Install Python dependencies
echo "Installing Python dependencies..."
if command -v pip3 &>/dev/null; then
    pip3 install --quiet --user aiohttp tiktoken 2>/dev/null || true
elif command -v pip &>/dev/null; then
    pip install --quiet --user aiohttp tiktoken 2>/dev/null || true
else
    echo "Warning: pip not found. Install manually: pip install aiohttp tiktoken"
fi

# Run installer
echo "Configuring hooks..."
python3 "$CCM_DIR/install.py"

# Check PATH
if ! echo "$PATH" | grep -q "$HOME/.local/bin"; then
    echo ""
    echo "Add to your shell rc file (~/.bashrc or ~/.zshrc):"
    echo '  export PATH="$HOME/.local/bin:$PATH"'
    echo ""
    echo "Then restart your shell or run: source ~/.bashrc"
fi

echo ""
echo "✓ Installation complete!"
echo ""
echo "Usage:"
echo "  c                  # Launch Claude with CCM"
echo "  c --resume <id>    # Resume a session"
echo ""
echo "Optional: Set up OpenRouter for external compaction:"
echo "  See: $CCM_DIR/README.md#external-compaction-optional"
