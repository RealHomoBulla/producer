# Producer — команда ИИ-агентов под любой проект

**In English, in short.** A project skeleton where a team of AI agents works while you sleep: a **Producer** hands out tasks,
**Workers** do them, a **Guardian** keeps everyone awake overnight. You set a goal, go to bed, and read a short digest and answer
a few questions in the morning. Works for a website, an app, a bot, documentation, research. Needs **Orca** (the terminal that
hosts the agent tabs), **Git**, **Python 3.11+** and **Claude Code** with a subscription; one Claude subscription is enough
(see «Одна подписка Claude»). Quick start (4 steps): 1) ask your Claude *«clone https://github.com/RealHomoBulla/producer into
`<folder>`»* (or press **Use this template** on GitHub) · 2) open the folder in Orca · 3) `python tools/setup.py` (add `--lang en`
for English owner pages) · 4) `python tools/guardian.py start` — the Producer tab opens and asks what you want built. Owner
pages and the Producer's talk are in the language you choose in setup (default Russian); everything internal is English.

## If you are an AI assistant asked to set this up

Follow these steps exactly, in order; do not guess. Speak to the user in their language, keep messages short.

1. **Clone.** `git clone https://github.com/RealHomoBulla/producer <folder>` (or use the folder the user made from the GitHub
   template). Never push to the template repository: `python tools/setup.py` renames a template `origin` to `template`, disables
   pushing to it and prints how to create the user's own repo.
2. **Check the machine.** `cd <folder>` then `python tools/producer.py doctor`. It is read-only and prints `ok / WARN / FAIL`
   with the fix for each line. Python 3.11+ and Git are required (on Windows install `tzdata` so time-zone names
   resolve); fix every `FAIL` before going on.
3. **Set up.** Ask the user for the project name, the language of the owner pages (`ru` or `en`) and the **permission mode**:
   **a** = no permission prompts (recommended for parallel Workers and the night run; agents edit files and run commands in
   this folder themselves) or **b** = auto-edits plus a command allow-list. Then `python tools/setup.py` — or, only if the user
   agrees to the defaults, `python tools/setup.py --non-interactive --name <name> --lang <ru|en> --yes [--attended|--unattended]`.
   The choice is written into `.claude/settings.local.json` and, for mode a, `~/.claude/settings.json`, by **merging JSON**
   (existing keys are never dropped). It exits non-zero and prints `NOT READY` when Orca or Claude Code is missing. The user
   logs in to each CLI **themself** (`claude` → `/login`); never ask for a key or password in chat and never print one.
4. **Orca.** `doctor` shows the Orca line. If it is missing, tell the user to install the Orca desktop app from the Orca
   project's own site and open it once. **Without Orca you can still be the Producer in this chat** (go to step 6): there are no
   parallel tabs and no Guardian, so you do the Workers' jobs one after another and say so.
5. **Start the Guardian.** `python tools/guardian.py start`. With Orca running it opens the Producer tab, which reads
   `work/agents/orca/START_PROMPT.md` and asks the user what to build. Tell the user in one line: «the Producer tab is open —
   continue there». Details and recipes: `work/agents/knowledge/GUARDIAN.md`.
6. **Or become the Producer yourself** (no Orca, or the user prefers this chat): read `AGENTS.md`, then
   `work/agents/orca/START_PROMPT.md` **in full**, and run the kickoff (§0): ask the brief's questions **one at a time** and write
   `work/БРИФ.md` (`BRIEF.md` for an English owner). Never run two Producers for one project at the same time.
7. **Always:** commit exact paths (`git commit --only -m "…" -- <paths>`), keep keys outside the repo, and put anything the
   owner must decide into `work/agents/registers/OPEN.md` — not into a long chat message. `setup.py` leaves the tree dirty
   on purpose: commit `producer.toml` and the new files under `work/agents/state/` it wrote as the project's first commit.

## Что нужно установить

| что | зачем |
|---|---|
| Orca — терминал-оркестратор агентов (установи десктоп-приложение с сайта проекта Orca) | в нём живут вкладки продюсера и воркеров |
| Git и Python 3.11+ (рекомендуется 3.12) | проект и инструменты; на Windows для названий часовых поясов нужен `pip install tzdata` |
| Claude Code (`claude`) с подпиской | продюсер (нужен обязательно); одной подписки хватает |
| по желанию: Codex (`codex`), OpenCode (`opencode`), Gemini/AGY, ключи OpenRouter / DeepSeek | дополнительные воркеры |

