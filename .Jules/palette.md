
## 2024-06-07 - Missing ARIA Attributes on Icon-Only Buttons
**Learning:** Found a recurring pattern in the application's HTML templates where icon-only buttons (containing only SVGs without text) lack explicit `aria-label` attributes, making them inaccessible to screen readers.
**Action:** Added `aria-label` attributes matching the `title` text to the icon buttons, and added `aria-hidden="true"` to the inner SVG elements to improve screen reader compatibility. Always check for this pattern when adding or modifying UI components.
