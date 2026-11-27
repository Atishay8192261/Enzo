# ADR 0033 — Enzo can see: images in messages and tool results

- **Status:** Accepted (2026-11-27)
- **Issue:** #53 (images in the input box, sent to models that take images; say so before sending
  when the model can't)
- **Spec:** §1.3.2 (translation per provider, never claim a capability), §5 (images count against
  the budget, compaction), §10.3 (plain words), §11.2 (media with provenance), §12.5 (privacy)
- **Builds on:** ADR 0002 (adapters), 0010 (routing), 0017 (terminal), 0024 (compaction, stored
  texts), 0027 (browser: it left out a screenshot step only because images were text-only)

## Decision

| # | Decision | Why (evidence) |
|---|---|---|
| 1 | **Canonical `ImagePart`** in user messages and in `ToolResult.images`. A message refers to a kept image by id (`img_` + 16 hex of its hash) with type and size; its bytes (`data`) and OpenAI's `detail` are filled in per call and are excluded from every dump and repr, so they are never stored in the transcript, logged or shown. The user's words stay the first part, then their images, then the context. | One image, many brains: each gets its own copy. Checked in the installed SDKs: `anthropic` 2.9 takes `{"type":"image","source":{"type":"base64",…}}` in user content and in `tool_result.content`; `openai` 3.53 Responses takes `input_image` with a data URL and `detail`, also inside `function_call_output.output`. |
| 2 | **Per-model vision in `brains.toml`**: `[vision.<profile>]` (max edge, patch size, patch budget, multiplier, byte limit, detail, sourced), named by each brain. Claude 5.5: high-resolution tier, 2,576 px, 4,784 tokens of 28 px. GPT-6.2 Astra/Sol, GPT-6 Luna: images per their model pages; not in the sizing table, so the strictest documented `high` (2,048 px, 2,500 patches of 32 px, ×1.2), `detail: high`. Grok 4.7 and Kimi K3/K3.1: documented as taking images (via search: their docs are not reachable here); limits and cost unpublished, so 1,568 px and counted ×2. | §1.3.2: a capability is claimed only with a source. Anthropic's published examples and OpenAI's are tests (Anthropic's table says 1269x952 for 2000x1500; its own reference code gives 1270x952, which the test follows). |
| 3 | **Enzo downscales before sending** (`enzo.images.sizing.fit`: Anthropic's reference search, generalised to a patch budget) and counts each image as billed: ⌈⌈w/p⌉·⌈h/p⌉·multiplier⌉. Estimates and exact counts include images; their bytes never count as text. | The provider never resizes on its own, so cost and coordinates are known (§5: images count against the budget). A wrong-low estimate is corrected by usage reports (ADR 0024 #1). |
| 4 | **Routing**: a prompt with an image goes only to brains with a vision profile (primary and fallback). A pinned brain that cannot see, or no connected brain that can, raises `CantSeeImagesError`: **before sending**, the gateway answers 422 with plain words ("… can't see images, so nothing was sent. Choose a model that can with /model …") and saves nothing, so the message stays with the user. An image is never dropped. | Issue #53 asks for the warning before sending. Clients also know ahead (`/brains` has `vision`) and warn on the chip. |
| 5 | **Storage** (`enzo.images.ImageStore`, `~/.local/share/enzo/images`, 0700/0600): an image is decoded with Pillow (13.0, added), turned upright (EXIF orientation) and rebuilt from its pixels only, so EXIF, GPS, XMP, ICC and text chunks are gone before it is stored or sent. Photos stay JPEG, everything else PNG. Limits: 20 MB, 50 megapixels (refused from the header, before decoding), kept at most 4,096 px. A sidecar records when, from what input (pasted, file, screenshot, tool), which tool and brain, which chat, and what was stripped. Each brain's copy is written once next to it, so its bytes are identical on every call (caches stay warm). | §11.2 media provenance; §12.5 nothing private leaves in metadata; a missing file becomes a visible note, never a silent drop. |
| 6 | **Upload first**: `POST /images` (raw body; name and source in headers, so they never reach an access log), then `POST …/turns` with `images: [ids]` (at most 10; a message may be only images). | Errors (not an image, too big) show when attaching, not after sending. |
| 7 | **Compaction**: folded turns' images become a line in the summary (`img_…: an image the user sent in turn 2: <what it showed>`; the summarizer reads a pointer, never the image, and describes it from the messages); an invariant keeps every image pointer; the bytes are no longer sent. `view_image` brings one back for a brain that can see. Until a summary, images stay in the prompt like any other content (append-only, ADR 0024 #11). | §5.2 selective retention: pointer plus a short description, never resent forever. |
| 8 | **Tools that show images** (`view_image`, `browser_screenshot`) are marked `needs_vision` and offered only when the turn's brain can see. The screenshot is the visible viewport, taken only on an approved site, with password, card and one-time-code fields blacked out (Playwright `mask`), kept with provenance (tool, brain, chat). | ADR 0027 #6 left the screenshot out only for want of images. |
| 9 | **Clients**: the terminal takes a dragged file (the path a terminal pastes: escaped, quoted, `file://`), or Ctrl+V from the clipboard (OpenTUI's native clipboard: X11, Wayland, AppKit; else `osascript`, `wl-paste`, `xclip`), as an `[Image N]` tag with a chip line above the input; deleting the tag removes the image; the transcript shows the tag, or `[image]`. The web app takes paste, drop or a picked file, with previews. | Issue #53. |

## Limits (honest)

- Not verified live: real Claude/GPT vision (no keys here), the ChatGPT plan route with images,
  and whether xAI's and Moonshot's Responses routes accept `input_image` (their docs show Chat
  Completions; ADR 0002 #11 already marks those routes unproven). A refusal there is an honest
  error, not a drop.
- The macOS clipboard path (OpenTUI's AppKit read, then `osascript`) is untested here: the laptop
  check covers it. Linux X11 was checked live (Xvfb + xclip).
- A card number printed in page text (not in a field) is visible in a screenshot; the text view
  masks it (ADR 0027 #7). Image ids are unguessable hashes, but `view_image` does not check the
  chat an id belongs to.
- HEIC photos are not read (Pillow needs a plugin): the user is told to use PNG, JPEG, GIF,
  WebP or BMP.
