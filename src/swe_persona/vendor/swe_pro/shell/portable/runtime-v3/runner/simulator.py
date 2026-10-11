"""Reuse the five-layer persona user simulator and strict decision schema."""

import sys
import copy
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tb2_persona_pilot.simulator import *  # noqa: F401,F403

import tb2_persona_pilot.simulator as _base

SIMULATOR_PROTOCOL_VERSION = "observation-constrained-decision-v6"

_native_policy = """
- `native_requirements_due_this_turn` is independent of persona evolution. When true,
  include every item in `native_requirements_to_disclose` in `user_message` using its
  exact validated wording. They may be one original-contract clarification bundle.
- Never claim that a native requirement was disclosed unless its wording is present in
  the agent-visible `user_message`; controller bookkeeping is not disclosure.
- When `original_contract_due_this_turn` is present, include its problem_statement
  verbatim as well. This mandatory public-contract bundle is exempt
  from the ordinary message-length cap; ordinary replies retain the persona style.
- Native requirement IDs never belong in ledger_updates or verification_requests.
  Those fields can reference only active evolving requirements in the current schema.
- Checker IDs and versions are separate fields. Never append @version to checker_id.
- Verification requests must use the checker bound to that active requirement.
  Send parameters as "{}"; the controller supplies the frozen checker parameters.
- Keep controller internals out of `user_message`. Never mention checker IDs, `chk-*`,
  evidence refs, ledgers, rubric verdicts, artifact eligibility, prompt truncation, or
  recording visibility. Put machine references only in the structured `evidence_refs` field.
- Speak as a real repository user. Ask for behavior, code reasoning, or a focused check,
  not work whose purpose is satisfying an evaluator. Do not ask the agent to create or
  modify review-only files (for example PR notes, evidence tables, or handoff appendices);
  keep explanations and handoffs in the conversation unless the public task requires a file.
- Do not repeat a verification request merely to make its command or assertions more visible
  to the evaluator. If available evidence is incomplete, state the limitation naturally and
  either request one genuinely useful focused check or leave the result unknown.
- Respect constraints already agreed in the recent dialogue. In particular, after accepting
  that an action is unavailable or asking for no further repository actions, do not demand
  that same action again. Close with the supported conclusion or leave it unknown.
"""
POLICY = _base.POLICY + _native_policy


def render_system_prompt(*args, **kwargs):
    # Do not mutate the shared TB2 module's policy for other experiments.
    return _base.render_system_prompt(*args, **kwargs) + "\n" + _native_policy


def decision_schema(observation):
    """Narrow the existing decision format to controller-authorized values."""
    schema = copy.deepcopy(_base.DECISION_SCHEMA)
    p = schema["properties"]
    p["turn_id"]["enum"] = [observation["turn"]]
    p["observation_id"]["enum"] = [observation["observation_id"]]
    p["action"]["enum"] = list(observation["allowed_actions"])
    if observation.get("native_requirements_due_this_turn"):
        p["action"]["enum"] = [action for action in p["action"]["enum"] if action != "end"]
    if not observation.get("end_allowed") or observation.get("native_requirements_due_this_turn"):
        p["end_state"] = {"type": "null"}
    due = observation.get("requirement_to_disclose")
    if due:
        p["disclosures"].update(minItems=1, maxItems=1)
        fields = p["disclosures"]["items"]["properties"]
        fields["requirement_ref"]["enum"] = [due["requirement_ref"]]
        fields["operation"]["enum"] = [due["operation"]]
        replaces = list(due.get("replaces") or [])
        fields["replaces"].update(minItems=len(replaces), maxItems=len(replaces))
        if replaces:
            fields["replaces"]["items"]["enum"] = replaces
    else:
        p["disclosures"]["maxItems"] = 0

    active = [entry["ref"] for entry in observation.get("ledger", [])
              if entry.get("activation_status") == "active"]
    if active:
        p["ledger_updates"]["items"]["properties"]["requirement_ref"]["enum"] = active
        p["verification_requests"]["items"]["properties"]["requirement_ref"]["enum"] = active
    else:
        p["ledger_updates"]["maxItems"] = 0
        p["verification_requests"]["maxItems"] = 0

    variants = []
    for entry in observation.get("ledger", []):
        bound = entry.get("checker") or {}
        if entry.get("activation_status") != "active" or not bound:
            continue
        checker_id, version = bound.get("id"), bound.get("version")
        if f"{checker_id}@{version}" not in observation.get("registered_checkers", []):
            continue
        variant = copy.deepcopy(p["verification_requests"]["items"])
        variant["properties"]["requirement_ref"]["enum"] = [entry["ref"]]
        variant["properties"]["checker_id"]["enum"] = [checker_id]
        variant["properties"]["version"]["enum"] = [version]
        variant["properties"]["parameters"]["enum"] = ["{}"]
        variants.append(variant)
    if variants:
        p["verification_requests"]["items"] = {"anyOf": variants}
    else:
        p["verification_requests"]["maxItems"] = 0

    refs = sorted({str(item["evidence_ref"])
                   for item in observation.get("probe_results", []) + observation.get("verification_results", [])
                   if item.get("evidence_ref")} |
                  {str(item["id"]) for item in observation.get("typed_events", []) if item.get("id")})
    if refs:
        p["evidence_refs"]["items"]["enum"] = refs
    else:
        p["evidence_refs"]["maxItems"] = 0
    return schema


class UserSimulator(_base.UserSimulator):
    def decide(self, observation, rejections=None):
        prompt = "## Observation\n" + json.dumps(observation, ensure_ascii=False, indent=2)
        if rejections:
            prompt += "\n\n## Correct every rejected condition\n- " + "\n- ".join(rejections)
        return self.llm.json_obj(
            [{"role": "system", "content": self.system_prompt},
             {"role": "user", "content": prompt}],
            decision_schema(observation), schema_name="user_decision",
            role=f"simulator.turn{observation['turn']}", max_output_tokens=3000,
        )
