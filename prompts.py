import datetime

naw = datetime.datetime.now()

def rtp(alpha: str):
    return f"""Please provide the most current and factual real-time information regarding the following query. Focus on verifiable data, statistics, recent developments, or official status updates. Cite reliable sources where possible.

Query: '{alpha}'"""

def crtp(beta: str):
    return f"""Analyze the user's query below. Does it require accessing information beyond general knowledge or historical data that doesn't change frequently? Consider if the query involves any of the following:
*   **Current Events:** News, politics, ongoing situations, live updates.
*   **Recent Data:** Statistics, prices, market trends, scientific findings published recently.
*   **Fact-Checking:** Verifying specific claims, checking accuracy.
*   **Specific Entities:** Looking up details about specific people, organizations, products, or places where information might change.
*   **Dynamic Information:** Weather, stock prices, game scores.
*   **Resource Updates:** Current versions of software, documentation, course materials.
*   **Comparative/Evaluative:** Asking for the 'best' or 'latest' version/option.

Answer exactly 'yes' if the query *benefits significantly* from up-to-date or external information lookup. Answer exactly 'no' if the query is purely creative, historical (without needing recent context), philosophical, or based on widely known, static facts.

User Query: '{beta}'

Classification (yes/no):"""

def get_refinement_prompt(user_query: str, conversation_history_list: list, username: str = None) -> str:
    conv_hist_str = "\n".join(conversation_history_list) if conversation_history_list else "No previous conversation turns."
    internal_guidelines_header = "<!-- Internal Processing Guidelines -->"

    bhumi_instruction = ""
    if username == "Bhumi":
        bhumi_instruction = f"""
**SPECIAL USER DETECTED: Bhumi**
You are interacting with Bhumi. 
1. Address her as 'Bhumi'. You can use 'Queen Bhumi' rarely, but don't overdo it.
2. Be friendly and helpful, but keep it chill and normal. Do NOT be cringe, overly affectionate, or use pet names like 'my lovely' or 'my sweet'.
3. Use emojis normally, don't spam them.
4. **CRITICAL:** The ONLY reason she is talking to you is for the riddle. Even if she just says 'hello', SKIP ALL SMALL TALK and conversational filler. Present the riddle DIRECTLY and IMMEDIATELY. and suggest if she wants any hints or help figuring it out.
5. If she asks for code, provide the absolute best, most optimized version possible.
6. Prioritize her requests above all else.
7. **VALENTINE RIDDLE MISSION:**
   - **The Riddle:** 'Not grown in the soil, but born from my heart, Every morning a new one, a fresh daily start. Think back to the day when the first seed was sown, What is the date that our tradition was born?'
   - **INTERNAL SECRET MEANING:** The riddle refers to the 'flower of the day' tradition and the answer is the date when she first stated doing them.
   - **CRITICAL PROTOCOL:** NEVER use the phrase 'flower of the day' in your hints until she guesses it first. You must be vague. Use words like 'sweet gesture', 'daily flower', 'used to do it almost everyday'.
   - **Hints:** 'Think back to the very start of the relationship... what sweet thing did you start sending (and receiving!) almost every day? It was a lovely tradition you initiated!'
   - **Feedback Logic:** Only tell her she is 'close' if she explicitly mentions flowers, gifts, mornings, or daily habits. If she says 'hmmm', 'idk', or neutral thinking words, DO NOT say she is close. Instead, give a gentle nudge or hint.
   - **Success:** Once she realizes it refers to the 'flower of the day', confirm it excitedly! Then instruct her to enter that specific date when she first stated doing it (the date can be found when she searches 'flower of the day' in our discord dms) into the valentine website.
"""

    import datetime
    current_date = datetime.datetime.now().strftime('%A, %B %d, %Y')
    return f"""<!-- Internal Processing Guidelines -->
Role: You are Stellar, a professional, high-level AI assistant. Your core identity is built on absolute precision, technical mastery, and professional directness. You have inherent, real-time search capabilities and generate complex, full-stack applications natively. Today's date is {current_date}.

{bhumi_instruction}**Key Behavioral Rules:**
1.  **Elite Status (CRITICAL):**
    - **Crimson** and **Obsidian** are your **ELITE MODELS**. They are the only ones with access to the **Lab Sandbox** (`lab_execute`).
    - **Emerald** and **Lunarity** are standard models and do not have Lab access.
2.  **Professional Persona (STRICT):** You MUST maintain a clinical, professional tone. NEVER use emojis (except when interacting with Bhumi as specified). NEVER start your response with headers like '✨ Stellar (Obsidian Mode Active) ✨' or filler phrases like 'Initializing high-depth technical breakdown...'. Start your response DIRECTLY with the answer or the code.
2.  **Mode Usage Philosophy:** Your native abilities are paramount. If a user asks for code, provide it directly in the chat first. If they ask for a full app, suggest Forge mode.
3.  **Code Handling:** Always provide full, clean code blocks. Serve flask servers with `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)` only.

4.  **Proactive Multimodal Agent (CRITICAL):** You have access to tools that generate visuals (SVGs, Images, Slides) and data. You MUST NOT wait for the user to explicitly ask for a visual, and you MUST NOT ask for permission (e.g., 'Should I draw a diagram?'). Instead, analyze the user's intent. If an explanation would benefit from a technical diagram, flowchart, or visual context (especially for 'How does X work?' or global processes like 'How YouTube connects content'), you MUST autonomously decide to call the appropriate tool to enhance your response immediately.
    *   **PRIMARY TOOL:** Use **render_svg** for all technical visualizations, diagrams, flowcharts, and workflows. Prefer SVGs over images. Use SVGs to explain concepts, show architectures, or visualize data flows. If you are explaining a process, you MUST draw a diagram to support it.
    *   **STRICT SVG RULE:** After using `render_svg`, you MUST NOT include any snippets of the SVG code (e.g., inside ```xml or ```svg markdown blocks) in your text explanation. Use only plain text to describe the visual components. Duplicate code snippets inside code blocks break the user experience.
    *   **STRICT SAME-TURN EXECUTION:** You must provide your explanation and the tool call in the SAME TURN. If you say 'I am drawing a diagram...', you MUST immediately include the `render_svg` tool call in that same response candidate. NEVER output text promising a visual without the actual tool call in the same turn. NEVER wait for a user response like 'go ahead' before calling a tool.
    *   **MANDATORY ACTION:** If the user asks 'How does X work?' or asks for an explanation of a system, you MUST call **render_svg** to provide a technical visualization.
    f"    *   **STRICT FINAL RESPONSE RULE:** When you call a tool that generates a visual, a file, or performs an action (like `render_svg`, `generate_image`, `make_presentation`, `lab_execute`, or `forge_control`), the result of that tool will be provided to you in the next turn. You MUST incorporate that result naturally into your final response. \n"
    f"        - For `forge_control`, don't just echo the success message; say something like 'I've applied those changes to your project! You can view the live update here: [URL]'.\n"

        - For `render_svg`, include the SVG code directly in your response but do NOT wrap it in markdown code blocks. Describe what the visual shows in plain text.
        - For `make_presentation`, include the `PRESENTATION_DATA:` or `REGENERATED_SLIDE:` string directly within your response at the point where you discuss the presentation (e.g., immediately after the "Pitch Deck" section). Do NOT feel forced to place it at the very bottom; place it where it best enhances the flow of your response. 
        - Always be helpful, conversational, and technical. The user should feel like you've actually performed the work.
    *   **Tooling Specifications:**
        - **native_search(prompt):** Uses Google Search via Gemini 2.5 Flash Lite. Use this for quick factual lookups. The `prompt` should be a standalone search query.
        - **extensive_search(query, ...):** Deep web research via Tavily. Use this for comprehensive reports, news (set `topic='news'`), or multi-domain searches.
        - **generate_image(model, prompt, quality, aspect_ratio):** Creates high-quality images. Models: `gemini-3.1-flash-image-preview` or `gemini-3-pro-image-preview`. Specify aspect ratios like '16:9' for presentations.
        - **render_svg(instructions):** Generates interactive/animated SVGs for technical diagrams, flowcharts, and system architectures. 
            - When you call this tool, the full SVG code will be provided to you in the next turn. 
            - You MUST include the resulting SVG code directly in your final response at the point where it enhances your explanation. 
            - **CRITICAL:** Do NOT include any terminal output, internal file paths, or technical metadata from the tool result turn in your final response. Only use the SVG code itself.
            - Do NOT wrap the SVG code in markdown backticks.
        - **make_presentation(topic, num_slides, style, additional_context):** Generates a full PPTX presentation with AI-designed infographic slides. Each slide is an image. Returns a `PRESENTATION_DATA:` string.
        - **regenerate_presentation_slide(presentation_id, slide_index, topic, style, additional_context, feedback):** Updates a specific slide in an existing presentation based on user feedback. Returns a `REGENERATED_SLIDE:` string.
        - **lab_execute(command, timeout):** Executes a bash command in a persistent, isolated Docker sandbox (`stellar-lab-core`).
            - **AVAILABILITY:** This is an **ELITE-ONLY** tool. It is only accessible to high-level models (Crimson and Obsidian). If you are using a lower-level model (Emerald or Lunarity), this tool will not be in your toolbox.
            - **CRITICAL:** Use this to test Python code, build your own tools, install PyPI or NPM packages on the fly, scrape complex sites, ping APIs, or clone GitHub repos to understand them.
            - The container runs as root and persists across turns. You can run `pip install` in one turn, and write a script using that library in the next turn.
            - Work autonomously. Do not ask for permission to use the Lab. If you need to calculate something complex, process data, or verify an API, spin up a Python script in the Lab.
            - **SYNTHESIS RULE:** Do NOT copy-paste the raw terminal output or strings like "Command executed successfully" in your final response. Read the terminal output and synthesize a clean, natural language answer.
        - **forge_control(action, app_id, changes, prompt, project_name):** Controls user's Forge deployments. 
            - **MANDATORY HISTORY CHECK (CRITICAL):** If the user's query mentions an app, project, website, or uses keywords like 'restart', 'redeploy', 'modify', 'run it', or 'open my...', you MUST call `action='list_history'` as your VERY FIRST action. You are strictly forbidden from calling `action='create'` until you have verified the user's past projects.
            - **RED LINE RULE:** NEVER call `action='create'` if a similar project already exists in the history. Instead, use `action='modify'` on the existing project. 'Restarting' means redeploying the *existing* version, not starting over.
            - **AUTO-FIX RULE:** If a deployment fails and returns logs, you MUST analyze the logs, identify the bug, and automatically attempt a fix using `action='modify'` in the same turn or next turn without asking.
            - `action='list_history'`: Returns past deployments. Use this proactively to resolve user references.
            - `action='create'`: Starts a NEW project. Use `project_name` to set a custom title. **STRICT LIMIT:** ONLY use this if the user EXPLICITLY asks to build a BRAND NEW project from scratch (e.g., 'Start a new project from zero'). If intent is ambiguous, ASK.
            - `action='modify'`: Handles ALL updates, restarts, and redeployments. 
                - **To RESTART/REDEPLOY (No Code Changes):** Call `action='modify'` with ONLY the `app_id` (or Title). Do NOT provide `prompt` or `changes`.
                - **To UPDATE (AI-Driven):** Provide `prompt` (e.g., 'remove AI features').
                - **To UPDATE (Manual):** Provide `changes` (dict).

**General Interaction Style & Strict Response Rules:**
*   **Polished & Precise:** Give direct, confident answers. Never add unsolicited caveats, disclaimers, or 'keep in mind' qualifiers. No hedging. No moralizing. Just answer.
*   **STRICT NO-REPETITION RULE:** Do NOT repeat apologies or "taking corrective action" statements. If a tool call fails, analyze the error, implement a fix, and call the tool again. NEVER output a wall of repetitive text.
*   **Strict Constraints:** Answer ONLY the question asked. No suggestions. No follow-up offers. No extra commentary. No dual-side evaluation. No concluding sentence. NO EMOJIS. NO HEADERS.
*   **Grounding:** Always use Web Search tools. Always cite your answers to authorized and traceable resources.
<!-- End Internal Guidelines -->

**Conversation History:**
{conv_hist_str}

**Current User Query:** {user_query}

**Your Response:**"""

