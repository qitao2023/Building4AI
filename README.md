# Building4AI — AI Native Structural Design

AI 驱动的建筑结构设计工具。当前聚焦「AI 楼梯设计」。

## 架构

```
Revit → IFC → 自动分析楼梯间 → DeepSeek AI 设计 → Blender/IFC 生成
```

- **AI 大脑**：DeepSeek（deepseek-chat / deepseek-v4-pro），负责任参数决策
- **几何内核**：ifcopenshell + Blender/Bonsai，负责精确几何生成
- **规范检查**：确定性代码，GB 50010

## 项目结构

```
webapp/
  index.html      SaaS 界面（零依赖，浏览器打开）
  viewer.html     三维查看 + 框选楼梯间
  server.py       后端 API（IFC 分析 / AI 设计 / IFC 生成）
ai_stair_addon/
  __init__.py     Blender 插件（导入 IFC → AI 设计 → 3D 预览）
ifc_to_json.py    IFC → JSON 转换器
json_to_ifc.py    JSON → IFC 生成器
stair_generator.py 楼梯生成器 + 规范检查
structure_demo.ifc 测试用 4 柱 4 梁结构模型
```

## 快速开始

### Web 应用
```bash
cd webapp
python server.py          # 启动后端 (http://localhost:8765)
# 浏览器打开 index.html 或 viewer.html
```

### Blender 插件
1. Blender → Preferences → Add-ons → Install from Disk
2. 选择 ai_stair_addon.zip
3. 3D 视口按 N → AI Stair 面板

## 技术栈

| 层 | 技术 | 许可 |
|----|------|------|
| AI | DeepSeek API | - |
| 数据标准 | IFC / ifcJSON | ISO 16739 |
| 几何引擎 | ifcopenshell + Blender | LGPL / GPL |
| 前端 3D | Three.js | MIT |
| 后端 | Python stdlib http.server | PSF |

## License

MIT
