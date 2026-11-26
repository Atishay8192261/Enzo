# ADR 0027 — Enzo uses a real browser: approved once per task, its own yes for what matters

- **Status:** Accepted (2026-11-26). Decided with Atishay (2026-11-20): laptop use is approved once
  per task; browser use is part of it, a real browser driven directly, in its own profile.
- **Spec:** §7.2.4 (live browser task), §4.2 (validation, errors as data, stop), §10.3 (no tool
  names), §12.2–12.5 (authority, gating, injection, discretion), §14 (honest limits)

## Context

`read_page` reads text; it cannot click, fill a cart or check out. Atishay wants Enzo to research,
compare prices and buy in a real browser, with one approval per task instead of one per click,
while anything irreversible still waits for the exact action.

## Decision

1. **Library: Playwright for Python 1.65.2** (PyPI, read 2026-11-25), driving Chromium over its
   pipe. Hand-built tools on it, no agent framework (ADR 0001).
2. **Which browser:** the user's installed Google Chrome, else Microsoft Edge, else Chromium, found
   at their usual install paths and launched by path; `ENZO_BROWSER_PATH` names another. No
   browser is downloaded (Playwright's own Chromium is used only if the user installed it). If
   none is found the step says so: "install Google Chrome, or set ENZO_BROWSER_PATH".
   - Why: nothing extra to install on a laptop that has Chrome (most do), and Google keeps it
     patched. A downloaded Chromium (~170 MB) would also need updating by Enzo.
3. **Its own profile** in `~/.local/share/enzo/browser` (0700). Before each launch Enzo switches
   off Chrome's password manager and address/card autofill there, so the profile never holds a
   saved password or card and Enzo can never type one. Logins the user makes in Enzo's window
   (cookies) persist in that profile.
4. **The user's logged-in Chrome profile is left out.** Chrome 136+ refuses automation of the
   default profile, and copying its cookies out is fragile and a large privacy step. Instead the
   user signs in themselves in Enzo's window when a site needs it. A permission switch for the
   real profile is not built.
5. **Window:** shown where there is a screen (a Mac, a Linux desktop), hidden on a server;
   `ENZO_BROWSER_HEADLESS=1/0` overrides. Chrome's own "controlled by automated software" bar
   shows on the window. Chrome's sandbox stays on (Playwright turns it off by default); only a
   browser run as root goes without, because Linux cannot sandbox it.
6. **Tools** (`enzo.browser.tools`): `browser_start` (task, exact sites, first page),
   `browser_open`, `browser_click`, `browser_type` (optionally Enter), `browser_select`,
   `browser_scroll`, `browser_back`, `browser_read`, `browser_close`. No screenshot tool: tool
   results are text only in Enzo's brain layer, so no model could see an image yet.
7. **What the brain sees:** Playwright's `aria_snapshot(mode="ai")`, an accessibility tree with
   element refs (`[ref=e5]`); refs stay with their element across snapshots. Cut at 10,000
   characters by default (up to 40,000 on request), like `read_page`. Passwords, card fields and
   one-time codes show only "filled (hidden)" or "card number ending 4242"; card numbers in page
   text keep their last four digits; a field Enzo cannot read is shown without its value; hidden
   inputs never appear. Every result is fenced as untrusted data (ADR 0011).
8. **Approve once per task.** `browser_start` is gated: the card names the task, the exact sites
   (flagging any on this computer or the local network) and what still asks. The other steps
   are not gated as tools; each one's `prepare` reads the live element, pins the call to it, and
   says whether *this* call needs a yes (`Prepared.ask`, a small loop change: a tool may decide
   per call). A step asks when:
   - **leaving the sites:** opening, or clicking a link to, a site outside the task's (a yes adds
     that site to the task);
   - **its label** says it buys or pays, sends or posts, deletes or cancels, or just "Confirm"
     (word lists in `enzo.browser.rules`);
   - **a submit** (a submit button, or Enter in a field) of a form that holds a password, card
     or one-time code, or of any form on a checkout/payment page (by URL path or title). The card
     lists what the form will send, field by field, secrets masked (§12.3, §12.5);
   - **typing** into a password, card (autocomplete `cc-*`, labels like "card number", "CVC",
     "expiry") or one-time-code field, or typing a Luhn-valid card number anywhere.
9. **Pins** (§12.3 "changed content asks again", ADR 0014): a step runs only if the element, its
   page address and, for a step that asks, its form's exact content still match what was checked
   (salted hash, so no form content is stored in the pin). A form changed while the card waited
   asks again; a page that changed under a plain step refuses it, and the brain looks again.
10. **Sites and the network:** a site covers its subdomains on the usual ports; a site with a port
    covers only that port. Every request is routed through a guard: a top-level visit outside the
    sites is blocked (the brain is told to ask with `browser_open`); any request into a private
    network (loopback, LAN, link-local, metadata) is blocked unless the task names that address;
    Enzo's own gateway port is never reachable from a page; WebSockets get the same check;
    service workers are blocked so nothing bypasses the guard. A guard that fails blocks.
11. **Page tricks:** dialogs are answered "no" and reported; file pickers and downloads are
    refused and reported; a new tab becomes the page Enzo is on.
12. **Lifetime and stop:** one browser task at a time (one profile). It ends when its turn ends,
    except while a step waits for the user's yes (its page stays open, closing after 10 idle
    minutes); a new message ends a waiting task; Esc (`/stop`) closes it at once, also while it
    waits, and a step cut off by that reports "stopped", not "failed"; gateway shutdown closes it;
    at start, browsers left on Enzo's profile by a crashed gateway are killed. These are
    `ConversationService` hooks (`turn_ended`, `moved_on`, `stop`).
13. **The sign:** while a task runs the gateway streams `browser` events (`{"site": …}`, `null`
    when it closes); the terminal shows "Enzo is using your browser: <site> · esc stops it", the
    web app a banner. Steps read "Opening shop.example/checkout", "Clicking “Add to cart”",
    "Typing into “Full name”": the page's own label, made printable and short, never a tool name.
14. **Runs inside the turn**, not asynchronously as §7.2.4 suggests: the user watches and can stop
    it, and an approval pauses it like any other step. Long unattended browsing waits for
    background runs on an always-on machine (`enzo deploy`).

## Limits (honest)

- The rules read what the page says. A site can mislabel a "Buy" button "Continue"; the checkout
  page and secret-field rules catch common cases, not all. Payment pages on another site (a
  payment provider) are outside the task's sites, so going there asks first.
- A server-side redirect from an approved site is followed by Chrome before Enzo sees it
  (Playwright routes only the first request of a redirect chain). Enzo then refuses to read or
  act on the page it landed on, but that site did receive the request.
- Whether a model ignores instructions planted in a page depends on the model (ADR 0011). The
  code guarantees the fence, and that following one (opening another site, submitting, buying)
  still waits for the user.
- Typed text, including a card number the user gave in chat, is in the tool call's arguments,
  so it is in the transcript and on the approval card's exact details; the card's summary and
  every page view mask it. Enzo never asks for a password in chat.
- One browser task at a time across all chats. Desktop apps (screen control) are not covered.
- Not verified here: a visible window on macOS, the user's real Chrome there, and CI runners'
  Chrome (tests run it without its sandbox; the default keeps it on).

## Consequences

- `playwright` (~50 MB wheel with its driver) joins the install; no browser download.
- Live check: a mock model drove the real gateway and Chromium through a local shop (one card for
  the task, the card number and "Place order" each paused with the exact action, the planted
  instruction's visit paused and declined, Esc closed the browser, the terminal showed the sign).
