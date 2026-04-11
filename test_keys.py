import os
import sys
from google import genai
from dotenv import load_dotenv

# Load keys from the specified file
env_path = 'keys.env'
if os.path.exists(env_path):
    load_dotenv(env_path)
    print(f"Loaded environment from {env_path}")
else:
    print(f"Error: {env_path} not found.")
    sys.exit(1)

api_key = os.getenv("PRIMARY_API_KEY")

if not api_key:
    print("Error: PRIMARY_API_KEY not found in environment.")
    sys.exit(1)

# Masked key for display
masked_key = api_key[:4] + "*" * (len(api_key) - 8) + api_key[-4:]
print(f"Testing PRIMARY_API_KEY: {masked_key}")

# Updated models based on latest documentation
models_to_test = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite"
]

def test_model(model_id):
    print(f"\n--- Testing Model: {model_id} ---")
    try:
        # Use v1beta as per the application's configuration
        client = genai.Client(api_key=api_key, http_options={'api_version': 'v1beta'})
        response = client.models.generate_content(
            model=model_id,
            contents="Say 'Key is working!' if you can read this."
        )
        if response.text:
            print(f"SUCCESS: {response.text.strip()}")
            return True
        else:
            print("FAILURE: Received empty response.")
            return False
    except Exception as e:
        print(f"FAILURE: {str(e)}")
        return False

results = {}
for model in models_to_test:
    results[model] = test_model(model)

print("\n" + "="*30)
print("FINAL SUMMARY:")
for model, success in results.items():
    status = "✅ WORKING" if success else "❌ FAILED"
    print(f"{model}: {status}")
print("="*30)
