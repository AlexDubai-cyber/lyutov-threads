# Автопубликация Threads @lyutov_ai

- `queue/ГГГГ-ММ-ДД_ЧЧММ/` — готовые посты (время по Дубаю): `post.json` + картинки PNG 1080×1350.
- `publish.py` — публикует через официальный Threads API.
- `.github/workflows/publish.yml` — запуск в 10:17 и 19:17 по Дубаю.
- `published.json` — журнал опубликованного (заполняется автоматически).

Ключ Threads хранится в секрете репозитория `THREADS_TOKEN` (Settings → Secrets and variables → Actions). Ключ живёт 60 дней.
Проверка без публикации: Actions → «Публикация в Threads» → Run workflow → dry_run = 1.

## Мониторинг объявлений аренды (leads.py)

- `leads.py` — ищет свежие русскоязычные объявления о сдаче квартир в Дубае (Threads Keyword Search, RECENT), отбирает high/medium, готовит лайк, личку и комментарий.
- `leads/config.json` — запросы, районы, здания, лимиты, `auto_comment` (по умолчанию выкл).
- `leads/state.enc` — зашифрованное состояние (обработанные посты, отметки, лимиты). Ключ: секрет `LEADS_STATE_KEY`.
- `.github/workflows/leads.yml` — запуск примерно раз в 15 минут (только при переменной `LEADS_ENABLED=1`) и вручную: mode=probe (проверка доступа) или monitor, dry_run=1/0.
- Реальные действия только при переменной `LEADS_LIVE=1`. Лайков и личных сообщений в Threads API нет: они приходят карточкой в Telegram (`TG_BOT_TOKEN`, `TG_CHAT_ID`) с кнопкой «Личку отправил».
- Секреты: `LEADS_WHATSAPP`, `LEADS_TELEGRAM_GROUP_URL`, `LEADS_CLIENT_REQUESTS` (необязательно), `LEADS_STATE_KEY`, `TG_BOT_TOKEN`, `TG_CHAT_ID`.
- Тесты: `python -m pytest -q tests`. Прогон на примерах: `python leads.py --fixtures tests/fixtures_leads.json`.
