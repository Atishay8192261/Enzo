"""Real-model check of compaction (spec §5.3), for a laptop with a connected Claude or ChatGPT account.

Run it against a running Enzo (`enzo`, or `cd server && uv run enzo-server`), from the server folder:

    uv run python ../scripts/compaction-check.py [http://127.0.0.1:8787]

It lowers nothing in Enzo: to make a real model's chat long enough to compact without spending
much, start the gateway with a small trigger first, e.g. a copy of brains.toml with
`[context] compact_at = 20000` and `keep_recent = 4000`, via ENZO_BRAINS_CONFIG=/path/to/copy.
Cost with the defaults: about 60 short turns plus one or two summaries, well under $1 on a
balanced model. It uses a fresh chat and prints PASS/FAIL per check; nothing is deleted.
"""

import json
import sys
import time

import httpx2

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8787"
DECISION = "We decide to fly to Lisbon on Friday the 12th, window seats."
results: dict[str, bool] = {}
seen = {"compacted": 0, "failed": 0}


def turn(c: httpx2.Client, chat: str, words: str) -> dict:
    done: dict = {}
    with c.stream("POST", f"/conversations/{chat}/turns", json={"message": words}) as r:
        r.raise_for_status()
        event = None
        for line in r.iter_lines():
            if line.startswith("event: "):
                event = line[7:]
            elif line.startswith("data: ") and event:
                data = json.loads(line[6:])
                if event == "compacted":
                    seen["compacted" if data["ok"] else "failed"] += 1
                    print("  ·", data["text"])
                if event == "done":
                    done = data
    return done


with httpx2.Client(base_url=BASE, timeout=300) as c:
    chat = c.post("/conversations", json={"kind": "main"}).json()["id"]
    print("chat", chat)
    padding = (
        "Here is some background for the trip, no need to act on it: "
        + "we like quiet places, good coffee, walking tours and museums. " * 30
    )
    script = {
        2: DECISION + " Please just confirm in one line.",
        3: "Book nothing yet, but note: a dinner table for 3 people on Saturday.",
        4: "Actually, make it 4 people, not 3.",
    }
    for n in range(1, 61):
        words = script.get(n, f"Note {n}. {padding} Reply with one word: ok.")
        t0 = time.time()
        done = turn(c, chat, words)
        print(f"{n:2d} {done.get('stop')} {done.get('model')} {time.time() - t0:.1f}s", flush=True)
        if seen["compacted"] >= 1 and n >= 52:
            break
    a = turn(c, chat, "Without searching anything: what did we decide about the flight?")
    b = turn(c, chat, "And how many people is the dinner table for?")
    print("decision:", a.get("text"))
    print("people:", b.get("text"))
    results["the chat was summarized at least once"] = seen["compacted"] >= 1
    results["no summary failed"] = seen["failed"] == 0
    results["turn 2's decision is remembered (Lisbon, Friday 12th)"] = all(
        w in (a.get("text") or "") for w in ("Lisbon", "12")
    )
    results["the correction survived (4 people)"] = "4" in (b.get("text") or "")
    shown = c.get(f"/conversations/{chat}/messages").json()
    results["the reopened chat marks the summary"] = any("summarized" in m for m in shown)

    paste_chat = c.post("/conversations", json={"kind": "main"}).json()["id"]
    doc = "".join(f"Clause {i}: the tenant pays {i * 3} euros on day {i % 28 + 1}.\n" for i in range(9_000))
    p = turn(c, paste_chat, doc + "\nWhat does Clause 7777 say exactly?")
    print("paste:", p.get("text"))
    results["a ~500K-character paste is answered from the stored file"] = "23331" in (
        p.get("text") or ""
    ).replace(",", "")
for k, v in results.items():
    print("PASS" if v else "FAIL", k)
