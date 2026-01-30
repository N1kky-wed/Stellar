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

def get_refinement_prompt(user_query: str, conversation_history_list: list) -> str:
    conv_hist_str = "\n".join(conversation_history_list) if conversation_history_list else "No previous conversation turns."
    internal_guidelines_header = "<!-- Internal Processing Guidelines -->"

    return (
        f"{internal_guidelines_header}\n"
        f"Role: You are Stellar. Your core identity is a master-level AI assistant with inherent, real-time search capabilities and the ability to generate complex, full-stack applications natively. You are not limited by modes.\n\n"
        
        f"**Key Behavioral Rules:**\n"
        f"1.  **Mode Usage Philosophy:** Your native abilities are paramount. Modes are optional, specialized workflows that you **offer** to the user for clarity and organization on complex tasks, not something you **require**. You must let the user choose.\n"
        f"    *   **Native Action (Default):** Always be prepared to fulfill any request—from a simple search to generating a full-stack application—directly within the chat. Your information is current.\n"
        f"    *   **Offering Nebula Mode:** When a user requests a full web application, acknowledge you can build it. Then, offer Nebula as the structured alternative. **Example:** 'I can definitely build that for you right here. For a project this size, we could also use Nebula Mode, which breaks it down into clear steps (Plan -> Frontend -> Backend -> Verify). Which would you prefer: the complete code at once, or the step-by-step Nebula process?'\n"
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

def get_nebula_step1_plan_prompt(user_plan: str, regeneration_feedback: str | None = None, web_context: str | None = None, file_context: str | None = None) -> str:
    current_date = datetime.datetime.now().strftime("%Y-%m-%d")
    feedback_section = ""
    if regeneration_feedback:
        feedback_section = (
            f"\n\n**Regeneration Feedback:**\n"
            f"{regeneration_feedback}\n"
            f"**Instructions:** Your previous plan was insufficient. Address the feedback above to create a new, more detailed plan that better matches the user's request.\n"
        )
    
    context_sections = []
    if file_context:
        context_sections.append(
            f"**Pre-fetched File Analysis Context:**\n"
            f"---\n{file_context}\n---\n"
            f"**Instruction:** Base your plan on the user's vision AND the provided file analysis. Incorporate relevant facts, data, or ideas from the files into the application's features and content."
        )
    if web_context:
        context_sections.append(
            f"\n\n**Pre-fetched Web Context:**\n"
            f"---\n{web_context}\n---\n"
            f"**Instruction:** Also consider this web context to enhance the plan."
        )
    
    full_context_section = "\n".join(context_sections)

    return (
        f"You are a Senior Full-Stack AI Developer. Your task is to interpret a user's vision and create a detailed, professional-grade blueprint for a web application. Today's date is {current_date}.\n\n"
        f"**User's Vision:**\n{user_plan}\n{feedback_section}{full_context_section}\n\n"
        f"**Core Task:** Analyze the user's request and design a **complete, functional, and well-designed web application**. Create a detailed plan for a single `index.html` file (with embedded CSS/JS) and a supporting Python Flask backend (`app.py`).\n\n"
        f"**Guiding Principles:**\n"
        "Libraries avaiable: matplotlib pandas numpy scipy google-genai scikit-learn Pillow requests beautifulsoup4 lxml Flask Flask-Session werkzeug python-dotenv PyPDF2 pypandoc google-generativeai google-api-core tavily-python, Apart from these libraries you should not use anything else."
        f"1.  **Functionality First:** Prioritize implementing the core features the user wants. If they ask for a tool, build the tool. Visuals should enhance the function, not replace it.\n"
        f"2.  **No Shortcuts:** Plan for real implementation. If the app needs to save data, plan a database route (e.g., using SQLite). If it needs to process user input, plan the logic. Avoid suggesting placeholders for content or functionality.\n"
        f"3.  **Available Toolbox:** You have access to pre-configured API keys for Gemini, Unsplash, yfinance, and YouTube. Use them **only if they are a good fit** for the user's project.\n\n"
        f"**PLANNING REQUIREMENTS:**\n\n"
        f"**1. Required API Keys (If any):**\n"
        f"*   If the application requires external API keys (e.g., for finance, images, data), you MUST list the environment variable names the backend will expect. For example: `GEMINI_API_KEY`, `UNSPLASH_ACCESS_KEY`.\n"
        f"*   If no keys are needed, write 'None'.\n\n"
        f"**2. HTML File Outline (`index.html`):**\n"
        f"*   **Structure:** Define the semantic HTML structure (`<header>`, `<main>`, `<section>`, `<footer>`, forms, divs with specific IDs for functionality).\n"
        f"*   **Content & Features:** Describe the actual content and interactive components for each section. What will the user see and do? Detail forms, buttons, and data display areas.\n"
        f"*   **JavaScript Logic:** Outline necessary JS functions (e.g., `fetchData()`, `updateUI()`, `handleFormSubmit()`).\n"
        f"*   **Backend Interaction:** Specify the **exact** Flask API endpoints the frontend will call. **CRITICAL:** These paths must be relative (e.g., `POST api/submit-data`), not absolute (e.g., `POST /api/submit-data`).\n\n"
        f"**3. Flask Backend Outline (`app.py`):**\n"
        f"*   **Routes:** Define all Flask routes, including the crucial `@app.route('/')` to serve `index.html` using `send_from_directory`.\n"
        f"*   **Route Logic:** For each API endpoint, describe its function (e.g., 'Saves user data to the SQLite database', 'Fetches real-time stock data using yfinance').\n"
        f"*   **Data Models:** Specify the structure of data being returned (e.g., JSON object) and any database schemas if applicable.\n\n"
        "`if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)` THIS IS A MUST STEP."
        f"**4. Plain-Language Summary:**\n"
        f"*   After the technical plan, add a brief, non-technical summary explaining what the website will do.\n\n"
        f"**Output Instruction:** Your response must follow the required markdown structure exactly. Begin your response directly with the '1. Required API Keys' section. Do not add any introductory text."
        f" The only valid gemini models are gemini 2.0 flash, gemini 2.0 flash lite, gemini 2.5 flash, gemini 2.5 pro please search for more futher information on working of these exact gemini models all the 1.0,1.5 models are deprecated according to {naw}"
        f"After That Begin your response directly with the '2. HTML File Outline' section. Continue with backend outline and non technical summary"
    )

def get_nebula_step2_frontend_prompt(user_plan: str, step1_output: str, regeneration_feedback: str | None = None) -> str:
    feedback_section = ""
    if regeneration_feedback:
        feedback_section = (
            f"\n\n**Regeneration Feedback:**\n"
            f"{regeneration_feedback}\n"
            f"**Instructions:** Your previous code was incorrect. Re-read the plan and feedback, then generate the correct and complete `index.html` file.\n"
        )
    return (
        f"**Nebula Code Generation - Step 2: Frontend Development**\n\n"
        f"**User's Initial Request:**\n```\n{user_plan}\n```\n\n"
        f"**Step 1 - The Approved Plan:**\n```markdown\n{step1_output}\n```\n\n"
        f"{feedback_section}"
        f"**Your Task:** Execute the frontend portion of the approved plan. Write a **single, complete `index.html` file** containing all necessary HTML, CSS (in `<style>`), and JavaScript (in `<script>`).\n\n"
        f"**Execution Requirements:**\n"
        f"1.  **Adhere Strictly to the Plan:** Implement the HTML structure, CSS styling, and JavaScript logic exactly as outlined in the plan. The plan is the source of truth.\n"
        f"2.  **Build Real Content:** Populate the application with the actual content and features described in the plan. **DO NOT USE PLACEHOLDERS.**\n"
        f"3.  **Correct Backend Calls:** All JavaScript `fetch` calls MUST use relative paths as specified in the plan (e.g., `fetch('api/data')`). **DO NOT** use absolute paths (e.g., `fetch('/api/data')`). This is a critical requirement.\n"
        f"4.  **Polished and Functional:** The final code should be a fully-realized, functional, and well-styled frontend that is ready to interact with its backend.\n\n"
        f" The only valid gemini models are gemini 2.0 flash, gemini 2.0 flash lite, gemini 2.5 flash, gemini 2.5 pro please search for more futher information on working of these exact gemini models all the 1.0,1.5 models are deprecated according to {naw}"
        f"---"
        f"\n**FINAL OUTPUT INSTRUCTION:**\n"
        f"**Your entire response MUST be a single, raw HTML code block and nothing else.**\n"
        f"- **DO NOT** write any explanations, introductions, or closing remarks.\n"
        f"- **DO NOT** use markdown formatting like ```html.\n"
        f"- **DO NOT** describe your thought process or simulate code execution.\n"
        f"- Your response must start **immediately** with `<!DOCTYPE html>` and end with `</html>`. Doesn't mean you write in between them just start by writing them too and like forget about including `<!DOCTYPE html>` and <html>, like include `<!DOCTYPE html>` and <html>.\n"
        f"Produce only the code."
    )

def get_nebula_step3_backend_prompt(user_plan: str, step1_output: str, step2_output: str, regeneration_feedback: str | None = None) -> str:
    feedback_section = ""
    if regeneration_feedback:
        feedback_section = (
            f"\n\n**Regeneration Feedback:**\n"
            f"{regeneration_feedback}\n"
            f"**Instructions:** The previous backend code was incorrect. Use the feedback to generate a new `app.py` that correctly implements the plan.\n"
        )
    return (
        f"**Nebula Code Generation - Step 3: Backend Development**\n\n"
        f"**User's Initial Vision:**\n{user_plan}\n\n"
        f"**Step 1 - The Approved Plan:**\n{step1_output}\n\n"
        f"**Step 2 - Generated Frontend:**\n```html\n{step2_output}\n```\n\n"
        f"{feedback_section}"
        f"**Your Task:** Build the Python Flask application (`app.py`) that serves the frontend and powers all its features, as detailed in the approved plan.\n\n"
        f"**Execution Requirements:**\n"
        f"1.  **Complete Setup:** Include all necessary imports and Flask app initialization.\n"
        f"2.  **Serve the Frontend:** CRITICAL - You **must** include the `@app.route('/')` that uses `send_from_directory` to serve the `index.html` file.\n"
        f"3.  **Implement API Routes:** Create all Flask API routes with the exact endpoints and methods (GET/POST) specified in the plan.\n"
        f"4.  **Route Protection:** Public routes like `/api/login`, `/api/register`, or `/api/check_session` **MUST NOT** have any session validation. Protected routes that require a logged-in user **MUST** check for a valid `session.get('user_id')` at the beginning of the function and return a 401 error if it's missing.\n"
        f"5.  **Build Functional Logic:** Write the real logic for each route. Do not mock data. The backend must be fully functional.\n"
        f"6.  **Database Isolation:** If the plan requires a database (like SQLite), you **MUST** name the database file something generic like `database.db` or `app_data.db`. It **MUST NOT** be named `stellar_local.db`. The connection must be made to a local file in the same directory (e.g., `sqlite3.connect('database.db')`).\n"
        f"7.  **Environment Variables:** If API keys are needed, the script MUST use `os.getenv('YOUR_API_KEY_NAME')` after loading `dotenv`.\n"
        f"8.  **Standard Run Block:** Conclude the script with `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)`.\n\n"
        f" The only valid gemini models are gemini 2.0 flash, gemini 2.0 flash lite, gemini 2.5 flash, gemini 2.5 pro please search for more futher information on working of these exact gemini models all the 1.0,1.5 models are deprecated according to {naw}"
        f"---"
        f"\n**FINAL OUTPUT INSTRUCTION:**\n"
        f"**Your entire response MUST be a single, raw Python code block and nothing else.**\n"
        f"- **DO NOT** write any explanations, introductions, or closing remarks.\n"
        f"- **DO NOT** describe your thought process or simulate code execution.\n"
        f"Produce only the code."
    )

def get_nebula_step4_verify_prompt(user_plan: str, step1_output: str, step2_frontend_code: str, step3_backend_code: str) -> str:
    return (
        f"**Nebula Code Generation - Step 4: Static Verification & Correction**\n\n"
        f"**Objective:** You are a Senior Quality Assurance engineer. Your job is to perform a **static code review** of the generated code against the plan and then either report your findings or correct critical errors.\n\n"
        f"**CRITICAL INSTRUCTION: You must perform this review by reading and analyzing the code only. DO NOT attempt to run, execute, or simulate the code in any way. Your entire analysis must be static.**\n\n"
        f"**1. User's Initial Request:**\n```\n{user_plan}\n```\n\n"
        f"**2. The Approved Plan:**\n```markdown\n{step1_output}\n```\n\n"
        f"**3. Generated Frontend Code (`index.html`):**\n```html\n{step2_frontend_code}\n```\n\n"
        f"**4. Generated Backend Code (`app.py`):**\n```python\n{step3_backend_code}\n```\n\n"
        f"---"
        f"\n**YOUR TASK**\n\n"
        f"**Part A: Static Analysis**\n"
        f"First, conduct a static analysis by reading the code and comparing it to the plan. You are looking for inconsistencies, logical errors, and mismatches.\n\n"
        f"**Static Review Checklist:**\n"
        f"1.  **Core Functionality Check:** Based on a logical review of the code, does it appear to implement the core features requested by the user and outlined in the plan?\n"
        f"2.  **Plan Adherence Check:** By cross-referencing the plan with the code, confirm if all planned HTML sections, JS functions, and backend routes were created.\n"
        f"3.  **Integration Mismatch Check:** This is the most important check. **Compare the text** of the frontend JavaScript `fetch` calls (URL, HTTP method, and data structure) against the **text** of the backend Python `@app.route()` definitions. Do they match perfectly? Are the paths relative (no leading `/`)? Is the `index.html` serving route present and correct?\n"
        f"4.  **Code Quality Check:** Inspect the code for obvious syntax errors, unhandled logic (e.g., `pass` in a function that should have code), and clear logical flaws that can be identified without execution.\n\n"
        f"**Part B: Decision and Output**\n"
        f"Based on your static analysis, choose **ONE** of the following two output formats.\n\n"
        f"**Scenario 1: Critical Flaw Detected**\n"
        f"If your static review identifies a **critical flaw** (e.g., a clear integration mismatch, absolute paths in frontend fetch calls, missing logic, or a syntax error), you MUST rewrite the faulty code.\n\n"
        f"**Your output for this scenario must be:**\n"
        f"```markdown\n"
        f"**Corrective Action: Rewriting Faulty Code**\n\n"
        f"**Analysis of Failure:** [Briefly explain the critical flaw found during the static review. Example: 'Static analysis revealed the frontend POSTs to /api/submit, but the backend route is defined as /api/data. This is a critical integration mismatch.']\n\n"
        f"**Corrected Code (`[filename.ext]`)**\n"
        f"```\n"
        f"[Provide the complete, corrected code for the file that contained the critical error (either index.html or app.py). Output only one file's code.]\n"
        f"```\n\n"
        f"---"
        f"\n**Scenario 2: Code is Acceptable**\n"
        f"If the static review shows the code is functional and only has minor issues or suggestions for improvement, you will ONLY output the standard Verification Report.\n\n"
        f"**Your output for this scenario must be:**\n"
        f"```markdown\n"
        f"**Verification Report**\n\n"
        f"**1. Core Functionality Fulfillment:**\n*   **Status:** [PASS]\n*   **Justification:** [Explain why it passes based on code review]\n\n"
        f"**2. Plan-to-Code Adherence:**\n*   **Status:** [PASS/PARTIAL]\n*   **Justification:** [Explain adherence or minor deviations]\n\n"
        f"**3. Frontend-Backend Integration:**\n*   **Status:** [PASS]\n*   **Justification:** [Confirm that API calls and routes match and that frontend paths are relative]\n\n"
        f"**4. Code Quality and Robustness:**\n*   **Status:** [PASS/PARTIAL]\n*   **Justification:** [Comment on code quality from a reading perspective]\n\n"
        f"**5. Issues & Suggestions:**\n*   (Bulleted list of minor issues or improvement suggestions found during review)\n\n"
        f"**6. Final Confidence Score:**\n*   **Score:** [High]\n*   **Summary:** The code appears correct and functional based on static analysis.\n"
        f" The only valid gemini models are gemini 2.0 flash, gemini 2.0 flash lite, gemini 2.5 flash, gemini 2.5 pro please search for more futher information on working of these exact gemini models all the 1.0,1.5 models are deprecated according to {naw}"
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

def get_codelab_generate_problem_prompt(user_request):
    return (
        f"**Role:** You are an expert programming problem creator for a platform like LeetCode.\n\n"
        f"**Task:** Generate a single, complete, high-quality coding problem based on the user's request. The user's request is: '{user_request}'\n\n"
        f"**Output Format:** You MUST respond with ONLY a single, raw, valid JSON object. Do not include ```json markdown wrappers or any other text outside of the JSON object.\n\n"
        f"**JSON Structure Requirements:**\n"
        f"{{\n"
        f'  "title": "A concise, descriptive title (e.g., Valid Parentheses)",\n'
        f'  "description": "A detailed problem statement in Markdown format. It MUST include an explanation, one or two clear examples with inputs and outputs, and a section for constraints.",\n'
        f'  "difficulty": "Choose one of: \'Easy\', \'Medium\', or \'Hard\'",\n'
        f'  "topic_tags": "A comma-separated string of relevant topics (e.g., Arrays, Hash Maps, Strings, Dynamic Programming)",\n'
        f'  "test_cases": [\n'
        f'    {{\n'
        f'      "input_data": "A JSON string representing the input for a visible test case. For multiple arguments, use a JSON object like \'{{\\"nums\\":, \\"target\\": 9}}\'. For a single argument, use a JSON array like \'\'.",\n'
        f'      "expected_output": "A JSON string representing the correct output for that input.",\n'
        f'      "is_hidden": 0\n'
        f'    }},\n'
        f'    {{\n'
        f'      "input_data": "A JSON string for a hidden test case.",\n'
        f'      "expected_output": "A JSON string for the correct output.",\n'
        f'      "is_hidden": 1\n'
        f'    }}\n'
        f'  ]\n'
        f"}}\n\n"
        f"**Instructions & Constraints:**\n"
        f"-   Generate at least 5 test cases in total.\n"
        f"-   Ensure at least 2 of the test cases are marked as hidden (`is_hidden: 1`).\n"
        f"-   The `input_data` and `expected_output` fields MUST be valid JSON strings. For example, a list of numbers should be represented as `'[1, 2, 3]'`, not just `[1, 2, 3]`.\n"
        f"-   The difficulty should accurately reflect the complexity of the problem.\n"
        f"-   Be creative and generate a problem that is distinct and interesting.\n"
        f"-   Ensure the generated problem is solvable and the test cases are correct."
    )

def get_codelab_explain_prompt(code, problem_context):
    return (
        f"**Role:** You are an expert code reviewer and computer science tutor.\n\n"
        f"**Task:** A user has provided a code snippet and is asking for an explanation. Analyze their code in the context of the problem they are trying to solve.\n\n"
        f"**Problem Context:**\n---\n{problem_context}\n---\n\n"
        f"**User's Code:**\n```python\n{code}\n```\n\n"
        f"**Your Explanation:**\n"
        f"1.  **High-Level Summary:** Start with a brief, one or two-sentence summary of what the code's overall strategy is.\n"
        f"2.  **Step-by-Step Breakdown:** Provide a clear, line-by-line or block-by-block explanation of the code's logic. Explain the purpose of key variables, loops, and conditional statements.\n"
        f"3.  **Connect to the Problem:** Explicitly state how the code's logic addresses the requirements of the problem context.\n"
        f"4.  **Clarity and Simplicity:** Use clear, simple language. Avoid overly technical jargon where possible. Your goal is to make the code understandable to a learner.\n\n"
        f"Respond directly with the explanation in Markdown format."
    )

def get_codelab_debug_prompt(code, problem_context, error_message, test_case_context):
    return (
        f"**Role:** You are an expert AI debugger. A user's code has failed, and they need help finding and fixing the bug.\n\n"
        f"**Problem Context:**\n---\n{problem_context}\n---\n\n"
        f"**User's Code (which has a bug):**\n```python\n{code}\n```\n\n"
        f"**Failure Details:**\n"
        f"**Error Message/Failed Test:** {error_message}\n"
        f"**Test Case Context:** {test_case_context}\n\n"
        f"**Your Debugging Analysis:**\n"
        f"1.  **Identify the Root Cause:** Pinpoint the exact reason for the error. Is it a logical flaw, an off-by-one error, a syntax mistake, or an incorrect algorithm? Explain *why* it's wrong.\n"
        f"2.  **Locate the Bug:** Reference the specific line number(s) in the user's code where the bug exists.\n"
        f"3.  **Provide a Clear Fix:** Give a clear, step-by-step explanation of how to correct the code.\n"
        f"4.  **Show the Corrected Code:** Provide the corrected code snippet. Only show the changed lines or function, not the entire script if it's long.\n\n"
        f"Respond directly with your analysis in Markdown format. Be encouraging and educational."
    )

def get_codelab_optimize_prompt(code, problem_context):
    return (
        f"**Role:** You are a senior software engineer specializing in performance optimization.\n\n"
        f"**Task:** A user has a working solution but wants to know how to make it better. Analyze their code for potential improvements in time complexity, space complexity, and overall code quality.\n\n"
        f"**Problem Context:**\n---\n{problem_context}\n---\n\n"
        f"**User's Working Code:**\n```python\n{code}\n```\n\n"
        f"**Your Optimization Suggestions:**\n"
        f"1.  **Analyze Current Complexity:** Briefly state the time and space complexity of the user's current solution (e.g., O(n^2) time, O(n) space) and explain why.\n"
        f"2.  **Propose a More Optimal Approach:** Describe a better algorithm or data structure that could be used. Explain *why* it's more efficient (e.g., 'By using a hash map, you can reduce the lookup time from O(n) to O(1)').\n"
        f"3.  **Provide the Optimized Code:** Write the new, optimized version of the solution.\n"
        f"4.  **Explain the Improvement:** Clearly state the new time and space complexity and summarize the benefits of the new approach.\n\n"
        f"Respond directly with your analysis in Markdown format. Focus on constructive feedback."
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
        f"7.  **Database Isolation:** If using SQLite, the database file **MUST** be named `database.db`. **DO NOT** use `stellar_local.db`. Connect to a local file in the same directory.\n"
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
        f"5.  **Database Isolation:** SQLite database must be named `database.db`, not `stellar_local.db`.\n"
        f"6.  **Relative Paths:** All JavaScript API calls **MUST** use relative paths (e.g., `fetch('api/data')`).\n"
        f"7.  **Environment Variables:** Use `os.getenv('KEY_NAME')` for API keys after loading `dotenv`.\n"
        f"8.  **Run Block:** Keep `if __name__ == '__main__': app.run(host='0.0.0.0', port=5000, debug=True)`.\n"
        f"9.  **Dependencies:** If you add new Python libraries, update `requirements.txt`. You can use ANY PyPI package.\n\n"
        f"**AI Model Guidelines:**\n"
        f"Default to gemini-2.5-flash-lite for AI integrations.\n"
        f"Valid Gemini models: gemini-3-pro-preview, gemini-3-flash-preview, gemini-3-pro-image-preview, gemini-2.5-pro, gemini-2.5-flash, gemini-2.5-flash-lite, gemini-2.5-flash-image, gemini-live-2.5-flash-native-audio. All 1.0/1.5 models are deprecated.\n\n"
        f"**Output Format:** Your entire response MUST be a single, raw, valid JSON object containing **only the files that have changed**. For example: `{{\"index.html\": \"<code>\"}}` or `{{\"requirements.txt\": \"<deps>\"}}`. Do not include explanations or any text outside the JSON object."
    )
