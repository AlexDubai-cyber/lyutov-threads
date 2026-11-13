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
    params["access_token"] = TOKEN
    data = urllib.parse.urlencode(params).encode()
    url = f"{API}/{path}"
    if method == "GET":
        req = urllib.request.Request(url + "?" + data.decode())
    else:
        req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")
        raise SystemExit(f"Threads API {e.code} на {path}: {body}")


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
    return f"https://raw.githubusercontent.com/{REPO}/{BRANCH}/queue/{folder}/{urllib.parse.quote(name)}"


def publish(folder, post):
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


def main():
    if not TOKEN:
        raise SystemExit("Нет THREADS_TOKEN: добавьте секрет в Settings → Secrets and variables → Actions")
    me = call("GET", "me", fields="username")
    print("Ключ работает, аккаунт:", me.get("username"))
    log = json.load(open(LOG)) if os.path.exists(LOG) else {}
    now = datetime.now(DUBAI)
    due = []
    for folder in sorted(os.listdir("queue")):
        p = os.path.join("queue", folder, "post.json")
        if folder in log or not os.path.exists(p):
            continue
        try:
            when = datetime.strptime(folder[:15], "%Y-%m-%d_%H%M").replace(tzinfo=DUBAI)
        except ValueError:
            print("Пропуск, неверное имя папки:", folder)
            continue
        # публикуем, если время наступило, но не позже чем через 6 часов (чтобы не выпускать старьё ночью)
        if when <= now <= when + timedelta(hours=6):
            due.append(folder)
    if not due:
        print("Нечего публиковать", now.strftime("%Y-%m-%d %H:%M"), "по Дубаю")
        return
    for folder in due[:1]:  # не больше одного поста за запуск
        post = json.load(open(os.path.join("queue", folder, "post.json")))
        if DRY:
            print("DRY_RUN: опубликовал бы", folder, "картинок:", len(post.get("images", [])))
            continue
        pid, link = publish(folder, post)
        log[folder] = {"id": pid, "permalink": link, "at": datetime.now(DUBAI).isoformat(timespec="minutes")}
        json.dump(log, open(LOG, "w"), ensure_ascii=False, indent=1)
        print("Опубликовано:", folder, link)


if __name__ == "__main__":
    main()
