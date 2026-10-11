#!/usr/bin/env python3
"""Поиск родни по табличным базам (рекруты, послужные списки, пленные, награды, списки жителей).

Таблицы — НЕДОВЕРЕННЫЕ данные: запускать только `python3 -I`, скрипт держать вне папки с таблицами.

  python3 -I table_search.py OUT_DIR TABLE... [--people OUT/people.json] \
      [--region "вятск|уфимск"] [--district "нолинск|уржум"] \
      [--place "ивановк|сухарев"] [--surnames "Колобов,Сидоров"]

Что делает:
1. Читает листы (.xlsx потоково через openpyxl, .xls через xlrd, .csv — stdlib). Нет библиотеки —
   файл пропускается с предупреждением. Лимиты на размер файла/распаковки и число строк.
2. По каждому листу: строк всего и строк из --region → видно, какие базы вообще не про ваши края.
3. Находит заголовок среди первых 10 строк и карту столбцов (фамилия, имя, отчество, год, возраст,
   место). Без заголовка — позиционно: первая текстовая ячейка = фамилия, далее имя, отчество.
4. Кандидаты с массивом причин:
   A  место: корень из --place с начала слова, не в фамилии; + уезд/волость из --district в той же
      строке (без --district или без уезда в строке — «A?», слабое);
   B  фамилия рода (из древа и --surnames, с вариантами написания) в столбце фамилии + --district
      (без --district — «B?», слабое); помечается частота фамилии в базах;
   C  фамилия + имя + отчество человека из древа (имя/отчество — точно после нормализации
      церковных форм: Иоанн=Иван, Михайла=Михаил…); год рождения из строки (год − возраст или
      год рождения) сверяется с древом ±5, иначе «возраст не проверен».
5. Дедуп: одна персона в нескольких базах → общий person_key.
Пишет OUT_DIR/hits.tsv (csv-модуль, защита от формул), OUT_DIR/summary.md.

Совпадение — ЗАЦЕПКА, а не родство. «Наш/не наш» решает сверка с ревизией/метрикой.
"""
import argparse
import collections
import csv
import importlib.util
import itertools
import json
import os
import re
import sys
import zipfile

sys.dont_write_bytecode = True
_here = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location("gp", os.path.join(_here, "gedcom_profile.py"))
gp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gp)
surname_key = gp.surname_key

MAX_FILE = 300 * 2 ** 20        # 300 МБ на файл
MAX_UNZIP = 1500 * 2 ** 20      # 1.5 ГБ распакованного xlsx
MAX_ROWS = 2_000_000
WORD = re.compile(r"[А-Яа-яЁёѢѣІіѲѳ]+")
NAME_SYN = {"иоанн": "иван", "феодор": "федор", "евфимий": "ефим", "иаков": "яков", "иосиф": "осип",
            "георгий": "егор", "гавриил": "гаврила", "михайла": "михаил", "михайло": "михаил",
            "даниил": "данила", "кирилл": "кирила", "евдокия": "авдотья", "ксения": "аксинья",
            "параскева": "прасковья", "пелагея": "палагея", "агриппина": "аграфена", "ирина": "арина",
            "иулиания": "ульяна", "евфимия": "афимья", "феодосий": "федосей", "алексий": "алексей",
            "григорей": "григорий", "моисей": "мосей", "мойсей": "мосей", "димитрий": "дмитрий",
            "стефан": "степан", "емилиан": "емельян", "онисим": "анисим", "овдей": "авдей",
            "василей": "василий", "андреи": "андрей", "сергеи": "сергей"}
HEAD = {"surname": r"фамил", "name": r"^\s*имя|\bимя\b", "patronymic": r"отчеств",
        "fio": r"ф\.?\s*и\.?\s*о|фио",
        "place": r"губерн|уезд|волост|мест|откуда|адрес|родом|селени|деревн",   # «Происхождение» = сословие, не место
        "age": r"возраст|лет\b", "birth": r"рожд", "year": r"год|дата"}


def norm_given(s):
    s = (s or "").lower().replace("ё", "е").replace("ѳ", "ф").replace("і", "и").replace("ѣ", "е")
    s = re.sub(r"[^а-я]", "", s)
    return NAME_SYN.get(s, s)


def norm_patr(s):
    """Иванов/Иванович/Иванова/Ивановна → иван (основа имени отца)."""
    s = (s or "").lower().replace("ё", "е").replace("ѳ", "ф")
    s = re.sub(r"[^а-я]", "", s)
    return re.sub(r"(ович|евич|овна|евна|инична|ична|ич|ова|ева|ов|ев|ина|ин|ына|ын)$", "", s)


