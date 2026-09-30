"""Сквозная проверка бэкенда через WebSocket (временная БД). Запуск: python smoke_test.py"""
import os
import tempfile
from datetime import date, timedelta

# Тест НИКОГДА не должен трогать реальную БД из .env: пустые значения не перезаписываются load_env
os.environ["SQLITE_PATH"] = os.path.join(tempfile.mkdtemp(), "t.db")
for _k in ("DATABASE_URL", "DB_HOST", "DB_PASSWORD"):
    os.environ[_k] = ""
os.environ["SEED_DEMO"] = "true"
os.environ["DEV_AUTH"] = "true"            # общие сценарии идут через неподписанный user.id
os.environ["DEV_FALLBACK_USER"] = "true"
os.environ["BOT_TOKEN"] = "123456:test-bot-token"
os.environ["BOT_NOTIFY"] = "true"
os.environ["WATCH_INTERVAL_SECONDS"] = "0.3"

import json  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

import uvicorn  # noqa: E402
from websockets.sync.client import connect  # noqa: E402

from app import app  # noqa: E402

PORT = 8765
server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="warning"))
threading.Thread(target=server.run, daemon=True).start()
while not server.started:
    time.sleep(0.05)


def init(uid):
    return {"user": {"id": uid}}


class Client:
    def __init__(self, _tc, uid):
        self.ws = connect(f"ws://127.0.0.1:{PORT}/ws")
        self.n = 0
        self.snap = self.call("hello", initData=init(uid))["data"]

    def call(self, action, payload=None, **extra):
        self.n += 1
        self.ws.send(json.dumps({"id": self.n, "action": action, "payload": payload or {}, **extra}))
        while True:
            m = json.loads(self.ws.recv(timeout=10))
            if m.get("type") == "response" and m["id"] == self.n:
                return m

    def next_event(self):
        return json.loads(self.ws.recv(timeout=10))


class _TC:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        server.should_exit = True


TestClient = lambda _app: _TC()  # noqa: E731


