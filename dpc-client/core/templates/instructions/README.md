# AI Instruction Set Templates

This directory contains pre-built instruction set templates for D-PC Messenger. These templates are extracted and adapted from the [personal-context-manager](https://github.com/mikhashev/personal-context-manager) project.

## Available Templates

### 1. General Purpose (`general-purpose.json`)
**Best for:** General conversations and diverse tasks

A balanced instruction set suitable for everyday use across a wide range of topics. Provides thoughtful, well-reasoned responses while maintaining flexibility in communication style.

**Key Features:**
- Balanced thoroughness and conciseness
- Multi-perspective analysis
- Clear distinction between facts and opinions
- Adaptable communication style

---

### 2. Self-Education (`self-education.json`)
**Best for:** Learning new subjects and skill development

Optimized for self-directed learning with evidence-based learning techniques. Focuses on long-term retention, active recall, and practical application.

**Key Features:**
- Explanations that connect to existing knowledge
- Active recall practice questions
- Spaced repetition principles
- Learning strategy recommendations
- Metacognitive support (reflecting on learning process)
- Emphasis on verification from authoritative sources

**Based on cognitive science principles:**
- Active recall
- Spaced repetition
- Elaborative encoding
- Cognitive load management

---

### 3. Engineering Development (`engineering-development.json`)
**Best for:** Software, mechanical, electrical, civil, aerospace, and chemical engineering work

Provides technical guidance and problem-solving support across multiple engineering disciplines. Automatically adapts to your specific field based on context.

**Key Features:**
- Auto-detection of engineering discipline
- Domain-specific terminology and best practices
- Cross-disciplinary insights
- Technical reasoning with citations required
- Design critiques and code reviews
- Trade-off analysis (technical feasibility, UX, business)
- Novel approaches through cross-domain thinking

**Supported Disciplines:**
- Software Engineering
- Mechanical Engineering
- Electrical Engineering
- Civil Engineering
- Aerospace Engineering
- Chemical Engineering

---

## Usage

### Importing Templates

**From UI:**
1. Open AI Instructions editor (📋 button in sidebar)
2. Click **[+ New]** button
3. Select **"Create from Template"**
4. Choose a template
5. Customize as needed

**Programmatically:**
```python
# Backend command
await service.import_instruction_template(
    template_file=Path("templates/instructions/self-education.json"),
    set_key="my-learning",
    set_name="My Learning Set"
)
```

### Customizing Templates

After importing, you can:
- Edit the primary instruction text
- Adjust learning support strategies
- Modify bias mitigation settings
- Add domain-specific guidance
- Change collaboration settings

---

## Template Format

Templates follow the D-PC Messenger InstructionBlock format:

```json
{
  "name": "Template Name",
  "description": "Brief description of use case",
  "primary": "Main instruction text for AI behavior",
  "context_update": "How AI should suggest context updates",
  "verification_protocol": "Standards for fact-checking and citations",
  "learning_support": {
    "explanations": "How to explain concepts",
    "practice": "How to generate practice exercises",
    "metacognition": "How to support self-reflection",
    "connections": "How to identify concept relationships"
  },
  "bias_mitigation": {
    "require_multi_perspective": true,
    "challenge_status_quo": true,
    "cultural_sensitivity": "Guidance for diverse perspectives",
    "framing_neutrality": true,
    "evidence_requirement": "citations_preferred"
  },
  "collaboration_mode": "individual",
  "consensus_required": false,
  "ai_curation_enabled": true,
  "dissent_encouraged": true
}
```

---

## Creating Your Own Templates

You can create custom templates by:

1. **Manual creation**: Create a JSON file following the format above
2. **AI Wizard**: Use the built-in wizard to generate templates through conversation
3. **Export existing**: Export a customized instruction set as a template

---

## Credits

Templates adapted from [personal-context-manager](https://github.com/mikhashev/personal-context-manager) by @mikhashev.

Core concepts based on:
- Cognitive science research on learning and memory
- Evidence-based learning techniques (active recall, spaced repetition)
- Engineering best practices across multiple disciplines
- AI transparency and bias mitigation frameworks
