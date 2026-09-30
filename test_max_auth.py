"""Проверка max_auth.py: сверка с эталоном из ТЗ (TypeScript на Node) + негативные сценарии.
Запуск: ./venv/bin/python test_max_auth.py"""
import json
import subprocess
import time
from urllib.parse import quote

import max_auth
from max_auth import AuthError, compute_hash, validate_init_data

TOKEN = "f9LHodD0cOKHx-test_token:with/specials=+"      # токен с «опасными» символами
NOW = 1_800_000_000.0
MAXAGE = 86400


def build(user=None, auth_date=NOW - 10, extra=None, token=TOKEN, drop=(), tamper=None):
    """Собираем initData так, как это делает MAX (значения — с двойным кодированием вложенного JSON)."""
    user = user if user is not None else {"id": 67890, "first_name": "Макс", "last_name": "Юзер+плюс",
                                          "username": None, "language_code": "ru", "photo_url": None}
    params = {
        "auth_date": str(int(auth_date)),
        "chat": json.dumps({"id": 12345, "type": "DIALOG"}, separators=(",", ":")),
        "ip": "192.168.0.1",
        "query_id": "4c0ab423-342b-4e45-aea4-2747dbc500cd",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
    }
    params.update(extra or {})
    for k in drop:
        params.pop(k, None)
    h = compute_hash(params, token)
    if tamper:
        params[tamper[0]] = tamper[1]
    parts = [f"{k}={quote(v, safe='')}" for k, v in params.items()] + [f"hash={h}"]
    return "&".join(parts)


def expect_fail(label, raw, **kw):
    try:
        validate_init_data(raw, kw.pop("token", TOKEN), kw.pop("max_age", MAXAGE), now=kw.pop("now", NOW))
    except AuthError as e:
        print(f"OK   {label}  ({e.reason})")
        return e
    raise SystemExit(f"FAIL {label}: данные были приняты")


def check(cond, label):
    print(("OK   " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


# ---------- 1. сверка с эталоном из ТЗ ----------
REFERENCE_JS = r"""
const validateAppData = async (appData, botToken) => {
  const params = appData.split('&').map((x) => x.split('='));
  if (params.filter((x) => x[0] === 'hash').length !== 1) return false;
  const originalHash = params.find((x) => x[0] === 'hash');
  if (!originalHash || typeof originalHash[1] !== 'string') return false;
  for (const param of params) param[1] = decodeURIComponent(param[1]);
  params.sort((a, b) => a[0].localeCompare(b[0]));
  const launchParams = params.filter((x) => x[0] !== 'hash').map((x) => `${x[0]}=${x[1]}`).join('\n');
  const encoder = new TextEncoder();
  const k1 = await crypto.subtle.sign('HMAC',
    await crypto.subtle.importKey('raw', encoder.encode('WebAppData'), {name:'HMAC', hash:{name:'SHA-256'}}, false, ['sign']),
    encoder.encode(botToken));
  const sig = await crypto.subtle.sign('HMAC',
    await crypto.subtle.importKey('raw', k1, {name:'HMAC', hash:{name:'SHA-256'}}, false, ['sign']),
    encoder.encode(launchParams));
  const hash = Array.from(new Uint8Array(sig)).map(b => ('00' + b.toString(16)).slice(-2)).join('');
  return hash === originalHash[1];
};
const [data, token] = JSON.parse(process.env.REF_INPUT);
validateAppData(data, token).then(r => console.log(r));
"""


def reference(data, token):
    import os
    out = subprocess.run(["/opt/homebrew/bin/node", "-e", REFERENCE_JS], capture_output=True, text=True,
                         timeout=20, env={**os.environ, "REF_INPUT": json.dumps([data, token])})
    if out.returncode != 0:
        raise SystemExit("эталонный скрипт упал:\n" + out.stderr)
    return out.stdout.strip() == "true"


good = build()
check(reference(good, TOKEN) is True, "эталон из ТЗ (TypeScript) принимает наши данные")
check(reference(build(tamper=("ip", "6.6.6.6")), TOKEN) is False, "эталон отклоняет подменённые данные")
launch = validate_init_data(good, TOKEN, MAXAGE, now=NOW)
check(launch.max_user_id == 67890 and launch.user["first_name"] == "Макс" and launch.user["last_name"] == "Юзер+плюс",
      "наша реализация принимает те же данные и отдаёт user (кириллица и «+» целы)")

# ---------- 2. URL-фрагмент из документации ----------
url = "https://example.com#WebAppData=" + quote(good, safe="") + "&WebAppPlatform=web&WebAppVersion=26.2.8"
check(validate_init_data(url, TOKEN, MAXAGE, now=NOW).max_user_id == 67890,
      "принимает полный URL с #WebAppData=… (двойное кодирование)")
check(validate_init_data("#WebAppData=" + quote(good, safe=""), TOKEN, MAXAGE, now=NOW).max_user_id == 67890,
      "принимает фрагмент #WebAppData=…")

# ---------- 3. атаки и ошибки ----------
expect_fail("подмена значения user (повышение до другого id)",
            build(tamper=("user", json.dumps({"id": 1}))))
expect_fail("чужой токен бота", good, token="другой-токен")
expect_fail("hash отсутствует", good.rsplit("&hash=", 1)[0])
expect_fail("hash повторяется", good + "&hash=" + "0" * 64)
expect_fail("параметр повторяется", good.replace("ip=", "ip=1.1.1.1&ip=", 1))
expect_fail("мусорный hash", good.rsplit("hash=", 1)[0] + "hash=zz")
expect_fail("пустой hash", good.rsplit("hash=", 1)[0] + "hash=")
expect_fail("пустая строка", "")
expect_fail("None", None)
expect_fail("объект вместо строки", {"user": {"id": 1}})
expect_fail("слишком длинный", "a=" + "x" * 9000)
expect_fail("параметр без значения", "auth_date")
expect_fail("BOT_TOKEN не настроен", good, token="")

old = expect_fail("устаревший auth_date", build(auth_date=NOW - MAXAGE - 5))
check(old.expired is True, "устаревшие данные помечены expired (понятное сообщение пользователю)")
expect_fail("auth_date из будущего", build(auth_date=NOW + 3600))
expect_fail("auth_date отсутствует", build(drop=("auth_date",)))
expect_fail("auth_date не число", build(extra={"auth_date": "abc"}))
expect_fail("нет пользователя", build(drop=("user",)))
expect_fail("user — не JSON", build(extra={"user": "not-json"}))
expect_fail("user.id — не число", build(user={"id": "abc"}))
expect_fail("user.id — bool", build(user={"id": True}))
check(validate_init_data(build(auth_date=NOW - 10 ** 9), TOKEN, 0, now=NOW).max_user_id == 67890,
      "AUTH_MAX_AGE=0 отключает проверку возраста")

# значения с «=» внутри (base64 в start_param) не ломают разбор
ok = build(extra={"start_param": "YWJj=="})
check(reference(ok, TOKEN) and validate_init_data(ok, TOKEN, MAXAGE, now=NOW).params["start_param"] == "YWJj==",
      "значение с «=» внутри: эталон и наша реализация согласны")

print("MAX_AUTH: ВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
