## 2026-10-27 - Initialization
**Learning:** Initializing the Palette journal.
**Action:** Ready to implement micro-UX changes.

## 2026-10-27 - Scrollable Container Accessibility in Reports
**Learning:** When creating or modifying scrollable wrapper containers (`overflow: auto` or `scroll`) in HTML reports, keyboard accessibility is critical. They require `tabindex="0"`, `role="region"`, a descriptive `aria-label` in Traditional Chinese, and a visible `:focus-visible` CSS outline to ensure keyboard users can navigate and scroll them. Python f-strings require double braces `{{}}` for CSS.
**Action:** Always ensure keyboard accessibility by adding `tabindex="0"`, `role="region"`, a descriptive `aria-label` in Traditional Chinese, and a visible `:focus-visible` CSS outline to scrollable containers.
