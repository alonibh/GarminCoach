"""LLM provider adapter for Ollama and Claude."""
import json
import logging
import requests

import config

logger = logging.getLogger(__name__)

def generate(system: str, user: str, history: list[dict] = None) -> str:
    """Generate a response from the configured LLM provider.
    `history` should be a list of dicts like [{"role": "user", "content": "..."}, ...]
    """
    history = history or []
    
    if config.LLM_PROVIDER == "claude":
        return _generate_claude(system, user, history)
    elif config.LLM_PROVIDER == "gemini":
        return _generate_gemini(system, user, history)
    else:
        return _generate_ollama(system, user, history)


def _generate_ollama(system: str, user: str, history: list[dict]) -> str:
    try:
        url = f"{config.OLLAMA_HOST.rstrip('/')}/api/chat"
        messages = [{"role": "system", "content": system}]
        messages.extend(history)
        messages.append({"role": "user", "content": user})
        
        payload = {
            "model": config.OLLAMA_MODEL,
            "messages": messages,
            "stream": False
        }
        resp = requests.post(url, json=payload, timeout=180)
        resp.raise_for_status()
        return resp.json().get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.error(f"Ollama generation failed: {e}")
        return "Coach is currently offline. Please ensure Ollama is running or check your LLM provider configuration."


def _generate_claude(system: str, user: str, history: list[dict]) -> str:
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        
        # Anthropic expects user/assistant alternation, so we just pass history + user.
        messages = list(history)
        messages.append({"role": "user", "content": user})
        
        resp = client.messages.create(
            model=config.CLAUDE_MODEL,
            max_tokens=1024,
            system=system,
            messages=messages
        )
        return resp.content[0].text.strip()
    except Exception as e:
        logger.error(f"Claude generation failed: {e}")
        return "Coach is currently offline. Please check your Anthropic API key and configuration."


def _generate_gemini(system: str, user: str, history: list[dict]) -> str:
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.GEMINI_MODEL}:generateContent?key={config.GEMINI_API_KEY}"
        
        # Format history to Gemini API specification
        contents = []
        for msg in history:
            role = "user" if msg["role"] == "user" else "model"
            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
        contents.append({"role": "user", "parts": [{"text": user}]})
        
        payload = {
            "system_instruction": {"parts": {"text": system}},
            "contents": contents
        }
        
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        
        data = resp.json()
        candidates = data.get("candidates", [])
        if candidates and candidates[0].get("content", {}).get("parts"):
            return candidates[0]["content"]["parts"][0]["text"].strip()
        return "Coach encountered an empty response from Gemini."
    except Exception as e:
        logger.error(f"Gemini REST API generation failed: {e}")
        return "Coach is currently offline. Please check your Gemini API key and configuration."
