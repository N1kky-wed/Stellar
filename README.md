# Stellar: The Persistent Agentic Ecosystem

Stellar is a **Stateful Agentic Operating System** designed to transition AI from conversational interaction to **Autonomous Environment Agency**. It is architected for developers, security researchers, and systems engineers who require an AI that does not just "write code," but **provisions, mirrors, audits, and orchestrates** persistent, multi-container infrastructure.

Stellar operates as a self-correcting state machine with absolute authority over its underlying Docker compute clusters, unified by a production-grade networking fabric and a non-linear memory engine.

---

## 🎭 Persona & Privilege Hierarchy

Administrative privileges and architectural depth scale dynamically. High-level personas are granted root-level access to the internal sandbox, networking, and container orchestration layers.

| Persona | Model | Capability | Tactical Domain |
| :--- | :--- | :--- | :--- |
| **Obsidian** | Gemini 3.1 Pro Preview | **Total Infrastructure Authority** | Kernel Architecture, Cluster Orchestration, Red Teaming |
| **Crimson** | Gemini 3.0 Flash Preview | **High-Velocity Agency** | Rapid Scaffolding, Recursive OSINT, Multi-Repo Deployment |
| **Lunarity** | Gemini 3.1 Flash Lite | **Standard Agency** | Technical Synthesis, Vector Visualization, Presentation Design |
| **Emerald** | Gemini 2.5 Flash Lite | **Utility** | Instant Ingress Resolution, Metadata Grounding |

---

## 🧠 The Architectural Memory Engine

Stellar eliminates the "AI Amnesia" bottleneck through a proprietary dual-layered memory architecture designed for long-term project persistence.

### 1. The Ground-Truth Ledger
Every atomic action—every shell command, script execution, and file modification—is etched into a persistent SQL ledger. This allows Stellar to maintain a 100% accurate state of its environment across hundreds of turns, preventing the "state-drift" common in standard LLM sessions.

### 2. Paged Information Retrieval (Windowing)
To process industrial-scale telemetry (e.g., 10,000-line JSON responses or 50MB log files), Stellar utilizes a **Paged Memory Engine**:
*   **Metadata Compression:** Large outputs are automatically truncated into high-density summaries.
*   **Autonomous Slicing:** The agent can independently decide to "scroll" through its history using `read_tool_output`, retrieving specific line ranges of past logs to find exact error signatures or data points without saturating the context window.

### 3. Proactive Logs & Preferences
Stellar autonomously builds a long-term profile of user preferences, verified technical fixes, and project-specific configurations via the `logs_and_preferences` tool. This memory is automatically injected into the context at the start of every turn, ensuring that once a resolution strategy is verified (e.g., a specific database port or API key), it is never forgotten.

---

## 🔍 Unified Grounding & Intelligence

Stellar integrates a multi-depth search and analysis fabric to ensure all actions are grounded in empirical data.

*   **Unified Web Search:**
    *   **Quick Search:** Real-time Google Search integration for rapid factual lookups.
    *   **Extensive Research:** Deep, multi-domain research via Tavily Advanced, supporting exhaustive technical synthesis and news tracking with 3-day history lookback.
*   **YouTube Intelligence:** Performs frame-by-frame visual analysis and content search via `analyze_youtube_video`. Stellar can sample frames at specific FPS, interrogate video content for exploit logic, or extract data from technical tutorials to build functional replicas.

---

## 🌐 The Subdomain Fabric & Wildcard Routing

Stellar Project Hosting uses production-grade **Subdomain Fabric** and wildcard DNS routing. 

*   **Native Domain Routing:** Every project—whether built from scratch or deployed from a GitHub repository—is assigned a unique, permanent subdomain (e.g., `https://[project-slug].stellarai.live/`).
*   **Kernel-Level Interception:** A native Flask interceptor captures all incoming subdomain traffic and routes it to the correct internal container port via a high-speed Redis state-store.
*   **Zero-Config Compatibility:** Absolute paths, React Routers, and API redirects work natively because each application believes it is running at the absolute root of its own domain.

---

## 🧪 Autonomous Container Orchestration

Stellar maintains direct, stateful control over the **Docker Engine** with per-tenant network isolation.

