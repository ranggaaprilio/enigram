# Web UI design reference

Decided 2026-11-08 (roadmap item 4, stage U1); replaces the Framer marketing spec used since
2026-11-05. Enigram is a work tool people keep open all day: the reference is a dense, quiet app
(Linear, the Vercel dashboard), not a landing page.

## Principles

- **Content first.** Chrome is neutral and thin; colour carries meaning (status, origin of a
  sentence, the one primary action), never decoration. No gradients, no spotlight cards.
- **Dense, not cramped.** 14px body, compact tables and controls, whitespace to group, not to
  impress.
- **One way per pattern.** The same page header, empty, loading and error states and feedback on
  every page (U2).
- **Both themes are first class.** Light, dark, or the system's (default); the choice is kept per
  browser and every colour is a token defined for both.

## App frame

- Left sidebar, open by default, collapses to icons (⌘B / Ctrl+B or the toggle); its state is kept
  per browser.
  - Top: the workspace picker and *New workspace*.
  - Middle: page groups. *Overview* (Dashboard, the home page: `/` and a workspace without a
    page open it), *Answers* (Ask, Trace), *Library* (Documents, Knowledge, Graph, Memory),
    *Review* (Inbox) and *Settings* (Members).
  - Bottom: the user menu (who is signed in, the role in this workspace, the theme).
- Below 768px the sidebar is a drawer opened from the menu button; the top bar shows the page
  title.
- Content is left-aligned next to the sidebar, at most 1140px wide, with 16px gutters on a phone.
- Routes stay `/w/<workspace>/<page>`.

## Page patterns (U2)

- **Where you are**: the top bar's breadcrumb, `workspace / page / item` (a query, a symbol, a
  note), each part a link back.
- **Page header** (`PageHeader`): title, one sentence on what the page is for, the main action on
  the right.
- **Loading** (`Loading`): skeleton rows where the content will come, never a bare "Loading…".
- **Empty** (`Empty`): what is missing and, when there is one, the next step as a button.
- **Errors** (`ErrorAlert`) stay where they happened, next to what failed.
- **Go anywhere** (U3): ⌘K / Ctrl+K, or *Search* in the top bar, opens the command palette:
  pages, other workspaces, and the code, entities and documents (`GET /targets`).
- **Numbers** (Dashboard, D2): a tile is a label, one number and a line of context, and links to
  the page that lists what it counts. A daily chart is one series of thin bars from the baseline
  in the foreground colour, every day of the period shown (quiet days empty); hover a bar, or
  focus the chart (one Tab stop) and use the arrow keys, Home and End, to read a day; otherwise
  the line above the chart gives the period's total. No chart library.
- **Outcome of an action**: a toast at the bottom right (saved, deleted, queued); work that runs
  on (jobs) also keeps its row on the page.

## Type

Inter Variable (bundled), with the character variants `cv01 cv05 cv09 cv11 ss03` and tabular
figures in tables.

| Use | Size / weight |
|---|---|
| Body, table cells, controls | 14px / 400, line height 1.45 |
| Secondary text, captions, badges | 12–13px |
| Page title (`h1`) | 20px / 600 |
| Section title (`h2`) | 16px / 600 |
| Answers (rendered markdown) | 15px, line height 1.6, max 72ch: they are read, not scanned |

## Colour

Neutral greys for surfaces and text; one blue accent for focus rings and links; status colours
only for status (success, info, warning, danger) and the origin badges of answers (code, document,
summary, human, unsupported).

| Token | Light | Dark | Use |
|---|---|---|---|
| `background` | `#ffffff` | `#0b0b0c` | Page |
| `sidebar` | `#f7f7f8` | `#111113` | Sidebar, top bar on a phone |
| `card` / `muted` | `#f4f4f5` | `#18181b` | Cards, empty states, code |
| `secondary` / `accent` | `#ececee` | `#232327` | Hover, selected item |
| `foreground` | `#09090b` | `#fafafa` | Text |
| `muted-foreground` | `#5f5f66` | `#a1a1aa` | Secondary text (AA on `background` and `card`) |
| `border` | `#e4e4e7` | `#27272a` | Hairlines |
| `ring` / `link` | `#2563eb` | `#60a5fa` | Focus, links |

The primary button is the foreground colour (black in light, white in dark). Destructive actions
are neutral buttons with red text, filled red only in a confirmation.

## Shape and depth

Radius 8px on controls and cards, 6px on badges; borders instead of shadows, a shadow only on
floating layers (menus, popovers, the drawer).

## Components

shadcn/ui (new-york) on Radix, Tailwind v4, lucide icons at 16px. New pieces are added with the
shadcn CLI, then imports point at `@/lib/utils` and versions stay pinned in `package.json`.

## Checks per stage

Light and dark, 1360px and 390px wide, no horizontal scroll, keyboard reaches every control with a
visible focus ring.

## Accessibility (U4)

- **WCAG 2.2 AA**, checked with axe-core on every page in both themes, and on the states that
  open on demand (palette, note form, account menu, new workspace, phone drawer, sign-in).
- **Contrast**: secondary text uses `muted-foreground` (AA on `background`, `card` and `sidebar`),
  never a lighter opacity of the foreground. A link inside a sentence of muted text is underlined.
- **Landmarks**: the sidebar's app name and workspace switch are a labelled region, the pages a
  `nav`, the page `main`; every page has one `h1` and headings go down one level at a time (an
  answer's own markdown headings start at `h3`, under a hidden "Answer" `h2`).
- **Keyboard**: *Skip to content* is the first Tab stop. Every Tab stop shows the focus ring. A
  dialog, drawer or menu gives focus back to what opened it when it closes. A chart is one Tab
  stop, read with the arrow keys. Menus are not modal: the page stays readable behind them.
