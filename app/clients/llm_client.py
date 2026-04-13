"""LLM client: send one-shot prompt to Gemini, parse JSON response."""

from __future__ import annotations

import json
from typing import Optional

import httpx

from app.core.config import get_settings
from app.core.logging import get_logger
from app.schemas.schemas import LLMRCAResponse

logger = get_logger(__name__)

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class LLMClient:
    def __init__(self):
        settings = get_settings()
        self.api_key = settings.gemini_api_key
        self.model = settings.gemini_model
        self.timeout = settings.llm_timeout
        self.max_retries = settings.llm_max_retries

    async def analyze_incident(self, prompt: str) -> tuple[Optional[LLMRCAResponse], Optional[str]]:
        """Send one-shot prompt to Gemini.
        
        Returns (parsed_response, raw_response_text).
        raw_response_text is always returned even if parsing fails, for debugging.
        """
        url = GEMINI_URL.format(model=self.model)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }

        logger.info(f"[LLM] Calling {self.model} at {url.split('?')[0]}")
        logger.info(f"[LLM] API key: {self.api_key[:10]}...{self.api_key[-4:]}" if len(self.api_key) > 14 else "[LLM] API key: (short)")
        logger.info(f"[LLM] Prompt length: {len(prompt)} chars, timeout: {self.timeout}s")

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=30)) as client:
                    resp = await client.post(url, params={"key": self.api_key}, json=payload)

                logger.info(f"[LLM] Response status: {resp.status_code} (attempt {attempt})")

                if resp.status_code == 503 or resp.status_code == 429:
                    logger.warning(f"[LLM] Rate limited/unavailable: {resp.status_code}")
                    if attempt < self.max_retries:
                        import asyncio
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return None, f"LLM error {resp.status_code}: {resp.text[:500]}"

                if resp.status_code != 200:
                    error_text = resp.text[:1000]
                    logger.error(f"[LLM] ❌ HTTP {resp.status_code}: {error_text}")
                    return None, f"LLM HTTP {resp.status_code}: {error_text}"

                data = resp.json()

                # Check for API-level errors
                if "error" in data:
                    err_msg = data["error"].get("message", str(data["error"]))
                    logger.error(f"[LLM] ❌ API error: {err_msg}")
                    return None, f"LLM API error: {err_msg}"

                # Extract text from Gemini response
                raw_text = ""
                candidates = data.get("candidates", [])
                if candidates:
                    # Check finish reason
                    finish = candidates[0].get("finishReason", "")
                    if finish and finish not in ("STOP", "MAX_TOKENS"):
                        logger.warning(f"[LLM] ⚠️ finishReason={finish}")
                    
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        raw_text = parts[0].get("text", "")

                if not raw_text:
                    logger.error(f"[LLM] ❌ Empty response from Gemini")
                    logger.error(f"[LLM] Full response: {json.dumps(data, indent=2)[:2000]}")
                    return None, f"Empty response. Full: {json.dumps(data)[:2000]}"

                logger.info(f"[LLM] ✅ Got response: {len(raw_text)} chars")
                logger.info(f"[LLM] First 200 chars: {raw_text[:200]}")

                # Parse JSON
                parsed = self._parse_response(raw_text)
                if parsed:
                    logger.info(f"[LLM] ✅ Parsed OK: confidence={parsed.confidence}, "
                                f"root_cause={parsed.canonical_root_cause}, "
                                f"options={len(parsed.remediation_options)}")
                    for i, opt in enumerate(parsed.remediation_options):
                        logger.info(f"[LLM]    Option {i+1}: {opt.title} ({len(opt.commands)} commands)")
                else:
                    logger.error(f"[LLM] ❌ JSON parse failed. Raw text saved for debugging.")

                return parsed, raw_text

            except httpx.TimeoutException as e:
                logger.error(f"[LLM] ❌ Timeout after {self.timeout}s (attempt {attempt}): {e}")
                if attempt >= self.max_retries:
                    return None, f"Timeout after {self.timeout}s: {e}"
            except httpx.ConnectError as e:
                logger.error(f"[LLM] ❌ Connection error: {e}")
                return None, f"Connection error: {e}"
            except Exception as e:
                logger.error(f"[LLM] ❌ Unexpected error: {e}", exc_info=True)
                if attempt >= self.max_retries:
                    return None, f"Error: {e}"

        return None, "Max retries exceeded"

    def _parse_response(self, text: str) -> Optional[LLMRCAResponse]:
        """Parse LLM JSON response with robust type normalization."""
        try:
            cleaned = text.strip()
            # Remove markdown fences
            if cleaned.startswith("```"):
                cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
                if cleaned.endswith("```"):
                    cleaned = cleaned[:-3]
                cleaned = cleaned.strip()

            data = json.loads(cleaned)
            
            # Normalize remediation_options
            for i, opt in enumerate(data.get("remediation_options", [])):
                if "option_id" not in opt:
                    opt["option_id"] = f"opt-{i+1}"
                if "priority" not in opt:
                    opt["priority"] = i + 1
                # Ensure list fields are actually lists
                for list_field in ("commands", "rollback_commands", "pre_checks", "post_checks", "warnings"):
                    val = opt.get(list_field)
                    if val is None:
                        opt[list_field] = []
                    elif isinstance(val, str):
                        opt[list_field] = [val] if val.strip() else []
                # Ensure string fields
                for str_field in ("title", "description", "risk_level", "action_type", "target", "expected_effect"):
                    if str_field in opt and opt[str_field] is None:
                        opt[str_field] = ""

            # Normalize root_causes
            for rc in data.get("root_causes", []):
                if isinstance(rc.get("evidence_refs"), str):
                    rc["evidence_refs"] = [rc["evidence_refs"]]
                elif rc.get("evidence_refs") is None:
                    rc["evidence_refs"] = []
                # Ensure confidence is float
                if isinstance(rc.get("confidence"), str):
                    try:
                        rc["confidence"] = float(rc["confidence"])
                    except ValueError:
                        rc["confidence"] = 0.0

            # Normalize top-level list fields
            for list_field in ("contributing_factors", "evidence_refs", "what_is_still_unknown", "warnings"):
                val = data.get(list_field)
                if val is None:
                    data[list_field] = []
                elif isinstance(val, str):
                    data[list_field] = [val] if val.strip() else []

            # Normalize confidence
            if isinstance(data.get("confidence"), str):
                try:
                    data["confidence"] = float(data["confidence"])
                except ValueError:
                    data["confidence"] = 0.0

            response = LLMRCAResponse(**data)
            return response

        except json.JSONDecodeError as e:
            logger.error(f"[LLM] JSON parse error at pos {e.pos}: {e.msg}")
            logger.error(f"[LLM] Text around error: ...{text[max(0,(e.pos or 0)-50):(e.pos or 0)+50]}...")
            return None
        except Exception as e:
            logger.error(f"[LLM] Validation error: {e}", exc_info=True)
            return None

    async def analyze_supervisor_incident(self, prompt: str) -> tuple[Optional[dict], Optional[str]]:
        """Send supervisor prompt to Gemini. Returns (parsed_dict, raw_text).
        
        Unlike analyze_incident which returns LLMRCAResponse,
        this returns a raw dict matching the supervisor JSON schema.
        """
        url = GEMINI_URL.format(model=self.model)
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": 0.2,
                "maxOutputTokens": 8192,
                "responseMimeType": "application/json",
            },
        }

        logger.info(f"[LLM-SUP] Calling {self.model} for supervisor analysis")
        logger.info(f"[LLM-SUP] Prompt length: {len(prompt)} chars")

        for attempt in range(1, self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout, connect=30)) as client:
                    resp = await client.post(url, params={"key": self.api_key}, json=payload)

                logger.info(f"[LLM-SUP] Response status: {resp.status_code} (attempt {attempt})")

                if resp.status_code in (429, 503):
                    logger.warning(f"[LLM-SUP] Rate limited: {resp.status_code}")
                    if attempt < self.max_retries:
                        import asyncio
                        await asyncio.sleep(2 ** attempt)
                        continue
                    return None, f"LLM error {resp.status_code}: {resp.text[:500]}"

                if resp.status_code != 200:
                    error_text = resp.text[:1000]
                    logger.error(f"[LLM-SUP] ❌ HTTP {resp.status_code}: {error_text}")
                    return None, f"LLM HTTP {resp.status_code}: {error_text}"

                data = resp.json()

                if "error" in data:
                    err_msg = data["error"].get("message", str(data["error"]))
                    logger.error(f"[LLM-SUP] ❌ API error: {err_msg}")
                    return None, f"LLM API error: {err_msg}"

                raw_text = ""
                candidates = data.get("candidates", [])
                if candidates:
                    parts = candidates[0].get("content", {}).get("parts", [])
                    if parts:
                        raw_text = parts[0].get("text", "")

                if not raw_text:
                    logger.error(f"[LLM-SUP] ❌ Empty response")
                    return None, f"Empty response. Full: {json.dumps(data)[:2000]}"

                logger.info(f"[LLM-SUP] ✅ Got response: {len(raw_text)} chars")

                # Parse as raw JSON dict
                parsed = self._parse_supervisor_response(raw_text)
                return parsed, raw_text

            except httpx.TimeoutException as e:
                logger.error(f"[LLM-SUP] ❌ Timeout: {e}")
                if attempt >= self.max_retries:
                    return None, f"Timeout: {e}"
            except Exception as e:
                logger.error(f"[LLM-SUP] ❌ Error: {e}", exc_info=True)
                if attempt >= self.max_retries:
                    return None, f"Error: {e}"

        return None, "Max retries exceeded"

    def _parse_supervisor_response(self, text: str) -> Optional[dict]:
        """Parse supervisor LLM JSON response into a dict.

        Robust parsing:
        1. Strip markdown code fences (```json ... ```)
        2. If still fails, extract first {...} block from anywhere in text
        3. Try to fix common issues: trailing commas, single quotes
        """
        import re

        if not text or not text.strip():
            logger.error(f"[LLM-SUP] Empty text to parse")
            return None

        def _try_parse(candidate: str) -> Optional[dict]:
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                return None

        cleaned = text.strip()

        # Attempt 1: remove markdown fences
        if cleaned.startswith("```"):
            # Handle ```json\n...\n``` or ```\n...\n```
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", cleaned)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned)
            cleaned = cleaned.strip()

        data = _try_parse(cleaned)

        # Attempt 2: extract first balanced {...} block
        if data is None:
            logger.warning(f"[LLM-SUP] Direct parse failed, trying to extract JSON block...")
            logger.warning(f"[LLM-SUP] Raw text preview: {text[:500]}")
            # Find first { and try to extract balanced JSON
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

        # Attempt 3: fix common JSON issues
        if data is None and cleaned:
            # Remove trailing commas before } or ]
            fixed = re.sub(r",(\s*[}\]])", r"\1", cleaned)
            data = _try_parse(fixed)

        if data is None:
            logger.error(f"[LLM-SUP] All parse attempts failed. Raw: {text[:1000]}")
            return None

        try:
            # Ensure required fields exist with defaults
            if "root_cause" not in data:
                data["root_cause"] = {"category": "UNKNOWN", "summary_vi": "", "evidence": "", "confidence": 0.3}
            if "severity" not in data:
                data["severity"] = "MEDIUM"
            if "immediate_action" not in data:
                data["immediate_action"] = {"description_vi": "", "commands": [], "estimated_ttr_s": 0}
            if "root_fix" not in data:
                data["root_fix"] = {"description_vi": "", "steps_vi": [], "requires_deploy": False, "requires_restart": False}

            # Normalize list fields
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

            logger.info(f"[LLM-SUP] ✅ Parsed OK: category={rc.get('category')}, "
                        f"severity={data.get('severity')}, "
                        f"commands={len(ia.get('commands', []))}")

            return data

        except Exception as e:
            logger.error(f"[LLM-SUP] Validation error: {e}", exc_info=True)
            return None

