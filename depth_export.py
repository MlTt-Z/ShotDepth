"""Shared VACE PNG and Fun Control WebP encoding from metric depth frames."""
import math
import struct
from fractions import Fraction
from io import BytesIO
from time import monotonic

from PIL import Image

from scene import frame_count, validate_scene
from shotdepth import MAX_DYNAMIC_PIXELS, _compile_frame, _has_keyframes, is_animated, vace_depth

MAX_WEBP_FRAMES = 117
# VP8L stores width/height minus one in 14 bits; libwebp accepts at most 16383.
# The existing multiple-of-16 rule makes 16368 the largest supported edge.
MAX_WEBP_EDGE = 16383


class DepthExportTimeout(TimeoutError):
    """The direct export exhausted its server-side time budget."""


def fps_value(value):
    error = 'fps 必须是有限的正数'
    if type(value) not in (str, int, float):
        raise ValueError(error)
    try:
        finite = math.isfinite(float(value))
    except OverflowError as exc:
        raise ValueError(error) from exc
    if not finite:
        raise ValueError(error)
    fps = Fraction(str(value))
    if fps <= 0:
        raise ValueError(error)
    return fps


def frame_times(frames, fps):
    step = 1000 / fps
    ends = [round(i * step) for i in range(frames + 1)]
    durations = [b - a for a, b in zip(ends, ends[1:])]
    if any(not 1 <= duration <= 0xffffff for duration in durations):
        raise ValueError('fps 无法用 WebP 的 24 位毫秒帧时长表示')
    return durations


def check_window(window):
    error = 'vace_window: 需要两个有限数值 [near, far]，且 0 < near < far'
    if (not isinstance(window, (tuple, list)) or len(window) != 2
            or any(type(n) not in (int, float) for n in window)):
        raise ValueError(error)
    try:
        near, far = map(float, window)
    except OverflowError as exc:
        raise ValueError(error) from exc
    if not (math.isfinite(near) and math.isfinite(far) and 0 < near < far):
        raise ValueError(error)
    return near, far


def check_webp_size(width, height, frames):
    if type(frames) is not int or not 5 <= frames <= MAX_WEBP_FRAMES or frames % 4 != 1:
        raise ValueError(f'frames 必须是 5–{MAX_WEBP_FRAMES} 内的 4n+1 整数（当前 {frames}）')
    if any(type(n) is not int or n <= 0 or n % 16 for n in (width, height)):
        raise ValueError(f'宽和高都必须为 16 的正整数倍（当前 {width} × {height}）')
    _check_webp_edge(width, height)
    if width * height * frames > MAX_DYNAMIC_PIXELS:
        raise ValueError(f'输出超过 {MAX_DYNAMIC_PIXELS} 像素预算（当前 {width} × {height} × {frames}）')


def _check_webp_edge(width, height):
    if max(width, height) > MAX_WEBP_EDGE:
        raise ValueError(f'WebP 单边不能超过 VP8L 编码上限 {MAX_WEBP_EDGE}（当前 {width} × {height}）')


def webp_durations(frames, count, source_fps, fps_override):
    fps = source_fps if fps_override is None else fps_value(fps_override)
    if count is not None:
        if frames != count:
            raise ValueError(f'动态导出的 frames 必须等于源帧数 {count}，不能截断或补帧')
        if fps != source_fps:
            raise ValueError(f'动态导出的 fps 必须等于源帧率 {float(source_fps)}，不能重采样')
        fps = source_fps
    return frame_times(frames, fps)


def _chunk(tag, data):
    return tag + struct.pack('<I', len(data)) + data + (b'\0' if len(data) % 2 else b'')


def _u24(value):
    return value.to_bytes(3, 'little')


def _depth_image(depth, window):
    return Image.fromarray(vace_depth(depth, *window)).convert('RGB')


def encode_png(depth, window):
    with BytesIO() as stream:
        _depth_image(depth, window).save(stream, 'PNG')
        return stream.getvalue()


