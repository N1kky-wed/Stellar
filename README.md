# Stellar AI Operating System

A persistent, stateful AI operating system for total autonomous agency across infrastructure, research, and design. Orchestrate Docker clusters, clone Git repos, and perform deep research with YouTube intelligence.

## Setup and Compilation

To set up and run Stellar, follow these steps:

1. **Clone the repository:**
   ```bash
   git clone <repository_url>
   cd my_app
   ```

2. **Set up a Virtual Environment:**
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   ```

3. **Install Dependencies:**
   Ensure you have all the required Python packages installed.
   ```bash
   pip install -r requirements.txt
   ```

4. **Environment Variables:**
   Set up your API keys and configuration in `keys.env`.

5. **Docker Infrastructure Setup:**
   Run the docker setup script to build the necessary containers.
   ```bash
   python3 dockersetup.py
   ```

6. **Run the Application:**
   Start the application locally.
   ```bash
   python3 app.py
   ```
   **Note for backend changes:** When making changes to backend files (e.g., `app.py`, `agent_tools.py`, `prompts.py`), always restart the service to apply changes:
   ```bash
   sudo systemctl restart stellar
   ```

## Feature Analysis

Stellar comes packed with powerful features out-of-the-box designed for complete autonomous development:

*   **Stateful Memory & Persistence:** By maintaining SQLite databases (`stellar_local.db`), it ensures any work completed, conversations had, and context built up is never lost between sessions.
*   **Docker Orchestration Framework:** Spin up hardened environments for running untrusted code securely using the internal Docker orchestration engine (`dockersetup.py` and `app.py`).
*   **Subagent Delegation:** A robust multi-agent framework where the main AI can spawn, monitor, and coordinate sub-agents specialized in different development mandates (frontend, backend, generative AI, etc.).
*   **Automated Workspace Management:** Manages files across multiple environments seamlessly (Chat, Lab, Repo), parsing logs, testing code, and generating structured reports automatically.
*   **Multi-Modal Intelligence Tools:** Native integration to pull youtube transcripts, perform web scraping (`webscrapper.py`), and analyze external intelligence directly into its reasoning loop.

## Use Cases & Detailed Examples

### 1. Autonomous Web Application Development
**Scenario:** Building a full-stack React application with a Node.js or Python backend.
**Example:** Provide Stellar with the project requirements. Stellar will automatically scaffold the frontend and backend architectures within its `Repo Control` environment. It can iteratively build components, run tests in an isolated Docker container, inspect network failures or container crash logs, patch the code, and present the finished, deployed prototype. 

### 2. Deep Security Analysis (Red Teaming)
**Scenario:** Identifying vulnerabilities in an open-source library or application codebase.
**Example:** Using the "Red Team Mandate," Stellar can be directed to pull down a target library. Using its Lab sandbox, it will run automated linting, AST analysis, and custom vulnerability scanners. It documents its findings, attempts to build proof-of-concept exploits in safe, containerized environments, and writes up comprehensive vulnerability reports.

### 3. Generative Media and Assets
**Scenario:** Generating professional assets for a new application or game project.
**Example:** Driven by the "Game Development" and "Generative AI" mandates, Stellar can write complex narrative structures, then automatically generate visual assets like textures, character sprites, and UI elements. It manages the files in the project folder and integrates them directly into the codebase.

## System Guidelines

### Infrastructure
*   **Docker Orchestration:** Every workspace is a hardened container with isolated networking.
*   **Persistence:** Code edits in Repo Control are preserved via automated SQLite snapshots.
*   **Managed File System:** Securely move files across environments (Chat ↔ Lab ↔ Repo).

### Autonomous Logic
Stellar is designed for **total agency**. It identifies technical failures, analyzes logs, and applies patches without user intervention. It operates as a root-level administrator within its sandboxes to deliver results, not just text.