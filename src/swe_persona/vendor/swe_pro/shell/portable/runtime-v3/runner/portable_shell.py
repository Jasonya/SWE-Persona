"""Bounded shell/reply adapter with complete host-side command receipts."""
from tb2_persona_pilot.agent import AgentTurn, TerminalAgent as ShellReplyAgent


class TurnBudgetExceeded(Exception):
    pass


class LimitedClient:
    def __init__(self, client):
        self.client = client
        self.calls = 0
        self.limit = 0

    def text(self, *args, **kwargs):
        if self.calls >= self.limit:
            raise TurnBudgetExceeded()
        self.calls += 1
        return self.client.text(*args, **kwargs)


class RecordingContainer:
    def __init__(self, container):
        self.container = container
        self.commands = []

    def sh(self, script, **kwargs):
        code, stdout, stderr = self.container.sh(script, **kwargs)
        combined = (stdout or '') + ('\n[stderr]\n' + stderr if stderr else '')
        self.commands.append((script, code, combined))
        return code, stdout, stderr


class ShellAgent(ShellReplyAgent):
    def __init__(self, llm, container, environment_card, **kwargs):
        self.client = LimitedClient(llm)
        self.broker = RecordingContainer(container)
        super().__init__(self.client, self.broker, environment_card, **kwargs)
        self.delivery_receipts = []

    def run_turn(self, user_message, turn, **kwargs):
        self.client.calls = 0
        self.client.limit = int(kwargs.get('step_budget') or self.max_steps)
        start = len(self.broker.commands)
        try:
            result = super().run_turn(user_message, turn, **kwargs)
        except TurnBudgetExceeded:
            from tb2_persona_pilot.agent import Step
            commands = self.broker.commands[start:]
            result = AgentTurn(turn=turn, message_id=f'a-{turn}',
                               reply='TurnStepLimitExceeded', status='budget_exhausted',
                               steps=[Step(i+1,c,rc,out) for i,(c,rc,out) in enumerate(commands)])
        for step, (command, code, full_output) in zip(result.steps, self.broker.commands[start:]):
            step.stdout = full_output
        result.turn_end_reason = {
            'working': 'assistant_reply', 'done': 'submitted',
            'budget_exhausted': 'step_budget_exhausted',
            'format_error': 'format_error',
        }.get(result.status, 'adapter_error')
        return result
