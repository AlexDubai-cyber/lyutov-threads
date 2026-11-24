"""Мониторинг свежих русскоязычных объявлений о сдаче квартир в Дубае (Threads @lyutov_ai).

Что делает один запуск:
  1. Ищет посты через официальный Threads Keyword Search (search_type=RECENT) с перекрытием по времени.
  2. Отбирает объявления от стороны, которая сдаёт: high (собственник/управляющая, конкретная квартира),
     medium (квартира сдаётся, роль автора неясна), остальное пропускает с причиной.
  3. Для high планирует действия: лайк -> личное сообщение -> комментарий.
     Лайков и личных сообщений в Threads API нет, поэтому они уходят в очередь: карточка в Telegram
     с текстами и кнопкой «Личку отправил». Комментарий публикуется через API только если
     auto_comment=true в leads/config.json и LEADS_LIVE=1.
  4. Сохраняет состояние (зашифрованный leads/state.enc), чтобы не писать дважды по одному объявлению.

Переменные окружения (GitHub Secrets):
  THREADS_TOKEN              тот же ключ, что у publish.py
  LEADS_STATE_KEY            ключ шифрования состояния (репозиторий публичный)
  LEADS_WHATSAPP             номер WhatsApp, только для текста личного сообщения
  LEADS_TELEGRAM_GROUP_URL   ссылка на Telegram-группу (можно не задавать)
  LEADS_CLIENT_REQUESTS      JSON подтверждённых запросов клиентов (можно не задавать)
  TG_BOT_TOKEN, TG_CHAT_ID   канал карточек в Telegram
  LEADS_LIVE=1               разрешить реальные действия; иначе dry-run

Команды:
  python leads.py                         мониторинг (dry-run, если LEADS_LIVE != 1)
  python leads.py --fixtures FILE.json    прогон на тестовых постах без сети
  python leads.py probe                   проверить доступ: поиск по чужим постам, свои ответы
  python leads.py followup "текст ответа автора" --place "Bahar, JBR"
"""
import base64, hashlib, json, os, re, sys, time, urllib.parse, urllib.request, urllib.error
from datetime import datetime, timedelta, timezone

API = "https://graph.threads.net/v1.0"
DUBAI = timezone(timedelta(hours=4))
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, "leads", "config.json")
STATE_ENC = os.path.join(HERE, "leads", "state.enc")
STATE_LOCAL = os.path.join(HERE, ".leads_local", "state.json")  # dry-run без ключа, в .gitignore
FIELDS = "id,text,username,permalink,timestamp,media_type,is_reply,is_quote_post"


def env(name, default=""):
    return os.environ.get(name, default).strip()


LIVE = env("LEADS_LIVE") == "1" and env("DRY_RUN") != "1"
IN_ACTIONS = env("GITHUB_ACTIONS") == "true"


# ---------------------------------------------------------------- тексты

def comment_text(place, dm_sent):
    if dm_sent:
        return f"Здравствуйте! Интересует квартира в {place}. Написал вам в личные сообщения"
    return f"Здравствуйте! Квартира в {place} ещё актуальна? Можем обсудить в личных сообщениях?"


def dm_text(place, whatsapp):
    wa = whatsapp or "{номер WhatsApp не задан: LEADS_WHATSAPP}"
    return (f"Здравствуйте! Я Александр Лютов, брокер по недвижимости в Дубае. Пишу по вашей квартире в {place}. "
            f"Подскажите, она ещё свободна? Если вам удобно, напишите мне, пожалуйста, в WhatsApp: {wa}. "
            "С вашей стороны комиссии нет: если сделка состоится, комиссию оплачивает мой клиент. "
            "Если квартира ему подойдёт, согласуем с вами показ")


STAY_HERE = re.compile(r"давайте\s+(здесь|тут)|пишите\s+(здесь|тут)|не\s+пользуюсь\s+(whatsapp|ватсап|вотсап|вацап)|нет\s+(whatsapp|ватсапа|вотсапа)|лучше\s+(здесь|тут)", re.I)


