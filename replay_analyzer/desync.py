"""Recorded-checkpoint investigation. Never reconstructs or authenticates game state."""
from collections import Counter, defaultdict


def analyze_desync(report):
    commands = report['commands']['items']
    expected = {p['source_index'] for p in report['players']}
    labels = {p['source_index']: p['name'] for p in report['players']}
    groups = defaultdict(list)
    invalid = []
    for c in commands:
        if c['name'] != 'MSG_LOGIC_CRC':
            continue
        args = c['arguments']
        if len(args) != 5 or [a['type'] for a in args] != ['integer', 'timestamp', 'timestamp', 'boolean', 'boolean']:
            invalid.append(c['index'])
            continue
        values = [a['value'] for a in args]
        if any(type(values[i]) is not int for i in (0, 1, 2)) or any(type(values[i]) is not bool for i in (3, 4)):
            invalid.append(c['index'])
            continue
        if not 0 <= values[2] <= report['match']['last_frame']:
            invalid.append(c['index'])
            continue
        groups[(values[1], values[2])].append({
            'source_index': c['source_index'], 'player': c['player'],
            'crc': values[0] & 0xffffffff, 'record_frame': c['frame'],
            'delay_frames': c['frame'] - values[2], 'playback': values[3],
            'mismatch_reporting': values[4], 'command_index': c['index'],
        })
    checkpoints = []
    for (epoch, frame), reports in sorted(groups.items(), key=lambda x: (x[0][1], x[0][0])):
        by_source = defaultdict(set)
        for item in reports:
            by_source[item['source_index']].add(item['crc'])
        values = sorted({v for vs in by_source.values() for v in vs})
        conflicts = [s for s, vs in by_source.items() if len(vs) > 1]
        modes = {r['playback'] for r in reports}
        missing = sorted(expected - by_source.keys())
        status = ('conflicting_reports' if conflicts else 'mixed_modes' if len(modes) > 1 else
                  'insufficient_reports' if len(by_source) < 2 else
                  'disagreement' if len(values) > 1 else 'matching_reports')
        checkpoints.append({
            'epoch': epoch, 'frame': frame, 'time': f'{frame // 900}:{frame // 15 % 60:02}',
            'status': status, 'agreement': True if status == 'matching_reports' else False if status == 'disagreement' else None,
            'report_count': len(reports), 'source_count': len(by_source),
            'unique_crc_count': len(values), 'crc_values': [f'0x{v:08X}' for v in values],
            'missing_sources': missing, 'conflicting_sources': conflicts,
            'unexpected_sources': sorted(by_source.keys() - expected),
            'duplicate_reports': len(reports) - sum(len(v) for v in by_source.values()),
            'mismatch_reporting': any(r['mismatch_reporting'] for r in reports),
            'reports': reports,
        })
    incidents = []
    for checkpoint in checkpoints:
        if checkpoint['status'] != 'disagreement' and not checkpoint['mismatch_reporting']:
            continue
        sources = {r['source_index'] for r in checkpoint['reports']}
        modes = {r['playback'] for r in checkpoint['reports']}
        previous = [p for p in checkpoints if p['epoch'] == checkpoint['epoch'] and p['frame'] < checkpoint['frame']
                    and p['status'] == 'matching_reports' and sources <= {r['source_index'] for r in p['reports']}
                    and {r['playback'] for r in p['reports']} == modes]
        last = previous[-1]['frame'] if previous else None
        start = last + 1 if last is not None else 0
        frame = checkpoint['frame']
        nearby = [c for c in commands if start <= c['frame'] <= frame and c['is_action']]
        objects = Counter(a['value'] for c in nearby for a in c['arguments'] if a['type'] == 'object_id' and a['value'])
        incidents.append({
            'epoch': checkpoint['epoch'], 'frame': frame, 'start_frame': start,
            'last_matching_frame': last, 'kind': checkpoint['status'] if checkpoint['status'] == 'disagreement' else 'reported_mismatch',
            'command_count': len(nearby), 'command_indices': [c['index'] for c in nearby[:250]],
            'commands_truncated': len(nearby) > 250,
            'categories': dict(Counter(c['category'] for c in nearby)),
            'objects': [{'id': key, 'references': count} for key, count in objects.most_common(30)],
            'sources': sorted(sources),
        })
        if len(incidents) == 100:
            break
    bad = [p for p in checkpoints if p['status'] == 'disagreement']
    flagged = [p for p in checkpoints if p['mismatch_reporting']]
    matched = [p for p in checkpoints if p['status'] == 'matching_reports']
    status = 'disagreement' if bad else 'reported_mismatch' if flagged else 'matching_reports' if matched else 'insufficient_data'
    title = {'disagreement': 'Recorded states disagree', 'reported_mismatch': 'A mismatch was reported',
             'matching_reports': 'Available CRC reports match', 'insufficient_data': 'Not enough CRC evidence'}[status]
    steps = []
    if incidents:
        first = incidents[0]
        steps.append({'title': 'Inspect the checkpoint window', 'detail': f"Review frames {first['start_frame']} through {first['frame']}. This brackets recorded evidence, not the exact cause.", 'action': 'window'})
    steps.append({'title': 'Compare another recording', 'detail': 'Add a replay from another player in the same match. Check command differences before the first CRC disagreement.', 'action': 'compare'})
    steps.append({'title': 'Compare native diagnostic files', 'detail': 'Add matching DESYNC text or BIN_DESYNC files from both players to locate the first differing captured field.', 'action': 'diagnostics'})
    coverage = {
        'expected_sources': sorted(expected), 'source_labels': labels,
        'basis': 'Header roster mapped to native slots; membership can change during play.',
        'checkpoint_count': len(checkpoints), 'matching_count': len(matched), 'disagreement_count': len(bad),
        'incomplete_count': sum(bool(p['missing_sources']) or p['source_count'] < 2 for p in checkpoints),
        'conflicting_count': sum(p['status'] == 'conflicting_reports' for p in checkpoints),
        'mixed_mode_count': sum(p['status'] == 'mixed_modes' for p in checkpoints),
        'invalid_crc_commands': invalid[:100], 'invalid_crc_count': len(invalid),
        'decode_errors': report['summary']['decode_error_count'],
    }
    result = {'version': 1, 'status': status, 'title': title, 'coverage': coverage,
              'checkpoints': checkpoints, 'incidents': incidents, 'next_steps': steps,
              'limitations': 'Recorded checksums cannot identify the faulty player or prove the root cause. Matching checksums are not proof of identical state.'}
    report['network']['crc_checkpoints'] = checkpoints
    report['summary']['crc_disagreement_count'] = len(bad)
    report['summary']['crc_checkpoint_count'] = len(checkpoints)
    # Replace earlier claims based on untyped values and raw counts.
    findings = report['cheat_analysis']['findings']
    findings[:] = [f for f in findings if f.get('detector') not in ('crc_agreement', 'crc_mismatch_routing')]
    if bad or flagged:
        report['cheat_analysis']['assessment'] = title
        report['cheat_analysis']['tone'] = 'danger' if bad else 'warning'
        report['summary']['assessment'] = title
        report['summary']['assessment_tone'] = report['cheat_analysis']['tone']
        findings.append({'detector': 'crc_agreement', 'severity': 'high', 'confidence': 'recorded_evidence',
                         'title': title, 'evidence': f'{len(bad)} differing checkpoint(s); {len(flagged)} flagged checkpoint(s).',
                         'interpretation': result['limitations']})
    report['cheat_analysis']['finding_count'] = len(findings)
    return result
