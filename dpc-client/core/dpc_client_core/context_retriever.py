"""
Context Retriever - Phase 5.1

Implements @-mention syntax for explicit context selection (inspired by Cursor/Windsurf).
Allows users to reference specific contexts, topics, or users in their queries.

Examples:
  - @user:alice - Get Alice's full context
  - @topic:game_design - Get game design topic from all accessible contexts
  - @topic:python:classes - Get specific subtopic (classes from python topic)
  - @all - Get all available contexts
"""

import re
from typing import Dict, List, Optional, Any, Tuple
from dataclasses import dataclass

from dpc_protocol.pcm_core import PersonalContext, Topic


@dataclass
class MentionedContext:
    """Represents a mentioned context to retrieve"""
    type: str  # "user", "topic", "all"
    identifier: str  # user node_id, topic name, or "*"
    subtopic: Optional[str] = None  # For @topic:python:classes
    relevance: float = 1.0  # Relevance score (for semantic search)


@dataclass
class RetrievedContext:
    """Retrieved context data"""
    source_id: str  # node_id or "local"
    source_name: str  # Display name
    context_type: str  # "full", "topic", "subtopic"
    data: Any  # PersonalContext or Topic object
    relevance: float = 1.0


class ContextRetriever:
    """Retrieves contexts based on @-mention syntax

    Supports:
    - @user:alice - Get user's context
    - @topic:game_design - Get topic from all contexts
    - @topic:python:classes - Get specific subtopic
    - @all - Get all available contexts
    """

    def __init__(self, local_context: PersonalContext, local_node_id: str):
        """Initialize context retriever

        Args:
            local_context: Local user's PersonalContext
            local_node_id: Local node identifier
        """
        self.local_context = local_context
        self.local_node_id = local_node_id

        # Available peer contexts (populated externally)
        self.peer_contexts: Dict[str, PersonalContext] = {}  # node_id -> context
        self.peer_names: Dict[str, str] = {}  # node_id -> name

    def parse_mentions(self, message: str) -> List[MentionedContext]:
        """Parse @-mentions from message

        Supported patterns:
        - @user:alice
        - @topic:game_design
        - @topic:python:classes
        - @all

        Args:
            message: Message text to parse

        Returns:
            List of MentionedContext objects
        """
        mentions = []

        # Pattern: @user:username
        user_pattern = r'@user:([a-zA-Z0-9_-]+)'
        for match in re.finditer(user_pattern, message):
            mentions.append(MentionedContext(
                type="user",
                identifier=match.group(1)
            ))

        # Pattern: @topic:topic_name or @topic:topic_name:subtopic
        topic_pattern = r'@topic:([a-zA-Z0-9_-]+)(?::([a-zA-Z0-9_-]+))?'
        for match in re.finditer(topic_pattern, message):
            topic_name = match.group(1)
            subtopic = match.group(2) if match.group(2) else None
            mentions.append(MentionedContext(
                type="topic",
                identifier=topic_name,
                subtopic=subtopic
            ))

        # Pattern: @all
        if '@all' in message:
            mentions.append(MentionedContext(
                type="all",
                identifier="*"
            ))

        return mentions

    async def retrieve_contexts(
        self,
        mentions: List[MentionedContext]
    ) -> Dict[str, RetrievedContext]:
        """Retrieve all mentioned contexts

        Args:
            mentions: List of MentionedContext objects

        Returns:
            Dictionary mapping source_id to RetrievedContext
        """
        retrieved = {}

        for mention in mentions:
            if mention.type == "user":
                result = await self._retrieve_user_context(mention.identifier)
                if result:
                    retrieved[result.source_id] = result

            elif mention.type == "topic":
                results = await self._retrieve_topic_contexts(mention.identifier, mention.subtopic)
                for result in results:
                    # Use compound key for topics to avoid collisions
                    key = f"{result.source_id}:{mention.identifier}"
                    retrieved[key] = result

            elif mention.type == "all":
                results = await self._retrieve_all_contexts()
                for result in results:
                    retrieved[result.source_id] = result

        return retrieved

    async def _retrieve_user_context(self, node_id: str) -> Optional[RetrievedContext]:
        """Retrieve specific user's full context

        Args:
            node_id: Node identifier

        Returns:
            RetrievedContext or None
        """
        # Check if it's local user
        if node_id == self.local_node_id or node_id == "local":
            return RetrievedContext(
                source_id=self.local_node_id,
                source_name=self.local_context.profile.name,
                context_type="full",
                data=self.local_context
            )

        # Check peer contexts
        if node_id in self.peer_contexts:
            return RetrievedContext(
                source_id=node_id,
                source_name=self.peer_names.get(node_id, node_id),
                context_type="full",
                data=self.peer_contexts[node_id]
            )

        return None

    async def _retrieve_topic_contexts(
        self,
        topic_name: str,
        subtopic: Optional[str] = None
    ) -> List[RetrievedContext]:
        """Retrieve topic from all available contexts

        Args:
            topic_name: Topic identifier
            subtopic: Optional subtopic filter

        Returns:
            List of RetrievedContext objects
        """
        results = []

        # Search local context
        if topic_name in self.local_context.knowledge:
            topic = self.local_context.knowledge[topic_name]
            results.append(RetrievedContext(
                source_id=self.local_node_id,
                source_name=self.local_context.profile.name,
                context_type="topic",
                data=topic
            ))

        # Search peer contexts
        for node_id, context in self.peer_contexts.items():
            if topic_name in context.knowledge:
                topic = context.knowledge[topic_name]
                results.append(RetrievedContext(
                    source_id=node_id,
                    source_name=self.peer_names.get(node_id, node_id),
                    context_type="topic",
                    data=topic
                ))

        # Filter by subtopic if specified
        if subtopic and results:
            # This is a simplification - could do more sophisticated filtering
            filtered_results = []
            for result in results:
                topic = result.data
                # Check if any entries match subtopic
                matching_entries = [
                    e for e in topic.entries
                    if subtopic.lower() in e.content.lower() or
                    any(subtopic.lower() in tag.lower() for tag in e.tags)
                ]
                if matching_entries:
                    # Create filtered topic
                    filtered_topic = Topic(
                        summary=topic.summary,
                        entries=matching_entries,
                        key_books=topic.key_books,
                        preferred_authors=topic.preferred_authors,
                        mastery_level=topic.mastery_level,
                        version=topic.version
                    )
                    result.data = filtered_topic
                    result.context_type = "subtopic"
                    filtered_results.append(result)
            results = filtered_results

        return results

    async def _retrieve_all_contexts(self) -> List[RetrievedContext]:
        """Retrieve all available contexts

        Returns:
            List of RetrievedContext objects
        """
        results = []

        # Add local context
        results.append(RetrievedContext(
            source_id=self.local_node_id,
            source_name=self.local_context.profile.name,
            context_type="full",
            data=self.local_context
        ))

        # Add all peer contexts
        for node_id, context in self.peer_contexts.items():
            results.append(RetrievedContext(
                source_id=node_id,
                source_name=self.peer_names.get(node_id, node_id),
                context_type="full",
                data=context
            ))

        return results

    def add_peer_context(self, node_id: str, name: str, context: PersonalContext):
        """Add peer context to available contexts

        Args:
            node_id: Peer node identifier
            name: Peer display name
            context: PersonalContext object
        """
        self.peer_contexts[node_id] = context
        self.peer_names[node_id] = name

    def remove_peer_context(self, node_id: str):
        """Remove peer context

        Args:
            node_id: Peer node identifier
        """
        if node_id in self.peer_contexts:
            del self.peer_contexts[node_id]
        if node_id in self.peer_names:
            del self.peer_names[node_id]

    def format_context_for_prompt(
        self,
        retrieved: Dict[str, RetrievedContext]
    ) -> str:
        """Format retrieved contexts for LLM prompt

        Args:
            retrieved: Dictionary of retrieved contexts

        Returns:
            Formatted string for prompt
        """
        lines = []
        lines.append("REFERENCED CONTEXTS:")
        lines.append("")

        for key, ctx in retrieved.items():
            lines.append(f"--- {ctx.source_name} ({ctx.context_type}) ---")

            if ctx.context_type == "full":
                # Format full context
                context = ctx.data
                lines.append(f"Profile: {context.profile.name} - {context.profile.description}")
                lines.append(f"Topics: {', '.join(context.knowledge.keys())}")

            elif ctx.context_type in ["topic", "subtopic"]:
                # Format topic
                topic = ctx.data
                lines.append(f"Summary: {topic.summary}")
                lines.append(f"Entries: {len(topic.entries)}")
                if topic.entries:
                    lines.append("Key points:")
                    for i, entry in enumerate(topic.entries[:3], 1):  # Show first 3
                        lines.append(f"  {i}. {entry.content[:100]}...")

            lines.append("")

        return "\n".join(lines)


