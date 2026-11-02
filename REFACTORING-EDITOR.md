# Рефакторинг редактора — анализ и план

> Статус: **только анализ, изменений в коде нет.** Документ перечисляет, что и почему стоит
> поправить, с конкретными местами, рисками и порядком работ. Ни одна правка ниже ещё не внесена.

---

## 1. Резюме

Редактор в целом спроектирован хорошо: `src/core` без Qt, тонкие презентеры, виджеты только с
сигналами, единая точка правды о правках в `RomSession`, аккуратная работа с квантованием и
общими осями. Это не «переписать всё», а точечная чистка.

Главные источники сложности, которые я нашёл:

1. **Ось карты представлена тремя разными способами** — строка `"x"/"y"` (core), `Qt.Orientation`
   (UI), индексы/row-col (таблица), и выбор «та или эта ось» дублируется в ~7 файлах.
2. **Две почти одинаковые реализации «ближайшее сырое значение»** — скалярная `encode_value`
   и векторная `encode_values` разошлись в поведении.
3. **`apply_transfer` — монолит в ~170 строк** с вложенным замыканием и несколькими фазами.
4. **`MainPresenter` (641 строка)** держит открытие/сохранение, перенос, переключатели, единицы,
   ИИ, логгер и заметки.
5. **`MainWindow` использует ручные колбэки `on_*`** вместо идиоматичных `pyqtSignal`.
6. Мелкий мёртвый код и `getattr`/`setattr` по f-строкам.

Ниже — карта кода, разбор каждого пункта и пошаговый план с рисками.

---

## 2. Карта редактора сейчас

Поток данных одной правки ячейки:

```
MapTableModel.setData           (число в модели, optimistic-запись)
  └─ cell_edited ─────────────────────────────► MapTableView.cell_value_changed
                                                 └─ MapWindow._on_table_cell_changed
                                                    (транспонирование _Mode.ROW)
                                                    └─ MapWindow.cell_value_changed(map_id, row, col, v)
                                                       └─ MainPresenter.on_cell_changed
                                                          └─ RomSession.write_cell
                                                             └─ EcuParser.write_cell
                                                                └─ rom_io.write_table_cell → stored
                                                          └─ EditorView.update_cell  (показ stored)
```

Слои и файлы:

| Слой | Файл | Строк | Назначение |
|---|---|---|---|
| core | `session.py` | 193 | checkpoint, сценарии записи, save, revert, transfer |
| core | `parser.py` | 274 | ROM-образ + definition, чтение/запись, общие оси |
| core | `rom_io.py` | 403 | низкоуровневое чтение/запись, кодирование, float endian |
| core | `models.py` | 154 | pydantic-модели таблиц/осей/чекпойнтов |
| core | `units.py` | 193 | пересчёт единиц definitions → метрические |
| core | `checkpoint.py` | 88 | сравнение с checkpoint (ecu_lookup/resample) |
| core | `transfer.py` | 548 | план/применение/отчёт переноса карт |
| core | `xml_reader.py` | 456 | разбор ecu_defs.xml, мердж base/child, дополнения |
| ai | `tools.py` | 315 | операции MCP (read/plan/apply) |
| presenters | `main_presenter.py` | 641 | связка окна ↔ сессии |
| presenters | `ai_presenter.py` | 203 | мост MCP-потоков в GUI-поток |
| ui | `main_window.py` | 439 | меню, диалоги, колбэки |
| ui | `editor_view.py` | 423 | дерево карт, окна карт, график |
| ui/map | `window.py` | 521 | окно одной карты, режимы, шаги, checkpoint |
| ui/map | `model.py` | 290 | QAbstractTableModel, checkpoint/baseline/цвет |
| ui/map | `view.py` | 326 | QTableView, выделение, буфер обмена |
| ui/map | `header.py` | 261 | заголовки осей, copy/paste оси, выделение точек |
| ui/map | `delegate.py` | 74 | фон по значению, рамка checkpoint, ввод |
| ui/map | `cells.py` | 110 | формат чисел, цвета, валидатор |
| ui/map | `constant_editor.py` | 131 | поле для 1×1 |

---

## 3. Найденные проблемы

### 3.1 Выбор «той самой оси» дублируется в 7 файлах

Строка `"x"/"y"` транслируется в конкретное поле/объект одним и тем же паттерном, но каждый раз
заново:

