# Автопубликация Threads @lyutov_ai

- `queue/ГГГГ-ММ-ДД_ЧЧММ/` — готовые посты (время по Дубаю): `post.json` + картинки PNG 1080×1350.
- `publish.py` — публикует через официальный Threads API.
- `.github/workflows/publish.yml` — запуск в 10:17 и 19:17 по Дубаю.
- `published.json` — журнал опубликованного (заполняется автоматически).

Ключ Threads хранится в секрете репозитория `THREADS_TOKEN` (Settings → Secrets and variables → Actions). Ключ живёт 60 дней.
Проверка без публикации: Actions → «Публикация в Threads» → Run workflow → dry_run = 1.
