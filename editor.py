import argparse
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from scene import (
    DEFAULT_HUMAN_BODY, component_catalog, frame_count, human_capsules, load_scene, prop_boxes,
    sample_scene, validate_shot_set,
)
from spikemark import apply_edit, compile_scene, control_package, preview_image, scene_baseline


def snapshot(document, depth, ids, geometry, frame=0, *, include_png=True):
    sampled = sample_scene(document, frame)
    human_parts, prop_parts = {}, {}
    for entity in sampled['scene']['entities']:
        if entity['kind'] == 'human':
            human_parts[entity['id']] = human_capsules(entity['body'], entity.get('joints'))
        elif entity['kind'] == 'prop':
            prop_parts[entity['id']] = prop_boxes(entity)
    result = dict(document=document, sampled_document=sampled, frame=frame, frame_count=frame_count(document),
                  baseline=scene_baseline(document), geometry=geometry, human_parts=human_parts, prop_parts=prop_parts,
                  default_human_body=DEFAULT_HUMAN_BODY, default_human_parts=human_capsules(DEFAULT_HUMAN_BODY))
    if include_png:
        buffer = io.BytesIO()
        preview_image(depth, ids, geometry).save(buffer, format='PNG')
        result['preview_png'] = base64.b64encode(buffer.getvalue()).decode('ascii')
    return result


def preview_snapshot(document, frame=0, *, include_png=True):
    return snapshot(document, *compile_scene(document, frame=frame), frame, include_png=include_png)


def edit_snapshot(document, operations, frame=0, *, include_png=True):
    return snapshot(*apply_edit(document, operations, frame=frame), frame, include_png=include_png)


def prepare_shots(shot_set):
    """Validate every shot and check frame-zero preview without changing editor state."""
    validate_shot_set(shot_set)
    summaries = []
    for index, document in enumerate(shot_set['shots']):
        try:
            compile_scene(document, frame=0)
            baseline = scene_baseline(document)
        except ValueError as exc:
            raise ValueError(f'shot_set.shots[{index}]: {exc}') from exc
        summaries.append({'id': document['shot']['id'], 'baseline': baseline})
    return {'shot_set': shot_set, 'summaries': summaries}


def _proposal_changes(before, after):
    old_entities = {entity['id']: entity for entity in before['scene']['entities']}
    new_entities = {entity['id']: entity for entity in after['scene']['entities']}
    changes = []
    for entity_id in dict.fromkeys([*old_entities, *new_entities]):
        old, new = old_entities.get(entity_id), new_entities.get(entity_id)
        if old is None or new is None:
            changes.append({'type': 'added' if old is None else 'removed', 'entity': entity_id})
        elif old != new:
            fields = sorted(key for key in old.keys() | new.keys() if old.get(key) != new.get(key))
            change = {'type': 'updated', 'entity': entity_id, 'fields': fields}
            if 'keyframes' in fields:
                old_frames = {item['frame']: item for item in old.get('keyframes', [])}
                new_frames = {item['frame']: item for item in new.get('keyframes', [])}
                change['frames'] = sorted(frame for frame in old_frames.keys() | new_frames.keys()
                                          if old_frames.get(frame) != new_frames.get(frame))
            changes.append(change)
    old_order = [entity_id for entity_id in old_entities if entity_id in new_entities]
    new_order = [entity_id for entity_id in new_entities if entity_id in old_entities]
    if old_order != new_order:
        changes.append({'type': 'updated', 'target': 'entity_order'})
    for field in ('camera', 'ground'):
        old = before['camera'] if field == 'camera' else before['scene']['ground']
        new = after['camera'] if field == 'camera' else after['scene']['ground']
        if old != new:
            changes.append({'type': 'updated', 'target': field})
    if before['shot'].get('action_text') != after['shot'].get('action_text'):
        changes.append({'type': 'updated', 'target': 'shot', 'fields': ['action_text']})
    return changes


def review_proposal(document, proposal, frame=0, *, include_png=True):
    """校验基线并生成前后快照，不提交编辑。"""
    if not isinstance(proposal, dict) or set(proposal) != {'version', 'baseline', 'operations'}:
        raise ValueError('proposal: 需要 version、baseline 和 operations')
    if proposal['version'] != '1':
        raise ValueError("proposal.version: 需要 '1'")
    if not isinstance(proposal['baseline'], str) or proposal['baseline'] != scene_baseline(document):
        raise ValueError('proposal.baseline: 基线已过期或无效')
    if not isinstance(proposal['operations'], list):
        raise ValueError('proposal.operations: 需要操作数组')
    after = edit_snapshot(document, proposal['operations'], frame, include_png=include_png)
    before = preview_snapshot(document, frame, include_png=include_png)
    return dict(proposal=proposal, before=before, after=after,
                changes=_proposal_changes(document, after['document']))


