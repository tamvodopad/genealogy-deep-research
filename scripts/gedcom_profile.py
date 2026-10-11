#!/usr/bin/env python3
"""Профиль GEDCOM-древа для планирования генеалогического ресерча (только stdlib).

Читает GEDCOM 5.5/5.5.1 (экспорт «Древа Жизни»/AgelongTree, MyHeritage, Gramps и др.),
ничего в нём не меняет. Пишет в OUT_DIR:

  people_full.json   — ВСЕ персоны, включая возможно живых. Только локально, никому не отдавать.
  people.json        — только умершие и предположительно умершие: можно отдавать в брифы агентов.
  families.json      — семьи (ссылки на персон по id).
  places.json        — кластеры мест (одноимённые места разных губерний НЕ склеиваются).
  profile.md         — сводка без живых: места, фамилии, «кирпичные стены»,
                       в каких ревизиях искать, проблемы данных.

Запуск:  python3 -I gedcom_profile.py TREE.ged OUT_DIR [--encoding cp1251] [--top 60]

Статус жизни: deceased (есть смерть/погребение) · presumed_deceased (по годам свои/родных) ·
possibly_living (события или рождение детей < 100 лет назад) · unknown (данных нет).
В people.json и profile.md — только deceased и presumed_deceased.
"""
import collections
import datetime
import json
import math
import os
import re
import sys