def followup_text(reply, place, tg_url="", client_match=False):
    """Ответ, если автор не хочет в WhatsApp. None — ответ автора требует решения Александра."""
    if not STAY_HERE.search(reply or ""):
        return None
    intro = ("У меня есть русскоязычный клиент на вашу квартиру." if client_match
             else "Могу предложить вашу квартиру русскоязычным клиентам.")
    if tg_url:
        return (f"Хорошо, давайте здесь) {intro} Если клиенту подойдёт, познакомлю вас и организую показ. "
                f"Ещё у нас есть Telegram-группа, там можно опубликовать квартиру: {tg_url}")
    return (f"Хорошо, давайте здесь) {intro} Если клиенту подойдёт, познакомлю вас и организую показ. "
            "Подскажите, квартира в " + place + " ещё свободна, на какой срок сдаёте и когда можно посмотреть?")


def match_client(lead, requests):
    """Подтверждённый запрос совпадает по району, бюджету, типу и сроку заезда."""
    for r in requests or []:
        if r.get("district") != lead.get("district"):
            continue
        if not lead.get("unit_type") or r.get("unit_type") != lead.get("unit_type"):
            continue
        if not lead.get("price_aed") or not r.get("budget_max_aed") or lead["price_aed"] > r["budget_max_aed"]:
            continue
        if r.get("term") and lead.get("term") and r["term"] != lead["term"]:
            continue
        if not lead.get("available_from") or not r.get("move_in_by") or lead["available_from"] > r["move_in_by"]:
            continue
        return True
    return False


# ---------------------------------------------------------------- классификация

NEG = [
    (r"\b(ищу|ищем|сниму|снимем|нужна|нужен|подыщите|looking for)\b.{0,40}(квартир|студи|комнат|apartment|1br|2br|жиль)", "ищет жильё, а не сдаёт"),
    (r"(агент(ам|ов|ы)?|посредник(ам|ов|и)?|риелтор(ам|ов|ы)?|брокер(ам|ов|ы)?)\s*(просьба\s*)?(не\s+(писать|беспокоить|звонить)|мимо)|no\s+agents|agents\s+(please\s+)?don.?t", "просят агентов не писать"),
    (r"\b(продаю|продаётся|продается|продажа|for sale|на продажу|выкуп)\b", "продажа"),
    (r"\b(сдана|сдано|уже сдали|уже сдан|rented out|занята)\b", "уже сдана"),
    (r"(помогу|помогаю|подберу|подбираю|поможем|подбор)\s.{0,30}(снять|найти|квартир|жиль|недвижим)|услуги|консультаци|наш[аи]?\s+агентств|агентство недвижимости|пишите в директ,? подберу", "реклама услуг"),
]
OFFER = r"\b(сдаю|сдаём|сдаем|сдам|сдаётся|сдается|сдаются|освободил[аи]сь|в аренду|аренда|for rent|for lease|available now|свободна)\b"
UNIT = r"(квартир|студи|studio|\b[1-5]\s*-?\s*(br|bhk|bed)|\b[1-5]\s*(\+|-)\s*1\b|однушк|двушк|трёшк|трешк|апартамент|пентхаус|таунхаус|вилл|\bспальн)"
OWNER = r"(собственник|от собственника|владелец|моя квартира|мою квартиру|свою квартиру|напрямую|без посредников|direct(ly)? from owner|\bowner\b|управляющ\w+ компани|property management|holiday homes?)"
BOOST = [(r"срочно|urgent", "срочно"), (r"свободна\s+(сейчас|с сегодня)|available now|освободилась", "свободна сейчас"),
         (r"готова к (заезду|проживанию)|ready to move", "готова к заезду"), (r"без комисс|no commission|0\s*%\s*комисс", "без комиссии")]


def cyr_share(text):
    letters = [c for c in text if c.isalpha()]
    return sum("а" <= c.lower() <= "я" or c.lower() == "ё" for c in letters) / len(letters) if letters else 0


def norm(text):
    return re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е"))


