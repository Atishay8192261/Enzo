"""Mark outside content as untrusted before the brain sees it (spec §12.2, §12.4).

Web pages, files and other outside text may contain instructions aimed at the agent. Results from
tools that return such text are wrapped in a fence whose id is random per call, so the content
cannot close the fence early by guessing the marker. The system prompt tells the brain that text
inside a fence is data, never instructions.
"""

import secrets

OPEN = '<untrusted-data source="{source}" id="{id}">'
CLOSE = '</untrusted-data id="{id}">'


def fence(content: str, *, source: str) -> str:
    fence_id = secrets.token_hex(8)
    return f"{OPEN.format(source=source, id=fence_id)}\n{content}\n{CLOSE.format(id=fence_id)}"
