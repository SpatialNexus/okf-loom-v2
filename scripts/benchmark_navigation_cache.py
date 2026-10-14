"""Compare live document/search bytes and paired TTFB against a local Git revision.

Opt-in maintainer proof: uses git archive, disposable bundle copies and two
private local servers. Never changes the checkout, its bundles or a running
preview. Example (from the checkout, using the test Python):
    python scripts/benchmark_navigation_cache.py --out /private/evidence
Only the per-server CSRF token in the Studio bootstrap is normalized. HTTP
Date is not part of the compared response body. No clock-derived content,
source revisions, projection identities, permissions or search data is removed.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import shutil
import socket
import statistics
import subprocess
import sys
import time
from urllib.parse import quote

from okf_loom import Bundle

ROOT = Path(__file__).resolve().parents[1]
QUERIES = ('theme', 'graph', 'comment', 'studio', 'heading fragment', 'zzznavigationcache_nohitzzz')
ROUTES = ('/how-to/archive_threads', '/__data/doc?id=how-to/archive_threads', '/__search?q=theme')


def fetch(port, route):
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=120)
    try:
        start = time.perf_counter()
        connection.request('GET', route)
        response = connection.getresponse()
        # Headers and first response byte: no body transfer in the TTFB metric.
        first = response.read(1)
        ttfb = time.perf_counter() - start
        body = first + response.read()
        return response.status, body, ttfb
    finally:
        connection.close()


def normalized(body):
    return re.sub(
        rb'(<script type="application/json" id="okf-studio-bootstrap">\{"token": ")[^"]*(")',
        rb'\1<per-server-token>\2', body)


@contextmanager
def server(runtime, bundle, port, log_path):
    env = dict(os.environ, PYTHON=sys.executable)
    with log_path.open('wb') as log:
        process = subprocess.Popen([
            str(runtime / 'scripts/okf-loom'), 'serve', str(bundle),
            '--host', '127.0.0.1', '--port', str(port), '--no-open', '--no-watch-ui',
        ], env=env, cwd=runtime, stdout=log, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic() + 60
            while True:
                if process.poll() is not None:
                    raise RuntimeError(f'server exited: see {log_path}')
                try:
                    status, _, _ = fetch(port, '/__search?q=zzznavigationcache_nohitzzz')
                    if status == 200:
                        break
                except OSError:
                    pass
                if time.monotonic() > deadline:
                    raise TimeoutError(f'server readiness: see {log_path}')
                time.sleep(0.1)
            yield
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--base-revision', default='8f38444')
    parser.add_argument('--pairs', type=int, default=9)
    parser.add_argument('--base-port', type=int, default=8900)
    parser.add_argument('--head-port', type=int, default=8901)
    args = parser.parse_args()
    if args.pairs < 7:
        parser.error('at least seven pairs required')
    if args.base_port == args.head_port or not all(
            1 <= port <= 65535 for port in (args.base_port, args.head_port)):
        parser.error('two distinct nonzero TCP ports required')
    # Refuse occupied ports before starting either process. Never adopt or
    # shut down another preview that happens to be listening on a chosen port.
    for port in (args.base_port, args.head_port):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    out = args.out.resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=False)
    base = out / 'base'
    base.mkdir()
    with (out / 'base.tar').open('wb') as archive:
        subprocess.run(['git', 'archive', args.base_revision], cwd=ROOT, stdout=archive, check=True)
    subprocess.run(['tar', '-xf', str(out / 'base.tar'), '-C', str(base)], check=True)
    (out / 'base.tar').unlink()
    summary = {'base_revision': subprocess.check_output(
        ['git', 'rev-parse', args.base_revision], cwd=ROOT, text=True).strip(),
        'normalizations': ['Studio bootstrap token (per-server CSRF secret)'],
        'comparisons': {}, 'benchmarks': {}}
    comparisons = []
    timings = []
    for name in ('docs-bundle', 'samples/demo_bundle'):
        tag = name.replace('/', '-')
        bundles = {}
        for variant in ('base', 'head'):
            destination = out / 'bundles' / tag / variant / Path(name).name
            shutil.copytree(base / name, destination)
            bundles[variant] = destination
        concepts = sorted('/'.join(cid) for cid in Bundle.load(bundles['base']).concepts)
        routes = [route for cid in concepts for route in (
            '/' + quote(cid, safe='/'), '/__data/doc?id=' + quote(cid, safe='/'))]
        routes.extend('/__search?q=' + quote(query) for query in QUERIES)
        with server(base, bundles['base'], args.base_port, out / (tag + '-base.log')), server(
                ROOT, bundles['head'], args.head_port, out / (tag + '-head.log')):
            # Two passes cover initial cache population and warm repeated queries.
            for repetition in range(2):
                for route in routes:
                    bstatus, before, _ = fetch(args.base_port, route)
                    hstatus, after, _ = fetch(args.head_port, route)
                    equal = bstatus == hstatus == 200 and normalized(before) == normalized(after)
                    row = {'bundle': name, 'pass': repetition, 'route': route, 'equal': equal,
                           'base_status': bstatus, 'head_status': hstatus,
                           'base_sha256': hashlib.sha256(normalized(before)).hexdigest(),
                           'head_sha256': hashlib.sha256(normalized(after)).hexdigest()}
                    comparisons.append(row)
                    if not equal:
                        key = hashlib.sha256((tag + route).encode()).hexdigest()[:16]
                        (out / (key + '-base.body')).write_bytes(normalized(before))
                        (out / (key + '-head.body')).write_bytes(normalized(after))
            summary['comparisons'][name] = {
                'concepts': len(concepts), 'queries': list(QUERIES), 'passes': 2,
                'responses': 2 * len(routes),
                'equal': all(row['equal'] for row in comparisons if row['bundle'] == name)}
            if name == 'docs-bundle':
                for route in ROUTES:
                    for pair in range(args.pairs):
                        uptime = subprocess.check_output(['uptime'], text=True).strip()
                        load = os.getloadavg()
                        order = ('base', 'head') if pair % 2 == 0 else ('head', 'base')
                        row = {'route': route, 'pair': pair, 'order': order,
                               'uptime': uptime, 'loadavg': load}
                        for variant in order:
                            port = args.base_port if variant == 'base' else args.head_port
                            status, _, elapsed = fetch(port, route)
                            assert status == 200
                            row[variant + '_ms'] = elapsed * 1000
                        timings.append(row)
                    rows = [row for row in timings if row['route'] == route]
                    values = {}
                    for variant in ('base', 'head'):
                        xs = [row[variant + '_ms'] for row in rows]
                        q1, _, q3 = statistics.quantiles(xs, n=4, method='inclusive')
                        values[variant] = {'n': len(xs), 'median_ms': statistics.median(xs),
                                           'q1_ms': q1, 'q3_ms': q3}
                    values['loadavg1_range'] = [min(row['loadavg'][0] for row in rows),
                                                max(row['loadavg'][0] for row in rows)]
                    summary['benchmarks'][route] = values
        (out / 'comparisons.json').write_text(json.dumps(comparisons, indent=2) + '\n')
        (out / 'timings.json').write_text(json.dumps(timings, indent=2) + '\n')
        (out / 'summary.json').write_text(json.dumps(summary, indent=2) + '\n')
    print(json.dumps(summary, indent=2))
    return 0 if all(item['equal'] for item in summary['comparisons'].values()) else 1


if __name__ == '__main__':
    raise SystemExit(main())