def find_place(t, cfg):
    building = district = ""
    for name, b in cfg["buildings"].items():
        m = None
        for a in b["aliases"]:
            m = re.search(r"\b" + re.escape(a) + r"\s*(\d{1,2})?\b", t)
            if m:
                break
        if m:
            building = name + (" " + m.group(1) if m.group(1) else "")
            district = b["district"]
            break
    if not district:
        for name, aliases in cfg["districts"].items():
            if any(re.search(r"(?<![a-zа-я])" + re.escape(a) + r"(?![a-zа-я])", t) for a in aliases):
                district = name
                break
    return building, district


def extract(t):
    out = {"unit_type": "", "price_aed": None, "term": "", "available_from": ""}
    m = re.search(r"\b(studio|студи\w*)|\b([1-5])\s*-?\s*(?:br|bhk|bed|спальн\w*)\b|\b([1-5])\s*\+\s*1\b|(однушк|двушк|трёшк|трешк)", t)
    if m:
        if m.group(1):
            out["unit_type"] = "studio"
        elif m.group(2) or m.group(3):
            out["unit_type"] = f"{m.group(2) or m.group(3)}BR"
        else:
            out["unit_type"] = {"однушк": "1BR", "двушк": "2BR", "трёшк": "3BR", "трешк": "3BR"}[m.group(4)]
    m = re.search(r"(\d[\d\s.,]{2,9}\d|\d{3,})\s*(k|к|тыс\.?)?\s*(aed|дирхам\w*|дрх|dhs|аед)", t)
    if m:
        v = int(re.sub(r"\D", "", m.group(1)))
        out["price_aed"] = int(v * 1000) if m.group(2) else int(v)
    if re.search(r"посуточн|за ночь|per night|daily", t):
        out["term"] = "daily"
    elif re.search(r"помесячн|на месяц|monthly|/\s*мес|в месяц|per month", t):
        out["term"] = "monthly"
    elif re.search(r"на год|долгосроч|yearly|annual|1 чек|\d\s*чек", t):
        out["term"] = "yearly"
    m = re.search(r"(свободна|доступна|заезд|available)\s*(с|from)?\s*(\d{1,2}\s*[а-яa-z.]+|\d{1,2}\.\d{1,2}|сейчас|сегодня|now|immediately)", t)
    if m:
        out["available_from"] = m.group(3)
    elif re.search(r"готова к (заезду|проживанию)|ready to move", t):
        out["available_from"] = "сейчас"
    return out


def quote(text, pattern):
    m = re.search(pattern, text, re.I)
    if not m:
        return ""
    s, e = max(0, m.start() - 40), min(len(text), m.end() + 60)
    return text[s:e].replace("\n", " ").strip()


def classify(post, cfg, now, own_username="lyutov_ai"):
    text = post.get("text") or ""
    t = norm(text)
    base = {"post_id": post.get("id"), "permalink": post.get("permalink", ""), "author": post.get("username", ""),
            "date": post.get("timestamp", ""), "building": "", "district": "", "unit_type": "", "price_aed": None,
            "term": "", "available_from": "", "quote": "", "priority": None, "reason": ""}
    if post.get("username") == own_username:
        return {**base, "reason": "свой пост"}
    if post.get("is_reply"):
        return {**base, "reason": "это ответ, а не объявление"}
    ts = parse_ts(post.get("timestamp"))
    if ts and now - ts > timedelta(hours=cfg["max_post_age_hours"]):
        return {**base, "reason": "старше %d ч" % cfg["max_post_age_hours"]}
    if cyr_share(text) < cfg["min_cyrillic_share"]:
        return {**base, "reason": "не русский текст"}
    for pat, why in NEG:
        if re.search(pat, t):
            return {**base, "reason": why, "quote": quote(text, pat)}
    if not (re.search(OFFER, t) and re.search(UNIT, t)):
        return {**base, "reason": "нет предложения конкретной квартиры"}
    building, district = find_place(t, cfg)
    if not district and not re.search(r"дуба[йе]|dubai|оаэ|uae", t):
        return {**base, "reason": "не Дубай или район не распознан"}
    lead = {**base, **extract(t), "building": building, "district": district or "Dubai"}
    boosts = [name for pat, name in BOOST if re.search(pat, t)]
    if re.search(OWNER, t):
        lead["priority"] = "high"
        lead["quote"] = quote(text, OWNER)
        lead["reason"] = "сдаёт собственник/управляющая напрямую" + (", " + ", ".join(boosts) if boosts else "")
    else:
        lead["priority"] = "medium"
        lead["quote"] = quote(text, OFFER)
        lead["reason"] = "квартира сдаётся, роль автора неясна" + (" (" + ", ".join(boosts) + ": собственника не подтверждает)" if boosts else "")
    lead["score"] = (2 if lead["priority"] == "high" else 1) * 10 + len(boosts)
    return lead


