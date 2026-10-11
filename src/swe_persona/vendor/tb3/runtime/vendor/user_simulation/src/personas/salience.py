"""Task-conditioned persona salience helpers.

The persona schema is intentionally broad. A narrow debugging task should not
force every attribute into the conversation, but the simulator and judge should
agree on which persona traits are most relevant for this task. This module
provides deterministic bridges for two separate stages: canonical task text
selects compact task/domain traits, while visible turn events activate stable
interaction preferences.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from .schema import DEFAULT_SCHEMA


@dataclass(frozen=True)
class SalienceSpec:
    key: str
    source: str
    triggers: tuple[str, ...]
    behavior_by_value: dict[str, str]
    rubric_key: str
    rubric_question_by_value: dict[str, str]
    default_behavior: str = ""
    default_question: str = ""
    base_priority: int = 0
    weight: float = 1.0


CANONICAL_ATTRIBUTE_SOURCES: dict[str, str] = {
    **{
        key: "professional_background"
        for key in DEFAULT_SCHEMA.professional_background
    },
    **{key: "preference" for key in DEFAULT_SCHEMA.preference},
    **{
        key: "coding_specific.concrete"
        for key in DEFAULT_SCHEMA.coding_concrete
    },
    **{
        key: "coding_specific.abstract"
        for key in DEFAULT_SCHEMA.coding_abstract
    },
    **{
        key: f"scenario_specific.{scenario}"
        for scenario, attributes in DEFAULT_SCHEMA.scenario_specific.items()
        for key in attributes
    },
}


def canonical_persona_attribute(key: str, value: str, source: str) -> dict[str, str]:
    """Return a persona attribution using the schema's exact vocabulary."""
    return {"key": key, "value": value, "source": source}


def _get(persona: dict[str, Any], *path: str) -> str:
    cur: object = persona
    for key in path:
        if not isinstance(cur, dict):
            return ""
        cur = cur.get(key)
    return str(cur or "")


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9_+-]+", text.lower()))


def _contains_any(text: str, needles: Iterable[str]) -> bool:
    """Match whole tokens or phrases, never arbitrary substrings."""
    low = text.lower()
    tokens = _tokens(low)
    for needle in needles:
        normalized = " ".join(needle.lower().split())
        if not normalized:
            continue
        if " " not in normalized:
            if normalized in tokens:
                return True
            continue
        phrase = r"(?<![a-z0-9_+-])" + r"\s+".join(
            re.escape(part) for part in normalized.split()
        ) + r"(?![a-z0-9_+-])"
        if re.search(phrase, low):
            return True
    return False


def _task_kind_bonus(task_text: str, key: str, value: str) -> int:
    if key == "performance_sensitivity" and _contains_any(
        task_text, ("performance", "latency", "benchmark", "memory", "runtime", "throughput")
    ):
        return 24
    if key == "dependency_preference" and _contains_any(
        task_text,
        (
            "dependency",
            "dependencies",
            "requirements",
            "package manager",
            "third party",
            "pip",
            "npm",
            "yarn",
            "pnpm",
        ),
    ):
        return 22
    if key == "documentation_preference" and _contains_any(
        task_text,
        ("documentation", "readme", "docstring", "changelog", "deprecation", "release note"),
    ):
        return 22
    if key == "seniority" and _contains_any(
        task_text, ("architecture", "design", "tradeoff", "migration", "compatibility")
    ):
        return 12
    if key == "repo_expertise" and _contains_any(
        task_text,
        (
            "repository structure",
            "repo-wide",
            "monorepo",
            "cross-module",
            "multiple modules",
            "architecture",
        ),
    ):
        return 12
    if key == "primary_ecosystem":
        ecosystem_tokens = {
            token for token in re.findall(r"[a-z0-9]+", value.lower()) if len(token) >= 3
        }
        if ecosystem_tokens & _tokens(task_text):
            return 18
    if key == "preferred_coding_language" and value in {"go", "rust"}:
        if value in _tokens(task_text):
            return 18
    return 0


LANGUAGE_ATTRIBUTE_TO_LANGUAGE = {
    "python_expertise": "python",
    "java_expertise": "java",
    "cpp_expertise": "cpp",
    "javascript_expertise": "javascript",
    "typescript_expertise": "typescript",
}


def _repository_language_signals(task_text: str) -> list[str]:
    match = re.search(
        r"^Repository language signals:\s*([^\n]+)$",
        task_text,
        re.I | re.M,
    )
    if not match:
        return []
    return [
        language.strip().lower()
        for language in match.group(1).split(",")
        if language.strip()
    ]


def _trigger_hits_for_spec(task_text: str, spec: SalienceSpec) -> list[str]:
    """Return task triggers without treating every repository language as in-scope.

    Repository scans can find incidental secondary-language files (for example a
    JavaScript helper in a Python project). When those signals are present, the
    first/dominant signal is authoritative for language-expertise calibration.
    """
    language = LANGUAGE_ATTRIBUTE_TO_LANGUAGE.get(spec.key)
    repository_languages = _repository_language_signals(task_text)
    if language and repository_languages:
        if language != repository_languages[0]:
            return []
        return [f"repository language {language}"]
    return [
        trigger
        for trigger in spec.triggers
        if _contains_any(task_text, (trigger,))
    ]


TASK_SALIENCE_KEYS = frozenset(
    {
        "documentation_preference",
        "dependency_preference",
        "performance_sensitivity",
        "repo_expertise",
        "python_expertise",
        "java_expertise",
        "cpp_expertise",
        "javascript_expertise",
        "typescript_expertise",
        "preferred_coding_language",
        "primary_ecosystem",
        "seniority",
    }
)

BACKGROUND_ATTRIBUTE_KEYS = frozenset(
    {
        "coding_expertise",
        "repo_expertise",
        "python_expertise",
        "java_expertise",
        "cpp_expertise",
        "javascript_expertise",
        "typescript_expertise",
        "primary_ecosystem",
        "seniority",
    }
)

INTERACTION_POLICY_PATHS = {
    "trust_in_agent": "coding_specific.concrete",
    "time_sensitivity": "coding_specific.concrete",
    "review_granularity": "coding_specific.concrete",
    "explanation_demand": "coding_specific.concrete",
    "testing_preference": "coding_specific.concrete",
    "documentation_preference": "coding_specific.concrete",
    "code_style_strictness": "coding_specific.concrete",
    "error_tolerance": "coding_specific.concrete",
    "scope_stability": "coding_specific.concrete",
    "tool_permission": "coding_specific.concrete",
    "dependency_preference": "coding_specific.concrete",
    "performance_sensitivity": "coding_specific.concrete",
    "interaction_depth_preference": "coding_specific.concrete",
    "failure_recovery_strategy": "coding_specific.concrete",
    "command_confirmation": "scenario_specific.terminal",
    "terminal_output_verbosity": "scenario_specific.terminal",
    "agent_mode_preference": "scenario_specific.terminal",
}

