"""用 Blender 后台模式生成并自检两种深度输入，无需准备 .blend。"""

import argparse
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import bpy
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from export_depth import export


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('output', type=Path, help='尚不存在的输出目录')
    parser.add_argument('--python', required=True, type=Path, help='已安装 ShotDepth 依赖的 Python')
    args = parser.parse_args(sys.argv[sys.argv.index('--') + 1:] if '--' in sys.argv else [])
    python = args.python.resolve(strict=True)
    args.output.mkdir()
    scene = bpy.data.scenes.new('ShotDepth Depth Demo')
    bpy.context.window.scene = scene
    scene.render.resolution_x = 64
    scene.render.resolution_y = 48
    scene.render.resolution_percentage = 100
    scene.render.fps = 24
    camera = bpy.data.objects.new('Camera', bpy.data.cameras.new('Camera'))
    scene.collection.objects.link(camera)
    scene.camera = camera
    bpy.ops.mesh.primitive_plane_add(size=0.6, location=(-0.45, 0, -3))
    bpy.ops.mesh.primitive_plane_add(size=1.0, location=(0.85, 0, -5))
    scene.frame_set(1)
    depth_dir = args.output / 'depth'
    export(scene, SimpleNamespace(start=1, end=1, out=depth_dir, meters_per_unit=1, depth_window=(2, 6)))
    depth = np.load(depth_dir / 'depth_000000.npy', allow_pickle=False)
    assert depth.shape == (48, 64) and depth.dtype == np.float32
    assert np.allclose([depth[24, 19], depth[24, 47], depth[0, 0]], [3, 5, 0])
    converter = HERE.parent / 'fun_control' / 'export_webp.py'
    png = args.output / 'vace_depth.png'
    webp = args.output / 'depth.webp'
    for output, flags in ((png, ['--format', 'vace-png']), (webp, ['--frames', '5'])):
        subprocess.run([str(python), '-B', str(converter), str(depth_dir / 'depth.json'),
                        str(output), '--depth-manifest', *flags], check=True)
    check = '''
import sys
from PIL import Image, ImageSequence
with Image.open(sys.argv[1]) as png, Image.open(sys.argv[2]) as webp:
    gray = png.convert('L')
    assert png.size == webp.size == (64, 48)
    assert [gray.getpixel(p) for p in ((19, 24), (47, 24), (0, 0))] == [191, 64, 0]
    assert webp.n_frames == 5
    rgb = png.convert('RGB').tobytes()
    assert all(frame.convert('RGB').tobytes() == rgb for frame in ImageSequence.Iterator(webp))
'''
    subprocess.run([str(python), '-B', '-c', check, str(png), str(webp)], check=True)
    print(f'深度、VACE PNG 和 5 帧 Fun Control WebP 自检通过：{args.output.resolve()}')


if __name__ == '__main__':
    main()
