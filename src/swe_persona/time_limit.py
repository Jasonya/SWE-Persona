"""Separate preparation, agent/user interaction, and verification watchdogs."""
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import time

POLICY = 'agent-user-wall-clock-v2'
SCOPE = 'agent/user interaction, online judges, tools and interaction snapshots; excludes calibration, input materialization, image/environment preparation, native preflight and final verification'
PHASE_ENV = 'SWE_PERSONA_CLOCK_FILE'
_alarm_state = None


class InteractionExpired(BaseException):
    """A controller cutoff, deliberately outside provider Exception retries."""


def arm_interaction(deadline):
    global _alarm_state
    if not managed_clock():return
    _alarm_state=(signal.getsignal(signal.SIGALRM),signal.getitimer(signal.ITIMER_REAL),time.monotonic())
    def expire(signum, frame):raise InteractionExpired('Agent/user interaction budget exhausted')
    signal.signal(signal.SIGALRM,expire)
    signal.setitimer(signal.ITIMER_REAL,max(.001,deadline-time.monotonic()))


def disarm_interaction():
    global _alarm_state
    if _alarm_state is None:return
    handler,timer,started=_alarm_state;_alarm_state=None
    signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,handler)
    if timer[0]>0:
        signal.setitimer(signal.ITIMER_REAL,max(.001,timer[0]-(time.monotonic()-started)),timer[1])


def managed_clock():
    return bool(os.environ.get(PHASE_ENV))


def _transition(phase, seconds=None):
    """Host controllers publish transitions; the agent never receives this file."""
    if not managed_clock():
        return time.monotonic()+seconds if seconds is not None else None
    path=Path(os.environ[PHASE_ENV]); data=json.loads(path.read_text())
    events=data['events']
    existing=next((e for e in events if e['phase']==phase),None)
    if existing:return existing['deadline_monotonic']
    if phase=='verification' and not events:return None
    if phase=='interaction' and events:raise RuntimeError('Interaction clock cannot restart')
    limit=data['interaction_sec'] if phase=='interaction' else data['verification_sec']
    if seconds is not None:limit=min(limit,positive_seconds(seconds))
    started=time.monotonic()
    events.append({'phase':phase,'started_monotonic':started,
                   'deadline_monotonic':started+limit,'limit_sec':limit})
    temp=path.with_suffix('.tmp');temp.write_text(json.dumps(data));temp.replace(path)
    return started+limit


def begin_interaction(seconds):
    return _transition('interaction',positive_seconds(seconds))


def begin_verification():
    disarm_interaction()
    return _transition('verification')


def positive_seconds(value):
    value=float(value)
    if not math.isfinite(value) or value<=0:
        raise ValueError('Time limit must be finite and greater than zero')
    return value


def resolve_limit(row, override=None):
    default=positive_seconds(row['budget']['episode_timeout_sec'])
    return {'policy':POLICY, 'scope':SCOPE, 'default_sec':default,
            'effective_sec':default if override is None else positive_seconds(override),
            'overridden':override is not None, 'cleanup_grace_sec':30,
            'preparation_sec':positive_seconds(os.environ.get('SWE_PERSONA_PREPARATION_TIMEOUT_SEC',7200)),
            'verification_sec':positive_seconds(os.environ.get('SWE_PERSONA_VERIFICATION_TIMEOUT_SEC',
                max(900,positive_seconds(row['budget']['verifier_timeout_sec'])+600))),
            'internal_budget':dict(row['budget']),
            'internal_limits_preserved':True}


def apply_timeout_result(result, execution):
    """Interaction exhaustion is a model failure; setup/grading are infrastructure."""
    if not execution['timed_out']:return result
    phase=execution.get('timeout_phase','interaction')
    if phase=='interaction':
        result.update(status='fail',joint_success=False,outcome_reason='total_timeout',
            verification_complete=isinstance(result.get('native_pass'),bool) and isinstance(result.get('persona_pass'),bool),
            score=0.0,note='Agent/user interaction budget exhausted. Preparation and final grading are timed separately; missing grades are not synthesized.')
    else:
        result.update(status='infrastructure_error',joint_success=False,outcome_reason=phase+'_timeout',
            verification_complete=False,score=None,
            note=phase.title()+' watchdog exhausted outside the agent/user interaction budget.')
    return result