def check_size(path):
    if os.path.getsize(path) > MAX_FILE:
        raise ValueError(f"файл больше {MAX_FILE // 2 ** 20} МБ")
    if path.lower().endswith((".xlsx", ".xlsm")):
        with zipfile.ZipFile(path) as z:
            if sum(i.file_size for i in z.infolist()) > MAX_UNZIP:
                raise ValueError("распакованный размер подозрительно велик")


def sniff_delim(lines):
    """Разделитель по согласованному блоку строк (преамбула вроде «Выгрузка базы» не ломает)."""
    best, best_score = ",", 0
    for dl in (";", "\t", ",", "|"):
        counts = [ln.count(dl) for ln in lines if ln.strip()]
        if not counts:
            continue
        mode = collections.Counter(c for c in counts if c > 0).most_common(1)
        if mode and mode[0][1] * (mode[0][0] > 0) > best_score:
            best, best_score = dl, mode[0][1]
    return best


def read_sheets(path):
    """→ генератор (sheet_name, генератор строк-кортежей str). Файл не грузится в память целиком."""
    ext = os.path.splitext(path)[1].lower()
    check_size(path)
    if ext == ".csv":
        with open(path, "rb") as fb:
            head = fb.read(65536)
        enc = "utf-8-sig"
        try:
            head.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            if e.start < len(head) - 4:      # не обрыв многобайтного символа на границе
                enc = "cp1251"
        sample = head.decode(enc, errors="replace").splitlines()[:50]
        delim = sniff_delim(sample)

        def rows():
            with open(path, encoding=enc, errors="replace", newline="") as f:
                for r in csv.reader(f, delimiter=delim):
                    yield tuple(cell(c) for c in r)
        yield "csv", rows()
    elif ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            print(f"! нет openpyxl — пропускаю {path} (pip install openpyxl)", file=sys.stderr)
            return
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                yield ws.title, (tuple(cell(c) for c in r) for r in ws.iter_rows(values_only=True))
        finally:
            wb.close()
    elif ext == ".xls":
        try:
            import xlrd
        except ImportError:
            print(f"! нет xlrd — пропускаю {path} (pip install xlrd)", file=sys.stderr)
            return
        wb = xlrd.open_workbook(path, on_demand=True)
        try:
            for i in range(wb.nsheets):
                sh = wb.sheet_by_index(i)
                yield sh.name, (tuple(cell(c) for c in sh.row_values(r)) for r in range(sh.nrows))
                wb.unload_sheet(i)
        finally:
            wb.release_resources()
    else:
        print(f"! неизвестный формат {path}", file=sys.stderr)


