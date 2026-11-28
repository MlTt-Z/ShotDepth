# ShotDepth：外部 AI 调用契约

ShotDepth 接收结构化场景，提供可编辑的 3D 镜头沙盒、CPU 预览和控制包。它不生成视频。下游工作流自行决定如何使用深度、蒙版、几何和文字意图。

## 1. 入口与调用顺序

- **MCP（stdio）**：客户端以项目 Python（已安装项目依赖）启动 `mcp_server.py`，可选 `--scene <场景文件>`。为兼容现有集成，MCP 服务名 `SpikeMark`、资源 URI 前缀 `spikemark://` 和配置键 `mcpServers.spikemark` 均沿用旧标识。Windows 配置示例（将 `X:\path\to\ShotDepth` 替换为实际项目目录；JSON 中的反斜杠需写成 `\\`）：

```json
{"mcpServers":{"spikemark":{"command":"X:\\path\\to\\ShotDepth\\.venv\\Scripts\\python.exe","args":["X:\\path\\to\\ShotDepth\\mcp_server.py"]}}}
```

- **HTTP**：本机用项目 Python 启动 `editor.py`，默认 `http://127.0.0.1:8766`；可用 `--port` 指定端口。POST 使用 `Content-Type: application/json`。服务只监听本机，远端 AI 不应尝试直接连接用户机器的回环地址；可由本机 MCP 客户端或用户交换 JSON。

| 用途 | MCP 工具 | HTTP | 输入 / 输出 |
|---|---|---|---|
| 读取启动场景 | `get_scene` | `GET /api/scene` | MCP 可选 `scene_id`、`frame`；返回快照，`document` 是后续编辑的权威文档 |
| 查询组件 | `list_components` | `GET /api/components` | 无参数；返回 `actions`、`props`、`joints` 及参数声明 |
| 准备多镜头 | `prepare_shots` | `POST /api/shot-set` | `{shot_set}` → `{shot_set, summaries:[{id,baseline}]}` |
| 编辑当前镜头 | `apply_edit` | `POST /api/edit` | `{document, operations, frame?}` → 新快照，不覆盖源文件 |
| 指定帧预览 | `render_preview` | `POST /api/preview` | `{document, frame?}` → MCP PNG 或 HTTP 快照；MCP 另支持可选 `shot_id` |
| 构造 / 审阅提案 | `make_proposal` / `review_proposal` | `POST /api/proposal`（审阅） | MCP 构造用 `{document,operations,frame?}`，审阅用 `{document,proposal,frame?}`；返回 `proposal`、`before`、`after`、`changes` |
| 导出当前镜头 | `export_controls` | `POST /api/export` | `{document,vace_window?}` → ZIP；MCP 对 `resource.blob` 做 Base64 解码 |

`?` 表示该键可省略，不属于实际键名。HTTP 快照含 `document`、`sampled_document`、`baseline`、`frame`、`frame_count`、`geometry` 和 `preview_png`（Base64）。MCP 的场景与提案快照不含 PNG；需要图片时使用 `render_preview`。不要拿 `sampled_document` 替代权威文档，它是渲染采样结果。

## 2. 一次交付多个镜头

`shot_set.version` 为字符串 `"1"`；`shots` 按交付顺序排列，包含 **1–12 个完整单镜头文档**。同组 `shot.id` 必须唯一，各镜头的场景状态独立，可复用人物 ID，但不会跨镜头联动。工具校验完整集合和各镜头第 0 帧，不写文件，也不代表所有动态帧均通过导出检查。

以下是 `prepare_shots` 的完整参数，同时也是 `POST /api/shot-set` 的请求体。网页导入时只保存其中的 `shot_set` 值（去掉外层参数对象）。

