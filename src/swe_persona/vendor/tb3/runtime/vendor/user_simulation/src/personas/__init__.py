"""Personas layer: User Simulation + profile configs.

The User Simulation library. Public API:
    - schema:    persona attribute space (``PersonaSchema``, ``DEFAULT_SCHEMA``)
    - Persona:   a single instantiated simulated user
    - PersonaGenerator / Distribution: sample persona databases
    - UserSimulator: litellm-backed user simulator

Also holds the lightweight profile JSONs under ``profiles/`` used by the
task-generator's ``inject_persona.py``.
"""

from .conflicts import ConflictMatch, ConflictRuleSet
from .persona import Persona
from .schema import DEFAULT_SCHEMA, SCHEMA_VERSION, AttributeSpec, PersonaSchema
from .salience import (
    activate_background_attributes,
    extract_background_calibration,
    format_active_background,
    format_background_calibration,
    activate_persona_preferences,
    compile_interaction_contract,
    extract_persona_salience,
    extract_interaction_policy,
    format_active_preferences,
    format_interaction_contract,
    format_opening_persona_guidance,
    format_salience_for_prompt,
    opening_persona_guidance,
    salience_rubric_items,
)
from .simulator import UserSimulator

__all__ = [
    "AttributeSpec",
    "PersonaSchema",
    "DEFAULT_SCHEMA",
    "SCHEMA_VERSION",
    "Persona",
    "ConflictMatch",
    "ConflictRuleSet",
    "UserSimulator",
    "activate_background_attributes",
    "extract_background_calibration",
    "format_active_background",
    "format_background_calibration",
    "activate_persona_preferences",
    "compile_interaction_contract",
    "extract_persona_salience",
    "extract_interaction_policy",
    "format_active_preferences",
    "format_interaction_contract",
    "opening_persona_guidance",
    "format_opening_persona_guidance",
    "format_salience_for_prompt",
    "salience_rubric_items",
]
