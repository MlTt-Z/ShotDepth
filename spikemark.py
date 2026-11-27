import argparse
import json
import math
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import numpy as np
from PIL import Image

import scene
from scene import apply_edit as edit_document, human_capsules, load_edits, load_scene, validate_scene


COLORS = [(235, 130, 82), (83, 169, 224), (132, 199, 117), (191, 134, 219)]
MAX_DYNAMIC_PIXELS = 32_000_000


def rotation_y(yaw):
    c, s = np.cos(np.deg2rad(yaw)), np.sin(np.deg2rad(yaw))
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]])


def camera_parameters(camera, size):
    w, h = size
    origin = np.asarray(camera['pos'], dtype=float)
    forward = np.asarray(camera['look_at'], dtype=float) - origin
    forward /= np.linalg.norm(forward)
    right = np.cross([0., 1., 0.], forward)
    right /= np.linalg.norm(right)
    up = np.cross(forward, right)
    c, s = np.cos(np.deg2rad(camera.get('roll', 0))), np.sin(np.deg2rad(camera.get('roll', 0)))
    # 右下前相机坐标，像素中心为 (x+0.5, y+0.5)。
    axes = np.stack([c*right+s*up, s*right-c*up, forward])
    focal = w * camera['lens_mm'] / camera['sensor_mm']
    k = np.array([[focal, 0, w/2], [0, focal, h/2], [0, 0, 1.]])
    return origin, axes, k


def capsule_hit(origin, rays, ray_norm2, a, b, r, near, far):
    ab = b-a
    length = np.linalg.norm(ab)
    unit = ab/length if length else np.zeros(3)
    offset = origin-a
    du, ou = rays@unit, offset@unit

    def cap(center, at_end):
        delta = origin-center
        dot = rays @ delta
        disc = dot*dot-ray_norm2*(delta@delta-r*r)
        root = np.sqrt(np.maximum(0, disc))
        hit = np.full(ray_norm2.shape, np.inf)
        for sign in (-1, 1):
            t = (-dot+sign*root)/ray_norm2
            axial = ou+t*du
            hemisphere = axial >= length if at_end else axial <= 0
            valid = (disc >= 0) & hemisphere & (t >= near) & (t <= far)
            np.minimum(hit, np.where(valid, t, np.inf), out=hit)
        return hit

    hit = cap(a, False)
    if not length:
        return hit
    np.minimum(hit, cap(b, True), out=hit)
    coeff = ray_norm2-du*du
    denom = np.where(coeff > 1e-12, coeff, 1.)
    dot = rays@offset-du*ou
    disc = dot*dot-coeff*(offset@offset-ou*ou-r*r)
    root = np.sqrt(np.maximum(0, disc))
    for sign in (-1, 1):
        t = (-dot+sign*root)/denom
        axial = ou+t*du
        valid = (coeff > 1e-12) & (disc >= 0) & (axial >= 0) & (axial <= length)
        hit = np.minimum(hit, np.where(valid & (t >= near) & (t <= far), t, np.inf))
    return hit


def box_hit(origin, rays, low, high, near, far):
    parallel = np.abs(rays) < 1e-12
    safe = np.where(parallel, 1., rays)
    a, b = (low-origin)/safe, (high-origin)/safe
    t0 = np.max(np.where(parallel, -np.inf, np.minimum(a, b)), axis=-1)
    t1 = np.min(np.where(parallel, np.inf, np.maximum(a, b)), axis=-1)
    t = np.where(t0 >= near, t0, t1)
    miss = np.any(parallel & ((origin < low) | (origin > high)), axis=-1)
    return np.where(~miss & (t1 >= t0) & (t >= near) & (t <= far), t, np.inf)


def corners(low, high):
    return np.array([[x, y, z] for x in (low[0], high[0]) for y in (low[1], high[1]) for z in (low[2], high[2])])


