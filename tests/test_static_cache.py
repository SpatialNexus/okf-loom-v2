"""HTTP validators preserve static bytes, live edits and ingress boundaries."""
from __future__ import annotations

import hashlib
import http.client
import os
import threading

import pytest

from okf_loom import Bundle
from okf_loom.server import _start_public_ingress
from okf_loom.viewer import assets, vendor
from test_server_smoke import _start_server
from test_tunnel import _app_server


SECURITY_HEADERS = (
    'Content-Security-Policy', 'X-Content-Type-Options', 'X-Frame-Options',
    'Referrer-Policy', 'X-XSS-Protection',
)


def _request(server, path, *, method='GET', validators=()):
    conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=20)
    try:
        conn.putrequest(method, '/__static/' + path)
        for value in validators:
            conn.putheader('If-None-Match', value)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


@pytest.fixture
def static_server(tmp_path, monkeypatch):
    root = tmp_path / 'static'
    root.mkdir()
    (root / 'test.js').write_bytes(b'const version = 1;\r\n')
    monkeypatch.setattr(assets, '_BUILTIN_STATIC', root)
    bundle = tmp_path / 'bundle'
    bundle.mkdir()
    (bundle / 'index.md').write_text('# Static cache\n')
    server, thread = _start_server(Bundle.load(bundle), port=0)
    try:
        yield server, root
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _assert_revalidation(server, path):
    status, headers, body = _request(server, path)
    assert status == 200 and body
    tag = headers['ETag']
    assert tag == '"' + hashlib.sha256(body).hexdigest() + '"'
    assert headers['Cache-Control'] == 'no-cache'
    for method in ('GET', 'HEAD'):
        for values in ((tag,), ('W/' + tag,), ('"other", W/' + tag,), ('*',),
                       ('"other"', tag), ('  "other" , ' + tag + '  ',)):
            code, fresh, empty = _request(server, path, method=method, validators=values)
            assert code == 304 and empty == b''
            assert fresh['ETag'] == tag and fresh['Cache-Control'] == 'no-cache'
            assert 'Content-Length' not in fresh
            for key in SECURITY_HEADERS:
                assert fresh[key] == headers[key]
        code, fresh, received = _request(server, path, method=method, validators=('"other"',))
        assert code == 200 and fresh['ETag'] == tag
        assert int(fresh['Content-Length']) == len(body)
        assert received == (body if method == 'GET' else b'')
    for value in ('"other,*,tag"', 'W/"other,*,tag"', '"unclosed', tag.strip('"')):
        assert _request(server, path, validators=(value,))[0] == 200
    assert _request(server, path, validators=('"other,tag", ' + tag,))[0] == 304
    return headers, body


@pytest.mark.parametrize('fields, expected', [
    (['"other"', '"current"'], True),
    (['"other,*,tag"'], False),
    (['"other,tag", "current"'], True),
    (['*'], True),
    (['W/"current"'], True),
    ([], False),
])
def test_shared_if_none_match_parser(fields, expected):
    from okf_loom.server import _if_none_match_matches
    assert _if_none_match_matches(fields, '"current"') is expected


def test_static_conditional_get_head_and_final_text_bytes(static_server):
    server, _ = static_server
    _, body = _assert_revalidation(server, 'test.js')
    # The text loader normalizes newlines; the tag identifies served bytes,
    # not the raw file bytes (nor a path/mtime/size approximation).
    assert body == b'const version = 1;\n'


def test_static_same_size_metadata_preserving_edit_is_fresh(static_server):
    server, root = static_server
    _, first, _ = _request(server, 'test.js')
    path = root / 'test.js'
    before = path.stat()
    path.write_bytes(b'const version = 2;\r\n')
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    code, after, body = _request(server, 'test.js', validators=(first['ETag'],))
    assert code == 200 and body == b'const version = 2;\n'
    assert after['ETag'] != first['ETag']
    _assert_revalidation(server, 'test.js')
    path.unlink()
    assert _request(server, 'test.js', validators=(after['ETag'],))[0] == 404


def test_static_generated_css_uses_final_bytes(static_server, monkeypatch):
    server, _ = static_server
    monkeypatch.setitem(assets._GENERATED_STATIC, 'generated.css', lambda: 'a { color: red }')
    first, _ = _assert_revalidation(server, 'generated.css')
    monkeypatch.setitem(assets._GENERATED_STATIC, 'generated.css', lambda: 'a { color: tan }')
    code, after, body = _request(server, 'generated.css', validators=(first['ETag'],))
    assert code == 200 and body == b'a { color: tan }'
    assert after['ETag'] != first['ETag']


def test_static_overrides_keep_gate_containment_and_byte_identity(static_server, monkeypatch):
    server, root = static_server
    override = server.bundle.root / '.okf-loom/viewer/static/test.js'
    override.parent.mkdir(parents=True)
    override.write_bytes(b'override\r\n')
    monkeypatch.setattr(assets, '_overrides_allowed', lambda bundle: False)
    _, builtin, _ = _request(server, 'test.js')
    monkeypatch.setattr(assets, '_overrides_allowed', lambda bundle: True)
    code, headers, body = _request(server, 'test.js', validators=(builtin['ETag'],))
    assert code == 200 and body == b'override\r\n'
    assert headers['ETag'] != builtin['ETag']
    _assert_revalidation(server, 'test.js')
    override.unlink()
    override.symlink_to(root / 'test.js')  # outside bundle, even though bytes are public
    code, headers, body = _request(server, 'test.js', validators=('*',))
    assert code == 404 and body == b'Not found' and 'ETag' not in headers


@pytest.mark.parametrize('path, expected', [
    ('missing.js', 404), ('nested/test.js', 404),
    ('../secret', 400), ('%2e%2e/secret', 400),
    ('ledger-public-sans/missing.woff2', 404), ('ledger-azeret-mono/missing.woff2', 404),
    ('vendor/cytoscape/cytoscape.min.js', 404), ('vendor/../secret', 400),
])
def test_static_unknown_disallowed_paths_ignore_validators(static_server, path, expected):
    server, _ = static_server
    plain = _request(server, path)
    conditional = _request(server, path, validators=('*',))
    assert plain[0] == conditional[0] == expected
    assert plain[2] == conditional[2]
    assert 'ETag' not in conditional[1]


def test_static_fonts_and_generated_css_local_public_parity(tmp_path):
    (tmp_path / 'a.md').write_text('---\ntype: note\n---\nBody\n')
    server = _app_server(tmp_path)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    ingress = _start_public_ingress(server, allow_public_edit=False)
    try:
        for path in ('theme.js', 'ledger-fonts.css', *(face[2] for face in vendor.LEDGER_FACES)):
            local, body = _assert_revalidation(server, path)
            public, public_body = _assert_revalidation(ingress, path)
            assert body == public_body and local['ETag'] == public['ETag']
            for key in SECURITY_HEADERS:
                assert local[key] == public[key]
        for path in ('missing.js', 'vendor/cytoscape/cytoscape.min.js'):
            assert _request(server, path, validators=('*',))[0] == 404
            assert _request(ingress, path, validators=('*',))[0] == 404
    finally:
        ingress.shutdown()
        ingress.server_close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        server.studio.close()
