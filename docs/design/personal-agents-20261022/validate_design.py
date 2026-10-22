"""Offline schema/fixture checks only. Does not register agents or call any service."""
import copy
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

root = Path(__file__).resolve().parent
profiles = json.loads((root / 'agent-definitions.json').read_text())
contracts = json.loads((root / 'tool-contracts.json').read_text())
fixtures = json.loads((root / 'synthetic-cases.json').read_text())
tools = {tool['name']: tool for tool in contracts['tools']}
checks = 0
for tool in tools.values():
    for field in ('input_schema', 'output_schema'):
        Draft202012Validator.check_schema(tool[field])
        checks += 1
    assert tool['input_schema']['additionalProperties'] is False
    for name in ('owner', 'owner_user_id', 'user_id', 'actor_kind', 'approval', 'grant'):
        assert name not in tool['input_schema']['properties']
        checks += 1
for case in fixtures['cases']:
    tool = tools[case['tool']]
    for field, sample in [('input_schema', case['input']), ('output_schema', case['expected_success'])]:
        Draft202012Validator(tool[field], format_checker=FormatChecker()).validate(sample)
        checks += 1
    forged = copy.deepcopy(case['input'])
    forged['owner_user_id'] = '00000000-0000-4000-8000-000000000099'
    assert not Draft202012Validator(tool['input_schema']).is_valid(forged)
    checks += 1
for agent in profiles['agents']:
    assert agent['tools'] and set(agent['tools']) <= set(tools)
    assert not any('verify' in tool or 'complete' in tool or 'accept' in tool for tool in agent['tools'])
    checks += 2
policy = profiles['shared_policy']
assert policy['execution_route'] == 'local_only'
assert not policy['automatic_cloud_fallback']
assert not policy['create_permissions_or_accounts']
assert policy['registration_state'] == 'disabled_design_only'
checks += 4
assert fixtures['synthetic_only']
print(f'PASS: {checks} schema, fixture and manifest assertions (offline only).')
print('NOT RUN: AoiTalk integration, provider routes, local OCR, DB migrations, scheduling, production registration.')
