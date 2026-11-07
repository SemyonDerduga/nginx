# Производительность и воспроизведение

База — [официальный NGINX 1.31.8](https://github.com/nginx/nginx/commit/140aa942711868a8daeb95810bc9a81f272ada72). В [HTTP/1-парсере](../../src/http/ngx_http_parse.c) SSE2 просматривает по 16 байт значения заголовка, пока не встретится байт ≤ 0x20. Штатный автомат обрабатывает этот байт и короткий остаток. В [модуле upstream random](../../src/http/modules/ngx_http_upstream_random_module.c) при единичных весах всех серверов результат случайной выборки уже равен индексу. Смешанные веса используют исходный двоичный поиск.

## Сквозной эксперимент с парсером

Linux x86-64, AMD EPYC 9V74, GCC 14.2, один worker NGINX. Клиент и HTTP-бэкенд закреплены за другими CPU на той же машине. HTTP/1.1 keepalive, loopback, ответ 200 с двухбайтовым телом, без TLS и access log. Восемь пар по 1,5 с на вариант с чередованием порядка. Приведены медианы парных относительных изменений; во всех пяти сценариях проверено 8 696 915 ответов. В этой серии был включён только патч парсера.

| Запрос | Прямой ответ | Reverse proxy |
| --- | --- | --- |
| Только Host | +1,9% (5/8 пар) | — |
| Host, User-Agent, Accept, Accept-Encoding и небольшая Cookie | −1,3% (2/8) | +1,1% (5/8) |
| Host и Cookie из 2 048 одинаковых байтов без пробелов | +15,9% (7/8) | +12,0% (8/8) |

Контроль A/A из четырёх пар на сценарий дал разброс отдельных сравнений от −9,9% до +7,4%. На коротких заголовках выигрыш не установлен. При фиксированных 25 000 запросов/с с длинной Cookie p50 снизилась с 18,29 до 14,12 мкс. Улучшение p99 не подтвердилось. Условия теста благоприятны для SIMD. Профили HTTP/2 и HTTP/3, TLS и сетевой трафик рабочих сервисов в эти замеры не входили.

## Проверка балансировщика и keepalive

На 9 840 наборах весов длиной 1–8, с весами 1–3, все 147 624 результата выбора совпали. При 256 серверах с весом один исключаются восемь сравнений в таблице диапазонов, остаётся проверка флага. Сквозной RPS для этого изменения не измеряли.

В штатном NGINX один клиент последовательно обходил 33 backend-адреса с обычным round robin. После прогрева пул на 32 записи открыл 264 новых TCP-соединения на 264 запроса; установка `keepalive 64 local;` в том же `upstream` устранила все 264 открытия. Это наблюдение о конфигурации. Полезная ёмкость пула зависит от параллелизма, числа воркеров и порядка обращений. [Исходники keepalive](../../src/http/modules/ngx_http_upstream_keepalive_module.c)

## Сборка и тесты

Из корня клона выполните команды на Linux с GCC, make, Perl, заголовками OpenSSL, zlib и PCRE2. Файл `auto/configure` находится в Git-репозитории. Для тестов используются локальные временные серверы.

```sh
git switch perf/1.31.8
git worktree add --detach ../nginx-base 140aa942711868a8daeb95810bc9a81f272ada72
for tree in . ../nginx-base; do
  (cd "$tree" && auto/configure --with-http_ssl_module --with-http_v2_module --with-cc-opt="-O2 -g -fno-omit-frame-pointer" && make -j4) || exit 1
done
git clone https://github.com/nginx/nginx-tests.git ../nginx-tests
git -C ../nginx-tests checkout --detach 16964ee752509eb6271c4e8760e7899274240755
bash docs/performance/check.sh ../nginx-tests "$PWD/objs/nginx"
```

На объединённой сборке выполнено 283 проверки из 12 официальных файлов: 280 прошли, три дали ожидаемый для этой версии результат TODO. Неожиданных отказов и пропусков нет. Отдельный корпус парсера прошёл 213 420 сравнительных случаев, включая 29 580 случаев с защищённой страницей после буфера.

## Нагрузочный стенд и данные

Из корня форка после сборки создайте новый каталог для результатов, скомпилируйте оба клиента и запустите парные измерения. Укажите свободные CPU и используйте машину без конкурирующей нагрузки. Архивные проценты относятся к сборке с одним патчем парсера `2dbc6a0af4edc36597fa54274837bebd3e7939ed`; команды ниже проверяют текущую ветку с двумя патчами. В наборе нагрузок используется один upstream с обычным round robin, поэтому режим random в этом тесте не задействован.

```sh
LAB=docs/performance
BASE=$(realpath ../nginx-base/objs/nginx)
CANDIDATE=$(realpath objs/nginx)
mkdir -p rerun
cc -O2 -pthread "$LAB/throughput/load.c" -o "$LAB/throughput/load"
cc -O2 "$LAB/latency/rate.c" -o "$LAB/latency/rate"
python3 "$LAB/throughput/run.py" "$BASE" "$CANDIDATE" --output rerun/e2e.csv
python3 "$LAB/throughput/run.py" "$BASE" "$BASE" --ctrl --rounds 4 --seconds 1 --output rerun/aa.csv
python3 "$LAB/throughput/summarize.py" rerun/e2e.csv
python3 "$LAB/latency/run.py" "$BASE" "$CANDIDATE" --out rerun/latency
python3 "$LAB/latency/summary.py" rerun/latency/fixed.csv
```

Исходные агрегаты по каждому запуску: [A/B](throughput/results/e2e.csv), [A/A](throughput/results/aa.csv), [фиксированная нагрузка](latency/results/fixed.csv). Клиенты проверяют статус, длину и тело ответа. При фиксированной нагрузке сохраняются отдельные времена запланированной отправки, фактической отправки и завершения запроса.

## Дополнительные проверки

```sh
python3 docs/performance/parser/prepare.py ../nginx-base .
cc -O2 -Wall -Wextra docs/performance/parser/compare.c -o rerun/parser-check
rerun/parser-check
python3 docs/performance/upstream/check_random.py ../nginx-base .
python3 docs/performance/upstream/check_pool.py "$CANDIDATE"
```

В быстром автономном тесте парсера используются минимальные структуры стенда. Для сборки проверки с реальными типами добавьте `-DUSE_NGINX_HEADERS`, `-Iobjs` и include-пути NGINX из `objs/Makefile`. Проверка random извлекает функции выбора из обоих исходных деревьев; проверка keepalive обращается к реальным серверным процессам. [Наблюдаемые соединения](upstream/observed.json).

## Гипотезы для следующих экспериментов

В `ngx_http_proxy_create_request` повторяется поиск исключений по заголовкам при расчёте длины и сериализации. В TLS можно исследовать пропуск копии полного буфера при сохранении семантики повторной записи. У файлового кеша стоит измерить конкуренцию за mutex при росте числа workers. Для этих трёх гипотез сквозной выигрыш ещё предстоит измерить.
