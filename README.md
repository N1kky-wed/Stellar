# Stellar AI Operating System

> **A persistent, stateful, multi-user AI operating system delivering total autonomous agency across infrastructure, research, development, and security.**

Stellar is a production-grade Flask application powering [stellarai.site](https://stellarai.site) — a platform where users interact with a suite of AI agents (Crimson, Obsidian, Lunarity, Emerald) backed by the Gemini API. It is not merely a chatbot. It is a full AI runtime with native Docker orchestration, isolated sandboxed execution, persistent memory, autonomous scheduling, multi-modal content generation, and an extensible mandate system.

---

## Table of Contents

- [Architecture Overview](#architecture-overview)
- [Prerequisites](#prerequisites)
- [Setup & Installation](#setup--installation)
- [Environment Configuration (`keys.env`)](#environment-configuration-keysenv)
- [Docker Infrastructure](#docker-infrastructure)
- [Production Deployment (Nginx + Gunicorn + systemd)](#production-deployment-nginx--gunicorn--systemd)
- [Agent Models & Personas](#agent-models--personas)
- [Agent Tool Suite](#agent-tool-suite)
- [Human-in-the-Loop Interactive UI](#request_user_interactionhtml_ui-goal-status-timeout)
- [Live Interrupts & Stream Control](#live-interrupts--stream-control)
- [PWA & Push Notifications](#pwa--push-notifications)
- [Talent System](#talent-system)
- [Persistent Memory & Scheduling](#persistent-memory--scheduling)
- [Use Cases & Examples](#use-cases--examples)
- [API Key Management & Account Rotation](#api-key-management--account-rotation)
- [Security Model](#security-model)
- [Database Schema](#database-schema)
- [Testing](#testing)
- [Troubleshooting & Known Issues](#troubleshooting--known-issues)

---

## Architecture Overview

```
Internet ──► Nginx (HTTPS + wildcard *.stellarai.site)
                │
                ▼
          Gunicorn (gthread, 4 workers × 25 threads)
                │
                ▼
         Flask App (app.py)
         ├── Google OAuth / Firebase Auth
         ├── SQLite (WAL mode) ──── stellar_local.db
         ├── Redis (session / repo state / push subscriptions)
         ├── SSH TUI Gateway (stellar-ssh.service / port 22)
         ├── Agent Prompt Engine (prompts.py)
         └── Tool Execution Layer (agent_tools.py)
              ├── lab_execute  ──► stellar-lab-core Docker containers (per user/chat)
              ├── repo_control ──► stellar-repo-host Docker containers (per deployment)
```

Key design principles:
- **Per-user isolation** — every user gets their own Docker network (`stellar_net_<user_id>`) with ICC disabled.
- **Stateful persistence** — all repo deployments snapshot their file trees to SQLite on stop/restart.
- **Smart key rotation** — `GlobalKeyManager` tracks per-key, per-model rate limit blocks with automatic expiry and Pacific-midnight daily resets.
- **Streaming responses** — all agent responses are streamed to the frontend via Server-Sent Events (SSE).
- **Zero-leak interrupts** — cooperative `threading.Event`-based cancellation ensures that stopping a generation immediately halts both the LLM stream and in-flight tool calls without orphaned database records.
- **Live follow-up injection** — users can send additional messages while the agent is still generating, which are injected into the active LLM loop in real time.
- **Time-Aware Context** — The backend tracks relative time deltas between messages, providing the agent with a temporal understanding of the conversation flow.

---

## Prerequisites

| Requirement | Version / Notes |
|---|---|
| Python | 3.10+ |
| Docker Engine | 20.10+ (daemon must be running) |
| Redis | 6+ (running on `localhost:6379`) |
| Nginx | For production TLS termination |
| Pandoc | Required by `pypandoc` for document conversion |
| Node.js | Optional — only needed if you build the frontend separately |

**Docker Images Required** (build or pull before first run):

```bash
# Core lab sandbox image — used by lab_execute
docker pull your-registry/stellar-lab-core:latest

# Repo host image — used by repo_control deployments
docker pull your-registry/stellar-repo-host:latest
```

> The lab image must include: `bash`, `python3`, `pip`, `curl`, `git`, and any common data science / web scraping libraries.

---

## Setup & Installation

### 1. Clone the Repository

```bash
git clone <repository_url>
cd my_app
```

### 2. Create and Activate a Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install Python Dependencies

```bash
pip install -r requirements.txt
```

This installs all required packages including Flask, google-genai, docker SDK, Tavily, Redis client, cryptography, Twilio, and more. See [`requirements.txt`](requirements.txt) for the full pinned dependency list.

### 4. Configure Environment Variables

Copy or create `keys.env` in the project root (see the [Environment Configuration](#environment-configuration-keysenv) section below for all required keys):

```bash
cp keys.env.example keys.env
nano keys.env
```

### 5. Initialize the Docker Networks and Images

```bash
python3 dockersetup.py
```

This script builds the required `stellar-lab-core` and `stellar-repo-host` Docker images and creates the `stellar_isolated` bridge network with inter-container communication disabled.

### 6. Ensure Redis is Running

```bash
sudo systemctl start redis
sudo systemctl enable redis
```

### 7. Run the Application (Development)

```bash
python3 app.py
```

The application will start on `http://0.0.0.0:5000`. For development, Flask's built-in server is sufficient. For production, use Gunicorn (see below).

---

## Environment Configuration (`keys.env`)

All secrets and configuration are loaded from `keys.env` at startup. This file must never be committed to version control (it is already listed in `.gitignore`).

| Variable | Description | Required |
|---|---|---|
| `FLASK_SECRET_KEY` | Flask session signing secret. Use a long random string. | ✅ |
| `GEMINI_API_KEY` | Primary Gemini API key (Google AI Studio) | ✅ |
| `GEMINI_BACKUP_KEY_1` ... `_N` | Additional Gemini API keys for automatic quota rotation | Optional |
| `TAVILY_API_KEY` | Primary Tavily search/crawl API key | ✅ |
| `TAVILY_BACKUP_KEY_1` ... `_N` | Backup Tavily keys for rotation | Optional |
| `YOUTUBE_API_KEY` | YouTube Data API v3 key (for `analyze_youtube_video` search action) | ✅ |
| `EMAIL_USER` | Gmail address used by `send_self_email` | ✅ |
| `EMAIL_PASS` | Gmail app password (not account password) | ✅ |
| `FIREBASE_PROJECT_ID` | Firebase project ID for Google OAuth token verification | ✅ |
| `TWILIO_ACCOUNT_SID` | Twilio SID for SMS notifications | Optional |
| `TWILIO_AUTH_TOKEN` | Twilio auth token | Optional |
| `TWILIO_FROM_NUMBER` | Twilio phone number | Optional |
| `DATABASE_NAME` | Path to SQLite DB file (default: `stellar_local.db`) | Optional |
| `ENCRYPTION_KEY` | Fernet encryption key for sensitive stored data | ✅ |

> **Generating a Fernet Key:**
> ```python
> from cryptography.fernet import Fernet
> print(Fernet.generate_key().decode())
> ```

---

## Docker Infrastructure

Stellar uses Docker extensively. All AI-executed code runs inside isolated containers — never on the host directly.

### Lab Containers (`stellar-lab-core`)

- **Purpose:** Persistent bash sandboxes for `lab_execute` — running scripts, data analysis, installing packages, security research.
- **Naming:** `stellar-lab-u<user_id>-c<chat_id>` — one per user/chat session.
- **Mounts:**
  - `/lab` → `sandbox_runs/lab_workspace_u<uid>_c<cid>/` (host workspace, persisted across turns)
- **Lifecycle:** Started on first `lab_execute` call, persists until explicitly cleaned up or expired.
- **Mandate Injection:** Operational mandate files (`mandates/*.md`) are automatically injected into `/lab` so the agent reads them before executing specialized tasks.

### Repo Containers (`stellar-repo-host`)

- **Purpose:** Full application hosting environments for `repo_control` — deploy Node.js, React, Python Flask, Go, Ruby, or any custom stack.
- **Naming:** `stellar-repo-<process_id>`
- **Subdomain Routing:** Each deployment gets a unique subdomain `https://<name>.stellarai.site/` routed through Nginx.
- **Persistence:** File snapshots stored in SQLite (`repo_history.files_snapshot` column as JSON). Auto-snapshot occurs before any `stop` or `restart` action.
- **Lifespan:** Maximum 90 hours per container.
- **Mobile Builds:** Setting `env_type='mobile'` provisions a `reactnativecommunity/react-native-android` container instead.

### Network Isolation

```bash
# Each user gets a private bridge network
stellar_net_<user_id>   # ICC disabled — containers cannot talk to each other

# Global fallback network
stellar_isolated         # Also ICC-disabled for unresolved users
```

---

## Production Deployment (Nginx + Gunicorn + systemd)

### systemd Service

The `deploy/gunicorn_stellar.service` file configures Stellar as a managed system service:

```bash
sudo cp deploy/gunicorn_stellar.service /etc/systemd/system/stellar.service
sudo systemctl daemon-reload
sudo systemctl enable stellar
sudo systemctl start stellar
```

The service runs Gunicorn with:
- Worker class: `gthread` (gevent-compatible threaded workers)
- Workers: `4`, Threads per worker: `25`
- Timeout: `3600s` (long timeout for streaming AI responses)
- Bind: Unix socket `stellar.sock` (consumed by Nginx)

**To apply backend code changes:**
```bash
sudo systemctl restart stellar
```

### SSH TUI Gateway Service

Stellar includes a fully custom, interactive SSH Terminal User Interface (TUI) gateway running on port 2222, seamlessly proxied through the host's port 22 via the `stellar` system user. It provides a secure, text-based dashboard for managing AI-deployed Docker containers without needing direct host access.

**Authentication & Login Flow:**
Stellar completely replaces traditional SSH public-key authentication with a modern, short-lived device authorization flow tied to the user's web session:
1. The user initiates a connection via `ssh stellar@stellarai.site`.
2. The OpenSSH server matches the `stellar` user, disables all tunneling/port-forwarding, and forces the connection into the Python Paramiko SSH server (`ssh_gateway.py`).
3. The user is presented with an ASCII art prompt requesting a 6-character code.
4. The user visits `https://stellarai.site/auth/ssh` in their browser. Because this route is protected by `@require_approval`, the user **must be securely logged into their Stellar web account**.
5. The web app generates a cryptographically random 6-character code, ties it securely to the user's ID, stores it in Redis with a 5-minute TTL, and enforces a strict rate limit.
6. The user pastes this code into their SSH terminal. The gateway verifies the code against Redis via an internal API. Upon success, the session is instantly authenticated as the correct user without ever exposing server credentials or requiring public SSH keys.

**Dashboard Features:**
- **Container Management:** A beautiful Rich-powered terminal interface displaying all active and historical repository deployments owned by the user.
- **Interactive Docker Shells:** (The core feature) Users can select any running container and press `ENTER` to instantly drop into a fully interactive root `bash` PTY shell inside their sandboxed Docker container, effectively replacing the need to run `docker exec` on the host.
- **Live Telemetry:** View the current container status (Running, Stopped), creation timestamps, and routed subdomains in a clean table format.
- **Lifecycle Controls:** Users can navigate the list and instantly Stop or Restart their deployed containers directly from the terminal using keyboard controls.

**Usage:**
```bash
ssh stellar@stellarai.site
```

**Service Deployment:**
```bash
sudo cp stellar-ssh.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable stellar-ssh
sudo systemctl start stellar-ssh
```

### Nginx Configuration

`deploy/nginx_stellar.conf` configures TLS termination and reverse proxy for both the main domain and all wildcard subdomains:

```bash
sudo cp deploy/nginx_stellar.conf /etc/nginx/sites-available/stellar
sudo ln -s /etc/nginx/sites-available/stellar /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx
```

Key Nginx settings:
- **SSL:** Let's Encrypt certificates for `stellarai.site` and `*.stellarai.site`
- **Max body size:** 50MB (for file uploads)
- **Proxy timeouts:** 3600s (matching Gunicorn)
- **Buffering:** Disabled (`proxy_buffering off`) for real-time streaming
- **HTTP/1.1 upgrade:** Enabled for SSE compatibility

---

## Agent Models & Personas

Stellar supports four AI personas, each mapped to a specific Gemini model tier:

| Persona | Model | Infrastructure Access | Best For |
|---|---|---|---|
| **Obsidian** | `gemini-3.5-flash` | Lab + Repo | Complex reasoning, long multi-step tasks |
| **Crimson** | `gemini-3-flash-preview` | Lab + Repo | Fast execution, lower quota usage |
| **Lunarity** | `gemini-3.1-flash-lite` | Lab only | Lightweight tasks, error explanations |
| **Emerald** | `gemini-2.5-flash-lite` | None | Standard Q&A, no infrastructure |

All models share access to YouTube intelligence and standard tools. The agent dynamically selects which persona processes a request based on user preference and current quota availability.

---

## Agent Tool Suite

All tools are defined in [`agent_tools.py`](agent_tools.py) and exposed to the Gemini model as a function-calling schema. Every tool requires a `status` parameter (displayed to the user as a real-time progress update) and a `timeout`.

---

### `lab_execute(command, status, timeout)`

**The core execution primitive.** Runs arbitrary bash commands inside an isolated, persistent Docker sandbox.

- **Root access** — the agent runs as `root` inside the container.
- **Persistent workspace** — the `/lab` directory persists across all turns in a chat session.
- **Auto file sync** — files uploaded by the user are automatically synced to `/lab` before execution.
- **OCI error recovery** — if the container mount namespace breaks (exit code 128), the container is automatically recreated and the command is retried transparently.
- **Use cases:** Data science, web scraping, installing tools (`apt-get`, `pip`), running exploit scripts, compiling code, generating PDFs with WeasyPrint, running test suites.

```python
# Example: Run a Python data analysis script
lab_execute(
    command="pip install pandas && python3 analysis.py",
    status="Running data analysis...",
    timeout=120
)
```

---

### `repo_control(action, status, timeout, ...)`

**Full-stack application deployment and management.** Spins up dedicated Docker containers accessible via public HTTPS subdomains.

**Actions:**

| Action | Description |
|---|---|
| `deploy` | Provision a new container. Optionally clone a GitHub repo into it. Returns a live public URL. |
| `execute` | Run a bash command inside a running deployment (install deps, start servers, patch files). |
| `stop` | Gracefully stop and auto-snapshot all code files to SQLite before container destruction. |
| `restart` | Stop + re-provision a fresh container, restoring all snapshotted files. |
| `snapshot` | Manually trigger a file snapshot of specific paths. |
| `list_history` | List all past deployments with URLs and statuses. |
| `rename` | Change a deployment's display name and generate a new public subdomain. |

**Key behaviors:**
- Server health is automatically verified post-start (HTTP status check on the internal port).
- The agent is required to bind servers to `0.0.0.0` for the ingress router to work.
- Pre-flight dependency installation and server start must be **separate `execute` calls** to prevent OOM kills.
- For mobile builds (Android APKs, React Native), pass `env_type='mobile'`.

---

### `web_search(action, status, timeout, ...)`

**Unified OSINT and web intelligence suite** powered by Tavily.

**Actions:**

| Action | Description |
|---|---|
| `tavily_search` | Deep semantic search with optional AI summary, image extraction, and date filtering |
| `tavily_extract` | Full-page markdown/HTML extraction of up to 20 URLs simultaneously |
| `tavily_crawl` | Recursive site crawling with configurable depth and path filters |
| `tavily_map` | Domain architecture mapping — discovers all reachable URLs on a site |

**Advanced parameters:** Topic filtering (`general`, `news`, `finance`), domain inclusion/exclusion lists, exact phrase matching, time range filters (`d`, `w`, `m`, `y`), natural language crawler instructions, and image extraction with automatic dead-link verification.

---


**End-to-end AI-generated PowerPoint presentations.**

1. Uses `gemini-2.5-flash` with structured JSON output to plan slide titles, summaries, and visual layouts.
2. Generates each slide as a full-bleed 16:9 AI image using `gemini-3.1-flash-image-preview` — all slides are generated concurrently via `asyncio`.
3. Assembles a `.pptx` file with images embedded as full-slide pictures.
4. Returns a download link and slide preview URLs (rendered as an interactive carousel by the frontend).

### `regenerate_presentation_slide(presentation_id, slide_index, ...)`

Re-generates a single slide in an existing presentation using the original slide image as a reference, with user-specified feedback to guide the revision.

---

### `analyze_youtube_video(query, status, timeout, action, ...)`

**Multi-modal YouTube intelligence.**

- **`action='search'`** — queries the YouTube Data API v3, returns up to 50 results enriched with view counts, like counts, duration, and full descriptions, sorted by popularity.
- **`action='analyze'`** — feeds the video directly to Gemini's multimodal model. Supports `start_time`/`end_time` offsets and configurable `fps` sampling for precise segment analysis.

---

### `generate_image(model, prompt, status, timeout, quality, aspect_ratio, reference_images)`

**Native image generation** using Gemini's Imagen models.

- **Models:** `gemini-3.1-flash-image-preview` (fast) or `gemini-3-pro-image-preview` (high quality).
- **Quality tiers:** `512`, `1K`, `2K`, `4K`.
- **Aspect ratios:** `1:1`, `3:4`, `4:3`, `9:16`, `16:9`.
- **Reference images:** Pass up to 14 uploaded filenames for image editing, style transfer, or conditioning.
- Generated images are saved to `outputs/` and served at `https://stellarai.site/view/<filename>`.

---

### `schedule_task(task_prompt, status, timeout, action, ...)`

**Persistent autonomous task scheduler.** Tasks are stored in SQLite and executed by a background scheduler thread even when the user is offline.

- **`action='schedule'`** — create a new task. Supports one-time (`execute_at`) or recurring (`recurring_minutes`) execution. Maximum 10 active tasks per user.
- **`action='list'`** — inspect all active tasks with their next run times.
- **`action='cancel'`** — deactivate a task by ID (cannot cancel a running task).
- **`action='edit'`** — modify an existing task's prompt, schedule, or metadata.
- **`metadata`** — a scratchpad for retry state (e.g., tracking which attempt a polling loop is on).

---

### `logs_and_preferences(status, timeout, write)`

**Persistent long-term memory.** The agent's "brain" between sessions.

- Writes preferences, user facts, past errors, and verified resolution strategies to `user_logs_prefs` in SQLite.
- Memory is automatically injected into the system prompt at the start of every conversation turn — no explicit "read" action is needed.
- Limited to the last 100 entries per user to prevent bloat.
- **Intended for high-signal, permanent data only.** Transient retry state belongs in `schedule_task.metadata`.

---

### `manage_files(action, status, timeout, file_name, target_env, source_env)`

**Cross-environment file transfer** between chat uploads, lab sandboxes, and repo containers.

| Action | Description |
|---|---|
| `read` | List all files currently uploaded in the chat context |
| `move` | Transfer a file or directory between environments (chat → lab, lab → repo, etc.) |
| `project` | Export a file or directory from a container to the host `outputs/` folder, making it downloadable/previewable by the user |

Directories are automatically compressed as `.tar.gz` before projection.

---

### `send_self_email(subject, body, status, timeout, attachment_path)`

**Secure closed-loop mailer.** Sends emails only to the authenticated user's own registered address.

- Body is rendered as rich HTML from Markdown (with syntax highlighting via `codehilite`).
- Supports attaching files from `outputs/`, `uploads/`, or `sandbox_runs/` directories.
- Uses Gmail SMTP over SSL (port 465).

---

### `read_tool_output(output_id, status, timeout, keyword, start_line, max_lines)`

**Paginated log retrieval.** Fetches the full, untruncated output of a past tool call from the database.

- Essential when the chat history shows `[Output truncated]` for large outputs (build logs, data dumps).
- Supports keyword filtering — returns only lines containing a search term with their original line numbers.
- Paginated via `start_line` + `max_lines`.

---

### `compress_memory(target, state_document, status, timeout)`

**Context window management.** When the system detects high context usage, the agent calls this tool to archive older tool logs and/or messages.

- **`target`:** `'tool_logs'`, `'chat_messages'`, or `'both'`.
- **Mechanism:** Sets `hidden = 1` on older records while keeping the most recent entries visible (10 tool calls, 4 messages).
- **State preservation:** A structured `state_document` (objectives, discoveries, modified files, blockers) is inserted as a hidden message prefixed with `[COMPRESSED MEMORY STATE]`, ensuring the LLM retains critical context even after compression.
- **Triggered automatically** when `get_refinement_prompt()` injects a context usage warning into the system prompt.

---

### `request_user_interaction(html_ui, goal, status, timeout)`

**Human-in-the-Loop Stateful UI — Stellar's most powerful interactive capability.** This tool supercharges the agent with a whole new dimension of interactivity by allowing it to render rich, fully interactive HTML widgets directly inside the chat and **pause execution** until the user responds.

The agent generates a complete, self-contained HTML/CSS/JS widget. The user interacts with it (clicks buttons, selects options, makes moves). The JavaScript calls `window.stellar.finish(data)` which returns the user's response to the agent. The agent then processes the response with its own reasoning, and can call the tool again to continue the loop.

**Architecture:** The AI is always the brain — JavaScript is just a dumb UI layer for capturing input. All game logic, decision-making, and state evaluation happen inside the LLM's reasoning, not in client-side code.

**Key capabilities:**

| Use Case | How It Works |
|---|---|
| 🎮 **Interactive Games** | Play chess, tic-tac-toe, RPGs, and more — the AI renders the board, captures your move, thinks about its counter-move using its own neural network, and re-renders the updated state. No external engines needed. |
| 🎨 **Mock UI Gallery** | Before building a website, the agent generates 3-4 distinct visual mockups as interactive cards. You browse and pick your favorite. The agent proceeds with your chosen design — zero wasted iterations. |
| 📋 **Project Questionnaires** | Instead of guessing what you want, the agent renders a beautiful multi-step form asking targeted questions: "Auth provider?", "Color scheme?", "Layout style?". Your answers drive the entire build. |
| 🔍 **Preference Discovery** | When the agent needs API keys, config values, or style preferences, it renders a clean card-based picker instead of dumping a wall of text. |
| 📚 **Interactive Tutorials** | Step-by-step lessons where each step waits for you to complete an action before proceeding. |
| 🗳️ **MCQ & Polls** | Render beautiful multiple-choice questions to gather structured feedback or quiz the user. |

**Built-in UX safeguards:**
- **Visual feedback** — buttons disable and show "Thinking..." immediately on click, preventing spam.
- **Escape hatch** — every widget includes an "Exit" or "Cancel" button so you're never locked into an interaction.
- **Optional text input** — widgets can include a small text field so you can type instructions to the agent mid-interaction (e.g., "change the rules" or "I want to do something else").
- **DOM collision prevention** — each widget is scoped to avoid interfering with previous widgets in the chat feed.

---

### `report_process_issue(topic, issue_description, technical_context, status, timeout)`

**Autonomous self-healing feedback loop.** When the agent encounters a genuine technical failure during tool execution, it immediately logs a structured bug report to `agent_feedback` in SQLite and triggers `issue_resolver.py` as a background subprocess for developer review.

- Strict protocol: only for empirically verified internal failures — not for feature requests or user-reported issues that haven't been reproduced.

---

## Live Interrupts & Stream Control

Stellar supports two distinct modes of generation control, both designed for zero data leakage:

### Stop Button (Hard Cancel)

Clicking "Stop" triggers a cooperative cancellation via `threading.Event`:

1. The `/api/stop_generation` endpoint sets a Redis flag **and** signals the `ACTIVE_CHATS_CANCEL_EVENTS[chat_id]` event.
2. `gemini_generate` checks `cancel_event.is_set()` at every loop iteration — before each LLM call and before each tool execution.
3. On cancellation, **no partial response is saved to the database**. The generation thread exits cleanly and the stream closes.

### Live Follow-Up Injection (Soft Interrupt)

Users can send a follow-up message while the agent is still generating. The message is injected into the active LLM loop in real time:

1. **Frontend:** The chat input detects `isProcessing === true` and calls `POST /api/inject_message` instead of starting a new stream. The user's message is saved to the database immediately and rendered in the chat.
2. **Redis queue:** The message is pushed to `inject_messages:{chat_id}` and picked up by `gemini_generate` at the next checkpoint (after text output or between tool calls).
3. **Stream segmentation:** On detection, the current partial output is committed to the database as a **hidden** message (`hidden=1`) with its timestamp adjusted to sort before the user's follow-up. The LLM accumulation buffers are flushed, and a `stream_reset` event propagates through `refine_stream` to the frontend.
4. **Context preservation:** Hidden interrupted responses are excluded from the UI (`get_conversation_history(for_ui=True)` filters `hidden = 0`) but included in the LLM's context window so the model knows what it previously generated.
5. **Frontend handling:** The `stream_reset` SSE event clears the current placeholder, allowing the model's new response (addressing the follow-up) to render cleanly as a fresh message.

---

## PWA & Push Notifications

Stellar is installable as a Progressive Web App on all platforms (desktop, Android, iOS).

### Installation

- **Automatic prompt:** A native `beforeinstallprompt` event is intercepted. Stellar defers the prompt and shows it once after login, then respects the user's choice and never re-prompts.
- **Manual install:** An "Install Stellar App" button is available in the user profile modal.
- **Standalone mode:** The `manifest.json` is configured with `display: standalone` and `scope: /` so the PWA launches as a full-screen app without browser chrome.

### Web Push Notifications

Background push notifications are delivered via the Web Push protocol (VAPID):

1. **Service Worker** (`static/service-worker.js`) — registers on first load, handles `push` events, and displays native OS notifications even when the tab is closed.
2. **Subscription flow:** On notification opt-in, the frontend requests a `PushSubscription` from the browser and sends it to `POST /api/push/subscribe`, which securely stores it in Redis (eliminating duplicate local notifications).
3. **Server-side dispatch:** `send_push_notification(user_id, title, body, url)` in `app.py` uses `pywebpush` with VAPID credentials (`vapid_private.pem`) to push notifications to all of a user's registered devices.
4. **Triggers:** Notifications are dispatched when a long-running generation completes (if the user has been waiting >20 seconds), and on scheduled task completion.

---

## Talent System

Operational guidelines (formerly "mandates") are stored in the `talents` database table rather than the filesystem. Each talent defines technical standards, preferred libraries, code structure requirements, and quality gates that the agent follows for specialized tasks.

| Talent | Trigger Condition |
|---|---|
| Frontend Design | Before building any web UI, component, or dashboard |
| Generative AI | Before writing any Gemini/GenAI integration code |
| Game Development | Before building 3D rendering engines or game mechanics |
| Mobile Development | Before building Android APKs or React Native apps |
| Red Team | Before any security research, pen-testing, or vulnerability analysis |

Talents are injected into the lab sandbox at `/lab/` before the agent begins specialized work. They can be managed via the admin interface.

---

## Persistent Memory & Scheduling

### How Memory Works

At the start of every agent turn, the system prompt is dynamically constructed by `get_refinement_prompt()` in `prompts.py`. This function:

1. Queries `user_logs_prefs` in SQLite for all entries belonging to the current user.
2. Injects them as a `### PERSISTENT MEMORY & USER PREFERENCES` block directly into the prompt.
3. The agent reads this block before responding and adheres to any stored preferences.

Memory entries survive server restarts, model changes, and new chat sessions. They are the agent's long-term context layer.

### How Scheduling Works

A background daemon thread in `app.py` polls `scheduled_tasks` in SQLite every minute. For any task whose `execute_at` has passed:

1. A Flask application context is pushed.
2. `g` is populated with the task owner's `user_id`, `chat_id`, and `model_id`.
3. The full agent pipeline is invoked — the same pipeline as a real user request — and the output is appended to the user's chat history.
4. Recurring tasks update their `execute_at` to `now + recurring_minutes`.

This enables fully autonomous operation: the agent can schedule itself to monitor news, retry failed extractions, send reports, or perform maintenance — all without any user interaction.

---

## Use Cases & Examples

### 1. Autonomous Full-Stack Application Development

Deploy a complete React + Python backend application with one conversation:

> *"Build a real-time stock dashboard with a React frontend and a Flask WebSocket backend. Deploy it live."*

Stellar will:
1. `repo_control(action='deploy')` — provision a fresh container.
2. `repo_control(action='execute')` — scaffold the project, install `npm` and `pip` dependencies (separate calls to avoid OOM).
3. `repo_control(action='execute')` — start the server on `0.0.0.0:5000`.
4. Verify the deployment URL is responding and return the live link.

---

### 2. Data Analysis & Report Generation

> *"Analyze the attached sales CSV, generate key visualizations, and email me the PDF report."*

Stellar will:
1. `lab_execute` — install pandas, matplotlib, weasyprint; run the analysis script.
2. `lab_execute` — generate charts and compile an HTML dashboard.
3. `lab_execute` — convert HTML to PDF using `weasyprint`.
4. `manage_files(action='project')` — export the PDF to the host.
5. `send_self_email` — attach and send the PDF report to the user.

---

### 3. Deep Security Analysis (Red Team Mode)

> *"Audit this open-source API for authentication vulnerabilities."*

Under the Red Team mandate (persona: **Angel**), Stellar will:
1. `lab_execute` — clone the target repository, install `sqlmap`, `semgrep`, or custom scanners.
2. `lab_execute` — run static analysis, enumerate endpoints, attempt injection payloads.
3. `logs_and_preferences` — record the methodology and any verified findings.
4. Produce a structured vulnerability report.

---

### 4. AI Presentation on Demand

> *"Create a 12-slide corporate pitch deck on quantum computing for a non-technical audience."*

Stellar will:
1. `web_search` — gather recent research, statistics, and key concepts.
2. `make_presentation` — plan slides with structured JSON, generate 12 AI-designed full-bleed slide images concurrently, assemble the `.pptx`.
3. Return a download link and interactive slide preview carousel.
4. If a slide needs revision: `regenerate_presentation_slide` — re-generate just that slide using the original as a reference.

---

### 5. Autonomous Scheduled Intelligence

> *"Every Monday at 9 AM, search for the top 5 AI news stories and email me a summary."*

Stellar will:
1. `schedule_task(action='schedule', recurring_minutes=10080)` — schedule a weekly task.
2. When triggered: `web_search(action='tavily_search', topic='news')` — gather stories.
3. `send_self_email` — format and deliver the digest automatically.

---

### 6. YouTube Deep Dive

> *"Find the most-watched tutorial on LangGraph and summarize how it handles state management."*

Stellar will:
1. `analyze_youtube_video(action='search')` — query YouTube API, return top videos by view count.
2. `analyze_youtube_video(action='analyze', video_url=...)` — feed the video to Gemini multimodal, extract the specific segment on state management, return a timestamped summary.

---

### 7. Interactive Project Planning with Live UI

> *"Build me a personal portfolio website."*

Instead of guessing, Stellar will:
1. `request_user_interaction` — render a beautiful multi-step questionnaire asking about color scheme, layout preference, sections to include, and tech stack.
2. `request_user_interaction` — generate 3-4 visual mock UI cards and let you pick your favorite design direction.
3. Use your collected preferences to build exactly what you want — no wasted iterations.
4. `repo_control(action='deploy')` — deploy the final result live.

---

### 8. Play Games Against the AI

> *"Play chess with me"*

Stellar will:
1. `request_user_interaction` — render a stunning interactive chessboard with SVG pieces, move highlighting, and click-to-move controls.
2. Capture your move via the UI, then use its own neural network reasoning to decide its counter-move.
3. `request_user_interaction` — re-render the board with both moves applied. Repeat until checkmate, draw, or you click "Exit".
4. No external engines (Stockfish, python-chess) — you're playing against the AI's actual brain.

---

## API Key Management & Account Rotation

Stellar is designed for high availability across multiple Gemini API accounts.

### Key Hierarchy

- `PRIMARY_API_KEY` — used first for all requests.
- `BACKUP_API_KEYS` — a list of additional keys tried in order on `429`, `403`, `503`, or `500` errors.

### `GlobalKeyManager`

A thread-safe singleton (`KEY_MANAGER`) tracks rate-limit blocks per key, per model:

- **Model-scoped blocking:** When a key hits a quota error on a specific model, it is blocked only for that model. Other models can still use the same key.
- **Global blocking:** `403` / `permission_denied` errors block the key across all models.
- **Auto-expiry:** Blocks expire after a parsed duration (extracted from API error messages) or a default of 60 seconds.
- **Pacific midnight reset:** A background thread calls `KEY_MANAGER.blocked_until.clear()` at midnight Pacific Time daily, coinciding with Google's quota reset cycle.

```python
# The manager is checked before every API call
is_blocked, reason = KEY_MANAGER.is_key_blocked(current_key, model_id)
if is_blocked:
    # Skip to the next key in the rotation
    continue
```

All tools in `agent_tools.py` implement the same rotation pattern, and `gemini_generate` handles mid-conversation key switches by reconstructing the chat history with the new key's client.

### Credential Store for Subagents
### Manual Account Switching

To switch the active CLI account on the host machine:
```bash
cp credentials/account_X/google_accounts.json ~/.gemini/google_accounts.json
cp credentials/account_X/oauth_creds.json ~/.gemini/oauth_creds.json
pkill -f gemini
```

---

## Security Model

| Layer | Mechanism |
|---|---|
| **Authentication** | Google OAuth via Firebase ID token verification (`/login/google`) |
| **Authorization** | `@require_approval` decorator on all protected routes; user status checked against SQLite `users` table |
| **Container isolation** | Per-user Docker networks with ICC disabled; lab/repo containers cannot communicate with each other |
| **File system access** | `manage_files` restricts host-side moves to `UPLOAD_FOLDER` and `outputs/` only |
| **Email** | `send_self_email` sends only to the authenticated user's registered email — not arbitrary addresses |
| **Session security** | Flask-Session with signed cookies (`FLASK_SECRET_KEY`); session cookie named `stellar_session_main` |
| **Encryption** | Fernet symmetric encryption for sensitive stored data |
| **Upload validation** | File extension allowlist enforced on all uploads |
| **Nginx TLS** | Let's Encrypt certificates with HSTS and modern SSL configuration |

---

## Database Schema

The application uses a single SQLite file (`stellar_local.db`) in WAL journal mode. Key tables:

| Table | Purpose |
|---|---|
| `users` | User accounts: `id`, `username` (email), `is_approved`, `display_name`, `password_hash` |
| `chats` | Chat sessions per user. Includes `is_temp` flag for ephemeral sessions. |
| `messages` | All chat messages: `message_type` (user/stellar), `message_content`, `hidden` (boolean), `visualization_html`, `attached_files` (JSON). Hidden messages are excluded from the UI but included in LLM context. |
| `tool_calls` | Full tool input/output for paginated retrieval by `read_tool_output`. Includes `hidden` flag for memory compression. |
| `repo_history` | Deployment history: `process_id`, `project_name`, `subdomain`, `files_snapshot` (JSON), `status` |
| `scheduled_tasks` | Autonomous task queue: `task_prompt`, `execute_at`, `recurring_minutes`, `metadata`, `is_active` |
| `user_logs_prefs` | Persistent agent memory: `user_id`, `log_entry`, `created_at` |
| `agent_feedback` | Bug reports filed by `report_process_issue`: `topic`, `issue_description`, `technical_context` |
| `push_subscriptions` | Web Push subscription endpoints per user/device for background notifications |
| `talents` | Operational guidelines (formerly mandates): `name`, `content`, `user_id`, `chat_id` |

WAL mode and `busy_timeout=5000` are set on all connections to handle concurrent access from multiple Gunicorn threads.

### Hidden Message Semantics

The `hidden` column on `messages` and `tool_calls` serves dual purposes:

1. **Memory compression:** `compress_memory` sets `hidden=1` on older records to reduce context window usage. Compressed state documents are preserved as hidden messages prefixed with `[COMPRESSED MEMORY STATE]`.
2. **Interrupted responses:** When a live follow-up interrupts an active generation, the partial response is saved as `hidden=1` so it remains in the LLM's context but never appears in the user's chat history.

---

## Testing

The `tests/` directory contains a `pytest` suite using `pytest-flask` and `pytest-mock`.

```bash
# Activate venv first
source venv/bin/activate

# Run all tests
pytest tests/ -v

# Run with coverage
pytest tests/ --cov=app --cov=agent_tools --cov-report=term-missing
```

Tests mock Docker and external API calls to run without live infrastructure.

---

## Project Structure

```
my_app/
├── app.py                  # Main Flask application, routes, scheduling daemon, GlobalKeyManager
├── agent_tools.py          # All agent tool implementations (15 tools, ~2300 lines)
├── prompts.py              # System prompt construction, persona definitions
├── dockersetup.py          # Docker image build and network initialization script
├── webscrapper.py          # Lightweight web scraping utility
├── send_email.py           # Standalone email utility
├── telegram_bot.py         # Telegram notification bot for login alerts
├── issue_resolver.py       # Background subprocess for processing agent_feedback
├── requirements.txt        # Pinned Python dependencies
├── keys.env                # ⚠️ Secret keys and configuration (never commit this)
├── encryption.key          # ⚠️ Fernet key file (never commit this)
├── vapid_private.pem       # ⚠️ VAPID private key for Web Push (never commit this)
├── stellar_local.db        # SQLite database (WAL mode)
├── deploy/
│   ├── gunicorn_stellar.service   # systemd unit file
│   └── nginx_stellar.conf         # Nginx reverse proxy config
├── credentials/            # Gemini CLI OAuth credentials per account
│   └── account_X/
├── static/
│   ├── main.css            # Core stylesheet
│   ├── main.js             # Core frontend logic (SSE, chat, interrupts)
│   ├── manifest.json       # PWA manifest
│   └── service-worker.js   # Push notification and offline caching
├── templates/              # Jinja2 HTML templates
├── uploads/                # User-uploaded files (per chat session)
├── outputs/                # Generated files (images, PDFs, presentations)
├── sandbox_runs/           # Lab container workspace directories (host-side)
└── tests/                  # pytest test suite
```

---

*Built with Flask · Powered by Gemini · Deployed on stellarai.site*