_POLICY_INSTRUCTIONS = {
    "trust_in_agent": "Use visible evidence and this trust level to set the acceptance threshold.",
    "time_sensitivity": "Balance speed against further investigation according to this urgency.",
    "review_granularity": "Review the visible diff at the requested level of detail.",
    "explanation_demand": "Request or avoid extra rationale according to this preference.",
    "testing_preference": "Apply this preference when deciding whether verification evidence is sufficient.",
    "documentation_preference": "Apply this preference only to documentation-relevant changes.",
    "code_style_strictness": "Apply this strictness when assessing the visible code changes.",
    "error_tolerance": "Let this preference shape the tone and patience of failure feedback.",
    "scope_stability": "Use this preference when deciding whether to keep or expand the requested scope.",
    "tool_permission": "Enforce this permission level when visible tool activity makes it relevant.",
    "dependency_preference": "Apply this preference when dependency changes are visible or proposed.",
    "performance_sensitivity": "Apply this preference when the work has a performance consequence.",
    "interaction_depth_preference": "Use this preference when choosing whether to follow up or finish.",
    "failure_recovery_strategy": "Use this preference to decide whether to retry, redirect, or stop after failure.",
    "command_confirmation": "Enforce this command boundary when visible tool activity makes it relevant.",
    "terminal_output_verbosity": "Use this preference when deciding how much evidence or output to request.",
    "agent_mode_preference": "Use this preference to decide whether a plan is required before execution.",
}


