"""Declared calibration-only supplements; never applied to scored candidates.

Pinned upstream reference bytes remain unchanged. Each supplement must reproduce
its public-contract defect before applying a minimal repair in an isolated box.
"""
import hashlib
import json

KIND = 'onedump-truncated-length-prefix-v1'
REFERENCE_SHA256 = '28da036349073dbf13453ee13b9fc66a655176d1c1fb47515bf09cb206195421'
OLD = '\t\tif err == io.EOF || err == io.ErrUnexpectedEOF {\n\t\t\tdr.done = true\n\t\t\treturn 0, io.EOF\n\t\t}'
NEW = '\t\tif err == io.EOF || err == io.ErrUnexpectedEOF {\n\t\t\treturn 0, io.ErrUnexpectedEOF\n\t\t}'
GO_TEST = '''package encryption_test

import (
 "bytes"
 "io"
 "testing"
 "github.com/liweiyi88/onedump/encryption"
)

func TestPersonaCalibrationTruncation(t *testing.T) {
 key := bytes.Repeat([]byte{7}, 32)
 for _, plain := range [][]byte{nil, bytes.Repeat([]byte("calibration-public-fixture"), 3)} {
  enc, err := encryption.NewEncryptor(key)
  if err != nil { t.Fatal(err) }
  var buf bytes.Buffer
  w := enc.EncryptWriter(&buf)
  if _, err := w.Write(plain); err != nil { t.Fatal(err) }
  if err := w.Close(); err != nil { t.Fatal(err) }
  wire := buf.Bytes()
  r, err := encryption.DecryptReader(bytes.NewReader(wire), key)
  if err != nil { t.Fatal(err) }
  got, err := io.ReadAll(r)
  if err != nil || !bytes.Equal(got, plain) { t.Fatalf("valid stream: %v", err) }
  for cut := 0; cut < len(wire); cut++ {
   r, err := encryption.DecryptReader(bytes.NewReader(wire[:cut]), key)
   if err == nil { _, err = io.ReadAll(r) }
   if err == nil { t.Fatalf("CALIBRATION_TRUNCATION_ACCEPTED at %d/%d", cut, len(wire)) }
  }
 }
}
'''


def validate(plan):
    repair = plan.get('calibration_fixture_repair')
    if repair is None:
        return
    if (plan['task_id'] != 'onedump-dump-encryption-pipeline'
            or not isinstance(repair, dict)
            or set(repair) != {'kind', 'reference_sha256', 'reason'}
            or repair['kind'] != KIND or repair['reference_sha256'] != REFERENCE_SHA256
            or not isinstance(repair['reason'], str) or not repair['reason'].strip()):
        raise ValueError('Unsupported calibration-only fixture repair')


def repaired_source(source):
    if source.count(OLD) != 1:
        raise ValueError('Calibration supplement source context changed')
    return source.replace(OLD, NEW, 1)


def apply(box, plan, reference, execute, out):
    """Returns provenance or None; requires real failing/passing executions."""
    if plan['task_id']=='yaegi-go-embed-directives':
        return apply_yaegi_supplement(box, reference, execute, out)
    if plan['task_id'] in SCOPING_SUPPLEMENTS:
        return apply_scoping_supplement(box, plan, reference, execute, out)
    if plan['task_id'] in ('etree-xml-diff-patch','dateutil-rfc5545-timezone-interop','fastapi-implicit-head-options'):
        return apply_public_supplement(box, plan, reference, execute, out)
    if plan['task_id'] == 'dasel-html-document-format':
        return apply_dasel(box, reference, execute, out)
    validate(plan)
    repair = plan.get('calibration_fixture_repair')
    if not repair:
        return None
    if hashlib.sha256(reference.encode()).hexdigest() != repair['reference_sha256']:
        raise ValueError('Pinned calibration reference identity changed')
    source_path = '/app/encryption/encryption.go'
    before = box.read(source_path)
    if before is None:
        raise ValueError('Missing calibration reference source')
    after = repaired_source(before)
    test_path = '/app/encryption/persona_calibration_truncation_test.go'
    command = ("go test ./encryption -run '^TestPersonaCalibrationTruncation$' -count=1; "
               "status=$?; rm -f encryption/persona_calibration_truncation_test.go; exit $status")
    box.put(test_path, GO_TEST)
    negative = execute(command)
    if negative['code'] == 0 or 'CALIBRATION_TRUNCATION_ACCEPTED' not in negative['output']:
        raise ValueError('Calibration reference defect was not independently reproduced')
    box.put(source_path, after)
    box.put(test_path, GO_TEST)
    positive = execute(command)
    if positive['code'] != 0:
        raise ValueError('Calibration-only truncation repair failed')
    receipt = {'kind': KIND, 'calibration_only': True,
               'upstream_reference_unchanged': True,
               'reference_sha256': repair['reference_sha256'],
               'source_before_sha256': hashlib.sha256(before.encode()).hexdigest(),
               'source_after_sha256': hashlib.sha256(after.encode()).hexdigest(),
               'negative': negative, 'positive': positive}
    (out/'calibration-repair.json').write_text(json.dumps(receipt, indent=2))
    return receipt


