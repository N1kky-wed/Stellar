# quota.py
"""
Utilities for checking, parsing, and management of agent API quotas.
Communicates with the docker container's CLI to read the /usage stats.
"""
import sys
import time
import re
import logging
from typing import Dict, Any

logger = logging.getLogger("stellar-orchestrator")

def parse_quota_text(text: str) -> Dict[str, Any]:
    """
    Parse the raw terminal output of the /usage command into a structured dictionary.

    Args:
        text (str): Raw terminal output containing the quota status.

    Returns:
        Dict[str, Any]: Parsed quota details containing Gemini and Claude limits.
    """
    result = {
        "gemini": {
            "account": "Unknown",
            "weekly_percent": 100.0,
            "weekly_refreshes_in_hours": 0.0,
            "sprint_percent": 100.0,
            "sprint_refreshes_in_hours": 0.0,
            "sprint_disabled": False,
            "ratio": 100.0,
            "status": "Unknown",
            "error": None
        },
        "claude": {
            "account": "Unknown",
            "weekly_percent": 100.0,
            "weekly_refreshes_in_hours": 0.0,
            "sprint_percent": 100.0,
            "sprint_refreshes_in_hours": 0.0,
            "sprint_disabled": False,
            "ratio": 100.0,
            "status": "Unknown",
            "error": None
        }
    }
    
    try:
        # Clean ANSI escape sequences
        clean_text = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', text)
        clean_text = re.sub(r'\x1b\(B', '', clean_text)
        # Remove control characters
        clean_text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', clean_text)
        
        # Parse account email
        email_match = re.search(r'Account:\s*([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})', clean_text)
        account_email = email_match.group(1) if email_match else "Unknown"
        result["gemini"]["account"] = account_email
        result["claude"]["account"] = account_email

        # Split into Gemini and Claude sections
        gemini_part = ""
        claude_part = ""
        
        gemini_idx = clean_text.find("GEMINI MODELS")
        claude_idx = clean_text.find("CLAUDE AND GPT MODELS")
        
        if gemini_idx != -1:
            if claude_idx != -1:
                gemini_part = clean_text[gemini_idx:claude_idx]
                claude_part = clean_text[claude_idx:]
            else:
                gemini_part = clean_text[gemini_idx:]
        elif claude_idx != -1:
            claude_part = clean_text[claude_idx:]
            
        def parse_section(section_text: str) -> dict:
            info = {
                "weekly_percent": 100.0,
                "weekly_refreshes_in_hours": 0.0,
                "sprint_percent": 100.0,
                "sprint_refreshes_in_hours": 0.0,
                "sprint_disabled": False
            }
            
            # Weekly Limit %
            pct_match = re.search(r'Weekly Limit\s*[\s\S]*?\[.*?\]\s*([\d.]+)%', section_text)
            if pct_match:
                info["weekly_percent"] = float(pct_match.group(1))
            
            # Weekly Limit Refresh Time (look only before Five Hour Limit)
            weekly_text = section_text
            five_hour_idx = section_text.find("Five Hour Limit")
            if five_hour_idx != -1:
                weekly_text = section_text[:five_hour_idx]
                
            ref_match = re.search(r'Refreshes in\s*(?:(\d+)h)?\s*(?:(\d+)m)?', weekly_text)
            if ref_match:
                h = int(ref_match.group(1)) if ref_match.group(1) else 0
                m = int(ref_match.group(2)) if ref_match.group(2) else 0
                info["weekly_refreshes_in_hours"] = h + (m / 60.0)
                
            # Five Hour Limit Block
            if "Five Hour Limit" in section_text:
                five_hour_text = section_text
                if five_hour_idx != -1:
                    five_hour_text = section_text[five_hour_idx:]
                    
                # Robust check: look for "Disabled" specifically near the header
                disabled_match = re.search(r'Five Hour Limit\s*\n?\s*Disabled:', five_hour_text, re.IGNORECASE)
                if disabled_match:
                    info["sprint_disabled"] = True
                    info["sprint_percent"] = 0.0
                else:
                    sprint_pct_match = re.search(r'Five Hour Limit\s*[\s\S]*?\[.*?\]\s*([\d.]+)%', five_hour_text)
                    if sprint_pct_match:
                        info["sprint_percent"] = float(sprint_pct_match.group(1))
                    
                    sprint_ref_match = re.search(r'Refreshes in\s*(?:(\d+)h)?\s*(?:(\d+)m)?', five_hour_text)
                    if sprint_ref_match:
                        h = int(sprint_ref_match.group(1)) if sprint_ref_match.group(1) else 0
                        m = int(sprint_ref_match.group(2)) if sprint_ref_match.group(2) else 0
                        info["sprint_refreshes_in_hours"] = h + (m / 60.0)
            return info

        if gemini_part:
            result["gemini"].update(parse_section(gemini_part))
        if claude_part:
            result["claude"].update(parse_section(claude_part))
            
    except Exception as e:
        logger.error(f"Error parsing quota text: {e}", exc_info=True)
        result["gemini"]["error"] = str(e)
        result["claude"]["error"] = str(e)
        
    # Calculate ratios and statuses mathematically
    for key in ["gemini", "claude"]:
        model_info = result[key]
        if model_info["error"]:
            model_info["status"] = "Error"
            continue
            
        weekly_pct = model_info["weekly_percent"]
        ref_hours = model_info["weekly_refreshes_in_hours"]
        ref_days = ref_hours / 24.0
        
        if ref_days > 0:
            ratio = weekly_pct / ref_days
        else:
            ratio = 100.0 # reset has occurred or unknown
            
        model_info["ratio"] = round(ratio, 2)
        
        # Decide status based on threshold (14.3% daily target)
        if weekly_pct <= 0.0:
            model_info["status"] = "Exhausted"
        elif model_info["sprint_disabled"]:
            model_info["status"] = "Sprint Disabled"
        elif model_info["sprint_percent"] < 10.0 and model_info["sprint_refreshes_in_hours"] > 0:
            model_info["status"] = "Sprint Exhausted"
        elif ratio < 14.3:
            model_info["status"] = "Throttled"
        else:
            model_info["status"] = "Healthy"
            
    return result


