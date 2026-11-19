"""Публикатор Threads для @lyutov_ai.

Берёт посты из папки queue/<YYYY-MM-DD_HHMM>/ (время по Дубаю), у которых время уже наступило
и которых нет в published.json, и публикует их через официальный Threads API.

Формат папки поста:
  post.json  {"text": "...", "images": ["1.png", "2.png", ...]}   images можно опустить
  *.png      картинки (до 20 для карусели)

Переменные окружения:
  THREADS_TOKEN  долгосрочный ключ Threads (секрет репозитория)
  DRY_RUN=1      только проверить ключ и показать, что было бы опубликовано
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timedelta, timezone

API = "https://graph.threads.net/v1.0"
TOKEN = os.environ.get("THREADS_TOKEN", "").strip()
DRY = os.environ.get("DRY_RUN") == "1"
REPO = os.environ.get("GITHUB_REPOSITORY", "")
BRANCH = os.environ.get("GITHUB_REF_NAME", "main")
DUBAI = timezone(timedelta(hours=4))
LOG = "published.json"


def call(method, path, **params):
    """Запрос к Threads API. Сбои 5xx и сети повторяем 3 раза с паузой, потом SystemExit."""
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    last = ""
    for attempt in range(3):
        if method == "GET":
            req = urllib.request.Request(url + "?" + data.decode())
        else:
            req = urllib.request.Request(url, data=data, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            hint = e.headers.get("www-authenticate", "") or ""
            dbg = e.headers.get("x-fb-debug", "") or ""
            last = f"Threads API {e.code} на {path}: {body} {hint} (x-fb-debug {dbg[:40]})".strip()
            if e.code < 500:
                raise SystemExit(last)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = f"Сеть: {path}: {e}"
        time.sleep(15 * (attempt + 1))
    raise SystemExit(last)


def wait_ready(cid, tries=20):
    for _ in range(tries):
        st = call("GET", cid, fields="status,error_message")
        if st.get("status") == "FINISHED":
            return
        if st.get("status") == "ERROR":
            raise SystemExit(f"Контейнер {cid}: {st}")
        time.sleep(6)
    raise SystemExit(f"Контейнер {cid} не готов: {st}")


def raw_url(folder, name):
    if folder is None:
        return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/queue/{urllib.parse.quote(name)}"
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/queue/{folder}/{urllib.parse.quote(name)}"


def publish(folder, post, name=None):
    text = post["text"]
    imgs = post.get("images", [])
    if len(text) > 500:
        raise SystemExit(f"{folder}: текст {len(text)} символов, лимит 500")
    if not imgs:
        c = call("POST", "me/threads", media_type="TEXT", text=text)
    elif len(imgs) == 1:
        c = call("POST", "me/threads", media_type="IMAGE", image_url=raw_url(folder, imgs[0]), text=text)
        wait_ready(c["id"])
    else:
        kids = []
        for n in imgs[:20]:
            k = call("POST", "me/threads", media_type="IMAGE", image_url=raw_url(folder, n), is_carousel_item="true")
            wait_ready(k["id"])
            kids.append(k["id"])
        c = call("POST", "me/threads", media_type="CAROUSEL", children=",".join(kids), text=text)
        wait_ready(c["id"])
    res = call("POST", "me/threads_publish", creation_id=c["id"])
    info = call("GET", res["id"], fields="permalink")
    return res["id"], info.get("permalink")


def load_times():
    t = json.load(open("times.json")) if os.path.exists("times.json") else {}
    return t.get("default", {"am": "10:17", "pm": "19:17"}), t.get("dates", {})


def slot_time(name, default, dates):
    """2026-11-16_am -> время из times.json; 2026-11-14_1917 -> 19:17"""
    day, part = name[:10], name[11:]
    if part in ("am", "pm"):
        hhmm = dates.get(day, {}).get(part, default[part])
    else:
        hhmm = part[:2] + ":" + part[2:4]
    return datetime.strptime(day + " " + hhmm, "%Y-%m-%d %H:%M").replace(tzinfo=DUBAI)


def list_posts():
    """Два формата: queue/<имя>/post.json (картинки в папке) и queue/<имя>.json (картинки queue/<имя>_N.png)."""
    out = {}
    if not os.path.isdir("queue"):
        return out
    for f in sorted(os.listdir("queue")):
        p = os.path.join("queue", f)
        if os.path.isdir(p) and os.path.exists(os.path.join(p, "post.json")):
            out[f] = (f, json.load(open(os.path.join(p, "post.json"))))
        elif f.endswith(".json") and f[:4].isdigit():
            out[f[:-5]] = (None, json.load(open(p)))
    return out


def update_metrics(log):
    """Раз в день собираем просмотры и реакции, чтобы понять пиковые часы."""
    m = json.load(open("metrics.json")) if os.path.exists("metrics.json") else {}
    for name, rec in log.items():
        try:
            r = call("GET", rec["id"] + "/insights", metric="views,likes,replies,reposts,quotes")
        except SystemExit as e:
            print("Метрики недоступны для", name, e)
            continue
        vals = {d["name"]: d.get("values", [{}])[0].get("value", d.get("total_value", {}).get("value")) for d in r.get("data", [])}
        vals["published_at"] = rec.get("at")
        m[name] = vals
    json.dump(m, open("metrics.json", "w"), ensure_ascii=False, indent=1)
    print("Метрики обновлены:", len(m))


def git_save(msg):
    """Сохранить журнал и метрики в репозиторий (внутри долгого запуска)."""
    import subprocess as sp
    files = [f for f in (LOG, "metrics.json") if os.path.exists(f)]
    if not sp.run(["git", "status", "--porcelain"] + files, capture_output=True, text=True).stdout.strip():
        return
    sp.run(["git", "config", "user.name", "threads-bot"])
    sp.run(["git", "config", "user.email", "threads-bot@users.noreply.github.com"])
    sp.run(["git", "add"] + files)
    sp.run(["git", "commit", "-m", msg])
    for _ in range(3):
        if sp.run(["git", "pull", "--rebase"]).returncode == 0 and sp.run(["git", "push"]).returncode == 0:
            return
        time.sleep(10)
    print("Не удалось сохранить журнал в репозиторий")


def run_once(state):
    log = json.load(open(LOG)) if os.path.exists(LOG) else {}
    now = datetime.now(DUBAI)
    default, dates = load_times()
    posts = list_posts()
    fails = state.setdefault("fails", {})  # имя -> [число попыток, когда можно снова]
    due = []
    for name, (folder, post) in posts.items():
        if name in log:
            continue
        try:
            when = slot_time(name, default, dates)
        except (ValueError, KeyError):
            continue
        # время наступило, не позже чем через 6 часов, и только 08:00-22:30 по Дубаю
        if when <= now <= when + timedelta(hours=6) and (8, 0) <= (now.hour, now.minute) <= (22, 30):
            n, after = fails.get(name, [0, 0])
            if n >= 3 or time.time() < after:
                continue  # после сбоя не долбим API каждую минуту: повтор через 15 и 45 минут, максимум 3 попытки
            due.append((when, name))
    due.sort()
    # между постами минимум 60 минут, чтобы после сбоя накопившиеся посты не вышли пачкой
    last = max((datetime.fromisoformat(r["at"]) for r in log.values() if r.get("at")), default=None)
    if due and last and now - last < timedelta(minutes=60):
        due = []
    for when, name in due[:1]:  # не больше одного поста за проход
        folder, post = posts[name]
        if DRY:
            print("DRY_RUN: опубликовал бы", name, "картинок:", len(post.get("images", [])))
            continue
        try:
            pid, link = publish(folder, post, name)
        except SystemExit as e:
            n = fails.get(name, [0, 0])[0] + 1
            fails[name] = [n, time.time() + (15 if n == 1 else 45) * 60]
            state["errors"] = state.get("errors", 0) + 1
            print(f"Сбой публикации {name} (попытка {n} из 3):", e, flush=True)
            continue
        fails.pop(name, None)
        log[name] = {"id": pid, "permalink": link, "at": datetime.now(DUBAI).isoformat(timespec="minutes")}
        json.dump(log, open(LOG, "w"), ensure_ascii=False, indent=1)
        print("Опубликовано:", name, link, "в", now.strftime("%H:%M"), flush=True)
        git_save("Журнал: опубликован " + name)
    today = now.date().isoformat()
    if not DRY and now.hour == 22 and state.get("metrics") != today:
        update_metrics(log)
        state["metrics"] = today
        git_save("Метрики " + today)


def check_key():
    me = call("GET", "me", fields="username")
    print("Ключ работает, аккаунт:", me.get("username"), flush=True)


def main():
    if not TOKEN:
        raise SystemExit("Нет THREADS_TOKEN: добавьте секрет в Settings → Secrets and variables → Actions")
    loop = int(os.environ.get("LOOP_MINUTES", "0") or 0)
    if not loop:
        check_key()  # ручная проверка и dry run: сразу показать ошибку
    end = time.time() + loop * 60
    state = {}
    key_ok, next_key_check = False, 0
    if os.environ.get("METRICS") == "1" and not DRY:
        update_metrics(json.load(open(LOG)) if os.path.exists(LOG) else {})
        git_save("Метрики вручную")
    while True:
        try:
            if loop and not key_ok:
                if time.time() >= next_key_check:
                    try:
                        check_key()
                        key_ok = True
                    except SystemExit as e:
                        state["errors"] = state.get("errors", 0) + 1
                        print("Ключ не прошёл проверку, повтор через 10 минут:", e, flush=True)
                        next_key_check = time.time() + 600
            if key_ok or not loop:
                if loop:
                    import subprocess as sp
                    sp.run(["git", "pull", "--rebase", "-q"])  # подхватить новые посты и times.json
                run_once(state)
        except SystemExit as e:
            print("Ошибка прохода:", e, flush=True)
            if not loop:
                raise
        if time.time() >= end:
            break
        time.sleep(60)
    print("Проход завершён", datetime.now(DUBAI).strftime("%Y-%m-%d %H:%M"), "по Дубаю", flush=True)
    if loop:
        open(".ran_long", "w").write("1")  # цепочку продолжаем только после полного долгого прохода
    if state.get("errors"):
        raise SystemExit(f"За проход было ошибок: {state['errors']} (см. лог выше)")


if __name__ == "__main__":
    main()
