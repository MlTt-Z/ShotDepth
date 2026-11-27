# SpikeMark

SpikeMark 是一款本地 3D 走位和构图工具。你可以摆放人物、道具和机位，再把深度、实例蒙版和相机几何交给外部 AI 视频工作流。**SpikeMark 不生成视频，不下载或附带模型权重，也不评价成片。**

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