- `src/core/parser.py:190`, `:220`, `:229` — `table.x_axis if axis == "x" else table.y_axis`.
- `src/core/parser.py:182-202` — `shared_axis_users` снова разбирает «x это x_axis».
- `src/core/units.py:114-115` — `self.x if name == "x" else self.y`.
- `src/ai/tools.py:93-102` и `:243-250` — четыре пары `x_* if axis == "x" else y_*` подряд.
- `src/presenters/main_presenter.py:639-641` — `_orientation(axis)` модульный помощник.
- `src/presenters/ai_presenter.py:159` — `(x_new if point.axis == "x" else y_new)`.

Это не только дублирование: «x/y» как строка без перечисления допускает опечатки, которые ловятся
только в рантайме. В `tools.py` список полей (`x_axis`/`x_labels`/`x_title`/`x_shared_with`)
подбирается вручную каждый раз.

**Что предлагаю:** единый тип `Axis` (enum, `X`/`Y`) и два помощника:

- на `TableDef` (или свободная функция в `models.py`/`parser.py`): `axis_def(axis)` и
  `axis_of(table: CalibrationTable, axis)` для значений/подписей/заголовков/шагов;
- в UI: единая функция `orientation(axis)` и обратная `axis_of(orientation)` вместо локальных
  `_orientation`/`_display_orientation`/`_on_table_axis_changed`-разборов.

`AxisPointChange.axis`, `AxisWrite.shared` и т.п. перейдут со строки на enum (свойство/строку для
JSON в `ai/tools.py` можно сохранить).

### 3.2 `encode_value` и `encode_values` — две реализации «ближайшего сырого»

- `src/core/rom_io.py:222-267` — скалярная `encode_value` (с «сдвигом» от застревания на грубой шкале).
- `src/core/rom_io.py:320-360` — векторная `encode_values` (без сдвига, `searchsorted`).

Логика почти совпадает (перебор `_raw_lookup`, поиск ближайшего, fallback на `to_byte`), но
детали разошлись: у скалярной есть «если ближайшее == текущее, взять соседнее в нужную сторону»,
у векторной — нет. Это намеренно (пакетный перенос не должен сдвигать), но разница закодирована
дублированием, а не параметром, и её легко сломать при правке одной из двух.

**Что предлагаю:** один векторный движок `encode_values(values, scaling, storagetype, current_raw, *, nudge=False)`,
а `encode_value` — тонкая обёртка над ним с `nudge=True`. Оба теста `test_rom_io.py` и поведение
переноса при этом обязаны остаться зелёными.

### 3.3 `apply_transfer` — монолит

`src/core/transfer.py:265-434` — одна функция на ~170 строк: план осей, запись осей, сбор
«пересчитанных соседей», запись значений, отклонение, отчёт. Внутри — вложенное `snapshot`,
мутабельные `desired`/`changed_axes`/`notes`/`before`. Фазы уже помечены комментариями `1/2/3`,
но не вынесены.

**Что предлагаю:** разбить на приватные функции-фазы с явными аргументами/возвратами:

- `_desired_axes(...) -> dict[address, (axis_def, values, owner)] + notes`;
- `_write_axes(...) -> (changed_axes, stored)`;
- `_resample_selected(...)` / `_resample_recalculated(...)`;
- `_build_report(...)`.

Это не меняет алгоритм, только дробит его на проверяемые куски. Заодно проще добавить отдельный
тест на фазу интерполяции (сейчас отдельного `test_transfer.py` нет — см. §6).

### 3.4 `MainPresenter` — «бог-объект»

`src/presenters/main_presenter.py` (641 строка) отвечает за: открытие ROM, загрузку definitions,
сохранение/Save As, отмену при закрытии, перенос карт, переключатели, единицы, язык, ИИ-сервер,
заметки, логгер. Это оркестрация, и она тут уместна, но несколько независимых подсистем можно
выделить, не ломая слойность:

- **Save/диалог отмены** (`save_rom`, `save_rom_as`, `_save_to`, `_can_save`, `_confirm_discard`,
  `next_save_stem`, копирование заметок при Save As) → `SaveController` или `SaveService`.
- **Перенос карт** (`transfer_maps`) → `TransferController`.
- **Переключатели** (`show_switches`, `_fill_switches`, `on_switch_toggled`) → `SwitchesController`.

`MainPresenter` останется композитором, который подключает окно к этим контроллерам. Ключевое —
не создавать новые зависимости между контроллерами; они получают `RomSession`/`EditorView`/`AppSettings`
через конструктор.

### 3.5 `MainWindow` — ручные колбэки вместо сигналов

`src/ui/main_window.py:60-81` — 18 полей `on_*: Callable | None`, каждое со своим `_emit_*`
(`:315-435`). Это «ручные сигналы»: по-идиоматичному Qt те же точки — `pyqtSignal`, и презентер
подключается `window.save_requested.connect(...)`.