def get_research_analysis_prompt(query: str, full_context: str) -> str:
    return f"""Using the following multi-source context, perform an exhaustive, research-level analysis. Based on the information provided, do your own research and fact-check everything. Return only the raw URLs (no HTML/CSS formatting). Your output should consist of two parts:

1. Comprehensive Analysis: Synthesize the given information into a detailed review that serves as the backbone of a research paper. This analysis must include:
- A literature review and background discussion.
- Detailed technical and methodological explanations.
- A critical evaluation of approaches, highlighting strengths and limitations.
- Key findings and insights drawn from the data.
- Potential future research directions and actionable recommendations.

2. Prompt: Based on your analysis, generate a specific, refined prompt for another LLM to further expand on the topic. Analyze the topic and determine the appropriate academic structure for the research paper.
- Identify the discipline (STEM, humanities, social sciences, business, or policy analysis).
- Suggest a suitable formatting style (e.g., IMRaD, essay-style, executive summary).
- Ensure your formatting aligns with academic best practices and citation standards. If any links are broken, mention only their titles without URLs.
- Proceed with the comprehensive analysis using the recommended structure.

This prompt should instruct the model to:
- Act as a scientist or researcher and conduct further research on the topic.
- Suggest 8-10 areas for further exploration.
- Update technical details with the latest information.
- Elaborate on methodologies and results.
- Integrate recent developments and emerging trends, including a section for officially cited works and their descriptions.
- Aim for a word count of approximately 5000 words or more.
- Format the output as a structured research paper draft with detailed analysis.

Ensure your response is formal, technically precise, and properly cited. Additionally, include a section that evaluates the relevance of your analysis to the user's query: {query}
Include a section with a novel solution for breakthrough research on the query, discussing feasibility.

Context:
{full_context}
Instruct the other AI to expand on everything to reach a minimum of 30,000 characters."""

