"""
Instruction Manager - Instruction Set Management

Manages AI instruction sets:
- Loading and saving instruction sets
- Creating, deleting, renaming instruction sets
- Setting default instruction set
- Importing templates from personal-context-manager format
- Hot-reload support for live updates

Extracted to follow the manager pattern (consistent with LLMManager, P2PManager, etc.)
"""

import logging
from pathlib import Path
from typing import Dict, Any, Optional
from dataclasses import asdict

from dpc_protocol.pcm_core import (
    InstructionBlock, InstructionSet, InstructionSetManager
)

logger = logging.getLogger(__name__)


class InstructionManager:
    """High-level manager for instruction sets with event broadcasting support.

    Responsibilities:
    - Wrap InstructionSetManager for service-level operations
    - Provide commands for UI (get/save/create/delete/rename)
    - Handle hot-reload and event broadcasting
    - Maintain in-memory cache for fast access
    """

    def __init__(self, config_dir: Path, event_broadcaster=None):
        """Initialize InstructionManager.

        Args:
            config_dir: Directory containing instructions.json
            event_broadcaster: Optional LocalApiServer for broadcasting events to UI
        """
        self.config_dir = config_dir
        self.event_broadcaster = event_broadcaster

        # Core manager from protocol library
        self.instruction_set_manager = InstructionSetManager(config_dir)

        # Cached instruction set (for fast access)
        self.instruction_set: Optional[InstructionSet] = None

        # Load instruction sets on initialization
        self.load()

    def load(self) -> InstructionSet:
        """Load instruction sets from disk.

        Returns:
            InstructionSet instance
        """
        self.instruction_set = self.instruction_set_manager.load()
        logger.info("Loaded %d instruction set(s)", len(self.instruction_set.sets))
        return self.instruction_set

    def get_all(self) -> Dict[str, Any]:
        """Get all instruction sets for UI display.

        Returns:
            Dict with schema_version, default, and sets
        """
        if self.instruction_set is None:
            self.load()

        return {
            "schema_version": self.instruction_set.schema_version,
            "default": self.instruction_set.default,
            "sets": {
                key: asdict(inst_block)
                for key, inst_block in self.instruction_set.sets.items()
            }
        }

    def get_set(self, set_key: str) -> Optional[Dict[str, Any]]:
        """Get a specific instruction set.

        Args:
            set_key: Key of the instruction set

        Returns:
            Dict representation of InstructionBlock or None
        """
        if self.instruction_set is None:
            self.load()

        inst_block = self.instruction_set.get_set(set_key)
        return asdict(inst_block) if inst_block else None

    def save_set(self, set_key: str, instruction_data: Dict[str, Any]) -> bool:
        """Save/update a specific instruction set.

        Args:
            set_key: Key of the instruction set
            instruction_data: Dictionary representation of InstructionBlock

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.instruction_set is None:
                self.load()

            # Create or update InstructionBlock
            inst_block = InstructionBlock(
                name=instruction_data.get('name', 'Unnamed'),
                description=instruction_data.get('description', ''),
                created=instruction_data.get('created', ''),
                last_updated=instruction_data.get('last_updated', ''),
                primary=instruction_data.get('primary', InstructionBlock().primary),
                context_update=instruction_data.get('context_update', InstructionBlock().context_update),
                verification_protocol=instruction_data.get('verification_protocol', InstructionBlock().verification_protocol),
                learning_support=instruction_data.get('learning_support', InstructionBlock().learning_support),
                bias_mitigation=instruction_data.get('bias_mitigation', InstructionBlock().bias_mitigation),
                collaboration_mode=instruction_data.get('collaboration_mode', 'individual'),
                consensus_required=instruction_data.get('consensus_required', True),
                ai_curation_enabled=instruction_data.get('ai_curation_enabled', True),
                dissent_encouraged=instruction_data.get('dissent_encouraged', True)
            )

            # Update in instruction set
            self.instruction_set.sets[set_key] = inst_block

            # Save to disk
            self.instruction_set_manager.save(self.instruction_set)

            # Broadcast event to UI
            if self.event_broadcaster:
                self.event_broadcaster.broadcast_event("instruction_set_updated", {
                    "set_key": set_key,
                    "set_name": inst_block.name
                })

            logger.info("Saved instruction set '%s'", set_key)
            return True

        except Exception as e:
            logger.error("Error saving instruction set '%s': %s", set_key, e, exc_info=True)
            return False

    def create_set(self, set_key: str, name: str, description: str = "") -> Optional[Dict[str, Any]]:
        """Create a new instruction set.

        Args:
            set_key: Key for the new instruction set
            name: Display name
            description: Description

        Returns:
            Dict representation of created InstructionBlock or None
        """
        try:
            if self.instruction_set is None:
                self.load()

            # Create new instruction set
            inst_block = self.instruction_set.create_set(set_key, name, description)

            # Save to disk
            self.instruction_set_manager.save(self.instruction_set)

            # Broadcast event to UI
            if self.event_broadcaster:
                self.event_broadcaster.broadcast_event("instruction_set_created", {
                    "set_key": set_key,
                    "set_name": name
                })

            logger.info("Created instruction set '%s'", set_key)
            return asdict(inst_block)

        except Exception as e:
            logger.error("Error creating instruction set '%s': %s", set_key, e, exc_info=True)
            return None

    def delete_set(self, set_key: str) -> bool:
        """Delete an instruction set.

        Args:
            set_key: Key of the instruction set to delete

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.instruction_set is None:
                self.load()

            # Delete from instruction set
            success = self.instruction_set.delete_set(set_key)

            if success:
                # Save to disk
                self.instruction_set_manager.save(self.instruction_set)

                # Broadcast event to UI
                if self.event_broadcaster:
                    self.event_broadcaster.broadcast_event("instruction_set_deleted", {
                        "set_key": set_key
                    })

                logger.info("Deleted instruction set '%s'", set_key)

            return success

        except Exception as e:
            logger.error("Error deleting instruction set '%s': %s", set_key, e, exc_info=True)
            return False

    def rename_set(self, old_key: str, new_key: str, new_name: str) -> bool:
        """Rename an instruction set.

        Args:
            old_key: Current key
            new_key: New key
            new_name: New display name

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.instruction_set is None:
                self.load()

            # Rename in instruction set
            success = self.instruction_set.rename_set(old_key, new_key, new_name)

            if success:
                # Save to disk
                self.instruction_set_manager.save(self.instruction_set)

                # Broadcast event to UI
                if self.event_broadcaster:
                    self.event_broadcaster.broadcast_event("instruction_set_renamed", {
                        "old_key": old_key,
                        "new_key": new_key,
                        "new_name": new_name
                    })

                logger.info("Renamed instruction set '%s' to '%s'", old_key, new_key)

            return success

        except Exception as e:
            logger.error("Error renaming instruction set '%s': %s", old_key, e, exc_info=True)
            return False

    def set_default(self, set_key: str) -> bool:
        """Set the default instruction set.

        Args:
            set_key: Key of the instruction set to make default

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.instruction_set is None:
                self.load()

            # Check if set exists
            if set_key not in self.instruction_set.sets:
                logger.warning("Instruction set '%s' not found", set_key)
                return False

            # Update default
            self.instruction_set.default = set_key

            # Save to disk
            self.instruction_set_manager.save(self.instruction_set)

            # Broadcast event to UI
            if self.event_broadcaster:
                self.event_broadcaster.broadcast_event("default_instruction_set_changed", {
                    "default": set_key
                })

            logger.info("Set default instruction set to '%s'", set_key)
            return True

        except Exception as e:
            logger.error("Error setting default instruction set: %s", e, exc_info=True)
            return False

    def import_template(self, template_file: Path, set_key: str, set_name: str) -> Optional[Dict[str, Any]]:
        """Import instruction template from personal-context-manager format.

        Args:
            template_file: Path to template JSON file
            set_key: Key for the new instruction set
            set_name: Display name

        Returns:
            Dict representation of imported InstructionBlock or None
        """
        try:
            # Import via InstructionSetManager
            self.instruction_set_manager.import_template(template_file, set_key, set_name)

            # Reload to get updated instruction set
            self.load()

            # Get the imported instruction set
            inst_block = self.instruction_set.get_set(set_key)

            # Broadcast event to UI
            if self.event_broadcaster:
                self.event_broadcaster.broadcast_event("instruction_set_imported", {
                    "set_key": set_key,
                    "set_name": set_name,
                    "template_file": str(template_file)
                })

            logger.info("Imported template from %s as '%s'", template_file, set_key)
            return asdict(inst_block) if inst_block else None

        except Exception as e:
            logger.error("Error importing template: %s", e, exc_info=True)
            return None

    def reload(self) -> bool:
        """Reload instruction sets from disk (hot-reload).

        Returns:
            True if successful, False otherwise
        """
        try:
            self.load()

            # Broadcast event to UI
            if self.event_broadcaster:
                self.event_broadcaster.broadcast_event("instruction_sets_reloaded", {
                    "count": len(self.instruction_set.sets)
                })

            logger.info("Reloaded instruction sets from disk")
            return True

        except Exception as e:
            logger.error("Error reloading instruction sets: %s", e, exc_info=True)
            return False
