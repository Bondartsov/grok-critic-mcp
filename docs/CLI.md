# grok-critic CLI — инструкция по использованию

> Версия документа: 1.12.0 (16.09.2026). CLI появился в v1.11.0, per-file store — в v1.11.1, цены в ₽ и `POLZA_DAILY_BUDGET_RUB` — в v1.12.0.

## Миграция с 1.11.x

- Удалены переменные `POLZA_PRICE_INPUT_PER_1M`, `POLZA_PRICE_OUTPUT_PER_1M`, `POLZA_DAILY_BUDGET_USD`. Если они остались в `.env`/окружении — запуск не ломается, в лог пишется один DEPRECATED-warning с именами ключей (без значений); их стоит удалить.
- Новая `POLZA_DAILY_BUDGET_RUB` (float, по умолчанию `0` = без лимита) — дневной лимит по **фактической** `cost_rub` (из ответа API либо оценке по тарифу модели), soft limit, проверяется ДО платного запроса.
- `--json` у `review`/`followup`: поле `cost_usd` удалено; есть `cost_rub` и новый `cost_is_estimate` (`true` — стоимость посчитана по тарифу модели, а не пришла из API).
- `health [--ping] --json`: новый блок `pricing` (тариф модели в ₽ + лимиты контекста/ответа) и `usage_today.date` в формате `DD.MM.YYYY`. `--json` теперь работает и **без** `--ping` (раньше флаг молча игнорировался и печатался текст).
- Актуальный тариф модели всегда показывает `grok-critic health --ping` (или `check_health` в MCP).

## Зачем

CLI — терминальный вход в того же критика, что работает через MCP, но **не зависящий от состояния MCP-сессии**:

- MCP-сервер отвалился или не подключён → ревью, followup и диагностика доступны из Bash/PowerShell;
- нужно вызвать критика из скрипта/CI или получить машинный JSON;
- нужно понять, **почему** сервер отвалился (`doctor` + `logs`), без просьб к пользователю.

> **Для AI-агента с подключённым MCP** сценарии ревью и followup — только диагностика на случай, когда MCP недоступен: обычный путь — `critic_review`/`critic_followup`/`architecture_review`/`security_audit` через MCP. Пакетные и скриптовые сценарии (цикл по файлам, CI-джобы) — для людей и не-агентских пайплайнов.

Это тот же `critic.py`/`api_client.py`, тот же баланс Polza.AI, тот же store диалогов, что и у MCP-инструментов.

## Требования и установка

```bash
cd grok-critic-mcp
pip install -e ".[dev]"     # или просто pip install -e .
cp .env.example .env        # и заполнить POLZA_API_KEY
```

- Консольная команда: `grok-critic`. Шим кладётся в Scripts вашего Python; при user-install это `%APPDATA%\Python\Python3XX\Scripts\` — если оболочка его не видит, используйте `python -m grok_critic.cli …` (полный аналог) или добавьте директорию в PATH. `grok-critic doctor` показывает, где шим.
- Конфигурация — та же `POLZA_*`-переменная окружения / `.env` (см. README, таблица параметров).
- Python 3.11+, сеть до `POLZA_BASE_URL`.

## Быстрый старт

### Bash

```bash
grok-critic health                                  # мгновенная проверка конфига (без сети)
echo "def add(a, b): return a - b" | grok-critic review - --agents 4   # ревью stdin
grok-critic review src/api.py --agents 16 --focus "security,performance"
grok-critic followup "Ответь одним предложением: код корректен?" --review-id rev_be618bd19c9f
```

### PowerShell

Консоль Windows по умолчанию не в UTF-8 — без этого ломаются `₽`/эмодзи в выводе (CLI сам форсирует `utf-8` для своих потоков, но лучше выставить и в консоли):

```powershell
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"
function critic { python -m grok_critic.cli @args }