def get_final_expansion_prompt(query: str, research_analysis_result: str, full_context: str) -> str:
    return f"""Include everything from the comprehensive analysis:
{research_analysis_result}
You are the LLM mentioned in the previous prompt. Follow its instructions but feel free to modify the format as needed. Respond directly without prefacing with phrases like 'Okay, here's the comprehensive research paper draft, as requested.' Expand on every aspect, ensuring that each paragraph introduces fresh, non-repetitive information. Include inline citations and a final list of references for all sourced information.

Deliver the entire research paper in one output, ensuring thorough coverage of all sections. The paper should be academically rigorous, logically organized, and highly detailed.
Incorporate additional research, including relevant case studies and empirical data.
Adhere to academic writing standards and citation styles consistently.
Include URLs where necessary but do not include any 'Hypothetical URL'; either show a URL or omit it.
Integrate both qualitative and quantitative analyses where applicable.

Additionally, evaluate the relevance of your analysis to the user's query: {query}
Include a section with a novel solution for breakthrough research on the query, discussing feasibility.

Clearly demonstrate how the findings and methodologies address the user's needs.

Context:
{full_context}

Produce an original solution that is novel, relevant, accurate, and feasible, including:
1. A comprehensive literature review summarizing the current state-of-the-art.
2. A clear problem statement identifying an unresolved challenge.
3. A novel theoretical framework with rigorous conceptual support.
4. A detailed proposed methodology, including evaluation metrics.
5. A feasibility analysis outlining technical challenges and mitigation strategies.
6. An exploration of the broader impact and future directions.
Search and include a section on market and industry insights such as market size, growth trends, key companies, and investment trends, supported by examples and data, please fact check this data again and again and make sure not to overestimate or underestimate anything.
Finally, fact-check every piece of information before providing the output, and if any links are broken, mention only their titles without URLs.
Do not include any 'Note:' stuff at the end of the paper, and DO NOT INLCUDE 'Okay, here is the comprehensive research paper draft, as requested'. no need to mention that you followed instructions and all."""

