# ShotDepth — 3D shot layout and control export for AI video

在 3D 中摆好人物和机位，把**深度图、实例 ID 蒙版与相机几何**导出给 ComfyUI 等视频工作流。<br>
Block actors and cameras in 3D, then export **depth maps, instance ID masks, and camera geometry** to workflows such as ComfyUI.

[![同一提示词与 seed：有深度时左近右远，无深度时两人近似等高 / Same prompt and seed: depth preserves the intended foreground placement](examples/demo/comparison.jpg)](examples/demo/framing-demo.mp4)

[观看 16 秒双语字幕演示 / Watch the 16-second demo](examples/demo/framing-demo.mp4)

[不打开 App：通过 MCP 复现实例 / Reproduce via MCP without the app](#无界面复现--headless-reproduction)

ShotDepth 只负责摆位、预览和导出控制数据。不生成视频、不下载或附带模型权重，也不评价成片。<br>
ShotDepth handles blocking, previews, and control export. It does not generate or score video, nor download or bundle model weights.

原名 SpikeMark。旧仓库地址会跳转到 ShotDepth；MCP 标识和 v1.1 下载文件名保持不变。<br>
Formerly SpikeMark. The old repository URL redirects to ShotDepth; the MCP identifier and v1.1 download filenames are unchanged.

v1.1 新增 Windows 桌面版、第一人称布景、实时预览，以及「简化 / 精确」两种界面模式。

v1.1 adds a Windows desktop app, first-person staging, live previews, and Simple and Precision workspace modes.

Windows x64 便携包 / Windows x64 portable build: [GitHub Release](https://github.com/MlTt-Z/ShotDepth/releases/tag/v1.1)（解压后运行 `SpikeMark.exe`，需要 Microsoft Edge WebView2 Runtime / Extract and run `SpikeMark.exe`; Microsoft Edge WebView2 Runtime required).

## 下游对照 / Downstream comparison

同一机位、同一提示词和 seed：A 中屏幕左侧人物更近，B 中右侧人物更近。无深度组不提供这一信息。案例检验**模型是否跟随人物的相对占画大小**，不检验动作、身份一致性或通用生成质量。

Same camera, prompt, and seed: the left actor is closer in A, the right actor in B. The no-depth runs receive neither layout. This checks relative on-screen size, not motion, identity, or general video quality.

| 镜头 / Shot | 3D 场景预览 / 3D scene preview | VACE 深度 / Depth |
| --- | --- | --- |
| A · 左近 / Left closer | ![A 预览](examples/g0/A/preview.png) | ![A 深度](examples/g0/A/vace_depth.png) |
| B · 右近 / Right closer | ![B 预览](examples/g0/B/preview.png) | ![B 深度](examples/g0/B/vace_depth.png) |

**判定标准 / Pass criteria**：在视频第 1、13、25 帧（从 1 开始计），两人均完整可见；A 中左人更高，B 中右人更高，且每帧较高者的像素身高至少是另一人的 1.10 倍。无深度组保留作对照，不要求它失败。单个 seed 不代表普遍有效。

**实测结果 / Results**：Wan2.1 VACE 1.3B 的四组视频均生成成功。人工检查第 1、13、25 帧：A 有深度组三帧均左人更高，B 有深度组三帧均右人更高；六帧均完整显示两人，较高者约为另一人的 1.3 倍，超过 1.10 的门槛。两组无深度视频在抽检帧中均出现三人。

All four Wan2.1 VACE 1.3B clips rendered successfully. On frames 1, 13, and 25, both depth-conditioned clips show two full-body actors. The left actor is taller in A and the right actor in B, at roughly 1.3× the other actor's on-screen height. Both no-depth clips show three people in all sampled frames. This is one seed and one setup, not a general benchmark.

![四组视频的第 1、13、25 帧 / Frames 1, 13 and 25 of the four clips](examples/g0/frames-1-13-25.jpg)

成片 / Clips: [A 有深度 / Depth](examples/g0/A/depth.mp4) · [A 无深度 / No depth](examples/g0/A/no_depth.mp4) · [B 有深度 / Depth](examples/g0/B/depth.mp4) · [B 无深度 / No depth](examples/g0/B/no_depth.mp4)

<details>
<summary>复现步骤 / Reproduce</summary>

1. A 使用 [scene.json](scene.json)，B 应用 [摆位改动](examples/g0/shot-b-edits.json)。可用以下命令重新导出（输出目录不得已存在）：

```powershell
python spikemark.py scene.json g0-a --vace-depth 2 7
python spikemark.py scene.json g0-b --edits examples/g0/shot-b-edits.json --vace-depth 2 7
```

2. 启动 ComfyUI，准备 `wan2.1_vace_1.3B_fp16.safetensors`、`umt5_xxl_fp8_e4m3fn_scaled.safetensors`、`wan_2.1_vae.safetensors`，分别放入 `models/diffusion_models`、`models/text_encoders`、`models/vae`。把本页两张深度图放入 ComfyUI 输入目录的 `spikemark-g0/A/vace_depth.png` 和 `spikemark-g0/B/vace_depth.png`。
3. 运行 [queue.ps1](examples/g0/queue.ps1) 提交四个任务。它使用 [ComfyUI API 工作流](examples/g0/workflow_api.json)（不是用于拖入画布的 UI 工作流）。A/B 有深度组只更换输入图，无深度组只移除深度连接。均为 832×480、25 帧、24 fps、seed `20261127`。无深度的 A/B 输入相同；重复运行用于保持配对清晰，而不是两次独立试验。

```powershell
.\examples\g0\queue.ps1
```

</details>

## 无界面复现 / Headless reproduction

不打开桌面端或网页：用 MCP 摆位、检查预览并导出深度图，再通过本机 ComfyUI API 对比有／无深度的成片。示例脚本重放 AI 在这次试验中选定的坐标，不接受任意自然语言输入。

No desktop app or browser is needed: place actors, check the preview, and export depth via MCP, then queue a with/without-depth pair through the local ComfyUI API. The script replays the coordinates chosen by the AI for this test; it is not a general text-to-scene tool.

按上面的 G0 步骤准备模型并启动 ComfyUI，然后用已安装本项目依赖的 Python 运行 [复现脚本 / reproduction script](examples/headless/reproduce.py)。将路径替换为该实例实际使用的输入目录；若不是默认端口，追加 `--port 8189` 等参数。只导出控制图可追加 `--controls-only`。

Use the same models as G0 and start ComfyUI. Run the script with the project's Python dependencies installed, passing the actual ComfyUI input directory. Use `--port` for a non-default port or `--controls-only` to skip video jobs.

```powershell
.\.venv\Scripts\python.exe examples/headless/reproduce.py "X:\ComfyUI\input"
```

同提示词、同 seed（`20261128`），抽查第 1、13、25 帧：有深度时左侧人物明显更大，无深度时两人近似等高。这是一次构图遵从度对照，不代表画质提升。 / Same prompt and seed (`20261128`): in sampled frames 1, 13, and 25, the left actor appears clearly taller with depth and both are nearly equal in height without it. This tests framing adherence, not video quality.

![上：有深度；下：无深度 / Top: with depth; bottom: without depth](examples/headless/frames-1-13-25.jpg)

## 快速开始

需要 Python 3.11 和现代浏览器。控制数据由 CPU 计算；交互式 3D 使用 WebGL，不可用时可使用表单和二维视图。Three.js 随项目提供，运行编辑器不依赖 CDN。

在项目根目录运行（Windows PowerShell）：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe editor.py --open
```

安装依赖后也可双击 `启动 SpikeMark.cmd`。默认地址为 `http://127.0.0.1:8766`，可用 `--port` 更改端口。服务仅用于本机，不要通过代理或端口转发公开它。

## 从构图到导出

1. 在沙盒中布置人物和道具，调整沙盒尺寸、体型、姿态和输出相机。观察沙盒的工作视角与输出机位相互独立。
2. 使用站立、走路、挥手预设，或在时间轴上编辑自己的关键帧。无法用几何表达的动作可写入 `shot.action_text`；它只传递给下游，不会改变预览或控制图。
3. 查看预览并提交草稿，再下载场景 JSON 或当前镜头的控制 ZIP。可导入、切换和下载由 1–12 个独立镜头组成的集合，ZIP 每次只导出当前镜头。编辑不会覆盖原始场景文件。
4. 将场景或控制包交给下游。外部 AI 也可以返回结构化编辑提案；在工作台中预览并选择应用或拒绝。

## 外部接口与输出

[外部 AI 调用契约](AI_INTEGRATION.md)包含 MCP（stdio）、本机 HTTP、批量镜头和提案的配置及示例。场景 JSON 是权威输入；坐标采用米、Y 轴向上，帧从 0 开始。

静态 ZIP 含清单、场景、几何、米制深度（`.npy`）、实例 ID 蒙版和预览。动态 ZIP 在 `frames/` 下提供逐帧深度、蒙版和几何；动态导出最多 120 帧、累计 3200 万像素。几何含相机内外参和投影框；深度为相机坐标系 Z 值，0 表示无效。可选 VACE 深度图采用固定近远范围（近处较亮）；成片效果取决于下游模型和工作流。

也可从命令行编译示例场景，输出目录必须尚不存在：

```powershell
.\.venv\Scripts\python.exe spikemark.py scene.json output
```

## 许可

项目代码采用 [Apache License 2.0](LICENSE)；示例场景数据 `scene.json` 以 [CC0 1.0](https://creativecommons.org/publicdomain/zero/1.0/) 提供。随附的 Three.js 保留其 [MIT 许可声明](vendor/LICENSE)。