def image_region(vertices, origin, axes, k, near, far, w, h):
    q = (vertices-origin) @ axes.T
    if q[:, 2].max() < near or q[:, 2].min() > far:
        return (slice(0, 0), slice(0, 0)), True
    if q[:, 2].min() < near:
        return (slice(0, h), slice(0, w)), True
    pixels = q[:, :2]/q[:, 2, None]*[k[0, 0], k[1, 1]] + k[:2, 2]
    low, high = pixels.min(0), pixels.max(0)
    a, b = map(int, np.floor(np.clip(low, 0, [w, h])))
    c, d = map(int, np.ceil(np.clip(high, 0, [w, h])))
    clipped = np.any(low < 0) or np.any(high > [w, h]) or q[:, 2].max() > far
    return (slice(b, d), slice(a, c)), bool(clipped)


def bbox(mask, region, w, h):
    y, x = np.nonzero(mask)
    if not x.size:
        return None
    bounds = [int(x.min())+region[1].start, int(y.min())+region[0].start,
              int(x.max())+1+region[1].start, int(y.max())+1+region[0].start]
    return {'px': bounds, 'norm': [v/s for v, s in zip(bounds, [w, h, w, h])]}


def _has_keyframes(doc):
    return any(entity.get('keyframes') for entity in doc['scene']['entities'])


def _is_animated(doc):
    # 首尾之外保持端点；端点之间插值。缺省关节与显式 0 等效。
    def pose(key):
        joints = {joint: angle for joint, angle in key['joints'].items() if angle}
        return key['pos'], key['yaw'], joints

    for entity in doc['scene']['entities']:
        track = entity.get('keyframes')
        if track:
            first = pose(track[0])
            if any(pose(key) != first for key in track[1:]):
                return True
    return False


def compile_scene(doc, frame=0):
    """编译从零开始编号的指定镜头帧。"""
    validate_scene(doc)
    return _compile_frame(doc, frame, _has_keyframes(doc))


def _compile_frame(doc, frame, dynamic):
    try:
        if dynamic:
            snapshot = scene.sample_scene(doc, frame)
            validate_scene(snapshot)
        else:
            if type(frame) is not int or not 0 <= frame < scene.frame_count(doc):
                raise ValueError('frame: 必须是镜头范围内的整数')
            snapshot = doc
        depth, ids, geometry = _compile_snapshot(snapshot)
    except ValueError as exc:
        raise ValueError(f'第 {frame} 帧 (frame {frame}): {exc}') from exc
    if dynamic:
        geometry.update(frame=frame, time_s=frame/doc['shot']['fps'])
    return depth, ids, geometry


