# LUMIO

**4.23.0 · UNIT Multi-Layer alpha01** — фото-мастер хронология, отдельные устройства и offsets, RAW/JPEG-пары, видео, сохранённый отбор, PHOTO ACTIVITY и проверяемая последовательная выдача в существующей среде UNIT. [Процесс, проверки и ограничения](unit-alpha01/MULTILAYER-MVP.md). Ветка `feature/unit-multilayer-mvp`, тег `v4.23.0-multilayer-alpha01`.

Обновление **4.22.2-scene-view**: просмотр группового/школьного репортажа ограничен выбранной сценой; общий переключатель «Комплект / Вся серия»; заголовки строк не прилипают. [Описание](unit-alpha01/SCENE-VIEW.md). Ветка `fix/reportage-scene-view`, отдельный тег `v4.22.2-scene-view`.

Программа для школьного фотографа: от карты памяти до готового заказа. **LUMIO — основной продукт.** Школьные фотокомпании (Fobox, Crous и любые другие) — это компании внутри LUMIO, каждая со своими правилами отбора и структурой папок. Их можно добавлять сколько угодно.

## Новая экспериментальная версия · 4.22.1 · UNIT processing α01

Среда **UNIT · обработка α01** добавлена в привычный переключатель LUMIO: проверяемый импорт с нескольких карт, ручной отбор, настоящая RAW-проявка, параметры света/цвета, JPEG 5000 px, мультимедийная сборка и экспорт. Рабочие школьные среды сохранены. [Описание и ограничения](unit-alpha01/UNIT-PROCESSING.md).

Исходники находятся в `unit-alpha01/`, ветка `feature/unit-processing-alpha01`, тег `v4.22.1-unit-processing-alpha01`. Ретушь и автоматическое согласование цвета разных сцен ещё не подключены. Это альфа, не замена зафиксированного стабильного снимка ниже.

## LUMIO 4.20.1 — зафиксированная стабильная версия (папка v4)

Локальная программа для школьного фотографа: импорт карты → проверка брака → автоматические комплекты по ведомости (15–20 кадров на ребёнка, учителя, общая фотография, репортаж) → экспорт в структуру заказа. Отдельная среда **Crous**: лента с разделителями и заливка на Яндекс.Диск со ссылкой.

- Запуск на Windows: двойной щелчок по `v4/Запустить LUMIO.bat`.
- Подробно: [v4/README.md](v4/README.md).
- Что изменилось: [v4/CHANGELOG.md](v4/CHANGELOG.md).

## Зафиксированный релиз v4.20.1

- **Стабильный снимок:** [страница релиза v4.20.1](https://github.com/u001stm/lumio/releases/tag/v4.20.1-frozen).
- Версия восстановлена и проверена 26.10.2026. 52 теста: 51 успешно, 1 пропущен.
- Светлая и тёмная темы, компактная шапка, выбор по галочкам, просмотр колесом и рукой, сравнение пары кадров.
- Импорт неполной ведомости, поиск лиц по всей съёмке, добавление учителя без повторного импорта.
- Экспорт на Яндекс.Диск или компьютер; **XMP не генерируются**.
- Для каждого основного группового кадра — до 5 дополнительных, отдельной строкой ниже.
- Снимок релиза, его тег и архив не заменять. Следующие изменения выпускаются с новым номером версии.

Подробности фиксации: [RELEASE_LOCK.md](RELEASE_LOCK.md). Каталог съёмок, фотографии и токены в репозиторий не включены.

## LUMIO 3.0 Preview

New local single-window review and order assembly prototype: [v3/README.md](v3/README.md). **Not yet AI-enabled**; manual review is required. The previous v1/v2 versions remain available for comparison.

## LUMIO 1.0 — RAW sorter (PowerShell)

Local RAW sorting for school photo delivery: `sort_raw.ps1`.

## Features

- Reads `Name,V,P1,P2,D1,D2` from UTF-8 CSV.
- Finds Sony RAW files using a photographer-specific three-letter Latin surname prefix, such as `ANTxxxxx.ARW` for Antonov. `ANT` is an example, **not a universal prefix**; another photographer may use `IVN`, `PET`, etc. The script currently recognizes `ANT` only; configurable prefix support is planned.
- Always includes five required RAW files and adds nearby frames (±8 frame numbers), up to 18 total per student by default.
- Renames **only copied selected files**: `Фамилия Имя_<PREFIX>xxxxx.ARW`, `ЛР_Фамилия Имя_<PREFIX>xxxxx.ARW`, `ЛС_Фамилия Имя_<PREFIX>xxxxx.ARW`, `Друг1_<PREFIX>xxxxx.ARW`, `Друг2_<PREFIX>xxxxx.ARW`. Here `<PREFIX>` means the photographer's three-letter surname code (e.g. `ANT`).
- Other copied RAW files keep original filenames; source files are never modified.
- Checks missing required frames, output folders and free space before confirmation.

**Limitations:** v1 selects neighbors by filename number, not visual similarity, identity, or pose. Review the output for photos of other students, white frames and exposure-bracketing duplicates. It requires five distinct frame numbers per student. Do not upload student photographs or real worksheets to this public repository.

## Run on Windows

1. Save the real worksheet as UTF-8 CSV with columns `Name,V,P1,P2,D1,D2`. Put it alongside `sort_raw.ps1` or specify its full path. `Name` may contain patronymic; only first and last name are used in output.
2. Open PowerShell in the script directory and run:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\sort_raw.ps1 -Source 'D:\DCIM\100MSDCF' -Destination 'F:\RAW_SORTED' -Manifest '.\students.csv'
```

3. Review the dry-run totals. Type uppercase `YES` to copy. Existing student output folders are not overwritten.

Use UTF-8 **with BOM** when saving `sort_raw.ps1` locally for Windows PowerShell 5.1 to read Cyrillic correctly. PowerShell 7 reads UTF-8 directly.

## Version 2 roadmap

- Analyze local JPEG previews without uploading photos to external services.
- Exclude all-white/black technical frames and exposure-bracketing duplicates.
- Identify diverse poses and compositions around selected frame numbers; keep required five.
- Treat 16–22 RAW per student as a **typical range, not a hard cap**. Keep fewer if there are fewer distinct useful frames, or more (including 25–30+) when additional poses/compositions are genuinely different. Always retain the five selected frames, and review ambiguous matches.
- Produce audit report and allow interactive approval before copying.

All real photos and personal data remain local.

## Camera-independent architecture (LUMIO 2.x roadmap)

LUMIO is intended for **multiple photographers and camera manufacturers**, not just Sony or Antonov. `ANT` and `.ARW` are examples only.

- RAW formats to support via suitable decoders: ARW, CR2, CR3, NEF, NRW, RAF, RW2, ORF, DNG, PEF, 3FR and others where supported.
- Filenames may use arbitrary camera-generated patterns (e.g. `ANT01750.ARW`, `DSC_1750.NEF`, `IMG_1750.CR3`, `DSCF1750.RAF`). Do not infer a photographer's identity from the prefix.
- Resolve worksheet selections against unique file identifiers, not just frame numbers; detect and ask to resolve collisions.
- Use sidecar JPEGs or RAW-embedded previews for analysis when supported; export the **original RAW format** without conversion.
- Keep the original filename as part of the copied filename; never rename source files.

**Implementation status:** v1 and current experimental `v2/sort_v2.py` still support only `ANT`-style three-letter-prefix ARW/JPG pairs (v2 can autodetect the three letters). Universal camera support described here is a requirement for future development, **not a feature already implemented**.