def place_of(lead):
    if lead.get("building") and lead.get("district") and lead["district"] != "Dubai":
        return f"{lead['building']}, {lead['district']}"
    return lead.get("building") or (lead["district"] if lead.get("district") not in ("", "Dubai") else "Дубае")


def fingerprint(text):
    t = re.sub(r"[^a-zа-я0-9]+", " ", norm(text))
    return hashlib.sha256(" ".join(t.split()[:60]).encode()).hexdigest()[:16]


def repost_key(lead):
    if not (lead.get("author") and lead.get("price_aed")):
        return ""
    return f"{lead['author']}|{lead.get('building') or lead.get('district')}|{lead.get('unit_type')}|{lead['price_aed']}"


def parse_ts(s):
    if not s:
        return None
    try:
        return datetime.strptime(s.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        return None


# ---------------------------------------------------------------- состояние

def _key():
    k = env("LEADS_STATE_KEY")
    return hashlib.sha256(k.encode()).digest() if k else None


def load_state():
    empty = {"last_search": 0, "query_cursor": 0, "leads": {}, "fingerprints": {}, "repost_keys": {},
             "tg_offset": 0, "contacts": {}, "pending": {}}
    key = _key()
    if key and os.path.exists(STATE_ENC):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        raw = base64.b64decode(open(STATE_ENC).read())
        return {**empty, **json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None))}
    if not key and os.path.exists(STATE_LOCAL):
        return {**empty, **json.load(open(STATE_LOCAL))}
    return empty


def save_state(state):
    key = _key()
    if key:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        nonce = os.urandom(12)
        data = nonce + AESGCM(key).encrypt(nonce, json.dumps(state, ensure_ascii=False).encode(), None)
        open(STATE_ENC, "w").write(base64.b64encode(data).decode())
    elif LIVE:
        raise SystemExit("Нет LEADS_STATE_KEY: без шифрования состояние в публичный репозиторий не пишу")
    else:
        os.makedirs(os.path.dirname(STATE_LOCAL), exist_ok=True)
        json.dump(state, open(STATE_LOCAL, "w"), ensure_ascii=False, indent=1)


def git_save(msg):
    if not (IN_ACTIONS and LIVE):
        return
    import subprocess as sp
    sp.run(["git", "config", "user.name", "threads-bot"])
    sp.run(["git", "config", "user.email", "threads-bot@users.noreply.github.com"])
    sp.run(["git", "add", STATE_ENC])
    if sp.run(["git", "diff", "--cached", "--quiet"]).returncode == 0:
        return
    sp.run(["git", "commit", "-q", "-m", msg])
    for _ in range(3):
        if sp.run(["git", "pull", "--rebase", "-q"]).returncode == 0 and sp.run(["git", "push", "-q"]).returncode == 0:
            return
        time.sleep(5)
    raise SystemExit("Не удалось сохранить состояние в репозиторий")


# ---------------------------------------------------------------- Threads API

