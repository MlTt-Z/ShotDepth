import argparse
import base64
import io
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from scene import (
    DEFAULT_HUMAN_BODY, MAX_DYNAMIC_FRAMES, component_catalog, frame_count, human_capsules, load_scene,
    prop_boxes, sample_scene, validate_scene, validate_shot_set,
)
from spikemark import apply_edit, compile_scene, control_package, edit_document, preview_image, scene_baseline


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


LIVE_PREVIEW_MAX_EDGE = 320


def live_preview(request):
    """Render one reduced frame without committing edits or changing the playback source."""
    required = {'document', 'operations', 'frame'}
    if not isinstance(request, dict) or not required <= request.keys() <= required | {'pose'}:
        raise ValueError('实时预览需要 document、operations、frame，可选 pose')
    operations = request['operations']
    if not isinstance(operations, list):
        raise ValueError('operations: 需要操作数组')
    if 'pose' in request:
        pose = request['pose']
        if not isinstance(pose, dict) or set(pose) != {'position', 'target'}:
            raise ValueError('pose: 需要 position 和 target')
    else:
        pose = None

    document = request['document']
    # Check the original dimensions and the edited camera before either can be replaced.
    document = edit_document(document, operations) if operations else validate_scene(document)
    width, height = document['resolution']
    scale = min(1, LIVE_PREVIEW_MAX_EDGE / max(width, height))
    reduced = {**document, 'resolution': [max(1, round(width * scale)), max(1, round(height * scale))]}
    if pose is not None:
        reduced['camera'] = {**document['camera'], 'pos': pose['position'], 'look_at': pose['target']}
    depth, ids, geometry = compile_scene(reduced, frame=request['frame'])
    buffer = io.BytesIO()
    preview_image(depth, ids, geometry).save(buffer, format='PNG')
    return {'preview_png': base64.b64encode(buffer.getvalue()).decode('ascii'), 'geometry': geometry}


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


PLAYBACK_MAX_BYTES = 2 * 1024 * 1024


def _playback_json(value):
    return json.dumps(value, allow_nan=False, separators=(',', ':')).encode('utf-8')


def _playback_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('播放请求不能包含重复的 JSON 字段')
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError(f'播放请求不接受 JSON 数值 {value}')


def _playback_page(document, baseline, start):
    count = frame_count(document)
    if not 0 <= start < count:
        raise ValueError(f'start_frame: 需要 [0, {count - 1}] 范围内的整数帧')
    animated = [index for index, entity in enumerate(document['scene']['entities']) if entity.get('keyframes')]
    prefix = _playback_json({'baseline': baseline, 'frame_count': count})[:-1] + b',"next_frame":'
    # Reserve enough bytes for the longest possible next_frame and the frames array wrapper.
    size = len(prefix) + max(len(str(count)), len('null')) + len(b',"frames":[]}')
    frames = []
    for frame in range(start, min(start + MAX_DYNAMIC_FRAMES, count)):
        transforms, instances = [], []
        if animated:
            entities = sample_scene(document, frame)['scene']['entities']
            for index in animated:
                entity = entities[index]
                transforms.append((index, entity['transform']))
                instances.append((index, human_capsules(entity['body'], entity.get('joints'))))
        entry = _playback_json({'frame': frame, 'transforms': transforms, 'instances': instances})
        size += len(entry) + bool(frames)
        if size > PLAYBACK_MAX_BYTES:
            break
        frames.append(entry)
    if not frames:
        raise ValueError('单个播放帧超出响应大小限制')
    next_frame = start + len(frames)
    return (prefix + _playback_json(next_frame if next_frame < count else None) +
            b',"frames":[' + b','.join(frames) + b']}')


def make_handler(initial, page, assets):
    # Only one validated scene is retained. A cache miss can restart from document.
    playback_source = (initial['baseline'], initial['document'])

    class EditorHandler(BaseHTTPRequestHandler):
        def playback(self, request):
            nonlocal playback_source
            if not isinstance(request, dict) or set(request) not in (
                {'document', 'start_frame'}, {'baseline', 'start_frame'},
            ):
                raise ValueError('播放请求需要 start_frame 和 document 或 baseline')
            start = request['start_frame']
            if type(start) is not int or start < 0:
                raise ValueError('start_frame: 需要非负整数帧')
            cached_baseline, cached_doc = playback_source
            if 'baseline' in request:
                baseline = request['baseline']
                if (not isinstance(baseline, str) or len(baseline) != 64 or
                        any(c not in '0123456789abcdef' for c in baseline)):
                    raise ValueError('baseline: 需要 64 位小写十六进制摘要')
                if baseline != cached_baseline:
                    self.send_json(409, {'error': '播放基线已失效，请携带 document 重试'})
                    return
                doc = cached_doc
            else:
                doc = request['document']
                baseline = scene_baseline(doc)
                if baseline == cached_baseline:
                    doc = cached_doc
                else:
                    validate_scene(doc)
            content = _playback_page(doc, baseline, start)
            playback_source = baseline, doc
            self.send_bytes(200, content, 'application/json; charset=utf-8')

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
            nonlocal playback_source
            if not self.allowed_host():
                return
            if self.path not in ('/api/edit', '/api/preview', '/api/export', '/api/proposal', '/api/shot-set',
                                 '/api/playback', '/api/live-preview'):
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
                if self.path == '/api/playback':
                    if len(self.headers.get_all('Content-Length', [])) != 1 or self.headers.get('Transfer-Encoding') is not None:
                        raise ValueError('播放请求需要且只接受 Content-Length 定界')
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError('播放请求长度不符')
                    request = json.loads(body.decode('utf-8'), object_pairs_hook=_playback_object,
                                         parse_constant=_reject_json_constant)
                    self.playback(request)
                    return
                request = json.loads(self.rfile.read(length).decode('utf-8'))
                if self.path == '/api/live-preview':
                    result = live_preview(request)
                elif self.path == '/api/shot-set':
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
            if self.path in ('/api/edit', '/api/preview'):
                playback_source = result['baseline'], result['document']
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
