"""
Meeting Scribe — LLM-based Meeting Structuring
Extracts summaries, action items, decisions, and timeline from transcripts.
Uses Ollama (local) or cloud APIs (BYO key) with graceful degradation.

If no LLM backend is available, structuring is silently skipped —
the user still gets the raw transcript and speaker labels.
"""
from __future__ import annotations

import json
import logging
import time
from typing import List, Optional

from src.core.models import (
    TranscriptSegment, StructuredMeeting, ActionItem,
    Decision, TimelineEvent, LLMBackend
)

logger = logging.getLogger(__name__)

# ─── Prompts ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a professional meeting analyst. You will receive a meeting transcript and must extract structured information from it.

Return ONLY valid JSON with the following schema:
{
  "summary": "A concise 2-3 sentence executive summary of the meeting",
  "decisions": [
    {"description": "What was decided", "speaker": "Who made/announced the decision", "timestamp": 0.0}
  ],
  "action_items": [
    {"owner": "Person responsible", "description": "What needs to be done", "due_date": "If mentioned, otherwise empty string"}
  ],
  "timeline": [
    {"timestamp": 0.0, "phase": "intro|discussion|decision|action|closing", "topic": "Brief topic description"}
  ]
}

Rules:
- Extract ONLY information explicitly stated in the transcript.
- Do NOT invent or assume information not present.
- Use speaker labels exactly as they appear in the transcript.
- If no decisions or action items are found, return empty lists.
- Timeline should capture major topic shifts, not every sentence.
- Phase must be one of: intro, discussion, decision, action, closing.
"""

USER_PROMPT_TEMPLATE = """Here is the meeting transcript to analyze:

{transcript_text}

