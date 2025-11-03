# src/pcm_core.py

import json
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import List, Dict, Any, Optional

# --- 1. Data Structure Definitions (Data Structures) ---
# We use dataclasses for strict typing and convenience.
# This is our "contract" for what personal_context.json should look like.

@dataclass
class Book:
    title: str
    rating: int
    authors: Optional[List[str]] = field(default_factory=list)

@dataclass
class Topic:
    summary: str
    key_books: List[Book] = field(default_factory=list)
    preferred_authors: List[str] = field(default_factory=list)

@dataclass
class Profile:
    name: str
    description: str
    core_values: List[str] = field(default_factory=list)

@dataclass
class Preferences:
    communication_style: str

@dataclass
class PersonalContext:
    """The main class representing the entire structure of personal_context.json."""
    profile: Profile
    knowledge: Dict[str, Topic] = field(default_factory=dict)
    preferences: Optional[Preferences] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'PersonalContext':
        """Creates a PersonalContext instance from a dictionary, handling nested structures."""
        profile_data = data.get('profile', {})
        profile = Profile(**profile_data)

        knowledge_data = data.get('knowledge', {})
        knowledge = {}
        for topic_name, topic_content in knowledge_data.items():
            books = [Book(**book_data) for book_data in topic_content.get('key_books', [])]
            knowledge[topic_name] = Topic(
                summary=topic_content.get('summary', ''),
                key_books=books,
                preferred_authors=topic_content.get('preferred_authors', [])
            )
        
        preferences_data = data.get('preferences')
        preferences = Preferences(**preferences_data) if preferences_data else None

        return cls(profile=profile, knowledge=knowledge, preferences=preferences)

# --- 2. Class for managing the context file (Core Logic) ---

class PCMCore:
    """
    Handles all operations with the personal_context.json file:
    reading, writing, and template creation.
    """
    def __init__(self, file_path: str | Path = "personal_context.json"):
        self.file_path = Path(file_path)

    def load_context(self) -> PersonalContext:
        if not self.file_path.exists():
            raise FileNotFoundError(f"Context file not found at {self.file_path}")
        
        with open(self.file_path, 'r', encoding='utf-8') as f:
            raw_data = json.load(f)
        
        return PersonalContext.from_dict(raw_data)

    def save_context(self, context: PersonalContext):
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(asdict(context), f, indent=2, ensure_ascii=False)

    def create_template(self, overwrite: bool = False):
        if self.file_path.exists() and not overwrite:
            print(f"File {self.file_path} already exists. Use --overwrite to replace it.")
            return

        template_profile = Profile(
            name="Anonymous",
            description="A person interested in learning and exploring new ideas.",
            core_values=["Learning", "Curiosity"]
        )
        template_context = PersonalContext(profile=template_profile)
        
        self.save_context(template_context)
        print(f"Template context file created at {self.file_path}")


# --- 3. Usage example (for self-testing the module) ---

if __name__ == '__main__':
    print("--- Testing PCMCore ---")
    
    test_file = "test_context.json"
    core = PCMCore(test_file)

    print("\n1. Creating template file...")
    core.create_template(overwrite=True)

    print("\n2. Loading context from file...")
    try:
        my_context = core.load_context()
        print(f"   - Successfully loaded context for user: {my_context.profile.name}")
        print(f"   - Knowledge topics: {list(my_context.knowledge.keys())}")
    except Exception as e:
        print(f"   - Error loading context: {e}")

    print("\n3. Modifying and saving context...")
    if 'my_context' in locals():
        new_topic = Topic(
            summary="I'm a beginner in Python programming.",
            key_books=[Book(title="Automate the Boring Stuff with Python", rating=5)]
        )
        my_context.knowledge["python_programming"] = new_topic
        
        core.save_context(my_context)
        print("   - Context updated and saved.")

        reloaded_context = core.load_context()
        print(f"   - Reloaded context. New topics: {list(reloaded_context.knowledge.keys())}")
    
    Path(test_file).unlink()
    print(f"\n--- Test finished, {test_file} cleaned up. ---")