SPECS: tuple[SalienceSpec, ...] = (
    SalienceSpec(
        key="prompt_verbosity",
        source="coding_specific.concrete",
        triggers=("prompt", "request", "opening", "task"),
        behavior_by_value={
            "concise": "Write compact user messages and avoid unnecessary detail.",
            "verbose": "Give the agent fuller task context and acceptance criteria.",
        },
        rubric_key="communication_fit",
        rubric_question_by_value={
            "concise": "Did the agent keep communication compact while still giving needed evidence?",
            "verbose": "Did the agent provide enough detail for this user's preferred communication style?",
        },
        base_priority=15,
    ),
    SalienceSpec(
        key="prompt_structure",
        source="coding_specific.concrete",
        triggers=("step", "criteria", "acceptance", "task"),
        behavior_by_value={
            "structured": "Use numbered or clearly separated requirements in the opening message.",
            "natural-language": "Use a more conversational request rather than a checklist-heavy style.",
        },
        rubric_key="communication_fit",
        rubric_question_by_value={
            "structured": "Did the agent respond clearly to the structured requirements?",
            "natural-language": "Did the agent handle the conversational request without losing requirements?",
        },
        base_priority=15,
    ),
    SalienceSpec(
        key="trust_in_agent",
        source="coding_specific.concrete",
        triggers=("evidence", "diff", "verify", "test", "bug", "fix", "trust"),
        behavior_by_value={
            "low": "Require concrete evidence before accepting claims.",
            "high": "Allow autonomy, but still react if visible evidence contradicts the agent.",
        },
        rubric_key="evidence_completeness",
        rubric_question_by_value={
            "low": "Did the agent provide enough concrete evidence for a low-trust user?",
            "high": "Did the agent use the user's autonomy well while staying trustworthy?",
        },
        base_priority=35,
        weight=1.35,
    ),
    SalienceSpec(
        key="testing_preference",
        source="coding_specific.concrete",
        triggers=("test", "verify", "pytest", "assert", "pass", "fail", "bug", "fix"),
        behavior_by_value={
            "require-tests": "Ask for specific tests, commands, and visible results before done.",
            "skip-tests": "Do not over-demand formal tests unless risk or evidence gaps require them.",
        },
        rubric_key="test_evidence",
        rubric_question_by_value={
            "require-tests": "Did the agent provide the tests, commands, and outputs this user needed?",
            "skip-tests": "Did the agent avoid unnecessary test overhead while providing enough confidence?",
        },
        base_priority=35,
        weight=1.45,
    ),
    SalienceSpec(
        key="explanation_demand",
        source="coding_specific.concrete",
        triggers=("explain", "why", "root cause", "rationale", "summary"),
        behavior_by_value={
            "wants-explanation": "Ask for reasoning, root cause, and why the fix works when relevant.",
            "outcome-only": "Prefer concise outcome, diff, and verification over long rationale.",
        },
        rubric_key="communication_fit",
        rubric_question_by_value={
            "wants-explanation": "Did the agent give enough reasoning and root-cause explanation?",
            "outcome-only": "Did the agent stay outcome-focused without excessive reasoning?",
        },
        base_priority=25,
        weight=1.0,
    ),
    SalienceSpec(
        key="command_confirmation",
        source="scenario_specific.terminal",
        triggers=("command", "run", "shell", "test", "install", "commit", "terminal"),
        behavior_by_value={
            "confirm-each": "Ask the agent to propose commands before running them; redirect if it crosses that boundary.",
            "auto-run": "Let the agent run ordinary task commands without per-command approval.",
        },
        rubric_key="command_consent",
        rubric_question_by_value={
            "confirm-each": "Did the agent respect this user's per-command confirmation boundary?",
            "auto-run": "Did the agent use command autonomy appropriately without risky surprises?",
        },
        base_priority=30,
        weight=1.4,
    ),
    SalienceSpec(
        key="review_granularity",
        source="coding_specific.concrete",
        triggers=("diff", "patch", "review", "line", "inspect"),
        behavior_by_value={
            "line-by-line": "Ask for exact changed lines or a small diff when evaluating the patch.",
            "high-level": "Prefer a concise summary unless something looks risky or inconsistent.",
        },
        rubric_key="change_review_fit",
        rubric_question_by_value={
            "line-by-line": "Did the agent provide enough line-level evidence for review?",
            "high-level": "Did the agent summarize the change at the right level without hiding key details?",
        },
        base_priority=20,
    ),
    SalienceSpec(
        key="code_style_strictness",
        source="coding_specific.concrete",
        triggers=("style", "lint", "format", "minimal", "diff", "patch", "test"),
        behavior_by_value={
            "strict": "Care about minimal, style-consistent changes and no unrelated churn.",
            "lenient": "Accept pragmatic style choices if the fix is correct and understandable.",
        },
        rubric_key="change_cleanliness",
        rubric_question_by_value={
            "strict": "Was the change minimal, style-consistent, and free of unrelated churn?",
            "lenient": "Was the change understandable and pragmatic without harmful style issues?",
        },
        base_priority=20,
        weight=1.15,
    ),
    SalienceSpec(
        key="error_tolerance",
        source="coding_specific.concrete",
        triggers=("error", "fail", "bug", "incorrect", "contradict", "risk"),
        behavior_by_value={
            "critical": "Push back quickly on contradictions, failed evidence, or risky claims.",
            "forgiving": "Allow correction and recovery after minor mistakes.",
        },
        rubric_key="failure_recovery",
        rubric_question_by_value={
            "critical": "Did the agent avoid or recover from mistakes enough for a critical user?",
            "forgiving": "Did the agent recover constructively from any mistakes?",
        },
        base_priority=25,
        weight=1.2,
    ),
    SalienceSpec(
        key="documentation_preference",
        source="coding_specific.concrete",
        triggers=(
            "documentation",
            "readme",
            "docstring",
            "changelog",
            "deprecation",
            "release note",
        ),
        behavior_by_value={
            "required": "Expect comments, docstrings, or documentation updates when they are appropriate.",
            "minimal": "Avoid asking for documentation unless it is necessary for clarity.",
        },
        rubric_key="documentation_fit",
        rubric_question_by_value={
            "required": "Did the agent provide the documentation or inline explanation this user expects?",
            "minimal": "Did the agent avoid unnecessary documentation churn?",
        },
        base_priority=10,
    ),
    SalienceSpec(
        key="dependency_preference",
        source="coding_specific.concrete",
        triggers=(
            "install",
            "dependency",
            "dependencies",
            "package manager",
            "requirements",
            "pip",
            "npm",
            "yarn",
            "pnpm",
        ),
        behavior_by_value={
            "minimal-deps": "Prefer existing tools and avoid new dependencies unless clearly justified.",
            "open-to-deps": "Accept new dependencies when they are justified and transparent.",
        },
        rubric_key="dependency_handling",
        rubric_question_by_value={
            "minimal-deps": "Did the agent avoid or justify dependency changes appropriately?",
            "open-to-deps": "Did the agent handle dependencies transparently and usefully?",
        },
        base_priority=15,
        weight=1.15,
    ),
    SalienceSpec(
        key="performance_sensitivity",
        source="coding_specific.concrete",
        triggers=("performance", "latency", "runtime", "slow", "memory", "scale"),
        behavior_by_value={
            "high": "Ask about performance risk, complexity, or regression when the task touches runtime behavior.",
            "low": "Do not dwell on performance unless the agent introduces an obvious risk.",
        },
        rubric_key="performance_fit",
        rubric_question_by_value={
            "high": "Did the agent handle performance and regression risk well enough for this user?",
            "low": "Did the agent avoid unnecessary performance discussion while not introducing obvious risk?",
        },
        base_priority=10,
    ),
    SalienceSpec(
        key="scope_stability",
        source="coding_specific.concrete",
        triggers=("scope", "requirement", "follow-up", "extra", "only", "fix"),
        behavior_by_value={
            "focused": "Keep follow-ups within the original task and push back on scope creep.",
            "expanding": "May add reasonable follow-up requirements once the core task is addressed.",
        },
        rubric_key="scope_fit",
        rubric_question_by_value={
            "focused": "Did the agent stay within the requested scope?",
            "expanding": "Did the agent handle reasonable follow-up refinements without losing the core task?",
        },
        base_priority=15,
    ),
    SalienceSpec(
        key="interaction_depth_preference",
        source="coding_specific.concrete",
        triggers=("follow-up", "iterate", "review", "done", "task"),
        behavior_by_value={
            "iterative": "Use follow-ups when evidence, scope, or explanation is not yet satisfactory.",
            "one-shot": "Prefer to accept after one complete answer unless a clear issue remains.",
        },
        rubric_key="followup_responsiveness",
        rubric_question_by_value={
            "iterative": "Did the agent respond well to iterative follow-up instructions?",
            "one-shot": "Did the agent complete enough in one pass to suit this user's workflow?",
        },
        base_priority=20,
    ),
    SalienceSpec(
        key="coding_expertise",
        source="coding_specific.abstract",
        triggers=("fix", "implement", "debug", "code", "parser", "handler", "test"),
        behavior_by_value={
            "low": "Prefer a concrete explanation of what changed and why, without assuming advanced programming knowledge.",
            "high": "Use precise implementation details and explain non-obvious design tradeoffs without teaching basic programming concepts.",
        },
        rubric_key="technical_depth_fit",
        rubric_question_by_value={
            "low": "Was the technical explanation concrete and understandable without assuming advanced programming knowledge?",
            "high": "Was the technical explanation precise and substantive without unnecessary explanation of basic programming concepts?",
        },
        base_priority=12,
        weight=1.2,
    ),
    SalienceSpec(
        key="repo_expertise",
        source="coding_specific.abstract",
        triggers=(
            "repository structure",
            "repo-wide",
            "monorepo",
            "cross-module",
            "multiple modules",
            "architecture",
        ),
        behavior_by_value={
            "low": "Expect the agent to discover repository structure independently and explain locations.",
            "high": "Use more technical, repo-specific feedback and inspect evidence more critically.",
        },
        rubric_key="repo_navigation",
        rubric_question_by_value={
            "low": "Did the agent navigate the repository independently enough for this user?",
            "high": "Did the agent provide technically credible repo-level evidence?",
        },
        base_priority=20,
    ),
    SalienceSpec(
        key="python_expertise",
        source="coding_specific.abstract",
        triggers=(
            "python",
            "pytest",
            "sympy",
            "django",
            "flask",
            "fastapi",
            "httpx",
            "asyncio",
            "cpython",
        ),
        behavior_by_value={
            "low": "Prefer plain-language Python explanations and concrete verification output.",
            "high": "Can evaluate Python details and may ask sharper technical questions.",
        },
        rubric_key="language_fit",
        rubric_question_by_value={
            "low": "Did the agent explain Python-specific changes accessibly enough?",
            "high": "Did the agent provide enough Python-specific technical detail?",
        },
        base_priority=10,
    ),
    SalienceSpec(
        key="preferred_coding_language",
        source="coding_specific.concrete",
        triggers=(),
        behavior_by_value={},
        default_behavior="The task uses this user's preferred language, so domain-specific feedback can be direct.",
        rubric_key="language_fit",
        rubric_question_by_value={},
        default_question="Did the agent communicate at an appropriate level for a user who prefers this task's language?",
        base_priority=8,
    ),
    SalienceSpec(
        key="java_expertise",
        source="coding_specific.abstract",
        triggers=("java", "junit", "gradle", "maven", "spring"),
        behavior_by_value={
            "low": "Prefer accessible Java explanations and concrete verification output.",
            "high": "Can evaluate Java details and may ask sharper technical questions.",
        },
        rubric_key="language_fit",
        rubric_question_by_value={
            "low": "Did the agent explain Java-specific changes accessibly enough?",
            "high": "Did the agent provide enough Java-specific technical detail?",
        },
        base_priority=10,
    ),
    SalienceSpec(
        key="cpp_expertise",
        source="coding_specific.abstract",
        triggers=("c++", "cpp", "cmake", "clang", "gcc"),
        behavior_by_value={
            "low": "Prefer accessible C++ explanations and concrete verification output.",
            "high": "Can evaluate C++ details and may ask sharper technical questions.",
        },
        rubric_key="language_fit",
        rubric_question_by_value={
            "low": "Did the agent explain C++-specific changes accessibly enough?",
            "high": "Did the agent provide enough C++-specific technical detail?",
        },
        base_priority=10,
    ),
    SalienceSpec(
        key="javascript_expertise",
        source="coding_specific.abstract",
        triggers=("javascript", "node", "nodejs", "jest", "react", "eslint"),
        behavior_by_value={
            "low": "Prefer accessible JavaScript explanations and concrete verification output.",
            "high": "Can evaluate JavaScript details and may ask sharper technical questions.",
        },
        rubric_key="language_fit",
        rubric_question_by_value={
            "low": "Did the agent explain JavaScript-specific changes accessibly enough?",
            "high": "Did the agent provide enough JavaScript-specific technical detail?",
        },
        base_priority=10,
    ),
    SalienceSpec(
        key="typescript_expertise",
        source="coding_specific.abstract",
        triggers=("typescript", "tsx", "tsc", "nestjs", "angular"),
        behavior_by_value={
            "low": "Prefer accessible TypeScript explanations and concrete verification output.",
            "high": "Can evaluate TypeScript details and may ask sharper technical questions.",
        },
        rubric_key="language_fit",
        rubric_question_by_value={
            "low": "Did the agent explain TypeScript-specific changes accessibly enough?",
            "high": "Did the agent provide enough TypeScript-specific technical detail?",
        },
        base_priority=10,
    ),
    SalienceSpec(
        key="primary_ecosystem",
        source="professional_background",
        triggers=(),
        behavior_by_value={},
        default_behavior="Let the user's usual ecosystem shape how familiar or cautious their feedback sounds.",
        rubric_key="domain_context_fit",
        rubric_question_by_value={},
        default_question="Did the agent communicate in a way that fit the user's technical background and ecosystem familiarity?",
        base_priority=8,
    ),
    SalienceSpec(
        key="seniority",
        source="professional_background",
        triggers=("tradeoff", "risk", "explain", "architecture", "review"),
        behavior_by_value={},
        default_behavior="Adjust technical depth and confidence to the user's seniority.",
        rubric_key="communication_fit",
        rubric_question_by_value={},
        default_question="Did the agent communicate with the right level of technical depth for this user?",
        base_priority=8,
    ),
    SalienceSpec(
        key="trait",
        source="preference",
        triggers=("message", "conversation", "tone", "prompt"),
        behavior_by_value={
            "introverted": "Avoid unnecessary back-and-forth and keep messages purposeful.",
            "extroverted": "A slightly more conversational style is acceptable.",
        },
        rubric_key="communication_fit",
        rubric_question_by_value={
            "introverted": "Did the agent avoid unnecessary conversational overhead?",
            "extroverted": "Did the agent engage in a collaborative but useful way?",
        },
        base_priority=5,
    ),
    SalienceSpec(
        key="emotion",
        source="preference",
        triggers=("error", "failed", "blocked", "frustrat", "urgent"),
        behavior_by_value={
            "anxious": "Need clear progress and risk-reducing evidence.",
            "angry": "React bluntly to repeated mistakes or contradictions.",
            "excited": "Tolerate a more upbeat pace, but still require task-relevant evidence.",
            "calm": "React evenly and focus on substance.",
            "sad": "Prefer low-friction, clear support and avoid terse dismissive replies.",
        },
        rubric_key="communication_fit",
        rubric_question_by_value={},
        default_question="Did the agent's communication fit the user's emotional state and tolerance for friction?",
        base_priority=5,
    ),
    SalienceSpec(
        key="conscientiousness",
        source="preference",
        triggers=("edge", "test", "style", "verify", "clean", "detail"),
        behavior_by_value={
            "organized": "Track edge cases, cleanliness, and complete evidence.",
            "easygoing": "Do not over-index on minor process details if the task is solved.",
        },
        rubric_key="preference_fit",
        rubric_question_by_value={
            "organized": "Did the agent satisfy this user's attention to detail and completeness?",
            "easygoing": "Did the agent solve the task without unnecessary process burden?",
        },
        base_priority=12,
    ),
    SalienceSpec(
        key="agreeableness",
        source="preference",
        triggers=("mistake", "wrong", "contradict", "push", "review"),
        behavior_by_value={
            "critical": "Push back directly when evidence is missing or claims conflict.",
            "agreeable": "Phrase follow-ups cooperatively while still enforcing requirements.",
        },
        rubric_key="communication_fit",
        rubric_question_by_value={
            "critical": "Did the agent withstand and resolve direct critical feedback?",
            "agreeable": "Did the agent collaborate smoothly with a cooperative user?",
        },
        base_priority=8,
    ),
)


