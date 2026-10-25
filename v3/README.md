# LUMIO 3.0 Preview — local school-order review

**Experimental preview, not a finished AI sorter.** No student photos or real worksheets are stored in this public repository.

## Features
- Local single-window review in the browser at `http://127.0.0.1:8765`; click photo tiles to select/deselect, approve each student's portraits and friends separately, move to the next person.
- Each student section is labeled by surname and given name, not just an index. Show neighboring frames as unchecked context.
- The five worksheet anchors (V/P1/P2/D1/D2) are mandatory; they must be corrected in the worksheet if wrong.
- Teacher series are provided in `teachers.csv` with full name, role and Start/End frame numbers. Select up to 5 frames; no automatic `_02`/`_03` suffixes.
- One background file can be specified manually.
- Source RAW files are **never modified**. Export is blocked until every section is approved.
- Recognizes common RAW extensions: ARW, CR2, CR3, NEF, NRW, RAF, RW2, ORF, DNG, PEF and more. Supports filenames ending with frame numbers; ambiguous numbers block export.
- Copies confirmed RAW into `<output>/<order>/<school>/<date>/<class>/` with categories `01_ДОКУМЕНТЫ`, `02_ЗДАНИЕ`, `03_ОБЩАЯ`, `04_РЕПОРТАЖ`, `05_УЧЕНИКИ`, `06_УЧИТЕЛЯ`, `07_ФОН`.

## Start on Windows
Open PowerShell in the `v3` folder and run:

```powershell
py -3.11 lumio3.py --source "D:\DCIM\100MSDCF" --output "F:\LUMIO_TEST" --order 7470 --school "Вешняковская" --date "09.10.2026" --class-name "9Л"
```

First replace illustrative `students.csv` with verified worksheet data. Use an **empty test destination**. Browser opens automatically; Ctrl+C stops the server. No third-party dependencies are required.

## Not implemented yet
- **AI face identification and pose clustering** from v2.1 are not integrated into v3 Preview. Extra frames start unchecked; review is manual.
- RAW-embedded previews (matching JPEG/PNG/WebP sidecars required).
- Automatic teacher-series detection.
- Populating documents, building, group and reportage categories (empty folders are created).
- Native background file picker (currently enter full file path).

Only use locally, do not expose the review server to the network.