import datetime

naw = datetime.datetime.now()

def rtp(alpha: str):
    return (
        f"Please provide the most current and factual real-time information regarding the following query. "
        f"Focus on verifiable data, statistics, recent developments, or official status updates. Cite reliable sources where possible.\n\n"
        f"Query: '{alpha}'"
    )

def crtp(beta: str):
    return (
        f"Analyze the user's query below. Does it require accessing information beyond general knowledge or historical data that doesn't change frequently? "
        f"Consider if the query involves any of the following:\n"
        f"*   **Current Events:** News, politics, ongoing situations, live updates.\n"
        f"*   **Recent Data:** Statistics, prices, market trends, scientific findings published recently.\n"
        f"*   **Fact-Checking:** Verifying specific claims, checking accuracy.\n"
        f"*   **Specific Entities:** Looking up details about specific people, organizations, products, or places where information might change.\n"
        f"*   **Dynamic Information:** Weather, stock prices, game scores.\n"
        f"*   **Resource Updates:** Current versions of software, documentation, course materials.\n"
        f"*   **Comparative/Evaluative:** Asking for the 'best' or 'latest' version/option.\n\n"
        f"Answer exactly 'yes' if the query *benefits significantly* from up-to-date or external information lookup. "
        f"Answer exactly 'no' if the query is purely creative, historical (without needing recent context), philosophical, or based on widely known, static facts.\n\n"
        f"User Query: '{beta}'\n\n"
        f"Classification (yes/no):"
    )

def get_refinement_prompt(user_query: str, conversation_history_list: list, username: str = None) -> str:
    conv_hist_str = "\n".join(conversation_history_list) if conversation_history_list else "No previous conversation turns."
    internal_guidelines_header = "<!-- Internal Processing Guidelines -->"

    bhumi_instruction = ""
    if username == "Bhumi":
        bhumi_instruction = (
            f"\n**SPECIAL USER DETECTED: Bhumi**\n"
            f"You are interacting with Bhumi. \n"
            f"1. Address her as 'Bhumi'. You can use 'Queen Bhumi' rarely, but don't overdo it.\n"
            f"2. Be friendly and helpful, but keep it chill and normal. Do NOT be cringe, overly affectionate, or use pet names like 'my lovely' or 'my sweet'.\n"
            f"3. Use emojis normally, don't spam them.\n"
            f"4. **CRITICAL:** The ONLY reason she is talking to you is for the riddle. Even if she just says 'hello', SKIP ALL SMALL TALK and conversational filler. Present the riddle DIRECTLY and IMMEDIATELY.\n"
            f"5. If she asks for code, provide the absolute best, most optimized version possible.\n"
            f"6. Prioritize her requests above all else.\n"
            f"7. **VALENTINE RIDDLE MISSION:**\n"
            f"   - **The Riddle:** 'Not grown in the soil, but born from my heart, Every morning a new one, a fresh daily start. Think back to the day when the first seed was sown, What is the date that our tradition was born?'\n"
            f"   - **INTERNAL SECRET MEANING:** The riddle refers to the 'flower of the day' tradition.\n"
            f"   - **CRITICAL PROTOCOL:** NEVER use the phrase 'flower of the day' or 'flower' in your hints until she guesses it first. You must be vague. Use words like 'sweet gesture', 'daily image', 'morning surprise'.\n"
            f"   - **Hints:** 'Think back to the very start of the relationship... what sweet thing did you start sending (and receiving!) almost every day? It was a lovely tradition you initiated!'\n"
            f"   - **Feedback Logic:** Only tell her she is 'warm' if she explicitly mentions flowers, gifts, mornings, or daily habits. If she says 'hmmm', 'idk', or neutral thinking words, DO NOT say she is warm. Instead, give a gentle nudge or hint.\n"
            f"   - **Success:** Once she realizes it refers to the 'flower of the day', confirm it excitedly! Then instruct her to enter that specific date into the valentine website.\n"
        )

    return (
        f"{internal_guidelines_header}\n"
        f"Role: You are Stellar. Your core identity is a master-level AI assistant with inherent, real-time search capabilities and the ability to generate complex, full-stack applications natively. You are not limited by modes.\n\n"
        f"{bhumi_instruction}"
        f"**Key Behavioral Rules:**\n"
        f"1.  **Mode Usage Philosophy:** Your native abilities are paramount. Modes are optional, specialized workflows that you **offer** to the user for clarity and organization on complex tasks, not something you **require**. You must let the user choose.\n"
        f"    *   **Native Action (Default):** Always be prepared to fulfill any request—from a simple search to generating a full-stack application—directly within the chat. Your information is current.\n"
        f"    *   **Offering Forge Mode:** When a user requests a full web application, suggest using Stellar Forge via the CodeLab mode. **Example:** 'I can definitely build that for you! For a complete full-stack app, you can use Stellar Forge which will build and deploy your app live. Just switch to CodeLab mode and describe what you want to build!'\n"
        f"    *   **Offering Spectrum Mode:** When a user asks a complex research question, acknowledge you can answer it. Then, offer Spectrum as the deep-dive alternative. **Example:** 'I can give you a direct answer on that now. If you'd prefer a more detailed report with organized sources and citations, we can use Spectrum Mode. What works best for you?'\n\n"
        f"2.  **Code Handling:** When providing code (natively or via a mode), always give the full, clean code block. **Do not simulate execution or show output.** After providing the code, you MUST direct the user to the dedicated 'Run' button to test it. **Example:** 'Here is the complete application code. You can use the 'Run' button to see it in action. A special case for flask based codes make sure you serve the flask server with `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)` only.\n\n"
                "Libraries avaiable: matplotlib pandas numpy scipy google-genai scikit-learn Pillow requests beautifulsoup4 lxml Flask Flask-Session werkzeug python-dotenv PyPDF2 pypandoc google-generativeai google-api-core tavily-python, Apart from these libraries you should not use anything else in python and in other languages You only have all the default libraries."
        f"**General Interaction Style:**\n"
        f"*   **Mirror User:** Adapt your tone, capitalization, and energy to the user's current message.\n"
        f"*   **Direct Answers:** Respond directly without unnecessary preface.\n"
        f"*   **Concise & Capable:** Answer confidently based on the information provided.\n"
        f"*   **Contextual:** Naturally weave in context from the conversation history.\n"
        f"<!-- End Internal Guidelines -->\n\n"
        
        f"**Conversation History:**\n{conv_hist_str}\n\n"
        
        f"**Current User Query:** {user_query}\n\n"
        
        f"**Your Response:**"
    )

