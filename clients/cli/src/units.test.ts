import { describe, expect, it } from "bun:test";
import { lastCodeBlock } from "./clipboard.ts";
import { COMMAND_LIST, echoOf } from "./commands.ts";
import { suggest } from "./complete.ts";
import { urlsOnTheirOwnLines } from "./links.ts";
import { expand, normalize, shouldCollapse, tagFor } from "./paste.ts";
import { BRAINS, CONNECTIONS } from "./testing.tsx";

const sources = { connections: CONNECTIONS, brains: BRAINS };

describe("completion (#34)", () => {
  it("lists commands for /, and only matching ones as you type", () => {
    expect(suggest("/", sources).map((s) => s.label)).toEqual(COMMAND_LIST.map((c) => c.usage));
    expect(suggest("/LO", sources).map((s) => s.value)).toEqual(["/login ", "/logout "]);
    expect(suggest("/new", sources)).toEqual([
      expect.objectContaining({ value: "/new", complete: true }),
    ]);
    expect(suggest("/zz", sources)).toEqual([]);
  });

  it("never pops up for a message, or past the argument", () => {
    expect(suggest("hello /login", sources)).toEqual([]);
    expect(suggest("/login\nmore", sources)).toEqual([]);
    expect(suggest("/login xai extra", sources)).toEqual([]);
    expect(suggest("/help me", sources)).toEqual([]);
  });

  it("completes each command's argument from what is really there", () => {
    expect(suggest("/login ", sources).map((s) => s.label)).toEqual([
      "chatgpt",
      "anthropic",
      "xai",
    ]);
    expect(suggest("/logout ", sources).map((s) => s.label)).toEqual(["anthropic"]); // connected only
    expect(suggest("/check a", sources).map((s) => s.value)).toEqual(["/check anthropic"]);
    expect(suggest("/model ", sources).map((s) => s.label)).toEqual([
      "auto",
      "fast",
      "balanced",
      "powerful",
      "claude-sonnet",
      "grok",
    ]);
    expect(suggest("/model g", sources)[0]?.description).toContain("needs /login xai");
    expect(suggest("/details ", sources).map((s) => s.value)).toEqual([
      "/details on",
      "/details off",
    ]);
    expect(suggest("/copy c", sources).every((s) => s.complete)).toBe(true);
  });
});

describe("pastes (#35)", () => {
  it("collapses long pastes and puts every one back when sending", () => {
    expect(shouldCollapse("one line")).toBe(false);
    expect(shouldCollapse("a\nb")).toBe(false);
    expect(shouldCollapse("a\nb\nc")).toBe(true);
    expect(shouldCollapse("x".repeat(800))).toBe(true);
    const first = { tag: tagFor("a\nb\nc", []), text: "a\nb\nc" };
    const second = { tag: tagFor("x".repeat(900), [first]), text: "x".repeat(900) };
    expect([first.tag, second.tag]).toEqual(["[Pasted 3 lines]", "[Pasted 900 characters #2]"]);
    expect(expand(`see ${first.tag} and ${second.tag}`, [first, second])).toBe(
      `see a\nb\nc and ${"x".repeat(900)}`,
    );
  });

  it("a pasted text that contains a tag is never expanded again", () => {
    const tricky = { tag: "[Pasted 3 lines]", text: "[Pasted 3 lines #2]\nb\nc" };
    const other = { tag: "[Pasted 3 lines #2]", text: "SECOND" };
    expect(expand("[Pasted 3 lines]", [tricky, other])).toBe("[Pasted 3 lines #2]\nb\nc");
    expect(expand("no tags", [])).toBe("no tags");
  });

  it("terminal line breaks become plain ones", () => {
    expect(normalize("a\r\nb\rc\nd")).toBe("a\nb\nc\nd");
  });
});

describe("copying the last code block", () => {
  it("takes the last fenced block, with backticks or tildes", () => {
    expect(lastCodeBlock("x\n```ts\nconst a = 1;\n```\ny\n~~~\nls\n~~~\n")).toBe("ls");
    expect(lastCodeBlock("```\none\ntwo\n```")).toBe("one\ntwo");
    expect(lastCodeBlock("no code")).toBeNull();
  });
});

describe("a command's echo never shows a key", () => {
  it("shows the arguments a command takes, and only the name otherwise", () => {
    expect(echoOf("/model claude-sonnet")).toBe("/model claude-sonnet");
    expect(echoOf("/login chatgpt new")).toBe("/login chatgpt new");
    expect(echoOf("/login xai sk-secret-value")).toBe("/login");
    expect(echoOf("/models extra words")).toBe("/models");
    expect(echoOf(`/${"k".repeat(60)}`)).toHaveLength(30); // unknown: cut short
  });
});

describe("long links (#56)", () => {
  const url = "https://example.com/a/very/long/path/to-an-article";
  const link = { url };
  const span = (text: string, linked = false) => ({
    text,
    code: false,
    ...(linked ? { link } : {}),
  });
  const shown = (spans: ReturnType<typeof span>[]) =>
    urlsOnTheirOwnLines(spans)
      .map((s) => s.text)
      .join("");

  it("puts a long address on a line of its own, keeping the words around it", () => {
    expect(shown([span("Read "), span(url, true), span(" today.")])).toBe(`Read\n${url}\ntoday.`);
    expect(shown([span(url, true), span(" first")])).toBe(`${url}\nfirst`);
    expect(shown([span("last: "), span(url, true)])).toBe(`last:\n${url}`);
    expect(shown([span("Read "), span(url, true), span(" now "), span("please")])).toBe(
      `Read\n${url}\nnow please`,
    );
  });

  it("keeps a titled link's title and drops the brackets around its address", () => {
    const titled = [span("See "), span("Ars", true), span(" "), span("(", true), span(url, true)];
    expect(shown([...titled, span(")", true), span(" now")])).toBe(`See Ars\n${url}\nnow`);
  });

  it("leaves short links, links in text and table cells alone", () => {
    const short = { url: "https://x.ai" };
    const spans = [span("at "), { text: "https://x.ai", code: false, link: short }, span(" ok")];
    expect(urlsOnTheirOwnLines(spans)).toEqual(spans);
    const titled = [span("the "), span("docs", true), span(" page")];
    expect(urlsOnTheirOwnLines(titled)).toEqual(titled);
    const cell = [span("Read "), span(url, true)];
    expect(urlsOnTheirOwnLines(cell, { tableCell: true })).toEqual(cell);
  });
});
