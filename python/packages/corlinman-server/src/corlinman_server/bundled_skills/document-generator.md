---
name: document-generator
description: 把内容生成为干净美观的 PDF / 文档报告的标准流程。触发词：总结成pdf、生成pdf、做一份报告、导出文档、生成word、做个文档、整理成文件、export pdf、generate report、make a document、写个报告发我。核心：先写 Markdown，再用 corlinman-md2pdf 渲染，绝不手写 PDF 字节、绝不用 reportlab、中文要用正确字体。
metadata:
  openclaw:
    emoji: "📄"
---

# Document Generator · 文档/PDF 生成

当用户要你把内容「总结成 PDF / 生成报告 / 导出文档」时，**严格按下面的流程**，不要即兴发挥。

## 标准流程（首选）

1. **先把报告内容写成 Markdown** 文件（`write_file`，写到工作区，例如 `report.md`）。
   用正常 Markdown：`#`/`##` 标题、`-`/`1.` 列表、`|` 表格、`**加粗**`、\`代码\`、`>` 引用。
   内容用中文就直接写中文，不需要做任何转义或加空格。
2. **渲染成 PDF**：运行
   ```
   corlinman-md2pdf report.md report.pdf --title "报告标题"
   ```
   这是项目内置命令（已在 PATH 上），用 Chrome 无头模式渲染，**自动处理中文字体**，
   输出干净排版的 PDF。成功会打印 `[md2pdf] wrote report.pdf (N bytes)`。
3. **检查再发送**：发送前必须打开或渲染检查 PDF，至少确认首页：
   - 中文字体正常，没有乱码、异常空格或逐字拆散。
   - 标题、段落、表格、代码块没有重叠、裁切、挤出页面。
   - 页面留白、层级、行高可读，不是密密麻麻的一屏文字。
   如果发现任何问题，先改 Markdown/HTML 结构再重新渲染。
4. **发送**：用 `send_attachment` 把 `report.pdf` 发给用户。

## 绝对不要做（这些就是上次把 PDF 弄乱的原因）

- ❌ **不要手写/拼接 PDF 字节流**，也不要自己写脚本逐字符画 PDF——会导致每个字之间
  出现多余空格、排版错乱。
- ❌ **不要用 `reportlab`**（环境里没有 pip，装不上）。
- ❌ **不要用裸 `--headless`**（旧写法会崩溃 exit 133）。
- ❌ 不要因为第一次没成功就降级去手画 PDF——先排查命令，再用下面的兜底方案。

## 兜底方案（万一 `corlinman-md2pdf` 不可用）

手动走同一条可靠链路：把内容写成 **带中文字体的 HTML**，再用 Chrome 渲染：

```bash
# 1) 写 report.html，<head> 里必须带这个字体栈，中文才不会乱：
#    body{font-family:'Noto Sans CJK SC','WenQuanYi Micro Hei','WenQuanYi Zen Hei',sans-serif;
#         line-height:1.7;} @page{size:A4;margin:18mm 16mm;}
# 2) 渲染（注意是 --headless=new）：
google-chrome --headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage \
  --no-pdf-header-footer --print-to-pdf=report.pdf "file://$PWD/report.html"
```

## 其它格式

- 需要 **图片/海报/幻灯片**：同时遵守 `visual-output-quality` 的防重叠验收；
  复杂视觉稿用 `huashu-design` 技能（HTML→图片/视频）。
- 需要纯文本/Markdown 交付：直接 `write_file` 后 `send_attachment` 发 `.md`。
