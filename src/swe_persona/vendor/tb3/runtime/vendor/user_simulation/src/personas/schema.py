"""Persona attribute schema.

This module encodes, in Python, the candidate user-attribute space defined in
``persona_schema_v1.md``. The schema is the single source of truth that the
persona generator samples from and that the persona renderer uses to build a
human-readable system prompt.

Structure mirrors the markdown schema:

    basic_information  -> identity-only fields (do not affect coding behaviour)
    professional_background -> role, domain, ecosystem, and work context
    preference         -> Big-Five personality-aware preferences
    coding_specific    -> { concrete: {...}, abstract: {...} }
    scenario_specific  -> { terminal: {...}, vscode: {...} }

Every *categorical* attribute is described by an :class:`AttributeSpec`. Basic
information is identity-only and is described separately by candidate pools in
:data:`BASIC_INFORMATION_POOLS`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


SCHEMA_VERSION = "persona-v1"


@dataclass(frozen=True)
class AttributeSpec:
    """A single categorical persona attribute and its candidate values."""

    name: str
    description: str
    values: List[str]
    # Interaction stage(s) the attribute influences. ``None`` for personality
    # preferences that have no explicit scope in the schema.
    scope: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.values:
            raise ValueError(f"attribute {self.name!r} must define at least one value")


# ---------------------------------------------------------------------------
# basic_information: identity-only candidate pools
# ---------------------------------------------------------------------------
# ``age`` is sampled from an integer range; everything else from a pool.
BASIC_INFORMATION_POOLS: Dict[str, object] = {
    "first_names": [
        "Alex", "Sam", "Jordan", "Taylor", "Morgan", "Casey", "Riley", "Jamie",
        "Avery", "Quinn", "Wei", "Priya", "Diego", "Yuki", "Omar", "Sofia",
        "Liam", "Noah", "Mia", "Chen",
    ],
    "last_names": [
        "Smith", "Johnson", "Garcia", "Kim", "Patel", "Nguyen", "Mueller",
        "Rossi", "Silva", "Khan", "Olsen", "Costa", "Tanaka", "Brown", "Lee",
    ],
    "age_range": (19, 65),
    "sex": ["female", "male", "non-binary"],
    "education": [
        "high school",
        "associate degree",
        "bachelor's degree",
        "master's degree",
        "PhD",
        "self-taught",
    ],
    "occupation": [
        "student",
        "junior software engineer",
        "senior software engineer",
        "data scientist",
        "devops engineer",
        "engineering manager",
        "research scientist",
        "QA engineer",
        "hobbyist programmer",
        "product manager",
    ],
    # Geographical region the user is based in, e.g., "United States".
    "region": [
        "United States",
        "Canada",
        "Brazil",
        "United Kingdom",
        "Germany",
        "France",
        "Nigeria",
        "India",
        "China",
        "Japan",
        "Australia",
    ],
}


def _spec(name: str, description: str, values: List[str], scope: Optional[str] = None) -> AttributeSpec:
    return AttributeSpec(name=name, description=description, values=values, scope=scope)


# ---------------------------------------------------------------------------
# professional_background: task and repository affinity
# ---------------------------------------------------------------------------
PROFESSIONAL_BACKGROUND: Dict[str, AttributeSpec] = {
    "role_family": _spec(
        "role_family",
        "The user's primary professional role family; guides which repositories and "
        "tasks are natural, adjacent, or out of domain.",
        [
            "ui-ux-design", "frontend-engineering", "backend-engineering",
            "full-stack-engineering", "mobile-engineering", "devops-sre",
            "data-engineering", "ml-engineering", "security-engineering",
            "qa-test-engineering", "systems-engineering", "research",
            "engineering-management",
        ],
        "all",
    ),
    "seniority": _spec(
        "seniority",
        "The user's career level, which affects expected independence, vocabulary, and "
        "ability to evaluate tradeoffs.",
        ["student", "entry-level", "mid-level", "senior", "staff", "manager"],
        "all",
    ),
    "years_of_experience": _spec(
        "years_of_experience",
        "A coarse range for the user's professional software experience.",
        ["less-than-1", "1-3", "4-7", "8-15", "16-plus"],
        "all",
    ),
    "primary_domain": _spec(
        "primary_domain",
        "The software domain in which the user most often works and can provide "
        "domain-specific context.",
        [
            "web-ui", "web-services", "mobile", "cloud-infrastructure",
            "data-analytics", "machine-learning", "security",
            "developer-tools", "systems", "compilers", "databases",
            "general-software",
        ],
        "all",
    ),
    "primary_ecosystem": _spec(
        "primary_ecosystem",
        "The language and framework ecosystem the user works in most frequently.",
        [
            "react-typescript", "node-typescript", "python-data-ml",
            "python-web", "java-jvm", "dotnet", "go-cloud", "rust-systems",
            "cpp-native", "mobile-native", "kubernetes-platform", "mixed",
        ],
        "all",
    ),
    "work_context": _spec(
        "work_context",
        "The organizational setting in which the user usually develops software.",
        [
            "learning", "hobby", "startup", "product-team", "platform-team",
            "enterprise", "open-source", "research",
        ],
        "all",
    ),
    "repository_scale_experience": _spec(
        "repository_scale_experience",
        "The largest repository structure the user regularly navigates and maintains.",
        ["small-single-package", "medium-multi-package", "large-monorepo"],
        "all",
    ),
    "maintenance_orientation": _spec(
        "maintenance_orientation",
        "Whether the user's work is primarily new feature development, maintenance and "
        "debugging, or a balance of both.",
        ["feature-development", "maintenance-and-debugging", "balanced"],
        "all",
    ),
}


# ---------------------------------------------------------------------------
# preference: Big-Five personality-aware preferences
# ---------------------------------------------------------------------------
PREFERENCE: Dict[str, AttributeSpec] = {
    "trait": _spec(
        "trait",
        "Social-interaction style; affects message length, chit-chat, and how "
        "directly the user engages the agent.",
        ["extroverted", "introverted"],
    ),
    "emotion": _spec(
        "emotion",
        "Current affective state; modulates tone of feedback and tolerance for friction.",
        ["calm", "sad", "angry", "anxious", "excited"],
    ),
    "openness": _spec(
        "openness",
        "Willingness to consider novel or experimental approaches suggested by the "
        "agent vs. preferring conventional, well-trodden solutions.",
        ["open", "conventional"],
    ),
    "conscientiousness": _spec(
        "conscientiousness",
        "Degree of organization and attention to detail; high users track edge cases, "
        "naming, and cleanliness, low users are easygoing about thoroughness.",
        ["organized", "easygoing"],
    ),
    "agreeableness": _spec(
        "agreeableness",
        "Cooperative vs. confrontational stance when the user disagrees with the "
        "agent; affects bluntness of feedback and willingness to push back.",
        ["agreeable", "critical"],
    ),
}


# ---------------------------------------------------------------------------
# coding_specific.concrete
# ---------------------------------------------------------------------------
CODING_CONCRETE: Dict[str, AttributeSpec] = {
    "prompt_verbosity": _spec(
        "prompt_verbosity",
        "Whether a user prefers to use a more verbose prompt to run the agent.",
        ["concise", "verbose"], "Initial Prompt",
    ),
    "prompt_structure": _spec(
        "prompt_structure",
        "Whether a user prefers to list the task in a structured guideline "
        "(step 1, step 2...) or unstructured natural language.",
        ["natural-language", "structured"], "Initial Prompt",
    ),
    "misleading_prior": _spec(
        "misleading_prior",
        "Whether a user prefers to add its (possibly incorrect) knowledge of the repo "
        "to the prompt.",
        ["yes", "no"], "Initial Prompt",
    ),
    "preferred_coding_language": _spec(
        "preferred_coding_language",
        "The preferred program language to solve the problem (if no language is specified).",
        ["python", "java", "cpp", "javascript", "typescript", "go", "rust"], "all",
    ),
    "trust_in_agent": _spec(
        "trust_in_agent",
        "The user's prior trust in the coding agent's competence. High-trust users may "
        "grant autonomy and accept larger inferred changes; low-trust users may demand "
        "explanations, smaller steps, or explicit justification.",
        ["high", "low"], "interaction, review feedback, approval process",
    ),
    "time_sensitivity": _spec(
        "time_sensitivity",
        "How urgently the user needs a working result. High-sensitivity users favor "
        "fast, working fixes and may accept tech debt or skipped exploration; "
        "low-sensitivity users tolerate longer, more thorough work.",
        ["high", "low"], "interaction, approval process",
    ),
    "context_provision": _spec(
        "context_provision",
        "How much surrounding context (relevant files, error logs, links) the user "
        "supplies up front vs. expecting the agent to discover it.",
        ["minimal", "rich"], "Initial Prompt, interaction",
    ),
    "specification_completeness": _spec(
        "specification_completeness",
        "How completely the requirements are stated. Underspecified users leave "
        "acceptance criteria, edge cases, or constraints implicit.",
        ["complete", "underspecified"], "Initial Prompt",
    ),
    "clarification_responsiveness": _spec(
        "clarification_responsiveness",
        "How readily and fully the user answers the agent's clarifying questions during "
        "the task.",
        ["responsive", "reluctant"], "interaction",
    ),
    "review_granularity": _spec(
        "review_granularity",
        "How closely the user inspects produced changes: line-by-line diff review vs. a "
        "high-level summary.",
        ["line-by-line", "high-level"], "review feedback",
    ),
    "explanation_demand": _spec(
        "explanation_demand",
        "Whether the user wants the agent to justify its reasoning and decisions, or "
        "only cares about the final outcome.",
        ["wants-explanation", "outcome-only"], "interaction, review feedback",
    ),
    "testing_preference": _spec(
        "testing_preference",
        "Whether the user expects tests to be written and/or run as part of the change.",
        ["require-tests", "skip-tests"], "interaction, approval process",
    ),
    "documentation_preference": _spec(
        "documentation_preference",
        "How much inline documentation (comments, docstrings, README updates) the user "
        "expects.",
        ["required", "minimal"], "interaction, review feedback",
    ),
    "code_style_strictness": _spec(
        "code_style_strictness",
        "How much the user cares about style conventions, linting, and formatting "
        "consistency.",
        ["strict", "lenient"], "review feedback, approval process",
    ),
    "error_tolerance": _spec(
        "error_tolerance",
        "How the user reacts when the agent produces a bug or mistake: forgiving and "
        "iterative vs. critical and low-patience.",
        ["forgiving", "critical"], "review feedback, interaction",
    ),
    "scope_stability": _spec(
        "scope_stability",
        "Tendency to keep the task fixed vs. introducing new requirements or scope creep "
        "mid-interaction.",
        ["focused", "expanding"], "interaction",
    ),
    "tool_permission": _spec(
        "tool_permission",
        "Willingness to let the agent execute commands, install packages, or modify the "
        "environment.",
        ["permissive", "restrictive"], "approval process, interaction",
    ),
    "dependency_preference": _spec(
        "dependency_preference",
        "Attitude toward adding third-party dependencies vs. solving with "
        "existing/standard-library code.",
        ["minimal-deps", "open-to-deps"], "review feedback, approval process",
    ),
    "performance_sensitivity": _spec(
        "performance_sensitivity",
        "How much the user weighs runtime/efficiency concerns in evaluating a solution.",
        ["high", "low"], "review feedback",
    ),
    "feedback_specificity": _spec(
        "feedback_specificity",
        "How precise the user's feedback is when requesting changes: pinpointed and "
        "actionable vs. vague and impressionistic.",
        ["specific", "vague"], "review feedback",
    ),
    "interaction_depth_preference": _spec(
        "interaction_depth_preference",
        "Whether the user generally delegates a task in one prompt and waits for the "
        "result, or prefers a multi-turn process with follow-up instructions and refinements.",
        ["one-shot", "iterative"], "interaction",
    ),
    "mid_turn_steering_preference": _spec(
        "mid_turn_steering_preference",
        "Whether the user usually lets an in-progress agent turn finish or sends additional "
        "steering or queued messages while the agent is still working.",
        ["wait-for-completion", "steer-during-execution"], "interaction",
    ),
    "failure_recovery_strategy": _spec(
        "failure_recovery_strategy",
        "What the user tends to do after an agent, model, or tool failure: continue by "
        "retrying or correcting the approach vs. abandon the task or session.",
        ["retry-or-correct", "abandon-after-failure"], "interaction, review feedback",
    ),
    "task_type_preference": _spec(
        "task_type_preference",
        "The dominant coding task category the user repeatedly asks an agent to perform across sessions.",
        ["implementation", "debugging", "testing", "code-review", "documentation", "operations", "explanation"],
        "all",
    ),
}


# ---------------------------------------------------------------------------
# coding_specific.abstract
# ---------------------------------------------------------------------------
CODING_ABSTRACT: Dict[str, AttributeSpec] = {
    "coding_expertise": _spec(
        "coding_expertise",
        "General programming skill. High-expertise users can read and critique complex "
        "diffs and give precise technical feedback; low-expertise users rely on the agent "
        "and struggle to judge correctness.",
        ["high", "low"], "all",
    ),
    "repo_expertise": _spec(
        "repo_expertise",
        "Familiarity with the targeted project. High-expertise users know the "
        "architecture, conventions, and where things live; low-expertise users are "
        "unfamiliar and may give incorrect pointers (interacts with misleading_prior).",
        ["high", "low"], "all",
    ),
    "python_expertise": _spec(
        "python_expertise",
        "The expertise level of the user in the Python programming language. Affects "
        "ability to evaluate Python code and the quality of feedback on it.",
        ["high", "low"], "all",
    ),
    "java_expertise": _spec(
        "java_expertise",
        "The expertise level of the user in the Java programming language. Affects "
        "ability to evaluate Java code and the quality of feedback on it.",
        ["high", "low"], "all",
    ),
    "cpp_expertise": _spec(
        "cpp_expertise",
        "The expertise level of the user in the C++ programming language. Affects "
        "ability to evaluate C++ code and the quality of feedback on it.",
        ["high", "low"], "all",
    ),
    "javascript_expertise": _spec(
        "javascript_expertise",
        "The expertise level of the user in the JavaScript programming language. Affects "
        "ability to evaluate JavaScript code and the quality of feedback on it.",
        ["high", "low"], "all",
    ),
    "typescript_expertise": _spec(
        "typescript_expertise",
        "The expertise level of the user in the TypeScript programming language. Affects "
        "ability to evaluate TypeScript code and the quality of feedback on it.",
        ["high", "low"], "all",
    ),
}


# ---------------------------------------------------------------------------
# scenario_specific
# ---------------------------------------------------------------------------
SCENARIO_TERMINAL: Dict[str, AttributeSpec] = {
    "command_confirmation": _spec(
        "command_confirmation",
        "Whether each shell command must be explicitly approved before execution or the "
        "agent may auto-run.",
        ["confirm-each", "auto-run"], "approval process",
    ),
    "terminal_output_verbosity": _spec(
        "terminal_output_verbosity",
        "Desired amount of streamed logs and intermediate output in the terminal.",
        ["verbose", "quiet"], "interaction",
    ),
    "destructive_command_caution": _spec(
        "destructive_command_caution",
        "Sensitivity to risky or irreversible commands (e.g., rm -rf, force push, schema "
        "migrations).",
        ["cautious", "relaxed"], "approval process",
    ),
    "agent_mode_preference": _spec(
        "agent_mode_preference",
        "The terminal agent mode the user tends to select: ordinary interactive execution, "
        "plan-first work, or autonomous multi-step execution.",
        ["interactive", "plan-first", "autopilot"], "Initial Prompt, interaction, approval process",
    ),
    "model_selection_strategy": _spec(
        "model_selection_strategy",
        "Whether the user tends to choose a specific model or delegates model selection to "
        "automatic routing.",
        ["explicit-model", "auto-routing"], "Initial Prompt, interaction",
    ),
}

SCENARIO_VSCODE: Dict[str, AttributeSpec] = {
    # Attributes for the VS Code chat session (e.g., GitHub Copilot Chat), where the
    # user converses with an agent in the side panel that edits the open file.
    "edit_application_mode": _spec(
        "edit_application_mode",
        "How the user applies the agent's proposed edits: reviewing each change in the "
        "diff view and accepting/discarding hunks individually vs. letting the agent "
        "apply edits directly to the file.",
        ["review-diffs", "auto-apply"], "review feedback, approval process",
    ),
    "context_attachment": _spec(
        "context_attachment",
        "Whether the user explicitly attaches context to the chat (e.g., #file, "
        "#selection, @workspace references) or relies on the agent automatically using "
        "the currently open/focused file.",
        ["explicit", "implicit"], "Initial Prompt, interaction",
    ),
    "chat_mode_preference": _spec(
        "chat_mode_preference",
        "Preferred chat surface for issuing requests: inline chat anchored at the cursor "
        "in the editor vs. the side-panel chat conversation.",
        ["inline", "side-panel"], "interaction",
    ),
}


# Names of the supported scenarios. ``None``/general personas omit scenario blocks.
SCENARIOS = ("terminal", "vscode")

SCENARIO_SPECIFIC: Dict[str, Dict[str, AttributeSpec]] = {
    "terminal": SCENARIO_TERMINAL,
    "vscode": SCENARIO_VSCODE,
}


@dataclass(frozen=True)
class PersonaSchema:
    """The full candidate attribute space, grouped exactly as in the markdown schema."""

    professional_background: Dict[str, AttributeSpec] = field(
        default_factory=lambda: dict(PROFESSIONAL_BACKGROUND)
    )
    preference: Dict[str, AttributeSpec] = field(default_factory=lambda: dict(PREFERENCE))
    coding_concrete: Dict[str, AttributeSpec] = field(default_factory=lambda: dict(CODING_CONCRETE))
    coding_abstract: Dict[str, AttributeSpec] = field(default_factory=lambda: dict(CODING_ABSTRACT))
    scenario_specific: Dict[str, Dict[str, AttributeSpec]] = field(
        default_factory=lambda: {k: dict(v) for k, v in SCENARIO_SPECIFIC.items()}
    )
    basic_information_pools: Dict[str, object] = field(
        default_factory=lambda: dict(BASIC_INFORMATION_POOLS)
    )

    def categorical_specs(self) -> Dict[str, AttributeSpec]:
        """Flat mapping ``attribute_name -> AttributeSpec`` for every categorical attribute.

        Useful for validating distribution weights and for sampling.
        """
        out: Dict[str, AttributeSpec] = {}
        for group in (
            self.professional_background,
            self.preference,
            self.coding_concrete,
            self.coding_abstract,
        ):
            out.update(group)
        for scen in self.scenario_specific.values():
            out.update(scen)
        return out


# A ready-to-use default schema instance.
DEFAULT_SCHEMA = PersonaSchema()
