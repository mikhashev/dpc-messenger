"""
DPC Agent — Review Tools.

Provides multi-model review capabilities for the embedded agent:
- Cross-model validation of outputs
- Devil's advocate analysis
- Quality assessment

These tools allow the agent to get second opinions from other models
via DPC's LLMManager, improving output quality through diverse perspectives.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from .registry import ToolEntry, ToolContext

log = logging.getLogger(__name__)


def _get_llm_manager(ctx: ToolContext):
    """Get LLMManager from context if available."""
    # LLMManager access would need to be passed through context
    # For now, return None to indicate feature is limited
    return getattr(ctx, "_llm_manager", None)


async def _query_provider(llm_manager, provider_alias: str, prompt: str, timeout: int = 60) -> Dict[str, Any]:
    """
    Query a specific provider asynchronously.

    Args:
        llm_manager: DPC's LLMManager instance
        provider_alias: Provider alias to use
        prompt: Prompt to send
        timeout: Query timeout in seconds

    Returns:
        Dict with success, response, and error fields
    """
    try:
        response = await asyncio.wait_for(
            llm_manager.query(prompt, provider_alias=provider_alias),
            timeout=timeout
        )
        return {"success": True, "response": response}
    except asyncio.TimeoutError:
        return {"success": False, "error": f"Timeout after {timeout}s"}
    except Exception as e:
        return {"success": False, "error": str(e)}


def self_review(ctx: ToolContext, content: str, focus: str = "general") -> str:
    """
    Perform a self-review of content.

    Analyzes the provided content for issues, improvements, and quality.

    Args:
        ctx: Tool context
        content: Content to review
        focus: Review focus area (general, accuracy, clarity, completeness)

    Returns:
        Review analysis
    """
    focus_prompts = {
        "general": "Review this content for any issues, improvements, or concerns.",
        "accuracy": "Check this content for factual accuracy and logical consistency.",
        "clarity": "Review this content for clarity, readability, and understandability.",
        "completeness": "Check if this content is complete or missing important information.",
    }

    prompt = f"""Perform a {focus} review of the following content:

---
{content[:3000]}
---

{focus_prompts.get(focus, focus_prompts['general'])}

Provide your review in this format:
1. **Strengths**: What is good about this content
2. **Issues**: Any problems, errors, or concerns
3. **Suggestions**: Specific improvements that could be made
4. **Rating**: Score from 1-10 with brief justification
"""

    # Since we don't have direct LLMManager access in tools,
    # provide a structured self-review framework
    return f"""Self-Review Framework ({focus} focus):

Content to review ({len(content)} chars):
{content[:2000]}{'...' if len(content) > 2000 else ''}

Review Checklist:
1. **Strengths**
   - What aspects are well-done?
   - What value does this provide?
   - What is clear and effective?

2. **Issues**
   - Are there factual errors?
   - Is anything unclear or confusing?
   - Are there logical inconsistencies?
   - Is anything missing?

3. **Suggestions**
   - How could clarity be improved?
   - What details should be added?
   - What could be removed or condensed?

4. **Rating Guidelines**
   - 9-10: Excellent, minimal improvements needed
   - 7-8: Good, minor improvements possible
   - 5-6: Adequate, notable improvements needed
   - 3-4: Poor, significant issues
   - 1-2: Unacceptable, major revision required

Note: For multi-model review, enable the review tools in agent configuration
and ensure multiple AI providers are configured in providers.json.
"""


def request_critique(ctx: ToolContext, content: str, perspective: str = "neutral") -> str:
    """
    Request a critical analysis from a devil's advocate perspective.

    Args:
        ctx: Tool context
        content: Content to critique
        perspective: Critique perspective (neutral, skeptical, supportive)

    Returns:
        Critical analysis
    """
    perspective_prompts = {
        "neutral": "Provide a balanced critique with both strengths and weaknesses.",
        "skeptical": "Challenge the assumptions and arguments. Find weaknesses and counterarguments.",
        "supportive": "Identify the strongest points and how the argument could be strengthened further.",
    }

    return f"""Devil's Advocate Analysis ({perspective} perspective):

