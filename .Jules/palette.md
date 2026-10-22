## 2025-02-15 - Scrollable Wrappers Need Manual Keyboard Accessibility
**Learning:** In generated HTML reports, wrapper elements using `overflow: auto` or `scroll` for tables and charts are not naturally keyboard focusable in some browsers, breaking keyboard navigation for users relying on arrows keys to scroll.
**Action:** Always add `tabindex="0"`, an appropriate `role="region"`, a localized `aria-label`, and a visible `:focus-visible` outline to any scrollable container.