**Что предлагаю:** заменить пары `on_*`/`_emit_*` на `pyqtSignal` (без аргументов или с нужными),
оставив имена близкими (`save_rom_requested`, `transfer_maps_requested`, …). Плюс: типизация,
меньше строк, легче ловить «никто не подключил». Минус: механическая правка ~10 подключений в
`MainPresenter.__init__` и меню. Делать отдельным коммитом.

### 3.6 `getattr`/`setattr` по f-строкам в `TableUnits.table`

`src/core/units.py:135-155` строит поля `f"{name}_axis"`, `f"{name}_increments"`, `f"{name}_title"`,
`f"{name}_decimals"` для `name in ("x", "y")`. Работает, но не проверяется типизатором и плохо
читается; связка «имя ↔ поле» живёт в строке.

**Что предлагаю:** маленькие явные структуры или методы `CalibrationTable.axis_fields(name)` /
`axis_increments(name)` / `axis_decimals(name)` (сосед по смыслу с предложением 3.1), чтобы в
`TableUnits.table` не было динамических имён атрибутов.

### 3.7 Мёртвая ветка в `read_raw_values`

`src/core/rom_io.py:67-71` — `if size == 1: ... else: ...`, и обе ветки делают одно и то же
`(raw,) = struct.unpack(fmt, chunk)`. `_struct_format` уже нормализует однобайтовые форматы, так
что ветка не нужна.

### 3.8 Транспонирование `_Mode.ROW` размазано по `MapWindow`

2D-вектор в ROM хранится столбцом (N×1), а показывается строкой (1×N). Для этого `MapWindow`
хранит `_Mode.ROW` и вручную переставляет оси в шести местах: `show_table` (`window.py:238-258`),
`update_cell` (`:271-279`), `update_axis`/`_display_orientation` (`:281-286`, `:391-397`),
`_on_table_cell_changed`/`_on_table_axis_changed` (`:417-427`), `graph_data`, `_transpose_table`/
`_transpose_checkpoint` (`:492-521`).

**Что предлагаю:** собрать перестановку в один слой — например, объект-адаптер «экран ↔ ROM»
с методами `to_screen(table/checkpoint/cell/axis)`, `to_rom(...)`, `orientation(...)`, который
`MapWindow` использует во всех местах. Это самая рискованная из «структурных» правок (здесь легко
напутать row/col), поэтому делать в самом конце, с ручной проверкой 2D-векторов и графика.

### 3.9 Три представления одной оси

Сквозной вопрос: ось бывает строкой `"x"/"y"` (core, AI), `Qt.Orientation` (UI) и парой
«row/col» (таблица). Переводы живут точечно (`_orientation` в презентере, `_display_orientation`
в окне, `orientation == Horizontal` в `view.py`/`model.py`/`header.py`). Вместе с 3.1 это единый
рефакторинг: завести один enum/маппинг в `src/ui` (или в core без Qt — ориентация это понятие
карты, не Qt) и использовать его везде. `Qt.Orientation` останется только там, где он реально
нужен виджетам.

### 3.10 Мелочи

- `src/presenters/main_presenter.py:456-457, 491-494` — подсчёт `decimals` и форматирование
  `f"{stored:.{d}f}"` повторяется в `on_cell_changed` и `_on_axis_changed`; вынести `_fmt(value, conv)`.
- `MapWindow._on_axis_selection_changed` (`window.py:399-415`) — переключение шагов «значения ↔ ось»
  с ручным запоминанием `_value_steps`; можно упростить, храня шаги явно.
- `parser.list_maps()`/`list_switches()` пересчитываются при каждом вызове (`list_switches` ещё и
  читает байты на каждый switch). Кэш на уровне `EcuParser` (инвалидируется сменой `definition`)
  даст заметный выигрыш на больших прошивках; низкий приоритет.

---

## 4. План по фазам

Фазы упорядочены по «выигрыш/риск». Каждая — отдельный коммит, после которого `mypy` и `pytest`
должны проходить (см. §6).

### Фаза 0 — чистка без изменения поведения (низкий риск)

1. Убрать мёртвую ветку в `read_raw_values` (`rom_io.py:67-71`).
2. Вынести `_fmt(value, conv)` в презентере (дублирование `decimals`/`f-string`).

### Фаза 1 — унификация оси (`Axis` enum + помощники)

