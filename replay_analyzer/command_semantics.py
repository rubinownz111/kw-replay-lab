"""Evidence-backed labels; unknown arguments retain their wire type and value."""
import json
from pathlib import Path

CATALOG = json.loads((Path(__file__).parent / 'data/command_semantics.json').read_text(encoding='utf-8'))


def annotate_command(command):
    spec = CATALOG['commands'].get(str(command['message_type']))
    command['display_name'] = spec['title'] if spec else command['name'].removeprefix('MSG_').replace('_', ' ').title()
    command['meaning'] = spec['meaning'] if spec else 'Recovered command name. Argument meanings have not been verified.'
    command['semantic_status'] = 'verified_fields' if spec else 'name_only'
    command['semantic_evidence'] = spec['evidence'] if spec else {'function': '0x575913', 'basis': 'Command name only.'}
    issues = []
    for i, arg in enumerate(command['arguments']):
        arg['label'] = f'Argument {i + 1}'
        arg['label_status'] = 'unknown'
    if spec:
        command['category'] = spec['category']
        for field in spec['arguments']:
            i = field['index']
            if i >= len(command['arguments']):
                issues.append(f"Missing {field['label']} (argument {i + 1}).")
                continue
            arg = command['arguments'][i]
            if arg['type'] != field['type']:
                issues.append(f"{field['label']} expects {field['type']}, found {arg['type']}.")
                continue
            arg.update(label=field['label'], label_status='verified')
        if command['message_type'] in (557, 558) and len(command['arguments']) > 2:
            mode, arg = command['arguments'][1:3]
            if mode['type'] == 'boolean' and arg['type'] == 'integer':
                arg['label'] = 'Production index' if mode['value'] else 'Unit template key'
    command['schema_issues'] = issues
    if issues:
        command['semantic_status'] = 'shape_mismatch'
    return command
