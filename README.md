# Stellar: The Persistent Agentic Ecosystem

Stellar is an elite, web-based AI platform that transitions from standard "chat" to **autonomous agency**. It is designed for developers, researchers, and engineers who require an AI that doesn't just suggest code, but **executes, builds, and remembers**.

Stellar is powered by an **Architectural Memory Engine** and a suite of high-privilege tools that allow it to operate as a self-correcting state machine.

---

## 🎭 Persona-Based Capability Mapping

Stellar's intelligence and tool access scale based on the selected model level:

| Persona | Model | Capability Level | Key Feature Access |
| :--- | :--- | :--- | :--- |
| **Obsidian** | Gemini 3.1 Pro Preview | Ultra-High Depth | Lab Sandbox, Full Forge, Complex Logic |
| **Crimson** | Gemini 3.0 Flash Preview | High Speed / Logic | Lab Sandbox, Recursive Recon, Rapid Forge |
| **Lunarity** | Gemini 2.5 Flash | Standard | Web Search, SVG Visualization, Slides |
| **Emerald** | Gemini 2.5 Flash Lite | Utility | Fast Chat, Basic Web Search |

---

## 🧠 The Memory Engine: `tool_calls`

Stellar solves the "AI amnesia" problem using a dedicated SQL-backed memory system.

*   **Ground-Truth Context:** Every command run in the Lab and every file written in the Forge is stored in the `tool_calls` table.
*   **Metadata Injection:** Before every response, the system queries this table and provides the AI with a hidden, un-truncated log of its previous inputs and outputs.
*   **Contextual Chaining:** Stellar knows *exactly* what it did in previous turns. Turn 5 can be a direct continuation of Turn 1's logic without needing to re-run time-consuming scripts.

---

## 🧪 High-Privilege Agentic Tools & Workflows

Stellar's autonomy is powered by these specialized tools:

### 1. The Lab Sandbox (`lab_execute`)
A persistent, root-access Docker container (`stellar-lab-core`) that acts as Stellar's hands.
*   **Stateful Persistence:** Install a library (e.g., `pip install requests`) in Turn 1, and use it in a script in Turn 10. The environment stays active.
*   **Network Operations:** Clone GitHub repositories, ping external APIs, scrape websites, or verify vulnerabilities natively.

### 2. Unified Forge Mode (`forge_control`)
Scaffolds and manages full-stack Flask/HTML/JS applications.
*   **Autonomous Deployments:** Build a complete app, run it, and deploy it to a live URL (e.g., `https://stellarai.live/apps/[ID]/`).
*   **Fuzzy Title Resolution:** Address projects by natural names: "Restart my **Task Manager app**."
*   **AI-Driven Code Iteration:** Tell Stellar what's wrong ("The sidebar is broken"), and it retrieves the code snapshot, patches it, and redeploys autonomously.
*   **Automatic Wake-up:** Modifying a stopped project pulls it from history and restarts the container instantly.

### 3. Visual & Presentation Engine
*   **Proactive SVGs (`render_svg`):** For technical "How it works" queries, Stellar autonomously generates interactive, transparent-background SVG diagrams that render inline.
*   **Presentation Carousel (`make_presentation`):** Generates `.pptx` files with an in-chat interactive preview carousel. You can ask Stellar to **Regenerate** a specific slide and it will rewrite only that slide.

### 4. Advanced Reconnaissance
*   **Spectral Search (Tavily API):** Multi-domain deep scraping.
*   **Native Search:** Ultra-fast lookup via Google Search.

---

## 📓 Advanced Guide: Agentic Use Cases

Below are detailed, real-world examples of how Stellar combines its tools and memory to solve complex workflows autonomously.

### Use Case A: Automated Vulnerability & API Reconnaissance
**Goal:** Extract API keys from a highly minified, production Single Page Application (SPA).

1.  **User Prompt:** *"Get me the Firebase keys from cmrhackfest.in using the Lab."*
2.  **Turn 1 (Recon & Discovery):** Stellar uses `lab_execute` to run `curl` on the target index page to identify the main `.js` bundle names.
3.  **Turn 2 (Targeted Scripting):** Stellar writes a custom Python script using `BeautifulSoup` inside the Lab, executes it, and isolates the specific asset (e.g., `assets/index-8rjvXeWC.js`).
4.  **Turn 3 (Regex Extraction):** Knowing the exact file, Stellar runs a highly specific `grep` and regex pipeline to pull just the `apiKey`, `appId`, and `projectId`.
5.  **Turn 4 (Verification & Output):** Stellar verifies server headers, synthesizes the results, and presents the plain-text keys to the user.
*   *Agentic Feature Highlight:* **State Machine Chaining.** The output of Turn 1 dictated the input of Turn 2. The persistent memory allowed Stellar to drill down without user intervention.

### Use Case B: Full-Stack Prototyping and Self-Correction
**Goal:** Build a functional Weather App, discover a bug, and fix it using natural language.

1.  **User Prompt:** *"Start a new Forge project for a Weather App that fetches data from an API."*
2.  **Turn 1 (Creation):** Stellar uses `forge_control(action='create')`. It plans the Python/Flask backend and HTML/JS frontend, writes the code, and spins up the Docker container. It returns the live URL.
3.  **User Prompt:** *"The API call is failing with a CORS error when I test the live URL."*
4.  **Turn 2 (Iteration):** Stellar uses `forge_control(action='modify')` with a prompt. It retrieves the project files from `forge_history`, analyzes the code, writes a patch to add Flask-CORS to the backend, and silently redeploys.
5.  **User Prompt:** *"Actually, change the background to dark mode."*
6.  **Turn 3 (Iteration):** Stellar knows exactly which project we are discussing (Contextual Continuity). It fetches the *latest* snapshot from Turn 2, modifies the CSS, and redeploys.
*   *Agentic Feature Highlight:* **The "Red Line" Rule and Contextual Continuity.** Stellar seamlessly transitions from building to patching, maintaining exact file states across turns without breaking context.

### Use Case C: Data Ingestion to Visual Presentation
**Goal:** Process a raw dataset and turn it into a professional presentation deck.

1.  **User Prompt:** *"Here is a CSV of customer sales data. Process it in the Lab and tell me the top 3 trends."*
2.  **Turn 1 (Lab Execution):** Stellar uses `lab_execute` to run a Python script using `pandas` (installing it if necessary), reads the CSV, calculates the trends, and prints the summary.
3.  **User Prompt:** *"Great. Now turn those trends into a 5-slide corporate presentation."*
4.  **Turn 2 (Presentation Generation):** Using the context gained in Turn 1, Stellar invokes `make_presentation`, defining the slide contents based on the pandas output. The slide carousel renders in the chat.
5.  **User Prompt:** *"Regenerate slide 3, it needs more focus on Q4 growth."*
6.  **Turn 3 (Granular Visual Update):** Stellar invokes `regenerate_presentation_slide` specifically for slide 3, updating the image and rebuilding the PPTX file without altering the other slides.
*   *Agentic Feature Highlight:* **Cross-Domain Tool Chaining.** Moving seamlessly from data processing (Lab Sandbox) to visual artifact generation (Presentation Engine), preserving data integrity throughout.

---

*Stellar: Build the future, one autonomous turn at a time.*
