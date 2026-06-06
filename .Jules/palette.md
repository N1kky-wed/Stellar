## 2024-06-06 - Initial setup
**Learning:** Establishing the journal for tracking UX/a11y learnings.
**Action:** Always document critical patterns and rejections here.
## 2024-06-06 - Accessible Icon-only Buttons
**Learning:** Icon-only buttons in the application's HTML templates (e.g., buttons containing only SVGs without text) often lack explicit `aria-label` attributes. When creating or modifying UI components, ensure proper `aria-label` attributes are added to the buttons, and `aria-hidden="true"` is applied to the inner SVG/icon elements for optimal screen reader compatibility.
**Action:** Always verify that buttons lacking visible text have descriptive `aria-label`s and their purely decorative children are hidden from assistive tech.