critic health --ping
critic review .\src\app.py --agents 4 --focus security --context "что это за код"
critic followup "почему это блокер?" --review-id rev_xxxxxxxxxxxx
```

`python -m grok_critic.cli` надёжнее шима `grok-critic.exe` (обычно не на PATH — см. «Требования и установка» выше и `grok-critic doctor`). На Windows зовите `python`, **не** `python3` — `python3` в PATH обычно заглушка Microsoft Store, которая падает без установленного интерпретатора.

## Справочник команд

### `grok-critic serve`

Запуск MCP stdio-сервера. Вызов `grok-critic` **без подкоманды** делает то же самое — поэтому существующие конфиги MCP-клиентов (`"command": "grok-critic"`) продолжают работать. Агентам вручную вызывать не нужно: процессом MCP управляет клиент.

### `grok-critic health [--ping] [--json]`

Проверка живости без обращения к API (конфиг + доступность store на запись).

- `--ping` — реальный запрос к Polza.AI: статус, тариф модели в ₽ (`pricing`: вход/выход/кэш за 1M токенов + лимиты контекста и ответа, из `GET /models/{model}`), баланс ₽, суточные счётчики (`Today (16.09.2026): N calls | X ₽ (N errors)`).
- `--json` — машинный вывод в обоих режимах: без `--ping` — `{status: ok|error, mode: offline, issues[]}`; с `--ping` — `{status, model, base_url, issues[], pricing, balance_rub, usage_today: {date, calls, errors, cost_rub}}` (`date` — `DD.MM.YYYY`). Работает независимо от `--ping` (раньше без `--ping` флаг молча игнорировался).
- Если тариф модели недоступен (`GET /models/{model}` не ответил) — issue `Тариф модели недоступен (GET /models/<model>)`, `status=degraded`.
- Выход: `0` ok / `1` degraded (например, Balance API или тариф модели недоступны) / `2` сломано (ключ, store).

### `grok-critic doctor`

Главная диагностическая команда — чеклист «почему критик отвалился»:

- версия Python и пакета, git HEAD (подсказка, если код обновился, а сессия старая);
- наличие шима CLI (в т.ч. различение «нет» и «есть, но не на PATH этой оболочки»);
- путь и наличие `.env`, маскированный ключ;
- TCP-доступность `POLZA_BASE_URL` (с указанием прокси-переменных, если заданы);
- записываемость store, состояние лог-файла.

Всегда exit `0`/`2` (2 — если сломано ядро: Python, ключ, сеть, store) и печатает подсказки в конце.

### `grok-critic review <файл | -> [--context S] [--agents N] [--focus S] [--json] [--json-output]`

One-shot ревью в stdout. `-` читает stdin (удобно для pipe: `git diff | grok-critic review -`).

| Опция           | Значение                                                                                                                                                     |
| --------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `--context`     | Контекст: проект, язык, назначение (всегда указывайте)                                                                                                       |
| `--agents`      | 4 = быстро (~30 с, ≈ 9–15 ₽ за файл), 16 = глубоко (~2–4 мин, ≈ 50–60 ₽ за файл ~700–850 строк). Дефолт из конфига (16). Клэмпится в 1–64                    |
| `--focus`       | Фокус-области через запятую: `"security,performance,SOLID"`                                                                                                  |
| `--json`        | Машинный вывод: `{success, text, review_id, tokens, cost_rub, cost_is_estimate, …}` (`cost_is_estimate=true` — цена посчитана по тарифу, а не пришла из API) |
| `--json-output` | Попросить у модели строгие JSON-findings `{summary, findings[{severity, title, location, description, recommendation}]}`                                     |

Примечания:

- Размер контента ограничен `POLZA_MAX_CONTENT_CHARS` (по умолчанию 100 000 символов).
- Sandbox MCP (`POLZA_ALLOWED_READ_DIRS`) в CLI **не применяется**: файл читаете вы сами — это осознанное действие владельца. Если имя файла попадает под тот же расширенный денилист секретов, что и у MCP (`_is_sensitive_file`: `.env*`, `*credential*`, `id_rsa*`/`*.pem`/`*.key`/…, `.claude.json*`, `*.tfstate*`, `.vault-token`, файлы внутри `.ssh`/`.aws`/`.kube`/… и т.д. — см. README, «Sandbox»), CLI предупредит в stderr, но не заблокирует. Проверка (SEC-ADS) учитывает и ADS-формы имени на Windows (`server.pem::$DATA`, `.env:secret`) — тот же `_normalize_component`, что и у MCP, срезает суффикс `:...` и хвостовые точки/пробелы перед сравнением с денилистом, так что `id_rsa.` или `credentials.json::$DATA` тоже дадут предупреждение.
- Ошибки (бюджет, слишком большой контент, сеть) → понятное сообщение в stderr и exit `2`, деньги не списываются.

### `grok-critic followup "вопрос" (--review-id ID | --from <файл | ->) [--agents N] [--json]`

Уточняющий вопрос по предыдущему ревью. Источник — ровно один:

- `--review-id rev_…` — диалог достаётся из **дискового store** (`db/reviews/`), общий с MCP-сервером. Дёшево (~7k input-токенов). Если id не найден — проверьте TTL (24 ч) и не чистили ли `db/reviews/`.
- `--from файл` или `--from -` (stdin) — полный текст предыдущего ревью (fallback, ~25k input-токенов).

Результат followup тоже сохраняется в store — диалог можно продолжать дальше по новому `review_id`.

### `grok-critic logs [--tail 50]`

Хвост лога сервера (путь — `POLZA_LOG_FILE`). Читает файл потоково, большой лог не уходит в память. Если `POLZA_LOG_FILE` пуст — подскажет, как включить файловый лог.

### `grok-critic config [--json]`

Текущая конфигурация с **маскированным ключом**: все `POLZA_*`-параметры, путь `.env`, путь store, версия пакета. После изменения `.env` для MCP-сервера не забудьте `reload_config_tool` (CLI сам перечитывает окружение при каждом запуске).

## Exit codes

|  Код  | Значение                                                      | Как использовать агенту                |
| :---: | ------------------------------------------------------------- | -------------------------------------- |
|  `0`  | Успех                                                         | Результат в stdout                     |
|  `1`  | Warning (деградация: Balance API недоступен, лог не настроен) | Читать stderr, можно продолжать        |
|  `2`  | Ошибка (ключ, сеть, бюджет, размер, файл не найден)           | Не ретраить вслепую — сначала `doctor` |
| `130` | Прервано пользователем (Ctrl+C)                               | —                                      |

`--json` у `health`/`review`/`followup`/`config` даёт машиночитаемый вывод — скрипты Branch по `success`/`status`.

## Store диалогов и review_id

- Расположение: `db/reviews/` в репозитории (override — `POLZA_STORE_PATH`). Каждый `review_id` — отдельный JSON-файл (`rev_xxxx.json`).
- **Общий для MCP-сервера и CLI**: ревью, полученное агентом через MCP, можно продолжать через CLI и наоборот.
- Переживает рестарты сервера. TTL записи — 24 ч с последнего обращения (`load` обновляет TTL); лимит — 50 файлов, самые старые вытесняются.
- Запись атомарная, пер-файловый layout исключает потерю чужих записей при параллельной работе нескольких процессов.
- ⚠️ Store содержит полный диалог (включая ревьюившийся код) **в plaintext**. `db/` вне git; при ревью чувствительного кода очищайте `db/reviews/` вручную.

## Типовые сценарии (CLI)

```bash
# 1. Ревью перед коммитом (после того, как сам прочитал свой код)
grok-critic review src/grok_critic/cli.py --agents 4 --focus "correctness,error-handling" \
  --context "grok-critic-mcp, Python 3.11+, CLI для агентов"

