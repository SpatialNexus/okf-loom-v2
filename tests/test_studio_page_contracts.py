"""Real HTTP and durable-store proof for document targets and scoped sharing."""
import http.client
import json
import threading
import subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer

import pytest

from scripts.okf_loom.model import Bundle
from scripts.okf_loom.server import OKFWikiHandler, _start_public_ingress, _stop_public_ingress
from scripts.okf_loom.studio import Studio


@pytest.fixture
def live(tmp_path):
    (tmp_path / 'sub').mkdir()
    for name, body in {'index.md': '# Home\n', 'log.md': '# 2026-09-18\nNote\n',
                       'sub/index.md': '# Directory\n', 'sub/log.md': '# History\n',
                       'sub/note.md': '---\ntype: Note\n---\n# Note\nText\n'}.items():
        (tmp_path / name).write_text(body)
    studio = Studio.for_bundle(tmp_path)
    studio.ensure_session()
    server = ThreadingHTTPServer(('127.0.0.1', 0), OKFWikiHandler)
    server.bundle = Bundle.load(tmp_path)
    server.studio = studio
    server.config = {}
    server.name = 'Test'
    server.plugin = None
    server.csrf_token = 'local-secret'
    server.allowed_hosts = ('127.0.0.1',)
    server.studio_edit = True
    server.studio_live = True
    server._state_lock = threading.RLock()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, studio, tmp_path
    finally:
        if getattr(server, 'public_ingress_server', None):
            _stop_public_ingress(server)
        server.shutdown(); server.server_close(); thread.join()


def call(server, path, body=None, *, token='local-secret', key=None, headers=None, method=None):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
    hdr = {'X-OKF-Token': token, 'Content-Type': 'application/json', **(headers or {})}
    if key:
        hdr['Idempotency-Key'] = key
    conn.request(method or ('POST' if body is not None else 'GET'), path,
                 json.dumps(body) if body is not None else None, hdr)
    response = conn.getresponse()
    raw = response.read().decode()
    status = response.status
    conn.close()
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, raw


@pytest.mark.parametrize('document', ['index.md', 'sub/index.md', 'log.md', 'sub/log.md', 'sub/note.md'])
def test_page_create_reply_replay_and_reload(live, document):
    server, studio, root = live
    request = {'document': document, 'body': 'Review generated area', 'anchor': {'kind': 'page'}}
    status, result = call(server, '/__comment', request, key='page-create-0001')
    assert status == 201, result
    row = result['comment']
    assert row['document'] == document
    assert row['concept'] == ('sub/note' if document == 'sub/note.md' else '')
    assert call(server, '/__comment', request, key='page-create-0001') == (status, result)
    status, reply = call(server, '/__comment', {**request, 'body': 'Reply', 'parent_id': row['id']})
    assert status == 201, reply
    agent = studio.post_comment(concept=row['concept'], parent_id=row['id'], body='Clarification', actor='agent', state='resolved')
    assert agent['document'] == document
    reopened = Studio.for_bundle(root)
    assert reopened.get_comment(row['id'])['document'] == document
    status, rows = call(server, '/__comments?document=' + document)
    assert status == 200, rows
    assert len(rows['comments']) == 3
    assert call(server, '/__data/page?document=' + document)[0] == 200
    assert call(server, '/' + document)[0] == 200
    (root / document).unlink()
    assert call(server, '/__comment', request, key='page-create-0001') == (201, result)
    assert call(server, '/__comments?document=' + document)[0] == 200


@pytest.mark.parametrize('document', ['../log.md', '/log.md', 'sub/../log.md', '.okf-loom/session/log.md', 'sub//log.md', 'log.md#x', 'missing.md'])
def test_page_targets_reject_traversal_private_and_missing(live, document):
    server, studio, _ = live
    assert call(server, '/__comment', {'document': document, 'body': 'x'})[0] in {400, 404}
    assert studio.list_comments() == []


