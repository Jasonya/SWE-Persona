"""Container-side continuation hooks used by Harbor's official mini CLI.

v6 addition: an idle guard.  With nothing left to do, the agent sometimes keeps issuing
placeholder commands (`cd /app && true`, or the same check over and over) instead of
submitting its reply, and the turn runs until the whole budget is gone.  The first idle
command gets an explicit reminder of how to reply; after IDLE_LIMIT consecutive idle
commands the turn ends as an `idle` yield with an empty reply.  Nothing is written on the
agent's behalf.
"""
import json
import os
import re
import shlex
import time
from pathlib import Path
from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError, InterruptAgentFlow
from mini_sidecar import save_trajectory, turn_reply

REQUEST = Path('/logs/agent/request.json')
IDLE_LIMIT = 5
NOOP = re.compile(r'^\s*(?:cd\s+\S+\s*(?:&&|;)\s*)?(?:true|:|sleep\s+\d+(?:\.\d+)?)\s*;?\s*$')
IDLE_REMINDER = ('\nThis command did nothing new; placeholder commands are never delivered to the user. '
                 'If you have finished what the latest user message asks (even if nothing needed changing), '
                 'reply now in one separate action: printf \'%s\\n\' COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT '
                 '\'<your complete message to the user>\'. If you need something from the user, use '
                 'ask_user \'<question>\'.\n')


class PersonaEnvironment(LocalEnvironment):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.request = json.loads(REQUEST.read_text())
        self.deadline = time.monotonic() + self.request['timeout']
        self.executions = []
        self.recent = []
        self.idle = 0

    def _idle_check(self, command, result):
        """Count placeholder commands and exact repeats of a recent command and output."""
        key = (command.strip(), result.get('output', ''))
        idle = bool(NOOP.match(command)) or key in self.recent
        self.recent = (self.recent + [key])[-4:]
        self.idle = self.idle + 1 if idle else 0
        if self.idle >= IDLE_LIMIT:
            raise InterruptAgentFlow({'role': 'exit', 'content': '',
                                      'extra': {'exit_status': 'IdleYield', 'submission': ''}})
        if idle:
            result['output'] = result.get('output', '') + IDLE_REMINDER
        return result

    def execute(self, action, cwd='', *, timeout=None):
        command = action.get('command', '')
        if command.strip().startswith('ask_user '):
            try:
                parts = shlex.split(command)
            except ValueError:  # unbalanced quotes
                parts = []
            if len(parts) != 2:
                # mini's observation template reads exception_info with StrictUndefined.
                return {'output': 'Use one quoted argument.', 'returncode': 2, 'exception_info': ''}
            raise InterruptAgentFlow({'role': 'exit', 'content': parts[1],
                'extra': {'exit_status': 'AskUser', 'submission': parts[1]}})
        remaining = max(1, min(600, int(self.deadline-time.monotonic())))
        pid = shlex.quote(self.request['pid_file'])
        # The command's output goes to a file that is printed when it ends.  A background process it
        # starts (e.g. Xvfb from a test runner) then holds the file, not mini's output pipe; through
        # the pipe mini kept reading after the command ended and never returned (2026-10-04).
        log_file = '/tmp/pcb-cmd-$$.log'
        script = ('echo $$ > %s; trap "rm -f %s %s" EXIT; timeout --kill-after=2s %ss bash -c %s > %s 2>&1 < /dev/null; '
                  'rc=$?; cat %s; exit $rc') % (pid, pid, log_file, remaining, shlex.quote(command), log_file, log_file)
        try:
            result = super().execute(dict(action, command='setsid bash -c '+shlex.quote(script)), cwd, timeout=remaining+3)
        except InterruptAgentFlow:
            self.executions.append({'command': command, 'returncode': 0, 'output': 'Submitted'})
            raise
        if 'COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT' in result.get('output',''):
            result['output'] += '\nSubmission NOT accepted. Issue the submission marker as a separate action with no preceding output, followed by the complete handoff. Do not poll or use a no-op.\n'
        self.executions.append({'command': command, **result})
        return self._idle_check(command, result)

class PersonaAgent(DefaultAgent):
    def __init__(self, model, env, **kwargs):
        kwargs.pop('mode', None)
        kwargs.pop('confirm_exit', None)
        self.trajectory = Path(kwargs.pop('output_path'))
        super().__init__(model, env, output_path=None, **kwargs)

    def run(self, task='', **kwargs):
        request = self.env.request
        if self.trajectory.exists():
            saved = json.loads(self.trajectory.read_text())
            self.messages = [m for m in saved['messages'] if m.get('role') != 'exit']
            self.n_calls = saved['info']['model_stats']['api_calls']
            self.cost = saved['info']['model_stats']['instance_cost']
            self.add_messages(self.model.format_message(role='user', content=task))
        else:
            self.extra_template_vars = {'task': task}
            self.add_messages(self.model.format_message(role='system', content=self._render_template(self.config.system_template)),
                self.model.format_message(role='user', content=self._render_template(self.config.instance_template)))
        while True:
            try:
                self.step()
                self.n_consecutive_format_errors = 0
            except FormatError as exc:
                self.add_messages(*exc.messages)
                self.n_consecutive_format_errors += 1
                if self.n_consecutive_format_errors >= 3:
                    raise RuntimeError('Repeated mini format errors')
            except InterruptAgentFlow as exc:
                self.add_messages(*exc.messages)
            finally:
                save_trajectory(self, self.trajectory)
            if self.messages[-1].get('role') != 'exit':
                continue
            end = self.messages[-1]
            exit_status = end.get('extra', {}).get('exit_status')
            if exit_status == 'IdleYield':
                payload = {'reply': '', 'status': 'idle', 'executions': self.env.executions,
                           'mini_version': '2.4.6', 'idle_limit': 5}
                Path('/logs/agent/turn-%s.json' % request['turn']).write_text(json.dumps(payload))
                return end.get('extra', {})
            try:
                reply = turn_reply(end, request['interactive'])
            except ValueError:
                self.messages.pop()
                self.add_messages(self.model.format_message(role='user', content='Include the complete user-facing handoff after the submission marker.'))
                save_trajectory(self, self.trajectory)
                continue
            payload = {'reply': reply, 'status': 'working' if end.get('extra', {}).get('exit_status') == 'AskUser' else 'done',
                       'executions': self.env.executions, 'mini_version': '2.4.6'}
            Path('/logs/agent/turn-%s.json' % request['turn']).write_text(json.dumps(payload))
            return end.get('extra', {})
