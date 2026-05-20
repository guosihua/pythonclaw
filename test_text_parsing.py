"""Test the text-based tool call parsing fallback mechanism."""

import json
import sys
import os
import re

# Add project root to path
project_root = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, project_root)

def test_text_parsing():
    """Test various text formats for tool call parsing logic."""
    
    print("="*60)
    print("Testing Text-Based Tool Call Parsing Logic")
    print("="*60)
    
    test_cases = [
        {
            "name": "JSON format in code block",
            "text": '''I'll use the tool.
```json
{
  "tool": "use_skill",
  "arguments": {"skill_name": "static-troubleshooting"}
}
```''',
            "expected_tool": "use_skill",
            "strategy": "JSON"
        },
        {
            "name": "Python code block",
            "text": '''I'll check the weather.
```python
get_weather("Beijing", "CN")
```''',
            "expected_tool": "get_weather",
            "strategy": "Code block"
        },
        {
            "name": "Direct JSON",
            "text": '{"tool": "use_skill", "arguments": {"skill_name": "test"}}',
            "expected_tool": "use_skill",
            "strategy": "Direct JSON"
        },
        {
            "name": "Skill activation pattern",
            "text": 'Let me activate the skill: use_skill("static-troubleshooting")',
            "expected_tool": "use_skill",
            "strategy": "Pattern match"
        },
        {
            "name": "Troubleshooting intent (Chinese)",
            "text": '我来帮你进行静态路由故障排查。我需要检查路由表。',
            "expected_intent": "troubleshooting",
            "strategy": "Intent detection"
        }
    ]
    
    for i, test_case in enumerate(test_cases, 1):
        print(f"\n[Test {i}] {test_case['name']}")
        print(f"Strategy: {test_case['strategy']}")
        print(f"Input preview: {test_case['text'][:80]}...")
        
        # Test Strategy 1: JSON parsing
        if test_case.get('expected_tool') == 'use_skill' and ('json' in test_case['text'].lower() or '{' in test_case['text']):
            json_pattern = r'```json\s*([\s\S]*?)\s*```|(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})'
            matches = re.findall(json_pattern, test_case['text'])
            if matches:
                print(f"✅ Strategy 1 (JSON): Found JSON pattern")
                for match in matches:
                    candidate = match[0] if match[0] else match[1]
                    if candidate:
                        try:
                            parsed = json.loads(candidate)
                            if "tool" in parsed:
                                print(f"   - Parsed tool: {parsed['tool']}")
                                if "arguments" in parsed:
                                    print(f"   - Arguments: {json.dumps(parsed['arguments'])}")
                        except json.JSONDecodeError:
                            pass
        
        # Test Strategy 2: Code block parsing
        if '```python' in test_case['text'] or '```' in test_case['text']:
            code_pattern = r'```(?:python)?\s*([a-zA-Z_][a-zA-Z0-9_]*)\(([^)]*)\)\s*```'
            code_matches = re.findall(code_pattern, test_case['text'], re.MULTILINE)
            if code_matches:
                func_name, args_str = code_matches[0]
                print(f"✅ Strategy 2 (Code block): Detected function call")
                print(f"   - Function: {func_name}")
                print(f"   - Arguments: {args_str}")
        
        # Test Strategy 3: Pattern matching
        skill_patterns = [
            r'use_skill\s*\(\s*["\']([^"\']+)["\']\s*\)',
            r'skill_name\s*[=:]\s*["\']([^"\']+)["\']',
        ]
        for pattern in skill_patterns:
            skill_match = re.search(pattern, test_case['text'], re.IGNORECASE)
            if skill_match:
                skill_name = skill_match.group(1).strip()
                print(f"✅ Strategy 3 (Pattern): Detected skill activation")
                print(f"   - Skill name: {skill_name}")
        
        # Test Strategy 4: Intent detection
        troubleshooting_keywords = ['排查', '诊断', 'troubleshoot', 'diagnose', 'static route', '静态路由']
        if any(kw in test_case['text'].lower() for kw in troubleshooting_keywords):
            print(f"✅ Strategy 4 (Intent): Detected troubleshooting intent")
            matched_keywords = [kw for kw in troubleshooting_keywords if kw in test_case['text'].lower()]
            print(f"   - Matched keywords: {matched_keywords}")
            print(f"   - Suggested action: use_skill(static-troubleshooting)")
        
        print(f"Expected: {test_case.get('expected_tool', test_case.get('expected_intent', 'N/A'))}")

if __name__ == "__main__":
    test_text_parsing()
    print("\n" + "="*60)
    print("All test cases completed!")
    print("="*60)