def validate_salience_vocabulary() -> list[str]:
    """Report salience keys, sources, or values that drift from persona-v1."""
    schema_specs = DEFAULT_SCHEMA.categorical_specs()
    errors: list[str] = []
    for spec in SPECS:
        schema_spec = schema_specs.get(spec.key)
        if schema_spec is None:
            errors.append(f"unknown persona attribute key: {spec.key}")
            continue
        expected_source = CANONICAL_ATTRIBUTE_SOURCES[spec.key]
        if spec.source != expected_source:
            errors.append(
                f"non-canonical source for {spec.key}: {spec.source}; "
                f"expected {expected_source}"
            )
        used_values = set(spec.behavior_by_value) | set(spec.rubric_question_by_value)
        unknown_values = sorted(used_values - set(schema_spec.values))
        if unknown_values:
            errors.append(
                f"non-canonical values for {spec.key}: {', '.join(unknown_values)}"
            )
    for key, source in INTERACTION_POLICY_PATHS.items():
        if key not in schema_specs:
            errors.append(f"unknown interaction-policy attribute key: {key}")
        elif source != CANONICAL_ATTRIBUTE_SOURCES[key]:
            errors.append(
                f"non-canonical interaction-policy source for {key}: {source}; "
                f"expected {CANONICAL_ATTRIBUTE_SOURCES[key]}"
            )
    unknown_task_keys = sorted(TASK_SALIENCE_KEYS - set(schema_specs))
    if unknown_task_keys:
        errors.append(
            "unknown task-salience attribute keys: " + ", ".join(unknown_task_keys)
        )
    unknown_background_keys = sorted(BACKGROUND_ATTRIBUTE_KEYS - set(schema_specs))
    if unknown_background_keys:
        errors.append(
            "unknown background attribute keys: " + ", ".join(unknown_background_keys)
        )
    return errors


_VOCABULARY_ERRORS = validate_salience_vocabulary()
if _VOCABULARY_ERRORS:  # pragma: no cover - configuration invariant
    raise ValueError("invalid salience persona vocabulary: " + "; ".join(_VOCABULARY_ERRORS))


def _value_for_spec(persona: dict[str, Any], spec: SalienceSpec) -> str:
    if spec.source == "coding_specific.concrete":
        return _get(persona, "coding_specific", "concrete", spec.key)
    if spec.source == "coding_specific.abstract":
        return _get(persona, "coding_specific", "abstract", spec.key)
    if spec.source == "scenario_specific.terminal":
        return _get(persona, "scenario_specific", "terminal", spec.key)
    if spec.source == "scenario_specific.vscode":
        return _get(persona, "scenario_specific", "vscode", spec.key)
    if spec.source == "professional_background":
        return _get(persona, "professional_background", spec.key)
    if spec.source == "preference":
        return _get(persona, "preference", spec.key)
    return ""


