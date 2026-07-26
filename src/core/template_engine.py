"""
Meeting Scribe — Template Engine
Renders .docx templates using docxtpl (Jinja2-based) and converts to PDF
via docx2pdf (requires Microsoft Word installed).
"""
from __future__ import annotations

import os
import re
import logging
import time
from typing import List, Optional, Dict, Any
from pathlib import Path

from src.core.models import Meeting, StructuredMeeting, TranscriptSegment

logger = logging.getLogger(__name__)


# ─── Placeholder Schema ─────────────────────────────────────────────

PLACEHOLDER_SCHEMA = {
    "meeting.title": {"type": "str", "description": "Meeting title"},
    "meeting.date": {"type": "str", "description": "ISO 8601 date"},
    "meeting.duration": {"type": "str", "description": "Human-readable duration"},
    "attendees": {"type": "List[str]", "description": "Speaker labels"},
    "summary": {"type": "str", "description": "Executive summary (LLM)"},
    "decisions": {"type": "List[Decision]", "description": "Decisions with speaker"},
    "action_items": {"type": "List[ActionItem]", "description": "Actions with owner"},
    "timeline": {"type": "List[TimelineEvent]", "description": "Chronological events"},
    "transcript": {"type": "List[TranscriptSegment]", "description": "Full transcript"},
}


class TemplateInfo:
    """Metadata about a document template."""
    def __init__(self, path: str, name: str, placeholders: List[str]):
        self.path = path
        self.name = name
        self.placeholders = placeholders

    def __repr__(self):
        return f"TemplateInfo({self.name}, {len(self.placeholders)} placeholders)"


class ValidationResult:
    """Result of template validation."""
    def __init__(self, valid: bool, placeholders: List[str],
                 unknown: List[str], errors: List[str]):
        self.valid = valid
        self.placeholders = placeholders
        self.unknown = unknown
        self.errors = errors


