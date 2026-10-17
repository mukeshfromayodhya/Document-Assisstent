"""Small OpenAI-compatible chat-completions client using the Python stdlib."""

from __future__ import annotations

import json
import ipaddress
import logging
import os
import re
import socket
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from dotenv import load_dotenv

from backend.app.config import PROJECT_ROOT

load_dotenv(PROJECT_ROOT / ".env")

logger = logging.getLogger(__name__)


class _NoRedirectHandler(HTTPRedirectHandler):
    """Prevent a provider redirect from forwarding request credentials."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _is_loopback_host(hostname: str | None) -> bool:
    if not hostname:
        return False
    normalized = hostname.strip("[]").rstrip(".").lower()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _api_key_for_url(base_url: str, explicit_key: str | None) -> str:
    if explicit_key is not None:
        return explicit_key.strip()
    configured_key = os.getenv("LLM_API_KEY", "").strip()
    if configured_key:
        return configured_key

    hostname = urlsplit(base_url).hostname
    if hostname == "api.openai.com":
        return os.getenv("OPENAI_API_KEY", "").strip()
    if hostname in {"integrate.api.nvidia.com", "api.nvcf.nvidia.com"}:
        return os.getenv("NVIDIA_API_KEY", "").strip()
    return ""


class LLMError(RuntimeError):
    """Raised when the configured LLM cannot return a usable answer."""


class LLMService:
    """Call an OpenAI-compatible ``/chat/completions`` endpoint.

    Configuration is read from ``LLM_BASE_URL``, ``LLM_MODEL``,
    ``LLM_API_KEY`` (or the matching provider-specific key), ``LLM_TIMEOUT_SECONDS``,
    ``LLM_MAX_OUTPUT_TOKENS``, ``LLM_TEMPERATURE``, and the optional request
    field/header settings. No network call or model download happens during
    import or service construction.
    """

    MAX_RESPONSE_BYTES = 4 * 1024 * 1024

    def __init__(
        self,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
        timeout_seconds: float | None = None,
        max_output_tokens: int | None = None,
        temperature: float | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("LLM_BASE_URL", "https://api.openai.com/v1")).strip()
        self.model = (model if model is not None else os.getenv("LLM_MODEL", "")).strip()
        try:
            parsed = urlsplit(self.base_url)
        except ValueError as exc:
            raise LLMError("LLM_BASE_URL is not a valid URL.") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise LLMError("LLM_BASE_URL must be an http(s) URL.")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise LLMError("LLM_BASE_URL must not contain credentials, a query, or a fragment.")
        try:
            parsed.port
        except ValueError as exc:
            raise LLMError("LLM_BASE_URL contains an invalid port.") from exc
        if parsed.scheme == "http" and not _is_loopback_host(parsed.hostname):
            raise LLMError("LLM_BASE_URL must use HTTPS for non-local providers.")
        self.api_key = _api_key_for_url(self.base_url, api_key)
        self.api_key_header = self._setting_header(
            os.getenv("LLM_API_KEY_HEADER", "Authorization")
        )
        self.api_key_prefix = os.getenv("LLM_API_KEY_PREFIX", "Bearer").strip()
        if len(self.api_key_prefix) > 64 or any(
            ord(character) < 32 or ord(character) == 127
            for character in self.api_key_prefix
        ):
            raise LLMError("LLM_API_KEY_PREFIX contains unsupported characters.")
        self.max_tokens_field = self._setting_field_name(
            os.getenv("LLM_MAX_TOKENS_FIELD", "max_tokens"),
            "LLM_MAX_TOKENS_FIELD",
        )
        self.completions_path = self._setting_completion_path(
            os.getenv("LLM_CHAT_COMPLETIONS_PATH", "chat/completions")
        )
        self.send_temperature = self._setting_bool(
            os.getenv("LLM_SEND_TEMPERATURE", "true"),
            "LLM_SEND_TEMPERATURE",
        )

        self.timeout_seconds = self._setting_float(
            timeout_seconds,
            "LLM_TIMEOUT_SECONDS",
            60.0,
            minimum=0.1,
        )
        self.max_output_tokens = self._setting_int(
            max_output_tokens,
            "LLM_MAX_OUTPUT_TOKENS",
            800,
            minimum=1,
        )
        self.temperature = self._setting_float(
            temperature,
            "LLM_TEMPERATURE",
            0.2,
            minimum=0.0,
            maximum=2.0,
        )
        self.opener = opener or build_opener(_NoRedirectHandler()).open


    @staticmethod
    def _setting_int(
        explicit: int | None,
        env_name: str,
        default: int,
        *,
        minimum: int,
    ) -> int:
        raw: Any = explicit if explicit is not None else os.getenv(env_name, str(default))
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:
            raise LLMError(f"{env_name} must be an integer.") from exc
        if value < minimum:
            raise LLMError(f"{env_name} must be at least {minimum}.")
        return value

    @staticmethod
    def _setting_float(
        explicit: float | None,
        env_name: str,
        default: float,
        *,
        minimum: float,
        maximum: float | None = None,
    ) -> float:
        raw: Any = explicit if explicit is not None else os.getenv(env_name, str(default))
        try:
            value = float(raw)
        except (TypeError, ValueError) as exc:
            raise LLMError(f"{env_name} must be a number.") from exc
        if value < minimum or (maximum is not None and value > maximum):
            if maximum is None:
                raise LLMError(f"{env_name} must be at least {minimum}.")
            raise LLMError(f"{env_name} must be between {minimum} and {maximum}.")
        if value != value or value in (float("inf"), float("-inf")):
            raise LLMError(f"{env_name} must be finite.")
        return value

    @staticmethod
    def _setting_bool(raw: str, env_name: str) -> bool:
        value = raw.strip().lower()
        if value in {"1", "true", "yes", "on"}:
            return True
        if value in {"0", "false", "no", "off"}:
            return False
        raise LLMError(f"{env_name} must be true or false.")

    @staticmethod
    def _setting_header(raw: str) -> str:
        value = raw.strip()
        if not value or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", value):
            raise LLMError("LLM_API_KEY_HEADER must be a valid HTTP header name.")
        return value

    @staticmethod
    def _setting_field_name(raw: str, env_name: str) -> str:
        value = raw.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", value):
            raise LLMError(f"{env_name} must be a valid JSON field name.")
        return value

    @staticmethod
    def _setting_completion_path(raw: str) -> str:
        try:
            parsed = urlsplit(raw.strip())
        except ValueError as exc:
            raise LLMError("LLM_CHAT_COMPLETIONS_PATH is invalid.") from exc
        path = parsed.path.strip("/")
        if (
            not path
            or parsed.scheme
            or parsed.netloc
            or parsed.query
            or parsed.fragment
            or any(part in {".", ".."} for part in path.split("/"))
        ):
            raise LLMError("LLM_CHAT_COMPLETIONS_PATH must be a relative endpoint path.")
        return path

    def generate(self, messages: list[dict[str, str]]) -> str:
        """Generate one answer from chat messages and return plain text."""

        if not self.model:
            raise LLMError("Set LLM_MODEL to a model supported by your provider.")
        normalized_messages = self._validate_messages(messages)
        payload = {
            "model": self.model,
            "messages": normalized_messages,
            "stream": False,
        }
        payload[self.max_tokens_field] = self.max_output_tokens
        # Gemini 3.x recommends its default temperature and no longer
        # recommends setting sampling parameters such as temperature.
        if self.send_temperature and not self.model.lower().startswith("gemini-3"):
            payload["temperature"] = self.temperature
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers[self.api_key_header] = (
                f"{self.api_key_prefix} {self.api_key}"
                if self.api_key_prefix
                else self.api_key
            )

        request = Request(
            self._completion_url(),
            data=body,
            headers=headers,
            method="POST",
        )

        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except HTTPError as exc:
            # Keep diagnostics useful without logging user prompts or secrets.
            # Google-style errors expose a stable status/code separate from
            # their potentially sensitive free-text message.
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            diagnostic = self._provider_error_diagnostic(exc)
            if retry_after:
                retry_after = re.sub(r"[^A-Za-z0-9., _:-]", "?", retry_after)[:64]
            logger.warning(
                "LLM provider returned HTTP %s%s%s",
                exc.code,
                f" ({diagnostic})" if diagnostic else "",
                f"; retry_after={retry_after}" if retry_after else "",
            )
            exc.close()
            raise LLMError(f"LLM provider returned HTTP {exc.code}.") from exc
        
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            logger.warning("Unable to reach the configured LLM provider: %s", type(exc).__name__)
            raise LLMError("Unable to reach the configured LLM provider.") from exc
        except Exception as exc:
            logger.exception("Unexpected error while calling the LLM provider")
            raise LLMError("Unexpected error while calling the LLM provider.") from exc

        if len(raw) > self.MAX_RESPONSE_BYTES:
            raise LLMError("LLM provider response exceeded the configured size limit.")
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LLMError("LLM provider returned invalid JSON.") from exc

        choices = data.get("choices") if isinstance(data, dict) else None
        if isinstance(choices, list) and choices and isinstance(choices[0], dict):
            finish_reason = choices[0].get("finish_reason")
            if isinstance(finish_reason, str) and finish_reason.lower() in {
                "length",
                "max_tokens",
                "max_output_tokens",
            }:
                raise LLMError(
                    "LLM answer reached its output-token limit; increase "
                    "LLM_MAX_OUTPUT_TOKENS and try again."
                )

        answer = self._extract_answer(data)
        if not answer:
            raise LLMError("LLM provider returned an empty answer.")
        return answer

    @staticmethod
    def _provider_error_diagnostic(exc: HTTPError) -> str:
        """Extract only safe, structured provider error identifiers."""
        try:
            raw = exc.read(16 * 1024)
            payload = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if not isinstance(error, dict):
            return ""
        parts = []
        for key in ("status", "code"):
            value = error.get(key)
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                safe_value = re.sub(r"[^A-Za-z0-9_.:-]", "?", str(value))[:64]
                if safe_value:
                    parts.append(f"{key}={safe_value}")
        return ", ".join(parts)

    def _completion_url(self) -> str:
        base = self.base_url.rstrip("/")
        if base.lower().endswith("/" + self.completions_path.lower()):
            return base
        return f"{base}/{self.completions_path}"

    @staticmethod
    def _validate_messages(messages: list[dict[str, str]]) -> list[dict[str, str]]:
        if not isinstance(messages, list) or not messages:
            raise LLMError("At least one chat message is required.")
        normalized: list[dict[str, str]] = []
        valid_roles = {"system", "user", "assistant"}
        for index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise LLMError(f"Chat message at index {index} must be an object.")
            role, content = message.get("role"), message.get("content")
            if not isinstance(role, str) or role not in valid_roles:
                raise LLMError(f"Chat message at index {index} has an unsupported role.")
            if not isinstance(content, str) or not content.strip():
                raise LLMError(f"Chat message at index {index} must contain text.")
            normalized.append({"role": role, "content": content})
        return normalized

    @staticmethod
    def _extract_answer(data: Any) -> str:
        if not isinstance(data, dict):
            raise LLMError("LLM provider returned an unexpected response format.")
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMError("LLM provider response did not contain a completion.")
        message = choices[0].get("message")
        if not isinstance(message, dict):
            raise LLMError("LLM provider response did not contain a message.")
        content = message.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = [
                item["text"]
                for item in content
                if isinstance(item, dict)
                and item.get("type") == "text"
                and isinstance(item.get("text"), str)
            ]
            return "".join(parts).strip()
        return ""