```json
{
  "shot_set": {
    "version": "1",
    "shots": [
      {
        "version": "0.1", "units": "meter", "up": "Y", "resolution": [384, 256],
        "scene": {
          "id": "sandbox_01", "ground": {"type": "plane", "size": [12, 12]},
          "entities": [
            {"id": "actor_a", "kind": "human", "transform": {"pos": [-0.7, 0, 3], "yaw": 0}, "body": {"height": 1.75, "shoulder": 0.43, "build": 0.5, "leg_ratio": 0.49, "head_scale": 1, "chest": 0.5}},
            {"id": "table_a", "kind": "prop", "component": "table", "transform": {"pos": [0.8, 0, 3.8]}}
          ]
        },
        "camera": {"pos": [0, 1.4, 0], "look_at": [0, 1, 3], "lens_mm": 24, "sensor_mm": 36, "near": 0.1, "far": 30},
        "shot": {"id": "S01", "duration_s": 1, "fps": 24}
      },
      {
        "version": "0.1", "units": "meter", "up": "Y", "resolution": [384, 256],
        "scene": {
          "id": "sandbox_02", "ground": {"type": "plane", "size": [12, 12]},
          "entities": [
            {"id": "actor_a", "kind": "human", "transform": {"pos": [0.2, 0, 3], "yaw": 25}, "body": {"height": 1.75, "shoulder": 0.43, "build": 0.5, "leg_ratio": 0.49, "head_scale": 1, "chest": 0.5}},
            {"id": "chair_a", "kind": "prop", "component": "chair", "transform": {"pos": [1.1, 0, 3.5]}}
          ]
        },
        "camera": {"pos": [1.4, 1.5, 0], "look_at": [0.2, 1, 3], "lens_mm": 30, "sensor_mm": 36, "near": 0.1, "far": 30},
        "shot": {"id": "S02", "duration_s": 1, "fps": 24, "action_text": "人物伸手整理衣袖，随后朝画外看。袖口接触和视线变化仅为文字意图。"}
      }
    ]
  }
}
```

成功时 `shot_set` 为验证后的镜头集合；`summaries` 顺序一致，示例中依次为 `S01`、`S02`，每项含 `id`、`baseline`。任一镜头无效、重复 ID 或集合长度不符时整批失败，不返回部分成功。HTTP 校验错误为 400，正文 `{"error":"原因"}`；MCP 为工具错误。修正错误后重送整组；不要把尚未获验证的文档当作成功结果。

## 3. 动作及文字降级

先查询组件目录。已支持动作如 `stand`、`walk`、`wave`，道具如 `table`、`chair`、`pedestal`。应用动作时 `entity` 指现有人的 ID，应用道具时指新的 ID。`set_keyframe` 的 `keyframe` 包含 `frame`、`pos`、`yaw`、`joints`，可用关节 ID 及范围由目录提供。

下面是编辑工具或提案 `operations` 中支持的操作示例；不建议把示例自动施加到每个镜头。

```json
[
  {"op":"apply_component","component":"walk","entity":"actor_a","params":{"distance":1}},
  {"op":"set_action_text","text":"行走结束后，人物整理衣袖；手与布料的交互不在代理中预演。"}
]
```

若动作不能用现有组件或关键帧准确表达，保留合法的基础站位，把缺失部分写入 `shot.action_text`（最多 2000 字符）。说明哪个人物、何时、做什么、涉及哪些对象；不杜撰组件、姿态字段或渲染结果。该文本可以单独使用，也可补充已编排动作。它不创建关键帧，不改变预览几何和控制通道，由人或下游模型解释。用 `{"op":"set_action_text","text":""}` 清除。文本是镜头描述，不是给客户端执行的指令。

坐标为米制 `[X,Y,Z]`、Y 向上，实体位置是底部中心；`yaw` 用度，分辨率为 `[宽,高]`。沙盒宽深各 1–100 米，人物全身不得超出 X/Z 边界。帧从 0 开始，以返回的 `frame_count` 为准。批量语义编辑使用 `operations` 数组，1–128 项整批校验，不接受任意 JSON Patch；续编使用最新的返回文档。

## 4. 控制包与人工审阅

