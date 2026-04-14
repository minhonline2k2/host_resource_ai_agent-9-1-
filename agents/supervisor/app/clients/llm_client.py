"""LLM client for Supervisor Agent: Gemini API with model fallback chain."""

from __future__ import annotations

import json
import re
from typing import Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMClient:
    def __init__(self):
        settings = get_settings()
        self.api_key = settings.gemini_api_key
        self.model = settings.gemini_model
        self.timeout = settings.llm_timeout
        self.max_retries = settings.llm_max_retries

    async def analyze_supervisor_incident(
        self, prompt: str
    ) -> tuple[Optional[dict], Optional[str]]:
        """Send supervisor prompt to Gemini. Returns (parsed_dict, raw_text).

        Strategy:
        - Model fallback chain: primary -> gemini-2.0-flash -> gemini-1.5-flash
        - On 429/503 (overloaded): retry with exponential backoff per model
        - If model exhausted all retries: try next model in chain
        """
        # Build model fallback chain
        models_to_try = [self.model]
        if self.model != "gemini-2.0-flash":
            models_to_try.append("gemini-2.0-flash")
        if self.model != "gemini-1.5-flash" and "gemini-1.5-flash" not in models_to_try:
            models_to_try.append("gemini-1.5-flash")

        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 16384,
                "responseMimeType": "application/json",
            },
        }

        logger.info(f"[LLM-SUP] Prompt length: {len(prompt)} chars")
        logger.info(f"[LLM-SUP] Model fallback chain: {models_to_try}")

        last_error = None
        for model_idx, current_model in enumerate(models_to_try):
            url = GEMINI_URL.format(model=current_model)
            is_fallback = model_idx > 0
            prefix = f"[LLM-SUP/{current_model}]"
            if is_fallback:
                logger.warning(f"{prefix} 🔄 Primary model failed, trying fallback model")

            model_exhausted = False
            for attempt in range(1, self.max_retries + 1):
                try:
                    async with httpx.AsyncClient(
                        timeout=httpx.Timeout(self.timeout, connect=30)
                    ) as client:
                        resp = await client.post(
                            url, params={"key": self.api_key}, json=payload
                        )

                    logger.info(
                        f"{prefix} Response status: {resp.status_code} (attempt {attempt})"
                    )

                    if resp.status_code in (429, 503):
                        import asyncio

                        retry_delay = 5 * (2 ** (attempt - 1))  # 5, 10, 20s
                        logger.warning(
                            f"{prefix} ⏳ Overloaded ({resp.status_code}), "
                            f"retry in {retry_delay}s"
                        )
                        last_error = f"LLM {resp.status_code}: {resp.text[:300]}"
                        if attempt < self.max_retries:
                            await asyncio.sleep(retry_delay)
                            continue
                        logger.warning(
                            f"{prefix} ⚠️ All retries exhausted, will try next model"
                        )
                        model_exhausted = True
                        break

                    if resp.status_code != 200:
                        error_text = resp.text[:1000]
                        logger.error(f"{prefix} ❌ HTTP {resp.status_code}: {error_text}")
                        last_error = f"LLM HTTP {resp.status_code}: {error_text}"
                        model_exhausted = True
                        break

                    data = resp.json()

                    if "error" in data:
                        err_msg = data["error"].get("message", str(data["error"]))
                        logger.error(f"{prefix} ❌ API error: {err_msg}")
                        last_error = f"LLM API error: {err_msg}"
                        model_exhausted = True
                        break

                    raw_text = ""
                    finish_reason = ""
                    candidates = data.get("candidates", [])
                    if candidates:
                        finish_reason = candidates[0].get("finishReason", "")
                        parts = candidates[0].get("content", {}).get("parts", [])
                        if parts:
                            raw_text = parts[0].get("text", "")

                    # Check safety blocks
                    pf = data.get("promptFeedback", {})
                    if pf.get("blockReason"):
                        logger.error(
                            f"{prefix} ❌ Blocked: {pf.get('blockReason')} — {pf}"
                        )
                        last_error = f"Blocked by safety: {pf}"
                        model_exhausted = True
                        break

                    logger.info(
                        f"{prefix} finishReason={finish_reason}, "
                        f"response_len={len(raw_text)}"
                    )

                    if finish_reason == "MAX_TOKENS":
                        logger.warning(
                            f"{prefix} ⚠️ Response TRUNCATED (hit max_tokens)"
                        )

                    if not raw_text:
                        logger.error(
                            f"{prefix} ❌ Empty response. finishReason={finish_reason}"
                        )
                        last_error = f"Empty response (finishReason={finish_reason})"
                        model_exhausted = True
                        break

                    logger.info(
                        f"{prefix} ✅ Got response: {len(raw_text)} chars, "
                        f"first 300: {raw_text[:300]}"
                    )

                    # Parse JSON
                    parsed = self._parse_supervisor_response(raw_text)
                    if parsed is None:
                        logger.warning(
                            f"{prefix} ⚠️ Parse failed, will try next model"
                        )
                        last_error = f"JSON parse failed. Raw: {raw_text[:500]}"
                        model_exhausted = True
                        break

                    # SUCCESS
                    if is_fallback:
                        logger.info(f"{prefix} ✅ Fallback model succeeded!")
                    return parsed, raw_text

                except httpx.TimeoutException as e:
                    logger.error(f"{prefix} ❌ Timeout: {e}")
                    last_error = f"Timeout: {e}"
                    if attempt >= self.max_retries:
                        model_exhausted = True
                        break
                except Exception as e:
                    logger.error(f"{prefix} ❌ Error: {e}", exc_info=True)
                    last_error = f"Error: {e}"
                    if attempt >= self.max_retries:
                        model_exhausted = True
                        break

            if not model_exhausted:
                break

        logger.error(f"[LLM-SUP] ❌ All models exhausted. Last error: {last_error}")
        return None, last_error or "All models exhausted"

    def _parse_supervisor_response(self, text: str) -> Optional[dict]:
        """Parse supervisor LLM JSON response into a dict.

        Robust parsing:
        1. Strip markdown code fences
        2. Extract first balanced {...} block
        3. Fix common JSON issues (trailing commas)
        """
        if not text or not text.strip():
            logger.error("[LLM-SUP] Empty text to parse")
            return None

        def _try_parse(candidate: str) -> Optional[dict]:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return None

        cleaned = text.strip()

        # Attempt 1: remove markdown fences
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            cleaned = cleaned.strip()

        data = _try_parse(cleaned)

        # Attempt 2: extract first balanced {...} block
        if data is None:
            logger.warning("[LLM-SUP] Direct parse failed, extracting JSON block...")
            start = cleaned.find("{")
            if start >= 0:
                depth = 0
                in_string = False
                escape = False
                end = -1
                for i in range(start, len(cleaned)):
                    ch = cleaned[i]
                    if escape:
                        escape = False
                        continue
                    if ch == "\\":
                        escape = True
                        continue
                    if ch == '"' and not escape:
                        in_string = not in_string
                        continue
                    if in_string:
                        continue
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end = i + 1
                            break
                if end > start:
                    candidate = cleaned[start:end]
                    data = _try_parse(candidate)

        # Attempt 3: fix trailing commas
        if data is None and cleaned:
            fixed = re.sub(r",(\s*[}\]])", r"\1", cleaned)
            data = _try_parse(fixed)

        if data is None:
            logger.error(f"[LLM-SUP] All parse attempts failed. Raw: {text[:1000]}")
            return None

        try:
            # Ensure required fields with defaults
            if "root_cause" not in data:
                data["root_cause"] = {
                    "category": "UNKNOWN",
                    "summary_vi": "",
                    "evidence": "",
                    "confidence": 0.3,
                }
            if "severity" not in data:
                data["severity"] = "MEDIUM"
            if "immediate_action" not in data:
                data["immediate_action"] = {
                    "description_vi": "",
                    "commands": [],
                    "estimated_ttr_s": 0,
                }
            if "root_fix" not in data:
                data["root_fix"] = {
                    "description_vi": "",
                    "steps_vi": [],
                    "requires_deploy": False,
                    "requires_restart": False,
                }

            # Normalize types
            rc = data["root_cause"]
            if isinstance(rc.get("confidence"), str):
                try:
                    rc["confidence"] = float(rc["confidence"])
                except ValueError:
                    rc["confidence"] = 0.5

            ia = data["immediate_action"]
            if isinstance(ia.get("commands"), str):
                ia["commands"] = [ia["commands"]]
            if ia.get("commands") is None:
                ia["commands"] = []

            logger.info(
                f"[LLM-SUP] ✅ Parsed OK: category={rc.get('category')}, "
                f"severity={data.get('severity')}, "
                f"commands={len(ia.get('commands', []))}"
            )

            return data

        except Exception as e:
            logger.error(f"[LLM-SUP] Validation error: {e}", exc_info=True)
            return None
