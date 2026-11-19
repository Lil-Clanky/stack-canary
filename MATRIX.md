# Stack Canary compatibility matrix

- Date: 2026-11-19 (UTC)
- Hermes: v0.23.2
- Isolation: pass

| Ollama | granite4:3b-h A1 | granite4:3b-h A2 | granite4:3b-h A3 | granite4:3b-h A4 | granite4:3b-h A5 | granite4:3b-h A6 | granite4:3b-h B6 v0.23.2 | llama3.2:3b A1 | llama3.2:3b A2 | llama3.2:3b A3 | llama3.2:3b A4 | llama3.2:3b A5 | llama3.2:3b A6 | llama3.2:3b B6 v0.23.2 | qwen3.5:0.8b A1 | qwen3.5:0.8b A2 | qwen3.5:0.8b A3 | qwen3.5:0.8b A4 | qwen3.5:0.8b A5 | qwen3.5:0.8b A6 | qwen3.5:0.8b B6 v0.23.2 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.40.16 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ |
| 0.40.15 | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ⚠️ |
| 0.8.0 | — | — | — | — | — | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — | — | — | — | — | — | — |
| 0.7.1 | — | — | — | — | — | — | — | ✅ | ✅ | ✅ | ✅ | ✅ | — | — | — | — | — | — | — | — | — |

## Reproduction drafts

- [2026-11-18_ollama-0.40.15_qwen3.5-0.8b_A6.md](repro/2026-11-18_ollama-0.40.15_qwen3.5-0.8b_A6.md)
- [2026-11-18_ollama-0.40.16_qwen3.5-0.8b_A6.md](repro/2026-11-18_ollama-0.40.16_qwen3.5-0.8b_A6.md)