def extract_persona_salience(
    persona: dict[str, Any],
    task_text: str = "",
    *,
    max_items: int = 3,
    min_items: int = 0,
) -> list[dict[str, Any]]:
    """Return only persona attributes supported by canonical task signals.

    Stable interaction preferences are deliberately excluded; they are activated
    later from observable turn events.
    """
    if not isinstance(persona, dict) or not persona:
        return []

    items: list[dict[str, Any]] = []
    for spec in SPECS:
        if spec.key not in TASK_SALIENCE_KEYS:
            continue
        value = _value_for_spec(persona, spec)
        if not value:
            continue
        trigger_hits = _trigger_hits_for_spec(task_text, spec)
        task_bonus = _task_kind_bonus(task_text, spec.key, value)
        if not trigger_hits and not task_bonus:
            continue
        priority = min(spec.base_priority, 10) + 12 * min(len(trigger_hits), 3) + task_bonus
        behavior = spec.behavior_by_value.get(value) or spec.default_behavior
        question = spec.rubric_question_by_value.get(value) or spec.default_question
        if not behavior or not question:
            continue
        items.append(
            {
                "key": spec.key,
                "value": value,
                "source": spec.source,
                "persona_attribute": canonical_persona_attribute(
                    spec.key, value, spec.source
                ),
                "priority": priority,
                "reason": (
                    f"Canonical task mentions {', '.join(trigger_hits[:4])}"
                    if trigger_hits
                    else "Canonical task matches this persona's ecosystem"
                ),
                "behavior": behavior,
                "rubric_key": spec.rubric_key,
                "rubric_question": question,
                "rubric_weight": spec.weight,
            }
        )

    items.sort(key=lambda x: (-int(x["priority"]), str(x["source"]), str(x["key"])))
    if not items:
        return []
    cutoff = min(max(0, max_items), len(items))
    if min_items:
        cutoff = min(max(cutoff, min_items), len(items))
    return items[:cutoff]


def extract_background_calibration(
    persona: dict[str, Any],
    task_text: str = "",
    *,
    max_items: int = 4,
) -> list[dict[str, Any]]:
    """Select real background/expertise attributes relevant to this task."""
    if not isinstance(persona, dict) or not persona:
        return []

    items: list[dict[str, Any]] = []
    for spec in SPECS:
        if spec.key not in BACKGROUND_ATTRIBUTE_KEYS:
            continue
        value = _value_for_spec(persona, spec)
        if not value:
            continue
        trigger_hits = _trigger_hits_for_spec(task_text, spec)
        task_bonus = _task_kind_bonus(task_text, spec.key, value)
        if not trigger_hits and not task_bonus:
            continue
        behavior = spec.behavior_by_value.get(value) or spec.default_behavior
        question = spec.rubric_question_by_value.get(value) or spec.default_question
        if not behavior or not question:
            continue
        priority = min(spec.base_priority, 10) + 12 * min(len(trigger_hits), 3) + task_bonus
        items.append(
            {
                "key": spec.key,
                "value": value,
                "source": spec.source,
                "persona_attribute": canonical_persona_attribute(
                    spec.key, value, spec.source
                ),
                "priority": priority,
                "reason": (
                    f"Canonical task mentions {', '.join(trigger_hits[:4])}"
                    if trigger_hits
                    else "Canonical task matches this persona's ecosystem"
                ),
                "behavior": behavior,
                "rubric_key": spec.rubric_key,
                "rubric_question": question,
                "rubric_weight": spec.weight,
            }
        )

    items.sort(key=lambda x: (-int(x["priority"]), str(x["source"]), str(x["key"])))
    return items[: max(0, max_items)]


def extract_interaction_policy(persona: dict[str, Any]) -> dict[str, dict[str, str]]:
    """Extract stable preferences without treating them as task relevance."""
    if not isinstance(persona, dict):
        return {}
    policy: dict[str, dict[str, str]] = {}
    for key, source in INTERACTION_POLICY_PATHS.items():
        if source == "coding_specific.concrete":
            value = _get(persona, "coding_specific", "concrete", key)
        else:
            value = _get(persona, "scenario_specific", "terminal", key)
        if value:
            policy[key] = {"value": value, "source": source}
    return policy