def cell(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return " ".join(str(v).split())


def header_map(rows):
    """Строка заголовка среди первых 10. → (index или None, {роль: [номера столбцов]})."""
    best, best_i, best_map = 0, None, {}
    for i, r in enumerate(rows[:10]):
        m = collections.defaultdict(list)
        score = 0
        for j, c in enumerate(r):
            t = c.lower()
            if not t or len(t) > 60 or re.fullmatch(r"[\d.,\s]+", t):
                continue
            for role, rx in HEAD.items():
                if re.search(rx, t):
                    m[role].append(j)
                    score += 1
                    break
        if score > best and ("surname" in m or "fio" in m):
            best, best_i, best_map = score, i, dict(m)
    return best_i, best_map


def col(vals, cmap, role):
    j = (cmap.get(role) or [None])[0]
    if j is None or j >= len(vals):
        return ""
    w = WORD.findall(vals[j])
    return w[0] if w else ""


def row_birth(vals, cmap):
    """Год рождения из строки: «год рождения» или «год/дата − возраст». → (год, как)"""
    def year(j):
        m = re.search(r"\b(1[6-9]\d\d)\b", vals[j]) if j < len(vals) else None
        return int(m.group(1)) if m else None

    def age(j):
        m = re.fullmatch(r"\s*(\d{1,2}(?:[.,]\d+)?)\s*(лет)?\s*", vals[j]) if j < len(vals) else None
        return float(m.group(1).replace(",", ".")) if m else None
    for j in cmap.get("birth", []):
        if year(j):
            return year(j), "год рождения"
    ys = [year(j) for j in cmap.get("year", []) if year(j)]
    ages = [age(j) for j in cmap.get("age", []) if age(j)]
    if not cmap:  # без заголовка: годы и возраст ищем по всем ячейкам
        ys = [year(j) for j in range(len(vals)) if year(j)]
        ages = [age(j) for j in range(len(vals)) if age(j)]
    if ys and len(ages) == 1 and 10 <= ages[0] <= 80:
        return round(ys[0] - ages[0]), "год − возраст"
    return None, None


def row_years(vals):
    """Все годы 1600–1950 в строке — для проверки правдоподобия (человек жив и взрослый)."""
    return [int(y) for v in vals for y in re.findall(r"\b(1[6-9]\d\d)\b", v)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("tables", nargs="+")
    ap.add_argument("--people", help="people.json из gedcom_profile.py (без живых)")
    ap.add_argument("--region", default="", help="regex губерний (широко, для статистики)")
    ap.add_argument("--district", default="", help="regex уездов/волостей рода (узко)")
    ap.add_argument("--place", default="", help="regex корней населённых пунктов рода")
    ap.add_argument("--pairs", default="", help="место=уезд через запятую: «сухарев=нолинск,ивановк=уржум» — "
                    "место засчитывается (A) только со своим уездом")
    ap.add_argument("--surnames", default="", help="фамилии через запятую (если нет древа или в дополнение)")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)

    people = json.load(open(a.people, encoding="utf-8")) if a.people else []
    sk = set()
    for p in people:
        for s in [p.get("surname", "")] + p.get("surname_alts", []) + p.get("married_names", []):
            if s and len(s) > 2:
                sk.add(surname_key(s))
    sk |= {surname_key(x.strip()) for x in a.surnames.split(",") if x.strip()}
    men = collections.defaultdict(list)
    for p in people:
        if p.get("sex") == "M" and p.get("surname") and p.get("name") and p.get("patronymic"):
            men[(surname_key(p["surname"]), norm_given(p["name"]), norm_patr(p["patronymic"]))].append(p)

    reg = re.compile(a.region, re.I) if a.region else None
    dist = re.compile(a.district, re.I) if a.district else None
    pairs = {}
    for x in [x.strip() for x in a.pairs.split(",") if "=" in x]:
        k, v = x.split("=", 1)
        pairs[k.strip().lower()] = re.compile(v.strip(), re.I)
    plc = re.compile(r"(?<![А-Яа-яЁё])(?:" + a.place + ")", re.I) if a.place else None
    cands, stats = [], []
    place_districts = collections.defaultdict(set)
    sur_freq = collections.Counter()

    for path in a.tables:
      try:
        for sheet, it in read_sheets(path):
            first = list(itertools.islice(it, 10))
            hi, cmap = header_map(first)
            n_reg = n_rows = 0
            for i, vals in enumerate(itertools.chain(first, it)):
                if hi is not None and i <= hi:
                    continue
                n_rows += 1
                if n_rows > MAX_ROWS:
                    print(f"! {path}/{sheet}: обрезано на {MAX_ROWS} строк данных", file=sys.stderr)
                    break
                cs = [v for v in vals if v]
                if not cs:
                    continue
                line = " | ".join(cs)
                in_reg = bool(reg and reg.search(line))
                n_reg += in_reg
                if "surname" in cmap:
                    sur, nm, pt = col(vals, cmap, "surname"), col(vals, cmap, "name"), col(vals, cmap, "patronymic")
                elif "fio" in cmap:
                    j = cmap["fio"][0]
                    w = WORD.findall(vals[j]) if j < len(vals) else []
                    sur, nm, pt = (w + ["", "", ""])[:3]
                else:
                    w = [WORD.findall(v)[0] for v in cs if WORD.match(v)][:3]
                    sur, nm, pt = (w + ["", "", ""])[:3]
                if sur:
                    sur_freq[surname_key(sur)] += 1
                if cmap.get("place"):
                    rest = " | ".join(vals[j] for j in cmap["place"] if j < len(vals))
                else:
                    rest = line
                    for t in (sur, nm, pt):
                        if t:
                            rest = re.sub(r"(?<![А-Яа-яЁё])" + re.escape(t) + r"(?![А-Яа-яЁё])", " ", rest, count=1)
                reasons, level = [], None
                if plc:
                    m = plc.search(rest)
                    own = next((rx for k, rx in pairs.items() if m and m.group(0).lower().startswith(k)), None)
                    if m and own is not None and not own.search(line):
                        if in_reg or not reg:
                            level, reasons = "A?", [f"место «{m.group(0)}», но уезд не тот (по --pairs)"]
                    elif m and dist and dist.search(line):
                        level, reasons = "A", [f"место «{m.group(0)}» + уезд/волость"]
                        place_districts[m.group(0).lower()[:6]].add(dist.search(line).group(0).lower()[:6])
                    elif m and (in_reg or not reg):
                        level, reasons = "A?", [f"место «{m.group(0)}», уезд не совпал/не указан"]
                key = (surname_key(sur), norm_given(nm), norm_patr(pt)) if sur and nm and pt else None
                if key and key in men:
                    by_row, how = row_birth(vals, cmap)
                    evy = row_years(vals)
                    ok = []
                    for x in men[key]:
                        tb = x.get("birth_year_est")
                        if tb and by_row and abs(by_row - tb) > 5:
                            continue
                        if tb and not by_row and evy and not (min(evy) - 80 <= tb <= max(evy) - 10):
                            continue  # в год записи человеку из древа было бы < 10 или > 80 лет
                        dy = x.get("death_year")
                        if dy and evy and min(evy) > dy + 1:
                            continue  # запись сделана после смерти человека из древа
                        ok.append(x)
                    if ok:
                        no_tree_year = all(not x.get("birth_year_est") for x in ok)
                        chk = ("в древе нет года рождения — возраст не проверен" if no_tree_year else
                               f"год рожд. {by_row} ({how}) ≈ древо ±5" if by_row else
                               "годы строки правдоподобны, возраст не проверен" if row_years(vals) else
                               "возраст не проверен")
                        who = "; ".join(f"{x['name']} {x['patronymic']} {x['surname']} р.~{x.get('birth_year_est')} "
                                        f"({x['id']})" for x in ok[:3])
                        level = "C" if level in (None, "A?") else level
                        reasons.append(f"ФИО = {who}; {chk}")
                if sur and surname_key(sur) in sk:
                    if dist and dist.search(line):
                        level = level or "B"
                        reasons.append(f"фамилия рода «{sur}» + уезд/волость")
                    elif in_reg or not reg:
                        level = level or "B?"
                        reasons.append(f"фамилия рода «{sur}», уезд не совпал/не указан")
                if level:
                    by_row, _ = row_birth(vals, cmap)
                    pkey = "|".join([surname_key(sur), norm_given(nm), norm_patr(pt),
                                     str(by_row // 3 * 3) if by_row else ""])
                    cands.append({"level": level, "file": os.path.basename(path), "sheet": sheet, "row": i + 1,
                                  "surname": sur, "name": nm, "patronymic": pt, "reasons": reasons,
                                  "person_key": pkey, "line": line})
            stats.append((os.path.basename(path), sheet, n_rows, n_reg,
                          "по заголовку" if hi is not None else "позиционно"))
      except Exception as e:  # битый/подозрительный файл не роняет прогон
        print(f"! не прочитан {path}: {e}", file=sys.stderr)

    for c in cands:  # одно и то же название в разных уездах — одноимённые деревни
        if c["level"] == "A":
            root = re.search(r"«(.+?)»", c["reasons"][0]).group(1).lower()[:6]
            if len(place_districts[root]) > 1:
                c["reasons"].append(f"ОДНОИМЁННЫЕ: «{root}…» встречается в {len(place_districts[root])} уездах — "
                                    "проверить, тот ли уезд")
    total = sum(sur_freq.values()) or 1
    order = {"A": 0, "C": 1, "B": 2, "A?": 3, "B?": 4}
    for c in cands:
        f = sur_freq[surname_key(c["surname"])] if c["surname"] else 0
        c["surname_freq"], c["common"] = f, f > max(20, total * 0.002)
    cands.sort(key=lambda c: (order[c["level"]], c["common"], c["surname_freq"]))
    dup = collections.Counter(c["person_key"] for c in cands)

    def safe(v):
        v = str(v)
        return "'" + v if v[:1] in ("=", "+", "-", "@") else v
    with open(os.path.join(a.out, "hits.tsv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["уровень", "файл", "лист", "строка", "фамилия", "имя", "отчество", "причины",
                    "частота фамилии", "частая", "персона в N строках", "строка таблицы"])
        for c in cands:
            w.writerow([safe(x) for x in (c["level"], c["file"], c["sheet"], c["row"], c["surname"], c["name"],
                                          c["patronymic"], "; ".join(c["reasons"]), c["surname_freq"],
                                          "да" if c["common"] else "", dup[c["person_key"]], c["line"])])
    L = ["# Поиск по таблицам\n", "## Файлы\n",
         "| Файл | Лист | Строк | Строк из региона | Столбцы |", "|---|---|---|---|---|"]
    for s in stats:
        L.append(f"| {s[0]} | {s[1]} | {s[2]} | {s[3] if reg else '—'} | {s[4]} |")
    L.append("\nНоль строк из региона → база, скорее всего, не про ваши края (или место не указано — тогда "
             "работает только уровень C).\n")
    cnt = collections.Counter(c["level"] for c in cands)
    L.append("## Кандидаты\n")
    for lv, desc in (("A", "место + уезд"), ("C", "полное ФИО человека из древа"), ("B", "фамилия рода + уезд"),
                     ("A?", "место без подтверждения уезда"), ("B?", "фамилия без подтверждения уезда")):
        L.append(f"- {lv} ({desc}): {cnt.get(lv, 0)}")
    L.append(f"- уникальных персон (дедуп между базами): {len(dup)}")
    L.append("\nКаждый кандидат — зацепка. В отчёт как «наш» — только после сверки с ревизией/метрикой.")
    with open(os.path.join(a.out, "summary.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(L) + "\n")
    print(f"OK: {len(cands)} кандидатов {dict(cnt)}, персон {len(dup)} → {a.out}")


if __name__ == "__main__":
    main()
