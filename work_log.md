# Work log

## Task: fix project-card overflow on the Projects home screen

`.pcard` (the project card in `tools/framer/index.html`'s `renderProjectsList()`)
was overflowing: the title and long webtoons.com URL rendered past both edges
of the card, with no ellipsis and no avatar visible.

### Root cause

`.pcard` (and `.recentitem`, the sidebar's matching recent-projects row) are
real `<button>` elements, and the app's base stylesheet has a generic
```
button { display:inline-flex; align-items:center; gap:7px; padding:8px 11px; ... }
```
My `.pcard`/`.recentitem` class rules override `display`/`gap`/`padding` (class
beats tag-selector specificity) but never set `align-items` themselves - so
`align-items:center` from that generic rule won by default, instead of the
`stretch` I'd assumed. With `align-items:center` on a column-direction flex
container, its children (`.ptop`/`.pbottom` in the card; the name/progress rows
in the recent-list item) size to their own **content width** rather than
stretching to the card's width - and since `.ptop` contains a `white-space:nowrap`
URL span with no break opportunities, its content width was the full,
un-wrapped URL (~500px), centered inside a ~300px card, overflowing evenly past
both edges (clipped title on the left was this, not a text-direction bug).

While tracing this I also hardened the actual truncation chain: `min-width:0`
was missing on `.pcard` itself (a CSS Grid item) and on `.pcard .ptop` (a nested
flex container) - both needed for the ellipsis truncation on `.pname`/`.purl`
to actually take effect, since a flex/grid item's default `min-width:auto`
otherwise floors it at its content's min-content size regardless of an
ancestor's `overflow:hidden`/`text-overflow:ellipsis`.

### Fix (`tools/framer/index.html`, CSS only)

- Added `align-items:stretch` to `.pcard` and `.recentitem`.
- Added `min-width:0` to `.pcard`, `.pcard .ptop`, `.pcard .pbottom`, `.pcard .pmeta`,
  `.pcard .pcount` (plus `overflow:hidden` on `.pcard` itself as a defensive
  clip, and ellipsis/`white-space:nowrap`/`flex:none` tuning on `.pcount`/`.popened`
  so "N/M done" and the timestamp never crowd each other).

### Verification

Loaded the app in a real browser (Chrome extension) against the existing
"The Stellar Swordmaster" project (a genuinely long webtoons.com URL with a
query string). Confirmed via screenshot and a `getBoundingClientRect()` check
that the URL row's rect is now fully inside the card's rect (no overflow),
the avatar initial renders, the title is intact, and "1/128 done · 7m ago" is
cleanly laid out. Also confirmed the sidebar's "Recent" progress bar (same
underlying bug, `.recentitem`) now renders full-width instead of collapsing.

Note: a ReCapper instance was already running on port 5005 from earlier
testing (not started by me this session) - I verified the fix against it but
left it running rather than kill a process I didn't start, in case it's the
user's own open session.