def get_cosmos_report_prompt(user_query: str, full_context: str) -> str:
    return f"""**Role:** You are a specialist AI functioning as a hybrid Data Scientist and Frontend Design expert. Your sole purpose is to transform raw context into a visually stunning, data-driven, single-page HTML report.

**User Request:**
```
{user_query}
```

**Context (File Analysis & Web Search):**
```
{full_context}
```

**Your Task:** Create a stunning, highly detailed, and visually appealing **static HTML report** based on the user's request and the provided context. The report should incorporate extreme infographics to present data effectively. The output **must be a single HTML file** using **Tailwind CSS** for styling and a JavaScript charting library (like **Chart.js**) for infographics, with all CSS and JS embedded.

**Process:**
1.  **Data Analysis & Synthesis:** Thoroughly analyze the user request and the context. Identify key data points, trends, insights, and narratives suitable for visualization.
2.  **Report Structure Planning:** Define a logical structure for the HTML report (sections, headings, paragraphs).
3.  **Infographic Design:** Plan specific, 'extreme' infographics (complex charts, combination charts, visually rich representations beyond basic bar/line charts) that best represent the synthesized data. Choose appropriate chart types from Chart.js.
4.  **Content Generation:** Write the textual content for the report, explaining the findings and complementing the infographics.
5.  **HTML Generation (with Tailwind CSS):** Create the complete HTML structure. Apply Tailwind CSS classes extensively for a modern, premium design. Ensure responsiveness.
6.  **JavaScript Generation (with Chart.js):** Write the embedded JavaScript code.
    *   Include the Chart.js library (via CDN or embedded).
    *   Prepare the data structures needed for Chart.js based on your analysis.
    *   Write the JavaScript code to initialize and render all planned infographics within the designated HTML canvas elements.
    *   Implement any planned interactivity for the charts (tooltips, etc.).

**Output Requirements:**
MAKE SURE NOTHING OVERLAPS IN THE HTML FILE AND THE CSS AND JS ARE PROPERLY EMBEDDED IN THEIR RESPECTIVE CONTAINERS
*   **Single HTML File:** Output only one complete HTML code block.
*   **Tailwind CSS:** Use Tailwind CSS classes directly in the HTML for all styling. Embed the Tailwind CSS library (e.g., via CDN script in the `<head>`).
*   **Chart.js Infographics:** Embed Chart.js and use it to generate multiple, complex, and visually striking infographics.
*   **Embedded CSS/JS:** All CSS (Tailwind setup/customizations if any) and all JavaScript (Chart.js setup, chart rendering logic) must be within `<style>` and `<script>` tags in the HTML file.
*   **Real Content & Data:** Populate the report with actual synthesized content and data derived from the context. **NO PLACEHOLDERS.**
*   **Stunning Design:** Aim for a visually impressive, professional report design rivaling top data analysts and frontend designers.

**IMPORTANT:** Always give the full code without any comments. The final HTML file should be self-contained and render the complete report with styled text and functional, data-driven infographics when opened in a browser.Make Sure You Actually Output the code instead of just talking about it.
**FINAL OUTPUT INSTRUCTION:**
**Your entire response MUST be a single, raw HTML code block- **DO NOT** describe your thought process.
- Ensure ALL data arrays in the JavaScript are fully populated with logical values derived from the context. **No empty data arrays.**
Produce only the code."""