class TemplateEngine:
    """
    Renders document templates using meeting data.

    Usage:
        engine = TemplateEngine()
        templates = engine.list_templates("/path/to/templates")
        engine.render(template_path, meeting, "/output/document.docx")
        engine.export_pdf("/output/document.docx", "/output/document.pdf")
    """

    # Regex to find Jinja2 placeholders in docx XML
    PLACEHOLDER_RE = re.compile(r'\{\{[\s]*([a-zA-Z_][a-zA-Z0-9_.]*(?:\|[a-zA-Z_]+)?)[\s]*\}\}')
    BLOCK_RE = re.compile(r'\{%.*?%\}')

    def list_templates(self, templates_dir: str) -> List[TemplateInfo]:
        """
        List all .docx templates in a directory.

        Args:
            templates_dir: Path to templates directory.

        Returns:
            List of TemplateInfo objects.
        """
        templates = []
        if not os.path.isdir(templates_dir):
            return templates

        for filename in sorted(os.listdir(templates_dir)):
            if filename.endswith('.docx') and not filename.startswith('~'):
                filepath = os.path.join(templates_dir, filename)
                try:
                    placeholders = self.get_placeholders(filepath)
                    name = os.path.splitext(filename)[0].replace('_', ' ').title()
                    templates.append(TemplateInfo(filepath, name, placeholders))
                except Exception as e:
                    logger.warning(f"Could not parse template {filename}: {e}")

        return templates

    def get_placeholders(self, template_path: str) -> List[str]:
        """
        Extract Jinja2 placeholders from a .docx template.

        Args:
            template_path: Path to .docx template file.

        Returns:
            List of placeholder names found in the template.
        """
        from docxtpl import DocxTemplate

        doc = DocxTemplate(template_path)
        # docxtpl provides undeclared variables
        try:
            variables = doc.get_undeclared_template_variables()
            return sorted(variables)
        except Exception:
            # Fallback: parse XML manually
            xml_content = doc.get_xml()
            matches = self.PLACEHOLDER_RE.findall(xml_content)
            return sorted(set(matches))

    def validate_template(self, template_path: str) -> ValidationResult:
        """
        Validate a template against the placeholder schema.

        Args:
            template_path: Path to .docx template file.

        Returns:
            ValidationResult with status and details.
        """
        errors = []
        try:
            placeholders = self.get_placeholders(template_path)
        except Exception as e:
            return ValidationResult(
                valid=False, placeholders=[], unknown=[],
                errors=[f"Could not read template: {e}"]
            )

        known = set(PLACEHOLDER_SCHEMA.keys())
        # Also allow dotted access patterns like "item.owner"
        loop_vars = {"item", "decision", "action", "event", "segment"}

        unknown = []
        for ph in placeholders:
            root = ph.split('.')[0]
            if ph not in known and root not in known and root not in loop_vars:
                unknown.append(ph)

        if unknown:
            errors.append(f"Unknown placeholders: {', '.join(unknown)}")

        return ValidationResult(
            valid=len(errors) == 0,
            placeholders=placeholders,
            unknown=unknown,
            errors=errors,
        )

    def build_context(self, meeting: Meeting) -> Dict[str, Any]:
        """
        Build the Jinja2 template context from a Meeting object.

        Args:
            meeting: Meeting object with metadata, transcript, and structured data.

        Returns:
            Dictionary ready for docxtpl rendering.
        """
        context = {
            "meeting": {
                "title": meeting.metadata.title,
                "date": meeting.metadata.date,
                "duration": meeting.metadata.duration,
            },
            "attendees": meeting.metadata.attendees or meeting.speaker_list,
            "transcript": [
                {
                    "speaker": s.speaker or "Speaker",
                    "start": f"{s.start:.0f}s",
                    "text": s.text,
                }
                for s in meeting.transcript
            ],
        }

        # Add structured data if available
        if meeting.structured:
            context["summary"] = meeting.structured.summary
            context["decisions"] = [d.to_dict() for d in meeting.structured.decisions]
            context["action_items"] = [a.to_dict() for a in meeting.structured.action_items]
            context["timeline"] = [t.to_dict() for t in meeting.structured.timeline]
        else:
            context["summary"] = "(No LLM backend configured — summary not available)"
            context["decisions"] = []
            context["action_items"] = []
            context["timeline"] = []

        return context

    def render(self, template_path: str, meeting: Meeting,
               output_path: str) -> str:
        """
        Render a template with meeting data and save the result.

        Args:
            template_path: Path to .docx template.
            meeting: Meeting object with all data.
            output_path: Path for the generated document.

        Returns:
            The output file path.
        """
        from docxtpl import DocxTemplate

        logger.info(f"Rendering template: {os.path.basename(template_path)}")
        start = time.time()

        doc = DocxTemplate(template_path)
        context = self.build_context(meeting)
        doc.render(context)

        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        doc.save(output_path)

        elapsed = time.time() - start
        logger.info(f"Document rendered in {elapsed:.1f}s: {output_path}")

        return output_path

    def render_preview(self, template_path: str) -> str:
        """
        Render a template with sample data for preview purposes.

        Args:
            template_path: Path to .docx template.

        Returns:
            Path to the temporary preview document.
        """
        from src.utils.file_utils import get_temp_dir

        sample_meeting = self._create_sample_meeting()
        preview_path = os.path.join(
            str(get_temp_dir()),
            f"preview_{os.path.basename(template_path)}"
        )
        return self.render(template_path, sample_meeting, preview_path)

    def export_pdf(self, docx_path: str, pdf_path: Optional[str] = None) -> Optional[str]:
        """
        Convert a .docx file to PDF using Microsoft Word (via docx2pdf).
        Requires MS Word to be installed on the system.

        Args:
            docx_path: Path to the .docx file.
            pdf_path: Output PDF path. If None, uses same name with .pdf extension.

        Returns:
            Path to the PDF file, or None if conversion failed.
        """
        if pdf_path is None:
            pdf_path = os.path.splitext(docx_path)[0] + ".pdf"

        try:
            from docx2pdf import convert

            logger.info(f"Converting to PDF: {pdf_path}")
            convert(docx_path, pdf_path)
            logger.info(f"PDF exported: {pdf_path}")
            return pdf_path

        except ImportError:
            logger.warning("docx2pdf not installed — PDF export unavailable")
            return None
        except Exception as e:
            logger.error(f"PDF conversion failed: {e}")
            return None

    def _create_sample_meeting(self) -> Meeting:
        """Create a sample Meeting object for template preview."""
        from src.core.models import (
            MeetingMetadata, ActionItem, Decision,
            TimelineEvent, StructuredMeeting
        )
        from datetime import datetime

        metadata = MeetingMetadata(
            title="Sample Meeting — Product Review",
            date=datetime.now().strftime("%Y-%m-%d"),
            duration="32m 15s",
            duration_seconds=1935.0,
            attendees=["Sarah Chen", "James Park", "Maria Garcia"],
            app_version="1.0.0",
        )

        transcript = [
            TranscriptSegment(0.0, 15.0, "Good morning everyone, let's get started with the product review.", "Sarah Chen"),
            TranscriptSegment(15.5, 30.0, "I'd like to discuss the Q3 roadmap updates first.", "James Park"),
            TranscriptSegment(31.0, 50.0, "We've completed the authentication module and it's ready for testing.", "Maria Garcia"),
            TranscriptSegment(52.0, 70.0, "Great work. Let's plan the rollout for next week.", "Sarah Chen"),
        ]

        structured = StructuredMeeting(
            summary="Product review meeting covering Q3 roadmap updates. Authentication module completed and ready for testing. Rollout planned for next week.",
            decisions=[
                Decision("Proceed with authentication module rollout next week", "Sarah Chen", 52.0),
                Decision("Prioritize performance testing before release", "James Park", 65.0),
            ],
            action_items=[
                ActionItem("Maria Garcia", "Complete integration tests for auth module", "2026-05-20"),
                ActionItem("James Park", "Prepare rollout plan document", "2026-05-19"),
                ActionItem("Sarah Chen", "Schedule stakeholder demo", ""),
            ],
            timeline=[
                TimelineEvent(0.0, "intro", "Meeting kickoff and agenda"),
                TimelineEvent(15.5, "discussion", "Q3 roadmap review"),
                TimelineEvent(31.0, "decision", "Auth module status and rollout"),
                TimelineEvent(52.0, "action", "Next steps and assignments"),
            ],
        )

        return Meeting(
            metadata=metadata,
            transcript=transcript,
            structured=structured,
        )
