# Arimo

`arimo.woff2` is a subset of the variable Arimo font from
[Google Fonts](https://github.com/google/fonts/tree/main/ofl/arimo), distributed under
[the SIL Open Font License](OFL.txt). The site serves it locally.

It keeps Basic Latin, Latin-1, General Punctuation, the arrows (U+2190–21FF), and the two
other characters the pages use: the subscript two of CO₂ (U+2082) and the box-drawing line
(U+2500). Outlines, widths, layout features and the weight axis (400–700) are the
original's. FontTools 4.68.0 cut the subset from the full WOFF2 that FontTools 4.67.0 had
encoded from the source TTF. The same command takes the TTF:

```sh
pyftsubset 'Arimo[wght].ttf' --output-file=arimo.woff2 --flavor=woff2 --layout-features='*' \
  --unicodes='U+0020-007E,U+00A0-00FF,U+2000-206F,U+2082,U+2190-21FF,U+2500'
```

A character outside the subset falls back to Helvetica or Arial. Before a page uses a new
one, add it to `--unicodes` and cut the subset again from the source TTF.

Source TTF SHA-256: `e43898b143ec826ac8cb4034816458a7047fbe0836558de2a1f8c6223ae3e0ca`.
