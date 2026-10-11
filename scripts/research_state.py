#!/usr/bin/env python3
"""Состояние генеалогического ресерча: рабочая папка, единицы работы, слияние и проверка находок.

  python3 -I research_state.py init DIR [--input FILE ...]      # папка, manifest.json с хешами входов
  python3 -I research_state.py add DIR --id U01 --track T1 --title "Ивановский приход: адрес и где лежит" [--priority 1]
  python3 -I research_state.py set DIR U01 running|done|blocked|pending [--note "…"]
  python3 -I research_state.py status DIR                       # что сделано, что дальше
  python3 -I research_state.py check DIR --unit U01             # проверить одну единицу (перед set done)
  python3 -I research_state.py merge DIR                        # units/*/findings.jsonl → findings.jsonl
  python3 -I research_state.py check DIR [--fetch]              # правила доказательности → checks.md
  python3 -I research_state.py orders DIR                       # orders.csv — только полностью проверенное

Раскладка DIR:
  manifest.json   входы (путь, sha256, размер), дата, версия схемы
  plan.json       единицы работы: id, track, title, priority, status, history
  known.jsonl     инвентарь: {"kind":"cipher|file|claim", "value":…, "archive":…, "where":…};
                  полнота: {"kind":"scope", "value":"<архив>", "where":"complete|partial"}
  units/<id>/     brief.md, findings.jsonl, queries.jsonl, summary.md — пишет только агент этой единицы
  findings.jsonl  слитые находки; findings.checked.jsonl + checks.md — результат проверки; orders.csv

Схема находки — references/verification.md.
"""
import contextlib
import csv
import datetime
import fcntl
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import urllib.request

SCHEMA = 2
STATUSES = ("pending", "running", "done", "blocked")
ACCESS = ("snippet", "catalog_entry", "page", "scan", "user_inventory")
SOURCE_TYPES = ("primary", "catalog", "index", "secondary", "user_inventory")
IDENT = ("ours", "probable", "possible", "not_ours", "n/a")
VERIFIED = ("confirmed", "downgraded", "excluded")
# номер: 1121, 12а, 12/3, 1-а; опись: 2, 1 (доп.), 2доп, 70(1889)
NUM = r"\d+(?:\s*/\s*\d+)?(?:-?[а-я](?![а-я.]))?"
CIPHER = re.compile(
    r"ф(?:онд)?\s*\.?\s*(?P<f>[РрPИиRМмЭэ]?\s*-?\s*\d+[а-я]?)\s*[,.;]?\s*"
    r"оп(?:ись)?\s*\.?\s*(?P<o>\d+[а-я]?(?:\s*\((?:доп\.?|\d{4}|[а-я]+\.?)\))?(?:\s*доп\.?)?)\s*[,.;]?\s*"
    r"(?:д(?:ело)?|ед\.?\s*хр)\s*\.?\s*(?P<d>" + NUM + r")", re.I)
CIPHER_HINT = re.compile(r"\bф(?:онд)?\s*\.?\s*[РрPИиRМм]?\s*-?\s*\d", re.I)
MATCH_CATS = {"name": r"имя|отчеств", "place": r"деревн|село|сел[оа]|место|волост|почин|приход|двор",
              "age": r"возраст|год|лет\b|±", "family": r"сем|сын|доч|жена|муж|брат|сестр|отец|мать|внук|родн"}


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


@contextlib.contextmanager
def locked(d):
    """Блокировка всего read–modify–write в рабочей папке (параллельные add/set не теряют записи)."""
    with open(os.path.join(d, ".lock"), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def atomic_write(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def atomic_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=1))


def atomic_jsonl(path, rows):
    atomic_write(path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))