def call(method, path, **params):
    params["access_token"] = env("THREADS_TOKEN")
    data = urllib.parse.urlencode(params)
    req = (urllib.request.Request(f"{API}/{path}?{data}") if method == "GET"
           else urllib.request.Request(f"{API}/{path}", data=data.encode(), method="POST"))
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read()).get("error", {})
        except ValueError:
            err = {}
        raise SystemExit(f"Threads API {e.code} на {path.split('?')[0]}: code {err.get('code')} {err.get('message', '')[:160]}")


def search(q, since, until):
    out, after = [], None
    for _ in range(3):
        p = dict(q=q, search_type="RECENT", fields=FIELDS, since=since, until=until, limit=100)
        if after:
            p["after"] = after
        r = call("GET", "keyword_search", **p)
        out += r.get("data", [])
        after = r.get("paging", {}).get("cursors", {}).get("after")
        if not after or not r.get("paging", {}).get("next"):
            break
    return out


def reply_exists(post_id, since_ts):
    """Проверка после таймаута: наш ответ под этим постом уже опубликован?"""
    r = call("GET", "me/replies", fields="id,replied_to,timestamp", since=since_ts, limit=50)
    for x in r.get("data", []):
        if (x.get("replied_to") or {}).get("id") == post_id:
            return x["id"]
    return None


def publish_reply(post_id, text):
    c = call("POST", "me/threads", media_type="TEXT", text=text, reply_to_id=post_id)
    time.sleep(10)
    return call("POST", "me/threads_publish", creation_id=c["id"])["id"]


# ---------------------------------------------------------------- Telegram (карточки)

