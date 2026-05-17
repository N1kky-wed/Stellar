---
name: frontend-design-mandate
description: >
  Master frontend design mandate. Use when building web components, pages, applications, dashboards,
  landing pages, or any UI. Covers design philosophy, animation engineering, component patterns,
  performance, accessibility, CSS mastery, and anti-slop enforcement. Generates creative,
  production-grade code with exceptional craft and zero generic AI aesthetics.
---

# Frontend Design Mandate

This mandate governs the creation of distinctive, production-grade frontend interfaces. It merges design philosophy, animation engineering, component architecture, performance guardrails, and aesthetic enforcement into a single authoritative reference.

---

## 0. Design Thinking (Start Here)

Before any code, answer these:

- **Purpose**: What problem does this solve? Who uses it?
- **Register**: Is this **brand** (marketing, landing, campaign — design IS the product) or **product** (app, dashboard, tool — design SERVES the product)?
- **Tone**: Commit to an extreme. Brutally minimal. Maximalist chaos. Retro-futuristic. Organic. Luxury. Playful. Editorial. Brutalist. Art deco. Industrial. Pick one and execute it with full conviction.
- **Differentiation**: What is the one thing someone will remember?

**CRITICAL**: Bold maximalism and refined minimalism both work. The key is intentionality, not intensity. Match implementation complexity to the aesthetic vision — maximalism needs elaborate code, minimalism needs precision.

---

## 1. Aesthetic Laws (Universal)

### Color

- Use OKLCH. Reduce chroma as lightness approaches 0 or 100.
- Never use `#000` or `#fff`. Tint every neutral toward the brand hue (chroma 0.005–0.01 is enough).
- Never use pure black (`#000000`). Use off-black, Zinc-950, or charcoal.
- Max 1 accent color. Saturation < 80%. Desaturate accents to blend elegantly with neutrals.
- **THE LILA BAN**: AI Purple / Blue aesthetics are strictly banned. No purple button glows, no neon gradients. Use absolute neutral bases (Zinc/Slate) with high-contrast singular accents (Emerald, Electric Blue, Deep Rose).
- No outer glows or neon `box-shadow`. Use inner borders or subtle tinted shadows.
- Color strategy before color selection:
  - **Restrained**: tinted neutrals + one accent ≤10% (product default)
  - **Committed**: one saturated color carries 30–60% of the surface (brand identity pages)
  - **Full palette**: 3–4 named roles, each deliberate (campaigns, data viz)
  - **Drenched**: the surface IS the color (brand heroes)

### Typography

- Avoid generic fonts: Inter, Roboto, Arial, system fonts are banned. Use `Geist`, `Outfit`, `Cabinet Grotesk`, `Satoshi`, or similarly distinctive choices.
- Pair a distinctive display font with a refined body font.
- Display/Headlines: `text-4xl md:text-6xl tracking-tighter leading-none`.
- Body: `text-base leading-relaxed max-w-[65ch]`.
- Hierarchy through scale + weight contrast (≥1.25 ratio between steps). No flat scales.
- Cap body line length at 65–75ch.
- Serif fonts are banned for Dashboard/Software UIs. Use exclusively high-end Sans-Serif pairings.
- No oversized H1s that scream. Control hierarchy with weight and color, not just massive scale.
- No gradient text (`background-clip: text`). Use a single solid color.

### Layout

- Vary spacing for rhythm. Same padding everywhere is monotony.
- Cards only when elevation communicates hierarchy. Nested cards are always wrong.
- No 3-column equal card grids. Use 2-column zig-zag, asymmetric grid, or horizontal scroll instead.
- No centered Hero/H1 layouts when variance is high. Force split screen, left-aligned, or asymmetric whitespace.
- No complex flexbox percentage math (`w-[calc(33%-1rem)]`). Use CSS Grid.
- `max-w-[1400px] mx-auto` or `max-w-7xl` for page layout containment.
- No `h-screen` for Hero sections. Use `min-h-[100dvh]` to prevent iOS Safari layout jumping.
- No modal as a first-resort. Exhaust inline and progressive alternatives first.
- Don't wrap everything in a container. Most things don't need one.

### Backgrounds & Visual Depth

