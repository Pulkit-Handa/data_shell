# Examples

This directory contains example contracts, sample data, scripts, and notebooks to help you get started with Data Shell.

## Contracts

- [vendor_settlements_contract.json](vendor_settlements_contract.json) - Vendor settlements contract demonstrating row rules, batch rules, and delivery configuration
- [nyc_taxi_contract.json](nyc_taxi_contract.json) - NYC taxi data contract example

## Samples

- [samples/vendor_settlements_1k.jsonl](samples/vendor_settlements_1k.jsonl) - 1,000-row sample dataset matching the vendor settlements contract (900 valid, 100 seeded defects)
- [samples/README.md](samples/README.md) - Details about the sample dataset

## Scripts

- [quickstart.sh](quickstart.sh) - 60-second walkthrough script (runs `datashell demo` and shows ledger summary)
- [render_demo_gif.sh](render_demo_gif.sh) - Generate the demo GIF from real CLI output using ImageMagick
- [demo.tape](demo.tape) - VHS tape file to generate the demo GIF (alternative to render_demo_gif.sh)
- [demo_run.py](demo_run.py) - Programmatic demo script

## Notebooks

- [notebook/demo.ipynb](notebook/demo.ipynb) - Google Colab-ready notebook demonstrating the full workflow

## Generating the Demo GIF

To regenerate `docs/demo.gif` without VHS:

```bash
./examples/render_demo_gif.sh
```

To regenerate with VHS (requires [VHS](https://github.com/charmbracelet/vhs)):

```bash
vhs examples/demo.tape
```
