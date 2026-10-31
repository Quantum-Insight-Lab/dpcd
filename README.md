# dpcd — Evolver

Эксперимент: агент (Claude Haiku) стартует с пустой папки `program/` и бесконечно, шаг за шагом,
наращивает/удаляет функционал. Каждый шаг: «Какое микроизменение происходит прямо сейчас?» → правка → git-коммит → запись в журнал.

## Запуск
```bash
pip install anthropic
export ANTHROPIC_API_KEY=...
EVOLVER_MAX_USD=5 EVOLVER_SLEEP=30 python3 evolver.py
```
Запускать в изолированной среде (Docker/отдельный VPS): у агента есть shell и интернет.

## Наблюдение (HTTP :8787)
| | |
|---|---|
| `GET /status` | шаг, статус, потрачено $ |
| `GET /journal?n=20` | последние шаги: вопрос, ответ, вызовы инструментов |
| `GET /files`, `GET /file?path=x` | текущая программа |
| `GET /diff?step=N` | что изменилось на шаге N |
| `POST /note {"text":...}` | записка агенту на следующий шаг |
| `POST /pause` `/resume` `/stop` | управление |

Данные на диске: `run/journal.jsonl` и git-история `run/program/` — полная хронология эволюции.

## Переменные
`EVOLVER_MODEL` (claude-haiku-5-5), `EVOLVER_SLEEP`, `EVOLVER_MAX_STEPS`, `EVOLVER_MAX_TURNS`, `EVOLVER_MAX_USD`, `EVOLVER_PORT`, `EVOLVER_HOME`.
