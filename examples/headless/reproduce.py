import argparse
import asyncio
import base64
import io
import json
import sys
from pathlib import Path
from urllib.request import Request, urlopen
from uuid import uuid4
from zipfile import ZipFile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[2]


async def controls():
    server = StdioServerParameters(command=sys.executable, args=['-B', str(ROOT / 'mcp_server.py')], cwd=ROOT)
    async with stdio_client(server) as (reader, writer), ClientSession(reader, writer) as session:
        await session.initialize()

        async def call(name, arguments):
            result = await session.call_tool(name, arguments)
            if result.isError:
                raise RuntimeError(f'{name}: {result.content}')
            return result

        initial = (await call('get_scene', {})).structuredContent
        # 固定广角，两人全身入镜，左近右远。使用本次对照中由 AI 选定的坐标。
        operations = [
            {'op': 'set_transform', 'entity': 'human_left', 'pos': [-0.93, 0, 3.05]},
            {'op': 'set_transform', 'entity': 'human_right', 'pos': [1.10, 0, 4.35]},
        ]
        edited = (await call('apply_edit', {'document': initial['document'], 'operations': operations})).structuredContent
        geometry = edited['geometry']
        left, right = (geometry['entities'][name] for name in ('human_left', 'human_right'))
        if len(geometry['entities']) != 2 or geometry['diagnostics'] or any(e['visible_fraction'] != 1 for e in (left, right)):
            raise RuntimeError(f'Unexpected framing: {geometry["diagnostics"]}')
        l, r = (entity['visible_bbox']['norm'] for entity in (left, right))
        ratio = (l[3] - l[1]) / (r[3] - r[1])
        if not (l[2] < r[0] and ratio >= 1.10 and all(0 < v < 1 for v in l + r)):
            raise RuntimeError(f'Unexpected framing: {l}, {r}')
        document = edited['document']
        preview = (await call('render_preview', {'document': document})).content[0]
        payload = (await call('export_controls', {'document': document, 'vace_window': [2, 7]})).content[0].resource
        package = base64.b64decode(payload.blob)
        with ZipFile(io.BytesIO(package)) as archive:
            depth = archive.read('vace_depth.png')
        return ratio, package, base64.b64decode(preview.data), depth


def main():
    parser = argparse.ArgumentParser(description='Reproduce the ShotDepth MCP → ComfyUI depth comparison.')
    parser.add_argument('input_dir', type=Path, help='Input folder of the local ComfyUI instance')
    parser.add_argument('--port', type=int, default=8188, help='Local ComfyUI port')
    parser.add_argument('--controls-only', action='store_true', help='Export inputs without submitting video jobs')
    args = parser.parse_args()
    if not args.input_dir.is_dir():
        parser.error('input_dir must be an existing directory')
    ratio, package, preview, depth = asyncio.run(controls())
    run = f'spikemark-headless-{uuid4().hex[:12]}'
    folder = args.input_dir / run
    folder.mkdir()
    for name, data in [('controls.zip', package), ('preview.png', preview), ('vace_depth.png', depth)]:
        (folder / name).write_bytes(data)
    print(f'Controls: {folder} (left/right proxy height: {ratio:.3f})')
    if args.controls_only:
        return

    workflow = (ROOT / 'examples/g0/workflow_api.json').read_text(encoding='utf-8')
    for mode in ('depth', 'no_depth'):
        graph = json.loads(workflow)
        graph['10']['inputs']['seed'] = 20261128
        graph['13']['inputs']['filename_prefix'] = f'{run}/{mode}'
        if mode == 'depth':
            graph['6']['inputs']['image'] = f'{run}/vace_depth.png'
        else:
            del graph['8']['inputs']['control_video'], graph['6'], graph['7']
        request = Request(f'http://127.0.0.1:{args.port}/prompt',
                          data=json.dumps({'prompt': graph}).encode('utf-8'),
                          headers={'Content-Type': 'application/json'})
        with urlopen(request, timeout=30) as response:
            print(f'{mode}: {json.load(response)["prompt_id"]}')
    print(f'ComfyUI output subfolder: {run}')


if __name__ == '__main__':
    main()
