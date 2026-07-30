"""AI provider adapter (strategy pattern) for generating commit messages."""

import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


def build_commit_prompt(diff: str, branch: Optional[str] = None) -> str:
    """Construct the AI prompt for generating a conventional commit message."""
    if branch:
        return (
            f"You are an expert in conventional commits. Generate a single commit message "
            f"for the following git diff. Include the branch name as the scope.\n\n"
            f"Branch: {branch}\n\n"
            f"Git diff:\n{diff}\n\n"
            f"Format: type(scope): description\n"
            f"Allowed types: feat, fix, docs, style, refactor, perf, test, chore, ci, build.\n"
            f"Output only the commit message, no explanations."
        )
    return (
        "You are an expert in conventional commits. Generate a single commit message "
        "for the following git diff.\n\n"
        f"Git diff:\n{diff}\n\n"
        "Format: type: description\n"
        "Allowed types: feat, fix, docs, style, refactor, perf, test, chore, ci, build.\n"
        "Output only the commit message, no explanations."
    )


def clean_commit_message(raw: str) -> str:
    """Clean and normalize the AI-generated commit message."""
    if not raw:
        return ""
    # Remove triple backticks and code fences
    cleaned = re.sub(r"```[\w]*\n?", "", raw)
    cleaned = cleaned.replace("```", "")
    # Remove surrounding quotes
    cleaned = cleaned.strip().strip('"').strip("'")
    # Remove leading bullet or dash
    cleaned = re.sub(r"^[-*]\s+", "", cleaned.strip())
    # Remove common AI preamble text
    cleaned = re.sub(r"^(here is the commit message:?\s*)", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"^(the commit message is:?\s*)", "", cleaned, flags=re.IGNORECASE)
    # Collapse multiple newlines
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    # Remove leading/trailing parentheses if they enclose the whole message
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1].strip()
    return cleaned.strip()


class AICommitter:
    """Strategy-based AI committer that delegates to the configured provider."""

    def __init__(
        self,
        provider: str = "grok",
        model: str = "grok-2",
        temperature: float = 0.5,
        grok_api_key: Optional[str] = None,
        openai_api_key: Optional[str] = None,
        anthropic_api_key: Optional[str] = None,
        ollama_base_url: str = "http://localhost:11434",
        ollama_model: str = "llama3",
    ):
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.grok_api_key = grok_api_key
        self.openai_api_key = openai_api_key
        self.anthropic_api_key = anthropic_api_key
        self.ollama_base_url = ollama_base_url
        self.ollama_model = ollama_model

    async def generate_message(self, diff: str, branch: Optional[str] = None) -> Optional[str]:
        """Generate a commit message using the configured AI provider.
        Returns the cleaned message or None on failure.
        """
        prompt = build_commit_prompt(diff, branch)

        try:
            if self.provider == "grok":
                return await self._call_grok(prompt)
            elif self.provider == "openai":
                return await self._call_openai(prompt)
            elif self.provider == "anthropic":
                return await self._call_anthropic(prompt)
            elif self.provider == "ollama":
                return await self._call_ollama(prompt)
            else:
                logger.error("Unknown AI provider: %s", self.provider)
                return None
        except Exception as exc:
            logger.exception("AI provider call failed for %s: %s", self.provider, exc)
            return None

    async def _call_grok(self, prompt: str) -> Optional[str]:
        import httpx
        if not self.grok_api_key:
            logger.error("Grok API key not configured")
            return None
        headers = {
            "Authorization": f"Bearer {self.grok_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.x.ai/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            return clean_commit_message(raw)

    async def _call_openai(self, prompt: str) -> Optional[str]:
        import httpx
        if not self.openai_api_key:
            logger.error("OpenAI API key not configured")
            return None
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["choices"][0]["message"]["content"]
            return clean_commit_message(raw)

    async def _call_anthropic(self, prompt: str) -> Optional[str]:
        import httpx
        if not self.anthropic_api_key:
            logger.error("Anthropic API key not configured")
            return None
        headers = {
            "x-api-key": self.anthropic_api_key,
            "Content-Type": "application/json",
            "anthropic-version": "2023-06-01",
        }
        payload = {
            "model": self.model,
            "max_tokens": 100,
            "temperature": self.temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data["content"][0]["text"]
            return clean_commit_message(raw)

    async def _call_ollama(self, prompt: str) -> Optional[str]:
        import httpx
        payload = {
            "model": self.ollama_model,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": self.temperature},
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.ollama_base_url}/api/generate",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()
            raw = data.get("response", "")
            return clean_commit_message(raw)