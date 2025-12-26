"""
Prompt Manager - AI Prompt Assembly

Manages construction of prompts sent to AI models, including:
- System instructions from instructions.json
- Personal context blocks (JSON format)
- Device context blocks (hardware/software specs)
- Conversation history
- Peer context integration

Extracted from service.py for better separation of concerns (v0.12.0 refactor)
"""

import logging
import json
from typing import Dict, Optional, List, Any
from dataclasses import asdict

logger = logging.getLogger(__name__)


class PromptManager:
    """Manages AI prompt assembly with context, history, and instructions.

    Responsibilities:
    - Load system instructions from instructions.json
    - Assemble prompts with optional context blocks
    - Format conversation history
    - Handle device context special instructions
    - Respect privacy settings (context checkbox state)
    """

    def __init__(self, instruction_set, peer_metadata: Dict[str, Dict[str, Any]]):
        """Initialize PromptManager.

        Args:
            instruction_set: InstructionSet from instructions.json (v2.0)
            peer_metadata: Dict of {node_id: {name, ...}} for peer labels
        """
        self.instruction_set = instruction_set
        self.peer_metadata = peer_metadata

    def assemble_prompt(
        self,
        query: str,
        contexts: Dict[str, Any] = None,
        device_context: Optional[dict] = None,
        peer_device_contexts: Optional[Dict[str, dict]] = None,
        message_history: Optional[list] = None,
        include_context: bool = True,
        instruction_set_name: Optional[str] = None
    ) -> str:
        """Assemble final prompt for the LLM with instruction processing.

        Phase 2: Incorporates InstructionBlock from PCM v2.0
        Phase 7: Supports conversation history and context optimization

        Args:
            query: The user's current question/message
            contexts: Dict of {source_id: PersonalContext} (only if include_context=True)
            device_context: Local device context (optional, only if include_context=True)
            peer_device_contexts: Dict of {peer_id: device_context} for peers (optional)
            message_history: List of conversation messages (optional, for Phase 7)
            include_context: If True, include context blocks; if False, skip (Phase 7)
            instruction_set_name: Name of instruction set to use (optional, defaults to default set)

        Returns:
            Complete prompt ready to send to LLM
        """
        # Build system instruction (ONLY from instructions.json when context enabled)
        # Fix: Don't leak instructions when checkbox is unchecked (v0.12.0)
        system_instruction = self._build_system_instruction(include_context, instruction_set_name)

        # Build context blocks (personal context + device context)
        context_blocks = []

        if contexts:
            context_blocks.extend(self._build_personal_context_blocks(contexts))

        if device_context:
            context_blocks.append(self._build_device_context_block(
                device_context,
                source_label="local"
            ))

        if peer_device_contexts:
            for peer_id, peer_device_ctx in peer_device_contexts.items():
                source_label = self._get_peer_label(peer_id)
                context_blocks.append(self._build_device_context_block(
                    peer_device_ctx,
                    source_label=source_label
                ))

        # Assemble final prompt with optional context and history
        if include_context and context_blocks:
            # Include full context (first message or context changed)
            final_prompt = (
                f"{system_instruction}\n\n"
                f"--- CONTEXTUAL DATA ---\n"
                f'{"\n\n".join(context_blocks)}\n'
                f"--- END OF CONTEXTUAL DATA ---\n\n"
            )
        else:
            # No context or context already included in previous messages
            final_prompt = f"{system_instruction}\n\n"

        # Add conversation history if provided
        if message_history and len(message_history) > 0:
            final_prompt += self._build_history_section(message_history)
            final_prompt += f"\nUSER QUERY: {query}"
        else:
            # No history, just the current query
            final_prompt += f"USER QUERY: {query}"

        return final_prompt

    def _build_system_instruction(self, include_context: bool, instruction_set_name: Optional[str] = None) -> str:
        """Build system instruction based on context inclusion state.

        Args:
            include_context: If True, use instructions.json; if False, pure mode (no instruction)
            instruction_set_name: Name of instruction set to use (optional)

        Returns:
            System instruction text
        """
        if include_context:
            # Get the instruction set to use
            if instruction_set_name:
                instructions = self.instruction_set.get_set(instruction_set_name)
            else:
                instructions = self.instruction_set.get_default()

            # Return primary instruction
            return instructions.primary if instructions and instructions.primary else ""
        else:
            # User disabled context - pure mode (no system instruction)
            return ""

    def _build_personal_context_blocks(self, contexts: Dict[str, Any]) -> List[str]:
        """Build personal context XML blocks.

        Args:
            contexts: Dict of {source_id: PersonalContext}

        Returns:
            List of formatted context blocks
        """
        blocks = []

        for source_id, context_obj in contexts.items():
            context_dict = asdict(context_obj)
            json_string = json.dumps(context_dict, indent=2, ensure_ascii=False)

            # Add peer name if available
            source_label = self._get_peer_label(source_id) if source_id != 'local' else source_id

            block = f'<CONTEXT source="{source_label}">\n{json_string}\n</CONTEXT>'
            blocks.append(block)

        return blocks

    def _build_device_context_block(self, device_ctx: dict, source_label: str) -> str:
        """Build device context XML block with special instructions.

        Args:
            device_ctx: Device context dictionary
            source_label: Label for the source (e.g., "local" or "Alice (dpc-node-...)")

        Returns:
            Formatted device context block
        """
        # Extract special instructions if present (schema v1.1+)
        special_instructions_text = ""
        if "special_instructions" in device_ctx:
            special_instructions_text = self._format_special_instructions(
                device_ctx["special_instructions"]
            )

        device_json = json.dumps(device_ctx, indent=2, ensure_ascii=False)
        return f'<DEVICE_CONTEXT source="{source_label}">{special_instructions_text}{device_json}\n</DEVICE_CONTEXT>'

    def _format_special_instructions(self, instructions_obj: dict) -> str:
        """Format special instructions from device context schema v1.1+.

        Args:
            instructions_obj: Special instructions dictionary

        Returns:
            Formatted special instructions text
        """
        text = "\nDEVICE CONTEXT INTERPRETATION RULES:\n"

        if "interpretation" in instructions_obj:
            text += "\nInterpretation Guidelines:\n"
            for key, value in instructions_obj["interpretation"].items():
                text += f"  - {key}: {value}\n"

        if "privacy" in instructions_obj:
            text += "\nPrivacy Rules:\n"
            for key, value in instructions_obj["privacy"].items():
                text += f"  - {key}: {value}\n"

        if "update_protocol" in instructions_obj:
            text += "\nUpdate Protocol:\n"
            for key, value in instructions_obj["update_protocol"].items():
                text += f"  - {key}: {value}\n"

        if "usage_scenarios" in instructions_obj:
            text += "\nUsage Scenarios:\n"
            for key, value in instructions_obj["usage_scenarios"].items():
                text += f"  - {key}: {value}\n"

        text += "\n"
        return text

    def _build_history_section(self, message_history: list) -> str:
        """Build conversation history section.

        Args:
            message_history: List of {role: str, content: str} dicts

        Returns:
            Formatted history section
        """
        history_lines = []
        for msg in message_history:
            role = msg.get('role', 'unknown').upper()
            content = msg.get('content', '')
            history_lines.append(f"{role}: {content}")

        return (
            "--- CONVERSATION HISTORY ---\n"
            f'{"\n\n".join(history_lines)}\n'
            "--- END OF CONVERSATION HISTORY ---\n"
        )

    def _get_peer_label(self, peer_id: str) -> str:
        """Get peer label with name if available.

        Args:
            peer_id: Node ID

        Returns:
            Formatted label (e.g., "Alice (dpc-node-...)" or just node_id)
        """
        if peer_id in self.peer_metadata:
            peer_name = self.peer_metadata[peer_id].get('name')
            if peer_name:
                return f"{peer_name} ({peer_id})"
        return peer_id