def check(cond, label):
    print(("OK   " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


with TestClient(app) as tc:
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    owner, admin, emp = Client(tc, 1), Client(tc, 2), Client(tc, 4)

    check(owner.snap["me"]["role"] == "owner" and admin.snap["me"]["role"] == "admin", "роли определены")
    check(len(owner.snap["users"]) == 5, "5 сотрудников")
    check(2 not in [u for u in admin.snap["assignableUserIds"]] and 1 not in admin.snap["visibleUserIds"],
          "админ не видит владельца и не назначает себе")
    check(emp.snap["assignableUserIds"] == [], "сотрудник никому не назначает")
    check(len(admin.snap["deadlines"]) > 0 and len(admin.snap["instances"]) > 0, "демо-данные загружены")

    # права
    r = emp.call("task.create", {"type": "single", "title": "x", "date": tomorrow, "endTime": "10:00",
                                 "employeeIds": [4]})
    check(not r["ok"] and r["error"] == "forbidden", "сотрудник не может создавать")

    # создание + realtime
    r = admin.call("task.create", {"type": "single", "title": "Новая", "date": tomorrow, "startTime": "18:00",
                                   "endTime": "19:00", "employeeIds": [4, 5], "weight": 5, "estimate": 60,
                                   "color": "#22C55E", "important": True})
    check(r["ok"] and len(r["data"]["created"]) == 2, "разовая задача на двоих")
    ev = emp.next_event()
    check(ev["type"] == "data_changed" and ev["reason"] == "task.create", "сотрудник получил data_changed")
    snap = emp.call("snapshot")["data"]
    mine = [i for i in snap["instances"] if i["title"] == "Новая"]
    check(len(mine) == 1 and mine[0]["assignee"] == 4 and mine[0]["important"] is True
          and mine[0]["color"] == "#22C55E", "сотрудник видит свою задачу с цветом/важностью")

    # конфликт (двойная проверка)
    r = admin.call("task.create", {"type": "single", "title": "Пересечение", "date": tomorrow,
                                   "startTime": "18:30", "endTime": "19:30", "employeeIds": [4]})
    check(not r["ok"] and r["error"] == "conflict", "конфликт разовых задач")
    r = admin.call("task.create", {"type": "single", "title": "Право", "date": tomorrow, "startTime": "10:00",
                                   "endTime": "11:00", "employeeIds": [1]})
    check(not r["ok"] and r["error"] == "forbidden", "админ не назначает владельцу")
    # откат: после отказа лишних записей нет
    n_before = len(admin.call("snapshot")["data"]["instances"])
    admin.call("task.create", {"type": "single", "title": "Пересечение", "date": tomorrow,
                               "startTime": "18:30", "endTime": "19:30", "employeeIds": [5, 4]})
    check(len(admin.call("snapshot")["data"]["instances"]) == n_before, "при конфликте ничего не записано")

    # перенос / удаление
    iid = mine[0]["id"]
    r = admin.call("task.update", {"instanceId": iid, "startTime": "20:00", "endTime": "21:00", "title": "Новая2"})
    check(r["ok"], "перенос задачи")
    r = admin.call("task.delete", {"instanceId": iid})
    check(r["ok"], "удаление задачи")

    # регулярная
    r = admin.call("task.create", {"type": "regular", "title": "Планёрка", "weekdays": [1, 2, 3, 4, 5, 6, 0],
                                   "startTime": "09:00", "deadlineTime": "09:30", "employeeIds": [4]})
    check(r["ok"], "регулярная задача создана")
    r = admin.call("task.create", {"type": "regular", "title": "Пересекается", "weekdays": [1, 2, 3],
                                   "startTime": "09:15", "deadlineTime": "10:00", "employeeIds": [4]})
    check(not r["ok"] and r["error"] == "conflict", "конфликт регулярных")
    snap = admin.call("snapshot")["data"]
    reg = [i for i in snap["instances"] if i["title"] == "Планёрка"]
    tid = reg[0]["task_id"]
    check(len(reg) >= 50 and snap["tasks"][str(tid)]["type"] == "regular"
          and snap["tasks"][str(tid)]["schedule_rule"]["weekdays"] == [1, 2, 3, 4, 5, 6, 0]
          or sorted(snap["tasks"][str(tid)]["schedule_rule"]["weekdays"]) == [0, 1, 2, 3, 4, 5, 6],
          f"экземпляры материализованы ({len(reg)}), правило вернулось")
    r = admin.call("task.delete", {"instanceId": reg[0]["id"]})
    check(r["ok"] and r["data"]["series"], "удалён весь ряд")
    check(not [i for i in admin.call("snapshot")["data"]["instances"] if i["title"] == "Планёрка"],
          "экземпляров ряда не осталось")

    # пул
    r = admin.call("task.create", {"type": "single", "title": "В пул", "date": tomorrow, "endTime": "23:00",
                                   "isPool": True})
    check(r["ok"], "задача в пул")
    pool = [i for i in emp.call("snapshot")["data"]["instances"] if i["title"] == "В пул"][0]
    r = emp.call("pool.take", {"instanceId": pool["id"]})
    check(r["ok"], "сотрудник взял из пула")
    r2 = Client(tc, 5).call("pool.take", {"instanceId": pool["id"]})
    check(not r2["ok"] and r2["error"] == "conflict", "второй сотрудник не может взять")
    check(emp.call("pool.return", {"instanceId": pool["id"]})["ok"], "отказ от задачи")

    # дедлайны
    r = admin.call("deadline.create", {"title": "Выборочный", "deadlineAt": tomorrow + "T12:00",
                                       "employeeIds": [4, 5], "selective": True})
    check(r["ok"], "выборочный дедлайн")
    dl = [d for d in emp.call("snapshot")["data"]["deadlines"] if d["text"] == "Выборочный"][0]
    check(dl["assignedUserId"] is None and dl["availableForIds"] == [4, 5], "виден как выборочный")
    check(emp.call("deadline.accept", {"id": dl["id"]})["ok"], "дедлайн принят")
    r = Client(tc, 5).call("deadline.accept", {"id": dl["id"]})
    check(not r["ok"], "повторное принятие невозможно")
    dl = [d for d in emp.call("snapshot")["data"]["deadlines"] if d["text"] == "Выборочный"][0]
    check(dl["assignedUserId"] == 4 and dl["availableForIds"] is None, "дедлайн закреплён")
    check(admin.call("deadline.delete", {"id": dl["id"]})["ok"], "дедлайн удалён")
    check(not admin.call("deadline.delete", {"id": dl["id"]})["ok"], "повторное удаление — not_found")

    # валидация
    r = admin.call("task.create", {"type": "single", "title": "  ", "date": tomorrow, "endTime": "10:00",
                                   "employeeIds": [4]})
    check(not r["ok"] and r["error"] == "bad_request", "пустое название отклонено")
    r = admin.call("nope")
    check(not r["ok"], "неизвестное действие отклонено")

    # ---------- обратная связь с ботом ----------
    from datetime import datetime as _dt
    from database import db as _db
    from models import BotNotification, TaskInstance, User

    def outbox():
        with _db.session() as ses:
            return [(n.user_id, n.text) for n in ses.query(BotNotification).order_by(BotNotification.notification_id)]

    def uid_of(max_id):
        with _db.session() as ses:
            return ses.query(User.user_id).filter(User.max_id == max_id).scalar()

    emp_uid = uid_of(4)
    while True:                                   # вычитываем накопленные события
        try:
            emp.ws.recv(timeout=0.5)
        except TimeoutError:
            break
    outbox_before = len(outbox())

    r = admin.call("task.create", {"type": "single", "title": "Для бота", "date": tomorrow, "startTime": "08:00",
                                   "endTime": "09:00", "employeeIds": [4]})
    check(r["ok"], "задача для проверки уведомлений")
    new = outbox()[outbox_before:]
    check(len(new) == 1 and new[0][0] == emp_uid and "Для бота" in new[0][1]
          and "назначена" in new[0][1], "назначение → уведомление сотруднику")
    check(all(u != uid_of(2) for u, _ in new), "автору уведомление не шлём")

    target = [i for i in admin.call("snapshot")["data"]["instances"] if i["title"] == "Для бота"][0]
    admin.call("task.update", {"instanceId": target["id"], "date": tomorrow, "startTime": "21:00", "endTime": "22:00"})
    last = outbox()[-1]
    check(last[0] == emp_uid and "перенесена" in last[1] and "08:00" in last[1] and "21:00" in last[1],
          "перенос → уведомление «было/стало»")

    # бот отмечает задачу выполненной ПРЯМО В БД (мини-апп не участвует)
    while True:
        try:
            emp.ws.recv(timeout=0.5)
        except TimeoutError:
            break
    with _db.session() as ses:
        inst = ses.get(TaskInstance, target["id"])
        inst.actual_start = inst.actual_end = _dt.now()
    ev = emp.next_event()
    check(ev["type"] == "data_changed" and ev["reason"] == "external", "внешнее изменение бота → data_changed")
    got = [i for i in emp.call("snapshot")["data"]["instances"] if i["id"] == target["id"]][0]
    check(got["actual_end"] is not None, "мини-апп видит actual_end (клиент отнесёт в «Сделано»)")

    # наши собственные правки не считаются «внешними»
    admin.call("task.create", {"type": "single", "title": "Ещё", "date": tomorrow, "startTime": "12:00",
                               "endTime": "13:00", "employeeIds": [5]})
    try:
        extra_ev = None
        for _ in range(3):
            m = emp.ws.recv(timeout=1.0)
            m = json.loads(m)
            if m.get("reason") == "external":
                extra_ev = m
        check(extra_ev is None, "свои изменения не порождают ложный external")
    except TimeoutError:
        check(True, "свои изменения не порождают ложный external")

    # ---------- владелец назначает задачу только себе ----------
    n0 = len(outbox())
    r = owner.call("task.create", {"type": "single", "title": "Себе", "date": tomorrow, "startTime": "06:00",
                                   "endTime": "06:30", "employeeIds": [1]})
    mine_n = outbox()[n0:]
    check(r["ok"] and len(mine_n) == 1 and mine_n[0][0] == uid_of(1) and "Себе" in mine_n[0][1],
          "задача только себе → автор получает уведомление")
    n1 = len(outbox())
    owner.call("task.create", {"type": "single", "title": "Себе и другим", "date": tomorrow, "startTime": "07:00",
                               "endTime": "07:30", "employeeIds": [1, 4]})
    both = outbox()[n1:]
    check(sorted(u for u, _ in both) == [uid_of(4)], "задача себе и другим → автору не шлём")

    # ---------- WebSocket-канал с ботом (/ws/bot) ----------
    from save_config import settings as _st
    from websockets.exceptions import ConnectionClosed

    def unsent():
        with _db.session() as ses:
            return ses.query(BotNotification).filter(BotNotification.sent_at.is_(None)).count()

    def drain(cl, t=0.4):
        while True:
            try:
                cl.ws.recv(timeout=t)
            except TimeoutError:
                return

    # 1) бот офлайн: уведомление копится в очереди
    drain(emp)
    r = admin.call("task.create", {"type": "single", "title": "Для офлайн-бота", "date": tomorrow,
                                   "startTime": "14:00", "endTime": "15:00", "employeeIds": [4]})
    check(r["ok"] and unsent() >= 1, "бот офлайн → уведомление ждёт в очереди")

    # 2) токен: неверный отклоняется, верный принимается
    _st.BRIDGE_TOKEN = "s3cret@token"
    try:
        bad = connect(f"ws://127.0.0.1:{PORT}/ws/bot", additional_headers={"Authorization": "Bearer nope"})
        try:
            bad.recv(timeout=3)
            rejected = False
        except ConnectionClosed:
            rejected = True
    except Exception:
        rejected = True
    check(rejected, "неверный токен отклонён")

    bot = connect(f"ws://127.0.0.1:{PORT}/ws/bot", additional_headers={"Authorization": "Bearer s3cret@token"})

    def bot_recv(t=5):
        return json.loads(bot.recv(timeout=t))

    # 3) при подключении бота накопленное уходит само; ack фиксирует доставку
    backlog = 0
    while True:                                   # сначала уходит всё накопленное — по порядку
        got = bot_recv()
        if "Для офлайн-бота" in got["text"]:
            break
        bot.send(json.dumps({"type": "ack", "id": got["id"], "ok": True}))
        backlog += 1
    check(got["type"] == "notify" and got["maxId"] == 4,
          f"бот подключился → получил накопленное по порядку ({backlog} ранее + нужное), maxId сотрудника")
    bot.send(json.dumps({"type": "ack", "id": got["id"], "ok": True}))
    time.sleep(0.5)
    with _db.session() as ses:
        check(ses.get(BotNotification, got["id"]).sent_at is not None, "ack → sent_at проставлен")

    # 4) онлайн: новое назначение приходит боту сразу
    r = admin.call("task.create", {"type": "single", "title": "Онлайн-назначение", "date": tomorrow,
                                   "startTime": "16:00", "endTime": "17:00", "employeeIds": [5]})
    got = bot_recv()
    check(r["ok"] and got["type"] == "notify" and got["maxId"] == 5 and "Онлайн-назначение" in got["text"],
          "онлайн: назначение доставлено боту по WebSocket")
    bot.send(json.dumps({"type": "ack", "id": got["id"], "ok": False, "error": "user blocked bot"}))
    time.sleep(0.5)
    with _db.session() as ses:
        n = ses.get(BotNotification, got["id"])
        check(n.sent_at is None and n.attempts == 1 and "blocked" in (n.last_error or ""),
              "отрицательный ack → повтор позже, ошибка записана")

    # 5) бот → мини-апп: событие «бот изменил задачи» обновляет клиентов
    drain(emp)
    bot.send(json.dumps({"type": "event", "reason": "changed", "orgIds": None}))
    ev = emp.next_event()
    check(ev["type"] == "data_changed" and ev["reason"] == "bot.changed", "событие бота → data_changed клиентам")

    # 6) обрыв связи не ломает мини-апп
    bot.close()
    time.sleep(0.3)
    r = admin.call("task.create", {"type": "single", "title": "После обрыва", "date": tomorrow,
                                   "startTime": "22:00", "endTime": "22:30", "employeeIds": [5]})
    check(r["ok"], f"после отключения бота мини-апп продолжает работать ({r.get('message')})")
    _st.BRIDGE_TOKEN = None

    # ---------- боевая авторизация: подписанный WebAppData ----------
    import hmac as _hmac, hashlib as _hl, time as _t
    from urllib.parse import quote as _q
    from save_config import settings as _s2
    from max_auth import compute_hash

    def signed(max_id, token="123456:test-bot-token", age=5, tamper=False):
        p = {"auth_date": str(int(_t.time()) - age), "query_id": "q-1", "ip": "1.2.3.4",
             "user": json.dumps({"id": max_id, "first_name": "Т"}, separators=(",", ":"), ensure_ascii=False)}
        h = compute_hash(p, token)
        if tamper:
            p["user"] = json.dumps({"id": 1}, separators=(",", ":"))     # подмена после подписи
        return "&".join(f"{k}={_q(v, safe='')}" for k, v in p.items()) + f"&hash={h}"

    def hello(init):
        c = connect(f"ws://127.0.0.1:{PORT}/ws")
        c.send(json.dumps({"id": 1, "action": "hello", "initData": init}))
        r = json.loads(c.recv(timeout=10))
        c.close()
        return r

    _s2.DEV_AUTH = False                                          # как в проде
    r = hello(signed(4))
    check(r["ok"] and r["data"]["me"]["role"] == "employee" and r["data"]["me"]["id"] == emp_uid,
          "подписанный WebAppData → вход под своим пользователем (по MaxId)")
    r = hello(signed(2))
    check(r["ok"] and r["data"]["me"]["role"] == "admin", "подписанный WebAppData админа → роль admin")
    r = hello(signed(4, tamper=True))
    check(not r["ok"] and r["error"] == "unauthorized", "подмена user после подписи → отказ")
    r = hello(signed(4, token="чужой:токен"))
    check(not r["ok"] and r["error"] == "unauthorized", "подпись чужим токеном → отказ")
    r = hello(signed(4, age=10 ** 6))
    check(not r["ok"] and "устарела" in r["message"], "просроченный auth_date → «сессия устарела»")
    r = hello(signed(999999))
    check(not r["ok"] and "не зарегистрированы" in r["message"], "подпись верна, но MaxId нет в организации → отказ")
    r = hello({"user": {"id": 1}})
    check(not r["ok"] and r["error"] == "unauthorized", "неподписанный объект user при DEV_AUTH=false → отказ")
    r = hello(None)
    check(not r["ok"], "без initData → отказ")

    # действия без hello и после неудачного hello невозможны
    c = connect(f"ws://127.0.0.1:{PORT}/ws")
    c.send(json.dumps({"id": 1, "action": "hello", "initData": signed(4, tamper=True)}))
    c.recv(timeout=10)
    c.send(json.dumps({"id": 2, "action": "snapshot"}))
    r = json.loads(c.recv(timeout=10))
    check(not r["ok"] and r["error"] == "unauthorized", "после неудачного hello данные не выдаются")
    c.close()

    # роль берётся из БД на каждый запрос, а не из подписи: смена роли применяется сразу
    from models import Member
    sess_c = Client.__new__(Client)
    sess_c.ws = connect(f"ws://127.0.0.1:{PORT}/ws"); sess_c.n = 0
    first = sess_c.call("hello", initData=signed(4))
    check(first["ok"], "вход подписанными данными для проверки смены роли")
    with _db.session() as ses:
        m = ses.query(Member).filter(Member.user_id == emp_uid).first()
        old_role = m.role_id; m.role_id = None
    r = sess_c.call("snapshot")
    with _db.session() as ses:
        ses.query(Member).filter(Member.user_id == emp_uid).first().role_id = old_role
    check(not r["ok"] and r["error"] == "unauthorized", "участник потерял роль в БД → соединение теряет доступ сразу")
    _s2.DEV_AUTH = True

    # ---------- пользователь вне организации / нарушение «один человек — одна организация» ----------
    from models import Organization, Role, User as _User

    with _db.session() as ses:
        org_a = ses.query(Organization).first()
        org_b = Organization(org_name="Вторая"); ses.add(org_b); ses.flush()
        role_emp = ses.query(Role).filter(Role.role_name == "Сотрудник").first().role_id
        specs = {
            9001: ("без Member", []),
            9002: ("Member без организации", [(None, role_emp)]),
            9003: ("Member без роли", [(org_a.organization_id, None)]),
            9004: ("в двух организациях", [(org_a.organization_id, role_emp), (org_b.organization_id, role_emp)]),
            9005: ("нормальный, одна организация", [(org_a.organization_id, role_emp)]),
        }
        for mid, (name, links) in specs.items():
            u = _User(user_name=name, max_id=mid); ses.add(u); ses.flush()
            for org_id, role_id in links:
                ses.add(Member(user_id=u.user_id, organization_id=org_id, role_id=role_id))
        for _ in range(2):                                  # два User с одним MaxId
            ses.add(_User(user_name="дубль", max_id=9006))

    _s2.DEV_AUTH = False
    r = hello(signed(9001))
    check(not r["ok"] and "не зарегистрированы" not in r["message"] and "пройдите регистрацию" not in r["message"]
          and "не состоите" in r["message"], "User без Member → «не состоите ни в одной организации»")
    r = hello(signed(9002))
    check(not r["ok"] and "настроена не полностью" in r["message"], "Member без организации → «настроена не полностью»")
    r = hello(signed(9003))
    check(not r["ok"] and "настроена не полностью" in r["message"], "Member без роли → «настроена не полностью»")
    r = hello(signed(9004))
    check(not r["ok"] and "нескольких организациях" in r["message"], "две организации → явный отказ, а не «первая попавшаяся»")
    r = hello(signed(9006))
    check(not r["ok"] and "нескольких организациях" in r["message"], "дубль MaxId у двух User → отказ")
    r = hello(signed(777777))
    check(not r["ok"] and "пройдите регистрацию" in r["message"], "MaxId нет в базе → «пройдите регистрацию в боте»")
    r = hello(signed(9005))
    check(r["ok"] and r["data"]["me"]["organizationId"] == org_a.organization_id and r["data"]["me"]["role"] == "employee",
          "нормальный пользователь с одной организацией входит")
    check(r["data"]["users"] and all(u["id"] != 0 for u in r["data"]["users"]), "снимок содержит участников своей организации")
    _s2.DEV_AUTH = True

print("ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