def apply_dasel(box, reference, execute, out):
    """Calibrate CLI evidence with a reference supplemented by format registration."""
    expected = 'd8344b7eea4b00bc021013adbda6626169750fde9d2b1a6385c0af37890acad2'
    if hashlib.sha256(reference.encode()).hexdigest() != expected:
        raise ValueError('Dasel calibration reference changed')
    command = "go build -mod=readonly -o /tmp/dasel-calibration ./cmd/dasel && printf '<p>Hello</p>' | /tmp/dasel-calibration -i html -o json body"
    before = execute(command)
    if before['code'] == 0 or 'unsupported reader file format: html' not in before['output']:
        raise ValueError('Dasel CLI registration defect was not reproduced')
    # The upstream patch supplies the parser, but its build tags exclude it from
    # the ordinary CLI and the CLI never imports its registration package.
    changes = []
    for name in ('html.go', 'parser.go', 'reader.go', 'writer.go'):
        path = '/app/parsing/html/' + name
        text = box.read(path)
        if not text.startswith('//go:build html\n'):
            raise ValueError('Dasel reference build constraint changed')
        new = text.replace('//go:build html\n', '', 1)
        box.put(path, new)
        changes.append({'path':path,'before':hashlib.sha256(text.encode()).hexdigest(),'after':hashlib.sha256(new.encode()).hexdigest()})
    path = '/app/cmd/dasel/main.go'; text = box.read(path)
    anchor = '_ "github.com/tomwright/dasel/v3/parsing/json"'
    if text.count(anchor) != 1: raise ValueError('Dasel registration context changed')
    new = text.replace(anchor, anchor + '\n\t_ "github.com/tomwright/dasel/v3/parsing/html"')
    box.put(path, new)
    changes.append({'path':path,'before':hashlib.sha256(text.encode()).hexdigest(),'after':hashlib.sha256(new.encode()).hexdigest()})
    after = execute(command)
    if after['code'] or json.loads(after['output']) != {'p':'Hello'}:
        raise ValueError('Dasel CLI calibration supplement failed')
    receipt = {'kind':'dasel-cli-registration-v1','calibration_only':True,'upstream_reference_unchanged':True,'reference_sha256':expected,'changes':changes,'negative':before,'positive':after}
    (out/'calibration-repair.json').write_text(json.dumps(receipt,indent=2))
    return receipt

PUBLIC_SUPPLEMENTS={'etree-xml-diff-patch': '010c309e24748c32335623f061e0c4571db428a77e7e427efbfa20254f94e8b1', 'dateutil-rfc5545-timezone-interop': 'dc6c13f7e3e211513af50613e61baf1d24a2115d91b932af89e0781335940a2b'}

PUBLIC_SUPPLEMENTS['fastapi-implicit-head-options']='900aa8a64c48adeec77fdb94bbe6f5621c6ed1848e2c13e819cc76206a67708a'