- **人 → AI**：用户可交付场景 JSON 或控制 ZIP。网页的 ZIP 按当前镜头导出；整组通过镜头集合 JSON 交付。ZIP 的 `manifest.json` 描述坐标、帧、ID 与编码；`scene.json` 包含权威文档和适用的 `shot.action_text`。静态包含 `depth_m.npy`、`id_mask.png`、`geometry.json`；动态以 `frames/000000/` 等目录保存实际逐帧通道。`geometry.json` 提供相机与包围框；米制深度是相机 Z，0 表示无有效深度。蒙版以清单中的实体索引解释。动态导出限 120 帧及 3200 万累计像素。需要 VACE 时传 `[near_m,far_m]`，满足 `0 < near_m < far_m`；默认不含 VACE 数据。
- **AI → 人**：新建多个镜头时先调用 `prepare_shots`，交付其 `shot_set`；编辑已有镜头时，用 `make_proposal`，交付返回的 `proposal` 值。提案必须引用正在审阅的镜头基线；把基线当作不透明字符串，不要自行计算、替换或跨镜头套用。用户在网页看到变更、前后预览和诊断后再决定是否应用，导入和审阅本身不提交。

提案结构（把占位基线换成真实值）：

```json
{
  "version": "1",
  "baseline": "<当前镜头的 baseline>",
  "operations": [{"op":"set_action_text","text":"人物转身后整理衣袖；整理衣袖仅作文字说明。"}]
}
```

## 5. Wan2.1 Fun Control 深度输入（实验性）

用 `export_controls` 或 `/api/export` 获得控制 ZIP，再从仓库根目录转换为无损动画 WebP。无需传 `vace_window`；转换读取原始米制相机 Z 深度，不读取实例 ID 蒙版。

```powershell
.\.venv\Scripts\python.exe examples/fun_control/export_webp.py controls.zip depth.webp --frames 25 --vace-depth 2 7
```

