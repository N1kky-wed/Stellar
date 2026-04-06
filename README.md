# Stellar: The Persistent Agentic Ecosystem

Stellar is an elite, web-based AI platform that transitions from standard "chat" to **autonomous agency**. It is designed for developers, researchers, and engineers who require an AI that doesn't just suggest code, but **provisions, configures, and hosts** full-scale production environments.

Stellar is powered by an **Architectural Memory Engine** and a suite of high-privilege tools that allow it to operate as a self-correcting state machine with absolute control over its deployment containers.

---

## 🎭 Persona-Based Capability Mapping

Stellar's intelligence and tool access scale based on the selected model level:

| Persona | Model | Capability Level | Key Feature Access |
| :--- | :--- | :--- | :--- |
| **Obsidian** | Gemini 3.1 Pro Preview | Ultra-High Depth | Lab Sandbox, Full Forge, Repo Hosting, Absolute Control |
| **Crimson** | Gemini 3.0 Flash Preview | High Speed / Logic | Lab Sandbox, Recursive Recon, Rapid Forge, Repo Hosting |
| **Lunarity** | Gemini 2.5 Flash | Standard | Web Search, SVG Visualization, Slides |
| **Emerald** | Gemini 2.5 Flash Lite | Utility | Fast Chat, Basic Web Search |

---

## 🧠 The Memory Engine: `tool_calls`

Stellar solves the "AI amnesia" problem using a dedicated SQL-backed memory system.

*   **Ground-Truth Context:** Every command run in the Lab and every manual file edit in a deployed project is stored in the `tool_calls` table.
*   **Metadata Injection:** Before every response, the system queries this table and provides the AI with a hidden, un-truncated log of its previous inputs and outputs.
*   **Contextual Chaining:** Stellar knows *exactly* what it did in previous turns. Turn 5 can be a direct continuation of Turn 1's logic without needing to re-run time-consuming scripts.

---

## 🌐 The Unique Subdomain Ecosystem

Stellar has moved beyond path-based proxying. Every project—whether built from scratch in the Forge or deployed from a GitHub repository—is assigned a **Unique Subdomain**.

*   **Native Routing:** Apps are hosted at `https://[project-slug].stellarai.live/`. 
*   **Production-Grade Traffic Interception:** A native Flask `@before_request` interceptor catches all incoming subdomain traffic and invisibly routes it to the correct internal container port.
*   **Zero-Config Compatibility:** Absolute paths, React Routers, and API endpoints work out-of-the-box because every app thinks it is running at the root of its own domain.

---

## 🧪 High-Privilege Agentic Tools & Workflows

Stellar's autonomy is powered by these specialized tools:

### 1. The Lab Sandbox (`lab_execute`)
A persistent, root-access Docker container (`stellar-lab-core`) that acts as Stellar's hands.
*   **Stateful Persistence:** Install a library (e.g., `pip install requests`) in Turn 1, and use it in a script in Turn 10.
*   **Network Operations:** Clone GitHub repositories, ping external APIs, or verify vulnerabilities natively.

### 2. Interactive Repo Hosting (`host_repo` + `repo_execute`)
Stellar can now deploy any external GitHub repository with absolute control.
*   **Provisioning (`host_repo`):** Clones the repo and provisions a dedicated, port-mapped container.
*   **Absolute Control (`repo_execute`):** Stellar has **Full Root Bash Access** to the container. It can:
    *   Install databases (e.g., `apt-get install postgresql`).
    *   Update runtimes (e.g., upgrading to Node 22).
    *   Rewrite code on the fly to fix hardcoded routes or add features.
    *   Manage background processes (`nohup npm start > app.log 2>&1 &`).

### 3. Unified Forge Mode (`forge_control`)
Scaffolds and manages full-stack Flask/HTML/JS applications from scratch.
*   **AI-Driven Code Iteration:** Tell Stellar what's wrong, and it retrieves the snapshot, patches it, and redeploys autonomously.
*   **Automatic Wake-up:** Modifying a stopped project pulls it from history and restarts the container instantly.

### 4. Visual & Presentation Engine
*   **Proactive SVGs (`render_svg`):** Generates interactive, transparent-background SVG diagrams that render inline.
*   **Presentation Carousel (`make_presentation`):** Generates `.pptx` files with an in-chat interactive preview carousel.

---

## 📓 Advanced Guide: Agentic Use Cases

### Use Case A: Complex Repository Deployment & Self-Healing
**Goal:** Deploy a modern React/Express repository that requires a specific Node version and a PostgreSQL database.

1.  **User Prompt:** *"Deploy https://github.com/user/my-fullstack-repo and fix any errors."*
2.  **Turn 1 (Provisioning):** Stellar uses `host_repo`. It provisions the container and clones the code.
3.  **Turn 2 (Environment Setup):** Using `repo_execute`, Stellar reads `package.json`, sees it needs Node 22, and autonomously installs it.
4.  **Turn 3 (Infrastructure):** Stellar runs `apt-get install postgresql`, initializes a database, and generates a `.env` file with the correct `DATABASE_URL`.
5.  **Turn 4 (Build & Start):** Stellar runs `npm install && npm run build`, then starts the server in the background.
6.  **Turn 5 (Final Polish):** Stellar hits the live subdomain, sees a 404 on the logo because of a hardcoded path, uses `sed` to fix the source code, rebuilds, and confirms the site is 100% functional.
*   *Agentic Feature Highlight:* **Absolute Control.** Stellar didn't just run a script; it acted as a DevOps engineer to build the environment the app required.

### Use Case B: Automated Vulnerability & API Reconnaissance
**Goal:** Extract API keys from a highly minified, production Single Page Application (SPA).

1.  **User Prompt:** *"Get me the Firebase keys from target-site.com using the Lab."*
2.  **Turn 1 (Recon):** Stellar uses `lab_execute` to run `curl` and identify `.js` bundle names.
3.  **Turn 2 (Targeted Scripting):** Stellar writes a custom Python script inside the Lab to isolate and download the specific asset.
4.  **Turn 3 (Extraction):** Stellar runs a highly specific `grep` and regex pipeline to pull the `apiKey` and `projectId`.
5.  **Turn 4 (Output):** Stellar synthesizes the results and presents the plain-text keys to the user.

### Use Case C: Full-Stack Prototyping and Self-Correction
**Goal:** Build a functional Weather App, discover a bug, and fix it using natural language.

1.  **User Prompt:** *"Start a new Forge project for a Weather App."*
2.  **Turn 1 (Creation):** Stellar uses `forge_control(action='create')`. It plans the app, writes the code, and spins up the container at `weather-app.stellarai.live`.
3.  **User Prompt:** *"The API call is failing with a CORS error."*
4.  **Turn 2 (Iteration):** Stellar uses `forge_control(action='modify')`, retrieves the files, patches the backend to add `flask-cors`, and silently redeploys.

---

*Stellar: Build the future, one autonomous turn at a time.*