3. Ввести `Axis` (enum) и помощники выбора поля на `TableDef`/`CalibrationTable`/`AxisDef`.
   Прокинуть в `parser.py`, `units.py`, `tools.py`, `ai_presenter.py`.
4. Ввести единый маппинг `Axis ↔ Qt.Orientation` в UI; заменить локальные `_orientation`/
   `_display_orientation`.

   Риск: многоточечная, но механическая правка. Спасает то, что `test_session.py`,
   `test_units.py`, `test_ai_tools.py`, `test_map_model.py` покрывают ключевые пути.

### Фаза 2 — кодирование и перенос

5. Объединить `encode_value`/`encode_values` на общий векторный движок с флагом `nudge`
   (`rom_io.py`). Покрытие: `test_rom_io.py` (все кейсы, включая «не застревает» и «совпало —
   не трогаем») + перенос.
6. Разбить `apply_transfer` на фазы (`transfer.py`). Покрытие: `test_session.py::test_transfer_*`
   и `test_rom_integration.py`; при желании добавить `tests/editor/test_transfer.py` на интерполяцию.

### Фаза 3 — виджеты

7. `MainWindow.on_*` → `pyqtSignal` (`main_window.py`, `main_presenter.py`).
8. Убрать `getattr`/`setattr` из `TableUnits.table` (`units.py`).
9. (опционально) Кэш `list_maps`/`list_switches` в `EcuParser`.

### Фаза 4 — крупное (только если нужно)

10. Разбить `MainPresenter` на `SaveController`/`TransferController`/`SwitchesController`.
11. Вынести транспонирование `_Mode.ROW` в адаптер «экран ↔ ROM» внутри `MapWindow`.

    Пункты 10–11 меняют внутренности, но не API для пользователя. Делать их стоит только если
    после фаз 0–3 код всё ещё «не дышит»; риск выше (особенно 11 — row/col).

---

## 5. Что сознательно НЕ трогаем

- **`src/core` без Qt** — границу держим: enum оси и помощники не должны тянуть PyQt.
- **Семантику квантования** — «сдвиг от застревания» в `encode_value`, обратный endian float,
  `to_byte`-путь для float/uint32 остаются как есть (это поведение, а не стиль).
- **`RomSession` как единственное место правил данных** — сценарии записи/отката/переноса
  остаются там; рефакторинг презентера не переносит логику в виджеты.
- **Тексты и i18n** — русские `tr(...)` и английские пары в `src/locale/en.py` не трогаем
  (тест `test_i18n.py` следит за полнотой).
- **Логгер (`src/logger`)** — это отдельная подсистема; её проблемы в этот план не входят.
- **Перенос через `resample_grid` вместо `ecu_lookup`** — открытое решение из `CLAUDE.md`,
  к рефакторингу отношения не имеет.

---

## 6. Как проверять

После каждой фазы:

```bash
source .venv/bin/activate
mypy main.py src          # без ошибок
pytest                    # все тесты (editor + logger)
```

Точечно по затронутым областям:

```bash
pytest tests/editor/test_rom_io.py tests/editor/test_session.py tests/editor/test_units.py
pytest tests/editor/test_ai_tools.py tests/editor/test_map_model.py
pytest tests/editor/test_rom_integration.py   # только при наличии ecu/
```

Ручная проверка GUI (то, что тесты не ловят):

- 2D-вектор: открыть, поправить ячейку и ось, скопировать/вставить — не перепутаны row/col.
- Общая ось (например, ECT в AZ1G301F): правка оси подсвечивает соседей и меняет ось у них.
- Единицы: переключить метрические — значения/оси/шаги пересчитаны, checkpoint рамок корректен.
- Перенос карт: размерность не совпадает — интерполяция, отчёт `.md` рядом с файлом.
- Предложение правки ИИ: предпросмотр на графике, подтверждение, отказ.

---

## 7. Открытые вопросы

- Стоит ли `Axis` enum класть в `core/models.py` (без Qt) и транслировать в `Qt.Orientation`
  только на границе UI, или сразу в `src/ui`? Я склоняюсь к первому — понятие оси принадлежит
  модели карты, а не виджетам.
- Делать ли `test_transfer.py` отдельным шагом (сейчас перенос покрыт только косвенно через
  сессию и интеграционный тест) — рекомендую да, до фазы 2.
- Фаза 4 (`MainPresenter` → контроллеры) — нужна ли вообще? После фаз 0–3 презентер станет
  заметно короче уже за счёт удаления дублей; возможно, делить его не придётся.

---

*План подготовлен по состоянию кода на момент анализа; номера строк могут «поехать» после
первых же правок — перед применением сверяться с текущим файлом.*