def apply_public_supplement(box, plan, reference, execute, out):
    from .task import BINDING_SPEC
    tid=plan['task_id']
    if hashlib.sha256(reference.encode()).hexdigest()!=PUBLIC_SUPPLEMENTS[tid]:
        raise ValueError('Calibration reference identity changed')
    probe=BINDING_SPEC.public_probes['case-v1']
    box.put('/tmp/'+probe['filename'],probe['script'])
    for item in probe.get('extra_files',[]):box.put('/tmp/'+item['filename'],item['script'])
    negative=execute(probe['command'])
    witness=('MISSING_REQUIRED_API' if tid.startswith('etree-') else 'HEAD emitted ASGI body bytes' if tid.startswith('fastapi-') else 'FEATURE_FAILURE')
    if negative['code']!=1 or witness not in negative['output']:
        raise ValueError('Calibration reference defect not reproduced')
    changes=[]
    paths=(['element_diff.go'] if tid.startswith('etree-') else ['fastapi/routing.py'] if tid.startswith('fastapi-') else ['src/dateutil/rrule.py'])
    for rel in paths:
        path='/app/'+rel;old=box.read(path)
        if tid.startswith('etree-'):
            anchor='//go:build diff\n// +build diff\n'
            if not old.startswith(anchor):raise ValueError('Etree build constraint changed')
            new=old.replace(anchor,'',1)
        elif tid.startswith('fastapi-'):
            anchor='        route.implicit_method = "HEAD"\n        return route'
            replacement='        route.implicit_method = "HEAD"\n        original_app = route.app\n\n        async def head_app(scope, receive, send):\n            async def send_without_body(message):\n                if message["type"] == "http.response.body":\n                    message = {**message, "body": b""}\n                await send(message)\n            await original_app(scope, receive, send_without_body)\n\n        route.app = head_app\n        return route'
            if old.count(anchor)!=1:raise ValueError('FastAPI implicit HEAD context changed')
            new=old.replace(anchor,replacement)
        else:
            anchor="re.findall('TZID=(?P<name>[^:]+):', s)"
            if old.count(anchor)!=1:raise ValueError('Dateutil parameter parser context changed')
            new=old.replace(anchor,"re.findall('TZID=(?P<name>[^:;]+)(?:;|:)', s)")
        box.put(path,new);changes.append({'path':rel,'before':hashlib.sha256(old.encode()).hexdigest(),'after':hashlib.sha256(new.encode()).hexdigest()})
    positive=execute(probe['command'])
    if positive['code']!=0:raise ValueError('Calibration public supplement failed')
    receipt={'kind':tid+'-public-supplement-v1','calibration_only':True,'upstream_reference_unchanged':True,'reference_sha256':PUBLIC_SUPPLEMENTS[tid],'negative':negative,'positive':positive,'changes':changes}
    (out/'calibration-repair.json').write_text(json.dumps(receipt,indent=2))
    return receipt


def scoped_reference(reference, task_id, out):
    """Remove one pinned upstream test-only hunk from a calibration fixture.

    The task forbids candidate test edits. The native P2P suite explicitly
    excludes this import-list test, so its reference-only update is unnecessary.
    Original solution bytes and scored-candidate integrity rules are unchanged.
    """
    if task_id != 'langchain-request-coalescing':
        return reference
    expected = '7ba4f9d876bd22c96a3588f6beca6acbbbc97022267d93c7a47a9eba7320ecaa'
    if hashlib.sha256(reference.encode()).hexdigest() != expected:
        raise ValueError('LangChain calibration reference identity changed')
    marker = 'diff --git a/libs/core/tests/unit_tests/runnables/test_imports.py b/libs/core/tests/unit_tests/runnables/test_imports.py\n'
    if reference.count(marker) != 1:
        raise ValueError('LangChain calibration test-only hunk changed')
    scoped, removed = reference.split(marker)
    if 'diff --git ' in removed:
        raise ValueError('Unexpected additional LangChain reference hunk')
    provenance = {'kind': 'langchain-source-only-reference-v1', 'calibration_only': True,
        'original_reference_sha256': expected,
        'scoped_reference_sha256': hashlib.sha256(scoped.encode()).hexdigest(),
        'removed_path': 'libs/core/tests/unit_tests/runnables/test_imports.py',
        'reason': 'Remove an out-of-scope test update; require unchanged native evaluator and integrity checks to pass again.'}
    (out / 'reference-scope-repair.json').write_text(json.dumps(provenance, indent=2) + '\n')
    return scoped

SCOPING_SUPPLEMENTS={'python-statemachine-state-data-scoping': '4197380535a2e67258afbdac3a7cb91d6a2c9ab1cd70c4cc09e973b739e4f81d', 'oxvg-structural-selector-preservation': '90cb591ba627b357411b384fad244d9994994ade202f5ca8dd0af09f89bbde47'}