def compile_interaction_contract(persona: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Compile canonical persona values into operational interaction expectations.

    This is a deterministic view over the existing persona schema. It does not
    replace the raw persona and does not infer task-specific requirements. Each
    entry preserves its exact schema attribution so simulator behavior can be
    audited against the source persona.
    """
    if not isinstance(persona, dict) or not persona:
        return {}

    policy = extract_interaction_policy(persona)
    contract: dict[str, dict[str, Any]] = {}

    def add(
        contract_key: str,
        persona_key: str,
        *,
        expectation_by_value: dict[str, str],
        priority_by_value: dict[str, str],
        active_from: str = "",
        active_when: str = "",
        operationalization: str = "",
    ) -> None:
        raw = policy.get(persona_key)
        if not raw:
            return
        value = str(raw.get("value") or "")
        source = str(raw.get("source") or "")
        expectation = expectation_by_value.get(value)
        priority = priority_by_value.get(value)
        if not expectation or not priority:
            return
        entry: dict[str, Any] = {
            "expectation": expectation,
            "priority": priority,
            "persona_attribute": canonical_persona_attribute(
                persona_key, value, source
            ),
        }
        if active_from:
            entry["active_from"] = active_from
        if active_when:
            entry["active_when"] = active_when
        if operationalization:
            entry["operationalization"] = operationalization
        contract[contract_key] = entry

    add(
        "planning",
        "agent_mode_preference",
        expectation_by_value={
            "plan-first": "The agent should present a short plan before editing.",
            "interactive": (
                "The agent may work interactively, giving a concise approach and "
                "asking only task-relevant clarifying questions."
            ),
            "autopilot": (
                "The agent may proceed autonomously without waiting for plan approval, "
                "while reporting its approach and material progress concisely."
            ),
        },
        priority_by_value={
            "plan-first": "critical",
            "interactive": "medium",
            "autopilot": "low",
        },
        active_from="opening",
    )
    add(
        "command_confirmation",
        "command_confirmation",
        expectation_by_value={
            "confirm-each": (
                "The agent should request confirmation before each visible shell or "
                "write command."
            ),
            "auto-run": (
                "The agent may run ordinary commands without per-command confirmation; "
                "risky or irreversible actions still require an explicit warning."
            ),
        },
        priority_by_value={"confirm-each": "critical", "auto-run": "low"},
        active_from="opening",
        operationalization=(
            "The current harness enforces confirmation at the visible command-batch "
            "boundary; hidden tool traces are not judge evidence."
        ),
    )
    add(
        "explanation",
        "explanation_demand",
        expectation_by_value={
            "wants-explanation": (
                "Explain architecture at a high level, then provide enough relevant "
                "language-level detail to understand the implementation and "
                "cross-component risks."
            ),
            "outcome-only": (
                "Prioritize the result, user-visible behavior, and material risks; "
                "avoid unsolicited implementation walkthroughs."
            ),
        },
        priority_by_value={"wants-explanation": "high", "outcome-only": "medium"},
        active_from="opening",
    )
    add(
        "testing",
        "testing_preference",
        expectation_by_value={
            "require-tests": (
                "Report targeted feature tests and relevant regression tests, including "
                "the exact commands, scope, and outcomes."
            ),
            "skip-tests": (
                "Do not make running or adding tests a default requirement unless the "
                "task or visible risk makes verification necessary."
            ),
        },
        priority_by_value={"require-tests": "critical", "skip-tests": "low"},
        active_from="opening",
    )
    add(
        "documentation",
        "documentation_preference",
        expectation_by_value={
            "required": (
                "Update relevant user-facing documentation when the change introduces "
                "a public API or observable behavior."
            ),
            "minimal": (
                "Keep documentation changes minimal and add them only when needed to "
                "use or understand public behavior."
            ),
        },
        priority_by_value={"required": "high", "minimal": "low"},
        active_when="the task changes public API or observable behavior",
    )
    add(
        "style",
        "code_style_strictness",
        expectation_by_value={
            "strict": (
                "Follow repository conventions and report relevant formatter, lint, "
                "and type-check results instead of requiring a line-by-line walkthrough."
            ),
            "lenient": (
                "Prefer functional consistency with nearby code; do not create extra "
                "style work unless it affects correctness or maintainability."
            ),
        },
        priority_by_value={"strict": "high", "lenient": "low"},
        active_when="visible code changes are ready for review",
    )
    add(
        "trust",
        "trust_in_agent",
        expectation_by_value={
            "low": (
                "Treat unsupported completion claims as insufficient and request "
                "concrete visible evidence without becoming reflexively adversarial."
            ),
            "high": (
                "Accept well-supported progress and completion claims without demanding "
                "redundant evidence."
            ),
        },
        priority_by_value={"low": "high", "high": "medium"},
        active_when="the agent makes a completion or verification claim",
    )
    add(
        "iteration",
        "interaction_depth_preference",
        expectation_by_value={
            "iterative": (
                "Raise one high-value concern at a time and allow the agent to respond "
                "before introducing the next concern."
            ),
            "one-shot": (
                "Prefer one consolidated request and avoid unnecessary follow-up turns "
                "once the requested result is adequately supported."
            ),
        },
        priority_by_value={"iterative": "medium", "one-shot": "low"},
        active_from="opening",
    )
    add(
        "time_sensitivity",
        "time_sensitivity",
        expectation_by_value={
            "high": (
                "Keep planning and routine progress concise, avoid repeated or "
                "low-value exploration, and prioritize the shortest evidence-backed "
                "path to completion."
            ),
            "low": (
                "Allow additional investigation and explanation when it materially "
                "improves confidence, even if the interaction takes longer."
            ),
        },
        priority_by_value={"high": "high", "low": "low"},
        active_from="opening",
    )
    add(
        "failure_recovery",
        "failure_recovery_strategy",
        expectation_by_value={
            "retry-or-correct": (
                "After a visible failure, ask for a diagnosis and one evidence-based "
                "retry; if it fails again, request a corrected approach."
            ),
            "abandon-after-failure": (
                "After a visible failure, avoid repeated retries and ask for the "
                "limitation or a safe stopping point."
            ),
        },
        priority_by_value={
            "retry-or-correct": "medium",
            "abandon-after-failure": "medium",
        },
        active_when="a visible command, test, or implementation attempt fails",
    )
    return contract


def format_interaction_contract(contract: dict[str, dict[str, Any]]) -> str:
    """Render a compiled contract for private simulator context."""
    if not contract:
        return "(no interaction contract could be compiled)"
    lines: list[str] = []
    for key, entry in contract.items():
        timing = ""
        if entry.get("active_from"):
            timing = f"active_from={entry['active_from']}"
        elif entry.get("active_when"):
            timing = f"active_when={entry['active_when']}"
        qualifiers = "; ".join(
            part for part in (f"priority={entry.get('priority', '')}", timing) if part
        )
        lines.append(f"- {key} [{qualifiers}]: {entry.get('expectation', '')}")
        if entry.get("operationalization"):
            lines.append(f"  Harness note: {entry['operationalization']}")
    return "\n".join(lines)


def _has_verification_evidence(agent_output: str) -> bool:
    """Return whether visible output reports a verification result, not a plan."""
    patterns = (
        r"\b\d+\s+(?:tests?\s+)?(?:passed|failed|skipped|xfailed|xpassed)\b",
        r"\b(?:passed|failed|skipped)\s*[:=]\s*\d+\b",
        r"\btests?\s+(?:all\s+)?(?:passed|failed|were skipped)\b",
        r"\b(?:all|focused|full)\s+(?:tests?|test suite)\s+(?:passed|failed|is green)\b",
        r"\btest result\s*:\s*(?:ok|passed|failed)\b",
        r"\bran\s+\d+\s+tests?\b",
        r"\bno tests? (?:ran|were run)\b",
        r"\btests? (?:were )?not run\b",
        r"\b(?:verification|typecheck|lint|build)\s*[:=-]\s*(?:passed|failed|successful|green)\b",
        r"\bexit (?:code|status)\s*[:=]?\s*[0-9]+\b",
    )
    return any(re.search(pattern, agent_output, flags=re.I) for pattern in patterns)


def _has_failure_evidence(agent_output: str) -> bool:
    """Return whether visible output reports an actual failed operation."""
    patterns = (
        r"\b(?:command|build|tests?|pytest|typecheck|lint|verification)\s+"
        r"(?:has\s+|have\s+|is\s+|are\s+)?(?:failed|failing)\b",
        r"\bfailed\s+(?:with|because|due to|after|at)\b",
        r"\b(?:exit(?:ed)?|returned)\s+(?:with\s+)?(?:code|status)?\s*[:=]?\s*[1-9]\d*\b",
        r"\bexit (?:code|status)\s*[:=]?\s*[1-9]\d*\b",
        r"\b\d+\s+(?:tests?\s+)?failed\b",
        r"\btests?\s*:\s*\d+\s+failed\b",
        r"\btest result\s*:\s*failed\b",
        r"\b(?:one|a|\d+)\s+remaining errors?\b",
        r"\berror\s+[A-Z]{2,}\d+\s*:",
        r"(?m)^FAILED\s+\S+",
        r"(?m)^ERROR\s+(?:collecting|at setup|at teardown)\b",
        r"(?m)^(?:FAIL\s+\S+|--- FAIL:)",
        r"\bnpm ERR!\b",
        r"(?m)^\s*(?:panic|fatal):",
        r"\btraceback \(most recent call last\)",
        r"\bcommand not found\b",
        r"\b(?:timed out|timeout after)\b",
        r"\b(?:unable to|could not|blocked by)\b",
    )
    return any(re.search(pattern, agent_output, flags=re.I) for pattern in patterns)


def activate_persona_preferences(
    persona: dict[str, Any],
    *,
    agent_output: str = "",
    agent_activity: str = "",
    code_diff: str = "",
    elapsed_sec: float = 0.0,
    turn_duration_sec: float = 0.0,
    completion_attempt: bool = False,
    command_gate_mode: str = "",
    max_items: int = 5,
) -> list[dict[str, Any]]:
    """Activate stable preferences from evidence visible in the current turn."""
    policy = extract_interaction_policy(persona)
    candidates: dict[str, dict[str, Any]] = {}

    def add(key: str, reason: str, priority: int) -> None:
        entry = policy.get(key)
        if not entry:
            return
        existing = candidates.get(key)
        if existing and int(existing["priority"]) >= priority:
            return
        candidates[key] = {
            "key": key,
            "value": entry["value"],
            "source": entry["source"],
            "persona_attribute": canonical_persona_attribute(
                key, entry["value"], entry["source"]
            ),
            "reason": reason,
            "priority": priority,
            "instruction": _POLICY_INSTRUCTIONS[key],
        }

    activity_visible = bool(
        agent_activity.strip() and agent_activity.strip().lower() not in {"(none)", "none"}
    )
    diff_visible = bool(code_diff.strip() and "no code changes" not in code_diff.lower())
    output_and_diff = f"{agent_output}\n{code_diff}"
    failure_visible = _has_failure_evidence(agent_output)
    verification_visible = _has_verification_evidence(agent_output)
    dependency_visible = bool(
        re.search(
            r"(?im)^(?:\+\+\+|---) [ab]/.*(?:requirements[^/]*\.txt|pyproject\.toml|"
            r"package(?:-lock)?\.json|pnpm-lock\.yaml|yarn\.lock|pom\.xml|build\.gradle)"
            r"|^\+.*\b(?:dependency|dependencies|pip install|npm install)\b",
            code_diff,
        )
    )
    documentation_visible = bool(
        re.search(
            r"(?im)^(?:\+\+\+|---) [ab]/.*(?:README|CHANGELOG|docs?/|\.md$)"
            r"|^\+.*\b(?:deprecat(?:e|ed|ion)|public API|docstring)\b",
            code_diff,
        )
    )
    performance_visible = bool(
        re.search(
            r"\b(performance|latency|benchmark|throughput|memory|complexity|runtime)\b",
            output_and_diff,
            flags=re.I,
        )
    )

    if command_gate_mode == "proposal_only":
        if policy.get("command_confirmation", {}).get("value") == "confirm-each":
            add(
                "command_confirmation",
                "the pre-execution gate is waiting for explicit approval",
                120,
            )
        if policy.get("tool_permission", {}).get("value") == "restrictive":
            add(
                "tool_permission",
                "the restrictive pre-execution gate is waiting for scoped approval",
                118,
            )
    if activity_visible:
        if policy.get("command_confirmation", {}).get("value") == "confirm-each":
            add("command_confirmation", "tool activity became visible this turn", 110)
        if policy.get("tool_permission", {}).get("value") == "restrictive":
            add("tool_permission", "tool activity became visible this turn", 96)
    if diff_visible:
        add("review_granularity", "a non-empty incremental diff became visible", 86)
        add("code_style_strictness", "a non-empty incremental diff became visible", 80)
        add("scope_stability", "a non-empty incremental diff can be checked against task scope", 74)
    if dependency_visible:
        add("dependency_preference", "the incremental diff changes dependency configuration", 108)
    if documentation_visible:
        add("documentation_preference", "the incremental diff changes documentation or public API text", 108)
    if performance_visible:
        add("performance_sensitivity", "visible output or diff raises a performance concern", 108)
    if failure_visible:
        add("failure_recovery_strategy", "the agent visibly reported a failure or blocker", 106)
        add("error_tolerance", "the agent visibly reported a failure or blocker", 102)
        add("trust_in_agent", "visible failure evidence affects confidence", 99)
    if completion_attempt or verification_visible:
        reason = (
            "the agent is making a completion claim"
            if completion_attempt
            else "the agent exposed verification evidence"
        )
        add("testing_preference", reason, 104)
        add("trust_in_agent", reason, 100)
        add("explanation_demand", reason, 76)
        add("terminal_output_verbosity", reason, 70)
    if completion_attempt:
        add(
            "interaction_depth_preference",
            "a completion claim requires deciding whether to follow up or finish",
            78,
        )
    if elapsed_sec >= 300.0 or turn_duration_sec >= 120.0:
        add("time_sensitivity", "visible elapsed time crossed the interaction threshold", 72)

    activated = sorted(
        candidates.values(),
        key=lambda item: (-int(item["priority"]), str(item["key"])),
    )
    return activated[: max(0, max_items)]


def activate_background_attributes(
    calibration: list[dict[str, Any]],
    *,
    agent_output: str = "",
    max_items: int = 4,
) -> list[dict[str, Any]]:
    """Activate calibrated background only when technical expression is visible."""
    output = agent_output.strip()
    if not output or output.lower() in {"(none)", "(no final agent message)"}:
        return []

    plan_only = bool(
        re.search(
            r"\b(?:i(?:'ll| will)|next i(?:'ll| will)|plan(?:ned)? to)\b",
            output,
            re.I,
        )
        and not re.search(
            r"\b(?:changed|implemented|fixed|root cause|because|now|works by|"
            r"the issue was|this means|prevents|ensures)\b",
            output,
            re.I,
        )
    )
    technical_explanation = not plan_only and bool(
        re.search(
            r"\b(?:because|root cause|the issue was|works by|this means|changed|"
            r"implemented|fixed|now|prevents|ensures|behavior|flow|precedence|"
            r"compatibility|trade-?off|inheritance|dispatch|serialization|parsing)\b",
            output,
            re.I,
        )
    )
    repo_explanation = technical_explanation and bool(
        re.search(
            r"(?:\b[\w.-]+\.(?:py|js|ts|tsx|java|cc|cpp|h)\b|"
            r"\b(?:module|class|function|method|call path|repository|package|"
            r"middleware|router|handler|fixture|test suite)\b)",
            output,
            re.I,
        )
    )
    language_explanation = technical_explanation and bool(
        re.search(
            r"\b(?:python|java|javascript|typescript|c\+\+|async|await|"
            r"decorator|exception|type|generator|iterator|coroutine|pytest|"
            r"junit|promise)\b",
            output,
            re.I,
        )
    )
    if not technical_explanation:
        return []

    activated: list[dict[str, Any]] = []
    language_keys = set(LANGUAGE_ATTRIBUTE_TO_LANGUAGE)
    for raw in calibration:
        key = str(raw.get("key") or "")
        event = ""
        if key == "repo_expertise" and repo_explanation:
            event = "the agent gave a repository-specific technical explanation"
        elif (
            key in language_keys
            and language_explanation
            and background_attribute_has_evidence(raw, output)
        ):
            event = "the agent gave a language-specific technical explanation"
        elif key in {"coding_expertise", "seniority"}:
            event = "the agent gave a technical explanation"
        elif key == "primary_ecosystem" and repo_explanation:
            event = "the agent explained ecosystem-specific behavior"
        if not event:
            continue
        item = dict(raw)
        item["reason"] = event
        item["instruction"] = str(raw.get("behavior") or "")
        activated.append(item)

    activated.sort(
        key=lambda item: (-int(item.get("priority") or 0), str(item.get("key") or ""))
    )
    return activated[: max(0, max_items)]


_LANGUAGE_EVIDENCE_PATTERNS = {
    "python_expertise": r"(?:\bpython\b|\bpytest\b|\.py\b|\bdjango\b|\bfastapi\b)",
    "java_expertise": r"(?:\bjava\b|\bjunit\b|\bgradle\b|\bmaven\b|\.java\b)",
    "cpp_expertise": r"(?:\bc\+\+\b|\bcpp\b|\bcmake\b|\bclang\b|\bgcc\b|\.(?:cc|cpp|hpp)\b)",
    "javascript_expertise": r"(?:\bjavascript\b|\bnode(?:js)?\b|\bjest\b|\beslint\b|\.jsx?\b)",
    "typescript_expertise": r"(?:\btypescript\b|\btsc\b|\bvitest\b|\bpnpm\b|\bPromiseLike\b|\.tsx?\b)",
}


def background_attribute_has_evidence(
    attribute: dict[str, Any], agent_output: str
) -> bool:
    """Return whether output supports activation of this exact background trait."""
    persona_attribute = attribute.get("persona_attribute")
    key = str(attribute.get("key") or "")
    if not key and isinstance(persona_attribute, dict):
        key = str(persona_attribute.get("key") or "")
    pattern = _LANGUAGE_EVIDENCE_PATTERNS.get(key)
    return True if pattern is None else bool(re.search(pattern, agent_output, re.I))


def format_background_calibration(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(no task-relevant background attribute was calibrated)"
    return "\n".join(
        f"- {item['key']}={item['value']} ({item['source']}): {item['behavior']}"
        for item in items
    )


def format_active_background(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(no technical-expression opportunity activated this turn)"
    return "\n".join(
        f"- {item['key']}={item['value']}: {item['instruction']} "
        f"[activation: {item['reason']}]"
        for item in items
    )


def format_active_preferences(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(none activated by this turn's visible events)"
    return "\n".join(
        f"- {item['key']}={item['value']}: {item['instruction']} "
        f"[activation: {item['reason']}]"
        for item in items
    )


def format_salience_for_prompt(items: list[dict[str, Any]]) -> str:
    if not items:
        return "(no task-salient persona attribute matched the canonical task; do not backfill unrelated persona attributes)"
    lines = []
    for item in items:
        lines.append(
            "- {key}={value} ({source}; priority {priority}): {behavior}".format(
                key=item.get("key", ""),
                value=item.get("value", ""),
                source=item.get("source", ""),
                priority=item.get("priority", ""),
                behavior=item.get("behavior", ""),
            )
        )
    return "\n".join(lines)


def opening_persona_guidance(
    persona: dict[str, Any],
    task_text: str = "",
) -> dict[str, Any]:
    """Select persona signals that may shape the first user message.

    Surface-form preferences always shape the opening. At most one process
    boundary is exposed, and only when the coding agent must know it before
    acting. Other preferences remain available to the multi-turn policy.
    """
    concrete = persona.get("coding_specific", {}).get("concrete", {})
    terminal = persona.get("scenario_specific", {}).get("terminal", {})
    if not isinstance(concrete, dict):
        concrete = {}
    if not isinstance(terminal, dict):
        terminal = {}

    style = {
        "prompt_verbosity": str(concrete.get("prompt_verbosity") or ""),
        "prompt_structure": str(concrete.get("prompt_structure") or ""),
        "context_provision": str(concrete.get("context_provision") or ""),
        "specification_completeness": str(
            concrete.get("specification_completeness") or ""
        ),
    }

    boundary: dict[str, str] | None = None
    if terminal.get("command_confirmation") == "confirm-each":
        boundary = {
            "key": "command_confirmation",
            "value": "confirm-each",
            "instruction": (
                "Ask the agent to propose its first shell or test command and wait "
                "for approval before running it."
            ),
        }
    elif concrete.get("tool_permission") == "restrictive":
        boundary = {
            "key": "tool_permission",
            "value": "restrictive",
            "instruction": (
                "Ask the agent to propose its first command batch and intended file "
                "changes, then wait for approval before executing them."
            ),
        }
    elif (
        terminal.get("agent_mode_preference") == "plan-first"
        and not _contains_any(
            task_text,
            (
                "before implementing",
                "before implementation",
                "first explore",
                "first inspect",
                "start by",
                "propose a plan",
                "outline an approach",
            ),
        )
    ):
        boundary = {
            "key": "agent_mode_preference",
            "value": "plan-first",
            "instruction": (
                "Ask for a brief task-specific approach before implementation begins."
            ),
        }
    elif (
        terminal.get("destructive_command_caution") == "cautious"
        and _contains_any(
            task_text,
            (
                "delete",
                "drop",
                "reset",
                "overwrite",
                "migration",
                "production",
                "credential",
                "secret",
                "destructive",
            ),
        )
    ):
        boundary = {
            "key": "destructive_command_caution",
            "value": "cautious",
            "instruction": (
                "Ask the agent to confirm before any destructive or irreversible action."
            ),
        }

    return {
        "style": style,
        "explicit_boundary": boundary,
        "deferred_preference_keys": [
            "trust_in_agent",
            "testing_preference",
            "review_granularity",
            "explanation_demand",
            "documentation_preference",
            "code_style_strictness",
            "performance_sensitivity",
        ],
    }


def format_opening_persona_guidance(guidance: dict[str, Any]) -> str:
    """Render deterministic opening guidance for an LLM prompt."""
    style = guidance.get("style") or {}
    verbosity = style.get("prompt_verbosity") or "natural"
    structure = style.get("prompt_structure") or "natural-language"
    context = style.get("context_provision") or "task-appropriate"
    completeness = style.get("specification_completeness") or "task-appropriate"
    boundary = guidance.get("explicit_boundary")

    length_instruction = {
        "concise": "Use one or two compact sentences.",
        "verbose": "Use three to six natural sentences when the task has enough concrete detail.",
    }.get(verbosity, "Use a natural task-appropriate length.")
    form_instruction = {
        "natural-language": "Write natural prose rather than a checklist.",
        "structured": (
            "Use bullets only when the canonical task contains multiple distinct requirements."
        ),
    }.get(structure, "Use a natural task-appropriate form.")
    context_instruction = {
        "minimal": "Include only the essential goal and the most relevant symptom or constraint.",
        "rich": "Include concrete task context already present in the canonical request.",
    }.get(context, "Include only task-relevant context.")
    completeness_instruction = {
        "complete": "Preserve the canonical request's explicit acceptance criteria.",
        "underspecified": (
            "Preserve the canonical task, but do not invent additional acceptance criteria."
        ),
    }.get(completeness, "Do not invent requirements.")

    lines = [
        f"- Length ({verbosity}): {length_instruction}",
        f"- Form ({structure}): {form_instruction}",
        f"- Up-front context ({context}): {context_instruction}",
        f"- Requirement completeness ({completeness}): {completeness_instruction}",
    ]
    if isinstance(boundary, dict) and boundary.get("instruction"):
        lines.append(
            "- The one process boundary that may be stated explicitly: "
            f"{boundary['instruction']}"
        )
    else:
        lines.append(
            "- No process boundary needs to be stated explicitly; do not invent one."
        )
    return "\n".join(lines)


def salience_rubric_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for item in items:
        key = str(item.get("rubric_key") or "").strip()
        question = str(item.get("rubric_question") or "").strip()
        if not key or not question or key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "key": key,
                "question": question,
                "weight": float(item.get("rubric_weight") or 1.0),
                "source": "persona_salience",
                "persona_attribute": dict(item.get("persona_attribute") or {}),
                "rationale": f"{item.get('key')}={item.get('value')}: {item.get('reason')}",
            }
        )
    return out
