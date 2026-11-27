# SpikeMark：外部 AI 调用契约

SpikeMark 接收结构化场景，提供可编辑的 3D 镜头沙盒、CPU 预览和控制包。它不生成视频。下游工作流自行决定如何使用深度、蒙版、几何和文字意图。

## 1. 入口与调用顺序

- **MCP（stdio）**：客户端以项目 Python（已安装项目依赖）启动 `mcp_server.py`，可选 `--scene <场景文件>`。Windows 配置示例（将 `X:\path\to\SpikeMark` 替换为实际项目目录；JSON 中的反斜杠需写成 `\\`）：

```json
{"mcpServers":{"spikemark":{"command":"X:\\path\\to\\SpikeMark\\.venv\\Scripts\\python.exe","args":["X:\\path\\to\\SpikeMark\\mcp_server.py"]}}}
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
