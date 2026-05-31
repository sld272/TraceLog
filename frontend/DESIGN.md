# TraceLog 拾迹 — Web 前端设计文档

## 设计灵感

### Dieter Rams（Braun）
极致功能主义。每个元素都有明确目的。圆角与柔和几何形态，中性色为主体配以极少量功能色。
- 版式：大量留白，极细分隔线区分层级，卡片圆角 12-14px
- 色彩：暖灰色系主体，琥珀/赭石色作为唯一 accent
- 动态：200ms ease-out，克制无弹跳

### SANAA（妹岛和世 & 西�的立卫）
透明性与轻盈感。层叠的半透明平面，光线穿透感。
- 版式：三栏之间通过背景透明度和 backdrop-blur 区分
- 色彩：极浅色彩层叠，SOUL 用极淡彩色底色区分
- 动态：fade-in + 极轻微 scale(0.98→1.0)

## Design Tokens

### 色彩系统
- 背景：#faf9f7 (primary) / #f3f1ee (secondary) / #ffffff (card)
- 文字：#1a1a1a (primary) / #5c5c5c (secondary) / #8c8c8c (tertiary)
- Accent：#c2703a (warm ochre)
- 边框：rgba(0,0,0,0.06) / rgba(0,0,0,0.12)

### 字体
- 主字体：Inter + Noto Sans SC
- 等宽：JetBrains Mono
- 尺寸：0.75rem ~ 2rem，行高 1.3 ~ 1.8

### 间距（4px 基准）
- space-1 到 space-16（0.25rem ~ 4rem）

### 圆角
- sm: 6px / md: 10px / lg: 14px / xl: 20px / full: 9999px

### 响应式断点
- Desktop ≥1200px：三栏
- Tablet 768-1199px：隐藏右栏
- Mobile <768px：单栏 + 抽屉导航
