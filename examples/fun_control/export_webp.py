import argparse
import json
import math
import os
import sys
from collections import Counter
from io import BytesIO
from pathlib import Path
from zipfile import BadZipFile, ZipFile

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from depth_export import check_webp_size, check_window, encode_animation, encode_png, fps_value, webp_durations
from scene import MAX_DYNAMIC_FRAMES
from shotdepth import MAX_DYNAMIC_PIXELS


def read_npy(source, file_size, shape):
    size = math.prod(shape) * 4
    version = np.lib.format.read_magic(source)
    readers = {(1, 0): np.lib.format.read_array_header_1_0,
               (2, 0): np.lib.format.read_array_header_2_0}
    if version not in readers:
        raise ValueError(f'不支持的 NPY 版本 {version}')
    # Check the length prefix before letting NumPy read an untrusted NPY header.
    prefix_size = 2 if version == (1, 0) else 4
    prefix = source.read(prefix_size)
    header_size = int.from_bytes(prefix, 'little')
    if len(prefix) != prefix_size or header_size > 10_000:
        raise ValueError('NPY 头部长度无效或超过上限')
    header = source.read(header_size)
    actual_shape, fortran, dtype = readers[version](BytesIO(prefix + header))
    if actual_shape != shape or dtype.kind != 'f' or dtype.itemsize != 4:
        raise ValueError(f'需要形状为 {shape} 的 float32 深度数组')
    if file_size - source.tell() != size:
        raise ValueError('NPY 数据长度与头部不符')
    data = source.read(size)
    if len(data) != size or source.read(1):
        raise ValueError('NPY 数据长度与头部不符')
    depth = np.frombuffer(data, dtype=dtype).reshape(shape, order='F' if fortran else 'C')
    if not np.isfinite(depth).all() or (depth < 0).any():
        raise ValueError('深度值必须有限且非负（0 表示无效像素）')
    return depth


def read_depth(archive, name, shape):
    entry = archive.getinfo(name)
    with archive.open(entry) as source:
        return read_npy(source, entry.file_size, shape)


def check_depth(depth):
    if (depth['units'], depth['axis'], depth['dtype'], depth['layout']) != (
        'meter', 'camera_z', 'float32', 'height_width'
    ) or type(depth['invalid']) is not int or depth['invalid'] != 0:
        raise ValueError('需要 meter / camera_z / float32 / height_width 深度，且无效值为 0')


def encode(archive, frames, fps_override, window):
    members = Counter(archive.namelist())
    if members['manifest.json'] != 1:
        raise ValueError('ZIP 必须恰好包含一个 manifest.json')
    manifest = json.loads(archive.read('manifest.json'))
    version = manifest['version']
    if version not in ('0.1', '0.2'):
        raise ValueError(f'不支持的 manifest 版本：{version!r}')
    dynamic = version == '0.2'
    width, height = manifest['resolution']
    check_webp_size(width, height, frames)

    timeline = manifest['timeline']
    count = timeline['frame_count']
    if timeline['static_repeat'] is not (not dynamic) or type(count) is not int or count < 1:
        raise ValueError('manifest.timeline 与包版本不符')
    source_fps = fps_value(timeline['fps'])
    if dynamic and (type(timeline['first_frame']) is not int or timeline['first_frame'] != 0):
        raise ValueError('动态包必须从第 0 帧开始')
    durations = webp_durations(frames, count if dynamic else None, source_fps, fps_override)

    depth = manifest['depth']
    check_depth(depth)
    if window is None:
        if 'vace_depth' in manifest:
            vace = manifest['vace_depth']
            window = (vace['near_m'], vace['far_m'])
        else:
            window = (depth['near'], depth['far'])
    window = check_window(window)

    if dynamic:
        pattern = depth['file_pattern']
        if not isinstance(pattern, str) or pattern.count('{frame:06d}') != 1:
            raise ValueError('动态深度 file_pattern 需要一个 {frame:06d} 占位符')
        names = [pattern.replace('{frame:06d}', f'{i:06d}') for i in range(frames)]
    else:
        names = [depth['file']]
    for i, name in enumerate(names):
        if not isinstance(name, str) or members[name] != 1:
            raise ValueError(f'第 {i} 帧：缺少或重复深度文件 {name!r}')

    def load_frame(index):
        try:
            return read_depth(archive, names[index], (height, width))
        except BadZipFile as exc:
            raise ValueError(str(exc)) from exc

    return encode_animation(width, height, durations, dynamic, window, load_frame)


