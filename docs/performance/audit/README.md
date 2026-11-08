# Linux x86-64, проверка ревизии 65284d1

В этой серии сравнивались официальный NGINX 1.31.8, первая версия форка и версия до исправления определения единичных весов. Для длинного query с JWT медиана парных изменений RPS от первой версии форка до 65284d1 составила +10,3% в основном опыте и +9,2% в отдельном повторе (по шесть пар). Обычный браузерный прокси дал +0,8% и +0,4%. Все четыре числа получены при одинаковом уровне -O2. Исторические измерения входят в этот каталог вместе с кодом стенда.

| CSV | Сравнение |
| --- | --- |
| [linux.csv](results/linux.csv) | Пять сборок и второй экземпляр того же HEAD как контроль A/A, шесть сценариев, 216 прогонов по 1,1 с. |
| [confirm.csv](results/confirm.csv) | Прежний форк против нового при -O2, три сценария, 36 прогонов по 2 с. |
| [logbuffer.csv](results/logbuffer.csv) | Один бинарник 65284d1, меняется только буферизация access log, 36 прогонов по 1,3 с. |

В каждом CSV `round` объединяет варианты в пару для выбранного сценария, `order` задаёт порядок запуска. Сначала вычисляется отношение показателей внутри одной пары, затем медиана этих отношений. CPU относится к процессу frontend. Виртуальная машина с девятью доступными логическими CPU, Intel Xeon Platinum 8573C, GCC 14.2.0, Linux 6.18.57. Один frontend-воркер, 12 keepalive-соединений HTTP/1.1 и TCP loopback. Генератор проверяет HTTP 200, Content-Length и тело OK. Процессы клиента, frontend и origin привязаны к разным CPU. Модель нагрузки использует URI и заголовки [ARM-спецификации](../arm64/spec-universal.json). Абсолютные RPS зависят от машины и планировщика.

## Как повторить

Выполните из корня полного клона форка. Нужны Linux, Python 3, GCC, make, заголовки OpenSSL, PCRE2 и zlib. Выберите новый каталог для исходников и CSV.

~~~sh
WORK=$(mktemp -d)
base=140aa942711868a8daeb95810bc9a81f272ada72
old=6f5052f0377d12286e8432ac1916c5d32f4c99c0
tested=65284d1bb40196e8dc62e5b8ea75e8dfa6188faf
git worktree add --detach "$WORK/base" "$base"
git worktree add --detach "$WORK/base-default" "$base"
git worktree add --detach "$WORK/old" "$old"
git worktree add --detach "$WORK/head" "$tested"
git worktree add --detach "$WORK/head-O1" "$tested"

for tree in base base-default old head head-O1; do
    flags="-O2 -g -fno-omit-frame-pointer"
    case "$tree" in base-default|head-O1) flags="-O1 -g -fno-omit-frame-pointer" ;; esac
    (cd "$WORK/$tree" && auto/configure --with-http_ssl_module --with-http_v2_module --with-stream --with-threads --with-cc-opt="$flags" && make -j4) || exit 1
done
cc -O2 -pthread docs/performance/throughput/load.c -o "$WORK/load"
runner=docs/performance/audit/linux_recheck.py
python3 "$runner" --sources "$WORK" --client "$WORK/load" --seconds 1.1 --rounds 6 --output "$WORK/linux.csv"
python3 "$runner" --sources "$WORK" --client "$WORK/load" --confirm --seconds 2 --rounds 6 --output "$WORK/confirm.csv"
python3 "$runner" --sources "$WORK" --client "$WORK/load" --buffer-check --seconds 1.3 --rounds 6 --output "$WORK/logbuffer.csv"
~~~

Для проверки более поздней ревизии задайте её SHA в `tested` перед созданием рабочих деревьев. Не сравнивайте новые цифры с архивными абсолютными значениями, измеренными на другом сервере. Для оценки эффекта сравните варианты внутри новой серии.