def make_proposal(document, operations, frame=0, *, include_png=True):
    proposal = {'version': '1', 'baseline': scene_baseline(document), 'operations': operations}
    return review_proposal(document, proposal, frame, include_png=include_png)


def make_handler(initial, page, assets):
    class EditorHandler(BaseHTTPRequestHandler):
        def send_bytes(self, code, content, content_type):
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(content)

        def send_json(self, code, data):
            self.send_bytes(code, json.dumps(data, ensure_ascii=True, allow_nan=False).encode('utf-8'),
                            'application/json; charset=utf-8')

        def allowed_host(self):
            if self.headers.get('Host') == f'127.0.0.1:{self.server.server_port}':
                return True
            self.send_json(403, {'error': '请通过 127.0.0.1 打开编辑器'})
            return False

        def do_GET(self):
            if not self.allowed_host():
                return
            if self.path == '/':
                self.send_bytes(200, page, 'text/html; charset=utf-8')
            elif self.path == '/api/scene':
                self.send_json(200, initial)
            elif self.path == '/api/components':
                self.send_json(200, component_catalog())
            elif self.path in assets:
                self.send_bytes(200, assets[self.path], 'text/javascript; charset=utf-8')
            else:
                self.send_json(404, {'error': '未找到页面'})

        def do_POST(self):
            if not self.allowed_host():
                return
            if self.path not in ('/api/edit', '/api/preview', '/api/export', '/api/proposal', '/api/shot-set'):
                self.send_json(404, {'error': '未找到接口'})
                return
            origin = self.headers.get('Origin')
            if origin is not None and origin != f'http://127.0.0.1:{self.server.server_port}':
                self.send_json(403, {'error': '只接受编辑器页面的编辑请求'})
                return
            if self.headers.get_content_type() != 'application/json':
                self.send_json(415, {'error': '需要 application/json'})
                return
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= (12 if self.path == '/api/shot-set' else 1) * 512 * 1024:
                    raise ValueError('请求大小超出限制')
                request = json.loads(self.rfile.read(length).decode('utf-8'))
                if self.path == '/api/shot-set':
                    if not isinstance(request, dict) or set(request) != {'shot_set'}:
                        raise ValueError('请求需要且只接受 shot_set')
                    result = prepare_shots(request['shot_set'])
                else:
                    if not isinstance(request, dict) or 'document' not in request:
                        raise ValueError('请求需要 document')
                    doc = request['document']
                    frame = request.get('frame', 0)
                    fields = set(request) - {'frame'}
                    if self.path == '/api/edit' and fields == {'document', 'operations'}:
                        result = edit_snapshot(doc, request['operations'], frame)
                    elif self.path == '/api/preview' and fields == {'document'}:
                        result = preview_snapshot(doc, frame)
                    elif self.path == '/api/proposal' and fields == {'document', 'proposal'}:
                        result = review_proposal(doc, request['proposal'], frame)
                    elif self.path == '/api/export' and set(request) <= {'document', 'vace_window'}:
                        archive = control_package(doc, request.get('vace_window'))
                        self.send_bytes(200, archive, 'application/zip')
                        return
                    else:
                        raise ValueError('请求字段不符合接口约定')
            except (ValueError, RecursionError) as exc:
                self.send_json(400, {'error': str(exc)})
                return
            self.send_json(200, result)

    return EditorHandler


def main():
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='启动 SpikeMark 本地场景编辑器')
    parser.add_argument('--scene', type=Path, default=root/'scene.json')
    parser.add_argument('--port', type=int, default=8766)
    parser.add_argument('--open', action='store_true', help='启动后打开默认浏览器')
    args = parser.parse_args()
    doc = load_scene(args.scene)
    initial = preview_snapshot(doc)
    assets = {'/three.min.js': (root/'vendor'/'three.min.js').read_bytes(),
              '/editor3d.js': (root/'editor3d.js').read_bytes()}
    with HTTPServer(('127.0.0.1', args.port), make_handler(initial, (root/'editor.html').read_bytes(), assets)) as server:
        url = f'http://127.0.0.1:{server.server_port}/'
        print(url, flush=True)
        if args.open:
            import webbrowser
            webbrowser.open(url)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == '__main__':
    main()
