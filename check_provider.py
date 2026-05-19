"""
Quick script to check which LLM provider is currently configured.
Run with: python check_provider.py
"""

import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from pythonclaw import config

def check_provider():
    """Check and display the current LLM provider configuration."""
    
    print("\n" + "="*60)
    print("PythonClaw - LLM Provider Check")
    print("="*60 + "\n")
    
    # Get provider name
    provider_name = config.get_str("llm", "provider", env="LLM_PROVIDER", default="deepseek")
    print(f"Provider Name: {provider_name}")
    
    # Check if it's H3C AI
    if provider_name.lower() in ("h3c", "h3cai"):
        print("\n✅ Using H3C Internal AI Platform")
        print("-" * 60)
        
        account = config.get_str("llm", "h3c", "account", default="ts_sn")
        model = config.get_str("llm", "h3c", "model", default="DEEPSEEK_V3_PRIVATE")
        auth_url = config.get_str("llm", "h3c", "authUrl", default="https://api-ai.h3c.com/session/api/user/login")
        api_endpoint = config.get_str("llm", "h3c", "apiEndpoint", default="https://api-ai.h3c.com/session/ai/chat/deepseek")
        
        print(f"Account:       {account}")
        print(f"Model:         {model}")
        print(f"Auth URL:      {auth_url}")
        print(f"API Endpoint:  {api_endpoint}")
        print(f"\nStatus:        🟢 Company Internal Model")
        
    else:
        print(f"\nUsing External Provider: {provider_name}")
        print("-" * 60)
        
        # Try to get model info for other providers
        provider_cfg = config.as_dict().get("llm", {}).get(provider_name, {})
        model = provider_cfg.get("model", "N/A")
        base_url = provider_cfg.get("baseUrl", "N/A")
        
        print(f"Model:         {model}")
        print(f"Base URL:      {base_url}")
        print(f"\nStatus:        🔵 External Provider")
    
    print("\n" + "="*60 + "\n")
    
    return provider_name

if __name__ == "__main__":
    try:
        provider = check_provider()
        
        # Exit with appropriate code
        if provider.lower() in ("h3c", "h3cai"):
            print("✓ Confirmed: Using H3C Internal AI Platform\n")
            sys.exit(0)
        else:
            print(f"✗ Not using H3C AI (currently using: {provider})\n")
            sys.exit(1)
            
    except Exception as e:
        print(f"\n❌ Error checking provider: {e}\n")
        sys.exit(2)