def _compile_snapshot(doc):
    w, h = doc['resolution']
    camera = doc['camera']
    near, far = camera['near'], camera['far']
    origin, axes, k = camera_parameters(camera, (w, h))
    x, y = np.meshgrid((np.arange(w)+.5-k[0, 2])/k[0, 0], (np.arange(h)+.5-k[1, 2])/k[1, 1])
    rays = np.stack([x, y, np.ones_like(x)], -1) @ axes
    ray_norm2 = 1.+x*x+y*y
    plane_x, plane_z = np.asarray(doc['scene']['ground']['size'])/2
    dy = rays[..., 1]
    floor_t = -origin[1]/np.where(np.abs(dy) > 1e-12, dy, 1.)
    ground = ((np.abs(dy) > 1e-12) & (floor_t >= near) & (floor_t <= far) &
              (np.abs(origin[0]+floor_t*rays[..., 0]) <= plane_x) &
              (np.abs(origin[2]+floor_t*rays[..., 2]) <= plane_z))
    depth = np.where(ground, floor_t, np.inf)
    ids = np.zeros((h, w), dtype=np.uint16)
    entities, diagnostics, world_bounds = [], [], []
    for number, entity in enumerate(doc['scene']['entities'], 1):
        entity_id, kind = entity['id'], entity['kind']
        transform = entity['transform']
        pos, rot = np.asarray(transform['pos']), rotation_y(transform.get('yaw', 0))
        local_origin = (origin-pos) @ rot
        if kind == 'human':
            joints = entity.get('joints')
            capsules = human_capsules(entity['body'], joints) if joints is not None else human_capsules(entity['body'])
            parts = [(np.asarray(a), np.asarray(b), r) for a, b, r in capsules]
            lows = [np.minimum(a,b)-r for a,b,r in parts]
            highs = [np.maximum(a,b)+r for a,b,r in parts]
            for a,b,r in parts:
                ab = b-a
                length2 = ab@ab
                t = np.clip((local_origin-a)@ab/length2, 0, 1) if length2 else 0.
                if np.linalg.norm(local_origin-a-t*ab) < r:
                    raise ValueError(f'camera.pos: 相机位于 {entity_id} 内部')
        else:
            if kind == 'prop':
                bounds = [(np.asarray(center), np.asarray(size)/2) for center, size in scene.prop_boxes(entity)]
                lows, highs = [c-r for c, r in bounds], [c+r for c, r in bounds]
            else:
                sx, sy, sz = entity['size']
                lows, highs = [np.array([-sx/2, 0, -sz/2])], [np.array([sx/2, sy, sz/2])]
            if any(np.all((local_origin > lo) & (local_origin < hi)) for lo, hi in zip(lows, highs)):
                raise ValueError(f'camera.pos: 相机位于 {entity_id} 内部')
        part_vertices = [corners(lo, hi) @ rot.T + pos for lo, hi in zip(lows, highs)]
        vertices = np.concatenate(part_vertices)
        low, high = vertices.min(0), vertices.max(0)
        for other, ol, oh in world_bounds:
            if np.all(np.minimum(high, oh)-np.maximum(low, ol) > 1e-6):
                diagnostics.append({'code': 'possible_intersection', 'entities': [other, entity_id]})
        world_bounds.append((entity_id, low, high))
        region, clipped = image_region(vertices, origin, axes, k, near, far, w, h)
        hit = np.full(depth[region].shape, np.inf)
        if hit.size:
            if kind == 'box':
                hit = box_hit(local_origin, rays[region] @ rot, lows[0], highs[0], near, far)
            else:
                for index, verts in enumerate(part_vertices):
                    area, _ = image_region(verts, origin, axes, k, near, far, w, h)
                    area = tuple(slice(max(s.start, t.start), min(s.stop, t.stop)) for s, t in zip(area, region))
                    if any(s.stop <= s.start for s in area):
                        continue
                    sub = tuple(slice(s.start-t.start, s.stop-t.start) for s, t in zip(area, region))
                    local = rays[area] @ rot
                    if kind == 'human':
                        a, b, radius = parts[index]
                        part_hit = capsule_hit(local_origin, local, ray_norm2[area], a, b, radius, near, far)
                    else:
                        part_hit = box_hit(local_origin, local, lows[index], highs[index], near, far)
                    np.minimum(hit[sub], part_hit, out=hit[sub])
        pixels = np.isfinite(hit)
        closer = hit < depth[region]
        depth[region][closer] = hit[closer]
        ids[region][closer] = number
        entities.append((number, entity_id, kind, region, bbox(pixels, region, w, h), int(pixels.sum()), clipped))

    details = {}
    for number, entity_id, kind, region, projected, pixels, clipped in entities:
        visible = ids[region] == number
        count = int(visible.sum())
        if not pixels:
            diagnostics.append({'code': 'out_of_view', 'entities': [entity_id]})
        elif not count:
            diagnostics.append({'code': 'fully_occluded', 'entities': [entity_id]})
        if clipped and pixels:
            diagnostics.append({'code': 'possibly_clipped', 'entities': [entity_id]})
        details[entity_id] = {'mask_id': number, 'kind': kind, 'visible': bool(count),
                              'visible_bbox': bbox(visible, region, w, h), 'projected_bbox': projected,
                              'visible_fraction': count/pixels if pixels else None}
    meta = {'version': '0.1', 'scene_id': doc['scene']['id'], 'shot': doc['shot'], 'resolution': [w,h],
            'depth': {'units': 'meter', 'axis': 'camera_z', 'invalid': 0, 'near': near, 'far': far},
            'bbox_format': 'xyxy_half_open_top_left', 'mask_background': 0,
            'camera': {'K': k.tolist(), 'R_world_to_camera': axes.tolist(), 't_world_to_camera': (-axes@origin).tolist(),
                       'camera_axes': 'right_down_forward', 'pixel_centers': 'x+0.5,y+0.5'},
            'entities': details, 'diagnostics': diagnostics}
    meta['semantics'] = describe_scene(doc, meta)
    return np.where(np.isfinite(depth), depth, 0).astype(np.float32), ids, meta


