# enigram (API, worker, CLI, MCP server)

The Python backend: GraphRAG indexing of written documents for agents, with evaluation.

```
src/enigram/
├── domain/        pure models and rules, no I/O
├── application/   use cases and ports (application/ports.py)
├── adapters/
│   ├── driving/   cli, http_api, mcp_server, worker
│   └── driven/    LightRAG, Docling, OpenAI-compatible LLM, Postgres, Ragas, Laya
└── bootstrap.py   picks the adapters behind the ports
```

Run every command from the repository root, where `.env`, `data/` and `eval/` live; see the
[root README](../../README.md) for setup and usage.
