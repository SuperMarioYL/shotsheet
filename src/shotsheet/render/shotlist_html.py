"""Printable shot sheet: timeline/镜头, 字幕, and platform-rule columns.

The table is shared with the local web page (``render_shot_table``); the page
wraps it in its own shell, while ``render_shotlist_html`` produces a standalone
A4 document. Vision fields sit under the timecode so the printed sheet still
reads as one row per shot.
"""

from __future__ import annotations

from collections.abc import Sequence

from jinja2 import Template

from shotsheet.pipeline.rules import Violation
from shotsheet.schema import Camera, Movement, ShotSheet, ShotSize

_STYLES = """
  @page { size: A4; margin: 10mm; }
  body {
    margin: 0;
    color: #111;
    font-family: -apple-system, "PingFang SC", "Noto Sans SC", "Microsoft YaHei", sans-serif;
    font-size: 9pt;
    line-height: 1.35;
  }
  header { margin-bottom: 3mm; }
  h1 { margin: 0 0 1mm; font-size: 14pt; }
  .meta { color: #555; font-size: 7.5pt; }
  table { width: 100%; border-collapse: collapse; }
  th, td {
    border: 1px solid #888;
    padding: 1.1mm 1.6mm;
    vertical-align: top;
    text-align: left;
  }
  th { background: #eee; font-size: 7.5pt; }
  thead { display: table-header-group; }
  tr { break-inside: avoid; page-break-inside: avoid; }
  .timeline { width: 34%; }
  .caption { width: 38%; }
  .rule { width: 28%; }
  .tc { font-variant-numeric: tabular-nums; }
  .index { color: #555; margin-right: 2mm; }
  .dur { color: #555; font-size: 7.5pt; }
  .vision { margin-top: 0.8mm; font-size: 8pt; }
  .props { color: #333; font-size: 7.5pt; margin-top: 0.5mm; }
  .caption:empty::after { content: "—"; color: #bbb; }
  .violation { font-size: 7.5pt; margin: 0 0 0.8mm; }
  .violation:last-child { margin-bottom: 0; }
  .violation.error { color: #b00020; }
  .violation.warning { color: #8a5300; }
  .ok { color: #2e6b33; font-size: 7.5pt; }
  .sheet-violations { margin: 0 0 3mm; }
"""

_TABLE = Template(
    """<table>
  <thead>
    <tr>
      <th class="timeline">时间码 / 镜头</th>
      <th class="caption">字幕</th>
      <th class="rule">规则</th>
    </tr>
  </thead>
  <tbody>
  {% for shot in sheet.shots %}
    <tr>
      <td class="timeline">
        <span class="index">#{{ "%02d"|format(shot.index + 1) }}</span>
        <span class="tc">{{ shot.tc_in }} → {{ shot.tc_out }}</span>
        <div class="dur">{{ "%.2f"|format(shot.duration_ms / 1000) }}s</div>
        {% if editable %}
        <div class="vision">
          <select class="cell" data-shot="{{ shot.index }}" data-field="shot_size">
            <option value="">—</option>
            {% for size in shot_sizes %}
            <option
              value="{{ size.value }}"
              {% if shot.shot_size == size %}selected{% endif %}>{{ size.value }}</option>
            {% endfor %}
          </select> ·
          <select class="cell" data-shot="{{ shot.index }}" data-field="camera">
            <option value="">—</option>
            {% for option in cameras %}
            <option
              value="{{ option.value }}"
              {% if shot.camera == option %}selected{% endif %}>{{ option.value }}</option>
            {% endfor %}
          </select> ·
          <select class="cell" data-shot="{{ shot.index }}" data-field="movement">
            <option value="">—</option>
            {% for option in movements %}
            <option
              value="{{ option.value }}"
              {% if shot.movement == option %}selected{% endif %}>{{ option.value }}</option>
            {% endfor %}
          </select>
        </div>
        <div class="props">道具：<span class="cell" contenteditable="true"
          data-shot="{{ shot.index }}" data-field="props">{{ shot.props|join("、") }}</span></div>
        <div class="props">场景：<span class="cell" contenteditable="true"
          data-shot="{{ shot.index }}" data-field="scene">{{ shot.scene or "" }}</span></div>
        {% else %}
        <div class="vision">
          {{ shot.shot_size.value if shot.shot_size else "—" }} ·
          {{ shot.camera.value if shot.camera else "—" }} ·
          {{ shot.movement.value if shot.movement else "—" }}
        </div>
        {% if shot.props %}<div class="props">道具：{{ shot.props|join("、") }}</div>{% endif %}
        {% if shot.scene %}<div class="props">场景：{{ shot.scene }}</div>{% endif %}
        {% endif %}
      </td>
      <td class="caption">
        {% if shot.caption %}
          <div>{{ shot.caption.text }}</div>
          <div class="dur">
            {{ shot.caption.in_ms }}–{{ shot.caption.out_ms }} ms
            {% if shot.caption.safe_area_ok is false %} · 安全区越界
            {% elif shot.caption.safe_area_ok %} · 安全区通过{% endif %}
          </div>
        {% endif %}
      </td>
      <td class="rule">
        {% for violation in shot_violations.get(shot.index, []) %}
          <div class="violation {{ violation.severity }}">
            [{{ violation.rule_id }}] {{ violation.message }}
          </div>
        {% else %}
          <span class="ok">通过</span>
        {% endfor %}
      </td>
    </tr>
  {% endfor %}
  </tbody>
</table>""",
    autoescape=True,
)

_PAGE = Template(
    """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>分镜表 · {{ sheet.source.file }}</title>
<style>"""
    + _STYLES
    + """</style>
</head>
<body>
<header>
  <h1>分镜表</h1>
  <p class="meta">
    {{ sheet.source.file }} · {{ sheet.source.platform.value }} ·
    {{ sheet.source.fps }} fps · {{ "%.2f"|format(sheet.source.duration_ms / 1000) }}s ·
    共 {{ sheet.shots|length }} 镜{% if sheet.rules %} · {{ sheet.rules }}{% endif %}
  </p>
  {% if sheet_violations %}
  <div class="sheet-violations">
    {% for violation in sheet_violations %}
      <div class="violation {{ violation.severity }}">
        [{{ violation.rule_id }}] {{ violation.message }}
      </div>
    {% endfor %}
  </div>
  {% endif %}
</header>
{{ table|safe }}
</body>
</html>
""",
    autoescape=True,
)


def render_shot_table(
    sheet: ShotSheet, violations: Sequence[Violation] = (), *, editable: bool = False
) -> str:
    """Render just the shot table; used by both the print page and the web UI.

    ``editable`` swaps the vision fields for inline controls carrying
    ``data-shot`` / ``data-field`` attributes; the web page listens for changes
    and persists them. The printable page keeps plain text.
    """
    shot_violations: dict[int, list[Violation]] = {}
    for violation in violations:
        if violation.shot_index is not None:
            shot_violations.setdefault(violation.shot_index, []).append(violation)
    return _TABLE.render(
        sheet=sheet,
        shot_violations=shot_violations,
        editable=editable,
        shot_sizes=list(ShotSize),
        cameras=list(Camera),
        movements=list(Movement),
    )


def render_shotlist_html(sheet: ShotSheet, violations: Sequence[Violation] = ()) -> str:
    """Render SHEET as a standalone printable HTML page, including rule flags."""
    sheet_violations = [v for v in violations if v.shot_index is None]
    return _PAGE.render(
        sheet=sheet,
        table=render_shot_table(sheet, violations),
        sheet_violations=sheet_violations,
    )