# Example usage
if __name__ == '__main__':
    import asyncio
    from dpc_protocol.pcm_core import Profile, Book, KnowledgeEntry, KnowledgeSource

    async def demo():
        print("=== ContextRetriever Demo ===\n")

        # Create local context
        local_context = PersonalContext(
            profile=Profile(name="Alice", description="Game designer"),
            knowledge={
                "game_design": Topic(
                    summary="Game design principles",
                    entries=[
                        KnowledgeEntry(
                            content="Environmental storytelling is powerful",
                            tags=["narrative"]
                        )
                    ]
                ),
                "python": Topic(
                    summary="Python programming knowledge",
                    entries=[
                        KnowledgeEntry(
                            content="Classes are blueprints for objects",
                            tags=["oop", "classes"]
                        )
                    ]
                )
            }
        )

        # Create retriever
        retriever = ContextRetriever(
            local_context=local_context,
            local_node_id="alice-node-123"
        )

        # Add peer context
        bob_context = PersonalContext(
            profile=Profile(name="Bob", description="Developer"),
            knowledge={
                "game_design": Topic(
                    summary="Game mechanics",
                    entries=[
                        KnowledgeEntry(
                            content="Puzzle difficulty should scale gradually",
                            tags=["puzzles"]
                        )
                    ]
                )
            }
        )
        retriever.add_peer_context("bob-node-456", "Bob", bob_context)

        # Test @-mention parsing
        print("1. Parsing mentions:")
        test_messages = [
            "What does @user:bob think about this?",
            "Show me @topic:game_design from everyone",
            "Tell me about @topic:python:classes",
            "Get @all contexts"
        ]

        for msg in test_messages:
            mentions = retriever.parse_mentions(msg)
            print(f"   '{msg}'")
            for m in mentions:
                print(f"     -> {m.type}: {m.identifier}" + (f":{m.subtopic}" if m.subtopic else ""))
        print()

        # Test context retrieval
        print("2. Retrieving contexts:")

        # Retrieve user
        mentions = retriever.parse_mentions("@user:bob")
        retrieved = await retriever.retrieve_contexts(mentions)
        print(f"   @user:bob -> {len(retrieved)} context(s)")
        for key, ctx in retrieved.items():
            print(f"     {ctx.source_name}: {ctx.context_type}")
        print()

        # Retrieve topic
        mentions = retriever.parse_mentions("@topic:game_design")
        retrieved = await retriever.retrieve_contexts(mentions)
        print(f"   @topic:game_design -> {len(retrieved)} context(s)")
        for key, ctx in retrieved.items():
            print(f"     {ctx.source_name}: {len(ctx.data.entries)} entries")
        print()

        # Retrieve subtopic
        mentions = retriever.parse_mentions("@topic:python:classes")
        retrieved = await retriever.retrieve_contexts(mentions)
        print(f"   @topic:python:classes -> {len(retrieved)} context(s)")
        for key, ctx in retrieved.items():
            print(f"     {ctx.source_name}: {len(ctx.data.entries)} matching entries")
        print()

        # Format for prompt
        print("3. Formatted for prompt:")
        mentions = retriever.parse_mentions("@topic:game_design")
        retrieved = await retriever.retrieve_contexts(mentions)
        formatted = retriever.format_context_for_prompt(retrieved)
        print(formatted)

    asyncio.run(demo())
    print("=== Demo Complete ===")
