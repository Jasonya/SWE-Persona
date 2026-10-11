"""Select a replaceable agent adapter without importing mini-swe-agent eagerly."""
import importlib
import os
HARNESS_PROTOCOL_VERSION = 'pcb-replaceable-agent-adapter-v1'


def resolve_factory(adapter):
    if adapter == 'mini':
        from agent_mini import TerminalAgent as factory
        return factory
    module, separator, symbol = adapter.partition(':')
    if not separator or not module or not symbol:
        raise ValueError('PCB_AGENT_ADAPTER must be mini or module:factory')
    factory = getattr(importlib.import_module(module), symbol)
    if not callable(factory):
        raise TypeError('Agent factory must be callable')
    return factory


def TerminalAgent(llm, container, environment_card, **kwargs):
    factory = resolve_factory(os.environ.get('PCB_AGENT_ADAPTER', 'portable_shell:ShellAgent'))
    agent = factory(llm, container, environment_card, **kwargs)
    if not callable(getattr(agent, 'run_turn', None)):
        raise TypeError('An agent adapter must implement run_turn(message, turn)')
    if not hasattr(agent, 'delivery_receipts'):
        agent.delivery_receipts = []
    return agent
