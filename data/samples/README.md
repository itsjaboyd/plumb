# Sample exports

The CSVs in this directory are the **demo set** - seven files covering
seven real bank formats, with one month of overlapping activity across
checking, savings and three cards. `make demo` imports all of them.

They are safe to re-import: the importer dedupes on
`(account_id, external_id)`, so running it twice changes nothing.

| Subdirectory | What it holds |
|---|---|
| `formats/` | the same shapes as single-row examples, for reading rather than importing |
| `edge-cases/` | a file with unquoted commas and a ragged file, to see how failures are reported |
| `budget/` | long and wide budget spreadsheets for `make import-budget` |

Nothing here is real. Delete the whole directory once you have your own
data in - `make demo` is the only thing that reads it.
