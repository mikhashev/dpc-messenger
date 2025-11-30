"""
Markdown Knowledge Manager - Phase 3

Manages Claude Code-style markdown files for human-readable knowledge storage.
Provides bidirectional sync between JSON PCM data and markdown files.
"""

import logging
import re
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)
from typing import List, Optional, Dict, Any, Tuple
from datetime import datetime

from .pcm_core import (
    PersonalContext,
    Topic,
    KnowledgeEntry,
    KnowledgeSource
)
from .crypto import DPC_HOME_DIR


class MarkdownKnowledgeManager:
    """Manages markdown files for knowledge topics (Claude Code pattern)"""

    def __init__(self, knowledge_dir: Optional[Path] = None):
        """Initialize markdown manager

        Args:
            knowledge_dir: Directory for markdown files (default: ~/.dpc/knowledge/)
        """
        if knowledge_dir:
            self.knowledge_dir = Path(knowledge_dir)
        else:
            self.knowledge_dir = DPC_HOME_DIR / "knowledge"

        self.knowledge_dir.mkdir(parents=True, exist_ok=True)

    def sanitize_filename(self, text: str, max_length: int = 50) -> str:
        """Convert text to safe filename

        Args:
            text: Text to convert
            max_length: Maximum filename length

        Returns:
            Safe filename without extension
        """
        # Convert to lowercase, replace spaces with underscores
        filename = text.lower().replace(' ', '_')

        # Remove non-alphanumeric characters (except underscore and hyphen)
        filename = re.sub(r'[^\w\-]', '', filename)

        # Truncate to max length
        return filename[:max_length]

    def create_topic_file(
        self,
        topic: Topic,
        topic_name: str,
        commit_info: Optional[Dict[str, Any]] = None
    ) -> Path:
        """Create markdown file for a new topic

        Args:
            topic: Topic object to create file for
            topic_name: Name of the topic
            commit_info: Optional commit information

        Returns:
            Path to created markdown file
        """
        # Generate filename
        filename = self.sanitize_filename(topic_name)
        filepath = self.knowledge_dir / f"{filename}.md"

        # Build markdown content
        content = self._build_topic_markdown(topic, topic_name, commit_info)

        # Write file
        filepath.write_text(content, encoding='utf-8')

        return filepath

    def _build_topic_markdown(
        self,
        topic: Topic,
        topic_name: str,
        commit_info: Optional[Dict[str, Any]] = None
    ) -> str:
        """Build complete markdown content for a topic

        Args:
            topic: Topic object
            topic_name: Name of the topic
            commit_info: Optional commit information

        Returns:
            Markdown content as string
        """
        lines = []

        # Title
        lines.append(f"# {topic_name.replace('_', ' ').title()}")
        lines.append("")

        # Metadata header
        lines.append(f"*Created: {topic.created_at}*")
        lines.append(f"*Last Modified: {topic.last_modified}*")
        lines.append(f"*Version: {topic.version}*")
        lines.append(f"*Mastery Level: {topic.mastery_level.title()}*")

        if commit_info:
            lines.append(f"*Commit: {commit_info.get('commit_id', 'N/A')}*")

        lines.append("")
        lines.append("---")
        lines.append("")

        # Overview
        lines.append("## Overview")
        lines.append("")
        lines.append(topic.summary)
        lines.append("")

        # Key books (if any)
        if topic.key_books:
            lines.append("## Key Books")
            lines.append("")
            for book in topic.key_books:
                authors_str = ", ".join(book.authors) if book.authors else "Unknown"
                rating_stars = "⭐" * book.rating
                lines.append(f"- **{book.title}** by {authors_str} {rating_stars}")
            lines.append("")

        # Preferred authors (if any)
        if topic.preferred_authors:
            lines.append("## Preferred Authors")
            lines.append("")
            for author in topic.preferred_authors:
                lines.append(f"- {author}")
            lines.append("")

        # Learning strategies (if any)
        if topic.learning_strategies:
            lines.append("## Learning Strategies")
            lines.append("")
            for strategy in topic.learning_strategies:
                lines.append(f"- {strategy}")
            lines.append("")

        lines.append("---")
        lines.append("")

        # Knowledge Entries
        if topic.entries:
            lines.append("## Knowledge Entries")
            lines.append("")

            for i, entry in enumerate(topic.entries, 1):
                lines.extend(self._format_knowledge_entry(entry, i))

        return "\n".join(lines)

    def _format_knowledge_entry(self, entry: KnowledgeEntry, index: int) -> List[str]:
        """Format a single knowledge entry as markdown

        Args:
            entry: KnowledgeEntry object
            index: Entry number

        Returns:
            List of markdown lines
        """
        lines = []

        # Entry header
        tag_label = entry.tags[0] if entry.tags else f"Entry {index}"
        lines.append(f"### {tag_label.replace('_', ' ').title()}")
        lines.append("")

        # Metadata
        lines.append(f"*Last Updated: {entry.last_updated}*")
        lines.append(f"*Confidence: {entry.confidence:.0%}*")

        if entry.source:
            source_type = entry.source.type.replace('_', ' ').title()
            lines.append(f"*Source: {source_type}*")

            if entry.source.participants:
                lines.append(f"*Participants: {', '.join(entry.source.participants)}*")

            if entry.source.cultural_perspectives_considered:
                perspectives = ', '.join(entry.source.cultural_perspectives_considered)
                lines.append(f"*Cultural Perspectives: {perspectives}*")

        if entry.cultural_specific:
            context_str = ', '.join(entry.requires_context)
            lines.append(f"*⚠️ Cultural Context Required: {context_str}*")

        lines.append("")

        # Content
        lines.append(entry.content)
        lines.append("")

        # Tags
        if len(entry.tags) > 1:
            lines.append(f"**Tags:** {', '.join(entry.tags)}")
            lines.append("")

        # Alternative viewpoints
        if entry.alternative_viewpoints:
            lines.append("**Alternative Viewpoints:**")
            for viewpoint in entry.alternative_viewpoints:
                lines.append(f"- {viewpoint}")
            lines.append("")

        # Self-improvement metrics
        if entry.usage_count > 0:
            lines.append(f"*Used {entry.usage_count} times | Effectiveness: {entry.effectiveness_score:.0%}*")
            lines.append("")

        lines.append("---")
        lines.append("")

        return lines

    def update_topic_file(
        self,
        filepath: Path,
        new_entries: List[KnowledgeEntry],
        update_metadata: Optional[Dict[str, Any]] = None
    ) -> None:
        """Append new entries to existing markdown file

        Args:
            filepath: Path to markdown file
            new_entries: List of new KnowledgeEntry objects to add
            update_metadata: Optional metadata to update in header
        """
        if not filepath.exists():
            raise FileNotFoundError(f"Markdown file not found: {filepath}")

        current_content = filepath.read_text(encoding='utf-8')

        # Find "## Knowledge Entries" section
        entries_marker = "## Knowledge Entries"
        insert_pos = current_content.find(entries_marker)

        if insert_pos == -1:
            # No entries section yet, add one at the end
            new_content = current_content.rstrip() + "\n\n---\n\n## Knowledge Entries\n\n"
            # Count as starting from entry 1
            start_index = 1
        else:
            # Find where to insert (after last entry, before next section or EOF)
            next_section_pos = current_content.find("\n## ", insert_pos + len(entries_marker))
            if next_section_pos == -1:
                insert_pos = len(current_content)
            else:
                insert_pos = next_section_pos

            # Count existing entries
            existing_entries = current_content[:insert_pos].count("### ")
            start_index = existing_entries + 1

            new_content = current_content[:insert_pos]

        # Append new entries
        for i, entry in enumerate(new_entries, start_index):
            entry_lines = self._format_knowledge_entry(entry, i)
            new_content += "\n".join(entry_lines)

        # Append remaining content if there was a next section
        if insert_pos < len(current_content) and current_content[insert_pos:].strip():
            new_content += current_content[insert_pos:]

        # Update metadata in header if provided
        if update_metadata:
            new_content = self._update_markdown_metadata(new_content, update_metadata)

        # Write updated file
        filepath.write_text(new_content, encoding='utf-8')

    def _update_markdown_metadata(self, content: str, metadata: Dict[str, Any]) -> str:
        """Update metadata fields in markdown header

        Args:
            content: Current markdown content
            metadata: Dictionary of metadata to update

        Returns:
            Updated markdown content
        """
        lines = content.split('\n')

        # Update specific metadata lines
        if 'last_modified' in metadata:
            for i, line in enumerate(lines):
                if line.startswith('*Last Modified:'):
                    lines[i] = f"*Last Modified: {metadata['last_modified']}*"
                    break

        if 'version' in metadata:
            for i, line in enumerate(lines):
                if line.startswith('*Version:'):
                    lines[i] = f"*Version: {metadata['version']}*"
                    break

        return '\n'.join(lines)

    def sync_context_to_markdown(self, context: PersonalContext) -> Dict[str, Path]:
        """Sync entire PersonalContext to markdown files

        Args:
            context: PersonalContext object

        Returns:
            Dictionary mapping topic names to markdown file paths
        """
        synced_files = {}

        for topic_name, topic in context.knowledge.items():
            # Check if topic already has a markdown file
            if topic.markdown_file:
                filepath = self.knowledge_dir / topic.markdown_file.split('/')[-1]
            else:
                # Create new file
                filepath = self.create_topic_file(topic, topic_name)
                # Update topic to reference the file
                topic.markdown_file = f"knowledge/{filepath.name}"

            synced_files[topic_name] = filepath

        return synced_files

    def read_markdown_file(self, filepath: Path) -> str:
        """Read markdown file content

        Args:
            filepath: Path to markdown file

        Returns:
            File content as string
        """
        if not filepath.exists():
            raise FileNotFoundError(f"Markdown file not found: {filepath}")

        return filepath.read_text(encoding='utf-8')

    def list_markdown_files(self) -> List[Path]:
        """List all markdown files in knowledge directory

        Returns:
            List of Path objects for markdown files
        """
        return sorted(self.knowledge_dir.glob("*.md"))

    def delete_markdown_file(self, filepath: Path) -> bool:
        """Delete a markdown file

        Args:
            filepath: Path to file to delete

        Returns:
            True if file was deleted, False if file didn't exist
        """
        if filepath.exists():
            filepath.unlink()
            return True
        return False

    def topic_to_markdown_content(self, topic: Topic) -> str:
        """
        Generate markdown content for a topic (without frontmatter).

        This is used for computing content hashes and for
        creating versioned markdown files.

        Args:
            topic: Topic object

        Returns:
            Markdown content as string (no frontmatter)
        """
        lines = []

        # Overview
        lines.append("## Overview")
        lines.append("")
        lines.append(topic.summary)
        lines.append("")

        # Key books (if any)
        if topic.key_books:
            lines.append("## Key Books")
            lines.append("")
            for book in topic.key_books:
                authors_str = ", ".join(book.authors) if book.authors else "Unknown"
                rating_stars = "⭐" * book.rating
                lines.append(f"- **{book.title}** by {authors_str} {rating_stars}")
            lines.append("")

        # Preferred authors (if any)
        if topic.preferred_authors:
            lines.append("## Preferred Authors")
            lines.append("")
            for author in topic.preferred_authors:
                lines.append(f"- {author}")
            lines.append("")

        # Learning strategies (if any)
        if topic.learning_strategies:
            lines.append("## Learning Strategies")
            lines.append("")
            for strategy in topic.learning_strategies:
                lines.append(f"- {strategy}")
            lines.append("")

        lines.append("---")
        lines.append("")

        # Knowledge Entries
        if topic.entries:
            lines.append("## Knowledge Entries")
            lines.append("")

            for i, entry in enumerate(topic.entries, 1):
                lines.extend(self._format_knowledge_entry(entry, i))

        return "\n".join(lines)

    def build_markdown_with_frontmatter(
        self,
        frontmatter: Dict[str, Any],
        content: str
    ) -> str:
        """
        Build markdown content with YAML frontmatter (returns string).

        Format:
            ---
            key: value
            list_key:
              - item1
              - item2
            dict_key:
              key1: value1
              key2: value2
            ---

            # Markdown content here

        Args:
            frontmatter: Dictionary of frontmatter metadata
            content: Markdown content

        Returns:
            Complete markdown with frontmatter as string
        """
        lines = ["---"]

        # Convert frontmatter to YAML-style format
        lines.append("# Commit Identification")
        if 'topic' in frontmatter:
            lines.append(f"topic: {frontmatter['topic']}")
        if 'commit_id' in frontmatter:
            lines.append(f"commit_id: {frontmatter['commit_id']}")
        if 'commit_hash' in frontmatter:
            lines.append(f"commit_hash: {frontmatter['commit_hash']}")
        if 'parent_commit' in frontmatter:
            lines.append(f"parent_commit: {frontmatter['parent_commit']}")
        lines.append("")

        # Integrity Verification
        lines.append("# Integrity Verification")
        if 'content_hash' in frontmatter:
            lines.append(f"content_hash: {frontmatter['content_hash']}")
        lines.append("")

        # Metadata
        lines.append("# Metadata")
        if 'timestamp' in frontmatter:
            lines.append(f"timestamp: {frontmatter['timestamp']}")
        if 'version' in frontmatter:
            lines.append(f"version: {frontmatter['version']}")
        if 'author' in frontmatter:
            lines.append(f"author: {frontmatter['author']}")
        if 'created_at' in frontmatter:
            lines.append(f"created_at: {frontmatter['created_at']}")
        if 'last_modified' in frontmatter:
            lines.append(f"last_modified: {frontmatter['last_modified']}")
        if 'mastery_level' in frontmatter:
            lines.append(f"mastery_level: {frontmatter['mastery_level']}")
        lines.append("")

        # Consensus Tracking
        lines.append("# Consensus Tracking")
        if 'participants' in frontmatter:
            lines.append("participants:")
            for p in frontmatter['participants']:
                lines.append(f"  - {p}")
        if 'approved_by' in frontmatter:
            lines.append("approved_by:")
            for a in frontmatter['approved_by']:
                lines.append(f"  - {a}")
        if 'rejected_by' in frontmatter:
            lines.append("rejected_by:")
            if frontmatter['rejected_by']:
                for r in frontmatter['rejected_by']:
                    lines.append(f"  - {r}")
            else:
                lines.append("  []")
        if 'consensus' in frontmatter:
            lines.append(f"consensus: {frontmatter['consensus']}")
        if 'confidence_score' in frontmatter:
            lines.append(f"confidence_score: {frontmatter['confidence_score']}")
        lines.append("")

        # Cryptographic Signatures
        if 'signatures' in frontmatter and frontmatter['signatures']:
            lines.append("# Cryptographic Signatures")
            lines.append("signatures:")
            for node_id, signature in frontmatter['signatures'].items():
                lines.append(f"  {node_id}: \"{signature}\"")
            lines.append("")

        # Cultural Context
        if 'cultural_perspectives' in frontmatter and frontmatter['cultural_perspectives']:
            lines.append("# Cultural Context")
            lines.append("cultural_perspectives:")
            for p in frontmatter['cultural_perspectives']:
                lines.append(f"  - {p}")
            lines.append("")

        lines.append("---")
        lines.append("")

        # Add topic title
        if 'topic' in frontmatter:
            topic_title = frontmatter['topic'].replace('_', ' ').title()
            lines.append(f"# {topic_title}")
            lines.append("")

        # Add content
        lines.append(content)

        return "\n".join(lines)

    def write_markdown_with_frontmatter(
        self,
        filepath: Path,
        frontmatter: Dict[str, Any],
        content: str
    ) -> None:
        """
        Write markdown file with YAML frontmatter.

        Format:
            ---
            key: value
            list_key:
              - item1
              - item2
            dict_key:
              key1: value1
              key2: value2
            ---

            # Markdown content here

        Args:
            filepath: Path to markdown file
            frontmatter: Dictionary of frontmatter metadata
            content: Markdown content
        """
        full_content = self.build_markdown_with_frontmatter(frontmatter, content)
        # Force LF line endings for consistent hashing across platforms
        with open(filepath, 'w', encoding='utf-8', newline='\n') as f:
            f.write(full_content)

    def parse_markdown_with_frontmatter(self, filepath: Path) -> Tuple[Dict[str, Any], str]:
        """
        Parse markdown file with YAML frontmatter.

        Args:
            filepath: Path to markdown file

        Returns:
            Tuple of (frontmatter_dict, content_string)
        """
        if not filepath.exists():
            raise FileNotFoundError(f"Markdown file not found: {filepath}")

        # Read with universal newlines to handle both LF and CRLF
        content = filepath.read_text(encoding='utf-8')

        # Check for frontmatter
        if not content.startswith('---'):
            return {}, content

        # Find end of frontmatter
        end_marker = content.find('\n---\n', 3)
        if end_marker == -1:
            end_marker = content.find('\n---\r\n', 3)
        if end_marker == -1:
            return {}, content

        # Extract frontmatter and content
        frontmatter_text = content[4:end_marker]
        markdown_content = content[end_marker + 5:].lstrip()  # Only strip leading whitespace

        # Remove topic title if present (added by build_markdown_with_frontmatter)
        # Title format: "# Topic Name\n\n"
        if markdown_content.startswith('# '):
            # Find first line break
            first_newline = markdown_content.find('\n')
            if first_newline != -1:
                # Skip title line and any following blank lines
                markdown_content = markdown_content[first_newline + 1:].lstrip('\n')

        # Parse YAML frontmatter
        try:
            frontmatter = yaml.safe_load(frontmatter_text)
            if frontmatter is None:
                frontmatter = {}
        except yaml.YAMLError:
            frontmatter = {}

        return frontmatter, markdown_content

    def markdown_to_entries(self, content: str) -> List[KnowledgeEntry]:
        """
        Parse markdown content back into KnowledgeEntry objects.

        Args:
            content: Markdown content (without frontmatter)

        Returns:
            List of KnowledgeEntry objects
        """
        entries = []

        # Find all ### headers (knowledge entries)
        lines = content.split('\n')
        current_entry = None
        current_content_lines = []
        current_tags = []
        current_confidence = 0.95
        current_timestamp = datetime.utcnow().isoformat()

        i = 0
        while i < len(lines):
            line = lines[i].strip()

            # Start of new entry
            if line.startswith('### '):
                # Save previous entry
                if current_entry is not None and current_content_lines:
                    current_entry.content = '\n'.join(current_content_lines).strip()
                    current_entry.tags = current_tags if current_tags else ['general']
                    current_entry.confidence = current_confidence
                    current_entry.last_updated = current_timestamp
                    entries.append(current_entry)

                # Start new entry
                tag = line[4:].strip().lower().replace(' ', '_')
                current_tags = [tag]
                current_content_lines = []
                current_entry = KnowledgeEntry(
                    content="",
                    tags=[],
                    confidence=0.95,
                    last_updated=datetime.utcnow().isoformat()
                )

            # Parse metadata lines
            elif line.startswith('*Last Updated:'):
                match = re.search(r'\*Last Updated:\s*(.+?)\*', line)
                if match:
                    current_timestamp = match.group(1)

            elif line.startswith('*Confidence:'):
                match = re.search(r'\*Confidence:\s*(\d+)%\*', line)
                if match:
                    current_confidence = int(match.group(1)) / 100.0

            elif line.startswith('**Tags:**'):
                tags_text = line.replace('**Tags:**', '').strip()
                current_tags = [t.strip() for t in tags_text.split(',')]

            # Content lines
            elif current_entry is not None and line and not line.startswith('*') and not line.startswith('**') and not line.startswith('---'):
                current_content_lines.append(line)

            i += 1

        # Save last entry
        if current_entry is not None and current_content_lines:
            current_entry.content = '\n'.join(current_content_lines).strip()
            current_entry.tags = current_tags if current_tags else ['general']
            current_entry.confidence = current_confidence
            current_entry.last_updated = current_timestamp
            entries.append(current_entry)

        return entries


