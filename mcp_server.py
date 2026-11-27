import argparse
from base64 import b64encode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from typing import Any

from mcp.server.fastmcp import FastMCP, Image
from mcp.types import BlobResourceContents, EmbeddedResource
from pydantic import StrictFloat, StrictInt

from editor import (
    edit_snapshot, make_proposal as build_proposal, prepare_shots as preflight_shots,
    preview_snapshot, review_proposal as inspect_proposal,
)
from scene import component_catalog, load_scene
from shotdepth import compile_scene, control_package, preview_image


def _scene_result(snapshot):
    geometry = snapshot['geometry']
    return dict(snapshot, semantics=geometry['semantics'], diagnostics=geometry['diagnostics'])


def make_server(scene_path: Path) -> FastMCP:
    server = FastMCP('SpikeMark', instructions=(
        '使用 get_scene 取得场景，再把返回的 document 传给 apply_edit；'
        '继续编辑时使用最新返回的 document，指定帧采样结果见 sampled_document。'
        '使用 list_components 查询组件；使用 make_proposal 生成可交给网页审阅的 proposal。'
        '使用 prepare_shots 验证镜头集合。提案审阅只返回前后快照，工具不会覆盖场景文件。'
    ))

    @server.tool()
    def list_components() -> dict[str, Any]:
        """列出支持的动作、道具组件及声明参数，供 apply_component 使用。"""
        return component_catalog()

    @server.tool()
    def get_scene(scene_id: str | None = None, frame: StrictInt = 0) -> dict[str, Any]:
        """读取启动场景，返回权威文档、基线及指定帧的采样、语义和构图诊断。帧从 0 开始。"""
        document = load_scene(scene_path)
        if scene_id is not None and document['scene']['id'] != scene_id:
            raise ValueError(f'未找到场景 {scene_id!r}')
        return _scene_result(preview_snapshot(document, frame, include_png=False))

    @server.tool()
    def prepare_shots(shot_set: dict[str, Any]) -> dict[str, Any]:
        """预检版本 "1" 的镜头集合（1–12 个完整单镜头文档，shot.id 唯一）。

        校验每个镜头的第 0 帧，返回 shot_set 和按输入排序的 summaries（id、baseline）。
        失败则整批拒绝，不修改场景。shot.action_text 只描述期望动作，不会渲染该动作。
        """
        return preflight_shots(shot_set)

    @server.tool()
    def apply_edit(document: dict[str, Any], operations: list[dict[str, Any]], frame: StrictInt = 0) -> dict[str, Any]:
        """批量编辑并返回新场景及指定帧快照；失败时整批拒绝。

        支持 set_transform（entity、pos/yaw）、set_body（actor、体型字段）、
        set_camera（pos/look_at/lens_mm/sensor_mm/roll/near/far）、set_ground_size（size）、
        add_entity/remove_entity（entity）、set_entity_name（entity、name）、
        set_keyframe（entity、keyframe）、remove_keyframe（entity、frame）、
        set_action（entity、keyframes）、apply_component（component、entity、params）、
        set_action_text（text，最多 2000 字符，空字符串清除；仅记录文字，不渲染动作）。
        """
        return _scene_result(edit_snapshot(document, operations, frame, include_png=False))

    @server.tool()
    def make_proposal(document: dict[str, Any], operations: list[dict[str, Any]], frame: StrictInt = 0) -> dict[str, Any]:
        """用当前文档的基线构造并校验提案；返回 proposal、前后快照（无 PNG）及变更摘要。"""
        return build_proposal(document, operations, frame, include_png=False)

    @server.tool()
    def review_proposal(document: dict[str, Any], proposal: dict[str, Any], frame: StrictInt = 0) -> dict[str, Any]:
        """审阅版本 1 提案，不提交编辑；基线过期或操作非法时拒绝，快照不含 PNG。"""
        return inspect_proposal(document, proposal, frame, include_png=False)

    @server.tool()
    def render_preview(document: dict[str, Any], shot_id: str | None = None, frame: StrictInt = 0) -> Image:
        """用 CPU 渲染指定镜头帧，返回 PNG 图像。帧从 0 开始。"""
        depth, ids, geometry = compile_scene(document, frame=frame)
        if shot_id is not None and geometry['shot']['id'] != shot_id:
            raise ValueError(f'未找到镜头 {shot_id!r}')
        buffer = BytesIO()
        preview_image(depth, ids, geometry).save(buffer, format='PNG')
        return Image(data=buffer.getvalue(), format='png')

    @server.tool(structured_output=False)
    def export_controls(
        document: dict[str, Any], vace_window: list[StrictInt | StrictFloat] | None = None,
    ) -> EmbeddedResource:
        """导出传入 document 的控制 ZIP，不执行推理。

        vace_window 可省略或传 null；传 [near_m, far_m] 时附带 VACE 深度图，要求 0 < near_m < far_m。
        返回 MIME 类型为 application/zip 的 MCP 内嵌资源；对 resource.blob 做 Base64 解码即可取得 ZIP，
        无需调用 resources/read 或访问服务器文件系统。通道及帧信息见 ZIP 内的 manifest.json。
        """
        package = control_package(document, vace_window)
        return EmbeddedResource(type='resource', resource=BlobResourceContents(
            uri=f'spikemark://controls/{sha256(package).hexdigest()}/controls.zip',
            mimeType='application/zip', blob=b64encode(package).decode('ascii'),
        ))

    return server


def main():
    parser = argparse.ArgumentParser(description='启动 ShotDepth MCP stdio 服务')
    parser.add_argument('--scene', type=Path, default=Path(__file__).with_name('scene.json'))
    args = parser.parse_args()
    make_server(args.scene.resolve()).run(transport='stdio')


if __name__ == '__main__':
    main()