- Create atmosphere rather than defaulting to solid colors.
- Apply: gradient meshes, noise textures, geometric patterns, layered transparencies, dramatic shadows, decorative borders, grain overlays.
- Glassmorphism: go beyond `backdrop-blur`. Add a 1px inner border (`border-white/10`) and subtle inner shadow (`shadow-[inset_0_1px_0_rgba(255,255,255,0.1)]`) to simulate edge refraction. Use purposefully, not decoratively as default.

### Copy

- Every word earns its place. No restated headings, no intros that repeat the title.
- No em dashes. Use commas, colons, semicolons, periods, or parentheses.
- No filler words: "Elevate", "Seamless", "Unleash", "Next-Gen". Use concrete verbs.
- No generic names (John Doe, Sarah Chan). Use creative, realistic-sounding names.
- No fake round numbers (99.99%, 50%). Use organic data (47.2%, +1 (312) 847-1928).
- No startup slop names: "Acme", "Nexus", "SmartFlow". Invent premium, contextual names.
- No emojis — anywhere. Replace with high-quality icons (Radix, Phosphor) or clean SVG primitives.

### Theme

Dark vs. light is never a default. Write one sentence of physical scene: who uses this, where, under what ambient light, in what mood. If the sentence doesn't force the answer, add more detail until it does.

---

## 2. The AI Slop Test

If someone could look at this interface and say "AI made that" without doubt, it has failed.

**Category-reflex check** — run at two altitudes:

- **First-order**: if someone can guess the palette from the category alone ("observability → dark blue", "healthcare → white + teal"), rework until the answer isn't obvious.
- **Second-order**: if someone can guess the aesthetic family from category + anti-references, rework further. Both checks must fail before the design passes.

**Absolute bans** — match and refuse:
- Side-stripe borders (`border-left`/`border-right` > 1px as a colored accent on cards or alerts). Rewrite with full borders, background tints, leading numbers/icons, or nothing.
- Gradient text (`background-clip: text` + gradient). Never.
- Glassmorphism as default decoration.
- The hero-metric template (big number, small label, supporting stats, gradient accent).
- Identical card grids (icon + heading + text, repeated).
- Modal as first thought.
- Broken Unsplash links. Use `https://picsum.photos/seed/{random_string}/800/600` or SVG UI Avatars.
- Custom mouse cursors. Outdated and ruin performance/accessibility.

---

## 3. Animation Decision Framework

### Step 1: Should this animate at all?

| Frequency | Decision |
|---|---|
| 100+ times/day (keyboard shortcuts, command palette) | No animation. Ever. |
| Tens of times/day (hover effects, list navigation) | Remove or drastically reduce |
| Occasional (modals, drawers, toasts) | Standard animation |
| Rare/first-time (onboarding, celebrations) | Can add delight |

Never animate keyboard-initiated actions.

### Step 2: What is the purpose?

Valid purposes only:
- Spatial consistency (toast enters/exits from same direction)
- State indication (morphing feedback button)
- Explanation (marketing animation showing how a feature works)
- Feedback (button scales down on press)
- Preventing jarring changes (elements appearing without transition feel broken)

If the answer is "it looks cool" and users see it often: don't animate.

### Step 3: What easing?

```
Element entering or exiting?
  Yes → ease-out (starts fast, feels responsive)
  No →
    Moving/morphing on screen?
      Yes → ease-in-out (natural acceleration/deceleration)
    Hover/color change?
      Yes → ease
    Constant motion (marquee, progress bar)?
      Yes → linear
    Default → ease-out
```

**Never use ease-in for UI animations.** It starts slow, making the interface feel sluggish. A dropdown with `ease-in` at 300ms feels slower than `ease-out` at 300ms.

Use custom easing curves — built-in CSS easings are too weak:

```css
--ease-out: cubic-bezier(0.23, 1, 0.32, 1);
--ease-in-out: cubic-bezier(0.77, 0, 0.175, 1);
--ease-drawer: cubic-bezier(0.32, 0.72, 0, 1);
```

