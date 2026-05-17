import os
from openai import OpenAI

api_key = os.environ.get("OPENROUTER_API_KEY", "no_key")
client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")

try:
    response = client.embeddings.create(
        model="openai/text-embedding-3-small",
        input=["Hello world"]
    )
    print("Success:", response.data[0].embedding[:5])
except Exception as e:
    print("Error:", e)
