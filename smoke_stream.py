#!/usr/bin/env python3
import os
import time

from scratch_site import _xai_chat, resolved_xai_model

t0 = time.monotonic()
print("model", resolved_xai_model())
content, tok = _xai_chat(
    os.environ["XAI_API_KEY"],
    model=resolved_xai_model(),
    messages=[
        {
            "role": "system",
            "content": "Reply with a tiny complete HTML5 page only. No markdown. One h1 Hello.",
        },
        {"role": "user", "content": "Make a 5-line HTML page with style color navy."},
    ],
    temperature=0.2,
    max_tokens=400,
    stream=True,
)
print("OK stream", round(time.monotonic() - t0, 1), "s", "tokens", tok, "len", len(content))
print(content[:240].replace("\n", " "))
