import sys as _s, os as _o
_s.path.insert(0, _o.path.dirname(_o.path.dirname(_o.path.abspath(__file__))))
#!/usr/bin/env python3
"""
Installation Test Script for MCP Server Inspector

This script verifies that all dependencies are correctly installed
and the framework is ready to use.

Run this after installation to ensure everything is working:
    python test_installation.py
"""

import sys
from pathlib import Path


def test_imports():
    """Test that all required packages can be imported."""
    print("🔍 Testing imports...")
    
    required_packages = [
        ("langchain", "LangChain"),
        ("langgraph", "LangGraph"),
        ("langchain_mcp_adapters", "LangChain MCP Adapters"),
        ("langchain_openai", "LangChain OpenAI"),
        ("pydantic", "Pydantic"),
        ("dotenv", "python-dotenv"),
    ]
    
    failed = []
    
    for package, name in required_packages:
        try:
            __import__(package)
            print(f"  ✅ {name}")
        except ImportError as e:
            print(f"  ❌ {name} - {e}")
            failed.append(name)
    
    if failed:
        print(f"\n❌ Failed to import: {', '.join(failed)}")
        print("Run: pip install -r requirements.txt")
        return False
    
    print("✅ All imports successful\n")
    return True


def test_project_structure():
    """Test that all required directories and files exist."""
    print("🔍 Testing project structure...")
    
    project_root = Path(__file__).parent.parent
    
    required_paths = [
        "src/agents/mcp_discovery_agent.py",
        "src/agents/mcp_analysis_agent.py",
        "src/models/structured_output.py",
        "src/utility/utils.py",
        "src/utility/mcp_connection_manager.py",
        "src/workflows/mcp_inspector_workflow.py",
        "src/prompts/discovery_agent_instruction.md",
        "src/prompts/analysis_agent_instruction.md",
        "examples/example_servers.json",
        "requirements.txt",
        "README.md",
    ]
    
    missing = []
    
    for path_str in required_paths:
        path = project_root / path_str
        if path.exists():
            print(f"  ✅ {path_str}")
        else:
            print(f"  ❌ {path_str}")
            missing.append(path_str)
    
    if missing:
        print(f"\n❌ Missing files: {', '.join(missing)}")
        return False
    
    print("✅ Project structure valid\n")
    return True


def test_configuration():
    """Test that configuration is valid."""
    print("🔍 Testing configuration...")
    
    project_root = Path(__file__).parent.parent
    
    # Check .env file
    env_file = project_root / ".env"
    if not env_file.exists():
        print("  ⚠️  .env file not found")
        print("     Create one from .env.example and add your OpenAI API key")
        has_env = False
    else:
        print("  ✅ .env file exists")
        has_env = True
        
        # Check if API key is set
        with open(env_file) as f:
            content = f.read()
            if "OPENAI_API_KEY=sk-" in content and "your-" not in content:
                print("  ✅ OpenAI API key appears to be set")
            else:
                print("  ⚠️  OpenAI API key not set or uses placeholder")
                print("     Update .env with your actual API key")
    
    # Check example_servers.json
    servers_file = project_root / "examples" / "example_servers.json"
    if servers_file.exists():
        try:
            import json
            with open(servers_file) as f:
                config = json.load(f)
            servers = config.get("servers", [])
            print(f"  ✅ example_servers.json valid ({len(servers)} servers)")
        except json.JSONDecodeError:
            print("  ❌ example_servers.json is not valid JSON")
            return False
    else:
        print("  ❌ example_servers.json not found")
        return False
    
    print("✅ Configuration valid\n")
    return has_env


def test_module_imports():
    """Test that internal modules can be imported."""
    print("🔍 Testing internal modules...")
    
    try:
        from src.models.structured_output import MCPServerInspectionReport
        print("  ✅ Models module")
        
        from src.utility.utils import Utils
        print("  ✅ Utils module")
        
        from src.utility.mcp_connection_manager import MCPConnectionManager
        print("  ✅ Connection manager module")
        
        from src.agents.mcp_discovery_agent import mcp_discovery_agent
        print("  ✅ Discovery agent module")
        
        from src.agents.mcp_analysis_agent import mcp_analysis_agent
        print("  ✅ Analysis agent module")
        
        from src.workflows.mcp_inspector_workflow import run_mcp_inspection
        print("  ✅ Workflow module")
        
        print("✅ All modules importable\n")
        return True
        
    except ImportError as e:
        print(f"  ❌ Module import failed: {e}\n")
        return False


def test_node_js():
    """Test that Node.js is installed (required for MCP servers)."""
    print("🔍 Testing Node.js installation...")
    
    import subprocess
    
    try:
        result = subprocess.run(
            ["node", "--version"],
            capture_output=True,
            text=True,
            timeout=5
        )
        
        if result.returncode == 0:
            version = result.stdout.strip()
            print(f"  ✅ Node.js {version} installed")
            return True
        else:
            print("  ❌ Node.js not working properly")
            return False
            
    except FileNotFoundError:
        print("  ❌ Node.js not found")
        print("     Install Node.js from https://nodejs.org/")
        return False
    except subprocess.TimeoutExpired:
        print("  ❌ Node.js check timed out")
        return False


def main():
    """Run all tests."""
    print("\n" + "="*60)
    print("  MCP SERVER INSPECTOR - INSTALLATION TEST")
    print("="*60 + "\n")
    
    tests = [
        ("Python Imports", test_imports),
        ("Project Structure", test_project_structure),
        ("Configuration", test_configuration),
        ("Internal Modules", test_module_imports),
        ("Node.js", test_node_js),
    ]
    
    results = []
    
    for test_name, test_func in tests:
        try:
            result = test_func()
            results.append((test_name, result))
        except Exception as e:
            print(f"❌ {test_name} test crashed: {e}\n")
            results.append((test_name, False))
    
    # Summary
    print("="*60)
    print("TEST SUMMARY")
    print("="*60 + "\n")
    
    passed = sum(1 for _, result in results if result)
    total = len(results)
    
    for test_name, result in results:
        status = "✅ PASS" if result else "❌ FAIL"
        print(f"{status} - {test_name}")
    
    print(f"\nTotal: {passed}/{total} tests passed\n")
    
    if passed == total:
        print("🎉 All tests passed! You're ready to use MCP Server Inspector.")
        print("\nNext steps:")
        print("  1. Ensure your OpenAI API key is set in .env")
        print("  2. Run: python src/workflows/mcp_inspector_workflow.py")
        print("  3. Check: reports/ directory for results")
        print("\nSee QUICKSTART.md for more information.\n")
        return 0
    else:
        print("⚠️  Some tests failed. Please fix the issues above before using the framework.\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())

