## 2024-11-12 - Scrollable Container Keyboard Navigation
**Learning:** HTML elements with `overflow: auto` or `scroll` are inaccessible to keyboard-only users unless explicitly placed in the tab order, which prevents them from scrolling content when the main window does not have focus on the element.
**Action:** Always add `tabindex="0"`, `role="region"`, a descriptive `aria-label`, and visible `:focus-visible` styles to scrollable wrappers to ensure they are fully navigable and understandable by keyboard and screen reader users.