def describe_scene(doc, geometry):
    """以首个人物为主角；构图数据直接复用编译器的可见包围框。"""
    camera_pos = np.asarray(doc['camera']['pos'], dtype=float)
    subject = None
    actors = {}
    for entity in doc['scene']['entities']:
        if entity['kind'] != 'human':
            continue
        actor = entity['id']
        subject = actor if subject is None else subject
        geo = geometry['entities'][actor]
        center = np.asarray(entity['transform']['pos'], dtype=float)
        center[1] += entity['body']['height']/2
        offset = camera_pos-center
        pitch = float(np.rad2deg(np.arctan2(offset[1], np.hypot(offset[0], offset[2]))))
        box = geo['visible_bbox']
        actors[actor] = {'visible': geo['visible'], 'visible_fraction': geo['visible_fraction'],
                         'bbox_norm': box['norm'] if box else None,
                         'distance_m': float(np.linalg.norm(offset)), 'camera_elevation_deg': pitch}
    shot = {'id': doc['shot']['id'], 'subject': subject, 'camera_height_m': float(camera_pos[1]),
            'size_ratio': None, 'headroom': None, 'subject_screen': None}
    box = actors[subject]['bbox_norm'] if subject is not None else None
    if box is not None:
        l, t, r, b = box
        shot.update(size_ratio=b-t, headroom=t, subject_screen={'x': (l+r)/2, 'y': (t+b)/2})
    return {'shot': shot, 'actors': actors}


def apply_edit(doc, operations, frame=0):
    """原子应用语义操作并编译指定帧；硬错误抛出，构图诊断随结果返回。"""
    updated = edit_document(doc, operations)
    depth, ids, geometry = compile_scene(updated, frame=frame)
    return updated, depth, ids, geometry


def vace_depth(depth_m, near, far):
    if not (np.isfinite(near) and np.isfinite(far) and 0 < near < far):
        raise ValueError('VACE 深度窗口需要 0 < near < far')
    clipped = np.clip(np.asarray(depth_m, dtype=np.float64), near, far)
    return np.where(depth_m > 0, np.rint(255*((far-clipped)/(far-near))), 0).astype(np.uint8)


def _repeat_control(image, frames):
    if type(frames) is not int or not 1 <= frames <= 4096:
        raise ValueError('控制序列帧数必须是 1 到 4096 之间的整数')
    if image.size * frames * 3 > 256 * 1024**2:
        raise ValueError('控制序列超过 256 MiB，请减小帧数或分辨率')
    return np.broadcast_to(image[None, :, :, None], (frames, *image.shape, 3))


def vace_control_video(depth_m, near, far, frames):
    """将静态米制深度转换为只读的 [帧, 高, 宽, 3] uint8 视图。"""
    return _repeat_control(vace_depth(np.asarray(depth_m), near, far), frames)


def load_vace_control(output_dir, frames):
    """将已导出的灰度条件展开为只读的 [帧, 高, 宽, 3] uint8 视图。"""
    with Image.open(Path(output_dir) / 'vace_depth.png') as source:
        return _repeat_control(np.asarray(source.convert('L')), frames)


def export_scene(doc, out, window=None, frames=None):
    if frames is not None and window is None:
        raise ValueError('--vace-frames 需要同时指定 --vace-depth NEAR_M FAR_M')
    depth, ids, meta = compile_scene(doc)
    control = vace_depth(depth, *window) if window is not None else None
    sequence = _repeat_control(control, frames) if frames is not None else None
    out = Path(out)
    # 每次写入独立目录，避免留下上一次的可选通道。
    out.mkdir(parents=True, exist_ok=False)
    np.save(out/'depth_m.npy', depth, allow_pickle=False)
    Image.fromarray(ids).save(out/'id_mask.png')
    if control is not None:
        Image.fromarray(control).convert('RGB').save(out/'vace_depth.png')
        meta['vace_depth'] = {'near_m': window[0], 'far_m': window[1], 'near_is_white': True}
    if sequence is not None:
        np.save(out/'vace_control_video.npy', sequence, allow_pickle=False)
        meta['vace_depth']['frames'] = frames
    preview_image(depth, ids, meta).save(out/'preview.png')
    (out/'geometry.json').write_bytes(_json_bytes(meta))
    return meta


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)+'\n').encode('utf-8')


