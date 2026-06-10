#!/usr/bin/env python3
"""
Stellar Agent Prompt Compiler & Mailer
Reads all agent prompt files, compiles them into a single document,
and emails it for review.
"""
import smtplib
import os
import json
from email.message import EmailMessage

AGENTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agents")
REVIEWER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "scratch", "code-review-plugin", "agents", "code-reviewer.json")

# Agent execution order with schedules (3-hour gaps starting 6 AM IST)
AGENT_ORDER = [
    ("bolt",     "Bolt",     "Performance Engineer",    "06:00 AM IST"),
    ("sentinel", "Sentinel", "Security Engineer",       "09:00 AM IST"),
    ("palette",  "Palette",  "UI Engineer",             "12:00 PM IST"),
    ("newton",   "Newton",   "Test Engineer",           "03:00 PM IST"),
    ("lucios",   "Lucios",   "Observability Engineer",  "06:00 PM IST"),
    ("proton",   "Proton",   "Documentation Engineer",  "09:00 PM IST"),
]

RECIPIENT = "nikhil080905@gmail.com"
SENDER = "stellarai.live@gmail.com"
SENDER_PASS = "xhlb etoe kunw poas"

def compile_prompts():
    """Read all agent prompts and compile into a single markdown document."""
    sections = []
    
    # Header
    sections.append("# Stellar Autonomous Agent System — Complete Prompt Registry\n")
    sections.append("**Generated for review**\n")
    sections.append("---\n")
    
    # Execution order table
    sections.append("## Execution Order & Schedule\n")
    sections.append("| # | Agent | Role | Schedule |")
    sections.append("|---|-------|------|----------|")
    for i, (_, name, role, schedule) in enumerate(AGENT_ORDER, 1):
        sections.append(f"| {i} | **{name}** | {role} | {schedule} |")
    sections.append("")
    sections.append("**+ Code Reviewer** — On-demand quality gate (invoked by each agent before PR submission)\n")
    sections.append("---\n")
    
    # Each engineering agent
    for slug, name, role, schedule in AGENT_ORDER:
        filepath = os.path.join(AGENTS_DIR, f"{slug}.txt")
        if os.path.exists(filepath):
            with open(filepath, "r") as f:
                content = f.read().strip()
            sections.append(f"## {name} — {role}")
            sections.append(f"**Schedule:** {schedule}\n")
            sections.append("```")
            sections.append(content)
            sections.append("```\n")
            sections.append("---\n")
        else:
            sections.append(f"## {name} — {role}")
            sections.append(f"⚠️ **FILE NOT FOUND:** `agents/{slug}.txt`\n")
            sections.append("---\n")
    
    # Code Reviewer
    sections.append("## Code Reviewer — Quality Gate (On-Demand)")
    sections.append("**Trigger:** Invoked by each engineering agent before PR submission\n")
    if os.path.exists(REVIEWER_PATH):
        with open(REVIEWER_PATH, "r") as f:
            reviewer_data = json.load(f)
        # Extract the system prompt sections
        spec = reviewer_data.get("customAgentSpec", {}).get("customAgent", {})
        prompt_sections = spec.get("systemPromptSections", [])
        tools = spec.get("toolNames", [])
        
        for ps in prompt_sections:
            sections.append(f"### {ps['title']}\n")
            sections.append("```")
            sections.append(ps["content"])
            sections.append("```\n")
        
        sections.append(f"**Permitted Tools:** {', '.join(f'`{t}`' for t in tools)}\n")
    else:
        sections.append("⚠️ **FILE NOT FOUND:** `code-reviewer.json`\n")
    
    sections.append("---\n")
    sections.append("*End of prompt registry. Reply with feedback or approve to proceed with orchestrator implementation.*")
    
    return "\n".join(sections)


def send_email(body_md: str):
    """Send the compiled prompt document via email."""
    import markdown
    
    msg = EmailMessage()
    msg["Subject"] = "[STELLAR] Agent Prompt Registry — Review Required"
    msg["From"] = f"Stellar System <{SENDER}>"
    msg["To"] = RECIPIENT
    
    # Plain text fallback
    msg.set_content(body_md + "\n\n---\nTransmission from your Stellar Environment.")
    
    # HTML version
    html_body = markdown.markdown(body_md, extensions=["extra", "codehilite", "tables", "fenced_code"])
    html_content = f"""
    <html>
      <head>
        <style>
          body {{ font-family: 'Segoe UI', sans-serif; line-height: 1.6; color: #e0e0e0; background: #0a0a0f; padding: 20px; }}
          h1 {{ color: #50fa7b; border-bottom: 2px solid #333; padding-bottom: 10px; }}
          h2 {{ color: #8be9fd; margin-top: 30px; border-bottom: 1px solid #333; padding-bottom: 6px; }}
          h3 {{ color: #bd93f9; }}
          code {{ background: #1a1a2e; padding: 2px 6px; border-radius: 4px; color: #f8f8f2; font-size: 0.9em; }}
          pre {{ background: #1a1a2e; padding: 14px; border-radius: 8px; overflow-x: auto; border: 1px solid #333; color: #f8f8f2; }}
          pre code {{ background: none; padding: 0; }}
          table {{ border-collapse: collapse; width: 100%; margin: 14px 0; }}
          th, td {{ border: 1px solid #333; padding: 10px; text-align: left; }}
          th {{ background-color: #1a1a2e; color: #50fa7b; }}
          td {{ background-color: #0d0d14; }}
          hr {{ border: 0; border-top: 1px solid #222; margin: 24px 0; }}
          strong {{ color: #ff79c6; }}
          .footer {{ font-size: 0.85em; color: #555; margin-top: 30px; }}
        </style>
      </head>
      <body>
        {html_body}
        <div class="footer">
          <hr>
          Transmission from your Stellar Environment.
        </div>
      </body>
    </html>
    """
    msg.add_alternative(html_content, subtype="html")
    
    # Also save compiled file to outputs for attachment
    output_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "agent_prompt_registry.md")
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        f.write(body_md)
    
    # Attach the .md file
    with open(output_path, "rb") as fp:
        msg.add_attachment(
            fp.read(),
            maintype="text",
            subtype="markdown",
            filename="agent_prompt_registry.md"
        )
    
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(SENDER, SENDER_PASS)
        smtp.send_message(msg)
    
    print(f"✅ Email sent to {RECIPIENT}")
    print(f"📎 Compiled file saved to: {output_path}")


if __name__ == "__main__":
    print("📋 Compiling all agent prompts...")
    compiled = compile_prompts()
    print(f"📄 Compiled {len(compiled)} chars across {len(AGENT_ORDER) + 1} agents")
    print(f"📧 Sending to {RECIPIENT}...")
    send_email(compiled)