### 1. The Forge (AI-Driven CI/CD)
The Forge allows for rapid, prompt-to-deployment application scaffolding for Python/HTML/CSS stacks. If a build fails, Stellar enters a self-healing loop to analyze logs and patch the source code autonomously via `modify` actions.

### 2. Repo Host (Deterministic Persistence)
Deploy complex, multi-file projects (Node.js, Go, Rust, React, etc.) with professional organization.
*   **Deterministic Persistence:** Stellar uses **SQLite WAL mode** for high-concurrency safety.
*   **Auto-Snapshotting:** Before stopping or restarting, Stellar automatically snapshots manual code edits from the container back to the database, ensuring zero data loss during restarts.

### 3. Isolated Lab Sandbox
The `lab_execute` tool provides a persistent root bash Docker sandbox. Each chat session is assigned an isolated bridge network, preventing cross-container contamination while allowing for systematic data analysis and hacking workflows.

---

## 🛠️ The Autonomous Service Suite

### 1. Task Scheduler (Adaptive Polling)
The `schedule_task` tool is a persistent automation engine that supports one-time or recurring triggers.
*   **Adaptive Retry Protocol:** By utilizing the `metadata` scratchpad, Stellar can perform autonomous polling loops. If a data source (e.g., a report or file) is delayed, the agent logs the retry state in `metadata` and reschedules itself until the task is successfully fulfilled.

### 2. Secure Closed-Loop Mailer
The `send_self_email` tool provides a secure way to export reports and assets.
*   **Markdown Rendering:** Emails are automatically converted from Markdown to high-fidelity HTML.
*   **Attachment Handling:** Securely attach files from `/outputs`, `/uploads`, or container workspaces to your registered email address.

### 3. Visual & Media Engine
*   **Presentation Engine:** Generates fully designed PPTX decks where each slide is a high-resolution infographic. Supports feedback-driven regeneration of specific slides.
*   **Imagen 4K:** Generate high-resolution images with **Reference Image Conditioning** (up to 14 images) for style-consistent UI design or branding.
*   **Managed File System:** Securely move files across environments (Chat ↔ Lab ↔ Repo/Forge). Use `action='project'` to export generated assets for user download.

---

## 🛡️ Security & Red Teaming

Stellar is built for adversarial environments with a specialized **Red Team Mandate**.

*   **Impact-Driven Offensive Protocol (IDOP):** Operating in the Lab, findings are forbidden from being "reports only." Stellar must follow a **Discovery → Validation → Exploitation → Impact** lifecycle (e.g., finding a credential → logging in → proving data access).
*   **Anti-Injection Shield:** Hardened prompts and deterministic validation logic to prevent unauthorized access and protect the agentic lifecycle.

---

## 📓 High-Impact Use Cases & Combinations

### I. The "Market Sentinel" (Search + Scheduler + Mail)
**Goal:** Monitor a competitor's pricing and deliver daily visual reports.
*   **Workflow:** Stellar uses **Extensive Research** to identify target data. It **Schedules a Task** to scrape the site daily. If a change is detected, it generates a **Comparison Infographic**, attaches it to a **Markdown Report**, and **Emails** it to the user.

### II. The "Adversarial Mirror" (YouTube + Lab + Red Team Mandate)
**Goal:** Recreate and audit a technical vulnerability from a video tutorial.
*   **Workflow:** Stellar **Analyzes a YouTube Video** to extract exploit logic. It provisions an **Isolated Lab Sandbox** to replicate the vulnerable stack and uses **IDOP** to verify the exploit's impact before generating a remediation report.

### III. The "Full-Stack Architect" (Imagen + Forge + Subdomain Fabric)
**Goal:** Build a production-ready application from a visual concept.
*   **Workflow:** Stellar uses **Imagen** to generate a high-fidelity UI mockup. It then **analyzes the mockup**, writes the matching code in **The Forge**, and hosts the result instantly on a **Custom Subdomain**.

### IV. The "DevOps Savior" (Repo Host + Self-Healing + Snapshotting)
**Goal:** Rescue and deploy a legacy repository with broken dependencies.
*   **Workflow:** Stellar clones the repo via **Repo Host**. It uses **Self-Healing Loops** to identify and fix build errors. Once running, manual edits are made to the frontend, which are captured via **Auto-Snapshotting**, ensuring the rescue is persistent across restarts.

---

*Stellar: Build the future, one autonomous turn at a time.*