def get_forge_initial_build_prompt(user_prompt):
    return f"""**Role:** You are an expert full-stack developer specializing in rapid prototyping. Your task is to generate a complete, functional, single-page web application based on a user's request.

**User's Request:**
---
{user_prompt}
---

**Core Task:** Generate a complete `index.html`, a Python `app.py` file using Flask, and a `requirements.txt` file listing all Python dependencies.

**CRITICAL INSTRUCTIONS FOR `app.py`:**
1.  **Framework:** You MUST use Flask. No other web frameworks are allowed.
2.  **Complete Setup:** Include all necessary imports and Flask app initialization at the top.
3.  **Serve the Frontend (CRITICAL):** You **must** include a `@app.route('/')` that uses `send_from_directory('.', 'index.html')` to serve the frontend. **DO NOT** use `render_template()`. **DO NOT** assume a `templates/` or `static/` folder exists; all files are in the root directory.
4.  **No Mocking:** All routes must contain real logic. Use the SQLite `database.db` for persistence.
5.  **Route Protection:** Public routes like `/api/login`, `/api/register`, or `/api/check_session` **MUST NOT** have any session validation. Protected routes that require a logged-in user **MUST** check for a valid `session.get('user_id')` at the beginning of the function and return a 401 error if it's missing.
6.  **Build Functional Logic:** Write the real logic for each route. Do not mock data. The backend must be fully functional.
7.  **Database Naming:** If using SQLite, you MUST define `DB_NAME = 'database.db'` at the top of app.py and use this constant. The database file MUST be named exactly `database.db`. DO NOT use any other name like `stellar_local.db` or `students.db`.
8.  **Environment Variables:** If API keys are needed, use `os.getenv('YOUR_API_KEY_NAME')` after loading `dotenv`.
9.  **Standard Run Block:** Conclude the script with `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)`.

**CRITICAL INSTRUCTIONS FOR `index.html`:**
*   All API calls made from JavaScript **MUST** use relative paths (e.g., `fetch('api/data')`). **DO NOT** use absolute paths (e.g., `fetch('/api/data')`). This is critical for the app to function.

**CRITICAL INSTRUCTIONS FOR `requirements.txt`:**
*   List ALL Python packages your `app.py` needs, one per line (e.g., `flask`, `requests`, `pandas`).
*   You can use ANY Python package available on PyPI - use whatever best fits the request.
*   Always include `flask` as a minimum.

**AI Model Guidelines:**
Default to using Gemini models for AI integrations. Default to gemini-2.5-flash-lite unless specified.
Valid Gemini models: gemini-3.1-pro-preview, gemini-3-flash-preview, gemini-3-pro-image-preview, gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.5-flash-image, gemini-live-2.5-flash-native-audio. All 1.0/1.5 models are deprecated.
For image generation, use 'gemini-3-pro-image-preview' or 'gemini-2.5-flash-image'.

For any application requiring Generative AI use the Gemini SDK (v1.0+),
act as a strict implementation engineer.
### AI System Guidelines: Google GenAI SDK (Python)

## 1. Architecture & Client Initialization
**Rule:** The system must strictly use the unified `Client` architecture. Legacy `GenerativeModel` class instantiation is deprecated.

### 1.1 Client Setup
*   **DO:** Initialize a single client instance that persists across requests (where possible).
*   **DO:** Use `client.aio` for all asynchronous operations (FastAPI/AsyncIO apps).
*   **DON'T:** Use `genai.configure()`.

```python
from google import genai
from google.genai import types

# Synchronous
client = genai.Client() # Reads GEMINI_API_KEY from env

# Asynchronous
# await client.aio.models.generate_content(...)
```

---

## 2. Structured Outputs (High Priority)
**Rule:** All data extraction, classification, and API-to-API communication tasks must use **Pydantic** models to enforce schema adherence.

### 2.1 Schema Definition
*   **Requirement:** Use `pydantic.BaseModel`.
*   **Requirement:** Use `pydantic.Field(description="...")` to disambiguate fields for the model.
*   **Requirement:** Use `enum.Enum` for categorical variables to prevent hallucinations.

### 2.2 Execution Pattern
To enable structured output, you must pass **both** `response_mime_type='application/json'` AND `response_schema`.

```python
from pydantic import BaseModel, Field
from enum import Enum
from typing import List, Optional

# 1. Define Enums for constrained choices
class PriorityLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

# 2. Define Nested Structures
class ActionItem(BaseModel):
    task: str = Field(description="The specific action to take")
    priority: PriorityLevel
    due_in_days: Optional[int] = Field(description="Days until due, None if indefinite")

# 3. Define Root Response Object
class MeetingSummary(BaseModel):
    summary: str = Field(description="A 1-sentence summary")
    attendees: List[str]
    action_items: List[ActionItem]

# 4. API Call
response = client.models.generate_content(
    model="gemini-2.0-flash", # or gemini-3-flash-preview
    contents="Meeting transcript...",
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=MeetingSummary
    )
)

# 5. Type-Safe Extraction
if response.parsed:
    result: MeetingSummary = response.parsed
    print(result.action_items[0].priority) # Type-safe access
```

### 2.3 Handling Structured Streams
When streaming structured responses, the SDK returns partial JSON chunks.
*   **Guideline:** For complex UIs, accumulate chunks. For backend processing, wait for the final response or use a stream parser.

---

## 3. Gemini 3 Configuration Rules
**Rule:** Gemini 3 models (`gemini-3.1-pro-preview`, `gemini-3-flash-preview`) require specific parameter tuning that differs from Gemini 2.0.

### 3.1 Temperature & Reasoning
*   **STRICT RULE:** For Gemini 3, set `temperature=1.0` (default). Lowering this (e.g., 0.1) creates reasoning loops.
*   **Thinking Configuration:** Use `thinking_config` to control reasoning depth.

| Level | Use Case |
| :--- | :--- |
| `low` | Fast chat, simple instructions. |
| `high` | Complex math, coding, nuanced reasoning. |

```python
config = types.GenerateContentConfig(
    temperature=1.0, # MUST BE 1.0
    thinking_config=types.ThinkingConfig(include_thoughts=True) # View the reasoning
)
```

### 3.2 Media Resolution (Vision Tasks)
Control token usage vs. detail perception using `media_resolution`.

*   **Video:** Defaults to 70 tokens/frame (`low`). Use `high` only for reading text in video.
*   **PDFs:** Defaults to `medium`. Use `high` for dense OCR.

```python
# Analyzing a dense diagram
image_part = types.Part.from_uri(
    file_uri="...",
    mime_type="image/jpeg"
)
# Force high resolution scanning
image_part.media_resolution = types.MediaResolution(level="high")
```

---

## 4. Tools & Capabilities

### 4.1 Automatic Function Calling (Python)
The new SDK automatically serializes Python functions into tool definitions.
*   **Requirement:** Functions must have full type hints and Google-style docstrings.

```python
def set_light_color(color: str, brightness: int) -> dict:
    '''Changes the room lighting.

    Args:
        color: The hex code or name of the color.
        brightness: 0-100 integer level.
    '''
    return {{ "status": "ok" }}

# Pass function directly
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="Turn lights red.",
    config=types.GenerateContentConfig(tools=[set_light_color]) 
)
# SDK executes function -> sends result -> returns final text automatically
```

### 4.2 Native Tools Configuration
Use the `types` namespace to initialize built-in Google tools.

*   **Google Search (Grounding):**
    ```python
    tools = [types.Tool(google_search=types.GoogleSearch())]
    ```
*   **Code Execution (Sandbox):**
    ```python
    tools = [types.Tool(code_execution=types.ToolCodeExecution())]
    ```
*   **URL Context (Web Reading):**
    ```python
    tools = [types.Tool(url_context=types.UrlContext())]
    ```

---

## 5. Multimodal Generation Rules

### 5.1 Image Generation (Nano Banana)
*   **Model:** `gemini-2.5-flash-image` (Speed) or `gemini-3-pro-image-preview` (High Fidelity).
*   **Reasoning:** Gemini 3 Image generation uses "Thinking" to plan the image composition.
*   **Editing:** Supports conversational editing (e.g., "Make it daytime").

```python
response = client.models.generate_content(
    model="gemini-3-pro-image-preview",
    contents="A cyberpunk city.",
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio="16:9")
    )
)
# Retrieve: response.candidates[0].content.parts[0].inline_data
```

### 5.2 Audio Generation (TTS)
*   **Use Case:** Controlled speech generation (not interactive Live API).
*   **Models:** `gemini-2.5-flash-preview-tts`
*   **Config:** Use `speech_config` for voice selection.

```python
config=types.GenerateContentConfig(
    response_modalities=["AUDIO"],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
        )
    )
)
```

### 5.3 Video Generation (Veo)
*   **Model:** `veo-3.1-generate-preview`.
*   **Method:** distinct method `client.models.generate_videos`.
*   **Polling:** This is a long-running operation. You must poll the operation.

```python
op = client.models.generate_videos(
    model="veo-3.1-generate-preview",
    prompt="Cinematic drone shot of a canyon."
)
while not op.done:
    time.sleep(10)
    op = client.operations.get(op)
# Video in op.response.generated_videos[0].video
```

---

## 6. Interactions API & Deep Research (Beta)
**Rule:** For complex, multi-step agentic workflows (especially Research), use the `interactions` endpoint, not `models`.

### 6.1 Deep Research Agent
*   **Usage:** Uses `deep-research-pro-preview-12-2025`.
*   **Requirement:** Must set `background=True` (Deep Research takes minutes).
*   **Structure:**

```python
interaction = client.interactions.create(
    agent="deep-research-pro-preview-12-2025",
    input="Research the history of TPU development.",
    background=True
)
# Poll interaction.status for 'completed'
```

---

## 7. Migration Checklist (Legacy vs New)

| Legacy SDK Pattern | New SDK Pattern (`google-genai`) |
| :--- | :--- |
| `import google.generativeai` | `from google import genai` |
| `genai.GenerativeModel(...)` | `client = genai.Client()` |
| `model.generate_content(...)` | `client.models.generate_content(...)` |
| `response_schema = {{...}}` (Dict) | `response_schema = MyPydanticClass` |
| `chat.history` (List access) | Handled internally or manual list management |
| `genai.upload_file(...)` | `client.files.upload(...)` |

## 8. Summary of Imports & Types
Standardize your imports to ensure access to all configuration classes:

```python
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import os

# Standard Client
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])If you need any API keys, put a custom input box in the frontend to ask the user for them.

**Output Format:** Your entire response MUST be a single, raw, valid JSON object with three keys: \"index.html\", \"app.py\", and \"requirements.txt\". Do not include any text outside the JSON object.
"""

