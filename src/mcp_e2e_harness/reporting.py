"""Durable derived reports and recovery from immutable row evidence."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import __version__, checks, measurements
from .manifest import Manifest, load_manifest
from .secrets import assert_clean, collect_scan_set


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, value: dict, scan: list[str]) -> None:
    """Replace a report atomically; interrupted writes retain the last checkpoint."""
    text = json.dumps(value, indent=2) + '\n'
    assert_clean(text, scan, str(path))
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(text)
    temporary.replace(path)


def read_trace(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def row_trace(run_dir: Path, label: str, meta: dict) -> list[dict]:
    path = run_dir / label / 'trace.jsonl'
    if meta.get('trace_records', 0) and not path.exists():
        raise FileNotFoundError(f'recorded trace missing: {path}')
    return read_trace(path)


def error_checks(declarations: list[dict], cells: list[str], error: str) -> list[dict]:
    report = []
    for index, declaration in enumerate(declarations):
        cid = declaration.get('id') if isinstance(declaration, dict) else None
        entry = {'id': cid or f'check[{index}]', 'outcome': 'error',
                 'error': error, 'matched': 0, 'failures': []}
        report.append({**entry, 'cells': {cell: dict(entry) for cell in cells}})
    return report


def finish_reports(run_dir: Path, record: dict, manifest: Manifest,
                   rows: dict[str, dict], *, update_rows: bool = False,
                   log=print) -> list[dict]:
    """Checkpoint before any evaluator, and after every completed or failed stage."""
    scan = collect_scan_set(manifest.data)
    stages = record['reporting'] = {'checks': {'status': 'pending'},
                                  'measurements': {'status': 'pending'}}
    manifest_path = run_dir / 'run-manifest.json'
    record['checks'] = None
    record['measurements'] = None
    write_json(manifest_path, record, scan)
    report = []
    for stage in ('checks', 'measurements'):
        stages[stage] = {'status': 'running'}
        write_json(manifest_path, record, scan)
        try:
            if stage == 'checks':
                if not record.get('dry_run'):
                    traces = {label: row_trace(run_dir, label, meta) for label, meta in rows.items()}
                    report = checks.evaluate_checks(manifest.checks, traces,
                                                    cells=list(record['cells']), metadata=rows)
                    for check in report:
                        for failure in check.get('failures', []):
                            meta = rows[failure['invocation']]
                            failure['driver_family'] = meta.get('driver_family')
                            failure['reproducibility'] = meta.get('reproducibility')
                write_json(run_dir / 'checks-report.json', {
                    'cells': {cell: {'driver': definition.get('driver'),
                                     **record.get('cell_marks', {}).get(cell, {})}
                              for cell, definition in record['cells'].items()},
                    'checks': report}, scan)
                record['checks'] = {entry['id']: entry['outcome'] for entry in report}
                record['failures'] += sum(entry['outcome'] == 'error' for entry in report)
                for entry in report:
                    log(f"check      : {entry['id']}: {entry['outcome']}  (matched {entry['matched']})")
            else:
                # All calculations precede row writes; recovery never edits rows.
                values = {}
                for label in rows:
                    if not manifest.measurements:
                        values[label] = {}
                        continue
                    traces = row_trace(run_dir, label, rows[label])
                    answer_path = run_dir / label / 'answer.txt'
                    answer = answer_path.read_text() if answer_path.exists() else ''
                    values[label] = measurements.compute_measurements(manifest.measurements, traces, answer)
                summary = {}
                for declaration in manifest.measurements:
                    name = declaration['name']
                    entries = [entry for value in values.values() for entry in value.get(name, [])]
                    measure = measurements.MEASURES[declaration['measure']]
                    summary[name] = {
                        'measure': measure.full_name, 'method_hash': measure.content_hash(),
                        'rows': sum(bool(value.get(name)) for value in values.values()),
                        'records_measured': sum(entry.get('note') is None for entry in entries),
                        'records_null': sum(entry.get('note') is not None for entry in entries)}
                if update_rows:
                    for label, meta in rows.items():
                        meta['measurements'] = values[label]
                        write_json(run_dir / label / 'meta.json', meta, scan)
                record['measurements'] = summary
                for name, value in summary.items():
                    log(f"measurement: {name}: {value['records_measured']} record(s) measured, "
                        f"{value['records_null']} null -- not an outcome")
            stages[stage] = {'status': 'complete'}
        except Exception as exc:
            error = f'{type(exc).__name__}: {exc}'
            stages[stage] = {'status': 'error', 'exception': error}
            record['failures'] += 1
            artifact = 'checks-report.json' if stage == 'checks' else 'run-manifest.json measurements summary'
            log(f'REPORTING ERROR: {stage}: {error}; missing completed {artifact}')
            if stage == 'checks':
                report = error_checks(manifest.checks, list(record['cells']), error)
                record['checks'] = {entry['id']: 'error' for entry in report}
                try:
                    write_json(run_dir / 'checks-report.json', {'checks': report, 'error': error}, scan)
                    log('checks-report.json written with every check marked error')
                except Exception as write_error:
                    stages[stage]['artifact_error'] = f'{type(write_error).__name__}: {write_error}'
                    log(f'REPORTING ERROR: missing checks-report.json: {write_error}')
        write_json(manifest_path, record, scan)
    return report


def find_manifest(run_dir: Path, rows: dict[str, dict], explicit: Path | None) -> Manifest:
    hashes = {meta['manifest_sha256'] for meta in rows.values()}
    if len(hashes) != 1:
        raise ValueError('row manifest hashes disagree; refusing to combine different runs')
    candidates = [explicit] if explicit else [run_dir / 'manifest-source.json']
    if not explicit:
        for parent in (run_dir, *run_dir.parents):
            candidates.extend(parent.glob('*manifest*.json'))
            candidates.extend((parent / 'documentation').glob('*manifest*.json'))
    for path in candidates:
        if path is None or not path.is_file():
            continue
        try:
            manifest = load_manifest(path)
        except (OSError, ValueError):
            continue
        if manifest.sha256 in hashes:
            return manifest
    raise ValueError('no suite manifest matching the rows; supply --manifest with the pinned file')


def rebuild(run_dir: Path, *, overwrite: bool = False, manifest_path: Path | None = None,
            log=print) -> dict:
    """Reconstruct identities from metadata, never from directory segment names."""
    # Import here to keep the execution/reporting dependency one-way at import time.
    from .runner import (
        answers_across_prompts,
        answers_by_prompt,
        plan_invocations,
        replacement_cause,
        unavailable_slot_failures,
        zero_trace_cells,
    )

    run_dir = run_dir.resolve()
    if (run_dir / 'run-manifest.json').exists() and not overwrite:
        raise ValueError('run-manifest.json already exists; use --overwrite to rebuild it')
    rows = {}
    for path in sorted(run_dir.rglob('meta.json')):
        meta = json.loads(path.read_text())
        if 'cell' in meta and 'prompt_id' in meta:
            rows[path.parent.relative_to(run_dir).as_posix()] = meta
    if not rows:
        raise ValueError('no row metadata found')
    manifest = find_manifest(run_dir, rows, manifest_path)
    results = list(rows.values())
    # Preserve known run-only facts from a previous checkpoint, when available.
    previous_path = run_dir / 'run-manifest.json'
    previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
    input_path = run_dir / 'run-input.json'
    inputs = json.loads(input_path.read_text()) if input_path.exists() else {}
    cells = list(dict.fromkeys(meta['cell'] for meta in results))
    known_selection = (previous.get('selection')
                       if 'selection' not in previous.get('unrecoverable', {}) else None)
    selection = known_selection or inputs.get('selection') or {
        'cells': cells, 'repeats': None, 'precondition_retries': None, 'groups': None, 'prompts': None}
    selection_known = bool(known_selection or inputs.get('selection'))
    cells = selection['cells']
    notes = dict(previous.get('unrecoverable', {}))
    notes.pop('replacement_events', None)
    if not selection_known:
        notes['selection'] = ('Only observed cells and slots are known; '
                              'original selection and retry allowance are unknown.')
    grouped = {}
    for label, meta in rows.items():
        key = (meta['cell'], meta['prompt_id'], meta.get('repetition'))
        grouped.setdefault(key, []).append((label, meta))
    slots, unmet, limits, counts = {}, {}, {}, {}
    replacement_events = []
    for (cell, prompt, repetition), attempts in grouped.items():
        attempts.sort(key=lambda pair: pair[1].get('attempt', 1))
        # A slot's file locator is its first retained attempt's actual path.
        slot_name = attempts[0][0]
        reached = [(label, meta) for label, meta in attempts if meta.get('scored_turn_reached')]
        scored = [(label, meta) for label, meta in reached if not meta.get('harness_failure')]
        last = attempts[-1][1]
        allowance = selection.get('precondition_retries')
        status = ('scored' if scored else 'failed')
        if not scored and last.get('precondition_unmet'):
            status = ('replacement_disabled' if allowance == 0 else 'exhausted'
                      if allowance is not None and last.get('attempt', 1) >= allowance + 1 else 'unmet')
        unavailable = replacement_cause(last) == 'upstream_unavailable'
        if not scored and unavailable and allowance and last.get('attempt', 1) >= allowance + 1:
            status = 'exhausted'
        if not scored and (previous.get('slots', {}).get(slot_name) or {}).get('status') == 'budget_stopped':
            status = 'budget_stopped'
        slots[slot_name] = {'status': status, 'result': reached[-1][0] if reached else None,
                            'attempts': len(attempts), 'cell': cell, 'prompt_id': prompt, 'repetition': repetition,
                            'attempts_unmet': sum(bool(meta.get('precondition_unmet')) for _, meta in attempts),
                            'attempts_unavailable': sum(replacement_cause(meta) == 'upstream_unavailable'
                                                        for _, meta in attempts)}
        if not scored and unavailable:
            slots[slot_name]['cause'] = 'upstream_unavailable'
        counter = counts.setdefault(cell, {}).setdefault(prompt, {
            'asked': None, 'observed_slots': 0, 'reached': 0, 'attempts': 0,
            'attempts_unmet': 0, 'attempts_unavailable': 0})
        counter['observed_slots'] += 1
        counter['reached'] += len(reached)
        counter['attempts_unmet'] += slots[slot_name]['attempts_unmet']
        counter['attempts_unavailable'] += slots[slot_name]['attempts_unavailable']
        counter['attempts'] += len(attempts)
        for index, (label, meta) in enumerate(attempts):
            if meta.get('attempt', 1) > 1:
                predecessor = attempts[index - 1][1] if index else {}
                replacement_events.append({
                    'slot': slot_name, 'invocation': label, 'attempt': meta['attempt'],
                    'at': meta.get('started_utc'),
                    'cause': replacement_cause(predecessor)})
            if meta.get('precondition_unmet'):
                unmet[label] = {'slot': slot_name, 'attempt': meta.get('attempt', 1),
                                'cause': meta['precondition_unmet']['cause']}
            elif not meta.get('harness_failure') and meta.get('consumer_limit'):
                limits[label] = meta['consumer_limit']['cause']
    if selection_known:
        planned = plan_invocations(manifest, cells,
                                   set(selection['groups']) if selection.get('groups') else None,
                                   set(selection['prompts']) if selection.get('prompts') else None)
        for item in planned:
            counter = counts.setdefault(item.cell_name, {}).setdefault(item.entry['id'], {
                'observed_slots': 0, 'reached': 0, 'attempts': 0,
                'attempts_unmet': 0, 'attempts_unavailable': 0})
            counter['asked'] = selection.get('repeats') or 1
    elif previous.get('invocation_counts'):
        for cell, prompts in counts.items():
            for prompt, counter in prompts.items():
                counter['asked'] = previous['invocation_counts'].get(cell, {}).get(prompt, {}).get('asked')
    else:
        notes['invocation_counts.asked'] = (
            'Requested slot counts are unknown; observed_slots counts retained slots only.')
    marks = {}
    recorded_gates = {}
    for cell in cells:
        first = next((meta for meta in results if meta['cell'] == cell), {})
        marks[cell] = {key: first.get(key) for key in (
            'driver_family', 'scaffold', 'provider_pin', 'quantization_asserted', 'reproducibility')}
        family = first.get('driver_family')
        if family and family != 'product':
            served, verified = {}, set()
            for meta in results:
                if meta['cell'] != cell:
                    continue
                family_record = meta.get(family) or {}
                for provider, count in (family_record.get('served_providers') or {}).items():
                    served[provider] = served.get(provider, 0) + count
                names = family_record.get('provider_verified') or []
                verified.update([names] if isinstance(names, str) else names)
                if family_record.get('probe') is not None:
                    recorded_gates[cell] = family_record['probe']
            marks[cell].update(served_providers=served, provider_verified=sorted(verified) or None,
                               probe=recorded_gates.get(cell))
        marks[cell]['answers'] = answers_by_prompt(results, cell)
        marks[cell]['answers_across_prompts'] = answers_across_prompts(marks[cell]['answers'])
    dead = zero_trace_cells(results, False)
    budget = previous.get('budget')
    if budget is None:
        budget = {'run_cap_usd': inputs.get('budget_usd'), 'spent_usd': None, 'per_cell_spent_usd': None,
                  'stops': None, 'run_stopped_by_budget': None,
                  'recorded_row_spend_usd': sum(meta.get('spend_usd') or 0 for meta in results)}
        notes['budget'] = ('Run totals and stop events are unknown; '
                           'recorded_row_spend_usd excludes unrecorded usage.')
    if not previous.get('pre_run'):
        notes['pre_run'] = 'Pre-run estimate and disclosures were not recorded in the rows.'
    record = {
        'harness_version': __version__, 'generated_utc': now(),
        'rebuilt': {'at': now(), 'harness_version': __version__,
                    'reason': 'Rebuilt from retained row artifacts by report --run-dir'},
        'unrecoverable': notes,
        'manifest': {'path': str(manifest.path), 'sha256': manifest.sha256},
        'selection': selection, 'cells': {cell: manifest.cells[cell] for cell in cells},
        'fixtures': manifest.fixtures, 'dry_run': previous.get('dry_run', inputs.get('dry_run', False)),
        'results': results, 'slots': slots, 'preconditions_unmet': unmet, 'consumer_limits': limits,
        'invocation_counts': counts, 'cell_marks': marks,
        'loop_cells': {cell: mark for cell, mark in marks.items() if mark['driver_family'] == 'loop'},
        'driver_versions': {meta['driver']['id']: meta['driver'].get('cli_version') for meta in results},
        'driver_probes': previous.get('driver_probes'),
        'cell_gates': previous.get('cell_gates') or recorded_gates or None,
        'pre_run': previous.get('pre_run'), 'replacement_events': replacement_events,
        'budget': budget, 'voided_cells': previous.get('voided_cells'), 'zero_trace_cell_failures': dead,
        'failures': sum(bool(meta.get('harness_failure')) and not replacement_cause(meta)
                        for meta in results) + len(dead) + unavailable_slot_failures(slots, results, dead),
    }
    for key in ('driver_probes', 'cell_gates', 'voided_cells'):
        if record[key] is not None:
            notes.pop(key, None)
        else:
            notes[key] = 'No run-level checkpoint retained this fact; row evidence remains available.'
    finish_reports(run_dir, record, manifest, rows, log=log)
    log(f"rebuilt: {len(rows)} rows, {len(slots)} slots, {len(unmet)} unmet attempts; {run_dir}")
    return record
