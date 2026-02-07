#!/usr/bin/env python3
"""Search script to find CodeLab problem solving code in index.html"""

import re

search_terms = [
    "handleCodeExecution",
    "handleAIAssist", 
    "loadCodeLabProblem",
    "handleGenerateProblem",
    "ai-mentor-panel",
    "problem-generation-container",
    "codelab-output-panel",
    "newProblemBtn",
    "codelab",
    "problem",
    "workspace-panel",
    "submitCode",
    "runCode"
]

def search_file(filepath, terms):
    with open(filepath, 'r', encoding='utf-8') as f:
        content = f.read()
        lines = content.split('\n')
    
    print(f"File has {len(lines)} lines\n")
    
    for term in terms:
        print(f"\n=== Searching for: '{term}' ===")
        found = False
        for i, line in enumerate(lines, 1):
            if term.lower() in line.lower():
                found = True
                # Show truncated line
                display_line = line[:100] + "..." if len(line) > 100 else line
                print(f"  Line {i}: {display_line.strip()}")
        if not found:
            print(f"  NOT FOUND")

if __name__ == "__main__":
    search_file(r"c:\Users\nikhi\Stellar\index.html", search_terms)