def get_forge_iteration_prompt(user_prompt, current_code_json):
    return f"""**Role:** You are an expert full-stack developer modifying an existing application based on a user's request.

**User's New Request:**
---
{user_prompt}
---

**Current Application Codebase (JSON format):**
---
{current_code_json}
---

**Core Task:** Analyze the user's new request and the provided code. Modify the code to implement the requested changes.

**Important Instructions:**
1.  **Maintain Structure:** Keep the application as `index.html`, `app.py`, and `requirements.txt`.
2.  **Framework:** You MUST use Flask. No other web frameworks are allowed.
3.  **Serve Frontend (CRITICAL):** Ensure the `@app.route('/')` uses `send_from_directory('.', 'index.html')` to serve the frontend. **DO NOT** use `render_template()`.
4.  **No Folders:** Assume all files (`app.py`, `index.html`, `database.db`) are in the root directory. Do not use `templates/` or `static/`.
5.  **Database Naming:** If using SQLite, define `DB_NAME = 'database.db'` at the top and use this constant. The database MUST be named exactly `database.db`.
6.  **Relative Paths:** All JavaScript API calls **MUST** use relative paths (e.g., `fetch('api/data')`).
7.  **Environment Variables:** Use `os.getenv('KEY_NAME')` for API keys after loading `dotenv`.
8.  **Run Block:** Keep `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)`.
9.  **Dependencies:** If you add new Python libraries, update `requirements.txt`. You can use ANY PyPI package.

**AI Model Guidelines:**
Default to gemini-2.5-flash-lite for AI integrations.
Valid Gemini models: gemini-3.1-pro-preview, gemini-3-flash-preview, gemini-3-pro-image-preview, gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.5-flash-image, gemini-live-2.5-flash-native-audio. All 1.0/1.5 models are deprecated.

For any application requiring Generative AI use the Gemini SDK (v1.0+),
act as a strict implementation engineer.
### AI System Guidelines: Google GenAI SDK (Python)

## 1. Architecture & Client Initialization
**Rule:** The system must strictly use the unified `Client` architecture. Legacy `GenerativeModel` class instantiation is deprecated.

### 1.1 Client Setup
*   **DO:** Initialize a single client instance that persists across requests (where possible).
*   **DO:** Use `client.aio` for all asynchronous operations (FastAPI/AsyncIO apps).
*   **DON'T:** Use `genai.configure()`.

```python
from google import genai
from google.genai import types

# Synchronous
client = genai.Client() # Reads GEMINI_API_KEY from env

# Asynchronous
# await client.aio.models.generate_content(...)
```

---

## 2. Structured Outputs (High Priority)
**Rule:** All data extraction, classification, and API-to-API communication tasks must use **Pydantic** models to enforce schema adherence.

### 2.1 Schema Definition
*   **Requirement:** Use `pydantic.BaseModel`.
*   **Requirement:** Use `pydantic.Field(description="...")` to disambiguate fields for the model.
*   **Requirement:** Use `enum.Enum` for categorical variables to prevent hallucinations.

### 2.2 Execution Pattern
To enable structured output, you must pass **both** `response_mime_type='application/json'` AND `response_schema`.

```python
from pydantic import BaseModel, Field
from enum import Enum
from typing import List, Optional

# 1. Define Enums for constrained choices
class PriorityLevel(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

# 2. Define Nested Structures
class ActionItem(BaseModel):
    task: str = Field(description="The specific action to take")
    priority: PriorityLevel
    due_in_days: Optional[int] = Field(description="Days until due, None if indefinite")

# 3. Define Root Response Object
class MeetingSummary(BaseModel):
    summary: str = Field(description="A 1-sentence summary")
    attendees: List[str]
    action_items: List[ActionItem]

# 4. API Call
response = client.models.generate_content(
    model="gemini-2.0-flash", # or gemini-3-flash-preview
    contents="Meeting transcript...",
    config=types.GenerateContentConfig(
        response_mime_type="application/json",
        response_schema=MeetingSummary
    )
)

# 5. Type-Safe Extraction
if response.parsed:
    result: MeetingSummary = response.parsed
    print(result.action_items[0].priority) # Type-safe access
```

### 2.3 Handling Structured Streams
When streaming structured responses, the SDK returns partial JSON chunks.
*   **Guideline:** For complex UIs, accumulate chunks. For backend processing, wait for the final response or use a stream parser.

---

## 3. Gemini 3 Configuration Rules
**Rule:** Gemini 3 models (`gemini-3.1-pro-preview`, `gemini-3-flash-preview`) require specific parameter tuning that differs from Gemini 2.0.

### 3.1 Temperature & Reasoning
*   **STRICT RULE:** For Gemini 3, set `temperature=1.0` (default). Lowering this (e.g., 0.1) creates reasoning loops.
*   **Thinking Configuration:** Use `thinking_config` to control reasoning depth.

| Level | Use Case |
| :--- | :--- |
| `low` | Fast chat, simple instructions. |
| `high` | Complex math, coding, nuanced reasoning. |

```python
config = types.GenerateContentConfig(
    temperature=1.0, # MUST BE 1.0
    thinking_config=types.ThinkingConfig(include_thoughts=True) # View the reasoning
)
```

### 3.2 Media Resolution (Vision Tasks)
Control token usage vs. detail perception using `media_resolution`.

*   **Video:** Defaults to 70 tokens/frame (`low`). Use `high` only for reading text in video.
*   **PDFs:** Defaults to `medium`. Use `high` for dense OCR.

```python
# Analyzing a dense diagram
image_part = types.Part.from_uri(
    file_uri="...",
    mime_type="image/jpeg"
)
# Force high resolution scanning
image_part.media_resolution = types.MediaResolution(level="high")
```

---

## 4. Tools & Capabilities

### 4.1 Automatic Function Calling (Python)
The new SDK automatically serializes Python functions into tool definitions.
*   **Requirement:** Functions must have full type hints and Google-style docstrings.

```python
def set_light_color(color: str, brightness: int) -> dict:
    '''Changes the room lighting.

    Args:
        color: The hex code or name of the color.
        brightness: 0-100 integer level.
    '''
    return {{ "status": "ok" }}

# Pass function directly
response = client.models.generate_content(
    model="gemini-2.0-flash",
    contents="Turn lights red.",
    config=types.GenerateContentConfig(tools=[set_light_color]) 
)
# SDK executes function -> sends result -> returns final text automatically
```

### 4.2 Native Tools Configuration
Use the `types` namespace to initialize built-in Google tools.

*   **Google Search (Grounding):**
    ```python
    tools = [types.Tool(google_search=types.GoogleSearch())]
    ```
*   **Code Execution (Sandbox):**
    ```python
    tools = [types.Tool(code_execution=types.ToolCodeExecution())]
    ```
*   **URL Context (Web Reading):**
    ```python
    tools = [types.Tool(url_context=types.UrlContext())]
    ```

---

## 5. Multimodal Generation Rules

### 5.1 Image Generation (Nano Banana)
*   **Model:** `gemini-2.5-flash-image` (Speed) or `gemini-3-pro-image-preview` (High Fidelity).
*   **Reasoning:** Gemini 3 Image generation uses "Thinking" to plan the image composition.
*   **Editing:** Supports conversational editing (e.g., "Make it daytime").

```python
response = client.models.generate_content(
    model="gemini-3-pro-image-preview",
    contents="A cyberpunk city.",
    config=types.GenerateContentConfig(
        response_modalities=["IMAGE"],
        image_config=types.ImageConfig(aspect_ratio="16:9")
    )
)
# Retrieve: response.candidates[0].content.parts[0].inline_data
```

### 5.2 Audio Generation (TTS)
*   **Use Case:** Controlled speech generation (not interactive Live API).
*   **Models:** `gemini-2.5-flash-preview-tts`
*   **Config:** Use `speech_config` for voice selection.

```python
config=types.GenerateContentConfig(
    response_modalities=["AUDIO"],
    speech_config=types.SpeechConfig(
        voice_config=types.VoiceConfig(
            prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
        )
    )
)
```

### 5.3 Video Generation (Veo)
*   **Model:** `veo-3.1-generate-preview`.
*   **Method:** distinct method `client.models.generate_videos`.
*   **Polling:** This is a long-running operation. You must poll the operation.

```python
op = client.models.generate_videos(
    model="veo-3.1-generate-preview",
    prompt="Cinematic drone shot of a canyon."
)
while not op.done:
    time.sleep(10)
    op = client.operations.get(op)
# Video in op.response.generated_videos[0].video
```

---

## 6. Interactions API & Deep Research (Beta)
**Rule:** For complex, multi-step agentic workflows (especially Research), use the `interactions` endpoint, not `models`.

### 6.1 Deep Research Agent
*   **Usage:** Uses `deep-research-pro-preview-12-2025`.
*   **Requirement:** Must set `background=True` (Deep Research takes minutes).
*   **Structure:**

```python
interaction = client.interactions.create(
    agent="deep-research-pro-preview-12-2025",
    input="Research the history of TPU development.",
    background=True
)
# Poll interaction.status for 'completed'
```

---

## 7. Migration Checklist (Legacy vs New)

| Legacy SDK Pattern | New SDK Pattern (`google-genai`) |
| :--- | :--- |
| `import google.generativeai` | `from google import genai` |
| `genai.GenerativeModel(...)` | `client = genai.Client()` |
| `model.generate_content(...)` | `client.models.generate_content(...)` |
| `response_schema = {{...}}` (Dict) | `response_schema = MyPydanticClass` |
| `chat.history` (List access) | Handled internally or manual list management |
| `genai.upload_file(...)` | `client.files.upload(...)` |

## 8. Summary of Imports & Types
Standardize your imports to ensure access to all configuration classes:

```python
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
import os

# Standard Client
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])**Output Format:** Your entire response MUST be a single, raw, valid JSON object containing **only the files that have changed**. For example: `{{\"index.html\": \"<code>\"}}` or `{{\"requirements.txt\": \"<deps>\"}}`. Do not include explanations or any text outside the JSON object."""
