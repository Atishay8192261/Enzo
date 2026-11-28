# ADR 0029 — Enzo shares only what the receiver needs: an outbound check and the user's rules

- **Status:** Accepted (2026-11-27).
- **Spec:** §12.5 (privacy and discretion), §12.7 third check, §17 "Privacy", §6.5, §11.2, §14.
  Builds on ADR 0011 (untrusted fence), 0014 (approvals), 0020 (memory), 0026 (Gmail/Calendar),
  0027 (browser).

## Context

The prompt already said "share only what they need to act", but nothing in code looked at what
left the computer. A model that over-shares ("table for 2 … my wife is pregnant") would put a
private detail on a card the user might approve without reading, and an ungated send (a web
search, a page address, a typed form field) would carry it with no card at all.

## Decision

1. **One hook in the loop.** A tool declares what it sends to a third party (`Tool.outbound`:
   texts, each with how it leaves: `message`, `field`, `search`, `address`). Before a call runs,
   `_gate` checks its prepared arguments (plus `Prepared.sends`, e.g. a forwarded email's own
   text). Declared: `web_search` (query), `read_page`, `browser_start`/`browser_open` (the
   address's path and query), `browser_type` (the text and what the field asks for), every MCP
   write (Gmail: subject, body, forward note and the forwarded text; Calendar: title,
   description, location as a field, a reply's note; GitHub and other servers: every string
   argument except what the write acts on). Drafts and reads are not checked.
2. **Never a block.** A gated action's card says what it includes, one plain line each ("This
   includes a health detail: “My wife is pregnant”"), above the exact details (terminal, web,
   and reopened cards: `approvals.privacy`, migration 0012). An ungated send that carries a
   detail asks first, on a card. A no tells the brain why ("do it again without those details";
   for a gated card: "offer to do it without those details, do not do it again unasked").
3. **Deterministic detection, no model classifier.** Patterns for health, money, relationships
   and sexuality, religion and politics, ID and card numbers (masked on the card: "ending
   4242"), secrets (never repeated), phone and email; plus what Enzo knows is private: names of
   people in memory (with who they are: "your wife") and those the user named in this chat ("my
   sister Priya"), the user's addresses from memory or this chat, memories quoted word for word
   (4 words in a row, messages only, a note not a question), and the user's rules. A classifier
   would cost a model call per send (and ChatGPT plan quota), send the text to another call, and
   answer differently each time.
4. **What counts depends on how the text leaves.**
   - message: every kind except contact details (a receiver needs a way to answer);
   - field: the same, minus what the field asks for by its label, name or autocomplete hint (an
     address in "Street address", a name in "Name", an allergy in "Dietary needs");
   - search and address: identifiers always; a topic only when the text ties it to the user ("my
     pregnant wife" asks; "pregnancy-safe restaurants" does not: it says nothing about anyone).
   - An address counts only when it is the user's (known, or the text says "I live at…"), so a
     venue's address in an email is not flagged.
5. **Discretion rules (§12.5).** `add_discretion_rule` / `remove_discretion_rule` need the user's
   exact words from the message being answered, checked in code (case, curly quotes and spacing
   aside; at least 10 characters; never in a scheduled run). So a rule is never inferred, and a
   page or an email can neither make nor lift one. A rule covers kinds and/or words, for this
   chat or every chat (`discretion_rules`, migration 0012). Rules ride with every message as a
   note, and the check applies them everywhere: a rule-covered detail always asks, whatever a
   field asks for. `/privacy` lists the rules that hold in the chat; `/privacy lift` removes one
   after a yes (keep is first). `GET /privacy?conversation=`, `DELETE /privacy/{id}`.
6. **A no is final.** A call the user declined never runs, even if its check would no longer ask
   (a memory forgotten while the card waited). The service passes the declined call ids to the
   loop (`Store.declined_calls`).
7. **A check that fails asks** ("Enzo could not check this for private details").
8. **Prompt:** a "Privacy and discretion" section: the receiver's needs only (a restaurant gets a
   name, a time, a party size; a need goes in without its private reason), no memory in group or
   public writing unless asked, searches carry the topic only, rules only from the user's words.
9. **Logs:** `privacy.flagged` (kinds, count, rule ids, channels), `privacy.rule_saved`,
   `privacy.rule_removed`, `privacy.unchecked`: never the words.

## Limits (honest)

- Words, not meaning. Missed: a detail in other words ("we're expecting", "my numbers came back
  high"), a name Enzo never heard, a detail split across fields. Flagged anyway: a word in
  another sense ("a cancer charity run"), a name that is also a word at the start of a sentence
  ("Will you…" when a brother is Will). A flag is a line on a card or one question, never a block.
- Searches about a topic, without saying it is about the user, are not flagged, even when memory
  says it is (a diabetic user searching "diabetic desserts").
- A search the user dictated with a personal detail still asks once: Enzo cannot tell dictation
  from over-sharing.
- "Needed" is read from a form field's own label: a site can mislabel a field. In email there is
  no such signal, so every private detail is named on the card.
- Only text Enzo sends is checked: what a site already holds, or learns from the user's own
  sign-in, is out of reach.

## Consequences

- §12.7's third check is a test (`tests/privacy/test_restaurant.py`): the over-shared booking
  email waits on a card naming the pregnancy; after a no, the email that goes out has only a name,
  a time and the party size.
- Tools that send text elsewhere must declare `outbound`; the wiring test lists them.
