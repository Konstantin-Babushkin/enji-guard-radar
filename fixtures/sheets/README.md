# Settings sheet fixtures

`values_batch_get.synthetic.json` is SYNTHETIC: written by hand in the shape of a Google Sheets
`spreadsheets.values.batchGet` answer (`valueRanges[].range` / `values`, with Google's habit of
dropping trailing empty cells and keeping empty rows as `[]`). It is not a recording: a real
answer needs a service account key and a shared sheet, which `make verify` never has. Replace it
with a recorded one (secrets and real ids removed) the first time an operator captures one.
