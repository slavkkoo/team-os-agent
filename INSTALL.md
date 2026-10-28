# Установка с нуля

Пошагово: чистый VPS → работающий бот и duty. Команды — для Debian 12/13. Где написано
`<...>` — подставьте своё. Шаги с пометкой «необязательно» можно пропустить: бот работает
и без Telegram-папок, кода и GitLab — тогда он отвечает только из базы знаний.

Что понадобится:

- VPS: 2 vCPU, 4 ГБ RAM, 20 ГБ диска, Debian 12 или 13, доступ по SSH.
- Подписка Claude (Pro/Max) — бот ходит в модель через `claude -p` на подписочном токене.
- Telegram-аккаунт (для создания бота и — необязательно — для чтения папок с чатами).
- Репо базы знаний на GitHub/GitLab — свой форк [team-os-knowledge](https://github.com/slavkkoo/team-os-knowledge).

## 1. Сервер

```bash
ssh root@<server>
apt update && apt -y full-upgrade
apt -y install python3 python3-venv git curl ca-certificates rsync ufw
timedatectl set-timezone <Europe/Berlin>      # ваш пояс; тот же пропишете в .env как TZ
ufw allow OpenSSH && ufw --force enable        # входящий — только SSH; бот ходит наружу сам
```

Боту нужен только исходящий доступ: `api.anthropic.com`, `claude.ai` (для `setup-token`),
`api.telegram.org`, хост вашего git (GitHub/GitLab). Входящих портов он не открывает
(long-poll, без вебхука).

## 2. Пользователь агента

```bash
adduser --disabled-password --gecos "" teamagent
```

(Если пропустите — `install.sh` создаст его сам.) Всё дальше от имени агента — через
`sudo -u teamagent -i` или `su - teamagent`.

## 3. Node.js и Claude Code

```bash
# Debian 13: nodejs из репозитория достаточно свежий; Debian 12 — возьмите Node 20+ с nodejs.org/NodeSource
apt -y install nodejs npm
npm install -g @anthropic-ai/claude-code
sudo -u teamagent claude --version
```

Альтернатива — нативный установщик Claude Code от имени `teamagent` (ставит в
`~/.local/bin`, юниты этот путь уже включают в `PATH`).

## 4. Токен подписки: `claude setup-token`

```bash
sudo -u teamagent -i
claude setup-token          # откроет ссылку: войдите в аккаунт Claude в браузере, вставьте код
```

Команда напечатает долгоживущий токен. Положите его в отдельный файл (его читает systemd
через `EnvironmentFile`, модель его не видит):

```bash
umask 077
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\n' '<токен>' > ~/.claude-token.env
set -a; . ~/.claude-token.env; set +a
claude -p "ответь одним словом: ok" --model claude-sonnet-5-5     # должно вернуть ok
exit
```

## 5. Бот в @BotFather

1. В Telegram откройте @BotFather → `/newbot` → имя (например «Робин») → username,
   оканчивающийся на `bot`. Токен → `TG_BOT_TOKEN`, username без `@` → `TG_BOT_USERNAME`.
2. `/setprivacy` → ваш бот → **Enable** (рекомендуется). С privacy mode бот в группах видит
   только сообщения с упоминанием, реплаи на свои ответы и команды — лишняя переписка к нему
   не попадает. **Disable** нужен, только если хотите, чтобы бот 5 минут после ответа
   слушал автора без тега (`GROUP_FOLLOWUP_S`); тогда он получает все сообщения группы.
   После смены privacy бота нужно удалить из группы и добавить снова.
3. `/setjoingroups` — разрешить добавлять в группы (или запретить, если нужна только личка).
4. `/setdescription`, `/setabouttext` — что это за бот и кто его владелец.

Узнать id (бот ещё не запущен — иначе `getUpdates` вернёт 409):

```bash
# напишите боту в личку любое сообщение, добавьте его в нужную группу/канал и напишите там
curl -s "https://api.telegram.org/bot<TG_BOT_TOKEN>/getUpdates" | python3 -m json.tool | grep -E '"(id|title|first_name)"'
```

`from.id` в личке — ваш `OWNER_ID`; id коллег — в `TEAM_IDS` через запятую; `chat.id`
группы/канала (вида `-100…`) — в `DIGEST_CHAT_ID`. В канал бота нужно добавить
администратором с правом публикации.

## 6. Код агента и install.sh

```bash
sudo -u teamagent git clone https://github.com/slavkkoo/team-os-agent /home/teamagent/.local/share/team-agent
cd /home/teamagent/.local/share/team-agent
./install.sh --dry-run          # посмотреть, что будет сделано
sudo ./install.sh               # каталоги, .env из примера, settings, venv с Telethon, юниты
```

Репо лежит прямо в `AGENT_HOME` — `deploy.sh` потом обновляет код там же. `install.sh`
идемпотентен: `.env` и токен не перезаписывает, сгенерированное (settings, юниты) — обновляет.

## 7. Telegram-сессия для чтения папок (необязательно: дайджест и VoC)

Bot API не видит папки и историю чатов, поэтому дайджест читает их от имени
пользовательского аккаунта через Telethon — только чтение, ничего не пишет и не отмечает
прочитанным.

1. Зайдите на <https://my.telegram.org> → **API development tools** → создайте приложение
   (название любое, платформа Other). `api_id` → `TG_API_ID`, `api_hash` → `TG_API_HASH`.
2. В Telegram создайте папки (Настройки → Папки с чатами), например «Работа» и «Клиенты»,
   и добавьте в них чаты **явно** (правила «все группы» скрипт не разворачивает).
   Имена папок → `DIGEST_FOLDERS` и `VOC_FOLDERS`.
3. Залогиньтесь интерактивно:

   ```bash
   sudo -u teamagent -i
   cd ~/.local/share/team-agent
   .venv/bin/python3 bin/tg_login.py
   ```

   Скрипт спросит телефон и покажет, куда Telegram отправил код (обычно — в чат «Telegram»
   в приложении на вашем телефоне). `r` — запросить код повторно другим способом. Если
   включена двухэтапная проверка — спросит облачный пароль. Код и пароль никуда не уходят.
4. Проверка: `.venv/bin/python3 bin/read_chat.py list` — список ваших папок;
   `.venv/bin/python3 bin/read_chat.py folder "Работа" 24h | head` — сообщения за сутки.

Сессия = полный доступ к аккаунту. Файл `tg/reader.session` закрыт правами 600, исключён из
чтения моделью и попадает в бэкап. Безопаснее завести для агента отдельный аккаунт и
добавить его в нужные чаты. Отозвать: Telegram → Настройки → Устройства.

## 8. База знаний (deploy key)

```bash
sudo -u teamagent -i
ssh-keygen -t ed25519 -f ~/.ssh/knowledge_deploy -N "" -C "team-agent knowledge"
cat ~/.ssh/knowledge_deploy.pub
```

Добавьте ключ в репо знаний: GitHub → Settings → Deploy keys → Add (поставьте **Allow write
access**, если хотите, чтобы brain-sync и learn-questions пушили ветки; без записи эти duty
будут слать алерт «push не прошёл»).

```bash
cat >> ~/.ssh/config <<'EOF'
Host github-knowledge
  HostName github.com
  User git
  IdentityFile ~/.ssh/knowledge_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config
ssh-keyscan github.com >> ~/.ssh/known_hosts
git clone git@github-knowledge:<you>/team-os-knowledge.git ~/.local/share/team-agent/knowledge
exit
```

В `.env`: `KNOWLEDGE_REPO_URL=git@github-knowledge:<you>/team-os-knowledge.git`,
`KNOWLEDGE_BRANCH=main`. В репо знаний включите защиту `main` (Settings → Branches →
require pull request): конвейеры агента и так пушат только ветки, защита страхует от ошибок.

## 9. Заполнить .env

```bash
sudo -u teamagent nano /home/teamagent/.local/share/team-agent/.env   # chmod 600 уже стоит
```

Минимум: `AGENT_NAME`, `TEAM_NAME`, `PRODUCT_NAME`, `OWNER_NAME`, `TG_BOT_TOKEN`,
`TG_BOT_USERNAME`, `OWNER_ID`, `TEAM_IDS`, `TZ`. Для дайджеста и VoC — `TG_API_ID`,
`TG_API_HASH`, `TG_SESSION`, `DIGEST_FOLDERS`, `VOC_FOLDERS`, `DIGEST_CHAT_ID`. Каждая
переменная описана в `.env.example`.

## 10. Код и GitLab (необязательно)

1. Токен GitLab: Project/Group → Settings → Access tokens, роль Reporter, scopes `read_api`
   (+ `read_repository` для клонов по HTTPS). → `GITLAB_TOKEN`, `GITLAB_URL`, `GITLAB_PROJECT`
   (id или `group/project` проекта с задачами).
2. Список репо кода:

   ```bash
   sudo -u teamagent -i
   cd ~/.local/share/team-agent
   cp config/code-repos.txt.example config/code-repos.txt && nano config/code-repos.txt
   bin/update_code.sh            # первый раз склонирует, дальше — раз в неделю по таймеру
   bin/gl search бронь           # проверка GitLab
   exit
   ```

Если GitLab во внутренней сети — нужен VPN. Только **split-tunnel**: в конфиге OpenVPN
`pull-filter ignore "redirect-gateway"` и маршруты внутренних сетей (`route <сеть> <маска>`).
Full-tunnel уведёт весь трафик сервера в туннель, и вы потеряете SSH. Доступность VPN
мониторит watchdog: `WATCHDOG_TCP_CHECKS=vpn-gitlab=<внутренний-хост>:443`.

## 11. Перерендерить и проверить без Telegram

```bash
cd /home/teamagent/.local/share/team-agent
sudo ./install.sh                         # подхватит TZ из .env и покажет, чего не хватает
sudo -u teamagent bin/ask.sh "Что ты знаешь о продукте?"
sudo -u teamagent env PUBLIC=1 bin/ask.sh "Покажи код бронирования"   # публичный режим: должен отказать
```

## 12. Включить юниты

```bash
systemctl enable --now team-agent.service teamos-watchdog.timer teamos-backup.timer
systemctl enable --now team-agent-decisions.timer team-agent-questionlearn.timer team-agent-mergereview.timer
# если настроены Telegram-папки:
systemctl enable --now team-agent-digest.timer team-agent-voc.timer
# если настроен код:
systemctl enable --now team-agent-codesync.timer team-agent-stalefacts.timer team-agent-brainsync.timer
systemctl list-timers 'team-agent-*' 'teamos-*'
```

Ручной прогон любой duty сейчас: `systemctl start team-agent-digest` (смотреть —
`journalctl -u team-agent-digest -f`).

## 12a. Коннекторы (необязательно)

Все коннекторы к внешним системам (логи и метрики, API продукта, хелпдеск, отчёты клиентам,
прод-дайджест, eval, PR на GitHub, MCP) выключены. Включайте по одному — подробности, права
токенов и риски каждого — в [connectors/README.md](connectors/README.md).

```bash
sudo -u teamagent -i
cd ~/.local/share/team-agent
# 1. конфиг коннектора, если он нужен (config/*.json — в .gitignore)
cp config/product-api.example.json config/product-api.json && nano config/product-api.json
# 2. в .env: CONNECTOR_<ИМЯ>_ENABLED=1 + его URL и read-only токены (см. .env.example)
nano .env
# 3. проверка без сети и ручной вызов
python3 connectors/product-api/product_api.py --selftest
connectors/product-api/product_api.py get /v1/services
exit
sudo ./install.sh        # прогонит selftest включённых и напечатает команды для их таймеров
```

Юниты duty коннекторов `install.sh` рендерит всегда, но **не включает**: сначала ручной прогон
(`systemctl start team-agent-ticketlearn.service`, `journalctl -u team-agent-ticketlearn -n 50`),
потом `systemctl enable --now team-agent-<duty>.timer`. Бот подхватывает включённые коннекторы
сам (промпт перечитывается), после правки `.env` — `systemctl restart team-agent`.

MCP: `cp connectors/mcp/mcp.json.example config/mcp.json && chmod 600 config/mcp.json`, в `.env`
`MCP_CONFIG=config/mcp.json` и `MCP_ALLOWED_TOOLS=mcp__<сервер>__<инструмент>,…`, проверка —
`python3 connectors/mcp/mcp_check.py check`.

## 13. Проверка: «напиши боту»

1. Личка: напишите боту вопрос. Должно прийти «Принял вопрос, проверяю источники…», затем
   ответ со строкой «Источник:». `/help` — бот скажет, какой у вас режим.
2. Попросите коллегу не из `TEAM_IDS` спросить «покажи код» — должен получить фразу про
   доступ к внутренним системам.
3. Группа: добавьте бота, напишите `@<username бота> вопрос` — ответ придёт реплаем.
4. Поправка: ответьте на ответ бота «ошибаешься, …» → «Записал поправку (staged: …)».
   От владельца в личку: `learnings`, затем `promote <slug>`.
5. Алерт: `systemctl start notify-failure@test.service` — владельцу придёт «Упал test.service».

## 14. Логи

```bash
journalctl -u team-agent -f                         # бот: старт, ошибки, 409, остановка
journalctl -u team-agent-digest -n 50               # конкретная duty
tail -n 5 /home/teamagent/.local/share/team-agent/logs/interactions.jsonl   # каждый вопрос: latency, turns, ok/ошибка
systemctl --failed
```

В `interactions.jsonl` у неудачных ответов есть поле `error` с причиной (`timeout after …`,
`is_error=True … result='…limit…'`, `non-json …`). Отладка duty без повторного чтения
Telegram: `sudo -u teamagent bin/run_digest.sh --reuse-dump`.

## 15. Обновления

Правьте код и промпты в своём форке, пушьте в `main`, на сервере:

```bash
/home/teamagent/.local/share/team-agent/deploy.sh
```

Скрипт тянет `main`, проверяет синтаксис python/bash, импортирует бота и собирает промпт
(ловит потерянные глобалы и дыры в шаблонах), мягко рестартит (начатые ответы дописываются),
проверяет журнал и **откатывается** на прошлый коммит при сбое. Изменились юниты или
settings — после деплоя запустите `sudo ./install.sh`.

## 16. SSH-харденинг

1. Положите свой публичный ключ: `ssh-copy-id root@<server>` (или в `~/.ssh/authorized_keys`
   отдельного админ-пользователя с sudo).
2. Проверьте вход по ключу в **новой** сессии, не закрывая текущую.
3. Узнайте, где у провайдера аварийная веб-консоль (VNC/serial) — на случай потери ключа.
4. Примените:

   ```bash
   cd /home/teamagent/.local/share/team-agent && sudo ./install.sh --ssh-hardening
   systemctl reload ssh
   ```

   Это `PasswordAuthentication no`, `PermitRootLogin prohibit-password`,
   `KbdInteractiveAuthentication no`. Публичный сервер с паролем root получает десятки тысяч
   попыток подбора в неделю. Дополнительно — `apt install fail2ban unattended-upgrades`.

## 17. Бэкапы

`teamos-backup.timer` каждую ночь кладёт в `/var/backups/teamos/teamos-ГГГГММДД.tar.gz`
(14 дней, chmod 600): `memory/` (поправки, offset, контексты, дайджесты), `.env`, токен
Claude, Telegram-сессию, deploy key, юниты и хост-скрипты. Код и база знаний — в git.

Копия на том же диске не спасает от потери сервера. Забирайте её на другую машину, например
cron на ноутбуке:

```bash
rsync -e ssh root@<server>:/var/backups/teamos/latest.tar.gz ~/backups/team-agent-$(date +%F).tar.gz \
  && ssh root@<server> touch /var/backups/teamos/.last_offsite_pull
```

и включите контроль в `.env`: `WATCHDOG_OFFSITE_MAX_H=48` — watchdog напишет, если копию не
забирали двое суток. Восстановление: `tar xzf teamos-….tar.gz -C /` на новом сервере после
шагов 1–6, затем `sudo ./install.sh` и `systemctl enable --now …`.

## Если что-то не так

| Симптом | Причина и что делать |
|---|---|
| В журнале `getUpdates error: HTTP Error 409` | Где-то запущен второй экземпляр с тем же токеном (старый сервер, ноутбук, ручной `getUpdates`). Погасите его. |
| Ответ «Не смог собрать ответ», в `error` — `Not logged in` / `401` | Пустой или протухший `CLAUDE_CODE_OAUTH_TOKEN`: повторите шаг 4, `systemctl restart team-agent`. |
| Алерт «упёрлись в лимит подписки» | Кончилось окно подписки. Снизьте `GLOBAL_DAILY_LIMIT`, переведите duty на `MODEL_DUTY` попроще, разнесите таймеры. |
| В группе бот молчит | Нет `@username` в сообщении или privacy mode сменили без переподключения бота к группе; группа не в `ALLOWED_GROUP_IDS`. |
| Дайджест не пришёл | `journalctl -u team-agent-digest -n 50`; «сессия не авторизована» — шаг 7; «Нет папок» — имя в `DIGEST_FOLDERS` не совпадает с `read_chat.py list`. |
| brain-sync: «push ветки не прошёл» | У deploy key нет записи, или ветка защищена правилом на все ветки. |
| `install.sh`: «репо лежит в …, а AGENT_HOME=…» | Клонируйте репо прямо в `/home/teamagent/.local/share/team-agent` (шаг 6). |