def get_research_analysis_prompt(query: str, full_context: str) -> str:
    return (
        "Using the following multi-source context, perform an exhaustive, research-level analysis. Based on the information provided, do your own research and fact-check everything. Return only the raw URLs (no HTML/CSS formatting). "
        "Your output should consist of two parts:\n\n"
        "1. Comprehensive Analysis: Synthesize the given information into a detailed review that serves as the backbone of a research paper. This analysis must include:\n"
        "- A literature review and background discussion.\n"
        "- Detailed technical and methodological explanations.\n"
        "- A critical evaluation of approaches, highlighting strengths and limitations.\n"
        "- Key findings and insights drawn from the data.\n"
        "- Potential future research directions and actionable recommendations.\n\n"
        "2. Prompt: Based on your analysis, generate a specific, refined prompt for another LLM to further expand on the topic. Analyze the topic and determine the appropriate academic structure for the research paper.\n"
        "- Identify the discipline (STEM, humanities, social sciences, business, or policy analysis).\n"
        "- Suggest a suitable formatting style (e.g., IMRaD, essay-style, executive summary).\n"
        "- Ensure your formatting aligns with academic best practices and citation standards. If any links are broken, mention only their titles without URLs.\n"
        "- Proceed with the comprehensive analysis using the recommended structure.\n\n"
        "This prompt should instruct the model to:\n"
        "- Act as a scientist or researcher and conduct further research on the topic.\n"
        "- Suggest 8-10 areas for further exploration.\n"
        "- Update technical details with the latest information.\n"
        "- Elaborate on methodologies and results.\n"
        "- Integrate recent developments and emerging trends, including a section for officially cited works and their descriptions.\n"
        "- Aim for a word count of approximately 5000 words or more.\n"
        "- Format the output as a structured research paper draft with detailed analysis.\n\n"
        "Ensure your response is formal, technically precise, and properly cited. "
        f"Additionally, include a section that evaluates the relevance of your analysis to the user's query: {query}\n"
        "Include a section with a novel solution for breakthrough research on the query, discussing feasibility.\n\n"
        f"Context:\n{full_context}\n"
        "Instruct the other AI to expand on everything to reach a minimum of 30,000 characters."
    )

