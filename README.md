# Stellar

Stellar is an advanced web-based AI assistant designed to streamline research, web/app development, and data analysis. It integrates powerful large language models with real-time web capabilities and file processing to provide comprehensive and dynamic responses.

## ✨ Features

*   **Multimode AI Assistance:**
    *   **Stellar Mode (Default):** General AI assistance for a wide range of queries.
    *   **Spectrum Mode:** Conducts in-depth research, integrates real-time web search (Spectral Search via Tavily API), and analyzes uploaded files to generate comprehensive research papers.
    *   **Nebula Mode:** Guides users through a multi-step process for web and application development, generating detailed plans, frontend (HTML/CSS/JS), and backend (Python/Flask) code, including necessary API integrations (e.g., Unsplash, YouTube, Gemini).
    *   **Cosmos Mode:** Creates stunning, interactive data analysis reports with extreme infographics using HTML, Tailwind CSS, and Chart.js, based on provided data and web context.
*   **Real-time Data Fetching:** Capable of fetching up-to-date information when needed, enhancing the relevance and accuracy of responses.
*   **File Analysis & Upload:** Upload various file types (documents, images, videos, audio, code) for detailed AI analysis and context integration into responses.
*   **Interactive Code Preview:** Directly preview generated HTML/CSS/JS code within the application, offering a live rendering of web projects.
*   **Chat History & Management:** Persistent chat sessions, allowing users to save, retrieve, and manage conversations. **Includes powerful chat search functionality that lets you search for any message content across your chats and jump directly to that message within its conversation.**
*   **User Authentication:** Secure user registration and login system with password management.
*   **Dynamic Theming:** Visual themes that adapt based on the selected AI model (Emerald, Lunarity, Crimson, Obsidian).

## 🚀 Technologies Used

**Backend (Python):**

*   **Flask:** Web framework for the server-side application.
*   **google-generativeai:** Integrates with Google's Gemini models for AI capabilities.
*   **tavily-python:** Facilitates real-time web search for Spectrum mode.
*   **requests & BeautifulSoup4:** Used by the internal `webscrapper` for URL content extraction.
*   **sqlitecloud:** Cloud-based SQLite database for managing user data, chat history, and file analysis results.
*   **PyPDF2:** For processing and chunking PDF documents during file analysis.
*   **pypandoc:** Used for converting Markdown research papers to HTML.
*   **werkzeug.security:** For password hashing and checking.
*   **python-dotenv:** Manages environment variables for API keys and configurations.
*   **uuid & threading & queue:** For managing unique IDs, concurrent tasks (like file analysis), and inter-thread communication.

**Frontend (Web):**

*   **HTML5:** Structure of the web application.
*   **CSS3:** Styling, with dynamic variables for theme adaptation.
*   **JavaScript (ES6+):** Handles UI interactions, API calls, real-time updates (SSE), and client-side logic.
*   **Marked.js:** Renders Markdown content in chat messages.
*   **Turndown.js:** Converts HTML back to Markdown for editing.
*   **Highlight.js:** Provides syntax highlighting for code blocks.
*   **KaTeX:** Renders mathematical equations.
*   **Chart.js:** (Used in Cosmos mode outputs) For creating dynamic and visually rich data infographics.
*   **Tailwind CSS:** (Used in Cosmos mode outputs) A utility-first CSS framework for rapid UI development.