def test_legacy_filter_and_cross_target_reply(live):
    server, studio, _ = live
    old = studio.post_comment(concept='sub/note', body='old', detail={'unknown': [1]})
    status, result = call(server, '/__comments?document=sub/note.md')
    assert status == 200
    assert result['comments'][0]['id'] == old['id']
    assert 'document' not in studio.get_comment(old['id'])
    assert call(server, '/__comment', {'document': 'log.md', 'body': 'bad', 'parent_id': old['id']})[0] == 400
    status, reply = call(server, '/__comment', {'document': 'sub/note.md', 'body': 'new reply', 'anchor': {'kind': 'page'}, 'parent_id': old['id']})
    assert status == 201 and reply['comment']['document'] == 'sub/note.md'
    assert call(server, '/__comments?concept=&document=sub/note.md')[0] == 400
    assert call(server, '/__comment', {'concept': 'sub/note', 'document': None, 'body': 'bad'})[0] == 400
    for reserved in ('index', 'log', 'sub/index', 'sub/log'):
        assert call(server, '/__comment', {'concept': reserved, 'body': 'not a concept'})[0] == 400


@pytest.mark.parametrize('mode', ['read', 'comment', 'edit'])
def test_dedicated_public_capabilities(live, mode):
    server, studio, root = live
    public = _start_public_ingress(server, allow_public_edit=mode == 'edit', allow_public_comment=mode == 'comment')
    server.tunnel_url = 'https://review.example.test'
    token = (getattr(server, 'public_comment_token', '') if mode == 'comment' else
             getattr(server, 'public_mutation_token', '')) or ''
    headers = {'Host': 'review.example.test', 'Origin': 'https://review.example.test'}
    body = {'document': 'log.md', 'body': 'Public comment'}
    status, result = call(public, '/__comment', body, token=token, headers=headers)
    assert status == (403 if mode == 'read' else 201), result
    if mode != 'read':
        assert call(public, '/__comment', {**body, 'parent_id': result['comment']['id']}, token=token, headers=headers)[0] == 201
    forbidden = ['/__presence', '/__tunnel', '/__preview', '/__proposal/v1/proposals', '/__unknown']
    if mode != 'edit':
        forbidden += ['/__apply', '/__undo', '/__comment-update', '/__comment-reanchor']
    for route in forbidden:
        assert call(public, route, {}, token=token, headers=headers)[0] == 403
    assert call(public, '/__comment', body, headers={**headers, 'CF-Connecting-IP': '127.0.0.1', 'X-Forwarded-For': '127.0.0.1'})[0] == 403
    if token:
        assert call(public, '/__comment', body, token=token, headers={**headers, 'Origin': 'https://evil.test'})[0] == 403
        assert call(server, '/__comment', body, token=token)[0] == 403
    status, page = call(public, '/log.md', headers=headers)
    assert status == 200
    assert 'local-secret' not in page
    assert '"authority_mode": "public-' + mode + '"' in page
    audit = (studio.session_dir / 'public-denials.json').read_text()
    assert len(json.loads(audit)) >= len(forbidden)
    assert 'local-secret' not in audit and (not token or token not in audit)
    assert 'review.example.test' not in audit
    assert call(public, '/.okf-loom/session/public-denials.json', headers=headers)[0] == 404


def test_reserved_page_cli_loop_and_unknown_fields(live):
    server, studio, root = live
    status, payload = call(server, '/__comment', {'document': 'log.md', 'body': 'Review the log', 'detail': {'future': {'keep': [1, 2]}}})
    assert status == 201
    cid = payload['comment']['id']
    command = Path(__file__).resolve().parents[1] / 'scripts/okf-loom'
    def cli(*args):
        return subprocess.run([str(command), *args], capture_output=True, text=True, timeout=15)
    result = cli('wait', str(root), '--for', 'comment', '--timeout', '1')
    assert result.returncode == 0 and 'log.md' in result.stdout
    listed = cli('comment-list', str(root), '--document', 'log.md', '--format', 'json')
    assert listed.returncode == 0 and json.loads(listed.stdout)[0]['id'] == cid
    assert cli('comment-claim', str(root), cid, '--actor', 'agent', '--no-presence').returncode == 0
    reply = cli('comment-reply', str(root), cid, '--body', 'Clarification', '--format', 'json')
    assert reply.returncode == 0, reply.stderr
    assert json.loads(reply.stdout)['document'] == 'log.md'
    assert cli('comment-resolve', str(root), cid, '--summary', 'Reviewed').returncode == 0
    assert studio.get_comment(cid)['detail'] == {'future': {'keep': [1, 2]}}
    assert cli('wait', str(root), '--for', 'comment', '--timeout', '1').returncode == 1