def fetch_quota_data_from_container(model: str = "Claude Sonnet 4.6 (Thinking)") -> str:
    """
    Spawn agy /usage inside the container via pexpect and return the raw output text.

    Args:
        model (str): The model identifier used to run agy.

    Returns:
        str: Raw output string captured from the command.
    """
    # Inline import of pexpect to speed up startup time
    import pexpect

    cmd = f'docker exec -it stellar-persistent /root/.local/bin/agy --model "{model}" --dangerously-skip-permissions'
    logger.info(f"Running pexpect command inside container: {cmd}")
    
    child = pexpect.spawn(cmd, encoding='utf-8', timeout=30)
    output_captured = []
    
    def read_callback(self, data):
        output_captured.append(data)
        
    child.logfile_read = type('Logger', (object,), {'write': read_callback, 'flush': lambda self: None})()
    
    try:
        # Wait for ? for shortcuts
        child.expect(r'\? for shortcuts', timeout=60)
        time.sleep(1)
        
        # Send /usage\r
        child.send('/usage\r')
        
        # Wait for Models & Quota title
        child.expect(r'Models & Quota', timeout=30)
        time.sleep(1)
        
        # Scroll to bottom using Page Down
        child.send('\x1b[6~')
        time.sleep(1)
        child.send('\x1b[6~')
        time.sleep(1)
        
        # Read the rest until quiet/timeout
        try:
            while True:
                out = child.read_nonblocking(size=1024, timeout=3)
                if not out:
                    break
        except (pexpect.TIMEOUT, pexpect.EOF):
            pass
            
    except Exception as e:
        logger.error(f"Pexpect quota query failed: {e}")
        output_captured.append(f"\nERROR: {e}")
    finally:
        child.close()
        
    return "".join(output_captured)