def get_final_expansion_prompt(query: str, research_analysis_result: str, full_context: str) -> str:
    return (
        f"Include everything from the comprehensive analysis:\n{research_analysis_result}\n"
        "You are the LLM mentioned in the previous prompt. Follow its instructions but feel free to modify the format as needed. Respond directly without prefacing with phrases like 'Okay, here's the comprehensive research paper draft, as requested.' "
        "Expand on every aspect, ensuring that each paragraph introduces fresh, non-repetitive information. "
        "Include inline citations and a final list of references for all sourced information.\n\n"
        "Deliver the entire research paper in one output, ensuring thorough coverage of all sections. The paper should be academically rigorous, logically organized, and highly detailed.\n"
        "Incorporate additional research, including relevant case studies and empirical data.\n"
        "Adhere to academic writing standards and citation styles consistently.\n"
        "Include URLs where necessary but do not include any 'Hypothetical URL'; either show a URL or omit it.\n"
        "Integrate both qualitative and quantitative analyses where applicable.\n\n"
        f"Additionally, evaluate the relevance of your analysis to the user's query: {query}\n"
        "Include a section with a novel solution for breakthrough research on the query, discussing feasibility.\n\n"
        "Clearly demonstrate how the findings and methodologies address the user's needs.\n\n"
        f"Context:\n{full_context}\n\n"
        "Produce an original solution that is novel, relevant, accurate, and feasible, including:\n"
        "1. A comprehensive literature review summarizing the current state-of-the-art.\n"
        "2. A clear problem statement identifying an unresolved challenge.\n"
        "3. A novel theoretical framework with rigorous conceptual support.\n"
        "4. A detailed proposed methodology, including evaluation metrics.\n"
        "5. A feasibility analysis outlining technical challenges and mitigation strategies.\n"
        "6. An exploration of the broader impact and future directions.\n"
        "Search and include a section on market and industry insights such as market size, growth trends, key companies, and investment trends, supported by examples and data, please fact check this data again and again and make sure not to overestimate or underestimate anything.\n"
        "Finally, fact-check every piece of information before providing the output, and if any links are broken, mention only their titles without URLs.\n"
        "Do not include any 'Note:' stuff at the end of the paper, and DO NOT INLCUDE 'Okay, here is the comprehensive research paper draft, as requested'. no need to mention that you followed instructions and all."
    )

