"""Export geometric camera-space depth from Blender's active scene.

Only full-frame, monoscopic perspective cameras are supported. This samples
mesh surfaces visible to Scene.ray_cast in the evaluated view layer, not a
render pass: render-only visibility/modifiers, shader transparency/displacement,
volumes, depth of field, motion blur and antialiasing are not reproduced.
"""

import argparse
import json
import math
import sys
from pathlib import Path

import bpy
import numpy as np
from mathutils import Vector


MAX_FRAMES = 120
MAX_PIXELS = 32_000_000
FLOAT32_MAX = float(np.finfo(np.float32).max)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        usage='blender -b file.blend --python-exit-code 1 --python %(prog)s -- '
              'OUT [--start N] [--end N] --meters-per-unit M --depth-window NEAR FAR',
        description=__doc__,
    )
    parser.add_argument('out', type=Path, metavar='OUT', help='new output directory (must not exist)')
    parser.add_argument('--start', type=int, help='first Blender frame (default: current frame)')
    parser.add_argument('--end', type=int, help='last Blender frame, inclusive (default: first frame)')
    parser.add_argument('--meters-per-unit', type=float, required=True, metavar='M',
                        help='meters per Blender unit; independent of scene unit settings')
    parser.add_argument('--depth-window', type=float, nargs=2, required=True, metavar=('NEAR', 'FAR'),
                        help='grayscale window in meters; does not clip or remap exported depth')
    args = parser.parse_args(argv[argv.index('--') + 1:] if '--' in argv else [])
    if not math.isfinite(args.meters_per_unit) or args.meters_per_unit <= 0:
        parser.error('--meters-per-unit must be finite and positive')
    near, far = args.depth_window
    if not (math.isfinite(near) and math.isfinite(far) and 0 < near < far):
        parser.error('--depth-window requires finite values with 0 < NEAR < FAR')
    return args


def sample_depth(scene, width, height, meters_per_unit):
    # Resolve once after each frame_set, including camera switches and animation.
    depsgraph = bpy.context.evaluated_depsgraph_get()
    camera = scene.camera
    if camera is None or camera.type != 'CAMERA':
        raise ValueError('the active scene has no camera')
    camera = camera.evaluated_get(depsgraph)
    data = camera.data
    if data.type != 'PERSP':
        raise ValueError('only perspective cameras are supported')

    # view_frame includes lens shift, sensor fit and render/pixel aspect ratio.
    # Blender looks along local -Z; express all rays with local Z = -1.
    corners = [(v.x / -v.z, v.y / -v.z) for v in data.view_frame(scene=scene)]
    left = min(x for x, _ in corners)
    right = max(x for x, _ in corners)
    bottom = min(y for _, y in corners)
    top = max(y for _, y in corners)
    dx, dy = (right - left) / width, (top - bottom) / height
    transform = camera.matrix_world
    origin = transform.translation
    rotation = transform.to_quaternion().to_matrix()
    forward = rotation @ Vector((0, 0, -1))
    clip_start, clip_end = data.clip_start, data.clip_end
    if not 0 < clip_start < clip_end:
        raise ValueError('camera clipping requires 0 < clip_start < clip_end')

    xs = [left + (x + 0.5) * dx for x in range(width)]
    depth = np.zeros((height, width), dtype='<f4')
    cast = scene.ray_cast
    for row in range(height):
        y = top - (row + 0.5) * dy
        for col, x in enumerate(xs):
            ray = rotation @ Vector((x, y, -1))
            length = ray.length
            # Clip on camera-Z planes, not at a constant distance along the ray.
            start = origin + ray * clip_start
            hit, location, *_ = cast(depsgraph, start, ray / length,
                                      distance=(clip_end - clip_start) * length)
            if hit:
                meters = (location - origin).dot(forward) * meters_per_unit
                if not 0 < meters <= FLOAT32_MAX:
                    raise ValueError('hit depth cannot be stored as a positive float32')
                value = np.float32(meters)
                if value == 0:
                    raise ValueError('hit depth rounds to the reserved float32 value zero')
                depth[row, col] = value
    return depth


def export(scene, args):
    # frame_set(frame) would discard the currently evaluated subframe.
    use_current_time = args.start is None and args.end is None
    first = scene.frame_current if args.start is None else args.start
    last = first if args.end is None else args.end
    frame_count = last - first + 1
    if not 1 <= frame_count <= MAX_FRAMES:
        raise ValueError(f'expected an inclusive frame range of 1 to {MAX_FRAMES} frames')

    render = scene.render
    if render.use_border:
        raise ValueError('render border is not supported; disable it to export the full frame')
    if render.use_multiview:
        raise ValueError('multiview rendering is not supported')
    width = render.resolution_x * render.resolution_percentage // 100
    height = render.resolution_y * render.resolution_percentage // 100
    if min(width, height) <= 0 or width * height * frame_count > MAX_PIXELS:
        raise ValueError(f'output must have positive dimensions and at most {MAX_PIXELS} total pixels')
    fps = render.fps / render.fps_base
    if not math.isfinite(fps) or fps <= 0:
        raise ValueError('effective scene FPS must be finite and positive')
    near, far = args.depth_window
    manifest = {
        'format': 'shotdepth-depth',
        'version': '1',
        'resolution': [width, height],
        'source': {'first_frame': first, 'last_frame': last},
        'timeline': {'fps': fps, 'frame_count': frame_count},
        'depth': {
            'units': 'meter', 'axis': 'camera_z', 'dtype': 'float32',
            'layout': 'height_width', 'invalid': 0, 'near': near, 'far': far,
        },
    }

    # Reserve OUT exclusively; publish the manifest only after all arrays are written.
    created = []
    args.out.mkdir()
    complete = False
    try:
        for index, frame in enumerate(range(first, last + 1)):
            try:
                if not use_current_time:
                    scene.frame_set(frame)
                    if scene.frame_current != frame:
                        raise ValueError('frame number is outside Blender\'s supported range')
                depth = sample_depth(scene, width, height, args.meters_per_unit)
            except (ValueError, OverflowError) as exc:
                raise ValueError(f'Blender frame {frame}: {exc}') from exc
            path = args.out / f'depth_{index:06d}.npy'
            with path.open('xb') as stream:
                created.append(path)
                np.save(stream, depth, allow_pickle=False)
            del depth
        path = args.out / 'depth.json'
        with path.open('x', encoding='utf-8') as stream:
            created.append(path)
            json.dump(manifest, stream, indent=2, allow_nan=False)
            stream.write('\n')
        complete = True
    finally:
        if not complete:
            for path in reversed(created):
                try:
                    path.unlink()
                except OSError as exc:
                    print(f'Could not remove {path}: {exc}', file=sys.stderr)
            try:
                args.out.rmdir()
            except OSError as exc:
                print(f'Could not remove {args.out}: {exc}', file=sys.stderr)


if __name__ == '__main__':
    export(bpy.context.scene, parse_args(sys.argv))