def load(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def jsonl(path):
    out, bad = [], []
    if not os.path.exists(path):
        return out, bad
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                try:
                    v = json.loads(line)
                    (out if isinstance(v, dict) else bad).append(v if isinstance(v, dict) else n)
                except json.JSONDecodeError:
                    bad.append(n)
    return out, bad


def file_hash(path):
    if not os.path.exists(path):
        return ""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_text(t):
    t = (t or "").lower()
    t = t.translate(str.maketrans({"ё": "е", "ѣ": "е", "і": "и", "ѳ": "ф", "ѵ": "и"}))
    t = re.sub(r"ъ\b", "", t)
    return " ".join(re.findall(r"[а-яa-z0-9]+", t))


def norm_archive(a):
    return re.sub(r"[^а-яa-z0-9]", "", (a or "").lower().replace("ё", "е"))


def parse_ciphers(text):
    """→ (список «фонд-опись-дело», есть ли нераспознанные упоминания фонда)."""
    text = text or ""
    res, spans = [], []
    for m in CIPHER.finditer(text):
        f = re.sub(r"[\s-]", "", m.group("f").lower()).translate(str.maketrans("pr", "рр"))
        o = re.sub(r"[\s.]", "", m.group("o").lower())
        dd = re.sub(r"\s", "", m.group("d").lower())
        res.append(f"{f}-{o}-{dd}")
        spans.append(m.span())
    hints = [h.start() for h in CIPHER_HINT.finditer(text)]
    unparsed = any(not any(a <= h < b for a, b in spans) for h in hints)
    return sorted(set(res)), unparsed


def cmd_init(d, inputs):
    for sub in ("units", "report", "sources"):
        os.makedirs(os.path.join(d, sub), exist_ok=True)
    with locked(d):
        man = load(os.path.join(d, "manifest.json"), {"schema": SCHEMA, "created": now(), "inputs": []})
        have = {i["path"] for i in man["inputs"]}
        for p in inputs:
            if p not in have:
                man["inputs"].append({"path": p, "sha256": file_hash(p), "size": os.path.getsize(p), "added": now()})
        atomic_json(os.path.join(d, "manifest.json"), man)
        if not os.path.exists(os.path.join(d, "plan.json")):
            atomic_json(os.path.join(d, "plan.json"), {"units": []})
        if not os.path.exists(os.path.join(d, "known.jsonl")):
            atomic_write(os.path.join(d, "known.jsonl"), "")
    print(f"OK: {d} (входов {len(man['inputs'])})")


def cmd_add(d, uid, track, title, prio):
    if not uid or not re.fullmatch(r"[A-Za-z0-9_-]+", uid):
        sys.exit("--id: латиница, цифры, _ и -")
    with locked(d):
        plan = load(os.path.join(d, "plan.json"), {"units": []})
        if any(u["id"] == uid for u in plan["units"]):
            sys.exit(f"единица {uid} уже есть")
        plan["units"].append({"id": uid, "track": track, "title": title, "priority": prio,
                              "status": "pending", "history": [[now(), "pending", ""]]})
        os.makedirs(os.path.join(d, "units", uid), exist_ok=True)
        atomic_json(os.path.join(d, "plan.json"), plan)
    print(f"OK: {uid} добавлена")


def cmd_set(d, uid, st, note):
    if st not in STATUSES:
        sys.exit(f"статус: {', '.join(STATUSES)}")
    with locked(d):
        plan = load(os.path.join(d, "plan.json"), {"units": []})
        u = next((u for u in plan["units"] if u["id"] == uid), None)
        if not u:
            sys.exit(f"нет единицы {uid}")
        u["status"] = st
        u["history"].append([now(), st, note])
        atomic_json(os.path.join(d, "plan.json"), plan)
    print(f"OK: {uid} → {st}")


def cmd_status(d):
    plan = load(os.path.join(d, "plan.json"), {"units": []})
    by = {s: [u for u in plan["units"] if u["status"] == s] for s in STATUSES}
    print(" · ".join(f"{s}: {len(v)}" for s, v in by.items()))
    for s in ("running", "blocked", "pending"):
        for u in sorted(by[s], key=lambda u: u.get("priority", 9)):
            n = len(jsonl(os.path.join(d, "units", u["id"], "findings.jsonl"))[0])
            last = u["history"][-1][2]
            print(f"  [{s}] {u['id']} {u['track']} p{u.get('priority', '?')} — {u['title']}"
                  f" (находок {n}){' — ' + last if last else ''}")


def ev_key(e):
    return (e.get("title") or "", e.get("locator") or "", e.get("url") or "")


def cmd_merge(d):
    merged, seen, bad_total = [], {}, 0
    udir = os.path.join(d, "units")
    for uid in sorted(os.listdir(udir)) if os.path.isdir(udir) else []:
        rows, bad = jsonl(os.path.join(udir, uid, "findings.jsonl"))
        bad_total += len(bad)
        for f in rows:
            f = dict(f, unit=f.get("unit", uid))
            subj = ",".join(sorted(map(str, (f.get("person_ids") or []) + (f.get("place_ids") or []))))
            k = (subj, norm_text(f.get("claim", "")), f.get("identification"), norm_archive(f.get("archive")))
            if k in seen:
                tgt = merged[seen[k]]
                have = {ev_key(e) for e in tgt.get("evidence") or []}
                for e in f.get("evidence") or []:
                    if ev_key(e) not in have:
                        tgt.setdefault("evidence", []).append(e)
                        have.add(ev_key(e))
                tgt.setdefault("also", []).append(f"{uid}:{f.get('id')}")
                continue
            seen[k] = len(merged)
            merged.append(f)
    # одно и то же утверждение с разными вердиктами — не сливать, а пометить противоречие
    by_claim = {}
    for f in merged:
        kk = (",".join(sorted(map(str, (f.get("person_ids") or []) + (f.get("place_ids") or [])))),
              norm_text(f.get("claim", "")), norm_archive(f.get("archive")))
        by_claim.setdefault(kk, []).append(f)
    for group in by_claim.values():
        if len({g.get("identification") for g in group}) > 1:
            for g in group:
                g["conflict_with"] = [x.get("id") for x in group if x is not g]
    with locked(d):
        atomic_jsonl(os.path.join(d, "findings.jsonl"), merged)
    print(f"OK: {len(merged)} находок (битых строк {bad_total}) → findings.jsonl")


def fetch_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (genealogy research check)"})
    with urllib.request.urlopen(req, timeout=20) as r:
        raw = r.read(3 * 2 ** 20)
        enc = r.headers.get_content_charset() or "utf-8"
    t = raw.decode(enc, errors="replace")
    t = re.sub(r"(?s)<(script|style)[^>]*>.*?</\1>", " ", t)
    return html.unescape(re.sub(r"<[^>]+>", " ", t))


def check_one(f, known_keys, known_bare, complete, do_fetch):
    flags, soft = [], []
    ev = f.get("evidence")
    if not isinstance(ev, list):
        flags.append("evidence должен быть списком")
        ev = []
    for fld in ("id", "claim", "identification", "inventory_status"):
        if not f.get(fld):
            flags.append(f"нет поля {fld}")
    if not ev:
        flags.append("нет ни одного свидетельства")
    ident = f.get("identification")
    if ident and ident not in IDENT:
        flags.append(f"identification вне {IDENT}")
    if f.get("inventory_status") not in (None, "known", "new", "unknown"):
        flags.append("inventory_status вне known|new|unknown")
    for e in ev:
        if not isinstance(e, dict):
            flags.append("свидетельство не объект")
            continue
        if e.get("access_level") not in ACCESS:
            flags.append(f"access_level вне {ACCESS}")
        if e.get("source_type") not in SOURCE_TYPES:
            flags.append(f"source_type вне {SOURCE_TYPES}")
        if not (e.get("locator") or e.get("url")):
            flags.append("свидетельство без locator/url")
        if len((e.get("excerpt") or "").split()) > 25:
            flags.append("цитата длиннее 25 слов (полный текст — в sources/<unit>/transcript.md)")
    ev = [e for e in ev if isinstance(e, dict)]
    strong = [e for e in ev if e.get("source_type") == "primary" and e.get("access_level") in ("page", "scan")]
    mo = f.get("match_on", [])
    if ident == "ours":
        if not strong:
            flags.append("«наш» без первоисточника, открытого на странице/скане")
        if not isinstance(mo, list):
            flags.append("match_on должен быть списком")
            mo = []
        cats = {c for m in mo for c, rx in MATCH_CATS.items() if re.search(rx, str(m), re.I)}
        if len(cats) < 2:
            flags.append("«наш» требует ≥2 НЕЗАВИСИМЫХ признаков в match_on: имя+отчество / деревня / возраст / семья")
    if ev and all(e.get("access_level") == "snippet" for e in ev) and ident in ("ours", "probable"):
        flags.append("только сниппет поисковой выдачи — максимум possible; открыть страницу")
    if ident == "not_ours" and not (f.get("completeness") or f.get("mismatch")):
        flags.append("исключение без поля completeness (документ полный: листы, годы) или mismatch (что не совпало)")
    # шифры: каждый упомянутый в claim/order должен быть виден в описи, на странице или в инвентаре
    order = f.get("order") or {}
    c_claim, u1 = parse_ciphers(f.get("claim", "") + " " + " ".join(str(v) for v in order.values()))
    seen_c, c_ev, u2 = set(), set(), False
    for e in ev:
        cs, uu = parse_ciphers((e.get("locator") or "") + " " + (e.get("excerpt") or ""))
        u2 = u2 or uu
        c_ev.update(cs)
        if e.get("access_level") in ("catalog_entry", "page", "scan", "user_inventory") and \
                e.get("source_type") in ("primary", "catalog", "user_inventory"):
            seen_c.update(cs)
    if u1 or (order and not parse_ciphers(order.get("cipher", ""))[0]):
        flags.append("шифр не распознан полностью — записать «ф. N, оп. N, д. N» целиком")
    unver = [c for c in c_claim if c not in seen_c]
    if unver:
        flags.append(f"шифр не подтверждён описью/документом: {', '.join(unver)}")
    arch = norm_archive(f.get("archive") or order.get("archive"))
    all_c = set(c_claim) | c_ev
    hits = [c for c in all_c if (arch and (arch, c) in known_keys)]
    bare = [c for c in all_c if c in known_bare and c not in hits]
    if (hits or bare) and f.get("inventory_status") == "new":
        if hits:
            soft.append(f"помечено НОВОЕ, но шифр есть в инвентаре: {', '.join(hits)} → исправлено на known")
            f["inventory_status"] = "known"
        else:
            soft.append(f"такой же шифр есть в инвентаре без/с другим архивом: {', '.join(bare)} — сверить архив")
    if f.get("inventory_status") == "new" and arch and arch not in complete and "*" not in complete:
        soft.append("НОВОЕ при неполном инвентаре по этому архиву → исправлено на unknown (нужна строка scope complete)")
        f["inventory_status"] = "unknown"
    vr = f.get("verified")
    if vr is not None and (not isinstance(vr, dict) or vr.get("result") not in VERIFIED):
        flags.append(f"verified.result вне {VERIFIED}")
    if f.get("conflict_with"):
        soft.append(f"противоречит находкам {f['conflict_with']} — решить при проверке")
    if do_fetch:
        for e in ev:
            url = e.get("url") or (e.get("locator") if str(e.get("locator", "")).startswith("http") else "")
            if url and e.get("excerpt") and e.get("access_level") == "page":
                try:
                    page = norm_text(fetch_text(url))
                    probe = norm_text(e["excerpt"])[:60]
                    e["fetch_check"] = "цитата найдена" if probe and probe in page else "цитата НЕ найдена"
                    if e["fetch_check"] == "цитата НЕ найдена":
                        flags.append(f"цитата не найдена на {url[:80]}")
                except Exception as ex:  # сеть/JS-сайты: не провал, но и не подтверждение
                    e["fetch_check"] = f"не проверено автоматически ({type(ex).__name__})"
                    soft.append(f"цитату не удалось проверить автоматически: {url[:80]}")
    return flags, soft


def known_index(d):
    known, _ = jsonl(os.path.join(d, "known.jsonl"))
    keys, bare, complete = set(), set(), set()
    for k in known:
        if k.get("kind") == "scope":
            if k.get("where") == "complete":
                for name in [k.get("value")] + list(k.get("aliases") or []):
                    complete.add("*" if str(name).strip() in ("*", "все", "all") else norm_archive(name))
            continue
        cs, _ = parse_ciphers(k.get("value", ""))
        a = norm_archive(k.get("archive"))
        for c in cs:
            (keys.add((a, c)) if a else None)
            bare.add(c)
    return keys, bare, complete


def cmd_check(d, do_fetch, unit=None):
    src = os.path.join(d, "units", unit, "findings.jsonl") if unit else os.path.join(d, "findings.jsonl")
    rows, bad = jsonl(src)
    keys, bare, complete = known_index(d)
    out, n_flag = [], 0
    for f in rows:
        flags, soft = check_one(f, keys, bare, complete, do_fetch)
        f["check"] = {"ok": not flags, "flags": flags, "soft": soft, "at": now()}
        n_flag += bool(flags)
        out.append(f)
    if unit:
        print(f"{unit}: находок {len(rows)}, битых строк {len(bad)}, с замечаниями {n_flag}")
        for f in out:
            for x in f["check"]["flags"]:
                print(f"  {f.get('id', '?')}: {x}")
            for x in f["check"]["soft"]:
                print(f"  {f.get('id', '?')}: (обратить внимание) {x}")
        if bad or n_flag:
            print("→ не ставить done: вернуть агенту (set pending --note …)")
        return
    stamp = {"findings": file_hash(os.path.join(d, "findings.jsonl")),
             "known": file_hash(os.path.join(d, "known.jsonl")), "at": now()}
    with locked(d):
        atomic_jsonl(os.path.join(d, "findings.checked.jsonl"), out)
        atomic_json(os.path.join(d, "checks.stamp.json"), stamp)
        L = [f"# Проверка находок ({now()})\n", f"- прошли: {len(out) - n_flag}", f"- с замечаниями: {n_flag}",
             f"- битых строк в findings.jsonl: {len(bad)}\n"]
        for f in out:
            if f["check"]["flags"] or f["check"]["soft"]:
                L.append(f"## {f.get('id', '?')} — {str(f.get('claim', ''))[:120]}")
                L += [f"- {x}" for x in f["check"]["flags"]] + [f"- (обратить внимание) {x}" for x in f["check"]["soft"]]
                L.append("")
        atomic_write(os.path.join(d, "checks.md"), "\n".join(L) + "\n")
    print(f"OK: прошли {len(out) - n_flag}, с замечаниями {n_flag} → checks.md, findings.checked.jsonl")


def cmd_orders(d):
    rows, _ = jsonl(os.path.join(d, "findings.checked.jsonl"))
    stamp = load(os.path.join(d, "checks.stamp.json"), {})
    if not rows or not stamp:
        sys.exit("сначала merge и check")
    if stamp.get("findings") != file_hash(os.path.join(d, "findings.jsonl")) or \
            stamp.get("known") != file_hash(os.path.join(d, "known.jsonl")):
        sys.exit("findings.jsonl или known.jsonl изменились после check — запусти check заново")
    n = skipped = already = 0
    out_rows = [["архив", "шифр", "что", "годы", "населённый пункт", "что ищем", "эл. копия",
                 "уже есть/заказано", "ориентир цены", "источник цены", "находка"]]
    for f in rows:
        o = f.get("order")
        if not o:
            continue
        if not f["check"]["ok"] or (f.get("verified") or {}).get("result") != "confirmed":
            skipped += 1
            continue
        if f.get("inventory_status") == "known":
            already += 1
            continue
        price, psrc = o.get("price", ""), o.get("price_source", "")
        if price and not psrc:
            price = f"уточнить (указано {price} без источника)"
        out_rows.append([o.get("archive", ""), o.get("cipher", ""), o.get("title", ""), o.get("years", ""),
                         o.get("place", ""), o.get("why", ""), o.get("digital", ""), f.get("inventory_status", ""),
                         price or "уточнить", psrc, f.get("id", "")])
        n += 1

    def safe(v):
        v = str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v
    import io
    buf = io.StringIO()
    w = csv.writer(buf)
    for r in out_rows:
        w.writerow([safe(x) for x in r])
    with locked(d):
        atomic_write(os.path.join(d, "orders.csv"), buf.getvalue())
    print(f"OK: {n} строк → orders.csv; пропущено {skipped} (check не пройден или нет verified=confirmed), "
          f"уже есть у пользователя {already}")


def main():
    a = sys.argv[1:]
    if len(a) < 2 or any(x in ("-h", "--help") for x in a) or a[1].startswith("-"):
        print(__doc__)
        sys.exit(2)
    cmd, d = a[0], a[1]

    def opt(name, default=None):
        return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default
    if cmd != "init" and not os.path.isdir(d):
        sys.exit(f"нет папки {d} — сначала init")
    if cmd == "init":
        cmd_init(d, [a[i + 1] for i, x in enumerate(a) if x == "--input" and i + 1 < len(a)])
    elif cmd == "add":
        cmd_add(d, opt("--id"), opt("--track", ""), opt("--title", ""), int(opt("--priority", "5")))
    elif cmd == "set":
        cmd_set(d, a[2], a[3], opt("--note", ""))
    elif cmd == "status":
        cmd_status(d)
    elif cmd == "merge":
        cmd_merge(d)
    elif cmd == "check":
        cmd_check(d, "--fetch" in a, opt("--unit"))
    elif cmd == "orders":
        cmd_orders(d)
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
