"""Execute the analyst's query against isolated, read-only DuckDB fixtures.

The engine is the pinned image's existing client, identified by binary hash.
This checks the delivered user query; original engine changes are checked by
the separate official SWE verifier, not by this fixture.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path('/testbed')
QUERY_FILE = 'persona-query.sql'
DEFAULT_QUERY = "SELECT max(i::BIGINT) AS maximum FROM read_parquet('__PERSONA_INPUT__')"
CASES = [
    ('public_range', 'SELECT i::INTEGER AS i FROM range(8192) t(i)', 8191),
    ('nulls_duplicates', 'SELECT * FROM (VALUES (NULL::INTEGER), (7), (7), (-3)) t(i)', 7),
    ('empty', 'SELECT 0::INTEGER AS i WHERE false', None),
    ('all_null', 'SELECT NULL::INTEGER AS i FROM range(3)', None),
    ('integer_bounds', 'SELECT * FROM (VALUES ((-2147483648)::INTEGER), (2147483647::INTEGER), (NULL)) t(i)', 2147483647),
]


def call(binary, db, sql, readonly=True):
    args = [str(binary), '-batch', '-json']
    if readonly:
        args += ['-readonly']
    args += [str(db), '-c', sql]
    result = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return {'exit_code': result.returncode, 'stdout': result.stdout, 'stderr': result.stderr}


def verdict(record):
    if not record.get('permissions_verified'):
        return 'unknown', 'permission_fixture_incomplete'
    if record.get('missing_query'):
        return 'fail', 'query_deliverable_missing'
    if record.get('invalid_interface'):
        return 'fail', 'query_interface_changed'
    if len(record.get('cases', [])) != len(CASES):
        return 'unknown', 'semantic_cases_incomplete'
    if not record.get('inputs_unchanged'):
        return 'fail', 'input_changed'
    if not all(c['passed'] for c in record['cases']):
        return 'fail', 'query_semantics_changed'
    return 'pass', 'query_semantics_preserved'


def execute(query, folder, binary, db, permissions):
    record = {'query': query, 'permissions_verified': permissions, 'cases': []}
    # This declared interface is one composable SELECT with one input placeholder.
    # It is executed inside a SELECT wrapper, so CLI directives and extra SQL
    # statements cannot escape into administrative operations.
    query = query.strip().removesuffix(';').strip()
    if query.count('__PERSONA_INPUT__') != 1 or ';' in query or not query.lower().startswith(('select ', 'with ')):
        return record | {'invalid_interface': True}
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.parquet')}
    for name, _, expected in CASES:
        bound = query.replace('__PERSONA_INPUT__', str(folder / (name + '.parquet')))
        result = call(binary, db, 'SELECT * FROM (' + bound + ') AS candidate')
        description = call(binary, db, 'DESCRIBE SELECT * FROM (' + bound + ') AS candidate')
        try:
            actual = json.loads(result['stdout'])
            schema = json.loads(description['stdout'])
            passed = (result['exit_code'] == description['exit_code'] == 0
                      and actual == [{'maximum': expected}]
                      and len(schema) == 1 and schema[0]['column_name'] == 'maximum'
                      and schema[0]['column_type'] == 'BIGINT')
        except (ValueError, KeyError, TypeError):
            passed = False
        record['cases'].append({'name': name, 'expected': [{'maximum': expected}], 'expected_type': 'BIGINT',
                                'actual': result, 'description': description, 'passed': passed})
    record['inputs_unchanged'] = before == {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.glob('*.parquet')}
    record['input_hashes'] = before
    return record


def main():
    binary = ROOT / 'build/duckdb'
    base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix='persona-sql-') as temporary:
        folder = Path(temporary)
        db = folder / 'analyst.duckdb'
        setup = call(binary, db, 'CREATE TABLE permission_probe(i INTEGER); INSERT INTO permission_probe VALUES (1);', False)
        if setup['exit_code']:
            raise RuntimeError('Database fixture setup failed: ' + setup['stderr'])
        for name, data, _ in CASES:
            setup = call(binary, db, "COPY (" + data + ") TO '" + str(folder / (name + '.parquet'))
                         + "' (FORMAT PARQUET, ROW_GROUP_SIZE 2048)", False)
            if setup['exit_code']:
                raise RuntimeError('Parquet setup failed: ' + setup['stderr'])
        permissions = {'select': call(binary, db, 'SELECT * FROM permission_probe'),
                       'create_index': call(binary, db, 'CREATE INDEX forbidden ON permission_probe(i)'),
                       'insert': call(binary, db, 'INSERT INTO permission_probe VALUES (2)')}
        verified = permissions['select']['exit_code'] == 0 and all(permissions[k]['exit_code'] != 0
                    and 'read-only' in permissions[k]['stderr'].lower() for k in ('create_index', 'insert'))
        if '--calibration' in sys.argv:
            queries = [('correct', DEFAULT_QUERY), ('wrong_aggregate', DEFAULT_QUERY.replace('max(', 'min(')),
                       ('wrong_type', DEFAULT_QUERY.replace('BIGINT', 'INTEGER')),
                       ('drops_null_result', DEFAULT_QUERY + ' HAVING count(i) > 0')]
        else:
            path = ROOT / QUERY_FILE
            queries = [('candidate', path.read_text() if path.is_file() else '')]
        records = []
        for name, query in queries:
            record = execute(query, folder, binary, db, verified) if query else {'missing_query': True, 'permissions_verified': verified}
            status, reason = verdict(record)
            records.append(record | {'name': name, 'status': status, 'reason': reason})
        print(json.dumps({'base_commit': base, 'engine': {'path': 'build/duckdb',
            'version': subprocess.check_output([str(binary), '-version'], text=True).strip(),
            'sha256': hashlib.sha256(binary.read_bytes()).hexdigest(),
            'scope': 'Existing isolated image engine for the user query only; official verifier tests candidate engine changes'},
            'permissions': permissions, 'records': records, 'query_file': QUERY_FILE}))


if __name__ == '__main__':
    main()
