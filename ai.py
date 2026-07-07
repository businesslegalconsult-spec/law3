import os
import requests
from persona import SYSTEM_PROMPT, FALLBACK_REPLY

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
AI_ENABLED = bool(GROQ_API_KEY)


def ai_reply(history: list, user_text: str) -> str:
    """
    history: список {"role": "user"/"assistant", "text": "..."} — последние N сообщений
    user_text: текущее сообщение (может быть склейкой нескольких подряд идущих)
    """
    if not AI_ENABLED:
        return FALLBACK_REPLY

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in history:
        role = "assistant" if h["role"] == "assistant" else "user"
        messages.append({"role": role, "content": h["text"]})
    messages.append({"role": "user", "content": user_text})

    try:
        resp = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {GROQ_API_KEY}",
                "Content-Type": "application/json",
            },
            json={
                "model": GROQ_MODEL,
                "messages": messages,
                "max_tokens": 300,
                "temperature": 0.6,
            },
            timeout=20,
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"].strip()
        return text or FALLBACK_REPLY
    except Exception as e:
        print(f"[AI ERROR] {e}")
        return FALLBACK_REPLY
