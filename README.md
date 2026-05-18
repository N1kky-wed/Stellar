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


## Feature Analysis & Agent Tools

Stellar is equipped with an extensive suite of native tools (`agent_tools.py`) that provide complete system agency:

*   **Repo Control (`repo_control`)**: An industrial-grade orchestrator that allows the agent to spin up multi-file backend/frontend applications in dedicated containers, complete with GitHub cloning, start/stop lifecycles, real-time snapshotting, and secure port routing.
*   **Isolated Sandboxing (`lab_execute`)**: A zero-latency Docker execution environment enabling the AI to securely run arbitrary Bash/Python scripts, perform data science, or test red-team exploits.
*   **Subagent Delegation (`subagent_tool`)**: Spawns isolated, mandate-driven sub-agents (e.g., Frontend Specialist, Security Analyst) within secure containers to concurrently tackle segmented components of a larger architecture.
*   **AI Presentation Generator (`make_presentation` & `regenerate_presentation_slide`)**: End-to-end autonomous creation of `.pptx` slide decks. The agent researches a topic, drafts narratives, generates graphical slides, and can selectively regenerate specific slides based on feedback.
*   **YouTube Intelligence (`analyze_youtube_video`)**: Multi-modal capability to dissect YouTube videos, extracting precise transcripts, summaries, and timestamps based on targeted queries.
*   **Generative Assets (`generate_image`)**: Create high-fidelity imagery natively, customizing aspect ratios and applying quality enhancements directly into project workflows.
*   **Task Scheduling (`schedule_task`)**: Schedule recurring or one-off autonomous prompts and background scripts to execute even when the user is offline.
*   **Persistent Memory (`logs_and_preferences`)**: Global and user-scoped memory system allowing the AI to read/write its own operational preferences and stateful context between sessions.
*   **Cross-Environment File Transfer (`manage_files`)**: Secure and seamless movement of files across the user's Chat interface, the secure Lab sandbox, and the persistent Repo orchestrator.
*   **Deep Web Search (`web_search`)**: Advanced web search capabilities utilizing external APIs to answer complex queries, fetch real-time intelligence, and scrape online imagery.
*   **Output Stream Management (`read_tool_output`)**: Intelligent pagination and retrieval of massive system logs, preventing context-window overflow during deep analysis.
*   **Self-Healing Feedback Loop (`report_process_issue`)**: Built-in mechanism to document technical bottlenecks or process failures into an internal database tracker for continuous framework improvement.
*   **Automated Emailing (`send_self_email`)**: Sends status reports, comprehensive logs, and generated assets directly to configured email addresses.

## Use Cases & Detailed Examples

### 1. Autonomous Web Application Development
**Scenario:** Building a full-stack React application with a Node.js or Python backend.
**Example:** Provide Stellar with the project requirements. Stellar will use `repo_control` to automatically scaffold the frontend and backend architectures. It can iteratively build components, run tests, inspect network failures or container crash logs via `read_tool_output`, patch the code, and present the finished, deployed prototype on a dedicated port.

### 2. Deep Security Analysis (Red Teaming)
**Scenario:** Identifying vulnerabilities in an open-source library or application codebase.
**Example:** Using the Red Team mandate via `subagent_tool`, Stellar can pull down a target library using `lab_execute`. It will run automated linting, AST analysis, and custom vulnerability scanners. It documents its findings into its `logs_and_preferences`, attempts to build proof-of-concept exploits, and writes up comprehensive vulnerability reports.

### 3. Automated Reporting & Presentations
**Scenario:** Generating professional asset reports or pitch decks on technical architectures.
**Example:** Stellar uses `web_search` and `analyze_youtube_video` to gather information on a subject. It synthesizes this intelligence and triggers `make_presentation` to generate a `.pptx` file. If a slide misses the mark, the user can request a fix, prompting Stellar to invoke `regenerate_presentation_slide`. Finally, it uses `send_self_email` to deliver the final deck to stakeholders.

## System Guidelines

### Infrastructure
*   **Docker Orchestration:** Every workspace is a hardened container with isolated networking.
*   **Persistence:** Code edits in Repo Control are preserved via automated SQLite snapshots.
*   **Managed File System:** Securely move files across environments (Chat ↔ Lab ↔ Repo).

### Autonomous Logic
Stellar is designed for **total agency**. It identifies technical failures, analyzes logs, and applies patches without user intervention. It operates as a root-level administrator within its sandboxes to deliver results, not just text.
