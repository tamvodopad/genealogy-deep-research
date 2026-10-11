# genealogy-deep-research

**[Русский](#русский) · [English](#english)**

Скилл для Claude Code: глубокий генеалогический ресерч по предкам из Российской империи и СССР — от древа в GEDCOM до большого отчёта с находками, отсеянным и списком заказов в архивы.

A Claude Code skill for deep genealogy research on ancestors from the Russian Empire and the USSR — from a GEDCOM tree to a large report with findings, ruled-out matches and a list of archive orders.

Лицензия / License: [CC0 1.0](LICENSE) — общественное достояние, без условий / public domain, no conditions.

---

## Русский

### Что это

Скилл превращает «найди всё, что есть по моему роду» в повторяемый процесс. Он вырос из нескольких дней реального ресерча по крестьянским родам Вятской и Уфимской губерний. Там сработало вот что: описи шести архивов, газеты XIX века, базы рекрутов и пленных, сверка с ревизскими сказками и отсев однофамильцев.

Главный принцип: **находка существует, только если у неё есть свидетельство, которое можно открыть (лист, кадр, строка описи, URL), и цитата.** Всё остальное — гипотеза.

### Что умеет

- **Читает древо** из GEDCOM («Древо Жизни», MyHeritage, Gramps и др.). Если древа нет, задаёт вопросы анкеты.
- **Анализирует без интернета:**
  - места, включая одноимённые деревни, которые не склеиваются;
  - варианты написания фамилий (Оверин/Аверин, Лапин/Лапкин);
  - «кирпичные стены» по мужским и женским линиям;
  - в каких ревизиях и переписях искать (1646–1858);
  - проблемы данных.
- **Планирует ресерч по единицам «кластер деревень × вид источников»:**
  - адрес деревни в документах по годам: уезд, волость, приход, архив;
  - полнотекст: газеты, памятные книжки, справочники;
  - персональные базы: ПМВ, ВОВ, репрессии, таблицы поисковых групп;
  - чтение сканов;
  - краеведение, фольклор, переселение.
- **Запускает агентов параллельно** и умеет продолжать после обрыва сессии.
- **Проверяет находки.** «Подтверждено» ставится, только если первоисточник открыт и совпали минимум два признака (имя+отчество, деревня, возраст, семья). Сниппет поисковой выдачи — максимум «возможно». Шифр дела засчитывается, только если его видели в описи.
- **Не выдаёт известное за новое:** сверяет находки со списком того, что у вас уже есть.
- **Выпускает отчёт:** главные находки, карточки деревень, стены и почему на них застряли, отсеянное, таблица заказов (`orders.csv`), черновики писем, журнал поиска, поправки к древу.
- **Останавливается на трёх контрольных точках:** план и масштаб; какие сканы читать (самое дорогое); что заказывать.

Скилл **не** отправляет писем, не заказывает и не платит, не обходит капчи и входы, не правит ваше древо и не упоминает возможно живых людей.

### Установка

Нужен [Claude Code](https://claude.com/claude-code) и Python 3.9+. Для таблиц .xlsx/.xls — `pip install openpyxl xlrd`.

```bash
git clone https://github.com/tamvodopad/genealogy-deep-research ~/.claude/skills/genealogy-deep-research
```

После этого перезапустите Claude Code: скилл появится в списке. Другие агенты, которые понимают формат `SKILL.md`, могут подхватить его из `~/.agents/skills/`, но это не проверялось.

### Как пользоваться

1. Выгрузите древо в GEDCOM. В «Древе Жизни»: Файл → Экспорт → GEDCOM, кодировка UTF-8.
2. Напишите Claude, например:
   - «Вот моё древо: ~/Downloads/tree.ged. Запусти большой ресерч по родовым деревням и сделай отчёт»;
   - «Что есть по моим деревням: Сосновка Моршанского уезда, Вязовка Керенского уезда; фамилии Шубин, Лапин»;
   - «Пробей стену по Карпу Шубину, ок. 1772 г.».
3. Ответьте на вопросы: что уже есть на руках, что заказано, бюджет, страна (часть российских сайтов закрыта из-за границы), масштаб.
4. Посмотрите план и выберите, что сузить. Дальше скилл работает сам до следующей контрольной точки.

Пример на вымышленной семье — [`examples/sample-tree.ged`](examples/sample-tree.ged), результат первого шага — [`examples/sample-profile.md`](examples/sample-profile.md).

### Что внутри

```
SKILL.md                     маршрут, контрольные точки, жёсткие правила
references/
  intake.md                  анкета, проверка возможностей, инвентарь «что уже есть»
  tracks.md                  виды работ (T0–T6), приёмы и грабли, шаблон брифа для агента
  verification.md            схема находки, правила «наш / не наш», передача текста документов
  report-template.md         структура отчёта и самопроверка
  sources-russia.md          общероссийские источники: доступ, API, приёмы, ограничения
  new-region.md              как собрать источники по губернии, для которой нет справочника
  regions/                   Вятка, Удмуртия, Татарстан, Башкирия, Оренбург
scripts/
  gedcom_profile.py          профиль древа (только stdlib)
  table_search.py            поиск родни по Excel/CSV-базам с вариантами фамилий
  research_state.py          рабочая папка ресерча, единицы работы, слияние и проверка находок, заказы
examples/                    вымышленное древо и его профиль
```

Скрипты работают и без Claude:

```bash
python3 -I scripts/gedcom_profile.py tree.ged out/            # профиль древа → out/profile.md
python3 -I scripts/table_search.py hits/ base1.xlsx base2.xls --people out/people.json \
    --region "вятск" --district "нолинск|уржум" --place "сосновк|вязовк"
python3 -I scripts/research_state.py --help
```

### Ограничения

- Ранняя версия. Разбор древа, поиск по таблицам и проверка находок протестированы. Сквозной большой ресерч (шаги 4–8) пока прошёл мало реальных прогонов.
- Региональные справочники есть пока только для пяти регионов. Для остальных скилл сначала собирает источники сам (трек T0), и глубина будет ниже.
- Доступ к сайтам меняется. Часть российских ресурсов закрыта из-за границы, часть требует входа или проходит через капчу. Это скилл отмечает как ограничение и не обходит.
- Ресерч большой и потребляет много токенов. Масштаб выбирается на первой контрольной точке.

### Как помочь

Самое ценное — региональные справочники в `references/regions/` по образцу `vyatka.md`: архив, сайт, как читать описи, ловушки, что там сильное. Пул-реквесты и issues приветствуются.

---

## English

### What it is

The skill turns "find everything about my family" into a repeatable process. It grew out of several days of real research on peasant families of the Vyatka and Ufa governorates. What worked there was archive inventories of six archives, 19th-century newspapers, recruit and POW databases, cross-checks against revision lists (*revizskie skazki*) and filtering out namesakes.

Core principle: **a finding exists only if it has evidence you can open (sheet, frame, inventory line, URL) and a quote.** Everything else is a hypothesis.

### What it does

- **Reads a tree** from GEDCOM (Drevo Zhizni / AgelongTree, MyHeritage, Gramps…). Without a tree, it asks a short questionnaire.
- **Analyses it offline:**
  - places, keeping same-name villages apart;
  - surname spelling variants;
  - brick walls on male and female lines;
  - which revisions and censuses (1646–1858) may cover your ancestors;
  - data problems.
- **Plans the research as units of "village cluster × source type":**
  - the village's address in documents over time: district, volost, parish, archive;
  - full-text search: newspapers, yearbooks, directories;
  - personal databases: WWI, WWII, repressions, volunteer spreadsheets;
  - reading scans;
  - local history, folklore, resettlement.
- **Runs agents in parallel** and can resume after an interrupted session.
- **Checks findings.** "Confirmed" requires an opened primary source plus at least two independent identifiers (name+patronymic, village, age, family). A search snippet is at most "possible". An archive reference counts only if it was seen in an inventory.
- **Won't present known as new:** findings are matched against an inventory of what you already have.
- **Produces a report:** key findings, place cards, brick walls with root causes, ruled-out matches, an archive order table (`orders.csv`), draft letters, a search log, suggested tree corrections.
- **Stops at three checkpoints:** plan and scale; which scans to read (the expensive part); what to order.

The skill does **not** send messages, place or pay for orders, bypass captchas or logins, edit your tree, or mention possibly living people.

### Installation

Requires [Claude Code](https://claude.com/claude-code) and Python 3.9+. For .xlsx/.xls tables run `pip install openpyxl xlrd`.

```bash
git clone https://github.com/tamvodopad/genealogy-deep-research ~/.claude/skills/genealogy-deep-research
```

Restart Claude Code and the skill will appear in the list. Other agents that understand `SKILL.md` may pick it up from `~/.agents/skills/` (untested).

### Usage

Export your tree to GEDCOM (UTF-8) and ask Claude, in Russian or English:
- "Here is my tree: ~/Downloads/tree.ged — run a large research on ancestral villages and write a report";
- "What can be found on my villages: Sosnovka, Morshansk district; surnames Shubin, Lapin".

Answer a few questions: what you already have, what is ordered, budget, country (some Russian sites are geo-blocked abroad), scale. Then review the plan.

The skill's instructions and reference files are in **Russian**, because the sources, archives and documents are Russian. Claude can still talk to you in English. A fictional example: [`examples/sample-tree.ged`](examples/sample-tree.ged) → [`examples/sample-profile.md`](examples/sample-profile.md).

### Limitations

- Early version. Tree profiling, table search and finding checks are tested. Full large runs (steps 4–8) have had few real runs so far.
- Regional source guides exist for five regions only. For others, the skill first compiles sources itself (track T0), with less depth.
- Site access changes. Some Russian resources are blocked outside Russia or need a login or captcha. These are reported as limitations, never bypassed.
- A large research run uses many tokens. Scale is chosen at the first checkpoint.

### Contributing

The most valuable contributions are regional guides in `references/regions/`, following `vyatka.md`: archive, website, how to read inventories, pitfalls, strong collections. Issues and pull requests are welcome.

### License

[CC0 1.0 Universal](LICENSE). You can copy, modify and distribute it, even commercially, without asking permission or giving attribution.