# Example usage
if __name__ == '__main__':
    from dpc_protocol.pcm_core import Book

    # Create manager
    manager = MarkdownKnowledgeManager()

    logger.info("Knowledge directory: %s", manager.knowledge_dir)

    # Create example topic
    topic = Topic(
        summary="Game design principles and patterns",
        key_books=[
            Book(title="The Art of Game Design", rating=5, authors=["Jesse Schell"])
        ],
        mastery_level="intermediate",
        entries=[
            KnowledgeEntry(
                content="Environmental storytelling is more powerful than explicit exposition.",
                tags=["narrative_design", "environmental_storytelling"],
                confidence=0.95,
                source=KnowledgeSource(
                    type="ai_summary",
                    participants=["alice", "bob"],
                    cultural_perspectives_considered=["Western", "Eastern"]
                ),
                alternative_viewpoints=[
                    "Explicit narrative works better for complex lore",
                    "Audio logs provide good middle ground"
                ]
            )
        ],
        learning_strategies=["Analyze successful games", "Prototype quickly"]
    )

    # Create markdown file
    filepath = manager.create_topic_file(topic, "game_design_philosophy")
    logger.info("Created: %s", filepath)

    # Read it back
    content = manager.read_markdown_file(filepath)
    logger.info("Content Preview")
    logger.info("File length: %d characters", len(content))
    logger.info("First line: %s", content.split(chr(10))[0])

    # Add another entry
    new_entry = KnowledgeEntry(
        content="Puzzle difficulty should scale gradually to maintain flow state.",
        tags=["puzzle_design", "difficulty_curve"],
        confidence=0.90
    )

    manager.update_topic_file(filepath, [new_entry], update_metadata={
        'version': 2,
        'last_modified': datetime.utcnow().isoformat()
    })

    logger.info("Updated: %s", filepath)

    # List all files
    files = manager.list_markdown_files()
    logger.info("Markdown files: %d", len(files))
    for f in files:
        logger.info("  %s", f.name)

    # Cleanup example file
    manager.delete_markdown_file(filepath)
    logger.info("Cleaned up example file")
