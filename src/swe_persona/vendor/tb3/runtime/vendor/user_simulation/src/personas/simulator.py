"""User simulator interface.

A :class:`UserSimulator` wraps a backbone LLM (called through ``litellm``) and a
:class:`Persona`. It loads the persona into the system prompt and exposes a
general, scenario-agnostic ``step(observation) -> action`` loop: the simulated
user *receives* information from the coding agent (or environment) and *acts* by
producing the next user message.

The interface is intentionally generic so the same simulator drives terminal
agents (Claude Code, Copilot CLI), VS Code inline completion, or any other
turn-based coding scenario. The only scenario-dependent piece is which
persona attributes are rendered into the system prompt, controlled by the
``scenario`` argument.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .persona import Persona
from .schema import DEFAULT_SCHEMA, PersonaSchema


# Type of a chat message: {"role": ..., "content": ...}
Message = Dict[str, str]
# A completion function with the litellm.completion signature; injectable for tests.
CompletionFn = Callable[..., Any]


@dataclass
class UserSimulator:
    """LLM-backed simulated user driven by a :class:`Persona`.

    Parameters
    ----------
    persona:
        The user persona to roleplay.
    model:
        Any model identifier understood by ``litellm`` (e.g. ``"gpt-4o"``,
        ``"anthropic/claude-opus-4-8"``).
    scenario:
        Optional scenario name (``"terminal"``/``"vscode"``) selecting which
        scenario-specific attributes are loaded into the system prompt. Ignored
        when ``persona_strategy="summary"``.
    persona_strategy:
        How the persona is loaded into the system prompt:
        ``"structured"`` (default) renders the structured attributes,
        ``"summary"`` uses the persona's pre-baked natural-language summary
        directly, and ``"auto"`` uses the summary when present else structured.
    task:
        Optional description of the coding task the user wants done; appended to
        the system prompt so the user has a concrete goal to pursue.
    temperature / max_tokens / extra_completion_kwargs:
        Forwarded to the backbone LLM call.
    completion_fn:
        Override the LLM call (defaults to ``litellm.completion``). Primarily for
        testing without network access.
    schema:
        Schema providing attribute descriptions for prompt rendering.
    """

    persona: Persona
    model: str = "gpt-4o"
    scenario: Optional[str] = None
    persona_strategy: str = "structured"
    task: Optional[str] = None
    temperature: float = 0.8
    max_tokens: Optional[int] = None
    extra_completion_kwargs: Dict[str, Any] = field(default_factory=dict)
    completion_fn: Optional[CompletionFn] = None
    schema: PersonaSchema = DEFAULT_SCHEMA

    # Conversation history from the *user simulator's* point of view. The agent's
    # messages are stored as role "user" (input to the LLM) and the simulated
    # user's own messages as role "assistant" (output of the LLM).
    _history: List[Message] = field(default_factory=list, init=False)

    # ----- prompt ----------------------------------------------------------
    @property
    def system_prompt(self) -> str:
        prompt = self.persona.to_system_prompt(
            scenario=self.scenario, schema=self.schema, strategy=self.persona_strategy
        )
        if self.task:
            prompt += (
                "\n\n## Your task\n"
                f"You want the agent to: {self.task}\n"
                "Pursue this goal across the conversation in a way consistent with the "
                "persona above. When the task is fully done to your satisfaction, say so "
                "clearly and end with the token [DONE]."
            )
        return prompt

    def messages(self) -> List[Message]:
        """The full message list (system prompt + history) sent to the LLM."""
        return [{"role": "system", "content": self.system_prompt}, *self._history]

    @property
    def history(self) -> List[Message]:
        return list(self._history)

    def reset(self) -> None:
        """Clear conversation history (persona and config are kept)."""
        self._history.clear()

    # ----- core loop -------------------------------------------------------
    def _complete(self, messages: List[Message]) -> str:
        fn = self.completion_fn
        if fn is None:  # Imported lazily so the package imports without litellm installed.
            import litellm

            fn = litellm.completion
        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            **self.extra_completion_kwargs,
        }
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        response = fn(**kwargs)
        return _extract_content(response)

    def step(self, observation: Optional[str] = None) -> str:
        """Advance one turn.

        ``observation`` is the latest information from the agent/environment
        (e.g. the agent's message, a diff, terminal output). Pass ``None`` for
        the very first turn to elicit the user's opening prompt. Returns the
        simulated user's next message (the action).
        """
        if observation is not None:
            self._history.append({"role": "user", "content": observation})
        reply = self._complete(self.messages())
        self._history.append({"role": "assistant", "content": reply})
        return reply

    # Convenience aliases matching common "receive information / do action" framing.
    def observe(self, information: str) -> None:
        """Record information from the agent without producing an action yet."""
        self._history.append({"role": "user", "content": information})

    def act(self) -> str:
        """Produce the next action given the current history."""
        return self.step(observation=None)

    @property
    def is_done(self) -> bool:
        """True if the simulated user signalled task completion via ``[DONE]``."""
        for msg in reversed(self._history):
            if msg["role"] == "assistant":
                return "[DONE]" in msg["content"]
        return False


def _extract_content(response: Any) -> str:
    """Pull message text out of a litellm/OpenAI-style response or a plain string."""
    if isinstance(response, str):
        return response
    try:
        return response.choices[0].message.content
    except AttributeError:
        # dict-shaped response
        return response["choices"][0]["message"]["content"]
