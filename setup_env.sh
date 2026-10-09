#!/bin/bash
# MCP Server Inspector - Secure Environment Setup Script
# 
# This script helps you create a .env file with your API tokens
# safely without exposing them in git.

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="$SCRIPT_DIR/.env"
ENV_TEMPLATE="$SCRIPT_DIR/env_template.txt"

echo "============================================================"
echo "     MCP Server Inspector - Secure Token Setup"
echo "============================================================"
echo ""

# Check if .env already exists
if [ -f "$ENV_FILE" ]; then
    echo "⚠️  WARNING: .env file already exists!"
    echo ""
    read -p "Do you want to overwrite it? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "❌ Cancelled. Keeping existing .env file."
        exit 0
    fi
fi

# Create .env from template
if [ -f "$ENV_TEMPLATE" ]; then
    cp "$ENV_TEMPLATE" "$ENV_FILE"
    echo "✅ Created .env file from template"
else
    echo "❌ Error: env_template.txt not found!"
    exit 1
fi

echo ""
echo "============================================================"
echo "  Step 1: Verify .env is Protected by Git"
echo "============================================================"

# Check if .env is in .gitignore
if grep -q "^\.env$" "$SCRIPT_DIR/.gitignore" 2>/dev/null; then
    echo "✅ .env is in .gitignore (safe from git commits)"
else
    echo "⚠️  .env is NOT in .gitignore!"
    echo "   Adding it now for your safety..."
    echo ".env" >> "$SCRIPT_DIR/.gitignore"
    echo "✅ Added .env to .gitignore"
fi

echo ""
echo "============================================================"
echo "  Step 2: Add Your API Tokens"
echo "============================================================"
echo ""
echo "Now you need to edit .env and add your actual tokens."
echo ""
echo "📍 Location: $ENV_FILE"
echo ""
echo "🔐 Required tokens:"
echo "   • OPENAI_API_KEY          - For AI analysis"
echo "   • GITHUB_PERSONAL_ACCESS_TOKEN - For GitHub MCP server"
echo ""
echo "🔐 Optional tokens (if using these services):"
echo "   • NOTION_TOKEN            - For Notion MCP server"
echo "   • LINEAR_API_KEY          - For Linear MCP server"
echo ""
echo "📖 Token guides:"
echo "   • GitHub: GITHUB_MCP_SETUP.md"
echo "   • All services: GETTING_API_TOKENS.md"
echo "   • Security: SECURITY_BEST_PRACTICES.md"
echo ""

# Ask if user wants to edit now
read -p "Open .env file for editing now? (Y/n): " -n 1 -r
echo
if [[ ! $REPLY =~ ^[Nn]$ ]]; then
    # Try to open with best available editor
    if [ -n "$EDITOR" ]; then
        $EDITOR "$ENV_FILE"
    elif command -v nano &> /dev/null; then
        nano "$ENV_FILE"
    elif command -v vi &> /dev/null; then
        vi "$ENV_FILE"
    else
        echo "⚠️  No editor found. Please manually edit:"
        echo "   $ENV_FILE"
    fi
fi

echo ""
echo "============================================================"
echo "  Step 3: Verify Token Format"
echo "============================================================"
echo ""

# Check if tokens have been updated from placeholders
if grep -q "your-openai-key-here" "$ENV_FILE" 2>/dev/null; then
    echo "⚠️  WARNING: OpenAI key still has placeholder value!"
    echo "   Update it with your actual key from: https://platform.openai.com/api-keys"
fi

if grep -q "your-token-here" "$ENV_FILE" 2>/dev/null; then
    echo "⚠️  WARNING: Some tokens still have placeholder values!"
    echo "   Remember to update them before running the inspector."
fi

echo ""
echo "✅ Expected token formats:"
echo "   • OpenAI:  sk-proj-xxxxx..."
echo "   • GitHub:  github_pat_xxxxx... or ghp_xxxxx..."
echo "   • Notion:  secret_xxxxx..."
echo "   • Linear:  lin_api_xxxxx..."
echo ""

echo "============================================================"
echo "  Step 4: Test Your Setup"
echo "============================================================"
echo ""
echo "To verify your tokens work, run:"
echo ""
echo "  # Test GitHub token"
echo "  source .venv/bin/activate"
echo "  python -m src.workflows.mcp_inspector_workflow --server GitHub-ReadOnly"
echo ""
echo "  # Or test endpoint directly"
echo "  source .env && curl -H \"Authorization: token \$GITHUB_PERSONAL_ACCESS_TOKEN\" https://api.github.com/user"
echo ""

echo "============================================================"
echo "  ✅ Setup Complete!"
echo "============================================================"
echo ""
echo "📁 Your .env file: $ENV_FILE"
echo "🔒 Protected by .gitignore: YES"
echo "📝 Next: Add your actual API tokens to .env"
echo ""
echo "💡 Pro tip: Never commit .env to git!"
echo "   Your actual tokens are safe in .env"
echo "   Config files only have placeholders like \${VAR_NAME}"
echo ""