def _lossless_frame(depth, window):
    with BytesIO() as stream:
        _depth_image(depth, window).save(stream, 'WEBP', lossless=True)
        webp = stream.getvalue()
    # Pillow 编码单张 RGB 图；仅取完整的 VP8L 子块（含 RIFF 对齐字节）。
    if webp[:4] != b'RIFF' or webp[8:16] != b'WEBPVP8L':
        raise ValueError('Pillow 未生成预期的 VP8L 图像')
    size = struct.unpack_from('<I', webp, 16)[0]
    return webp[12:20 + size + (size % 2)]


def encode_animation(width, height, durations, dynamic, window, load_frame, *, check=None):
    _check_webp_edge(width, height)
    canvas = _u24(width - 1) + _u24(height - 1)
    with BytesIO() as webp:
        webp.write(b'RIFF\0\0\0\0WEBP')
        webp.write(_chunk(b'VP8X', b'\x02\0\0\0' + canvas))
        webp.write(_chunk(b'ANIM', b'\0' * 6))  # 透明黑背景，无限循环。
        for i, duration in enumerate(durations):
            if check is not None:
                check()
            if dynamic or i == 0:
                try:
                    depth = load_frame(i)
                    if check is not None:
                        check()
                    payload = _lossless_frame(depth, window)
                    del depth
                except DepthExportTimeout:
                    raise
                except (ValueError, OSError, EOFError) as exc:
                    raise ValueError(f'第 {i} 帧：{exc}') from exc
            if check is not None:
                check()
            # 全画布、不混合、不清除；重复帧也单独写入 ANMF，不交给 Pillow 合并。
            header = b'\0' * 6 + canvas + _u24(duration) + b'\x02'
            webp.write(_chunk(b'ANMF', header + payload))
        size = webp.tell()
        webp.seek(4)
        webp.write(struct.pack('<I', size - 8))
        return webp.getvalue()


_OMITTED = object()


def export_depth(document, format, *, vace_window=_OMITTED, frames=_OMITTED, fps=_OMITTED, deadline=None):
    """Compile the supplied shot, with an optional absolute monotonic deadline."""
    def check():
        if deadline is not None and monotonic() >= deadline:
            raise DepthExportTimeout('深度导出超时，请缩短镜头或降低分辨率后重试')

    check()
    if format not in ('vace-png', 'fun-webp'):
        raise ValueError('format 必须是 vace-png 或 fun-webp')
    if format == 'vace-png' and (frames is not _OMITTED or fps is not _OMITTED):
        raise ValueError('vace-png 不接受 frames 或 fps')
    validate_scene(document, check=check)
    dynamic = is_animated(document)
    has_keyframes = _has_keyframes(document)

    def load_frame(index):
        # Full validation has already checked every sampled animated pose.
        return _compile_frame(document, index, has_keyframes, validate_snapshot=False, check=check)[0]

    camera = document['camera']
    window = check_window((camera['near'], camera['far']) if vace_window is _OMITTED else vace_window)

    if format == 'vace-png':
        if dynamic:
            raise ValueError('vace-png 仅支持静态镜头；动态镜头请使用 fun-webp')
        depth = load_frame(0)
        check()
        content = encode_png(depth, window)
        check()
        return content, 'image/png'

    if frames is not _OMITTED and type(frames) is not int:
        raise ValueError('frames 必须是整数')
    if fps is not _OMITTED and type(fps) not in (int, float):
        raise ValueError('fps 必须是有限的正数')
    count = frame_count(document) if dynamic else None
    source_fps = fps_value(document['shot']['fps'] if dynamic else 24)
    frames = (count if dynamic else 25) if frames is _OMITTED else frames
    width, height = document['resolution']
    check_webp_size(width, height, count if dynamic else frames)
    durations = webp_durations(frames, count, source_fps, None if fps is _OMITTED else fps)
    content = encode_animation(width, height, durations, dynamic, window, load_frame, check=check)
    check()
    return content, 'image/webp'
