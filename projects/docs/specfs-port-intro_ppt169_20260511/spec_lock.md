# Execution Lock

## canvas
- viewBox: 0 0 1280 720
- format: PPT 16:9

## colors
- bg: #F7FAFC
- secondary_bg: #EDF2F7
- primary: #1A365D
- accent: #C53030
- secondary_accent: #2C7A7B
- text: #2D3748
- text_secondary: #4A5568
- text_tertiary: #718096
- border: #CBD5E0
- success: #2C7A7B
- warning: #C53030
- card_bg: #FFFFFF
- text_dark_bg: #A0AEC0

## typography
- font_family: "Microsoft YaHei", "PingFang SC", Arial, sans-serif
- emphasis_family: Georgia, "Microsoft YaHei", serif
- code_family: Consolas, "Courier New", monospace
- body: 18
- title: 32
- subtitle: 24
- annotation: 14
- cover_title: 60
- chapter_title: 45
- chapter_number: 120
- hero_text: 96
- page_number: 11

## icons
- library: chunk-filled
- inventory: file-text, code, circle-checkmark, circle-x, arrow-right, hierarchy, shield, arrow-clockwise, clipboard-check, chart-bar, gear, layers, magnifying-glass, lightning

## page_rhythm
- P01: anchor
- P02: dense
- P03: dense
- P04: dense
- P05: dense
- P06: dense
- P07: dense
- P08: anchor
- P09: dense
- P10: dense
- P11: dense
- P12: dense
- P13: dense
- P14: dense
- P15: dense
- P16: dense
- P17: dense
- P18: dense
- P19: dense
- P20: dense
- P21: anchor
- P22: anchor

## forbidden
- Mixing icon libraries
- rgba()
- `<style>`, `class`, `<foreignObject>`, `textPath`, `@font-face`, `<animate*>`, `<script>`, `<iframe>`, `<symbol>`+`<use>`
- `<g opacity>` (set opacity on each child element individually)
- HTML named entities in text (`&nbsp;`, `&mdash;`, `&copy;`, `&ndash;`, `&reg;`, `&hellip;`, `&bull;` …) — write as raw Unicode (`—`, `©`, `→`, NBSP, etc.); XML reserved chars `& < > " '` must be escaped as `&amp; &lt; &gt; &quot; &apos;`