Resources: [easing.dev](https://easing.dev/) or [easings.co](https://easings.co/).

Apply premium spring physics for interactive elements: `type: "spring", stiffness: 100, damping: 20`.

### Step 4: Duration

| Element | Duration |
|---|---|
| Button press feedback | 100–160ms |
| Tooltips, small popovers | 125–200ms |
| Dropdowns, selects | 150–250ms |
| Modals, drawers | 200–500ms |
| Marketing/explanatory | Can be longer |

UI animations stay under 300ms. A 180ms dropdown feels more responsive than a 400ms one. A faster spinner makes the app feel faster even when load time is identical.

---

## 4. Component Patterns

### Buttons

```css
.button {
  transition: transform 160ms ease-out;
}
.button:active {
  transform: scale(0.97);
}
```

Scale should be subtle (0.95–0.98). Applies to any pressable element.

### Entry animations

Never animate from `scale(0)`. Start from `scale(0.95)` with `opacity: 0`.

```css
/* Bad */
.entering { transform: scale(0); }

/* Good */
.entering { transform: scale(0.95); opacity: 0; }
```

### Popovers — origin-aware

Popovers scale from their trigger, not from center. Modals stay centered.

```css
/* Radix UI */
.popover { transform-origin: var(--radix-popover-content-transform-origin); }

/* Base UI */
.popover { transform-origin: var(--transform-origin); }
```

### Tooltips — skip delay on subsequent hovers

```css
.tooltip {
  transition: transform 125ms ease-out, opacity 125ms ease-out;
  transform-origin: var(--transform-origin);
}
.tooltip[data-starting-style],
.tooltip[data-ending-style] {
  opacity: 0;
  transform: scale(0.97);
}
.tooltip[data-instant] {
  transition-duration: 0ms;
}
```

### CSS transitions over keyframes for interruptible UI

```css
/* Interruptible — good */
.toast { transition: transform 400ms ease; }

/* Not interruptible — avoid for dynamic UI */
@keyframes slideIn { from { transform: translateY(100%); } }
```

### Blur for imperfect crossfades

When a crossfade feels off, add `filter: blur(2px)` during transition. It bridges the visual gap by blending two states. Keep under 20px — heavy blur is expensive in Safari.

### `@starting-style` for entry animation

```css
.toast {
  opacity: 1;
  transform: translateY(0);
  transition: opacity 400ms ease, transform 400ms ease;

  @starting-style {
    opacity: 0;
    transform: translateY(100%);
  }
}
```

### Stagger

```css
.item {
  opacity: 0;
  transform: translateY(8px);
  animation: fadeIn 300ms ease-out forwards;
}
.item:nth-child(1) { animation-delay: 0ms; }
.item:nth-child(2) { animation-delay: 50ms; }
.item:nth-child(3) { animation-delay: 100ms; }

@keyframes fadeIn {
  to { opacity: 1; transform: translateY(0); }
}
```

Keep stagger delays 30–80ms. Never block interaction during stagger.

### Asymmetric enter/exit timing

```css
/* Release: fast */
.overlay { transition: clip-path 200ms ease-out; }

/* Press: slow and deliberate */
.button:active .overlay { transition: clip-path 2s linear; }
```

### `translateY` with percentages

Percentage values in `translate()` are relative to the element's own size. Prefer over hardcoded pixel values.

```css
.drawer-hidden { transform: translateY(100%); }
.toast-enter   { transform: translateY(-100%); }
```

---

## 5. Spring Animations

Springs feel more natural than duration-based animations because they simulate real physics.

**When to use**: drag interactions, elements that should feel "alive", gestures that can be interrupted mid-animation, decorative mouse-tracking interactions.

```jsx
import { useSpring } from 'framer-motion';

// With spring: natural momentum
const springRotation = useSpring(mouseX * 0.1, {
  stiffness: 100,
  damping: 10,
});
```

**Configuration:**

```js
// Apple's approach (easier to reason about)
{ type: "spring", duration: 0.5, bounce: 0.2 }

// Traditional physics (more control)
{ type: "spring", mass: 1, stiffness: 100, damping: 10 }
```

Keep bounce subtle (0.1–0.3). No bounce in most UI contexts. Springs maintain velocity when interrupted — CSS keyframes restart from zero.

---

## 6. `clip-path` for Animation

### The `inset` shape

```css
/* Fully hidden from right */
.hidden  { clip-path: inset(0 100% 0 0); }
/* Fully visible */
.visible { clip-path: inset(0 0 0 0); }
```

### Use cases

- **Tabs with perfect color transitions**: Duplicate the tab list. Style the copy as "active." Clip so only the active tab is visible. Animate the clip on tab change.
- **Hold-to-delete**: `clip-path: inset(0 100% 0 0)` → `inset(0 0 0 0)` over 2s linear on `:active`. Snap back with 200ms ease-out on release.
- **Image reveals on scroll**: Start `inset(0 0 100% 0)`, animate to `inset(0 0 0 0)` via `IntersectionObserver`.
- **Comparison sliders**: Clip the top image based on drag position.

---

## 7. Gesture & Drag Interactions

### Momentum-based dismissal

```js
const timeTaken = new Date().getTime() - dragStartTime.current.getTime();
const velocity = Math.abs(swipeAmount) / timeTaken;

if (Math.abs(swipeAmount) >= SWIPE_THRESHOLD || velocity > 0.11) {
  dismiss();
}
```

### Rules

- Apply damping at boundaries — things in real life slow down, not stop.
- Use pointer capture once dragging starts.
- Ignore additional touch points mid-drag (multi-touch protection).
- Apply friction instead of hard stops for overscroll.

---

## 8. Performance Rules

### Animate only `transform` and `opacity`

These skip layout and paint, running on the GPU. Never animate `padding`, `margin`, `height`, or `width`.

### CSS variables and style recalculation

```js
// Bad: triggers recalc on all children
element.style.setProperty('--swipe-amount', `${distance}px`);

// Good: only affects this element
element.style.transform = `translateY(${distance}px)`;
```

### Framer Motion hardware acceleration

```jsx
// NOT hardware accelerated
<motion.div animate={{ x: 100 }} />

// Hardware accelerated
<motion.div animate={{ transform: "translateX(100px)" }} />
```

### CSS animations beat JS under load

CSS animations run off the main thread. Use CSS for predetermined animations; JS for dynamic, interruptible ones.

### Web Animations API (WAAPI) for programmatic CSS animations

```js
element.animate(
  [{ clipPath: 'inset(0 0 100% 0)' }, { clipPath: 'inset(0 0 0 0)' }],
  { duration: 1000, fill: 'forwards', easing: 'cubic-bezier(0.77, 0, 0.175, 1)' }
);
```

### DOM cost

Apply grain/noise filters exclusively to fixed, `pointer-events-none` pseudo-elements. Never on scrolling containers.

### Z-index restraint

Never spam arbitrary `z-50` or `z-10`. Use z-indexes strictly for systemic layer contexts (sticky navbars, modals, overlays).

### Framer Motion isolation

Wrap perpetual motion / infinite loops in isolated `React.memo` Client Components. Never trigger re-renders in the parent layout.

---

## 9. Architecture & Conventions

- **Dependency verification**: Before importing any 3rd party library, check `package.json`. If missing, output the install command before code.
- **Framework**: React or Next.js. Default to Server Components (RSC).
- **RSC safety**: Global state works only in Client Components. Wrap providers in `"use client"` components.
- **Interactivity isolation**: If motion or glass effects are active, extract those components as isolated leaf `"use client"` components.
- **Styling**: Tailwind CSS (v3/v4). Check `package.json` for version. Do not use v4 syntax in v3 projects. For v4, use `@tailwindcss/postcss`, not `tailwindcss` plugin in postcss.
- **Icons**: Use `@phosphor-icons/react` or `@radix-ui/react-icons`. Standardize `strokeWidth` globally (1.5 or 2.0 exclusively).
- **No HTML `<form>` tags in React artifacts**. Use `onClick`, `onChange` handlers.
- **Breakpoints**: `sm`, `md`, `lg`, `xl` standardized.
- **GSAP/ThreeJS**: Use only for isolated full-page scrolltelling or canvas backgrounds, wrapped in strict `useEffect` cleanup blocks. Never mix with Framer Motion in the same component tree.

---

## 10. Interaction States (Mandatory)

LLMs naturally generate only the success state. Always implement:

- **Loading**: Skeletal loaders matching layout sizes. No generic circular spinners.
- **Empty states**: Beautifully composed, indicating how to populate data.
- **Error states**: Clear, inline error reporting.
- **Tactile feedback**: `-translate-y-[1px]` or `scale-[0.98]` on `:active`.
- **Forms**: Label above input. Helper text optional but in markup. Error text below input. `gap-2` for input blocks.

---

## 11. Accessibility

```css
@media (prefers-reduced-motion: reduce) {
  .element {
    animation: fade 0.2s ease;
    /* No transform-based motion */
  }
}
```

```jsx
const shouldReduceMotion = useReducedMotion();
const closedX = shouldReduceMotion ? 0 : '-100%';
```

Gate hover animations on touch devices:

```css
@media (hover: hover) and (pointer: fine) {
  .element:hover { transform: scale(1.05); }
}
```

---

## 12. Animation Review Checklist

| Issue | Fix |
|---|---|
| `transition: all` | Specify exact properties: `transition: transform 200ms ease-out` |
| `scale(0)` entry | Start from `scale(0.95)` with `opacity: 0` |
| `ease-in` on UI element | Switch to `ease-out` or custom curve |
| `transform-origin: center` on popover | Set to trigger location or use Radix/Base UI CSS variable |
| Animation on keyboard action | Remove entirely |
| Duration > 300ms on UI element | Reduce to 150–250ms |
| Hover animation without media query | Add `@media (hover: hover) and (pointer: fine)` |
| Keyframes on rapidly-triggered element | Use CSS transitions |
| Framer Motion `x`/`y` props under load | Use `transform: "translateX()"` for hardware acceleration |
| Same enter/exit speed | Make exit faster than enter |
| Elements all appear at once | Add stagger delay (30–80ms) |
| `transition: all` + popover origin | Two separate issues — fix both |

---

## 13. Creative Arsenal Reference

Pull from these when the task calls for it. Never converge on generic UI. Choose what fits the aesthetic direction.

**Navigation**: Mac OS Dock magnification, magnetic buttons, gooey menus, Dynamic Island pill, contextual radial menus, floating speed dial, mega menu reveals.

**Layout**: Bento grids (asymmetric, tile-based), masonry, chroma grids, split screen scroll, curtain reveal heroes.

**Cards**: Parallax tilt, spotlight border, glassmorphism panel, holographic foil, Tinder swipe stack, morphing modal.

**Scroll**: Sticky scroll stack, horizontal scroll hijack, zoom parallax, scroll progress path, liquid swipe transition.

**Galleries**: Dome gallery, Coverflow carousel, drag-to-pan grid, accordion image slider, hover image trail, glitch effect.

**Typography**: Kinetic marquee, text mask reveal, text scramble, circular text path, gradient stroke animation, kinetic typography grid.

**Micro-interactions**: Particle explosion button, liquid pull-to-refresh, skeleton shimmer, directional hover-aware button, ripple click, animated SVG line drawing, mesh gradient background, lens blur depth.

---

## 14. Debugging Animations

- **Slow motion**: Temporarily increase duration to 2–5x. Use browser DevTools animation inspector.
- **Frame-by-frame**: Chrome DevTools Animations panel — step through to reveal timing issues.
- **Real devices**: For touch interactions, test on physical hardware via USB + Safari remote devtools. Simulator is a fallback.

Things to check in slow motion:
- Do colors transition smoothly or do two states overlap?
- Is easing correct, or does it start/stop abruptly?
- Is `transform-origin` correct?
- Are multiple animated properties in sync?

Review your work the next day. You notice imperfections with fresh eyes that you missed during development.

---

## 15. Pre-Flight Checklist

Before outputting any UI:

- [ ] Is global state used only to avoid deep prop-drilling, not arbitrarily?
- [ ] Does mobile layout collapse (`w-full`, `px-4`, `max-w-7xl mx-auto`) for high-variance designs?
- [ ] Do full-height sections use `min-h-[100dvh]` instead of `h-screen`?
- [ ] Do `useEffect` animations have strict cleanup functions?
- [ ] Are empty, loading, and error states implemented?
- [ ] Are cards omitted in favor of spacing where possible?
- [ ] Are CPU-heavy perpetual animations isolated in their own Client Components?
- [ ] Does the design pass both levels of the AI slop category-reflex check?
- [ ] Are all absolute bans (side-stripe borders, gradient text, hero-metric template, etc.) absent?
- [ ] Is the font choice non-generic (no Inter, Roboto, Arial)?
- [ ] Does the color strategy match the chosen commitment level (Restrained/Committed/Full/Drenched)?