def scene_baseline(document):
    """对 UTF-8 紧凑、按键排序的 JSON 计算 SHA-256 十六进制摘要。"""
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)
    return sha256(canonical.encode('utf-8')).hexdigest()


def _zip_write(archive, name, data):
    entry = ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    entry.create_system = 3
    entry.external_attr = 0o100644 << 16
    archive.writestr(entry, data, compress_type=ZIP_DEFLATED, compresslevel=6)


def _zip_image(archive, name, image):
    with BytesIO() as stream:
        image.save(stream, format='PNG')
        _zip_write(archive, name, stream.getbuffer())


def _dynamic_control_package(document, vace_window):
    count = scene.frame_count(document)
    w, h = document['resolution']
    if w*h*count > MAX_DYNAMIC_PIXELS:
        raise ValueError(f'动态控制包像素超过预算：{w*h*count} > {MAX_DYNAMIC_PIXELS}')

    shot = document['shot']
    camera = document['camera']
    files = {'manifest': 'manifest.json', 'scene': 'scene.json',
             'geometry': 'frames/{frame:06d}/geometry.json', 'depth': 'frames/{frame:06d}/depth_m.npy',
             'id_mask': 'frames/{frame:06d}/id_mask.png'}
    manifest = {
        'version': '0.2', 'scene_version': document['version'], 'baseline': scene_baseline(document),
        'scene_id': document['scene']['id'], 'shot': shot, 'units': document['units'],
        'up': document['up'], 'resolution': [w, h],
        'timeline': {'duration_s': shot['duration_s'], 'fps': shot['fps'], 'frame_count': count,
                     'static_repeat': False, 'first_frame': 0},
        'depth': {'file_pattern': files['depth'], 'units': 'meter', 'axis': 'camera_z', 'invalid': 0,
                  'near': camera['near'], 'far': camera['far'], 'dtype': 'float32', 'layout': 'height_width'},
        'id_mask': {'file_pattern': files['id_mask'], 'dtype': 'uint16', 'layout': 'height_width',
                    'background_id': 0, 'zero_includes_ground': True,
                    'id_mapping': {entity['id']: index for index, entity in enumerate(document['scene']['entities'], 1)}},
        'files': files,
    }
    if vace_window is not None:
        files['vace_depth'] = 'frames/{frame:06d}/vace_depth.png'
        manifest['vace_depth'] = {'file_pattern': files['vace_depth'], 'near_m': vace_window[0],
                                  'far_m': vace_window[1], 'near_is_white': True, 'invalid': 0,
                                  'dtype': 'uint8', 'layout': 'height_width_rgb'}

    with BytesIO() as buffer:
        with ZipFile(buffer, 'w') as archive:
            _zip_write(archive, files['manifest'], _json_bytes(manifest))
            _zip_write(archive, files['scene'], _json_bytes(document))
            for frame in range(count):
                depth, ids, geometry = _compile_frame(document, frame, dynamic=True)
                try:
                    with BytesIO() as stream:
                        np.save(stream, depth, allow_pickle=False)
                        _zip_write(archive, files['depth'].format(frame=frame), stream.getbuffer())
                    _zip_image(archive, files['id_mask'].format(frame=frame), Image.fromarray(ids))
                    if vace_window is not None:
                        control = vace_depth(depth, *vace_window)
                        _zip_image(archive, files['vace_depth'].format(frame=frame), Image.fromarray(control).convert('RGB'))
                        geometry['vace_depth'] = {'near_m': vace_window[0], 'far_m': vace_window[1], 'near_is_white': True}
                    _zip_write(archive, files['geometry'].format(frame=frame), _json_bytes(geometry))
                except (ValueError, OSError) as exc:
                    raise ValueError(f'第 {frame} 帧 (frame {frame}): {exc}') from exc
        return buffer.getvalue()