Extract the structured meeting data as JSON."""


class MeetingStructurer:
    """
    Extracts structured data from meeting transcripts using an LLM.

    Supported backends:
    - Ollama (local, free, default)
    - OpenAI (cloud, BYO API key)
    - Anthropic/Claude (cloud, BYO API key)

    Usage:
        structurer = MeetingStructurer(backend=LLMBackend.OLLAMA)
        structured = structurer.extract_structure(transcript_segments)
        if structured:
            print(structured.summary)
            for item in structured.action_items:
                print(f"  [{item.owner}] {item.description}")
    """

    def __init__(self, backend: LLMBackend = LLMBackend.OLLAMA,
                 model: str = "llama3.1:8b",
                 api_key: Optional[str] = None,
                 base_url: Optional[str] = None):
        """
        Args:
            backend: Which LLM backend to use.
            model: Model name (e.g., "llama3.1:8b" for Ollama).
            api_key: API key for cloud backends.
            base_url: Custom API base URL (e.g., for local Ollama).
        """
        self.backend = backend
        self.model = model
        self.api_key = api_key
        self.base_url = base_url or "http://localhost:11434"

    @property
    def is_available(self) -> bool:
        """Check if the configured backend is reachable."""
        if self.backend == LLMBackend.NONE:
            return False

        if self.backend == LLMBackend.OLLAMA:
            return self._check_ollama()

        if self.backend in (LLMBackend.GROQ, LLMBackend.OPENAI, LLMBackend.ANTHROPIC):
            return bool(self.api_key)

        return False

    def _check_ollama(self) -> bool:
        """Check if Ollama is running locally."""
        try:
            import urllib.request
            req = urllib.request.Request(f"{self.base_url}/api/tags", method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    def extract_structure(self, transcript: List[TranscriptSegment]) -> Optional[StructuredMeeting]:
        """
        Extract structured meeting data from a transcript.

        Args:
            transcript: List of TranscriptSegment with text and speaker labels.

        Returns:
            StructuredMeeting if successful, None if LLM unavailable or fails.
        """
        if self.backend == LLMBackend.NONE:
            logger.info("No LLM backend configured — skipping structuring")
            return None

        if not self.is_available:
            logger.warning(
                f"LLM backend '{self.backend.value}' is not available. "
                "Skipping structuring. Install Ollama or add an API key in Settings."
            )
            return None

        # Format transcript for the LLM
        transcript_text = self._format_transcript(transcript)

        if not transcript_text.strip():
            logger.warning("Empty transcript — nothing to structure")
            return None

        logger.info(f"Structuring meeting with {self.backend.value}/{self.model}...")
        start = time.time()

        try:
            if self.backend == LLMBackend.OLLAMA:
                response = self._call_ollama(transcript_text)
            elif self.backend == LLMBackend.GROQ:
                response = self._call_groq(transcript_text)
            elif self.backend == LLMBackend.OPENAI:
                response = self._call_openai(transcript_text)
            elif self.backend == LLMBackend.ANTHROPIC:
                response = self._call_anthropic(transcript_text)
            else:
                return None

            result = self._parse_response(response)

            elapsed = time.time() - start
            logger.info(
                f"Structuring complete in {elapsed:.1f}s: "
                f"{len(result.action_items)} actions, "
                f"{len(result.decisions)} decisions"
            )
            return result

        except Exception as e:
            logger.error(f"Structuring failed: {e}", exc_info=True)
            return None

    def _format_transcript(self, transcript: List[TranscriptSegment]) -> str:
        """Format transcript segments into readable text for the LLM."""
        lines = []
        for seg in transcript:
            speaker = seg.speaker if seg.speaker else "Speaker"
            timestamp = f"[{seg.start:.0f}s]"
            lines.append(f"{timestamp} {speaker}: {seg.text}")
        return "\n".join(lines)

    def _call_ollama(self, transcript_text: str) -> str:
        """Call local Ollama API."""
        try:
            import ollama

            response = ollama.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                        transcript_text=transcript_text
                    )},
                ],
                options={"temperature": 0.1},
            )
            return response["message"]["content"]

        except ImportError:
            # Fallback to raw HTTP
            import urllib.request
            import json as json_module

            payload = json_module.dumps({
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                        transcript_text=transcript_text
                    )},
                ],
                "stream": False,
                "options": {"temperature": 0.1},
            }).encode('utf-8')

            req = urllib.request.Request(
                f"{self.base_url}/api/chat",
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json_module.loads(resp.read().decode('utf-8'))
                return result["message"]["content"]

    def _call_groq(self, transcript_text: str) -> str:
        """Call Groq's OpenAI-compatible chat endpoint (FREE TIER).

        Uses the same API key as Groq speech-to-text. Llama 3.3 70B is far
        stronger than a local 8B model and costs nothing on the free tier.
        """
        import urllib.request
        import json as json_module

        payload = json_module.dumps({
            "model": self.model or "llama-3.3-70b-versatile",
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                    transcript_text=transcript_text
                )},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }).encode('utf-8')

        req = urllib.request.Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json_module.loads(resp.read().decode('utf-8'))
            return result["choices"][0]["message"]["content"]

    def generate_document(self, transcript: List[TranscriptSegment],
                          instruction: str,
                          structured: Optional[StructuredMeeting] = None,
                          meeting_title: str = "",
                          meeting_date: str = "") -> str:
        """
        Generate a free-form document from the transcript using the LLM.

        This is the "tell the AI what document you want" path — as opposed to
        extract_structure(), which fills fixed template placeholders.

        Args:
            transcript: The meeting transcript.
            instruction: What the user wants, e.g. "formal minutes of meeting
                         in Bahasa Malaysia" or "client follow-up email".
            structured: Optional already-extracted structure for extra context.
            meeting_title / meeting_date: Metadata for the header.

        Returns:
            Markdown text of the generated document.

        Raises:
            RuntimeError: If no backend is available or the call fails.
        """
        if not self.is_available:
            raise RuntimeError(
                f"LLM backend '{self.backend.value}' is not available. "
                "Configure it in Settings → AI Document Structuring."
            )

        transcript_text = self._format_transcript(transcript)
        if not transcript_text.strip():
            raise RuntimeError("Transcript is empty — nothing to generate from.")

        context_parts = [
            f"Meeting title: {meeting_title or 'Untitled'}",
            f"Date: {meeting_date or 'Unknown'}",
        ]
        if structured:
            if structured.summary:
                context_parts.append(f"Summary: {structured.summary}")
            if structured.action_items:
                items = "; ".join(
                    f"{a.owner}: {a.description}" for a in structured.action_items
                )
                context_parts.append(f"Action items: {items}")
            if structured.decisions:
                decs = "; ".join(d.description for d in structured.decisions)
                context_parts.append(f"Decisions: {decs}")

        system = (
            "You are a professional business writer. Produce polished, "
            "well-structured documents from meeting transcripts.\n\n"
            "Rules:\n"
            "- Use ONLY information present in the transcript. Never invent "
            "facts, names, dates, or commitments.\n"
            "- If the transcript does not contain something the requested "
            "document normally includes, write 'Not discussed'.\n"
            "- Output GitHub-flavored Markdown: # for the title, ## for "
            "sections, - for bullets, and Markdown tables where useful.\n"
            "- Write in the same language the user requests; if unspecified, "
            "use the dominant language of the transcript.\n"
            "- Do not add commentary before or after the document."
        )

        user = (
            f"{chr(10).join(context_parts)}\n\n"
            f"=== TRANSCRIPT ===\n{transcript_text}\n=== END TRANSCRIPT ===\n\n"
            f"Produce the following document:\n{instruction}"
        )

        logger.info(f"Generating document via {self.backend.value}: {instruction[:60]}")

        if self.backend == LLMBackend.OLLAMA:
            return self._chat_ollama(system, user)
        if self.backend == LLMBackend.GROQ:
            return self._chat_openai_compatible(
                system, user,
                url="https://api.groq.com/openai/v1/chat/completions",
                model=self.model or "llama-3.3-70b-versatile",
            )
        if self.backend == LLMBackend.OPENAI:
            return self._chat_openai_compatible(
                system, user,
                url="https://api.openai.com/v1/chat/completions",
                model=self.model or "gpt-4o-mini",
            )
        if self.backend == LLMBackend.ANTHROPIC:
            return self._chat_anthropic(system, user)

        raise RuntimeError(f"Unsupported backend: {self.backend}")

    # ─── Free-form chat helpers (used by generate_document) ──────────

    def _chat_ollama(self, system: str, user: str) -> str:
        import urllib.request
        import json as json_module

        payload = json_module.dumps({
            "model": self.model or "llama3.1:8b",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": 0.3},
        }).encode('utf-8')

        req = urllib.request.Request(
            f"{self.base_url}/api/chat", data=payload,
            headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            return json_module.loads(resp.read().decode('utf-8'))["message"]["content"]

    def _chat_openai_compatible(self, system: str, user: str,
                                url: str, model: str) -> str:
        import urllib.request
        import json as json_module

        payload = json_module.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.3,
            "max_tokens": 8000,
        }).encode('utf-8')

        req = urllib.request.Request(
            url, data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json_module.loads(resp.read().decode('utf-8'))
            return result["choices"][0]["message"]["content"]

    def _chat_anthropic(self, system: str, user: str) -> str:
        import urllib.request
        import json as json_module

        payload = json_module.dumps({
            "model": self.model if "claude" in (self.model or "") else "claude-sonnet-4-20250514",
            "max_tokens": 8000,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "temperature": 0.3,
        }).encode('utf-8')

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages", data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json_module.loads(resp.read().decode('utf-8'))
            return result["content"][0]["text"]

    def _call_openai(self, transcript_text: str) -> str:
        """Call OpenAI-compatible API."""
        import urllib.request
        import json as json_module

        payload = json_module.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                    transcript_text=transcript_text
                )},
            ],
            "temperature": 0.1,
        }).encode('utf-8')

        req = urllib.request.Request(
            "https://api.openai.com/v1/chat/completions",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json_module.loads(resp.read().decode('utf-8'))
            return result["choices"][0]["message"]["content"]

    def _call_anthropic(self, transcript_text: str) -> str:
        """Call Anthropic Claude API."""
        import urllib.request
        import json as json_module

        payload = json_module.dumps({
            "model": self.model if "claude" in self.model else "claude-sonnet-4-20250514",
            "max_tokens": 4096,
            "system": SYSTEM_PROMPT,
            "messages": [
                {"role": "user", "content": USER_PROMPT_TEMPLATE.format(
                    transcript_text=transcript_text
                )},
            ],
            "temperature": 0.1,
        }).encode('utf-8')

        req = urllib.request.Request(
            "https://api.anthropic.com/v1/messages",
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json_module.loads(resp.read().decode('utf-8'))
            return result["content"][0]["text"]

    def _parse_response(self, response: str) -> StructuredMeeting:
        """Parse the LLM JSON response into a StructuredMeeting object."""
        # Extract JSON from the response (LLMs sometimes wrap in markdown)
        json_str = response.strip()

        # Try to find JSON block in markdown code fences
        if "```json" in json_str:
            start = json_str.index("```json") + 7
            end = json_str.index("```", start)
            json_str = json_str[start:end].strip()
        elif "```" in json_str:
            start = json_str.index("```") + 3
            end = json_str.index("```", start)
            json_str = json_str[start:end].strip()

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse LLM response as JSON: {e}")
            # Return a minimal structure with just the summary
            return StructuredMeeting(summary=response[:500])

        return StructuredMeeting.from_dict(data)