def apply_scoping_supplement(box, plan, reference, execute, out):
    from .task import BINDING_SPEC
    tid=plan['task_id'];expected=SCOPING_SUPPLEMENTS[tid]
    if hashlib.sha256(reference.encode()).hexdigest()!=expected:
        raise ValueError('Calibration reference identity changed')
    probe=BINDING_SPEC.public_probes['case-v1']
    box.put('/tmp/'+probe['filename'],probe['script'])
    for item in probe.get('extra_files',[]):box.put('/tmp/'+item['filename'],item['script'])
    negative=execute(probe['command'])
    witness='Lookup must return active data' if tid.startswith('python-') else 'FEATURE_MISMATCH: core'
    if negative['code']!=1 or witness not in negative['output']:
        raise ValueError('Calibration reference scoping defect not reproduced')
    if tid.startswith('python-'):
        rel='statemachine/state_data.py';old=box.read('/app/'+rel)
        anchor='        raw = self._data.get(state_id)\n        if raw is None:\n            return None\n        return dict(raw)'
        replacement='        raw = self._data.get(state_id)\n        if raw is None:\n            return None\n        return raw'
    else:
        rel='crates/oxvg_ast/src/style.rs';old=box.read('/app/'+rel)
        anchor='    selector_compounds(selector)\n        .into_iter()\n        .any(|compound| compound_matches_element(element, compound))'
        replacement="""    // A binary child relationship implicates only an actual parent/child pair.
    // Matching one compound elsewhere in the document is insufficient.
    let parts = split_top_level(selector, '>');
    if parts.len() == 2
        && selector_compounds(parts[0]).len() == 1
        && selector_compounds(parts[1]).len() == 1
    {
        let left = parts[0].trim();
        let right = parts[1].trim();
        return (compound_matches_element(element, left)
            && element.children_iter().any(|child| compound_matches_element(&child, right)))
            || (compound_matches_element(element, right)
                && element.parent_element().is_some_and(|parent| compound_matches_element(&parent, left)));
    }
    selector_compounds(selector)
        .into_iter()
        .any(|compound| compound_matches_element(element, compound))"""
    if old.count(anchor)!=1:raise ValueError('Calibration scoping supplement context changed')
    new=old.replace(anchor,replacement,1);box.put('/app/'+rel,new)
    positive=execute(probe['command'])
    if positive['code']!=0:raise ValueError('Calibration scoping supplement public probe failed: '+positive['output'][-3000:])
    receipt={'kind':tid+'-scoping-supplement-v1','calibration_only':True,'upstream_reference_unchanged':True,'reference_sha256':expected,'negative':negative,'positive':positive,'changes':[{'path':rel,'before':hashlib.sha256(old.encode()).hexdigest(),'after':hashlib.sha256(new.encode()).hexdigest()}]}
    (out/'calibration-repair.json').write_text(json.dumps(receipt,indent=2))
    return receipt

YAEGI_REFERENCE_SHA256='cda60587b551a81b8a09d713a047e7b65685ecda3473dacd5e52004f591148e2'

def apply_yaegi_supplement(box, reference, execute, out):
    from .task import BINDING_SPEC
    if hashlib.sha256(reference.encode()).hexdigest()!=YAEGI_REFERENCE_SHA256:
        raise ValueError('Yaegi calibration reference identity changed')
    probe=BINDING_SPEC.public_probes['case-v1']
    box.put('/tmp/'+probe['filename'],probe['script'])
    for item in probe.get('extra_files',[]):box.put('/tmp/'+item['filename'],item['script'])
    negative=execute(probe['command'])
    if negative['code']!=1 or 'FEATURE_IMPORT_UNAVAILABLE_OR_BROKEN' not in negative['output']:
        raise ValueError('Yaegi missing embed package registration not reproduced')
    rel='interp/program.go';old=box.read('/app/'+rel)
    anchor='\tdirectives := extractEmbedDirectives(src)\n\tif len(directives) > 0 {\n\t\tinterp.registerEmbedPackage()'
    replacement='\tdirectives := extractEmbedDirectives(src)\n\tinterp.registerEmbedPackage()\n\tif len(directives) > 0 {'
    if old.count(anchor)!=1:raise ValueError('Yaegi embed registration context changed')
    new=old.replace(anchor,replacement,1);box.put('/app/'+rel,new);positive=execute(probe['command'])
    if positive['code']!=0:raise ValueError('Yaegi public supplement failed: '+positive['output'][-3000:])
    receipt={'kind':'yaegi-embed-import-registration-v1','calibration_only':True,'upstream_reference_unchanged':True,'reference_sha256':YAEGI_REFERENCE_SHA256,'negative':negative,'positive':positive,'changes':[{'path':rel,'before':hashlib.sha256(old.encode()).hexdigest(),'after':hashlib.sha256(new.encode()).hexdigest()}]}
    (out/'calibration-repair.json').write_text(json.dumps(receipt,indent=2))
    return receipt