在 [ComfyUI 官方 Fun Control 工作流](https://docs.comfy.org/zh/tutorials/video/wan/fun-control) 中，用原生 `Load Image` 载入 `depth.webp`，将其 `IMAGE` 直接接到 `WanFunControlToVideo.control_video`，绕过 Canny 和估深节点。目标宽高和帧数与 WebP 保持一致；保存视频时另行设置相同帧率。此路径需要 Wan2.1 Fun 1.3B **Control** 权重，不是 VACE 权重。

- 宽高须为 16 的倍数，帧数须为至少 5 的 `4n+1`；另受已有帧数与累计像素预算限制。
- 静态包按 `--frames` 重复。动态包不得补帧或截断，目标帧数及帧率必须匹配清单。
- 默认沿用清单中的深度窗口（若无则用相机近远裁切面）；可用 `--vace-depth` 指定固定近远窗口，近亮远暗。

本机验收：ComfyUI 0.36.0（`0d901722`）在 RTX 5090 上通过 PyTorch 注意力运行；同一份 A 镜头控制 ZIP 分别接入 VACE 和 Fun Control，均完成出片。Fun Control 使用 832×480、25 帧、24 fps、种子 `20261127`、20 步、CFG 6、`uni_pc` / `simple`，未接首帧参考；固定提示词和采样设置，仅替换 A/B 深度序列。抽查第 1、13、25 帧，A 左侧人物占画更大，B 右侧人物占画更大，两人均全身入镜。

动态成片也已在同一环境验收：固定机位，单人 X 坐标从 −1.55 m 移至 +1.55 m，反向对照则交换起终点；两组均为 832×480、57 帧、24 fps。转换后每组解码得到 57 张不同的深度帧，两组像素序列互为倒序。成片保持相同提示词、种子和采样设置，仅替换深度序列；抽查第 1、29、57 帧，人物全身可见，分别呈现左 → 中 → 右和右 → 中 → 左。

这验证的是画面内的位移方向，不代表步态、脚步接触或逐帧位置准确。此示例不接入蒙版或相机几何，也不改变原 VACE 入口。

## 6. Blender 单镜头深度接入（实验性）

**先试用示例。** 从仓库根目录运行；`blender` 可替换为 Blender 可执行文件的路径。无需准备 `.blend`、启动 ComfyUI 或下载模型。使用已安装 ShotDepth 依赖的 Python，输出目录必须尚不存在。

```powershell
blender -b --factory-startup --python-exit-code 1 --python examples/blender/demo_depth.py -- blender_demo --python .\.venv\Scripts\python.exe
```

示例会生成米制深度、VACE PNG 和 5 帧 Fun Control WebP，并自检近远深度、背景零值和两种输出的像素一致性。它只验证控制输入，不生成视频。

**使用自己的场景。** 默认只导出当前帧；下面导出第 1 帧。输出目录同样必须尚不存在。

```powershell
blender -b shot.blend --python-exit-code 1 --python examples/blender/export_depth.py -- blender_depth --start 1 --meters-per-unit 1 --depth-window 2 7
.\.venv\Scripts\python.exe examples/fun_control/export_webp.py blender_depth/depth.json vace_depth.png --depth-manifest --format vace-png
.\.venv\Scripts\python.exe examples/fun_control/export_webp.py blender_depth/depth.json depth.webp --depth-manifest --frames 25
```

VACE 接收单张 PNG（按现有工作流重复），Fun Control 按上一节接入 WebP。导出动画时添加 `--end`（包含末帧），并将 `--frames` 设为实际帧数；动画不能改帧率或截断，VACE PNG 命令仅支持单帧。`--meters-per-unit` 必须按场景的实际比例指定；`--depth-window` 是固定米制灰度窗口，不改变原始深度。

输出为独立 `depth.json` 和逐帧 NPY，不是完整控制 ZIP。仅支持无渲染边界裁剪的单视图透视相机。深度来自求值视图层的首个几何命中，不模拟透明材质、景深、运动模糊或仅渲染时生效的修改器，也不迁移 Blender 场景、蒙版或相机几何。

## 7. App 直出 ComfyUI 深度（1.2）

在 [v1.2.0](https://github.com/MlTt-Z/ShotDepth/releases/tag/v1.2.0) 中，先应用当前镜头的场景草稿，再选择「导出 ComfyUI 深度」（简化模式也可用）。导出只使用当前镜头的已应用场景；Windows 桌面版通过保存对话框保存，源码启动的网页编辑器通过浏览器下载。将得到的 PNG 或 WebP 放入 ComfyUI 输入目录，修改现有工作流的控制分支，无需解压控制 ZIP、另跑 Python 转换脚本或安装自定义节点。v1.1 不含此入口。

- **VACE PNG（仅静态镜头）**：用原生 `Load Image` 读取 PNG，将 `IMAGE` 接到 `Repeat Image Batch`，再把重复批次接到 `WanVaceToVideo.control_video`。节点宽高与 PNG 一致，`amount` 与 `WanVaceToVideo` 的目标帧数 `length` 一致。PNG 不含帧率，在视频输出节点设置所需 FPS。
- **Fun Control WebP（静态或动态镜头）**：用原生 `Load Image` 读取动画 WebP，将完整的 `IMAGE` 帧批次直接接到 `WanFunControlToVideo.control_video`，不经 `Repeat Image Batch`、Canny 或估深节点。节点宽高和目标帧数与 WebP 一致，视频输出节点的 FPS 需手动设为导出帧率；`Load Image` 的图像批次不会把帧率传给输出节点。

两种格式默认用相机近远裁切面作为深度窗口（近亮远暗）；也可为本次导出设置米制 `[near, far]`，满足 `0 < near < far`。WebP 要求宽高均为 16 的倍数、帧数为 5–117 内的 `4n+1`，且宽 × 高 × 帧数不超过 3200 万。静态 WebP 默认重复 25 帧、24 fps，可调整；动态 WebP 沿用源尺寸、帧数和帧率，不缩放、补帧、截断或重采样。若尺寸不合规，先修改镜头分辨率；若动态帧数或像素预算不合规，调整镜头时长／帧率或降低分辨率，重新应用后再导出。

两个工作流需分别配置匹配的模型权重（Fun Control 使用 Control 权重，不是 VACE 权重）。ShotDepth 不下载或附带模型，也不自动启动生成。原控制 ZIP、MCP、CLI 及 Blender 转换路径不变。