def run_bounded(command, *, env, log, seconds, cleanup_grace_sec=30,
                phase_file=None, preparation_sec=7200, verification_sec=1800):
    """Interrupt the complete worker process group; never interrupt sibling runs."""
    seconds=positive_seconds(seconds)
    started=time.monotonic(); signals=[];timed_out=False;timeout_phase=None;events=[]
    env=dict(env)
    if phase_file is not None:
        phase_file=Path(phase_file).resolve()
        phase_file.write_text(json.dumps({'policy':POLICY,'interaction_sec':seconds,
            'verification_sec':positive_seconds(verification_sec),'events':[]}))
        env[PHASE_ENV]=str(phase_file)
        env['SWE_PERSONA_INTERACTION_SECONDS']=str(seconds)
    process=subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT,
                             start_new_session=True)
    cleanup_started=None
    def send(sig):
        try:os.killpg(process.pid,sig);signals.append(sig.name)
        except ProcessLookupError:pass
    try:
        if phase_file is None:
            process.wait(timeout=seconds)
        else:
            while True:
                events=json.loads(phase_file.read_text())['events']
                now=time.monotonic()
                phase=events[-1]['phase'] if events else 'preparation'
                deadline=events[-1]['deadline_monotonic'] if events else started+positive_seconds(preparation_sec)
                interaction=next((e for e in events if e['phase']=='interaction'),None)
                verification=next((e for e in events if e['phase']=='verification'),None)
                if interaction and ((verification['started_monotonic'] if verification else now)
                                    > interaction['deadline_monotonic']):
                    timed_out=True;timeout_phase='interaction'
                # Give the controller bounded time to freeze and enter grading.
                # This grace never permits task credit after the interaction cap.
                guard=deadline+(cleanup_grace_sec if phase=='interaction' else 0)
                if now>=guard:
                    timed_out=True;timeout_phase=phase
                    if process.poll() is not None:break
                    raise subprocess.TimeoutExpired(command,seconds)
                if process.poll() is not None:break
                try:process.wait(timeout=min(.1,max(.001,guard-now)))
                except subprocess.TimeoutExpired:pass
    except subprocess.TimeoutExpired:
        timed_out=True;timeout_phase=timeout_phase or 'interaction';send(signal.SIGINT)
        cleanup_started=time.monotonic()
        try:process.wait(timeout=cleanup_grace_sec)
        except subprocess.TimeoutExpired:
            send(signal.SIGTERM)
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                send(signal.SIGKILL);process.wait(timeout=5)
        # A subprocess may outlive a worker that has already exited. Stop only
        # members of this run's dedicated group, never other benchmark runs.
        send(signal.SIGTERM)
    except BaseException:
        send(signal.SIGINT)
        try:process.wait(timeout=cleanup_grace_sec)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL);process.wait(timeout=5)
        raise
    elapsed=time.monotonic()-started
    if phase_file is not None:events=json.loads(phase_file.read_text())['events']
    durations={}
    for i,event in enumerate(events):
        end=events[i+1]['started_monotonic'] if i+1<len(events) else started+elapsed
        durations[event['phase']]=max(0,end-event['started_monotonic'])
    durations['preparation']=max(0,(events[0]['started_monotonic'] if events else started+elapsed)-started)
    return {'policy':POLICY,'scope':SCOPE,'timeout_sec':seconds,'timed_out':timed_out,
            'timeout_phase':timeout_phase,'interaction_started':bool(events) if phase_file else True,
            'phase_events':events,'phase_elapsed_sec':durations,
            'elapsed_sec':elapsed,'cleanup_elapsed_sec':max(0,time.monotonic()-cleanup_started) if cleanup_started else 0,
            'cleanup_grace_sec':cleanup_grace_sec,'signals':signals,
            'process_exit_code':process.returncode,
            'note':'Cleanup grace is outside the scoring budget. Unfinished verification is not synthesized.'}
