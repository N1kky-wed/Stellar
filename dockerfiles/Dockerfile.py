# Python sandbox with all required packages
FROM python:3.12-slim
WORKDIR /app
RUN apt-get update && apt-get install -y curl && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir matplotlib pandas numpy scipy google-genai scikit-learn Pillow requests beautifulsoup4 lxml Flask Flask-Session werkzeug python-dotenv pypdf pypandoc google-generativeai google-api-core tavily-python sqlitecloud ultralytics opencv-python-headless