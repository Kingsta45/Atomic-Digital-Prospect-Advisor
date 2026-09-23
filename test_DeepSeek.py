import os
from openai import OpenAI
client = OpenAI(
    api_key=os.environ.get("DEEPSEEK_API_KEY"),
    base_url="https://api.deepseek.com"
)
resp = client.chat.completions.create(
    model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
    messages=[{"role": "user", "content": "Hello DeepSeek!"}]
)
print(resp.choices[0].message.content)
