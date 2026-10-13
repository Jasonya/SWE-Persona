"""Public case measurements available at disclosure, separate from acceptance."""


def collect_explanation_evidence(controller, requirements, probes, snapshot,
                                 trusted, bases, cache, out, execute):
    if snapshot is None:
        return
    for key in sorted(controller.disclosed):
        req = requirements[key]
        if req.get('checker', {}).get('kind') != 'grounded_explanation':
            continue
        pid = req.get('public_probe') or req['checker'].get('parameters', {}).get('public_probe')
        if not pid:
            continue
        probe = probes[pid]
        if pid not in bases:
            bases[pid] = execute('', out / f'probe-base-{pid}', probe)
        lifecycle = getattr(controller, 'plan', {}).get('requirements', {}).get(key, {})
        if not controller.implementation_authorized or lifecycle.get('before_implementation'):
            continue
        cache_key = (pid, snapshot['patch_sha256'])
        if cache_key not in cache:
            cache[cache_key] = execute(snapshot['patch'], out / f'probe-candidate-{len(cache)+1:03d}', probe)
        trusted[pid] = cache[cache_key] | {'baseline_record': bases[pid]}