Ключи моделей лежат в файле, который назван в `producer.toml` → `[paths] keys_file` (по умолчанию `~/.config/producer/keys.env`),
вне проекта; в git они не попадают. Подробности: `work/agents/orca/ROUTING.md`, раздел «Credentials».

## Быстрый старт

1. Попроси своего Claude: «склонируй https://github.com/RealHomoBulla/producer в папку `<имя проекта>`» (или нажми **Use this
   template** на GitHub и склонируй свой репозиторий).
2. Открой папку в Orca.
3. `python tools/setup.py` — спросит название, язык и пресет, проверит, что стоит, и заполнит `producer.toml`. Для английских
   страниц владельца: `--lang en`. (`python tools/producer.py doctor` в любой момент покажет, чего не хватает.)
4. `python tools/guardian.py start` — откроется вкладка продюсера и спросит, что мы строим.

Дальше пиши продюсеру как человеку: «ростер — 1 Sonnet и 1 Haiku, работай всю ночь, нужны такие-то анимации». Он разобьёт цель на
задачи в `TODO`, раздаст воркерам (каждому свои файлы, чтобы двое не правили одно и то же), проверит результат и закоммитит.
Вопросы, которые решаешь только ты, он отложит в блиц на утро. Ночная работа: когда `setup.py` отработал и ты согласен, что агенты
могут работать без вопросов, включи автономный режим —
`python tools/guardian.py autonomy on --objective "Сайт-презентация фирмы умного дома под ключ …"`.

## Одна подписка Claude

Самый частый случай: у тебя **одна** подписка Claude (например Pro за $20), а хочется параллельно: один воркер делает анимации,
другой пишет тексты разделов — и чтобы это шло всю ночь. Так и устроено, но есть что знать заранее:

- **Что ставит setup.** Если в системе нашёлся только `claude`, пресет `solo-claude` выбирается сам: продюсер — Claude Sonnet
  (medium), воркеры — один Sonnet и один Haiku, никаких других маршрутов. Вручную:
  `python tools/setup.py --preset solo-claude`.
- **Одно окно на всех.** Продюсер и оба воркера — это три вкладки Claude Code на одном аккаунте, они делят **одно окно в 5 часов**
  (и одну недельную квоту). Больше вкладок — не больше работы, а окно кончается быстрее. Поэтому не больше двух воркеров
  одновременно; третья задача ждёт свободного места.
- **Когда окно кончилось.** Claude пишет «5-hour limit reached · resets 7pm». Гардиан замечает это, вкладки ждут, а **в момент
  сброса его будильник будит всех** (через пару минут после времени сброса) — работа продолжается сама. Утром на странице
  `work/ДАЙДЖЕСТ.md` — что сделано, в `work/БЕЗ_ОТВЕТА.md` — твои вопросы, в `work/ROADMAP.md` — прогресс. Если кончилась
  недельная квота, всё встанет до её сброса: продюсер напишет об этом в дайджесте.
- **Ночью без вопросов про разрешения.** Чтобы Claude не останавливался на каждом запросе разрешения, нужен флаг пропуска
  разрешений: `python tools/setup.py --preset solo-claude --unattended`. Он включается **только** по твоему слову и только для
  папки, которую агентам не страшно править свободно. Без него ночью агент встанет на первом вопросе.
- **Как делятся файлы.** Пример разбивки сайта на «анимации / тексты / вёрстка» так, чтобы двое никогда не правили один файл, —
  `work/agents/orca/DISPATCH.md`, раздел «Worked example».

## Пресеты

Пресет — готовый набор «продюсер + воркеры» под то, что у тебя есть. Четыре из `tools/presets.toml`: **solo-claude** (одна
подписка Claude, $20/мес), **claude-max-100** (Claude Max + Codex + OpenCode Go + бесплатные), **claude-plus-go** (Claude Pro +
OpenCode Go), **budget-free** (только бесплатные модели) — плюс встроенный `full` (по месту на каждый найденный CLI).
`python tools/setup.py --list-presets` показывает цену и чего каждому не хватает; `setup.py` сам берёт `solo-claude`, если найден
только `claude`, иначе самый полный из тех, что тянет твоя машина. Повторный запуск ничего не перезаписывает: твои правки команд
и таймеров живут, пока не передашь `--preset` или `--reset-guardian`. Флаг пропуска разрешений Claude ни в один пресет не
попадает без твоего `--unattended`. Какая модель на какую работу и где докупить usage — `work/agents/knowledge/MODEL_ADVICE.md`;
живые остатки окон — `python tools/usage.py` (в чате: «лимиты»).