THIS_YEAR = datetime.date.today().year
MONTHS = {m: i for i, m in enumerate(
    "JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split(), 1)}
# переписи, после которых есть поимённые списки: (метка, год)
CENSUSES = [("1646", 1646), ("1678", 1678), ("1710–17", 1715), ("I", 1719), ("II", 1745), ("III", 1763),
            ("IV", 1782), ("V", 1795), ("VI", 1811), ("VII", 1816), ("VIII", 1834), ("IX", 1850),
            ("X", 1858)]
EVENT_TAGS = {"BIRT", "CHR", "BAPM", "DEAT", "BURI", "MARR", "RESI", "OCCU", "EVEN", "CENS",
              "EMIG", "IMMI", "GRAD", "EDUC", "RELI", "DIV", "ENGA", "MARB", "PROB", "WILL"}
ADMIN_WORDS = re.compile(r"(губерни|уезд|волост|район|р-н|област|обл\.|край|республик|"
                         r"с/с|сельсовет|приход|стан|округ|провинци|асср|автономн)", re.I)
REGION_WORDS = re.compile(r"(губерни|област|обл\.|край|республик|асср|провинци)", re.I)
COUNTRY = {"россия", "российская империя", "ссср", "рсфср", "russia"}
NAME_LIKE = re.compile(r"(ий|ей|ья|ия|ея|он|ан|ам|ил|ор|ат|ен|им|ла|на|та|ра|да|фа|за)$", re.I)
SURNAME_LIKE = re.compile(r"(ов|ев|ёв|ин|ын|ских|цких|ых|их|ский|цкий|ской|цкой|ова|ева|ёва|ина|ына|"
                          r"ская|цкая|ая|ой|ий|ко|ук|юк|ич|ян|енко|ман)$", re.I)
PATR_NEW = re.compile(r"(вич|вна|ична|чна|ич)$", re.I)
PATR_OLD = re.compile(r"(ов|ев|ёв|ин|ын|ова|ева|ёва|ина|ына)$", re.I)


# ---------- чтение файла ----------

def read_text(path, forced=None):
    raw = open(path, "rb").read()
    if forced:
        return raw.decode(forced), forced
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16"), "utf-16"
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8 (BOM)"
    head = raw[:4000].decode("latin-1")
    m = re.search(r"\n\s*1\s+CHAR\s+(\S+)", head)
    declared = m.group(1).upper() if m else ""
    if declared == "ANSEL":
        raise SystemExit("Файл в кодировке ANSEL — пересохраните GEDCOM в UTF-8 "
                         "(в «Древе Жизни»: Файл → Экспорт → GEDCOM, кодировка UTF-8) "
                         "или укажите --encoding.")
    order = ["utf-8", "cp1251"]
    if declared in ("ANSI", "WINDOWS-1251", "CP1251", "WINDOWS"):
        order = ["cp1251", "utf-8"]
    for enc in order:
        try:
            return raw.decode(enc), enc + (f" (объявлено {declared})" if declared else "")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8 с заменой битых байтов — проверьте --encoding"


def parse_records(text):
    """Строки → записи уровня 0: {lvl, xref, tag, value, kids, line}. Возвращает (records, bad_lines)."""
    records, stack, bad = [], [], []
    for n, line in enumerate(text.splitlines(), 1):
        line = line.rstrip("\r")
        if not line.strip():
            continue
        m = re.match(r"\s*(\d+)\s+(?:(@[^@]+@)\s+)?(\S+)(?:\s(.*))?$", line)
        if not m:
            bad.append(n)
            continue
        lvl, xref, tag, val = int(m.group(1)), m.group(2), m.group(3), m.group(4) or ""
        if tag in ("CONC", "CONT"):
            parent = next((x for x in reversed(stack) if x["lvl"] == lvl - 1), None)
            if parent is not None:
                parent["value"] += ("\n" if tag == "CONT" else "") + val
            else:
                bad.append(n)
            continue
        node = {"lvl": lvl, "xref": xref, "tag": tag, "value": val, "kids": [], "line": n}
        while stack and stack[-1]["lvl"] >= lvl:
            stack.pop()
        if stack:
            stack[-1]["kids"].append(node)
        elif lvl == 0:
            records.append(node)
        else:
            bad.append(n)
            continue
        stack.append(node)
    return records, bad


def kid(node, tag):
    return next((k for k in node["kids"] if k["tag"] == tag), None) if node else None


def kids(node, tag):
    return [k for k in node["kids"] if k["tag"] == tag] if node else []


# ---------- даты ----------

def _one_date(s):
    """«12 JAN 1850», «JAN 1850», «1850», «1731/32», «DEC 7» → (year|None, dual|None, precision)."""
    toks = s.split()
    year = dual = None
    day = month = None
    for t in toks:
        m = re.fullmatch(r"(\d{3,4})(?:/(\d{1,2}))?", t)
        if m and year is None and (len(t) >= 3):
            year = int(m.group(1))
            if m.group(2):
                base = 10 ** len(m.group(2))
                dual = year // base * base + int(m.group(2))
                if dual < year:
                    dual += base
            continue
        if t in MONTHS:
            month = MONTHS[t]
            continue
        if re.fullmatch(r"\d{1,2}", t):
            day = int(t)
    prec = "day" if (day and month and year) else "month" if (month and year) else "year" if year else None
    return year, dual, prec


def parse_date(raw):
    """→ dict(raw, kind, lower, upper, year_est, method, calendar, dual, precision)."""
    r = {"raw": raw or "", "kind": None, "lower": None, "upper": None, "year_est": None,
         "method": None, "calendar": "gregorian", "dual": None, "precision": None}
    if not raw or not raw.strip():
        return r
    s = raw.strip().upper()
    cal = re.search(r"@#D([A-Z ]+)@", s)
    if cal:
        r["calendar"] = cal.group(1).strip().lower()
    s = re.sub(r"@#D[A-Z ]+@", " ", s)
    s = re.sub(r"\(.*?\)", " ", s)          # фраза в INT … (…)
    s = " ".join(s.split())
    m = re.match(r"^(BET|FROM)\s+(.*?)\s+(AND|TO)\s+(.*)$", s)
    if m:
        a, da, pa = _one_date(m.group(2))
        b, db, pb = _one_date(m.group(4))
        a, b = da or a, db or b
        r.update(kind="range" if m.group(1) == "BET" else "period", lower=a, upper=b,
                 precision=pa or pb)
        if a and b:
            r["year_est"] = (a + b) // 2 if m.group(1) == "BET" else a
            r["method"] = "середина диапазона" if m.group(1) == "BET" else "начало периода"
        else:
            r["year_est"] = a or b
            r["method"] = "граница"
        return r
    m = re.match(r"^(ABT|CAL|EST|BEF|AFT|FROM|TO|INT)\s+(.*)$", s)
    qual, rest = (m.group(1), m.group(2)) if m else (None, s)
    y, dual, prec = _one_date(rest)
    r["dual"], r["precision"] = dual, prec
    if y is None:
        r["kind"] = "phrase"
        return r
    ymod = dual or y
    if qual in ("ABT", "CAL", "EST"):
        r.update(kind="approx", lower=ymod - 5, upper=ymod + 5, year_est=ymod, method=qual.lower())
    elif qual == "BEF":
        r.update(kind="before", upper=ymod, year_est=ymod, method="граница «до»")
    elif qual == "AFT":
        r.update(kind="after", lower=ymod, year_est=ymod, method="граница «после»")
    elif qual == "FROM":
        r.update(kind="period", lower=ymod, year_est=ymod, method="начало периода")
    elif qual == "TO":
        r.update(kind="period", upper=ymod, year_est=ymod, method="конец периода")
    elif qual == "INT":
        r.update(kind="interpreted", lower=ymod, upper=ymod, year_est=ymod, method="интерпретация")
    else:
        r.update(kind="exact", lower=ymod, upper=ymod, year_est=ymod, method="дата")
    return r


# ---------- имена ----------

def split_alts(s):
    """«Семенов (Семеновых)» → ["Семенов", "Семеновых"]; латиницу и «?» отбрасывает."""
    s = (s or "").replace("?", "")
    s = re.sub(r"(?<=[А-Яа-яЁё])\([^()\s]{1,3}\)(?=[А-Яа-яЁё])", "", s)  # «Прокоп(ф)ий» → «Прокопий»
    main = re.sub(r"\(.*?\)", " ", s).strip()
    alts = [a.strip() for a in re.findall(r"\(([^)]*)\)", s)]
    out = [x for x in [main] + alts if x and re.search(r"[А-Яа-яЁё]", x)]
    return [" ".join(x.split()) for x in out]


def split_given(given):
    """«Илья Юрьевич» → (Илья, Юрьевич, new); «Яков Баженов (Карпов)» → (Яков, Баженов, old)."""
    g = re.sub(r"\(.*?\)", " ", given or "").replace("?", " ")
    parts = g.split()
    if not parts:
        return "", "", ""
    for p in parts[1:]:
        if PATR_NEW.search(p):
            return parts[0], p, "new"
    for p in parts[1:]:
        if PATR_OLD.search(p):
            return parts[0], p, "old"
    return parts[0], "", ""


def surname_key(s):
    """Ключ ПОИСКА вариантов написания (не тождества!): о/а, е/я/э, ы/и, ѣ/і/ѳ, -ев/-ов,
    женские формы. Колобов/Калабов, Оверин/Аверин, Мякишев/Мекишов → один ключ."""
    s = (s or "").lower().strip()
    if not s:
        return ""
    s = s.translate(str.maketrans({"ё": "е", "ѣ": "е", "і": "и", "ї": "и", "ѳ": "ф", "ѵ": "и", "й": "и"}))
    s = re.sub(r"ъ$", "", s)
    s = re.sub(r"[^а-яa-z]", "", s)
    s = gender_base(s)
    s = re.sub(r"(ев)$", "ов", s)
    s = s.replace("я", "е").replace("ы", "и").replace("о", "а").replace("э", "е")
    s = re.sub(r"(.)\1+", r"\1", s)
    return s


def gender_base(s):
    """Детерминированное снятие рода: Петрова → петров, Толстая → толстои, Казанская → казанскии."""
    s = s.lower().replace("ё", "е").replace("й", "и")
    for fem, masc in (("ская", "скии"), ("цкая", "цкии"), ("ова", "ов"), ("ева", "ев"),
                      ("ина", "ин"), ("ына", "ын"), ("ая", "ои"), ("ои", "ои"), ("ии", "ои")):
        if s.endswith(fem) and len(s) > len(fem) + 2:
            return s[: -len(fem)] + masc
    return s


def deletion_index(keys):
    """Пары ключей на расстоянии Левенштейна ≤ 1 через индекс удалений: O(N·L) вместо O(N²)."""
    idx = collections.defaultdict(set)
    for k in keys:
        if len(k) < 5:
            continue
        idx[k].add(k)
        for i in range(len(k)):
            idx[k[:i] + k[i + 1:]].add(k)
    pairs = set()
    for bucket in idx.values():
        if 1 < len(bucket) <= 20:
            b = sorted(bucket)
            for i, x in enumerate(b):
                for y in b[i + 1:]:
                    if max(len(x), len(y)) >= 6:
                        pairs.add((x, y))
    return pairs


# ---------- места ----------

def place_info(node):
    plac = kid(node, "PLAC")
    if not plac or not plac["value"].strip():
        return None
    raw = " ".join(plac["value"].split())
    lat = lon = None
    mp = kid(plac, "MAP")
    if mp:
        la, lo = kid(mp, "LATI"), kid(mp, "LONG")
        try:
            if la and lo and la["value"] and lo["value"]:
                lat = float(la["value"][1:]) * (-1 if la["value"][0] in "Ss" else 1)
                lon = float(lo["value"][1:]) * (-1 if lo["value"][0] in "Ww" else 1)
        except ValueError:
            pass
    return {"raw": raw, "lat": lat, "lon": lon}


def settlement(raw):
    for part in [p.strip() for p in raw.split(",")]:
        if part and not ADMIN_WORDS.search(part) and part.lower() not in COUNTRY:
            return part
    return raw.split(",")[0].strip()


def district_of(raw):
    ds = [p.strip().lower() for p in raw.split(",") if re.search(r"(уезд|район|р-н|волост|кантон)", p, re.I)]
    return ds[0] if ds else ""


def region_of(raw):
    regs = [p.strip().lower() for p in raw.split(",") if REGION_WORDS.search(p)]
    return regs[0] if regs else ""


def km(a, b):
    (la1, lo1), (la2, lo2) = a, b
    p = math.pi / 180
    h = (math.sin((la2 - la1) * p / 2) ** 2 +
         math.cos(la1 * p) * math.cos(la2 * p) * math.sin((lo2 - lo1) * p / 2) ** 2)
    return 12742 * math.asin(math.sqrt(h))


# ---------- main ----------

def main():
    argv = sys.argv[1:]
    opts = {"--encoding": None, "--top": "60"}
    pos = []
    i = 0
    while i < len(argv):
        if argv[i] in opts and i + 1 < len(argv):
            opts[argv[i]] = argv[i + 1]
            i += 2
        else:
            pos.append(argv[i])
            i += 1
    if len(pos) != 2:
        print(__doc__)
        sys.exit(2)
    ged, out = pos
    top = int(opts["--top"])
    os.makedirs(out, exist_ok=True)
    text, enc = read_text(ged, opts["--encoding"])
    recs, bad = parse_records(text)
    if not recs:
        raise SystemExit("Не найдено ни одной записи GEDCOM — проверьте файл и --encoding.")

    by_xref, dup = {}, []
    for r in recs:
        if r["xref"]:
            if r["xref"] in by_xref:
                dup.append(r["xref"])
            by_xref.setdefault(r["xref"], r)

    def note_text(n):
        v = n["value"]
        if re.fullmatch(r"@[^@]+@", v.strip()) and v.strip() in by_xref:
            return by_xref[v.strip()]["value"]
        return v

    def sources(node):
        res = []
        for s in kids(node, "SOUR"):
            v = s["value"].strip()
            title = ""
            if re.fullmatch(r"@[^@]+@", v) and v in by_xref:
                t = kid(by_xref[v], "TITL")
                title = t["value"] if t else ""
            else:
                title = v
            pg = kid(s, "PAGE")
            res.append({"ref": v if v.startswith("@") else "", "title": title[:300],
                        "page": pg["value"][:300] if pg else ""})
        return res

    head = next((r for r in recs if r["tag"] == "HEAD"), None)
    source = ""
    if head and kid(head, "SOUR"):
        s = kid(head, "SOUR")
        source = " ".join(x for x in [s["value"], (kid(s, "NAME") or {}).get("value", ""),
                                       (kid(s, "VERS") or {}).get("value", "")] if x)

    people, fams, flags = {}, {}, collections.Counter()
    for r in recs:
        if r["tag"] == "INDI" and r["xref"] not in people:
            names = []
            for nm in kids(r, "NAME"):
                raw_name = nm["value"]
                m = re.search(r"/([^/]*)/", raw_name)
                sur = m.group(1).strip() if m else ""
                if not sur and kid(nm, "SURN"):
                    sur = kid(nm, "SURN")["value"].strip()
                given = (kid(nm, "GIVN")["value"] if kid(nm, "GIVN") else raw_name.split("/")[0]).strip()
                names.append({"raw": raw_name, "given": given, "surname_raw": sur,
                              "married": [f for k in kids(nm, "_MARNM") for f in split_alts(k["value"])]})
            nm0 = names[0] if names else {"raw": "", "given": "", "surname_raw": "", "married": []}
            sur_forms = split_alts(nm0["surname_raw"])
            given = nm0["given"]
            name_in_surname = False
            if not given and sur_forms and (not SURNAME_LIKE.search(sur_forms[0]) or (
                    NAME_LIKE.search(sur_forms[0]) and not re.search(r"(ов|ев|ин|ын|ск|цк|ых|их|ова|ева|ина|ына)\w{0,2}$",
                                                                    sur_forms[0], re.I))):
                # «/Софон/» — имя, записанное в поле фамилии
                given, sur_forms, name_in_surname = sur_forms[0], [], True
                flags["имя в поле фамилии"] += 1
            name, patr, patr_kind = split_given(given)
            alt_given = split_alts(given)[1:] if "(" in given else []
            events = []
            for e in r["kids"]:
                if e["tag"] not in EVENT_TAGS:
                    continue
                d = parse_date((kid(e, "DATE") or {}).get("value", ""))
                events.append({"tag": e["tag"], "type": (kid(e, "TYPE") or {}).get("value", ""),
                               "value": e["value"], "date": d, "place": place_info(e),
                               "note": " ".join(note_text(n) for n in kids(e, "NOTE"))[:600],
                               "sources": sources(e)})
            famc = []
            for k in kids(r, "FAMC"):
                ped = kid(k, "PEDI")
                famc.append({"fam": k["value"], "pedi": ped["value"].lower() if ped else ""})
            people[r["xref"]] = {
                "id": r["xref"], "name_raw": nm0["raw"], "given": given, "name": name,
                "alt_given": alt_given, "patronymic": patr, "patronymic_form": patr_kind,
                "surname": sur_forms[0] if sur_forms else "", "surname_alts": sur_forms[1:],
                "married_names": nm0["married"], "other_names": [n["raw"] for n in names[1:]],
                "name_in_surname_field": name_in_surname,
                "sex": (kid(r, "SEX") or {}).get("value", "").upper()[:1],
                "events": events, "famc": famc, "fams": [k["value"] for k in kids(r, "FAMS")],
                "notes": " ".join(note_text(n) for n in kids(r, "NOTE"))[:2000],
                "sources": sources(r), "n_media": len(kids(r, "OBJE")), "line": r["line"],
            }
        elif r["tag"] == "FAM" and r["xref"] not in fams:
            mr = kid(r, "MARR")
            fams[r["xref"]] = {
                "id": r["xref"], "husb": (kid(r, "HUSB") or {}).get("value", ""),
                "wife": (kid(r, "WIFE") or {}).get("value", ""),
                "children": [k["value"] for k in kids(r, "CHIL")],
                "marr": parse_date((kid(mr, "DATE") or {}).get("value", "")) if mr else parse_date(""),
                "marr_place": place_info(mr) if mr else None,
            }

    # ---- годы рождения (с методом) и статус жизни
    def own_birth(p):
        for tag in ("BIRT", "CHR", "BAPM"):
            for e in p["events"]:
                if e["tag"] == tag and e["date"]["year_est"]:
                    return e["date"]["year_est"], ("рождение" if tag == "BIRT" else "крещение") + \
                        ("" if e["date"]["kind"] == "exact" else f" ({e['date']['kind']})")
        return None, None

    def own_birth_upper(p):
        """Верхняя граница года рождения; None, если она не ограничена (AFT, FROM)."""
        for tag in ("BIRT", "CHR", "BAPM"):
            for e in p["events"]:
                if e["tag"] == tag and e["date"]["year_est"]:
                    return e["date"]["upper"]
        return "none"

    def children(pid):
        return [c for f in people[pid]["fams"] for c in fams.get(f, {}).get("children", []) if c in people]

    def parents(pid):
        res = []
        for fc in people[pid]["famc"]:
            fam = fams.get(fc["fam"])
            if fam:
                res += [x for x in (fam["husb"], fam["wife"]) if x in people]
        return res

    for pid, p in people.items():
        y, how = own_birth(p)
        if y is None:
            cands = []
            for f in p["fams"]:
                fam = fams.get(f)
                if fam and fam["marr"]["year_est"]:
                    cands.append((fam["marr"]["year_est"] - 22, "брак − 22"))
            for c in children(pid):
                cy, _ = own_birth(people[c])
                if cy:
                    cands.append((cy - 27, "первый ребёнок − 27"))
            if cands:
                y, how = min(cands)
            else:
                ys = [e["date"]["year_est"] for e in p["events"] if e["date"]["year_est"]]
                if ys:
                    y, how = min(ys) - 20, "самое раннее событие − 20"
        p["birth_year_est"], p["birth_method"] = y, how
        bu = own_birth_upper(p)
        p["birth_upper"] = (y + 10 if y is not None else None) if bu == "none" else bu
        dys = [e["date"]["year_est"] for e in p["events"] if e["tag"] in ("DEAT", "BURI") and e["date"]["year_est"]]
        p["death_year"] = min(dys) if dys else None

    for pid, p in people.items():
        has_death = any(e["tag"] in ("DEAT", "BURI") and e["value"].strip().upper() != "N" for e in p["events"])
        recent = [e["date"]["upper"] or e["date"]["year_est"] for e in p["events"]
                  if e["date"]["year_est"]] + [people[c]["birth_year_est"] for c in children(pid)
                                               if people[c]["birth_year_est"]]
        by, bu = p["birth_year_est"], p["birth_upper"]
        if has_death:
            st = "deceased"
        elif by is not None and bu is None:
            st = "possibly_living"          # «после 1800», «с 1800» — верхней границы нет
        elif bu is not None and bu < THIS_YEAR - 110:
            st = "presumed_deceased"
        elif recent and max(recent) > THIS_YEAR - 100:
            st = "possibly_living"
        elif bu is not None:
            st = "possibly_living"
        else:
            par = [people[x]["birth_year_est"] for x in parents(pid) if people[x]["birth_year_est"]]
            if par and min(par) < THIS_YEAR - 140:
                st = "presumed_deceased"
            elif par:
                st = "possibly_living"
            else:
                st = "unknown"
        p["living_status"] = st
    public = {k: v for k, v in people.items() if v["living_status"] in ("deceased", "presumed_deceased")}

    # ---- места: кластер = поселение + регион; одноимённые в разных регионах — разные кластеры
    raw_places = collections.defaultdict(lambda: {"people": set(), "years": [], "coords": [], "n": 0})
    for p in public.values():
        for e in p["events"]:
            if e["place"]:
                rp = raw_places[e["place"]["raw"]]
                rp["people"].add(p["id"])
                rp["n"] += 1
                if e["date"]["year_est"]:
                    rp["years"].append(e["date"]["year_est"])
                if e["place"]["lat"] is not None:
                    rp["coords"].append((e["place"]["lat"], e["place"]["lon"]))
    by_name = collections.defaultdict(list)
    for raw in raw_places:
        by_name[settlement(raw).casefold()].append(raw)
    clusters, raw2cl, homonyms = {}, {}, []
    for nm, raws in by_name.items():
        full = sorted({(region_of(r), district_of(r)) for r in raws if region_of(r) and district_of(r)})
        groups, unsure = collections.defaultdict(list), []
        R = {region_of(r) for r in raws if region_of(r)}
        D = {district_of(r) for r in raws if district_of(r)}
        if len(R) <= 1 and len(D) <= 1:      # адреса не противоречат друг другу — одно место
            groups[(next(iter(R), ""), next(iter(D), ""))] = list(raws)
            raws_left = []
        else:
            raws_left = raws
        for r in raws_left:
            rg, ds = region_of(r), district_of(r)
            if rg and ds:
                key = (rg, ds)
            else:
                comp = [k for k in full if (not rg or k[0] == rg) and (not ds or k[1] == ds)]
                if len(comp) == 1:
                    key = comp[0]
                else:
                    key = (rg, ds)
                    if comp:
                        unsure.append(r)
            groups[key].append(r)
        final = []
        for (rg, ds), rs in groups.items():
            # внутри группы — разнести формы, чьи координаты расходятся > 25 км
            subs = []
            for r in sorted(rs, key=lambda r: -len(raw_places[r]["coords"])):
                c = raw_places[r]["coords"][0] if raw_places[r]["coords"] else None
                tgt = next((sb for sb in subs if c is None or sb["c"] is None or km(sb["c"], c) <= 25), None)
                if tgt is None:
                    subs.append({"c": c, "rs": [r]})
                else:
                    tgt["rs"].append(r)
                    tgt["c"] = tgt["c"] or c
            for i, sb in enumerate(subs):
                final.append((rg, ds, i, sb["rs"], sb["c"]))
        if len(final) > 1:
            homonyms.append((settlement(raws[0]),
                             [f"{rg or 'регион ?'} / {ds or 'уезд ?'}" + (f" #{i + 1}" if i else "") for rg, ds, i, _, _ in final],
                             "одноимённые или неполный адрес/координаты — не склеены" +
                             (f"; неоднозначно: {len(unsure)} форм без уезда" if unsure else "")))
        for rg, ds, i, rs, c in final:
            cid = f"{settlement(rs[0])}|{rg}|{ds}" + (f"#{i + 1}" if i else "")
            clusters[cid] = {"id": cid, "name": settlement(rs[0]), "region": rg, "district": ds,
                             "forms": sorted(rs, key=lambda r: (-len(r), r)), "coords": c,
                             "people": set().union(*(raw_places[r]["people"] for r in rs)),
                             "years": sorted(y for r in rs for y in raw_places[r]["years"])}
            for r in rs:
                raw2cl[r] = cid

    def cl_of(e):
        return raw2cl.get(e["place"]["raw"]) if e.get("place") else None

    for p in people.values():
        p["place_ids"] = sorted({raw2cl[e["place"]["raw"]] for e in p["events"]
                                 if e["place"] and e["place"]["raw"] in raw2cl})

    # ---- фамилии: точные формы основные; род снимается детерминированно; нечёткие — кандидаты
    exact_people = collections.defaultdict(set)
    for p in public.values():
        for s in [p["surname"]] + p["surname_alts"] + p["married_names"]:
            if s and len(s) > 1:
                exact_people[s].add(p["id"])
    by_gender = collections.defaultdict(set)
    for s in exact_people:
        by_gender[gender_base(s)].add(s)

    def display(base):
        forms = by_gender[base]
        masc = sorted((f for f in forms if not re.search(r"(ова|ева|ина|ына|ая|ская|цкая)$", f)),
                      key=lambda f: -len(exact_people[f]))
        if masc:
            return masc[0]
        fem = sorted(forms, key=lambda f: -len(exact_people[f]))[0]
        for a, b in (("ская", "ский"), ("цкая", "цкий"), ("ова", "ов"), ("ева", "ев"), ("ёва", "ёв"),
                     ("ина", "ин"), ("ына", "ын")):
            if fem.endswith(a) and len(fem) > len(a) + 2:
                return fem[: -len(a)] + b + "*"
        return fem
    surname_people = {}
    for base, forms in by_gender.items():
        surname_people[display(base)] = set().union(*(exact_people[f] for f in forms))
    key_groups = collections.defaultdict(set)
    for d in surname_people:
        key_groups[surname_key(d)].add(d)
    variants = [(sorted(g), "правило нормализации (о/а, е/я, -ев/-ов…)") for g in key_groups.values() if len(g) > 1]
    for a, b in deletion_index(list(key_groups)):
        variants.append((sorted(key_groups[a] | key_groups[b]), "отличие в одну букву"))
    for p in public.values():
        if p["surname"] and p["surname_alts"]:
            variants.append(([p["surname"]] + p["surname_alts"], "«он же» в древе"))
    seen_v, uniq_v = set(), []
    for g, why in variants:
        t = tuple(g)
        if t not in seen_v and len(set(g)) > 1:
            seen_v.add(t)
            uniq_v.append((g, why))

    def surname_disp(s):
        return display(gender_base(s)) if s and gender_base(s) in by_gender else s

    # ---- «кирпичные стены»: нет отца в древе
    def father_of(pid):
        for fc in people[pid]["famc"]:
            fam = fams.get(fc["fam"])
            if fam and fam["husb"] in people:
                return fam["husb"], fc["pedi"]
        return None, None

    idx = {pid: i for i, pid in enumerate(people)}
    desc_bits = {}

    def desc_mask(root):
        stack, state = [root], {}
        while stack:
            x = stack[-1]
            if x in desc_bits:
                stack.pop()
                continue
            if state.get(x) is None:
                state[x] = 1
                stack += [c for c in children(x) if c not in desc_bits and state.get(c) is None]
                continue
            m = 0
            for c in children(x):
                m |= (1 << idx[c]) | desc_bits.get(c, 0)   # цикл: незавершённый узел даёт 0
            desc_bits[x] = m & ~(1 << idx[x])
            stack.pop()
        return desc_bits[root]

    def n_desc(pid):
        return bin(desc_mask(pid)).count("1")

    walls = []
    for pid, p in public.items():
        f, _ = father_of(pid)
        if f:
            continue
        evs = sorted([e for e in p["events"] if e["place"]], key=lambda e: e["date"]["year_est"] or 9999)
        place = cl_of(evs[0]) if evs else None
        place_note = ""
        if not place:
            frontier = [pid]
            for _ in range(3):
                frontier = [c for x in frontier for c in children(x)]
                cnt = collections.Counter(pl for c in frontier if c in public for pl in people[c]["place_ids"])
                if cnt:
                    place, place_note = cnt.most_common(1)[0][0], " (по потомкам)"
                    break
        husband = marr = ""
        if p["sex"] == "F":
            for fam_id in p["fams"]:
                fam = fams.get(fam_id, {})
                h = people.get(fam.get("husb"))
                if h and h["id"] in public:
                    husband = " ".join(x for x in (h["name"], h["patronymic"], surname_disp(h["surname"])) if x)
                    my = fam["marr"]["year_est"]
                    mp = settlement(fam["marr_place"]["raw"]) if fam.get("marr_place") else ""
                    marr = ", ".join(str(x) for x in (my, mp) if x)
                    break
        mother_known = any(fams.get(fc["fam"], {}).get("wife") in people for fc in p["famc"])
        walls.append({"id": pid, "who": " ".join(x for x in (p["name"], p["patronymic"], surname_disp(p["surname"])) if x),
                      "sex": p["sex"] or "?", "by": p["birth_year_est"], "by_method": p["birth_method"],
                      "place": (clusters[place]["name"] + place_note) if place else "",
                      "desc": n_desc(pid), "husband": husband, "marr": marr, "mother_known": mother_known})
    walls.sort(key=lambda w: (-w["desc"], w["by"] or 9999))

    # ---- переписи: кандидаты (оценка по годам жизни — не подтверждение проживания)
    cens = collections.defaultdict(collections.Counter)
    for p in public.values():
        by = p["birth_year_est"]
        if not by:
            continue
        dy = p["death_year"] or by + 60
        dated = [(e["date"]["year_est"], cl_of(e)) for e in p["events"] if e["place"] and e["date"]["year_est"]]
        for lab, y in CENSUSES:
            if not by <= y <= dy:
                continue
            if dated:   # место события, ближайшего по времени к году переписи (не дальше 40 лет)
                yr, cid = min(dated, key=lambda t: abs(t[0] - y))
                if cid and abs(yr - y) <= 40:
                    cens[cid][lab] += 1
            else:
                for cid in p["place_ids"]:
                    cens[cid][lab] += 1

    # ---- проблемы данных
    probs = []
    if bad:
        probs.append(f"Нераспознанных строк GEDCOM: {len(bad)} (первые: {', '.join(map(str, bad[:10]))})")
    if dup:
        probs.append(f"Повторяющиеся XREF (взята первая запись): {', '.join(dup[:10])}")
    for k, v in flags.items():
        probs.append(f"{k}: {v} персон (перенесено в имя)")
    probs.append(f"Без единого места: {sum(1 for p in public.values() if not p['place_ids'])} персон")
    probs.append(f"Без единой даты: {sum(1 for p in public.values() if not any(e['date']['year_est'] for e in p['events']))} персон")
    probs.append(f"Мужчины до 1918 без отчества: {sum(1 for p in public.values() if p['sex'] == 'M' and not p['patronymic'] and (p['birth_year_est'] or 3000) < 1918)}"
                 " — у крестьян отчество важнее фамилии для поиска в ревизиях")
    lonely = [p for p in public.values() if not p["fams"] and not any(fams.get(fc["fam"]) for fc in p["famc"])]
    if lonely:
        probs.append(f"Персон без связей (ни родителей, ни своей семьи в древе): {len(lonely)} — "
                     "чьи это дети/братья? без привязки их не найти в ревизской семье")
    early = [p for p in public.values() if p["birth_year_est"] and p["birth_year_est"] < 1620]
    if early:
        probs.append(f"Рождение до 1620 (вне поимённых документов — проверить источник оценки): "
                     + ", ".join(f"{p['id']} ~{p['birth_year_est']}" for p in early[:10]))
    bare = sorted(c["name"] for c in clusters.values() if not c["region"] and len(c["people"]) >= 3)
    if bare:
        probs.append("Места без уезда/губернии — нужна административная привязка до ресерча: " + ", ".join(bare))
    st_cnt = collections.Counter(p["living_status"] for p in people.values())

    # ---- запись
    def dump(name, obj):
        with open(os.path.join(out, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1, default=sorted)
    dump("people_full.json", list(people.values()))
    dump("people.json", list(public.values()))
    dump("families_full.json", list(fams.values()))
    pub_fams = []
    for fam in fams.values():
        h, w = fam["husb"] in public, fam["wife"] in public
        ch = [c for c in fam["children"] if c in public]
        if h or w or ch:
            pub_fams.append(dict(fam, husb=fam["husb"] if h else "", wife=fam["wife"] if w else "", children=ch))
    dump("families.json", pub_fams)
    dump("places.json", [dict(c, people=sorted(c["people"])) for c in clusters.values()])

    years = [p["birth_year_est"] for p in public.values() if p["birth_year_est"]]
    L = [f"# Профиль древа: {os.path.basename(ged)}\n",
         f"- Программа-источник: {source or 'не указана'}; кодировка: {enc}",
         f"- Персон всего {len(people)}: умерших {st_cnt['deceased']}, предположительно умерших "
         f"{st_cnt['presumed_deceased']}, возможно живых {st_cnt['possibly_living']}, без данных "
         f"{st_cnt['unknown']}. **В этой сводке — только {len(public)} умерших и предположительно умерших.**",
         f"- Семей: {len(fams)}"]
    if years:
        L.append(f"- Годы рождения (вкл. оценки): {min(years)}–{max(years)}")
    L.append(f"- Дат по юлианскому календарю: "
             f"{sum(1 for p in people.values() for e in p['events'] if e['date']['calendar'] == 'julian')}")
    L.append(f"- Ссылок на источники (SOUR) в событиях и персонах: "
             f"{sum(len(p['sources']) + sum(len(e['sources']) for e in p['events']) for p in public.values())}\n")

    L.append(f"## Места (топ {top} по числу персон)\n")
    L.append("| Место | Регион | Персон | Годы событий | Как записано в древе | Коорд. | Частые фамилии (персон) |")
    L.append("|---|---|---|---|---|---|---|")
    for c in sorted(clusters.values(), key=lambda c: -len(c["people"]))[:top]:
        sc = collections.Counter(surname_disp(public[i]["surname"]) for i in c["people"] if public[i]["surname"])
        yr = f"{c['years'][0]}–{c['years'][-1]}" if c["years"] else "—"
        form = c["forms"][0] + (f" (+{len(c['forms']) - 1} вар.)" if len(c["forms"]) > 1 else "")
        L.append(f"| {c['name']} | {c['region'] or '—'} | {len(c['people'])} | {yr} | {form} | "
                 f"{'да' if c['coords'] else '—'} | {', '.join(f'{k} {v}' for k, v in sc.most_common(4))} |")
    if len(clusters) > top:
        L.append(f"\n…и ещё {len(clusters) - top} мест (см. places.json).")
    if homonyms:
        L.append("\n**Возможные одноимённые места — не склеены, проверить:**")
        for nm, regs, why in homonyms:
            L.append(f"- {nm}: {', '.join(regs)} — {why}")

    L.append("\n## В каких переписях и ревизиях искать (кандидаты)\n")
    L.append("Число персон, которые **по оценке** годов жизни были живы в год переписи; место — по событию, "
             "ближайшему по времени к году переписи (не дальше 40 лет). Подсказка, где искать, а не подтверждение.\n")
    L.append("| Место | " + " | ".join(lab for lab, _ in CENSUSES) + " |")
    L.append("|---|" + "---|" * len(CENSUSES))
    for cid, cnt in sorted(cens.items(), key=lambda kv: -sum(kv[1].values()))[:top]:
        if sum(cnt.values()) < 2:
            continue
        L.append(f"| {clusters[cid]['name']} | " + " | ".join(str(cnt.get(lab, "")) for lab, _ in CENSUSES) + " |")
    L.append("\nГде лежат (общее правило, сверять по путеводителю архива): переписные книги 1646/1678, "
             "ландратские 1710–1717 и I–III ревизии — РГАДА; IV–X ревизии — архив губернского центра "
             "(фонд казённой палаты); копии — уездные казначейства. Между ревизиями — исповедные ведомости "
             "(ежегодно) и метрики по приходу.")

    L.append(f"\n## Фамилии (топ {top})\n")
    L.append("_* — мужская форма выведена из женской (в древе только женщины этой фамилии)._\n")
    L.append("| Фамилия | Персон | Места |")
    L.append("|---|---|---|")
    for s, ids in sorted(surname_people.items(), key=lambda kv: -len(kv[1]))[:top]:
        pl = collections.Counter(clusters[c]["name"] for i in ids for c in people[i]["place_ids"])
        L.append(f"| {s} | {len(ids)} | {', '.join(k for k, _ in pl.most_common(4))} |")
    if uniq_v:
        L.append("\n**Возможные варианты написания** — искать все формы; совпадение формы само по себе не "
                 "доказывает родства:")
        for g, why in uniq_v[:60]:
            L.append(f"- {' / '.join(g)} — {why}")

    L.append("\n## «Кирпичные стены» — у персоны нет отца в древе\n")
    L.append("По числу потомков в древе. Отчество = имя отца уже известно → искать отца в ревизии/исповедной "
             "по месту и году. Для женщин — брачный обыск и метрика венчания по приходу мужа дают деревню "
             "и отца невесты.\n")
    for title, sexes, cols in (("### Мужские линии", {"M"}, False), ("### Женщины", {"F"}, True),
                               ("### Пол не указан", {"?", "U", ""}, False)):
        rows = [w for w in walls if w["sex"] in sexes and w["desc"] > 0][:40]
        if not rows:
            continue
        L.append(title + "\n")
        if cols:
            L.append("| Кто | Год рожд. | Место | Муж | Брак (год, место) | Потомков |")
            L.append("|---|---|---|---|---|---|")
        else:
            L.append("| Кто | Год рожд. | Место | Потомков |")
            L.append("|---|---|---|---|")
        for w in rows:
            by = f"{w['by']} ({w['by_method']})" if w["by"] else "?"
            who = f"{w['who'] or '(без имени)'} ({w['id']})"
            if cols:
                L.append(f"| {who} | {by} | {w['place'] or '—'} | {w['husband'] or '—'} | {w['marr'] or '—'} | {w['desc']} |")
            else:
                L.append(f"| {who} | {by} | {w['place'] or '—'} | {w['desc']} |")
        L.append("")
    L.append("## Проблемы данных\n")
    L += [f"- {x}" for x in probs]
    with open(os.path.join(out, "profile.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"OK: {len(people)} персон ({len(public)} в публичной части), {len(fams)} семей, "
          f"{len(clusters)} мест, {len(surname_people)} фамилий → {out}")


if __name__ == "__main__":
    main()
