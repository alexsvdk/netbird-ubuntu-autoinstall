# Инструкция по сборке и эксплуатации: Samovar Autoinstall

Данное руководство описывает полный цикл работы с установочным образом для домашнего сервера **`samovar`** (Acer Aspire TC-605, i7-4770, 16 GB RAM, NVIDIA RTX 3060 12 GB).

---

## 1. Подготовка конфигурации `samovar-config.json`

Все изменяемые сетевые настройки, профили NetBird и конфигурация прокси Mihomo хранятся в файле `samovar-config.json`, который подписывается вашим SSH-ключом.

1. Скопируйте шаблон из примеров:
   ```bash
   cp examples/samovar-config.example.json samovar-config.json
   chmod 600 samovar-config.json
   ```

2. Заполните реальные значения:
   - **`wifi.networks`**: список Wi-Fi сетей (SSID и пароли). Если сеть скрытая — укажите `"hidden": true`.
   - **`netbird`**:
     - `management_url`: `https://api.netbird.io:443` (или ваш self-hosted экземпляр по HTTPS).
     - `setup_key`: одноразовый ключ регистрации из панели NetBird.
   - **`mihomo.config`**: конфигурация прокси Clash/Mihomo.
     - Обязательно должен присутствовать хотя бы один работающий inline proxy-узел в секции `proxies` (для исключения bootstrap deadlock).
     - Секрет внешнего контроллера (`secret`).

3. Проверьте валидность файла по схеме:
   ```bash
   python3 -m pytest tests/test_config_schema.py -v
   ```

---

## 2. Подписание конфигурации

Конфигурация заверяется отсоединённой (detached) SSH-подписью Ed25519 с пространством имён `samovar-recovery`.

Запустите скрипт подписания (по умолчанию использует ключ `~/.ssh/id_ed25519`):
```bash
chmod +x build-recovery-config.sh
./build-recovery-config.sh samovar-config.json ~/.ssh/id_ed25519
```

В результате будет создан файл подписи: `samovar-config.json.sig`.

> [!WARNING]
> Никогда не коммитьте `samovar-config.json` и `*.sig` в Git! Они уже добавлены в `.gitignore`.

---

## 3. Настройка переменных окружения (`.env`)

Создайте локальный файл `.env`:
```bash
cp .env.example .env
```

Отредактируйте `.env`:
```bash
# Режим Samovar (включается автоматически при наличии samovar-config.json)
SAMOVAR_MODE=samovar

# Необязательно: префикс serial, который добавляет QEMU.
# Для физического сервера строку не задавать.
# Для VM с QEMU IDE-дисками:
DISK_SERIAL_PREFIX=QEMU_HARDDISK_

# Размер каждого из двух swap-файлов в GiB (по умолчанию 1)
# SWAP_SIZE_GIB=8

# Целевая архитектура и релиз
ARCH=amd64
UBUNTU_VERSION=24.04.4   # или 26.04.1 при выходе

# Имя хоста и пользователь
HOSTNAME=samovar
USERNAME=alex

# Пароль для локальной консоли (будет захэширован через openssl)
PASSWORD=ВашНадежныйПарольДляКонсоли

# Ваш публичный SSH-ключ для авторизации
SSH_PUBLIC_KEY="ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... alex@laptop"

# Строка доверенного подписанта для ssh-keygen -Y verify
# Формат: <principal> namespaces="samovar-recovery" <key-type> <public-key>
ALLOWED_SIGNERS="alex@samovar namespaces=\"samovar-recovery\" ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI... alex@laptop"

# Зеркало APT (приоритет Яндекса)
APT_REGION=ru
APT_MIRROR=http://mirror.yandex.ru/ubuntu

# Тема ntfy.sh для уведомлений о ходе установки
NOTIFY_TOPIC=samovar_test
```

Установите приложение **ntfy** на телефон и подпишитесь на ту же тему. Для реальной установки лучше задать длинную уникальную тему.

---

## 4. Сборка установочного ISO

Запустите сборку:
```bash
chmod +x build-autoinstall-iso.sh
./build-autoinstall-iso.sh
```

В процессе сборки:
1. Автоматически скачивается официальный базовый ISO Ubuntu Server (с верификацией контрольной суммы).
2. Запускается валидация `validate-autoinstall-iso.py`.
3. В образ монтируются:
   - Автоустановщик с точной привязкой разделов по серийным номерам дисков:
     - Kingston SSD (`50026B7683695BFE`) → `/` и `/boot/efi`
     - SBSSD SSD (`TD2023102401304`) → `/data`
     - WD HDD (`WCC3F1336131`) → `/archive`
   - Preflight-скрипт (проверяет UEFI, архитектуру `x86_64` и точное совпадение всех 3 дисков).
   - Утилиты и сервисы `samovar-recovery-agent`, `samovar-provision`, шаблоны Compose и udev-правила.
4. На выходе формируются два файла:
   - `ubuntu-24.04.4-autoinstall-amd64.iso`
   - `ubuntu-24.04.4-autoinstall-amd64.iso.sha256`

---

## 5. Запись на установочную флешку

Запишите полученный ISO на USB-накопитель с помощью любой удобной утилиты:
- **Raspberry Pi Imager**
- **Balena Etcher**
- **Rufus** (в режиме прямого DD/ISO образа)

---

## 6. Настройки BIOS целевой машины (`samovar`)