# 2. Диагностика «критик отвалился»
grok-critic health || grok-critic doctor
grok-critic logs --tail 100          # читать реальную причину
# → если API жив, ревью доступно прямо отсюда (см. сценарий 1);
#   в конце попросить пользователя переподключить MCP

# 3. Спор с критиком
grok-critic followup "Не согласен с пунктом 3, потому что …" --review-id rev_xxxx

# 4. Пакетная проверка (дёшево, 4 агента)
for f in src/*.py; do
  grok-critic review "$f" --agents 4 --json > "review-$(basename "$f" .py).json" \
    || echo "FAIL: $f"
done

# 5. Скрипт ветвится по результату
if ! grok-critic review patch.diff --agents 4 --json > r.json; then
  echo "ревью не выполнено"; exit 2
fi
python - <<'EOF'
import json; r = json.load(open("r.json"))
print("CRITICAL найден" if "CRITICAL" in r["text"] else "ok")
EOF
```

## Отличия CLI от MCP-инструментов

| Аспект                           | MCP (`critic_review`, …)                                    | CLI                                                                                |
| -------------------------------- | ----------------------------------------------------------- | ---------------------------------------------------------------------------------- |
| Transport                        | MCP-сессия с клиентом                                       | Обычный процесс — работает всегда                                                  |
| Sandbox `file_path`              | MCP: opt-in + корни (проект сессии, без `$HOME`) + денилист | Нет — вы читаете файл сами (только warning на секреты)                             |
| Heartbeat во время ревью         | Да (если клиент поддерживает)                               | Нет — просто ждёт (см. `⏱ Elapsed` в результате)                                   |
| Store / review_id                | Общий                                                       | Общий (тот же)                                                                     |
| `restart_server` / `self_update` | Доступны как tools                                          | Нет — рестартом MCP управляет клиент; обновление — `git pull` + `pip install -e .` |
| Бюджет, dedup, retry, счётчики   | Общие                                                       | Общие                                                                              |

## Решение проблем

| Симптом                                      | Причина / действие                                                                                                                |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `grok-critic: command not found`             | Шим не на PATH этой оболочки — используйте `python -m grok_critic.cli …` или `pip install -e .` заново; `doctor` покажет путь     |
| `POLZA_API_KEY` / ValidationError при старте | `.env` не найден — задайте `POLZA_ENV_FILE` или запуститесь из директории с `.env`                                                |
| `Превышен дневной бюджет`                    | Лимит `POLZA_DAILY_BUDGET_RUB` исчерпан — увеличить в `.env` (для MCP затем `reload_config_tool`)                                 |
| `review_id не найден`                        | Истёк TTL (24ч с последнего обращения), store очищен вручную или запись вытеснена лимитом 50 файлов — используйте `--from <файл>` |
| `Контент слишком большой`                    | Уменьшите вход или поднимите `POLZA_MAX_CONTENT_CHARS`                                                                            |
| Долгое молчание при 16 агентах               | Норма (2–4 мин); heartbeat в CLI не шлётся                                                                                        |