def control_package(document, vace_window=None) -> bytes:
    """生成完整的控制 ZIP；动态镜头逐帧编译，失败时不交付部分结果。"""
    if vace_window is not None:
        error = 'vace_window: 必须是 null 或两个有限的正数 [near_m, far_m]，且 near_m < far_m'
        if (not isinstance(vace_window, (list, tuple)) or len(vace_window) != 2
                or any(type(value) not in (int, float) for value in vace_window)):
            raise ValueError(error)
        try:
            near, far = map(float, vace_window)
        except OverflowError as exc:
            raise ValueError(error) from exc
        if not (math.isfinite(near) and math.isfinite(far) and 0 < near < far):
            raise ValueError(error)
        vace_window = (near, far)

    validate_scene(document)
    if _is_animated(document):
        return _dynamic_control_package(document, vace_window)

    # 恒定轨道也可能覆盖基础姿态；只用首帧快照渲染，归档仍保留原文档。
    snapshot = scene.sample_scene(document, 0) if _has_keyframes(document) else document
    files = {'manifest': 'manifest.json', 'scene': 'scene.json', 'geometry': 'geometry.json',
             'depth': 'depth_m.npy', 'id_mask': 'id_mask.png', 'preview': 'preview.png'}
    with TemporaryDirectory(prefix='spikemark-') as workspace:
        out = Path(workspace) / 'controls'
        geometry = export_scene(snapshot, out, window=vace_window)
        shot = geometry['shot']
        manifest = {
            'version': '0.1', 'scene_version': document['version'], 'baseline': scene_baseline(document),
            'scene_id': geometry['scene_id'], 'shot': shot, 'units': document['units'],
            'up': document['up'], 'resolution': geometry['resolution'],
            'timeline': {'duration_s': shot['duration_s'], 'fps': shot['fps'],
                         'frame_count': scene.frame_count(document),
                         'static_repeat': True},
            'depth': {**geometry['depth'], 'file': files['depth'], 'dtype': 'float32',
                      'layout': 'height_width'},
            'id_mask': {'file': files['id_mask'], 'dtype': 'uint16', 'layout': 'height_width',
                        'background_id': geometry['mask_background'],
                        'zero_includes_ground': True, 'id_mapping': 'geometry.json:entities.*.mask_id'},
            'files': files,
        }
        if vace_window is not None:
            files['vace_depth'] = 'vace_depth.png'
            manifest['vace_depth'] = {**geometry['vace_depth'], 'file': files['vace_depth'],
                                      'dtype': 'uint8', 'layout': 'height_width_rgb', 'invalid': 0}

        (out / files['scene']).write_bytes(_json_bytes(document))
        (out / files['manifest']).write_bytes(_json_bytes(manifest))
        buffer = BytesIO()
        with ZipFile(buffer, 'w') as archive:
            for name in sorted(files.values()):
                _zip_write(archive, name, (out / name).read_bytes())
        return buffer.getvalue()


def preview_image(depth, ids, geometry):
    palette = np.array([(29, 38, 54)] + [COLORS[i % len(COLORS)] for i in range(len(geometry['entities']))], dtype=np.float32)
    preview = palette[ids]
    preview[(ids == 0) & (depth > 0)] = [65, 76, 91]
    shade = np.where(depth > 0, .62+.38*np.exp(-depth/10), 1)
    return Image.fromarray(np.uint8(preview*shade[..., None]))


def main():
    parser = argparse.ArgumentParser(description='SpikeMark M0：用 CPU 编译固定镜头的场景。')
    parser.add_argument('scene', help='输入场景 JSON')
    parser.add_argument('output', help='尚不存在的输出目录')
    parser.add_argument('--edits', help='语义操作数组 JSON；不覆盖原场景')
    parser.add_argument('--vace-depth', type=float, nargs=2, metavar=('NEAR_M','FAR_M'), help='额外导出近亮的 VACE 深度图')
    parser.add_argument('--vace-frames', type=int, metavar='N', help='同时导出静态 VACE 控制序列的帧数')
    args = parser.parse_args()
    try:
        doc = load_scene(args.scene)
        if args.edits is not None:
            doc = edit_document(doc, load_edits(args.edits))
        meta = export_scene(doc, args.output, args.vace_depth, args.vace_frames)
        if args.edits is not None:
            (Path(args.output)/'scene.json').write_text(json.dumps(doc, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    except (OSError, ValueError) as exc:
        parser.exit(2, f'{exc}\n')
    print(f'已导出 {meta["scene_id"]}：{args.output}；诊断 {len(meta["diagnostics"])} 条')


if __name__ == '__main__':
    main()