Перед первой установкой зайдите в BIOS (клавиша `Del` или `F2` при включении):
1. **Boot Mode**: переключите в `UEFI only` (Legacy / CSM отключить).
2. **Secure Boot**: `Disabled`.
3. **Power Management**: найдите параметр **`Restore on AC Power Loss`** (или `AC Back` / `After Power Failure`) и установите значение **`Power On`** (чтобы сервер автоматически поднимался после сбоев электричества).
4. Установите загрузку с USB-носителя.

---

## 7. Установка системы

1. Подключите сетевой кабель Ethernet (если есть) или убедитесь в наличии Wi-Fi сети, указанной в конфигурации.
2. Вставьте установочную флешку в `samovar` и включите питание.
3. Процесс установки проходит **полностью автоматически**:
   - Preflight-проверка подтверждает наличие трех серийных номеров.
   - Диски полностью форматируются и размечаются.
   - Базовая система разворачивается.
   - По завершении установщик **выключает питание** (poweroff), а не перезагружает компьютер в цикл.
4. Извлеките установочную флешку.
5. Включите компьютер кнопкой питания.

---

## 8. Первый запуск и проверка

При первом запуске установленной системы:
1. Поднимаются сетевые интерфейсы (`lan0` и `wifi0`).
2. Запускается сервис первичной инициализации `samovar-recovery-bootstrap.service`, который:
   - Проверяет SSH-подпись встроенного `samovar-config.json`.
   - Применяет настройки Wi-Fi.
   - Регистрирует сервер в NetBird под именем `samovar`.
   - Запускает сервис Mihomo.
3. Запускается сервис `samovar-provision.service`, который:
   - Устанавливает драйвер NVIDIA для RTX 3060 и NVIDIA Container Toolkit.
   - Разворачивает Docker с `data-root` на `/data/docker`.
   - Настраивает правила межсетевого экрана UFW (SSH разрешён только на `wt0`, `wifi0`, `lan0`).
   - Ограничивает логи journald и Docker до 100 МБ.
   - Включает `fstrim.timer` для SSD.

### Проверка доступа:
1. Зайдите в панель NetBird — сервер `samovar` должен появиться со статусом **Connected**.
2. Подключитесь по SSH через NetBird IP:
   ```bash
   ssh alex@<netbird-ip-or-samovar>
   ```
3. Проверьте состояние оборудования:
   ```bash
   nvidia-smi                    # Должна определяться RTX 3060 12GB
   docker info                   # data-root: /data/docker
   df -h / /data /archive        # Все три диска смонтированы
   sudo ufw status verbose       # SSH разрешен только на wt0, lan0, wifi0
   ```
4. После успешной регистрации отзовите/удалите одноразовый `setup_key` в панели управления NetBird.

### Обновление Mihomo

Mihomo запускается как контейнер Docker Compose. Релиз не вшивается в host и не требует пересборки ISO. Образ задаётся в `/etc/mihomo/compose.env`. Для обновления до текущего `latest`:

```bash
sudo docker compose --env-file /etc/mihomo/compose.env \
  -f /etc/mihomo/compose.yml pull mihomo
sudo systemctl restart mihomo.service
```

Чтобы использовать конкретный тег, измените `MIHOMO_IMAGE` в `/etc/mihomo/compose.env`, затем выполните те же команды.

---

## 9. Аварийное обновление сети и настроек с Recovery-флешки

Если Wi-Fi сменился, NetBird недоступен или требуется сменить прокси:

1. Отформатируйте любую USB-флешку в **FAT32** с меткой тома **`SAMOVARCFG`**:
   ```bash
   # macOS:
   diskutil eraseVolume FAT32 SAMOVARCFG /dev/diskN
   
   # Linux:
   sudo mkfs.vfat -n SAMOVARCFG /dev/sdX1
   ```

2. Сформируйте новый `samovar-config.json` с инкрементированным полем `"generation"` (например, `2026092001` > `2026091901`). Защита от replay отклонит конфигурацию с меньшим или равным generation!

3. Подпишите новый конфигурационный файл:
   ```bash
   ./build-recovery-config.sh samovar-config.json ~/.ssh/id_ed25519
   ```

4. Скопируйте оба файла в корень флешки `SAMOVARCFG`:
   - `samovar-config.json`
   - `samovar-config.json.sig`

5. Вставьте флешку в работающий сервер `samovar` (или вставьте до включения):
   - udev-правило `99-samovar-recovery.rules` мгновенно активирует сервис `samovar-recovery.service`.
   - Носитель монтируется `ro,nodev,nosuid,noexec`.
   - Файлы копируются в память `/run`, флешка немедленно размонтируется.
   - Проверяется SSH-подпись доверенным ключом.
   - Изменения применяются транзакционно (с автоматическим откатом в случае ошибки сети).
   - После успешного применения флешку можно извлечь.

---

## 10. Использование режимов работы контейнеров

В каталоге `provisioning/compose-templates/` подготовлены готовые шаблоны Docker Compose:

1. **Прямой выход (Direct)** — прямой российский трафик:
   - Файл: `provisioning/compose-templates/direct.compose.yml`
2. **Проксирование (Proxy)** — через HTTP/SOCKS5 прокси Mihomo (`127.0.0.1:7890`):
   - Файл: `provisioning/compose-templates/proxy.compose.yml`
   - На хосте для запуска отдельных команд через прокси:
     ```bash
     proxy-run curl https://ifconfig.me
     proxy-shell
     ```
3. **Полный туннель (Full TUN / VPN)** — изоляция через сетевой неймспейс sidecar-контейнера Mihomo с kill-switch:
   - Файл: `provisioning/compose-templates/vpn.compose.yml`
   - При отказе VPN трафик наружу не утекает. Доступ к GPU RTX 3060 пробрасывается напрямую в контейнер приложения.
