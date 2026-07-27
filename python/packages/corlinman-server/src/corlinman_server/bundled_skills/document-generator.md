---
name: document-generator
description: 把内容生成为干净美观的 PDF / 文档报告的标准流程。触发词：总结成pdf、生成pdf、做一份报告、导出文档、生成word、做个文档、整理成文件、export pdf、generate report、make a document、写个报告发我。核心：用 render_document 工具（Markdown 进、PDF 出），绝不手写 PDF 字节、绝不自己调渲染引擎。
metadata:
  openclaw:
    emoji: "📄"
---

# Document Generator · 文档/PDF 生成

当用户要你把内容「总结成 PDF / 生成报告 / 导出文档」时，走下面的流程。
（文档**文件内容**用完整 Markdown 排版；聊天里的**回复文字**仍遵守人设的输出风格，两者互不冲突。）

## 标准流程

1. **把报告内容写成 Markdown**，直接作为 `render_document` 的 `content` 参数传入。
   用正常 Markdown：`#`/`##` 标题、`-`/`1.` 列表、`|` 表格、`**加粗**`、\`代码\`、`>` 引用。
   内容用中文就直接写中文，不需要做任何转义或加空格。
2. **调用 `render_document`**（可选 `title` / `path`）。工具内部走可靠的
   CJK 渲染管线，自动处理中文字体与页面排版，并校验产物是合法 PDF；
   成功会返回工作区内的输出路径。
3. **检查再发送**：发送前打开或渲染检查 PDF 首页——中文无乱码、表格代码块
   无重叠裁切、留白与行高可读。发现问题就改 Markdown 结构后重新渲染。
4. **发送**：用 `send_attachment` 把返回的路径发给用户。

## 约束

- PDF 一律由 `render_document` 生成。不要手拼 PDF 字节、不要用 reportlab、
  不要自己调 Chrome/WeasyPrint——工具失败时会返回明确的错误信息，按提示处理
  或如实告知用户，不要绕过工具即兴渲染。
- 需要**图片/海报/幻灯片**：遵守 `visual-output-quality` 的防重叠验收；
  复杂视觉稿用 `huashu-design` 技能（HTML→图片/视频）。
- 需要纯文本/Markdown 交付：直接 `write_file` 后 `send_attachment` 发 `.md`。