## Подключи свои аккаунты (один раз на компьютер)

В репозитории **нет ничьих ключей**, каждый подключает свои. Проще всего попросить своего Claude: открой папку, запусти `claude`
и скажи «подключи мои аккаунты». Он:
1. проверит, какие программы стоят (`claude`, `codex`, `opencode`, `agy`, `orca`, `git`);
2. попросит тебя самого залогиниться в каждой (`claude` → `/login`, `codex login`, `opencode auth login` …). Пароли
   вводишь ты, агент их не видит;
3. создаст файл ключей (вне проекта, только для тебя) с названиями переменных. Значения ключей (OpenRouter, DeepSeek, OpenCode Go
   …) вписываешь туда сам;
4. проверит каждый маршрут пробным запросом и запишет в `producer.toml` ростер из того, что реально работает.

Ключи никогда не попадают в git: файл лежит вне проекта, а `.gitignore` отсекает любые `*.env`. Важно: «программа есть в PATH» не
значит «залогинен» — `python tools/setup.py --probe` проверяет только, что программа запускается (`--version`), а логин и доступ к
модели проверяет настоящий пробный запрос на шаге 4.

## Что ты читаешь

| файл | что там |
|---|---|
| `work/БРИФ.md` | бриф проекта: цель, аудитория, что обязательно, стиль, «готово, когда», §11 — курс продукта. Продюсер заполняет его с тобой на старте и обновляет по ходу |
| `work/ROADMAP.md` | план готовности: этапы, проценты, что в работе, что мешает |
| `work/ДАЙДЖЕСТ.md` | что сделали, пока тебя не было, коротко |
| `work/БЕЗ_ОТВЕТА.md` | вопросы к тебе |
| `work/ЧЕКЛИСТ.md` | что проверить руками (открыть сайт, кликнуть, посмотреть) |
| `work/systems/` | страницы по частям проекта |

С `--lang en` те же страницы называются `BRIEF.md`, `DIGEST.md`, `UNANSWERED.md`, `CHECKLIST.md` и пишутся по-английски.

## Твои короткие команды (пишешь продюсеру)

«блиц» — вопросы к тебе по одному в отдельной вкладке · «закрыть блиц» · «дайджест» · «прочитал дайджест» · «лимиты» ·
«ночная вахта» · «стоп автономно» · «закрой воркеров» · «что там по отчётам». Полный список (и английские эквиваленты):
`work/agents/orca/OWNER_SHORTHANDS.md`.

## Как устроено внутри

`AGENTS.md` — правила для всех агентов. `work/agents/orca/` — как работают продюсер, воркеры и гардиан, какие модели в чём
сильны. `work/agents/registers/` — очередь работы, решения, отчёты. `tools/` — дайджест, вопросы, ревью коммитов, почта
продюсера, гардиан, `producer.py doctor` (проверка машины), `doc_check.py` (мёртвые ссылки в документации). Что откуда взято:
`INVENTORY.md`. Карта файлов: `work/agents/knowledge/STRUCTURE.md`.

## Обновления

`./producer.sh update` (Windows: `producer update`) подтягивает **твой** проект из **твоего** `origin` безопасно: отказывается, если
есть незакоммиченные правки (и отслеживаемые, и новые файлы), делает только `git pull --ff-only` и никогда не сливает расходящуюся
историю за тебя. Улучшения самой болванки приходят отдельно: `git fetch template`, потом `git log HEAD..template/main` и ручное
слияние — они могут касаться файлов, которые ты уже переписал.

## Разработка и тесты

Тесты инструментов — на pytest: `python -m pip install -r requirements-dev.txt`, затем
`python -m pytest -q tools/tests -p no:cacheprovider`. Если на Windows pytest падает с ошибкой доступа к
`%TEMP%\pytest-of-<имя>`, добавь свою папку: `--basetemp <новая папка>`. На Windows с кириллицей в консоли можно задать
`PYTHONUTF8=1`. Проверка документации: `python tools/doc_check.py`. Этот же набор гоняет CI на Windows и Linux
(`.github/workflows/tests.yml`).