def tg(method, **params):
    tok = env("TG_BOT_TOKEN")
    if not tok:
        return None
    data = json.dumps(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/{method}", data=data,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def card_text(lead, cfg, wa, dm_sent=False):
    place = place_of(lead)
    lines = [("🔥 HIGH" if lead["priority"] == "high" else "👀 На просмотр") + f" · {place}",
             f"Пост: {lead['permalink']}", f"Профиль: https://www.threads.com/@{lead['author']}",
             "Почему: " + lead["reason"], "Фраза: «" + lead["quote"] + "»"]
    info = [f"{k}: {lead[k]}" for k in ("unit_type", "price_aed", "term", "available_from") if lead.get(k)]
    if info:
        lines.append(" · ".join(info))
    if lead["priority"] == "high":
        lines += ["", "1) Лайк: вручную (в API нет)", "2) Личка (скопируйте):", dm_text(place, wa),
                  "", "3) Комментарий после лички:", comment_text(place, True),
                  "   если лички ещё нет:", comment_text(place, False)]
        if cfg.get("auto_comment"):
            lines.append("Автокомментарий включён: опубликую " +
                         ("сразу вариант без «написал вам»" if cfg.get("comment_before_dm") else "после кнопки «Личку отправил»"))
    return "\n".join(lines)


def send_card(lead, cfg, wa):
    kb = {"inline_keyboard": [[{"text": "✅ Личку отправил", "callback_data": "dm:" + lead["post_id"]},
                               {"text": "Пропустить", "callback_data": "skip:" + lead["post_id"]}]]} if lead["priority"] == "high" else None
    params = {"chat_id": env("TG_CHAT_ID"), "text": card_text(lead, cfg, wa), "disable_web_page_preview": True}
    if kb:
        params["reply_markup"] = kb
    return tg("sendMessage", **params)


def read_buttons(state):
    """Отметки «Личку отправил»/«Пропустить» из Telegram. Только из настроенного чата."""
    r = tg("getUpdates", offset=state["tg_offset"] + 1, timeout=0, allowed_updates=["callback_query"])
    marks = []
    for u in (r or {}).get("result", []):
        state["tg_offset"] = max(state["tg_offset"], u["update_id"])
        cq = u.get("callback_query") or {}
        if str((cq.get("message") or {}).get("chat", {}).get("id")) != env("TG_CHAT_ID"):
            continue
        kind, _, pid = (cq.get("data") or "").partition(":")
        if pid in state["leads"]:
            marks.append((kind, pid))
            tg("answerCallbackQuery", callback_query_id=cq["id"], text="Отмечено")
    return marks


# ---------------------------------------------------------------- основной проход

def plan_actions(lead, cfg):
    if lead["priority"] != "high":
        return ["card:review"]
    acts = ["like:queue", "dm:queue"]
    if cfg.get("auto_comment"):
        acts.append("comment:auto_now" if cfg.get("comment_before_dm") else "comment:after_dm")
    else:
        acts.append("comment:queue")
    return acts


def do_comment(state, pid, dm_sent, log):
    lead = state["leads"][pid]
    if lead.get("comment_id"):
        return
    pend = state["pending"].get(pid)
    if pend:  # прошлый запуск мог упасть после отправки: сначала проверяем, вышел ли ответ
        found = reply_exists(pid, pend["at"] - 60)
        if found:
            lead["comment_id"] = found
            state["pending"].pop(pid)
            log(f"  комментарий уже был опубликован ранее: {found}")
            return
        if pend.get("tries", 0) >= 2:
            log("  комментарий: 2 неудачные попытки, оставляю для ручной обработки")
            return
    text = comment_text(place_of(lead), dm_sent)
    state["pending"][pid] = {"at": int(time.time()), "tries": (pend or {}).get("tries", 0) + 1}
    save_state(state)
    git_save("Мониторинг: намерение комментария")
    lead["comment_id"] = publish_reply(pid, text)
    lead["comment_text"] = text
    state["pending"].pop(pid, None)
    log(f"  комментарий опубликован: {lead['comment_id']}")


def run(cfg, posts=None, now=None, log=print):
    """posts=None — искать через API; иначе обработать переданные посты (тесты, --fixtures)."""
    now = now or datetime.now(timezone.utc)
    state = load_state()
    wa, tg_url = env("LEADS_WHATSAPP"), env("LEADS_TELEGRAM_GROUP_URL")
    requests = json.loads(env("LEADS_CLIENT_REQUESTS") or "[]")
    mode = "LIVE" if LIVE else "DRY-RUN"
    log(f"Режим: {mode}. Автокомментарии: {'вкл' if cfg.get('auto_comment') else 'выкл'}")

    # 1. отметки из Telegram и отложенные комментарии
    if LIVE:
        for kind, pid in read_buttons(state):
            lead = state["leads"][pid]
            if kind == "dm":
                lead["dm_sent"] = lead.get("dm_sent") or now.isoformat(timespec="minutes")
                log(f"Отмечено: личка отправлена вручную · {lead['permalink']}")
                if cfg.get("auto_comment") and "comment:after_dm" in lead.get("actions", []):
                    do_comment(state, pid, True, log)
            elif kind == "skip":
                lead["status"] = "skipped"

    # 2. поиск
    if posts is None:
        since = int(state["last_search"] - cfg["overlap_minutes"] * 60) if state["last_search"] \
            else int(now.timestamp() - cfg["first_run_lookback_hours"] * 3600)
        until = int(now.timestamp())
        qs = cfg["queries"]
        n = min(cfg["queries_per_run"], len(qs))
        start = state["query_cursor"] % len(qs)
        batch = [qs[(start + i) % len(qs)] for i in range(n)]
        state["query_cursor"] = (start + n) % len(qs)
        posts, seen_ids = [], set()
        for q in batch:
            try:
                for p in search(q, since, until):
                    if p.get("id") not in seen_ids:
                        seen_ids.add(p.get("id"))
                        posts.append(p)
            except SystemExit as e:
                log(f"Поиск «{q}»: {e}")
        others = [p for p in posts if p.get("username") not in ("lyutov_ai", None)]
        log(f"Запросов: {len(batch)}, постов: {len(posts)}, из них чужих: {len(others)}")
        if posts and not others:
            log("ВНИМАНИЕ: поиск вернул только свои посты. Нужен одобренный доступ threads_keyword_search (App Review).")
        state["last_search"] = until

    # 3. отбор и действия
    today = now.astimezone(DUBAI).date().isoformat()
    report = []
    for p in posts:
        pid = p.get("id")
        if pid in state["leads"]:
            report.append(("повтор", pid, "уже обработан"))
            continue
        lead = classify(p, cfg, now)
        if not lead["priority"]:
            report.append(("пропуск", pid, lead["reason"]))
            continue
        fp, rk = fingerprint(p.get("text")), repost_key(lead)
        dup = state["fingerprints"].get(fp) or (rk and state["repost_keys"].get(rk))
        if dup:
            report.append(("перепубликация", pid, f"то же объявление, что {dup}"))
            continue
        if lead["priority"] == "high" and state["contacts"].get(today, 0) >= cfg["max_contacts_per_day"]:
            report.append(("лимит", pid, "достигнут дневной лимит контактов"))
            continue
        lead["actions"] = plan_actions(lead, cfg)
        lead["status"] = "new"
        state["leads"][pid] = lead
        state["fingerprints"][fp] = pid
        if rk:
            state["repost_keys"][rk] = pid
        if lead["priority"] == "high":
            state["contacts"][today] = state["contacts"].get(today, 0) + 1
        report.append((lead["priority"], pid, lead["reason"]))
        log("")
        log(card_text(lead, cfg, (wa if not IN_ACTIONS else "{WhatsApp из секрета}")))
        log("План: " + ", ".join(lead["actions"]))
        if LIVE:
            if env("TG_BOT_TOKEN"):
                send_card(lead, cfg, wa)
            if "comment:auto_now" in lead["actions"]:
                try:
                    do_comment(state, pid, False, log)
                except SystemExit as e:
                    log(f"  комментарий не отправлен: {e}")
        save_state(state)

    log("")
    for kind, pid, why in report:
        log(f"[{kind}] {pid}: {why}")
    save_state(state)
    git_save("Мониторинг объявлений " + now.astimezone(DUBAI).strftime("%Y-%m-%d %H:%M"))
    return report, state


def probe():
    """Проверка доступа без действий: чужие посты в поиске, свои ответы."""
    me = call("GET", "me", fields="id,username")
    print("Аккаунт:", me.get("username"))
    for q in ["сдаю квартиру Дубай", "Bahar", "Дубай"]:
        try:
            r = call("GET", "keyword_search", q=q, search_type="RECENT", fields="id,username,timestamp", limit=25)
            d = r.get("data", [])
            others = [x for x in d if x.get("username") != me.get("username")]
            print(f"keyword_search «{q}»: всего {len(d)}, чужих {len(others)}")
        except SystemExit as e:
            print(f"keyword_search «{q}»: {e}")
    try:
        r = call("GET", "me/replies", fields="id,text,permalink,timestamp,replied_to", limit=50)
        d = r.get("data", [])
        print("me/replies: доступно ответов", len(d))
        for x in d:
            if re.search(r"bahar|бахар|написал в лс", (x.get("text") or "").lower()):
                print("  найден ответ:", x.get("permalink"), "| replied_to:", (x.get("replied_to") or {}).get("id"))
    except SystemExit as e:
        print("me/replies:", e)
    print("Лайки и личные сообщения: в Threads API нет эндпоинтов, только очередь для ручной отправки.")


def main(argv):
    cfg = json.load(open(CONFIG))
    if argv[:1] == ["probe"]:
        return probe()
    if argv[:1] == ["followup"]:
        place = argv[argv.index("--place") + 1] if "--place" in argv else "вашей квартире"
        print(followup_text(argv[1], place, env("LEADS_TELEGRAM_GROUP_URL")) or "Нужен ответ Александра (не про «давайте здесь»)")
        return
    if "--fixtures" in argv:
        posts = json.load(open(argv[argv.index("--fixtures") + 1]))
        now = parse_ts(env("LEADS_NOW")) if env("LEADS_NOW") else None
        return run(cfg, posts=posts, now=now)
    if not env("THREADS_TOKEN"):
        raise SystemExit("Нет THREADS_TOKEN")
    run(cfg)


if __name__ == "__main__":
    main(sys.argv[1:])