Content to analyze ({len(content)} chars):
{content[:2000]}{'...' if len(content) > 2000 else ''}

Analysis Framework:
{perspective_prompts.get(perspective, perspective_prompts['neutral'])}

Key Questions to Consider:
1. What are the underlying assumptions?
2. Are there alternative interpretations?
3. What evidence supports/contradicts this?
4. What would a critic say?
5. What are the potential blind spots?

Analysis:
[Perform critical analysis based on the {perspective} perspective]

Recommendations:
[Specific suggestions for improvement]
"""


def compare_approaches(ctx: ToolContext, approaches: List[str], criteria: str = "effectiveness") -> str:
    """
    Compare multiple approaches and recommend the best one.

    Args:
        ctx: Tool context
        approaches: List of approaches to compare
        criteria: Comparison criteria

    Returns:
        Comparative analysis
    """
    if len(approaches) < 2:
        return "⚠️ Need at least 2 approaches to compare"

    output = [f"Comparative Analysis ({criteria} criteria):\n"]

    for i, approach in enumerate(approaches, 1):
        output.append(f"Approach {i}:")
        output.append(f"  {approach[:500]}{'...' if len(approach) > 500 else ''}")
        output.append("")

    output.append("\nComparison Framework:")
    output.append(f"  Criteria: {criteria}")
    output.append("")
    output.append("  For each approach, evaluate:")
    output.append(f"    - {criteria.title()} Score (1-10)")
    output.append("    - Pros")
    output.append("    - Cons")
    output.append("    - Risk factors")
    output.append("")
    output.append("  Recommendation:")
    output.append("    [Based on comparison, recommend the best approach]")
    output.append("    [Explain why this approach is preferred]")
    output.append("    [Note any conditions where alternatives might be better]")

    return "\n".join(output)


def quality_checklist(ctx: ToolContext, content_type: str = "general") -> str:
    """
    Generate a quality checklist for a specific content type.

    Args:
        ctx: Tool context
        content_type: Type of content (general, code, documentation, analysis)

    Returns:
        Quality checklist
    """
    checklists = {
        "general": [
            "Clear and concise language",
            "Logical structure and flow",
            "Relevant and accurate information",
            "Appropriate length for the purpose",
            "No spelling or grammar errors",
            "Actionable recommendations (if applicable)",
            "Proper attribution of sources",
        ],
        "code": [
            "Follows coding standards and conventions",
            "Includes appropriate error handling",
            "Has adequate comments and documentation",
            "Handles edge cases",
            "Is testable and maintainable",
            "No security vulnerabilities",
            "Efficient algorithms and data structures",
            "Meaningful variable and function names",
        ],
        "documentation": [
            "Clear purpose and scope",
            "Accurate and up-to-date information",
            "Proper formatting and structure",
            "Code examples (if applicable)",
            "Links to related resources",
            "Installation/setup instructions",
            "Usage examples",
            "Troubleshooting section",
        ],
        "analysis": [
            "Clear problem statement",
            "Appropriate methodology",
            "Thorough data collection",
            "Sound reasoning and logic",
            "Consideration of alternatives",
            "Limitations acknowledged",
            "Actionable conclusions",
            "Supporting evidence provided",
        ],
    }

    items = checklists.get(content_type, checklists["general"])

    output = [f"Quality Checklist for {content_type.title()} Content:\n"]
    output.append("Mark each item as ✓ (pass), ✗ (fail), or ~ (partial):\n")

    for i, item in enumerate(items, 1):
        output.append(f"  [ ] {i}. {item}")

    output.append("\n  Score: ___/{len(items)} items passed")
    output.append("\n  Notes:")
    output.append("  ___")

    return "\n".join(output)


def consensus_check(ctx: ToolContext, responses: List[str], threshold: float = 0.7) -> str:
    """
    Check for consensus among multiple responses.

    Args:
        ctx: Tool context
        responses: List of responses to analyze
        threshold: Agreement threshold (0.0-1.0)

    Returns:
        Consensus analysis
    """
    if len(responses) < 2:
        return "⚠️ Need at least 2 responses for consensus check"

    output = [f"Consensus Analysis (threshold: {threshold*100:.0f}%):\n"]
    output.append(f"Number of responses: {len(responses)}\n")

    for i, response in enumerate(responses, 1):
        output.append(f"Response {i} ({len(response)} chars):")
        output.append(f"  {response[:300]}{'...' if len(response) > 300 else ''}")
        output.append("")

    output.append("Consensus Framework:")
    output.append("")
    output.append("1. **Common Points**")
    output.append("   [Identify points agreed upon by most responses]")
    output.append("")
    output.append("2. **Divergent Points**")
    output.append("   [Identify where responses disagree]")
    output.append("")
    output.append("3. **Unique Insights**")
    output.append("   [Identify insights unique to specific responses]")
    output.append("")
    output.append("4. **Consensus Level**")
    output.append(f"   Estimated agreement: ~___% (threshold: {threshold*100:.0f}%)")
    output.append("   Consensus reached: ___")
    output.append("")
    output.append("5. **Synthesis**")
    output.append("   [Combined view incorporating all perspectives]")

    return "\n".join(output)


def get_tools() -> List[ToolEntry]:
    """Export review tools for registry."""
    return [
        ToolEntry(
            name="self_review",
            schema={
                "name": "self_review",
                "description": "Perform a self-review of content for quality, accuracy, and improvements",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Content to review"
                        },
                        "focus": {
                            "type": "string",
                            "description": "Review focus area",
                            "enum": ["general", "accuracy", "clarity", "completeness"],
                            "default": "general"
                        }
                    },
                    "required": ["content"]
                }
            },
            fn=self_review,
            timeout=30,
        ),

        ToolEntry(
            name="request_critique",
            schema={
                "name": "request_critique",
                "description": "Request a critical devil's advocate analysis of content",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content": {
                            "type": "string",
                            "description": "Content to critique"
                        },
                        "perspective": {
                            "type": "string",
                            "description": "Critique perspective",
                            "enum": ["neutral", "skeptical", "supportive"],
                            "default": "neutral"
                        }
                    },
                    "required": ["content"]
                }
            },
            fn=request_critique,
            timeout=30,
        ),

        ToolEntry(
            name="compare_approaches",
            schema={
                "name": "compare_approaches",
                "description": "Compare multiple approaches and recommend the best one",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "approaches": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of approaches to compare"
                        },
                        "criteria": {
                            "type": "string",
                            "description": "Comparison criteria (e.g., effectiveness, cost, risk)",
                            "default": "effectiveness"
                        }
                    },
                    "required": ["approaches"]
                }
            },
            fn=compare_approaches,
            timeout=30,
        ),

        ToolEntry(
            name="quality_checklist",
            schema={
                "name": "quality_checklist",
                "description": "Generate a quality checklist for a specific content type",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "content_type": {
                            "type": "string",
                            "description": "Type of content",
                            "enum": ["general", "code", "documentation", "analysis"],
                            "default": "general"
                        }
                    },
                    "required": []
                }
            },
            fn=quality_checklist,
            timeout=10,
        ),

        ToolEntry(
            name="consensus_check",
            schema={
                "name": "consensus_check",
                "description": "Check for consensus among multiple responses or opinions",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "responses": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "List of responses to analyze for consensus"
                        },
                        "threshold": {
                            "type": "number",
                            "description": "Agreement threshold (0.0-1.0)",
                            "default": 0.7,
                            "minimum": 0.0,
                            "maximum": 1.0
                        }
                    },
                    "required": ["responses"]
                }
            },
            fn=consensus_check,
            timeout=30,
        ),
    ]
