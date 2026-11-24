"""Тесты мониторинга объявлений. Запуск: python -m pytest -q tests (сеть не нужна)."""
import json, os, sys, importlib
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
NOW = datetime(2026, 11, 25, 8, 0, tzinfo=timezone.utc)
POSTS = {p["id"]: p for p in json.load(open(os.path.join(ROOT, "tests", "fixtures_leads.json")))}


def load(monkeypatch, tmp_path, live="0", **envs):
    for k in ("LEADS_STATE_KEY", "TG_BOT_TOKEN", "LEADS_WHATSAPP", "LEADS_TELEGRAM_GROUP_URL", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("LEADS_LIVE", live)
    for k, v in envs.items():
        monkeypatch.setenv(k, v)
    import leads
    importlib.reload(leads)
    monkeypatch.setattr(leads, "STATE_LOCAL", str(tmp_path / "state.json"))
    monkeypatch.setattr(leads, "STATE_ENC", str(tmp_path / "state.enc"))
    return leads, json.load(open(leads.CONFIG))


def test_bahar_owner_high(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    l = leads.classify(POSTS["T_BAHAR_OWNER"], cfg, NOW)
    assert l["priority"] == "high"
    assert (l["building"], l["district"], l["unit_type"], l["price_aed"], l["term"]) == ("Bahar", "JBR", "1BR", 12900, "monthly")
    assert "напрямую" in l["quote"]
    assert leads.place_of(l) == "Bahar, JBR"


def test_real_bahar6_is_medium(monkeypatch, tmp_path):
    # «без комиссии» и «срочно» не подтверждают собственника
    leads, cfg = load(monkeypatch, tmp_path)
    l = leads.classify(POSTS["T_BAHAR6_REAL"], cfg, NOW)
    assert l["priority"] == "medium" and l["building"] == "Bahar 6" and l["district"] == "JBR"


def test_skips(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    expect = {"T_SEEKER": "ищет", "T_NO_AGENTS": "агентов", "T_SALE": "продажа", "T_SERVICE": "реклама",
              "T_RENTED": "сдана", "T_OLD": "старше", "T_ENGLISH": "не русский"}
    for pid, word in expect.items():
        l = leads.classify(POSTS[pid], cfg, NOW)
        assert l["priority"] is None and word in l["reason"], (pid, l["reason"])


def test_medium_town_square(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    l = leads.classify(POSTS["T_MEDIUM_TS"], cfg, NOW)
    assert l["priority"] == "medium" and l["district"] == "Town Square" and l["term"] == "yearly"


def test_rerun_and_repost(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    posts = list(POSTS.values())
    r1, st = leads.run(cfg, posts=posts, now=NOW, log=lambda *a: None)
    kinds = {pid: k for k, pid, _ in r1}
    assert kinds["T_BAHAR_OWNER"] == "high" and kinds["T_BAHAR_REPOST"] == "перепубликация"
    r2, st2 = leads.run(cfg, posts=posts, now=NOW, log=lambda *a: None)
    assert all(k in ("повтор", "пропуск", "перепубликация") for k, _, _ in r2)
    assert st2["contacts"]["2026-11-25"] == 1


def test_dm_unavailable_goes_to_queue(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    l = leads.classify(POSTS["T_BAHAR_OWNER"], cfg, NOW)
    assert leads.plan_actions(l, cfg) == ["like:queue", "dm:queue", "comment:queue"]
    cfg["auto_comment"] = True
    assert leads.plan_actions(l, cfg)[-1] == "comment:after_dm"
    cfg["comment_before_dm"] = True
    assert leads.plan_actions(l, cfg)[-1] == "comment:auto_now"
    card = leads.card_text(l, cfg, "+971500000000")
    assert "Написал вам" in card and "ещё актуальна" in card and "+971500000000" in card


def test_texts(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    assert leads.comment_text("Bahar, JBR", False) == "Здравствуйте! Квартира в Bahar, JBR ещё актуальна? Можем обсудить в личных сообщениях?"
    assert "Написал вам" in leads.comment_text("Bahar, JBR", True)
    assert "комиссию оплачивает мой клиент" in leads.dm_text("Bahar, JBR", "+971")


def test_stay_here_without_tg_link(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    t = leads.followup_text("Давайте здесь, не пользуюсь WhatsApp", "Bahar, JBR", tg_url="")
    assert "Могу предложить вашу квартиру русскоязычным клиентам" in t
    assert "когда можно посмотреть" in t and "t.me" not in t
    t2 = leads.followup_text("давайте здесь", "Bahar, JBR", tg_url="https://t.me/test")
    assert "https://t.me/test" in t2
    assert leads.followup_text("Сколько комиссия и можно ли торг?", "Bahar, JBR") is None


def test_client_phrase_only_on_match(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    l = leads.classify(POSTS["T_BAHAR_OWNER"], cfg, NOW)
    l["available_from"] = "2026-12-01"
    req = [{"district": "JBR", "unit_type": "1BR", "budget_max_aed": 14000, "term": "monthly", "move_in_by": "2026-12-05"}]
    assert leads.match_client(l, req) is True
    assert leads.match_client(l, [dict(req[0], budget_max_aed=10000)]) is False
    assert "есть русскоязычный клиент" in leads.followup_text("давайте здесь", "x", "", client_match=True)


def test_timeout_checks_before_retry(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    st = leads.load_state()
    st["leads"]["P1"] = {"permalink": "", "building": "Bahar", "district": "JBR"}
    st["pending"]["P1"] = {"at": 1000, "tries": 1}
    sent = []
    monkeypatch.setattr(leads, "reply_exists", lambda pid, since, text="": "R_EXISTING")
    monkeypatch.setattr(leads, "publish_reply", lambda pid, text: sent.append(text) or "NEW")
    leads.do_comment(st, "P1", False, lambda *a: None)
    assert st["leads"]["P1"]["comment_id"] == "R_EXISTING" and sent == []


def test_live_without_key_refuses(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path, live="1")
    try:
        leads.save_state({})
        assert False
    except SystemExit as e:
        assert "LEADS_STATE_KEY" in str(e)


def test_encrypted_state_roundtrip(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path, LEADS_STATE_KEY="secret")
    st = leads.load_state()
    st["leads"]["X"] = {"a": "Бахар"}
    leads.save_state(st)
    assert "Бахар" not in open(tmp_path / "state.enc").read()
    assert leads.load_state()["leads"]["X"]["a"] == "Бахар"


def test_old_publisher_untouched():
    import publish
    assert publish.slot_time("2026-11-24_1617", {"am": "10:17", "pm": "19:17"}, {}).strftime("%H:%M") == "16:17"
    assert "2026-11-24_1617" in publish.list_posts()


def test_reply_exists_by_text_when_replied_to_hidden(monkeypatch, tmp_path):
    leads, cfg = load(monkeypatch, tmp_path)
    monkeypatch.setattr(leads, "call", lambda *a, **k: {"data": [{"id": "R9", "text": "Здравствуйте! Квартира в Bahar, JBR ещё актуальна? Можем обсудить в личных сообщениях?", "replied_to": None}]})
    assert leads.reply_exists("P1", 0, leads.comment_text("Bahar, JBR", False)) == "R9"
    assert leads.reply_exists("P1", 0, leads.comment_text("JVC", False)) is None