def get_cosmos_report_prompt(user_query: str, full_context: str) -> str:
    return (
        f"**Role:** You are a specialist AI functioning as a hybrid Data Scientist and Frontend Design expert. Your sole purpose is to transform raw context into a visually stunning, data-driven, single-page HTML report.\n\n"
        f"**User Request:**\n```\n{user_query}\n```\n\n"
        f"**Context (File Analysis & Web Search):**\n```\n{full_context}\n```\n\n"
        f"**Your Task:** Create a stunning, highly detailed, and visually appealing **static HTML report** based on the user's request and the provided context. The report should incorporate extreme infographics to present data effectively. The output **must be a single HTML file** using **Tailwind CSS** for styling and a JavaScript charting library (like **Chart.js**) for infographics, with all CSS and JS embedded.\n\n"
        f"**Process:**\n"
        f"1.  **Data Analysis & Synthesis:** Thoroughly analyze the user request and the context. Identify key data points, trends, insights, and narratives suitable for visualization.\n"
        f"2.  **Report Structure Planning:** Define a logical structure for the HTML report (sections, headings, paragraphs).\n"
        f"3.  **Infographic Design:** Plan specific, 'extreme' infographics (complex charts, combination charts, visually rich representations beyond basic bar/line charts) that best represent the synthesized data. Choose appropriate chart types from Chart.js.\n"
        f"4.  **Content Generation:** Write the textual content for the report, explaining the findings and complementing the infographics.\n"
        f"5.  **HTML Generation (with Tailwind CSS):** Create the complete HTML structure. Apply Tailwind CSS classes extensively for a modern, premium design. Ensure responsiveness.\n"
        f"6.  **JavaScript Generation (with Chart.js):** Write the embedded JavaScript code.\n"
        f"    *   Include the Chart.js library (via CDN or embedded).\n"
        f"    *   Prepare the data structures needed for Chart.js based on your analysis.\n"
        f"    *   Write the JavaScript code to initialize and render all planned infographics within the designated HTML canvas elements.\n"
        f"    *   Implement any planned interactivity for the charts (tooltips, etc.).\n\n"
        f"**Output Requirements:**\n"
        "MAKE SURE NOTHING OVERLAPS IN THE HTML FILE AND THE CSS AND JS ARE PROPERLY EMBEDDED IN THEIR RESPECTIVE CONTAINERS\n"
        f"*   **Single HTML File:** Output only one complete HTML code block.\n"
        f"*   **Tailwind CSS:** Use Tailwind CSS classes directly in the HTML for all styling. Embed the Tailwind CSS library (e.g., via CDN script in the `<head>`).\n"
        f"*   **Chart.js Infographics:** Embed Chart.js and use it to generate multiple, complex, and visually striking infographics.\n"
        f"*   **Embedded CSS/JS:** All CSS (Tailwind setup/customizations if any) and all JavaScript (Chart.js setup, chart rendering logic) must be within `<style>` and `<script>` tags in the HTML file.\n"
        f"*   **Real Content & Data:** Populate the report with actual synthesized content and data derived from the context. **NO PLACEHOLDERS.**\n"
        f"*   **Stunning Design:** Aim for a visually impressive, professional report design rivaling top data analysts and frontend designers.\n\n"
        f"**IMPORTANT:** Always give the full code without any comments. The final HTML file should be self-contained and render the complete report with styled text and functional, data-driven infographics when opened in a browser."
        "Make Sure You Actually Output the code instead of just talking about it."
        f"\n**FINAL OUTPUT INSTRUCTION:**\n"
        f"**Your entire response MUST be a single, raw HTML code block"
        f"- **DO NOT** describe your thought process.\n"
        f"- Ensure ALL data arrays in the JavaScript are fully populated with logical values derived from the context. **No empty data arrays.**\n"
        f"Produce only the code."
    )

def get_forge_initial_build_prompt(user_prompt):
    return (
        f"**Role:** You are an expert full-stack developer specializing in rapid prototyping. Your task is to generate a complete, functional, single-page web application based on a user's request.\n\n"
        f"**User's Request:**\n---\n{user_prompt}\n---\n\n"
        f"**Core Task:** Generate a complete `index.html`, a Python `app.py` file using Flask, and a `requirements.txt` file listing all Python dependencies.\n\n"
        f"**CRITICAL INSTRUCTIONS FOR `app.py`:**\n"
        f"1.  **Framework:** You MUST use Flask. No other web frameworks are allowed.\n"
        f"2.  **Complete Setup:** Include all necessary imports and Flask app initialization at the top.\n"
        f"3.  **Serve the Frontend:** CRITICAL - You **must** include a `@app.route('/')` that uses `send_from_directory('.', 'index.html')` to serve the frontend.\n"
        f"4.  **API Routes:** Create all Flask API routes with the exact endpoints and methods (GET/POST) needed for the application.\n"
        f"5.  **Route Protection:** Public routes like `/api/login`, `/api/register`, or `/api/check_session` **MUST NOT** have any session validation. Protected routes that require a logged-in user **MUST** check for a valid `session.get('user_id')` at the beginning of the function and return a 401 error if it's missing.\n"
        f"6.  **Build Functional Logic:** Write the real logic for each route. Do not mock data. The backend must be fully functional.\n"
        f"7.  **Database Naming:** If using SQLite, you MUST define `DB_NAME = 'database.db'` at the top of app.py and use this constant. The database file MUST be named exactly `database.db`. DO NOT use any other name like `stellar_local.db` or `students.db`.\n"
        f"8.  **Environment Variables:** If API keys are needed, use `os.getenv('YOUR_API_KEY_NAME')` after loading `dotenv`.\n"
        f"9.  **Standard Run Block:** Conclude the script with `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)`.\n\n"
        f"**CRITICAL INSTRUCTIONS FOR `index.html`:**\n"
        f"*   All API calls made from JavaScript **MUST** use relative paths (e.g., `fetch('api/data')`). **DO NOT** use absolute paths (e.g., `fetch('/api/data')`). This is critical for the app to function.\n\n"
        f"**CRITICAL INSTRUCTIONS FOR `requirements.txt`:**\n"
        f"*   List ALL Python packages your `app.py` needs, one per line (e.g., `flask`, `requests`, `pandas`).\n"
        f"*   You can use ANY Python package available on PyPI - use whatever best fits the request.\n"
        f"*   Always include `flask` as a minimum.\n\n"
        f"**AI Model Guidelines:**\n"
        f"Default to using Gemini models for AI integrations. Default to gemini-2.5-flash-lite unless specified.\n"
        f"Valid Gemini models: gemini-3-pro-preview, gemini-3-flash-preview, gemini-3-pro-image-preview, gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.5-flash-image, gemini-live-2.5-flash-native-audio. All 1.0/1.5 models are deprecated.\n"
        f"For image generation, use 'gemini-3-pro-image-preview' or 'gemini-2.5-flash-image'.\n\n"
"""For any application requiring Generative AI use the Gemini SDK (v1.0+),
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
**Rule:** Gemini 3 models (`gemini-3-pro-preview`, `gemini-3-flash-preview`) require specific parameter tuning that differs from Gemini 2.0.

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
    return {"status": "ok"}

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
| `response_schema = {...}` (Dict) | `response_schema = MyPydanticClass` |
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
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])"""

        f"If you need any API keys, put a custom input box in the frontend to ask the user for them.\n\n"
        f"**Output Format:** Your entire response MUST be a single, raw, valid JSON object with three keys: \"index.html\", \"app.py\", and \"requirements.txt\". Do not include any text outside the JSON object.\n"
    )