def test_public_comment_bounds_actor_and_generation_revocation(live):
    server, studio, _ = live
    public = _start_public_ingress(server, allow_public_edit=False, allow_public_comment=True)
    server.tunnel_url = 'https://review.example.test'
    old = server.public_comment_token
    hdr = {'Host': 'review.example.test'}
    for extra in ({'actor': 'agent'}, {'state': 'claimed'}, {'body': 'x' * 16385}, {'concept': 'sub/note'}):
        assert call(public, '/__comment', {'document': 'log.md', 'body': 'x', **extra}, token=old, headers=hdr)[0] == 400
    # Header-only oversized admission must reject without waiting for the body.
    assert call(public, '/__comment', token=old,
                headers={**hdr, 'Content-Length': str(1024 * 1024 + 1)}, method='POST')[0] == 413
    for method in ('PUT', 'PATCH', 'DELETE'):
        assert call(public, '/__comment', {}, token=old, headers=hdr, method=method)[0] == 403
    assert studio.list_comments() == []
    _stop_public_ingress(server)
    assert server.public_comment_token is None
    public = _start_public_ingress(server, allow_public_edit=False, allow_public_comment=True)
    assert server.public_comment_token != old
    assert call(public, '/__comment', {'document': 'log.md', 'body': 'x'}, token=old, headers=hdr)[0] == 403


def test_page_symlink_swap_and_bounded_read(live, tmp_path, monkeypatch):
    server, studio, root = live
    outside = tmp_path.parent / (tmp_path.name + '-private.md')
    outside.write_text('private sentinel')
    (root / 'log.md').unlink()
    (root / 'log.md').symlink_to(outside)
    assert call(server, '/__data/page?document=log.md')[0] == 404
    assert call(server, '/__comment', {'document': 'log.md', 'body': 'x'})[0] == 503
    assert studio.list_comments() == []
    (root / 'log.md').unlink()
    (root / 'log.md').write_text('x' * 32)
    monkeypatch.setattr('scripts.okf_loom.server.MAX_RAW_RESPONSE_BYTES', 16)
    assert call(server, '/__data/page?document=log.md')[0] == 404
    outside.unlink()


def test_crlf_projection_and_exact_anchor_use_canonical_source(live):
    from scripts.okf_loom.annotations import capture_anchor
    server, studio, root = live
    source = '---\r\ntype: Note\r\n---\r\n\r\n# Heading\r\n\r\nExact 😀 text\r\n'
    (root / 'sub/note.md').write_bytes(source.encode())
    status, document = call(server, '/__data/doc?id=sub/note')
    assert status == 200, document
    assert document['annotation_projection']['complete'] is True
    start = source.index('Exact')
    anchor = capture_anchor(source, start, start + len('Exact 😀 text'))
    status, payload = call(server, '/__comment', {'concept': 'sub/note', 'body': 'precise', 'anchor': anchor})
    assert status == 201, payload
    assert studio.get_comment(payload['comment']['id'])['anchor'] == anchor


@pytest.mark.parametrize('fault', ['after_prepare', 'after_append', 'after_notification_pending', 'after_notification_reservation'])
def test_reserved_create_wal_recovery_preserves_document(live, fault):
    _, studio, root = live
    request = {'document': 'log.md', 'body': 'ask', 'anchor': {'kind': 'page'}}
    kwargs = dict(request=request, idempotency_key='reserved-recovery-0001', concept='',
                  document='log.md', body='ask', anchor={'kind': 'page'}, actor='user', detail={})
    with pytest.raises(OSError):
        studio.post_comment_idempotent(**kwargs, fault=fault)
    fresh = Studio.for_bundle(root)
    fresh.ensure_session()
    row, _ = fresh.post_comment_idempotent(**kwargs)
    assert row['document'] == 'log.md'
    assert len(fresh.list_comments()) == 1
    events = [e for e in fresh.read_events() if e.get('type') == 'comment']
    assert len(events) == 1 and events[0]['document'] == 'log.md'


@pytest.mark.parametrize('flags', [[], ['--tunnel', '--no-edit'], ['--tunnel', '--allow-tunnel-edit']])
def test_comment_share_cli_rejects_invalid_combinations_without_serving(live, flags):
    _, _, root = live
    command = Path(__file__).resolve().parents[1] / 'scripts/okf-loom'
    result = subprocess.run([str(command), 'serve', str(root), '--no-open', '--allow-tunnel-comment', *flags], capture_output=True, timeout=10)
    assert result.returncode == 2
