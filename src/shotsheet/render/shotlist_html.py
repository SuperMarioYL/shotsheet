"""Printable two-column storyboard skeleton: timeline/镜头 and 字幕.

m1 only knows shot boundaries and transcript text, so the sheet deliberately has
two columns. Later milestones add rule flags and vision fields to the same table.
"""

from __future__ import annotations

from jinja2 import Template

from shotsheet.schema import ShotSheet

_TEMPLATE = Template(
    """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>分镜表 · {{ sheet.source.file }}</title>
<style>
  @page { size: A4; margin: 14mm; }
  body {
    margin: 0;
    color: #111;
    font-family: -apple-system, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    font-size: 11pt;
  }
  header { margin-bottom: 8mm; }
  h1 { margin: 0 0 2mm; font-size: 18pt; }
  .meta { color: #555; font-size: 9pt; }
  table { width: 100%; border-collapse: collapse; }
  th, td { border: 1px solid #888; padding: 2.5mm 3mm; vertical-align: top; text-align: left; }
  th { background: #eee; font-size: 9pt; }
  thead { display: table-header-group; }
  tr { break-inside: avoid; }
  .timeline { width: 42%; }
  .tc { font-variant-numeric: tabular-nums; }
  .index { color: #555; margin-right: 2mm; }
  .dur { color: #555; font-size: 9pt; }
  .caption:empty::after { content: "—"; color: #bbb; }
</style>
</head>
<body>
<header>
  <h1>分镜表</h1>
  <p class="meta">
    {{ sheet.source.file }} · {{ sheet.source.platform.value }} ·
    {{ sheet.source.fps }} fps · {{ "%.2f"|format(sheet.source.duration_ms / 1000) }}s ·
    共 {{ sheet.shots|length }} 镜
  </p>
</header>
<table>
  <thead>
    <tr><th class="timeline">时间码 / 镜头</th><th>字幕</th></tr>
  </thead>
  <tbody>
  {% for shot in sheet.shots %}
    <tr>
      <td class="timeline">
        <span class="index">#{{ "%02d"|format(shot.index + 1) }}</span>
        <span class="tc">{{ shot.tc_in }} → {{ shot.tc_out }}</span>
        <div class="dur">{{ "%.2f"|format(shot.duration_ms / 1000) }}s</div>
      </td>
      <td class="caption">{{ shot.caption.text if shot.caption else "" }}</td>
    </tr>
  {% endfor %}
  </tbody>
</table>
</body>
</html>
""",
    autoescape=True,
)


def render_shotlist_html(sheet: ShotSheet) -> str:
    """Render SHOT as a standalone printable HTML page."""
    return _TEMPLATE.render(sheet=sheet)
