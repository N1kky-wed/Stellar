# config.py
"""
Configuration settings for the Stellar Autonomous Agent Orchestrator.
Defines execution orders, paths, schedules, environment layouts, models, and thresholds.
"""
import os

# Agent execution order with schedules (3-hour gaps starting 6 AM IST)
AGENT_PIPELINE = [
    {"id": "bolt",     "name": "Bolt",     "role": "Performance Engineer",    "schedule": "06:00", "prompt_file": "bolt.md"},
    {"id": "sentinel", "name": "Sentinel", "role": "Security Engineer",       "schedule": "09:00", "prompt_file": "sentinel.md"},
    {"id": "palette",  "name": "Palette",  "role": "UI Engineer",             "schedule": "12:00", "prompt_file": "palette.md"},
    {"id": "newton",   "name": "Newton",   "role": "Test Engineer",           "schedule": "15:00", "prompt_file": "newton.md"},
    {"id": "lucios",   "name": "Lucios",   "role": "Observability Engineer",  "schedule": "18:00", "prompt_file": "lucios.md"},
    {"id": "proton",   "name": "Proton",   "role": "Documentation Engineer",  "schedule": "21:00", "prompt_file": "proton.md"},
    {"id": "mercury",  "name": "Mercury",  "role": "Reliability Engineer",    "schedule": "event-based", "prompt_file": "mercury.md"},
]

CONTAINER_NAME = 'stellar-persistent'
CONTAINER_WORKSPACE = '/root/Stellar'
AGY_BINARY = '/root/.local/bin/agy'

HOST_AGENTS_DIR = '/home/stellaradmin/my_app/agents'
HOST_REVIEWER_DIR = '/home/stellaradmin/my_app/scratch/code-review-plugin'

CONTAINER_AGENTS_DIR = '/root/.agents'
CONTAINER_REVIEWER_DIR = '/root/.gemini/antigravity-cli/plugins/code-review'

DB_PATH = '/home/stellaradmin/my_app/orchestrator/orchestrator.db'
MEMORY_DB_PATH = '/home/stellaradmin/my_app/orchestrator/memory.db'
LOG_PATH = '/home/stellaradmin/my_app/orchestrator/orchestrator.log'
TIMEZONE = 'Asia/Kolkata'

MAX_AGENT_RUNTIME_MINUTES = 45  # watchdog timeout
PR_CHECK_INTERVAL_SECONDS = 60
GITHUB_REPO = 'N1kky-wed/Stellar'
GITHUB_BRANCH_PREFIX = 'agent/'

# Models
MODEL_GEMINI = "Gemini 3.5 Flash (High)"
MODEL_CLAUDE = "Claude Sonnet 4.6 (Thinking)"

# Quota Spacing Targets
DAILY_PACING_TARGET_PERCENT = 100.0 / 7.0  # ~14.2857% daily target for weekly limit

