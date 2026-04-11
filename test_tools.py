import sys
from google import genai
from google.genai import types
import agent_tools

try:
    client = genai.Client(api_key="AIzaSyA_DUMMY_KEY_FOR_TESTING_PURPOSES")
    client.models.generate_content(
        model='gemini-2.5-flash-lite',
        contents='test',
        config=types.GenerateContentConfig(
            tools=agent_tools.available_tools
        )
    )
except Exception as e:
    print(f"FAILED: {e}")
else:
    print("SUCCESS")
