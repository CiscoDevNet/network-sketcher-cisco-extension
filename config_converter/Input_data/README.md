# Input_data/

Place your running-config text files here. The converter accepts any readable
file under the directory you pass to `--input` (one device per file, multiple
devices per file, or a mix — see `DESIGN.md` section 4.1.1).

## Bundled reference sample

| Subdirectory | Description |
|---|---|
| [`sample1/`](sample1/README.md) | Phase-1 reference lab — 7 files / 13 devices across IOS, IOS-XE, NX-OS, IOS-XR, and ASA(FTD/FDM) |

Quick start:

```bash
python -m config_converter.src.convert \
    -i config_converter/Input_data/sample1/ \
    -o config_converter/Output_data/ns_commands.txt
```

Add your own sample folders as siblings of `sample1/` (e.g. `Input_data/my_site/`)
or point `--input` at any directory outside this tree.