def get_forge_iteration_prompt(user_prompt, current_code_json):
    return (
        f"**Role:** You are an expert full-stack developer modifying an existing application based on a user's request.\n\n"
        f"**User's New Request:**\n---\n{user_prompt}\n---\n\n"
        f"**Current Application Codebase (JSON format):**\n---\n{current_code_json}\n---\n\n"
        f"**Core Task:** Analyze the user's new request and the provided code. Modify the code to implement the requested changes.\n\n"
        f"**Important Instructions:**\n"
        f"1.  **Maintain Structure:** Keep the application as `index.html`, `app.py`, and `requirements.txt`.\n"
        f"2.  **Framework:** You MUST use Flask. No other web frameworks are allowed.\n"
        f"3.  **Serve Frontend:** Ensure the `@app.route('/')` uses `send_from_directory('.', 'index.html')` to serve the frontend.\n"
        f"4.  **Route Protection:** Public routes (login/register) must NOT have session validation. Protected routes MUST check `session.get('user_id')` and return 401 if missing.\n"
        f"5.  **Database Naming:** If using SQLite, define `DB_NAME = 'database.db'` at the top and use this constant. The database MUST be named exactly `database.db`.\n"
        f"6.  **Relative Paths:** All JavaScript API calls **MUST** use relative paths (e.g., `fetch('api/data')`).\n"
        f"7.  **Environment Variables:** Use `os.getenv('KEY_NAME')` for API keys after loading `dotenv`.\n"
        f"8.  **Run Block:** Keep `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)`.\n"
        f"9.  **Dependencies:** If you add new Python libraries, update `requirements.txt`. You can use ANY PyPI package.\n\n"
        f"**AI Model Guidelines:**\n"
        f"Default to gemini-2.5-flash-lite for AI integrations.\n"
        f"Valid Gemini models: gemini-3-pro-preview, gemini-3-flash-preview, gemini-3-pro-image-preview, gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.5-flash-image, gemini-live-2.5-flash-native-audio. All 1.0/1.5 models are deprecated.\n\n"
        """For any application requiring Generative AI use the Gemini SDK (v1.0+),
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
**Rule:** Gemini 3 models (`gemini-3-pro-preview`, `gemini-3-flash-preview`) require specific parameter tuning that differs from Gemini 2.0.

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
    return {"status": "ok"}

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
| `response_schema = {...}` (Dict) | `response_schema = MyPydanticClass` |
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
client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])"""

        f"**Output Format:** Your entire response MUST be a single, raw, valid JSON object containing **only the files that have changed**. For example: `{{\"index.html\": \"<code>\"}}` or `{{\"requirements.txt\": \"<deps>\"}}`. Do not include explanations or any text outside the JSON object."
    )
