"""The :class:`Persona` configuration class.

A :class:`Persona` is one concrete, fully-instantiated simulated user: every
attribute in the schema has been assigned a single chosen value. The class is
serialisable to/from the JSON persona database and can render itself into a
natural-language system prompt for the user simulator.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .schema import DEFAULT_SCHEMA, SCENARIOS, PersonaSchema


@dataclass
class Persona:
    """A single instantiated simulated user.

    Attributes are stored as plain ``name -> chosen_value`` mappings grouped the
    same way as the schema so the object round-trips cleanly to JSON.
    """

    basic_information: Dict[str, Any] = field(default_factory=dict)
    professional_background: Dict[str, str] = field(default_factory=dict)
    preference: Dict[str, str] = field(default_factory=dict)
    coding_concrete: Dict[str, str] = field(default_factory=dict)
    coding_abstract: Dict[str, str] = field(default_factory=dict)
    # scenario -> {attribute: value}
    scenario_specific: Dict[str, Dict[str, str]] = field(default_factory=dict)
    # Optional pre-baked natural-language summary (see ``src.personas.summarize``).
    # When present it can be used *directly* as the user-simulator system prompt
    # instead of rendering the structured attributes above.
    summary: Optional[str] = None

    # ----- serialisation ---------------------------------------------------
    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable dict mirroring ``persona_schema_v1.md``."""
        data: Dict[str, Any] = {
            "basic_information": dict(self.basic_information),
            "professional_background": dict(self.professional_background),
            "preference": dict(self.preference),
            "coding_specific": {
                "concrete": dict(self.coding_concrete),
                "abstract": dict(self.coding_abstract),
            },
            "scenario_specific": {k: dict(v) for k, v in self.scenario_specific.items()},
        }
        if self.summary is not None:
            data["summary"] = self.summary
        return data

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Persona":
        coding = data.get("coding_specific", {})
        return cls(
            basic_information=dict(data.get("basic_information", {})),
            professional_background=dict(data.get("professional_background", {})),
            preference=dict(data.get("preference", {})),
            coding_concrete=dict(coding.get("concrete", {})),
            coding_abstract=dict(coding.get("abstract", {})),
            scenario_specific={
                k: dict(v) for k, v in data.get("scenario_specific", {}).items()
            },
            summary=data.get("summary"),
        )

    @property
    def id(self) -> Optional[str]:
        return self.basic_information.get("id")

    # ----- prompt rendering ------------------------------------------------
    def to_system_prompt(
        self,
        scenario: Optional[str] = None,
        schema: PersonaSchema = DEFAULT_SCHEMA,
        strategy: str = "structured",
    ) -> str:
        """Render the persona into a system prompt for the user simulator.

        Two loading strategies are supported:

        - ``"structured"`` (default): render the structured persona attributes
          (basic info + preferences + coding + the chosen scenario) into a prompt.
        - ``"summary"``: use the persona's pre-baked natural-language
          :attr:`summary` *directly* as the system prompt (see
          ``src.personas.summarize``). ``scenario`` is ignored in this mode since
          the summary already folds in every attribute.
        - ``"auto"``: use the summary when one is present, otherwise fall back to
          structured rendering.

        Parameters
        ----------
        scenario:
            If one of :data:`schema.SCENARIOS` (e.g. ``"terminal"``/``"vscode"``),
            only that scenario's attributes are included. ``None`` produces a
            general, scenario-agnostic persona (all scenario blocks omitted).
            Only relevant for ``strategy="structured"``.
        schema:
            Schema providing the human-readable attribute descriptions.
        strategy:
            Persona-loading strategy: ``"structured"``, ``"summary"`` or ``"auto"``.
        """
        if strategy not in ("structured", "summary", "auto"):
            raise ValueError(
                f"unknown strategy {strategy!r}; expected 'structured', 'summary' or 'auto'"
            )
        if strategy == "auto":
            strategy = "summary" if self.summary else "structured"
        if strategy == "summary":
            if not self.summary:
                raise ValueError(
                    "persona has no summary; cannot render with strategy='summary'. "
                    "Generate one with src.personas.summarize or use strategy='structured'."
                )
            return self.summary

        if scenario is not None and scenario not in SCENARIOS:
            raise ValueError(f"unknown scenario {scenario!r}; expected one of {SCENARIOS}")

        specs = schema.categorical_specs()
        lines: List[str] = []

        bi = self.basic_information
        identity = (
            f"You are {bi.get('name', 'a software user')}, "
            f"a {bi.get('age', 'n/a')}-year-old {bi.get('occupation', 'developer')}"
        )
        edu = bi.get("education")
        if edu:
            identity += f" with a background in {edu}"
        region = bi.get("region")
        if region:
            identity += f", based in {region}"
        lines.append(identity + ".")
        lines.append(
            "You are interacting with an AI coding agent. Stay fully in character and "
            "let the preferences below drive how you write prompts, react to the agent's "
            "work, give feedback, and approve or reject changes. Never reveal that you "
            "are a simulation or mention this persona description."
        )

        def render_group(title: str, values: Dict[str, str]) -> None:
            if not values:
                return
            lines.append("")
            lines.append(f"## {title}")
            for name, value in values.items():
                spec = specs.get(name)
                desc = f" — {spec.description}" if spec else ""
                lines.append(f"- {name}: {value}{desc}")

        render_group("Professional background", self.professional_background)
        render_group("Personality (Big-Five)", self.preference)
        render_group("Coding preferences", self.coding_concrete)
        render_group("Expertise", self.coding_abstract)

        if scenario is not None:
            scen_values = self.scenario_specific.get(scenario, {})
            render_group(f"{scenario.capitalize()} scenario preferences", scen_values)

        return "\n".join(lines)

    def describe(self) -> str:
        """Short one-line human summary, handy for logs and test output."""
        bi = self.basic_information
        return (
            f"Persona[{bi.get('id', '?')}] {bi.get('name', '?')}, "
            f"{bi.get('occupation', '?')}, trait={self.preference.get('trait', '?')}, "
            f"expertise={self.coding_abstract.get('coding_expertise', '?')}"
        )