def encode_depth_manifest(source, output_format, frames, fps_override, window):
    manifest = json.loads(source.read_text(encoding='utf-8'))
    if (manifest['format'], manifest['version']) != ('shotdepth-depth', '1'):
        raise ValueError('需要 Blender shotdepth-depth 版本 1 清单')

    width, height = manifest['resolution']
    if any(type(n) is not int or n <= 0 for n in (width, height)):
        raise ValueError('宽和高必须为正整数')
    timeline = manifest['timeline']
    count = timeline['frame_count']
    if type(count) is not int or not 1 <= count <= MAX_DYNAMIC_FRAMES:
        raise ValueError(f'Blender 清单帧数必须在 1 到 {MAX_DYNAMIC_FRAMES} 之间')
    first = manifest['source']['first_frame']
    last = manifest['source']['last_frame']
    if type(first) is not int or type(last) is not int or last - first + 1 != count:
        raise ValueError('Blender 清单帧范围与帧数不符')
    dynamic = count > 1
    if output_format == 'vace-png' and dynamic:
        raise ValueError('--format vace-png 只支持单帧 Blender 深度，不支持动态清单')
    if width * height * count > MAX_DYNAMIC_PIXELS:
        raise ValueError(f'Blender 源深度超过 {MAX_DYNAMIC_PIXELS} 像素预算（宽 × 高 × 帧数）')
    if type(timeline['fps']) not in (int, float):
        raise ValueError('Blender 清单 fps 必须是有限的正数')
    source_fps = fps_value(timeline['fps'])
    depth = manifest['depth']
    check_depth(depth)
    source_window = check_window((depth['near'], depth['far']))
    window = source_window if window is None else check_window(window)

    def load_frame(index):
        # Blender 帧号可以非零或为负；同目录的文件名总按从 0 开始的索引生成。
        path = source.with_name(f'depth_{index:06d}.npy')
        if path.is_symlink() or not path.is_file():
            raise ValueError(f'需要普通 NPY 文件：{path}')
        with path.open('rb') as stream:
            return read_npy(stream, os.fstat(stream.fileno()).st_size, (height, width))

    if output_format == 'vace-png':
        return encode_png(load_frame(0), window)
    check_webp_size(width, height, frames)
    durations = webp_durations(frames, count if dynamic else None, source_fps, fps_override)
    return encode_animation(width, height, durations, dynamic, window, load_frame)


def check_output(output):
    if output.exists() or output.is_symlink():
        raise FileExistsError(f'拒绝覆盖已有输出：{output}')


def write_output(output, data):
    stream = output.open('xb')  # 即使编码期间目标路径被创建，也不会覆盖。
    try:
        with stream:
            stream.write(data)
    except BaseException:
        output.unlink()
        raise


def export_webp(source, output, frames, fps=None, window=None):
    check_output(output)
    with ZipFile(source) as archive:
        webp = encode(archive, frames, fps, window)
    write_output(output, webp)


def main():
    parser = argparse.ArgumentParser(description='将 ShotDepth ZIP 或 Blender 深度转为 RGB 无损动画 WebP 或静态 VACE PNG。')
    parser.add_argument('source', type=Path, help='输入控制 ZIP（manifest 0.1 或 0.2），或配合 --depth-manifest 输入 Blender depth.json')
    parser.add_argument('output', type=Path, help='输出文件路径（不得已存在）')
    parser.add_argument('--depth-manifest', action='store_true', help='将 source 作为 Blender 深度清单读取，而不是 ZIP')
    parser.add_argument('--format', choices=('webp', 'vace-png'), default='webp',
                        help='默认 webp；vace-png 只支持单帧 Blender 深度')
    parser.add_argument('--frames', type=int, help='WebP 必填：目标帧数 4n+1（至少 5）；动态输入必须与清单一致')
    parser.add_argument('--fps', help='WebP 默认取清单 fps，仅静态输入可以改变 fps')
    parser.add_argument('--vace-depth', type=float, nargs=2, metavar=('NEAR_M', 'FAR_M'),
                        help='VACE 米制深度窗口，默认取清单深度窗口（ZIP 优先采用 VACE 窗口）')
    args = parser.parse_args()
    if args.format == 'vace-png':
        if not args.depth_manifest:
            parser.error('--format vace-png 需要 --depth-manifest')
        if args.frames is not None or args.fps is not None:
            parser.error('--format vace-png 不接受 --frames 或 --fps')
    elif args.frames is None:
        parser.error('WebP 输出需要 --frames')
    try:
        if args.depth_manifest:
            check_output(args.output)
            data = encode_depth_manifest(args.source, args.format, args.frames, args.fps, args.vace_depth)
            write_output(args.output, data)
        else:
            export_webp(args.source, args.output, args.frames, args.fps, args.vace_depth)
    except (ValueError, TypeError, KeyError, OSError, OverflowError, EOFError, BadZipFile) as exc:
        parser.exit(1, f'{parser.prog}: {exc}\n')


if __name__ == '__main__':
    main()
