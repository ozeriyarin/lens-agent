# Knowledge base

Put Markdown notes here (any name, any folder depth). Headings become passage boundaries, so
write one `##` per thing: a plant, an appliance, a house rule. Then:

```bash
uv run python -m app.kb index
uv run python -m app.kb search "cactus watering"
```

The agent's `lookup` tool searches these passages when you ask a question on a tap scan.
Everything except this README is gitignored: your notes are yours.
